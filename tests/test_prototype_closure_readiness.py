from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

from control_fabric_core.artifact_registry import ArtifactRegistryAuthorizer
from control_fabric_core.db.models import LedgerEvent, PrototypeClosureReadinessRecord, metadata
from control_fabric_core.prototype_closure_authority import (
    ClosureSource, PrototypeClosureAuthority, PrototypeClosureRequestError,
    PrototypeClosureUnavailable, studio_digest,
)
from control_fabric_core.prototype_closure_evidence import (
    ClosureEvidenceLookup, OwnerBackedClosureEvidenceResolver,
)
from control_fabric_core.prototype_closure_policy import (
    VerifiedReference, evaluate_prototype_closure, resolve_evidence,
)
from control_fabric_core.prototype_closure_readiness import (
    PrototypeClosureNotFound, PrototypeClosureReadinessService,
    build_prototype_closure_readiness_runtime,
)
from control_fabric_core.prototype_maturity_contracts import artifact_digest, digest
from wgcf_api.app import create_app


REVISION = "a" * 40
OWNER = "repo://product-owner"
REPO = "repo://product-owner/sample-tool"
POLICY = {"actions": {
    "apply-delivery": {"from_lifecycle": "baseline-approved"},
    "graduate-source": {"from_lifecycle": "graduating"},
    "retire-incubation": {"from_lifecycle": ["exploring", "candidate", "baseline-approved", "graduating"]},
    "reopen-incubation": {"from_lifecycle": "retired"},
}}
MANIFEST = {
    "authority_commit": "b" * 40,
    "files": {"prototype-closure.yaml": "c" * 64},
    "security_review": {"merge_commit": "d" * 40},
}


def source(action: str, *, custody: str = "incubation-repo") -> ClosureSource:
    lifecycle = {
        "apply-delivery": "baseline-approved",
        "graduate-source": "graduating",
        "retire-incubation": "exploring",
        "reopen-incubation": "retired",
    }[action]
    record = {"id": "sample-tool", "lifecycle": lifecycle, "source_custody": custody}
    last_event = None
    if action == "apply-delivery":
        record.update(delivery_packet_ref="record://delivery-packets/sample-tool",
                      design_baseline_ref="record://design-baselines/sample-tool")
    if action == "graduate-source":
        record.update(project_phase="delivery-governed",
                      delivery_packet_ref="record://delivery-packets/sample-tool",
                      accepted_delivery_target_receipt_ref="oos://receipts/prototype-delivery-application/" + "f" * 64)
    if action == "reopen-incubation":
        record.update(retirement_ref="record://prototype-closure/sample-tool/history/retired",
                      closure_event_ref="record://prototype-closure/sample-tool/history/retired")
        last_event = {"event_type": "incubation-retired"}
    return ClosureSource(REVISION, record, studio_digest(record), lifecycle, custody, None, last_event)


def request(action: str) -> dict:
    value = {
        "schema_version": 2,
        "artifact_type": "prototype-closure-request",
        "request_id": f"prototype-closure-request:sample-{action}",
        "prototype_id": "sample-tool",
        "action": action,
        "expected_lifecycle": source(action).lifecycle,
        "expected_source_revision": REVISION,
        "operator_id": "mfshaf7",
        "correlation_id": f"corr-{action}",
        "idempotency_key": f"idem-{action}",
    }
    if action == "apply-delivery":
        value.update(accepted_baseline_receipt_ref="record://baseline/accepted",
                     accepted_delivery_target_receipt_ref="oos://receipts/prototype-delivery-application/" + "f" * 64,
                     target_kind="new-delivery-epic", target_delivery_ref="openproject://work_packages/100")
    elif action == "graduate-source":
        value.update(accepted_delivery_target_receipt_ref="oos://receipts/prototype-delivery-application/" + "f" * 64,
                     durable_owner_ref=OWNER, durable_repo_ref=REPO,
                     durable_owner_acceptance_ref="repo://product-owner/acceptance",
                     transfer_strategy="transfer")
    elif action == "retire-incubation":
        value.update(retirement_reason="The experiment is complete.",
                     retention_plan_ref="record://retention/sample-tool",
                     runtime_disposition_plan_ref="platform://runtime-disposition/sample-tool")
    else:
        value["prior_retirement_receipt_ref"] = "oos://closure/retired"
    return value


