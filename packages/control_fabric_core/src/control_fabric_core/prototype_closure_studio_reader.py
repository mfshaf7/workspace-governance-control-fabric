"""Verify Studio Closure owner proof from its trusted committed source."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

import yaml

from .prototype_closure_authority import (
    PrototypeClosureAuthority,
    PrototypeClosureRequestError,
    PrototypeClosureUnavailable,
    load_bundle_manifest,
    studio_digest,
)
from .prototype_closure_evidence import ClosureEvidenceLookup
from .prototype_closure_policy import VerifiedReference


PLAN_REF = re.compile(
    r"^record://prototype-closure/([a-z0-9]+(?:-[a-z0-9]+)*)/retention-plans/([0-9a-f]{64})$"
)
COMMIT = re.compile(r"^[0-9a-f]{40}$")
MAX_RETAINED_FILES = 100


class StudioClosureOwnerReader:
    def __init__(self, authority: PrototypeClosureAuthority) -> None:
        self.authority = authority

    def read(self, lookup: ClosureEvidenceLookup) -> VerifiedReference | None:
        if lookup.owner_ref != "workspace-prototype-studio":
            raise PrototypeClosureUnavailable("Studio Closure reader owner differs")
        source = self.authority.snapshot(lookup.prototype_id)
        if source.revision != lookup.source_revision:
            raise PrototypeClosureUnavailable("Studio Closure source revision changed")
        if lookup.field == "retention_plan_ref":
            if (
                not lookup.requested_ref or not lookup.operator_id.strip()
                or not lookup.retirement_reason or not lookup.retirement_reason.strip()
            ):
                raise PrototypeClosureUnavailable("Studio retention decision is incomplete")
            plan = self._plan(source.revision, lookup.prototype_id, lookup.requested_ref)
            if (
                plan["operator_id"] != lookup.operator_id
                or plan["retirement_reason"] != lookup.retirement_reason
                or source.lifecycle != plan["basis_lifecycle"]
                or source.custody != plan["basis_source_custody"]
            ):
                raise PrototypeClosureUnavailable("Studio retention decision differs from committed source")
            self._verify_objects(source.revision, source.record, plan)
            return VerifiedReference(
                lookup.requested_ref, lookup.owner_ref,
                "sha256:" + PLAN_REF.fullmatch(lookup.requested_ref).group(2), "accepted",
                prototype_id=lookup.prototype_id, source_revision=source.revision,
            )
        if lookup.field != "retained_source_readback_ref":
            raise PrototypeClosureUnavailable("Studio Closure reader does not own this field")
        if source.lifecycle != "retired" or source.custody != "incubation-repo":
            raise PrototypeClosureUnavailable("Studio source is not retained for reopen")
        event = source.last_event
        retirement_ref = source.record.get("retirement_ref")
        if (
            not event or event.get("event_type") != "incubation-retired"
            or not isinstance(retirement_ref, str)
            or retirement_ref != source.record.get("closure_event_ref")
            or retirement_ref != f"record://prototype-closure/{lookup.prototype_id}/history/{event['event_id']}"
        ):
            raise PrototypeClosureUnavailable("Studio retirement history does not bind current source")
        plan_ref = event.get("retention_plan_ref")
        plan = self._plan(source.revision, lookup.prototype_id, plan_ref)
        self._verify_objects(source.revision, source.record, plan)
        ref = f"record://prototype-closure/{lookup.prototype_id}/retained-source/{source.revision}"
        if lookup.requested_ref is not None and lookup.requested_ref != ref:
            raise PrototypeClosureUnavailable("retained source reference differs from committed source")
        return VerifiedReference(
            ref, lookup.owner_ref,
            studio_digest({"record": source.record, "retirement_event": event}),
            "accepted", subject_ref=retirement_ref,
            source_revision=source.revision, prototype_id=lookup.prototype_id,
        )

    def _plan(self, revision: str, prototype_id: str, ref: str) -> dict[str, Any]:
        match = PLAN_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match is None or match.group(1) != prototype_id:
            raise PrototypeClosureUnavailable("Studio retention plan reference is invalid")
        plan = self.authority._json(
            revision,
            f"records/prototype-closure/{prototype_id}/retention-plans/{match.group(2)}.json",
        )
        expected = load_bundle_manifest()["source_authority"]["retention_plan_schema_sha256"]
        validator = self.authority._validator(
            revision, "schemas/prototype-closure-retention-plan.schema.json", expected,
        )
        try:
            self.authority._validate(validator, plan, "retention plan")
        except PrototypeClosureRequestError as exc:
            raise PrototypeClosureUnavailable("committed Studio retention plan is invalid") from exc
        if (
            plan["prototype_id"] != prototype_id
            or plan["source_tree_path"] not in {None, f"prototypes/{prototype_id}"}
            or (plan["source_tree_path"] is None) != (plan["source_tree_oid"] is None)
            or studio_digest(plan) != "sha256:" + match.group(2)
        ):
            raise PrototypeClosureUnavailable("Studio retention plan differs from its reference")
        basis = plan["basis_revision"]
        self.authority._git("merge-base", "--is-ancestor", basis, revision)
        try:
            registry = yaml.safe_load(self.authority._read(basis, "prototypes.yaml"))
            matches = [item for item in registry["prototypes"] if item.get("id") == prototype_id]
        except (TypeError, KeyError, yaml.YAMLError) as exc:
            raise PrototypeClosureUnavailable("Studio retention basis is invalid") from exc
        if len(matches) != 1 or (
            matches[0].get("lifecycle") != plan["basis_lifecycle"]
            or matches[0].get("source_custody", "incubation-repo") != plan["basis_source_custody"]
        ):
            raise PrototypeClosureUnavailable("Studio retention basis differs from the plan")
        return plan

    def _verify_objects(self, revision: str, record: dict[str, Any], plan: dict[str, Any]) -> None:
        paths = record.get("paths")
        if not isinstance(paths, dict) or len(paths) > MAX_RETAINED_FILES:
            raise PrototypeClosureUnavailable("Studio retained source paths are invalid")
        normalized = []
        for raw in paths.values():
            if not isinstance(raw, str):
                raise PrototypeClosureUnavailable("Studio retained source path is invalid")
            path = PurePosixPath(raw)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise PrototypeClosureUnavailable("Studio retained source path escapes the repository")
            normalized.append(path.as_posix())
        retained = []
        for path in sorted(set(normalized)):
            oid = self.authority._git("rev-parse", "--verify", f"{revision}:{path}").decode().strip()
            if not COMMIT.fullmatch(oid) or self.authority._git("cat-file", "-t", oid).strip() != b"blob":
                raise PrototypeClosureUnavailable("Studio retained source object is not a file")
            retained.append({"path": path, "oid": oid})
        if retained != plan["retained_files"]:
            raise PrototypeClosureUnavailable("Studio retained files differ from the plan")
        source_tree = f"prototypes/{plan['prototype_id']}"
        present = bool(self.authority._git("ls-tree", "-r", "--name-only", revision, "--", source_tree))
        if present != (plan["source_tree_path"] is not None):
            raise PrototypeClosureUnavailable("Studio source tree presence differs from the plan")
        if present:
            oid = self.authority._git("rev-parse", "--verify", f"{revision}:{source_tree}").decode().strip()
            if oid != plan["source_tree_oid"] or self.authority._git("cat-file", "-t", oid).strip() != b"tree":
                raise PrototypeClosureUnavailable("Studio source tree differs from the plan")
