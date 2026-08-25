from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
from unittest import TestCase

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
import yaml

from control_fabric_core.canonical_json import canonical_json_bytes
from control_fabric_core.db import metadata
from control_fabric_core.db.models import LedgerEvent, RepositoryReadinessReceipt
from control_fabric_core.repository_readiness import (
    RepositoryReadinessRequestError,
    RepositoryReadinessService,
    RepositoryReadinessUnavailable,
)
from control_fabric_core.repository_readiness_contracts import (
    RepositoryReadinessContractBundle,
    RepositoryReadinessContractError,
    authority_content_digest,
)


IMPLEMENTATION_REF = "f" * 40
SERVICE_IDENTITY_REF = (
    "kubernetes://devint-governance-control-fabric/"
    "serviceaccount/workspace-governance-control-fabric-api"
)


class RepositoryAuthorityFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo_name = "sample-repository"
        self.repository = {
            "lifecycle": "active",
            "repo_class": "product-owner",
            "requires_security_bindings": True,
            "security_review_subject": True,
            "owns": ["sample product source"],
            "must_not_own": ["workspace contracts"],
            "allowed_authoritative_refs": ["workspace-governance"],
            "validation_behavior": {
                "posture": "covered-by-owner-repo",
                "wgcf_graph_role": "product-runtime-source",
                "catalog_refs": ["component-contracts", "security-bindings"],
            },
        }
        self.rule = {
            "schema_version": 1,
            "repo": self.repo_name,
            "lifecycle": "active",
            "required_repo_refs": ["workspace-governance"],
            "required_patterns": {"readme": ["Sample"], "agents": ["security"]},
            "forbidden_patterns": {"readme": [], "agents": []},
            "security_requirements": {
                "security_owner": "security-architecture",
                "review_checklist_path": "docs/security-checklist.md",
                "review_output_path": "docs/security-review.md",
                "required_artifacts": [{"id": "sample-review", "path": "docs/security-review.md"}],
            },
        }
        self.write_authority()
        self.write_rule()

    def write_authority(
        self,
        *,
        repositories: dict | None = None,
        retired_repositories: dict | None = None,
    ) -> None:
        path = self.root / "contracts/repos.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "repos": repositories if repositories is not None else {self.repo_name: self.repository},
            "retired_repos": retired_repositories or {},
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def write_rule(self, *, rule: dict | None = None) -> None:
        path = self.root / "contracts/repo-rules" / f"{self.repo_name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(rule or self.rule, sort_keys=False), encoding="utf-8")

    @property
    def authority_digest(self) -> str:
        return authority_content_digest((self.root / "contracts/repos.yaml").read_bytes())


class RepositoryReadinessTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source = RepositoryAuthorityFixture(Path(self.temp.name) / "workspace-governance")
        self.engine = create_engine("sqlite:///:memory:")
        metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.service = RepositoryReadinessService(
            session_factory=self.sessions,
            authority_repo_root=self.source.root,
            service_identity_ref=SERVICE_IDENTITY_REF,
            implementation_ref=IMPLEMENTATION_REF,
            clock=lambda: datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp.cleanup()

    def request(self, **overrides: object) -> bytes:
        payload = {
            "schema_version": 1,
            "profile_id": "dev-integration",
            "policy_scope": "delivery-catalog-owner-repo",
            "repo_name": self.source.repo_name,
            "repo_ref": f"repo://{self.source.repo_name}",
            "expected_owner_repo": self.source.repo_name,
            "catalog_value_key": self.source.repo_name,
            "expected_authority_digest": self.source.authority_digest,
            **overrides,
        }
        return canonical_json_bytes(payload)

    def test_admitted_repository_issues_replay_safe_oos_compatible_receipt(self) -> None:
        authority_path = self.source.root / "contracts/repos.yaml"
        rule_path = self.source.root / "contracts/repo-rules" / f"{self.source.repo_name}.yaml"
        source_before = (authority_path.read_bytes(), rule_path.read_bytes())
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        replay = self.service.issue(self.request(), actor="operator-orchestration-service")
        token = created.receipt["receipt_id"].split(":", 1)[1]
        read = self.service.read(token, actor="operator-orchestration-service")

        self.assertEqual("ready", created.receipt["decision"]["outcome"])
        self.assertTrue(created.receipt["decision"]["linking_allowed"])
        self.assertEqual("none", created.receipt["decision"]["mutation_authority"])
        self.assertEqual("created", created.resolution)
        self.assertEqual("reused", replay.resolution)
        self.assertEqual("read", read.resolution)
        self.assertEqual(created.receipt, replay.receipt)
        self.assertEqual(created.receipt, read.receipt)
        self.assertIsNotNone(created.reference)
        self.service._contracts.require_valid(
            "repository_readiness_reference",
            created.reference,
        )
        self.assertEqual(source_before, (authority_path.read_bytes(), rule_path.read_bytes()))
        with self.sessions() as session:
            self.assertEqual(1, len(session.scalars(select(RepositoryReadinessReceipt)).all()))
            actions = {event.action for event in session.scalars(select(LedgerEvent)).all()}
        self.assertEqual(
            {
                "repository.readiness.persisted",
                "repository.readiness.reused",
                "repository.readiness.read",
            },
            actions,
        )

    def test_missing_retired_and_stale_authority_have_distinct_outcomes(self) -> None:
        expected = self.source.authority_digest
        self.source.write_authority(repositories={})
        not_admitted = self.service.issue(
            self.request(expected_authority_digest=self.source.authority_digest),
            actor="operator-orchestration-service",
        )
        self.assertEqual("not_admitted", not_admitted.receipt["decision"]["outcome"])
        self.assertIsNone(not_admitted.reference)

        self.source.write_authority(
            repositories={},
            retired_repositories={
                self.source.repo_name: {
                    "lifecycle": "retired",
                    "replaced_by": {"product": "replacement-repository"},
                },
            },
        )
        retired = self.service.issue(
            self.request(expected_authority_digest=self.source.authority_digest),
            actor="operator-orchestration-service",
        )
        self.assertEqual("retired", retired.receipt["decision"]["outcome"])

        stale = self.service.issue(
            self.request(expected_authority_digest=expected),
            actor="operator-orchestration-service",
        )
        self.assertEqual("stale", stale.receipt["decision"]["outcome"])
        self.assertEqual("authority-version-stale", stale.receipt["decision"]["reason_codes"][0])

    def test_rule_and_security_contract_mismatches_fail_closed(self) -> None:
        rule_path = self.source.root / "contracts/repo-rules" / f"{self.source.repo_name}.yaml"
        rule_path.unlink()
        missing = self.service.issue(self.request(), actor="operator-orchestration-service")
        self.assertEqual("contract_mismatch", missing.receipt["decision"]["outcome"])
        self.assertEqual(["repository-rule-missing"], missing.receipt["decision"]["reason_codes"])

        rule = copy.deepcopy(self.source.rule)
        rule.pop("security_requirements")
        self.source.write_rule(rule=rule)
        missing_security = self.service.issue(
            self.request(),
            actor="operator-orchestration-service",
        )
        self.assertEqual(
            ["repository-security-posture-missing"],
            missing_security.receipt["decision"]["reason_codes"],
        )

    def test_duplicate_authority_keys_fail_closed_as_contract_mismatch(self) -> None:
        path = self.source.root / "contracts/repos.yaml"
        path.write_text(
            "schema_version: 1\nretired_repos: {}\nrepos:\n  sample-repository:\n"
            "    lifecycle: active\n    lifecycle: retired\n",
            encoding="utf-8",
        )

        result = self.service.issue(self.request(), actor="operator-orchestration-service")

        self.assertEqual("contract_mismatch", result.receipt["decision"]["outcome"])
        self.assertEqual(["authority-contract-invalid"], result.receipt["decision"]["reason_codes"])

    def test_identity_mismatch_is_not_accepted_as_repository_truth(self) -> None:
        result = self.service.issue(
            self.request(catalog_value_key="another-repository"),
            actor="operator-orchestration-service",
        )
        self.assertEqual("contract_mismatch", result.receipt["decision"]["outcome"])
        self.assertEqual(["repository-reference-mismatch"], result.receipt["decision"]["reason_codes"])

    def test_changed_authority_creates_next_generation_and_supersedes(self) -> None:
        first = self.service.issue(self.request(), actor="operator-orchestration-service")
        repository = copy.deepcopy(self.source.repository)
        repository["owns"].append("another bounded capability")
        self.source.write_authority(repositories={self.source.repo_name: repository})
        second = self.service.issue(self.request(), actor="operator-orchestration-service")

        self.assertEqual(1, first.generation)
        self.assertEqual(2, second.generation)
        self.assertEqual(first.receipt["custody"]["uri"], second.receipt["custody"]["supersedes"]["uri"])

    def test_readback_rejects_generation_not_bound_to_receipt(self) -> None:
        created = self.service.issue(self.request(), actor="operator-orchestration-service")
        with self.sessions.begin() as session:
            row = session.get(RepositoryReadinessReceipt, created.receipt["receipt_id"])
            assert row is not None
            row.generation = 2

        token = created.receipt["receipt_id"].split(":", 1)[1]
        with self.assertRaisesRegex(RepositoryReadinessUnavailable, "ledger integrity"):
            self.service.read(token, actor="operator-orchestration-service")

    def test_contract_bundle_rejects_unpinned_consumer_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            contract_root = Path(temp_dir) / "repository-readiness"
            shutil.copytree(
                Path(__file__).resolve().parents[1] / "contracts/repository-readiness",
                contract_root,
            )
            schema = contract_root / "repository-readiness-reference.schema.json"
            schema.write_bytes(schema.read_bytes() + b"\n")
            with self.assertRaisesRegex(
                RepositoryReadinessContractError,
                "does not match manifest",
            ):
                RepositoryReadinessContractBundle.load(contract_root)

    def test_malformed_request_is_rejected_without_receipt(self) -> None:
        request = json.loads(self.request())
        request["repo_name"] = "Invalid Name"
        with self.assertRaises(RepositoryReadinessRequestError):
            self.service.issue(
                canonical_json_bytes(request),
                actor="operator-orchestration-service",
            )
        with self.sessions() as session:
            self.assertEqual([], session.scalars(select(RepositoryReadinessReceipt)).all())


if __name__ == "__main__":
    import unittest

    unittest.main()
