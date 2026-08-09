#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


def collect_secret_refs(pod: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    spec = pod.get("spec") or {}

    for container_group in ("containers", "initContainers", "ephemeralContainers"):
        for container in spec.get(container_group) or []:
            for env in container.get("env") or []:
                name = ((env.get("valueFrom") or {}).get("secretKeyRef") or {}).get("name")
                if name:
                    refs.add(name)
            for env_from in container.get("envFrom") or []:
                name = (env_from.get("secretRef") or {}).get("name")
                if name:
                    refs.add(name)

    for volume in spec.get("volumes") or []:
        name = (volume.get("secret") or {}).get("secretName")
        if name:
            refs.add(name)
        for source in (volume.get("projected") or {}).get("sources") or []:
            name = (source.get("secret") or {}).get("name")
            if name:
                refs.add(name)

    for image_pull_secret in spec.get("imagePullSecrets") or []:
        name = image_pull_secret.get("name")
        if name:
            refs.add(name)
    return refs


def classify_pod(
    pod: dict[str, Any],
    *,
    api_replica_sets: set[tuple[str, str]],
    storage_controller: tuple[str, str],
    provision_controller: tuple[str, str],
    api_service_account: str,
    storage_service_account: str,
    maintenance_service_account: str,
) -> str | None:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    labels = metadata.get("labels") or {}
    component = labels.get("app.kubernetes.io/component")
    service_account = spec.get("serviceAccountName")
    owners = {
        (owner.get("name"), owner.get("uid"))
        for owner in metadata.get("ownerReferences") or []
        if owner.get("controller") is True
    }
    if (
        component == "api"
        and service_account == api_service_account
        and bool(owners.intersection(api_replica_sets))
    ):
        return "api"
    if component == "object-storage" and service_account == storage_service_account:
        if storage_controller in owners:
            return "storage"
    if component == "object-storage-maintenance" and service_account == maintenance_service_account:
        if provision_controller in owners:
            return "provision"
    return None


def controller_identity(controller: dict[str, Any], *, kind: str, name: str) -> tuple[str, str]:
    metadata = controller.get("metadata") or {}
    if controller.get("kind") != kind or metadata.get("name") != name:
        raise ValueError(f"expected {kind} controller {name} is unavailable")
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise ValueError(f"{kind} controller {name} has no UID")
    return name, uid


def api_replica_set_identities(
    replica_sets: list[dict[str, Any]],
    deployment: dict[str, Any],
    *,
    api_name: str,
) -> set[tuple[str, str]]:
    deployment_identity = controller_identity(deployment, kind="Deployment", name=api_name)
    identities: set[tuple[str, str]] = set()
    for replica_set in replica_sets:
        metadata = replica_set.get("metadata") or {}
        owners = {
            (owner.get("name"), owner.get("uid"))
            for owner in metadata.get("ownerReferences") or []
            if owner.get("kind") == "Deployment" and owner.get("controller") is True
        }
        if deployment_identity not in owners:
            continue
        name = metadata.get("name")
        uid = metadata.get("uid")
        if isinstance(name, str) and name and isinstance(uid, str) and uid:
            identities.add((name, uid))
    if not identities:
        raise ValueError(f"Deployment {api_name} has no owned ReplicaSet")
    return identities


def _provision_job_signature(job: dict[str, Any]) -> dict[str, Any]:
    metadata = job.get("metadata") or {}
    spec = job.get("spec") or {}
    template = spec.get("template") or {}
    template_metadata = template.get("metadata") or {}
    pod_spec = template.get("spec") or {}
    source_label_keys = (
        "app.kubernetes.io/name",
        "app.kubernetes.io/component",
        "devint.profile",
    )

    def source_labels(labels: dict[str, Any]) -> dict[str, Any]:
        return {key: labels.get(key) for key in source_label_keys}

    containers = [
        {
            key: container.get(key)
            for key in (
                "name",
                "image",
                "imagePullPolicy",
                "command",
                "args",
                "env",
                "volumeMounts",
            )
        }
        for container in pod_spec.get("containers") or []
    ]
    volumes = [
        {
            "name": volume.get("name"),
            "configMap": {"name": (volume.get("configMap") or {}).get("name")},
        }
        for volume in pod_spec.get("volumes") or []
    ]
    return {
        "metadata_labels": source_labels(metadata.get("labels") or {}),
        "backoff_limit": spec.get("backoffLimit"),
        "template_labels": source_labels(template_metadata.get("labels") or {}),
        "service_account": pod_spec.get("serviceAccountName"),
        "restart_policy": pod_spec.get("restartPolicy"),
        "containers": containers,
        "volumes": volumes,
    }


def job_identity(
    jobs: list[dict[str, Any]],
    recorded_job: dict[str, Any],
    *,
    name: str,
) -> tuple[str, str]:
    matches = [job for job in jobs if (job.get("metadata") or {}).get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"expected Job controller {name} is unavailable")
    live_identity = controller_identity(matches[0], kind="Job", name=name)
    recorded_identity = controller_identity(recorded_job, kind="Job", name=name)
    if live_identity != recorded_identity:
        raise ValueError(f"Job controller {name} does not match the recorded provision identity")
    if _provision_job_signature(matches[0]) != _provision_job_signature(recorded_job):
        raise ValueError(f"Job controller {name} does not match the recorded provision template")
    return live_identity


def validate_network_policy(
    policy: dict[str, Any],
    *,
    app_label: str,
    profile_id: str,
) -> list[str]:
    expected_pod_selector = {
        "matchLabels": {
            "app.kubernetes.io/name": app_label,
            "app.kubernetes.io/component": "object-storage",
            "devint.profile": profile_id,
        }
    }
    expected_source_selector = {
        "matchExpressions": [
            {
                "key": "app.kubernetes.io/component",
                "operator": "In",
                "values": ["api", "object-storage-maintenance"],
            }
        ]
    }
    spec = policy.get("spec") or {}
    if spec.get("podSelector") != expected_pod_selector:
        raise ValueError("storage NetworkPolicy selects an unexpected target")
    if spec.get("policyTypes") != ["Ingress"]:
        raise ValueError("storage NetworkPolicy has unexpected policy types")
    ingress = spec.get("ingress")
    if not isinstance(ingress, list) or len(ingress) != 1:
        raise ValueError("storage NetworkPolicy must have exactly one ingress rule")
    rule = ingress[0]
    if set(rule) != {"from", "ports"}:
        raise ValueError("storage NetworkPolicy ingress rule has unexpected fields")
    sources = rule.get("from")
    if sources != [{"podSelector": expected_source_selector}]:
        raise ValueError("storage NetworkPolicy has unexpected ingress subjects")
    if rule.get("ports") != [{"protocol": "TCP", "port": 9000}]:
        raise ValueError("storage NetworkPolicy has unexpected ingress ports")
    return ["api", "object-storage-maintenance"]


def verify_isolation(
    pods: list[dict[str, Any]],
    network_policy: dict[str, Any],
    deployment: dict[str, Any],
    replica_sets: list[dict[str, Any]],
    stateful_set: dict[str, Any],
    jobs: list[dict[str, Any]],
    recorded_provision_job: dict[str, Any],
    *,
    api_name: str,
    storage_name: str,
    provision_name: str,
    api_service_account: str,
    storage_service_account: str,
    maintenance_service_account: str,
    app_secret: str,
    root_secret: str,
    app_label: str,
    profile_id: str,
) -> dict[str, Any]:
    api_controllers = api_replica_set_identities(
        replica_sets,
        deployment,
        api_name=api_name,
    )
    storage_controller = controller_identity(
        stateful_set,
        kind="StatefulSet",
        name=storage_name,
    )
    provision_controller = job_identity(
        jobs,
        recorded_provision_job,
        name=provision_name,
    )
    pod_refs = {
        (pod.get("metadata") or {}).get("name", ""): collect_secret_refs(pod)
        for pod in pods
    }
    pod_roles = {
        (pod.get("metadata") or {}).get("name", ""): classify_pod(
            pod,
            api_replica_sets=api_controllers,
            storage_controller=storage_controller,
            provision_controller=provision_controller,
            api_service_account=api_service_account,
            storage_service_account=storage_service_account,
            maintenance_service_account=maintenance_service_account,
        )
        for pod in pods
    }
    app_holders = sorted(name for name, refs in pod_refs.items() if app_secret in refs)
    root_holders = sorted(name for name, refs in pod_refs.items() if root_secret in refs)
    unexpected_app = [
        name for name in app_holders if pod_roles.get(name) not in {"api", "provision"}
    ]
    unexpected_root = [
        name for name in root_holders if pod_roles.get(name) not in {"storage", "provision"}
    ]
    if unexpected_app:
        raise ValueError(f"application storage credential leaked to pods: {unexpected_app}")
    if unexpected_root:
        raise ValueError(f"root storage credential leaked to pods: {unexpected_root}")
    if not any(pod_roles.get(name) == "api" for name in app_holders):
        raise ValueError("application storage credential is not projected to the WGCF API pod")
    if not any(pod_roles.get(name) == "storage" for name in root_holders):
        raise ValueError("root storage credential is not projected to the storage pod")
    for name, refs in pod_refs.items():
        if refs.intersection({app_secret, root_secret}) and pod_roles.get(name) is None:
            raise ValueError(f"unclassified pod holds a storage credential: {name}")

    allowed_components = validate_network_policy(
        network_policy,
        app_label=app_label,
        profile_id=profile_id,
    )
    return {
        "schema_version": 1,
        "application_secret_holders": app_holders,
        "root_secret_holders": root_holders,
        "network_policy_allowed_components": allowed_components,
        "oos_credential_issued": False,
        "openproject_credential_issued": False,
        "verified_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 18:
        raise SystemExit("expected output, pod, policy, workload, credential, and profile arguments")
    (
        output_path,
        pods_path,
        policy_path,
        deployment_path,
        replica_sets_path,
        stateful_set_path,
        jobs_path,
        recorded_provision_job_path,
    ) = map(Path, argv[:8])
    payload = verify_isolation(
        json.loads(pods_path.read_text(encoding="utf-8"))["items"],
        json.loads(policy_path.read_text(encoding="utf-8")),
        json.loads(deployment_path.read_text(encoding="utf-8")),
        json.loads(replica_sets_path.read_text(encoding="utf-8"))["items"],
        json.loads(stateful_set_path.read_text(encoding="utf-8")),
        json.loads(jobs_path.read_text(encoding="utf-8"))["items"],
        json.loads(recorded_provision_job_path.read_text(encoding="utf-8")),
        api_name=argv[8],
        storage_name=argv[9],
        provision_name=argv[10],
        api_service_account=argv[11],
        storage_service_account=argv[12],
        maintenance_service_account=argv[13],
        app_secret=argv[14],
        root_secret=argv[15],
        app_label=argv[16],
        profile_id=argv[17],
    )
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