def proofs(action: str) -> dict[str, VerifiedReference]:
    req = request(action)
    def proof(field: str, owner: str, *, ref: str | None = None,
              subject: str | None = None, revision: str | None = None,
              packet: str | None = None, prototype_id: str | None = None) -> VerifiedReference:
        return VerifiedReference(ref or req[field], owner, "sha256:" + "e" * 64,
                                 "accepted", subject, revision, packet, prototype_id)
    if action == "apply-delivery":
        return {
            "accepted_baseline_receipt_ref": proof("accepted_baseline_receipt_ref", "operator-orchestration-service", subject="record://design-baselines/sample-tool", prototype_id="sample-tool"),
            "accepted_delivery_target_receipt_ref": proof("accepted_delivery_target_receipt_ref", "operator-orchestration-service", subject="openproject://work_packages/100", packet="record://delivery-packets/sample-tool", prototype_id="sample-tool"),
            "target_delivery_ref": proof("target_delivery_ref", "workspace-delivery-art"),
        }
    if action == "graduate-source":
        return {
            "accepted_delivery_target_receipt_ref": proof("accepted_delivery_target_receipt_ref", "operator-orchestration-service", packet="record://delivery-packets/sample-tool", prototype_id="sample-tool"),
            "durable_owner_acceptance_ref": proof("durable_owner_acceptance_ref", OWNER, subject=REPO),
            "source_transfer_receipt_ref": proof("source_transfer_receipt_ref", OWNER, ref="repo://product-owner/transfer", subject=REPO, revision=REVISION),
        }
    if action == "retire-incubation":
        return {
            "retention_plan_ref": proof("retention_plan_ref", "workspace-prototype-studio"),
            "runtime_disposition_plan_ref": proof("runtime_disposition_plan_ref", "platform-engineering"),
            "runtime_disposition_proof_ref": proof("runtime_disposition_proof_ref", "platform-engineering", ref="platform://runtime-disposition/absent", subject="platform://runtime-disposition/sample-tool"),
        }
    return {
        "prior_retirement_receipt_ref": proof("prior_retirement_receipt_ref", "operator-orchestration-service", subject="record://prototype-closure/sample-tool/history/retired"),
        "retained_source_readback_ref": proof("retained_source_readback_ref", "workspace-prototype-studio", ref="repo://workspace-prototype-studio/sample-tool", revision=REVISION),
    }


class FakeAuthority:
    def __init__(self, snapshot: ClosureSource):
        self.source = snapshot
        self.revision = snapshot.revision

    def snapshot(self, prototype_id: str) -> ClosureSource:
        if prototype_id != "sample-tool":
            raise PrototypeClosureRequestError("unknown prototype")
        return self.source

    def validate_request(self, revision: str, value: dict) -> tuple[dict, dict]:
        if revision != self.revision or value.get("artifact_type") != "prototype-closure-request":
            raise PrototypeClosureRequestError("invalid Closure request")
        return MANIFEST, POLICY

    def current_revision(self) -> str:
        return self.revision


class FakeResolver:
    def __init__(self, evidence: dict[str, VerifiedReference]):
        self.evidence = evidence
        self.available = True

    def resolve(self, request: dict, source: ClosureSource) -> dict[str, VerifiedReference]:
        if not self.available:
            raise RuntimeError("target unavailable")
        return self.evidence


class RecordingOwnerReader:
    def __init__(self, evidence: dict[str, VerifiedReference]):
        self.evidence = evidence
        self.lookups: list[ClosureEvidenceLookup] = []

    def read(self, lookup: ClosureEvidenceLookup) -> VerifiedReference | None:
        self.lookups.append(lookup)
        return self.evidence.get(lookup.field)


