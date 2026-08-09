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
    api_name: str,
    storage_name: str,
    provision_name: str,
    transfer_name: str,
    api_service_account: str,
    storage_service_account: str,
    maintenance_service_account: str,
) -> str | None:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    name = metadata.get("name", "")
    labels = metadata.get("labels") or {}
    component = labels.get("app.kubernetes.io/component")
    service_account = spec.get("serviceAccountName")
    owners = {
        (owner.get("kind"), owner.get("name"))
        for owner in metadata.get("ownerReferences") or []
    }
    if component == "api" and service_account == api_service_account and name.startswith(f"{api_name}-"):
        return "api"
    if component == "object-storage" and service_account == storage_service_account:
        if ("StatefulSet", storage_name) in owners:
            return "storage"
    if component == "object-storage-maintenance" and service_account == maintenance_service_account:
        if ("Job", provision_name) in owners:
            return "provision"
        if name == transfer_name and not owners:
            return "transfer"
    return None


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
    *,
    api_name: str,
    storage_name: str,
    provision_name: str,
    transfer_name: str,
    api_service_account: str,
    storage_service_account: str,
    maintenance_service_account: str,
    app_secret: str,
    root_secret: str,
    app_label: str,
    profile_id: str,
) -> dict[str, Any]:
    pod_refs = {
        (pod.get("metadata") or {}).get("name", ""): collect_secret_refs(pod)
        for pod in pods
    }
    pod_roles = {
        (pod.get("metadata") or {}).get("name", ""): classify_pod(
            pod,
            api_name=api_name,
            storage_name=storage_name,
            provision_name=provision_name,
            transfer_name=transfer_name,
            api_service_account=api_service_account,
            storage_service_account=storage_service_account,
            maintenance_service_account=maintenance_service_account,
        )
        for pod in pods
    }
    app_holders = sorted(name for name, refs in pod_refs.items() if app_secret in refs)
    root_holders = sorted(name for name, refs in pod_refs.items() if root_secret in refs)
    unexpected_app = [
        name for name in app_holders if pod_roles.get(name) not in {"api", "provision", "transfer"}
    ]
    unexpected_root = [
        name for name in root_holders if pod_roles.get(name) not in {"storage", "provision", "transfer"}
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
    if len(argv) != 14:
        raise SystemExit("expected output, pod, policy, workload, credential, and profile arguments")
    output_path, pods_path, policy_path = map(Path, argv[:3])
    payload = verify_isolation(
        json.loads(pods_path.read_text(encoding="utf-8"))["items"],
        json.loads(policy_path.read_text(encoding="utf-8")),
        api_name=argv[3],
        storage_name=argv[4],
        provision_name=argv[5],
        transfer_name=argv[6],
        api_service_account=argv[7],
        storage_service_account=argv[8],
        maintenance_service_account=argv[9],
        app_secret=argv[10],
        root_secret=argv[11],
        app_label=argv[12],
        profile_id=argv[13],
    )
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