class PrototypeClosureReadinessTests(TestCase):
    def test_owner_readers_are_action_scoped_and_bind_lookup_context(self) -> None:
        for action in ("apply-delivery", "graduate-source", "retire-incubation", "reopen-incubation"):
            with self.subTest(action=action):
                expected = proofs(action)
                readers = {
                    owner: RecordingOwnerReader({
                        field: proof for field, proof in expected.items()
                        if proof.owner_ref == owner
                    })
                    for owner in {proof.owner_ref for proof in expected.values()}
                }
                actual = resolve_evidence(
                    OwnerBackedClosureEvidenceResolver(readers), request(action), source(action)
                )
                self.assertEqual(actual, expected)
                self.assertEqual(
                    {lookup.field for reader in readers.values() for lookup in reader.lookups},
                    set(expected),
                )
                for owner, reader in readers.items():
                    self.assertTrue(all(lookup.owner_ref == owner for lookup in reader.lookups))
                    self.assertTrue(all(lookup.source_revision == REVISION for lookup in reader.lookups))
                lookups = {
                    lookup.field: lookup
                    for reader in readers.values() for lookup in reader.lookups
                }
                if action == "apply-delivery":
                    self.assertEqual(
                        lookups["accepted_baseline_receipt_ref"].subject_ref,
                        source(action).record["design_baseline_ref"],
                    )
                    self.assertEqual(
                        lookups["accepted_delivery_target_receipt_ref"].subject_ref,
                        request(action)["target_delivery_ref"],
                    )
                    self.assertEqual(
                        lookups["accepted_delivery_target_receipt_ref"].source_packet_ref,
                        source(action).record["delivery_packet_ref"],
                    )
                if action == "retire-incubation":
                    self.assertIsNone(lookups["runtime_disposition_proof_ref"].requested_ref)
                    self.assertEqual(
                        lookups["runtime_disposition_proof_ref"].subject_ref,
                        request(action)["runtime_disposition_plan_ref"],
                    )
                result = evaluate_prototype_closure(
                    request(action), source(action), actual, POLICY, source(action).record_digest
                )
                self.assertEqual(result["outcome"], "ready")

    def test_owner_reader_absence_and_unaccepted_proof_fail_closed(self) -> None:
        req, src = request("apply-delivery"), source("apply-delivery")
        with self.assertRaises(PrototypeClosureUnavailable):
            resolve_evidence(OwnerBackedClosureEvidenceResolver({}), req, src)
        expected = proofs("apply-delivery")
        readers = {
            owner: RecordingOwnerReader({
                field: proof for field, proof in expected.items() if proof.owner_ref == owner
            })
            for owner in {proof.owner_ref for proof in expected.values()}
        }
        readers["workspace-delivery-art"].evidence.pop("target_delivery_ref")
        actual = resolve_evidence(OwnerBackedClosureEvidenceResolver(readers), req, src)
        result = evaluate_prototype_closure(req, src, actual, POLICY, src.record_digest)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("evidence-unavailable", {finding["code"] for finding in result["findings"]})

        for state in ("revoked", "stale", "denied"):
            with self.subTest(state=state):
                readers["workspace-delivery-art"].evidence["target_delivery_ref"] = replace(
                    expected["target_delivery_ref"], state=state
                )
                actual = resolve_evidence(OwnerBackedClosureEvidenceResolver(readers), req, src)
                result = evaluate_prototype_closure(req, src, actual, POLICY, src.record_digest)
                self.assertEqual(result["outcome"], "blocked")
                self.assertIn("evidence-unavailable", {finding["code"] for finding in result["findings"]})

        readers["workspace-delivery-art"].evidence["target_delivery_ref"] = replace(
            expected["target_delivery_ref"], ref="openproject://work_packages/999"
        )
        actual = resolve_evidence(OwnerBackedClosureEvidenceResolver(readers), req, src)
        result = evaluate_prototype_closure(req, src, actual, POLICY, src.record_digest)
        self.assertIn("evidence-reference-mismatch", {finding["code"] for finding in result["findings"]})

    def test_apply_delivery_rejects_unbound_or_unsupported_ingress(self) -> None:
        req, src = request("apply-delivery"), source("apply-delivery")
        expected = proofs("apply-delivery")
        for field, changed in (
            ("accepted_delivery_target_receipt_ref", {"source_packet_ref": "record://delivery-packets/other"}),
            ("accepted_delivery_target_receipt_ref", {"source_packet_ref": None}),
            ("accepted_delivery_target_receipt_ref", {"prototype_id": "other"}),
            ("accepted_delivery_target_receipt_ref", {"prototype_id": None}),
            ("accepted_delivery_target_receipt_ref", {"subject_ref": "openproject://work_packages/999"}),
            ("accepted_baseline_receipt_ref", {"prototype_id": "other"}),
        ):
            with self.subTest(field=field, changed=changed):
                actual = {**expected, field: replace(expected[field], **changed)}
                self.assertEqual(
                    evaluate_prototype_closure(req, src, actual, POLICY, src.record_digest)["outcome"],
                    "blocked",
                )
        unsupported = {**req, "target_kind": "existing-delivery-item"}
        self.assertIn("source-precondition-missing", {
            finding["code"] for finding in evaluate_prototype_closure(
                unsupported, src, expected, POLICY, src.record_digest
            )["findings"]
        })
        invalid_target = {**req, "target_delivery_ref": "repo://not-an-art-target"}
        self.assertIn("source-precondition-missing", {
            finding["code"] for finding in evaluate_prototype_closure(
                invalid_target, src, expected, POLICY, src.record_digest
            )["findings"]
        })

    def test_already_owned_graduation_reads_alternate_proof_only(self) -> None:
        req, src = request("graduate-source"), source("graduate-source")
        req["transfer_strategy"] = "already-owned"
        expected = proofs("graduate-source")
        expected.pop("source_transfer_receipt_ref")
        expected["already_owned_source_proof_ref"] = VerifiedReference(
            "repo://product-owner/already-owned", OWNER, "sha256:" + "e" * 64,
            "accepted", REPO, REVISION,
        )
        readers = {
            owner: RecordingOwnerReader({
                field: proof for field, proof in expected.items() if proof.owner_ref == owner
            })
            for owner in {proof.owner_ref for proof in expected.values()}
        }
        actual = resolve_evidence(OwnerBackedClosureEvidenceResolver(readers), req, src)
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(
            evaluate_prototype_closure(req, src, actual, POLICY, src.record_digest)["outcome"],
            "ready",
        )

    def test_studio_unicode_digest_matches_closure_source_algorithm(self) -> None:
        self.assertEqual(
            studio_digest({"note": "caf\u00e9"}),
            "sha256:4f04d229f04347a677771f9c19db24439d899760249282ba081fa345cf491b72",
        )
        self.assertNotEqual(studio_digest({"note": "caf\u00e9"}), digest({"note": "caf\u00e9"}))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.temp.name) / 'ledger.sqlite'}")
        self.addCleanup(self.engine.dispose)
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.authority = FakeAuthority(source("apply-delivery"))
        self.resolver = FakeResolver(proofs("apply-delivery"))
        self.service = PrototypeClosureReadinessService(
            session_factory=self.sessions, authority=self.authority,
            evidence_resolver=self.resolver,
            service_identity_ref="spiffe://test/wgcf/prototype-closure",
            implementation_ref="f" * 40,
            clock=lambda: datetime(2026, 9, 13, tzinfo=timezone.utc),
        )
        self.client = TestClient(create_app(
            prototype_closure_readiness=self.service,
            artifact_registry_authorizer=ArtifactRegistryAuthorizer(
                oos_secret="o" * 32, reconciler_secret="r" * 32
            ),
        ))
        self.addCleanup(self.client.close)
        self.headers = {
            "x-wgcf-caller-id": "operator-orchestration-service",
            "x-wgcf-caller-secret": "o" * 32,
        }

    def envelope(self, action: str) -> dict:
        value = {
            "schema_version": 1,
            "artifact_type": "prototype-closure-evaluation",
            "evaluation_id": f"prototype-closure-evaluation:sample-{action}",
            "expected_record_digest": self.authority.source.record_digest,
            "request": request(action),
        }
        value["evaluation_digest"] = digest(value)
        return value

    def test_all_four_actions_have_ready_owner_bound_evidence(self) -> None:
        for action in ("apply-delivery", "graduate-source", "retire-incubation", "reopen-incubation"):
            with self.subTest(action=action):
                src = source(action)
                result = evaluate_prototype_closure(request(action), src, proofs(action),
                                                    POLICY, src.record_digest)
                self.assertEqual(result["outcome"], "ready")
                self.assertFalse(result["findings"])

    def test_stale_missing_target_custody_and_invalid_state(self) -> None:
        req = request("graduate-source")
        src = source("graduate-source")
        stale = {**req, "expected_source_revision": "b" * 40}
        self.assertEqual(evaluate_prototype_closure(stale, src, proofs("graduate-source"),
                                                    POLICY, src.record_digest)["outcome"], "stale")
        missing = dict(proofs("graduate-source"))
        missing.pop("durable_owner_acceptance_ref")
        self.assertIn("evidence-unavailable", {f["code"] for f in evaluate_prototype_closure(
            req, src, missing, POLICY, src.record_digest)["findings"]})
        conflict = source("graduate-source", custody="shared-owner-repo")
        self.assertIn("custody-conflict", {f["code"] for f in evaluate_prototype_closure(
            req, conflict, proofs("graduate-source"), POLICY, conflict.record_digest)["findings"]})
        invalid = replace(src, lifecycle="candidate")
        self.assertIn("invalid-lifecycle", {f["code"] for f in evaluate_prototype_closure(
            req, invalid, proofs("graduate-source"), POLICY, invalid.record_digest)["findings"]})

    def test_forged_owner_and_stale_source_transfer_are_blocked(self) -> None:
        req = request("graduate-source")
        src = source("graduate-source")
        wrong_owner = dict(proofs("graduate-source"))
        wrong_owner["durable_owner_acceptance_ref"] = replace(
            wrong_owner["durable_owner_acceptance_ref"], owner_ref="repo://unrelated-owner"
        )
        self.assertIn("evidence-authority-mismatch", {f["code"] for f in evaluate_prototype_closure(
            req, src, wrong_owner, POLICY, src.record_digest)["findings"]})
        stale_transfer = dict(proofs("graduate-source"))
        stale_transfer["source_transfer_receipt_ref"] = replace(
            stale_transfer["source_transfer_receipt_ref"], source_revision="b" * 40
        )
        self.assertIn("source-transfer-stale", {f["code"] for f in evaluate_prototype_closure(
            req, src, stale_transfer, POLICY, src.record_digest)["findings"]})

    def test_digest_scoped_api_replay_and_source_staleness(self) -> None:
        envelope = self.envelope("apply-delivery")
        first = self.client.post("/v1/readiness/prototype-closure", json=envelope, headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        self.assertEqual(body["readiness"]["outcome"], "ready")
        self.assertEqual(len(body["readiness"]["evidence"]), 3)
        self.assertEqual(body["readiness"]["evidence_digest"], digest(body["readiness"]["evidence"]))
        self.assertEqual(body["ledger"]["state"], "durable")
        self.assertEqual(body["readiness"]["actor"], "operator-orchestration-service")
        replay = self.client.post("/v1/readiness/prototype-closure", json=envelope, headers=self.headers)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["ledger"]["resolution"], "reused")
        token = body["readiness"]["readiness_digest"].split(":", 1)[1]
        read = self.client.get(f"/v1/readiness/prototype-closure/{token}", headers=self.headers)
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["readiness"], body["readiness"])
        self.authority.revision = "b" * 40
        stale = self.client.get(f"/v1/readiness/prototype-closure/{token}", headers=self.headers)
        self.assertEqual(stale.json()["ledger"]["state"], "stale")
        with self.sessions() as session:
            self.assertEqual(session.query(PrototypeClosureReadinessRecord).count(), 1)
            self.assertEqual(session.execute(select(LedgerEvent)).scalars().all().__len__(), 4)

    def test_tamper_conflicting_replay_and_unavailable_dependency(self) -> None:
        envelope = self.envelope("apply-delivery")
        tampered = json.loads(json.dumps(envelope))
        tampered["request"]["operator_id"] = "other"
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=tampered,
                                          headers=self.headers).status_code, 422)
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=envelope,
                                          headers=self.headers).status_code, 200)
        changed = json.loads(json.dumps(envelope))
        changed["request"]["operator_id"] = "other"
        changed["evaluation_digest"] = artifact_digest(changed, "evaluation_digest")
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=changed,
                                          headers=self.headers).status_code, 409)
        self.resolver.available = False
        new = self.envelope("apply-delivery")
        new["evaluation_id"] += "-again"
        new["evaluation_digest"] = artifact_digest(new, "evaluation_digest")
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=new,
                                          headers=self.headers).status_code, 503)
        self.resolver.available = True
        self.resolver.evidence = dict(proofs("apply-delivery"))
        self.resolver.evidence["accepted_baseline_receipt_ref"] = replace(
            self.resolver.evidence["accepted_baseline_receipt_ref"],
            ref="record://baseline/accepted?token=not-safe",
        )
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=new,
                                          headers=self.headers).status_code, 503)

    def test_unauthorized_actor_cannot_issue_or_read(self) -> None:
        envelope = self.envelope("apply-delivery")
        self.assertEqual(self.client.post("/v1/readiness/prototype-closure", json=envelope,
                                          headers={"x-wgcf-caller-id": "reconciler", "x-wgcf-caller-secret": "r" * 32}).status_code, 401)
        with self.assertRaises(PrototypeClosureRequestError):
            self.service.issue(json.dumps(envelope).encode(), actor="artifact-reconciler")
        issued = self.service.issue(json.dumps(envelope).encode(), actor="operator-orchestration-service")
        token = issued["readiness"]["readiness_digest"].split(":", 1)[1]
        with self.assertRaises(PrototypeClosureNotFound):
            self.service.read(token, actor="artifact-reconciler")

    def test_default_runtime_cannot_be_enabled_without_owner_resolver(self) -> None:
        with patch.dict("os.environ", {
            "WGCF_RUNTIME_PROFILE": "dev-integration",
            "WGCF_PROTOTYPE_CLOSURE_READINESS_ENABLED": "true",
        }):
            with self.assertRaises(PrototypeClosureUnavailable):
                build_prototype_closure_readiness_runtime()

    def test_committed_studio_readback_ignores_dirty_tree_and_rejects_history_conflict(self) -> None:
        repo = Path(self.temp.name) / "studio"
        repo.mkdir()

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()

        def commit() -> None:
            git("add", ".")
            git("commit", "-m", "Update committed Studio authority")

        git("init", "-b", "main")
        git("config", "user.name", "Closure Test")
        git("config", "user.email", "closure@example.invalid")
        registry = {"prototypes": [{"id": "sample-tool", "lifecycle": "exploring",
                                     "source_custody": "incubation-repo"}]}
        registry_path = repo / "prototypes.yaml"
        registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")
        commit()
        initial_revision = git("rev-parse", "HEAD")
        validators = {
            "prototype-registry.schema.json": Draft202012Validator({
                "type": "object", "required": ["prototypes"],
                "properties": {"prototypes": {"type": "array"}},
            }),
            "prototype-closure-history-event.schema.json": Draft202012Validator({
                "type": "object", "required": ["event_id", "prototype_id",
                                              "prior_event_digest", "observed_lifecycle",
                                              "observed_source_custody"],
            }),
        }
        authority = PrototypeClosureAuthority(repo, trusted_ref="refs/heads/main")
        with patch.object(authority, "contract", return_value=(MANIFEST, validators, POLICY)):
            first = authority.snapshot("sample-tool")
            self.assertEqual(first.lifecycle, "exploring")
            registry["prototypes"][0]["lifecycle"] = "candidate"
            registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")
            self.assertEqual(authority.snapshot("sample-tool").lifecycle, "exploring")

            event_id = "prototype-closure:sample-tool:0001"
            event_ref = f"record://prototype-closure/sample-tool/history/{event_id}"
            registry["prototypes"][0].update(lifecycle="retired", closure_event_ref=event_ref)
            registry_path.write_text(yaml.safe_dump(registry), encoding="utf-8")
            history = repo / "records/prototype-closure/sample-tool/history/0001.json"
            history.parent.mkdir(parents=True)
            event = {
                "event_id": event_id, "prototype_id": "sample-tool",
                "prior_event_digest": None, "observed_lifecycle": "retired",
                "observed_source_custody": "incubation-repo",
                "expected_source_revision": initial_revision,
                "previous_lifecycle": "exploring",
                "previous_source_custody": "incubation-repo",
            }
            history.write_text(json.dumps(event), encoding="utf-8")
            commit()
            self.assertEqual(authority.snapshot("sample-tool").history_digest, studio_digest(event))

            event["previous_lifecycle"] = "candidate"
            history.write_text(json.dumps(event), encoding="utf-8")
            commit()
            with self.assertRaisesRegex(PrototypeClosureUnavailable, "prior state"):
                authority.snapshot("sample-tool")

            event["previous_lifecycle"] = "exploring"
            event["prior_event_digest"] = "sha256:" + "0" * 64
            history.write_text(json.dumps(event), encoding="utf-8")
            commit()
            with self.assertRaisesRegex(PrototypeClosureUnavailable, "history chain"):
                authority.snapshot("sample-tool")

    def test_contract_manifest_is_pinned(self) -> None:
        repo = Path(self.temp.name) / "invalid-studio"
        path = repo / "contracts/prototype-closure/manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "init", "-b", "main"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Closure Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "closure@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "Invalid manifest"], check=True,
                       capture_output=True)
        authority = PrototypeClosureAuthority(repo, trusted_ref="refs/heads/main")
        with self.assertRaisesRegex(PrototypeClosureUnavailable, "manifest changed"):
            authority.contract(authority.current_revision())
