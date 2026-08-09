#!/usr/bin/env bash

STORAGE_BACKUP_STAGING_ARCHIVE=""
STORAGE_BACKUP_STAGING_MANIFEST=""
STORAGE_BACKUP_STAGING_RECEIPT=""

cleanup_storage_backup_staging() {
  local path=""
  for path in "${STORAGE_BACKUP_STAGING_ARCHIVE:-}" \
    "${STORAGE_BACKUP_STAGING_MANIFEST:-}" \
    "${STORAGE_BACKUP_STAGING_RECEIPT:-}"; do
    if [[ -n "${path}" ]]; then
      rm -f -- "${path}"
    fi
  done
  STORAGE_BACKUP_STAGING_ARCHIVE=""
  STORAGE_BACKUP_STAGING_MANIFEST=""
  STORAGE_BACKUP_STAGING_RECEIPT=""
}

generate_storage_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

ensure_storage_credentials() {
  ensure_state_dirs
  if [[ -f "${STORAGE_CREDENTIALS_ENV}" ]]; then
    return
  fi

  umask 077
  cat >"${STORAGE_CREDENTIALS_ENV}" <<EOF
STORAGE_ROOT_USER=wgcf-root
STORAGE_ROOT_PASSWORD=$(generate_storage_secret)
STORAGE_APP_ACCESS_KEY=wgcf-evidence-api
STORAGE_APP_SECRET_KEY=$(generate_storage_secret)
EOF
}

load_storage_credentials() {
  ensure_storage_credentials
  # shellcheck disable=SC1090
  source "${STORAGE_CREDENTIALS_ENV}"
}

storage_credentials_digest() {
  ensure_storage_credentials
  sha256sum "${STORAGE_CREDENTIALS_ENV}" | awk '{print $1}'
}

require_storage_security_review() {
  python3 - "${PROFILE_JSON}" "${WORKSPACE_ROOT}" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

profile = json.loads(sys.argv[1])
workspace_root = pathlib.Path(sys.argv[2]).resolve()
expected_repo = "security-architecture"
expected_path = "docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md"
refs = (profile.get("security") or {}).get("activation_review_refs") or []
matches = [
    item for item in refs
    if item.get("repo") == expected_repo and item.get("path") == expected_path
]
if len(matches) != 1:
    raise SystemExit("WGCF evidence storage requires the approved Security activation review reference")
review_ref = matches[0]
source_commit = review_ref.get("source_commit")
expected_digest = review_ref.get("content_sha256")
if not isinstance(source_commit, str) or len(source_commit) != 40:
    raise SystemExit("WGCF evidence storage Security review has no immutable source commit")
if not isinstance(expected_digest, str) or len(expected_digest) != 64:
    raise SystemExit("WGCF evidence storage Security review has no content digest")
review_path = workspace_root / expected_repo / expected_path
repo_root = workspace_root / expected_repo
if not (repo_root / ".git").exists():
    raise SystemExit(f"WGCF evidence storage Security repository is unavailable: {repo_root}")
result = subprocess.run(
    ["git", "-C", str(repo_root), "show", f"{source_commit}:{expected_path}"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if result.returncode != 0:
    raise SystemExit("WGCF evidence storage Security review is unavailable at its pinned commit")
actual_digest = hashlib.sha256(result.stdout).hexdigest()
if actual_digest != expected_digest:
    raise SystemExit("WGCF evidence storage Security review commit does not match its pinned digest")
PY
}

require_storage_authority_contract() {
  python3 - "${PROFILE_JSON}" "${WORKSPACE_ROOT}" <<'PY'
import json
import pathlib
import sys

import yaml

profile = json.loads(sys.argv[1])
workspace_root = pathlib.Path(sys.argv[2]).resolve()
binding = (profile.get("authority") or {}).get("activation_contract") or {}
required_binding_fields = {
    "repo",
    "path",
    "profile_id",
    "platform_acceptance_ref",
    "required_actions",
    "required_stage_checks",
}
if set(binding) != required_binding_fields:
    raise SystemExit("WGCF evidence storage profile has an incomplete authority binding")

registry_path = workspace_root / binding["repo"] / binding["path"]
if not registry_path.is_file():
    raise SystemExit(f"WGCF evidence storage authority registry is unavailable: {registry_path}")
registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
registered_profile = (registry.get("profiles") or {}).get(binding["profile_id"])
if not isinstance(registered_profile, dict):
    raise SystemExit("WGCF evidence storage is not registered by workspace authority")
if registered_profile.get("lifecycle") != "active":
    raise SystemExit("WGCF evidence storage authority profile is not active")

admission = registered_profile.get("admission") or {}
if admission.get("platform_acceptance_ref") != binding["platform_acceptance_ref"]:
    raise SystemExit("WGCF evidence storage Platform acceptance is not active in workspace authority")
acceptance_prefix = "repo://platform-engineering/"
if not binding["platform_acceptance_ref"].startswith(acceptance_prefix):
    raise SystemExit("WGCF evidence storage Platform acceptance reference is invalid")
acceptance_path = (
    workspace_root
    / "platform-engineering"
    / binding["platform_acceptance_ref"][len(acceptance_prefix):]
)
if not acceptance_path.is_file():
    raise SystemExit(f"WGCF evidence storage Platform acceptance is unavailable: {acceptance_path}")

registered_actions = set(registered_profile.get("actions") or [])
missing_actions = sorted(set(binding["required_actions"]) - registered_actions)
if missing_actions:
    raise SystemExit(f"WGCF evidence storage actions are not authorized: {missing_actions}")
registered_checks = set((registered_profile.get("stage_handoff") or {}).get("required_checks") or [])
missing_checks = sorted(set(binding["required_stage_checks"]) - registered_checks)
if missing_checks:
    raise SystemExit(f"WGCF evidence storage gates are not authorized: {missing_checks}")
PY
}

storage_seed_digest() {
  printf '%s\n' "${STORAGE_SEED_PAYLOAD}" | sha256sum | awk '{print $1}'
}

apply_storage_secrets() {
  load_storage_credentials
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: ${STORAGE_ROOT_SECRET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
type: Opaque
stringData:
  root-user: "${STORAGE_ROOT_USER}"
  root-password: "${STORAGE_ROOT_PASSWORD}"
---
apiVersion: v1
kind: Secret
metadata:
  name: ${STORAGE_APP_SECRET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-api-credential
    devint.profile: ${PROFILE_ID}
type: Opaque
stringData:
  access-key: "${STORAGE_APP_ACCESS_KEY}"
  secret-key: "${STORAGE_APP_SECRET_KEY}"
EOF
}

append_storage_runtime_manifest() {
  cat <<EOF
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${STORAGE_SERVICE_ACCOUNT}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-maintenance
    devint.profile: ${PROFILE_ID}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${STORAGE_STATEFULSET}-seed
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-seed
    devint.profile: ${PROFILE_ID}
data:
  evidence-custody-v1.json: |
    ${STORAGE_SEED_PAYLOAD}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${STORAGE_STATEFULSET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: ${STORAGE_VOLUME_SIZE}
---
apiVersion: v1
kind: Service
metadata:
  name: ${STORAGE_SERVICE}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
  ports:
    - name: s3
      port: 9000
      targetPort: s3
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ${STORAGE_STATEFULSET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
spec:
  serviceName: ${STORAGE_SERVICE}
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${APP_LABEL}
      app.kubernetes.io/component: object-storage
      devint.profile: ${PROFILE_ID}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: object-storage
        devint.profile: ${PROFILE_ID}
      annotations:
        devint.workspace/storage-credentials-sha256: $(storage_credentials_digest)
    spec:
      serviceAccountName: ${STORAGE_SERVICE_ACCOUNT}
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: minio
          image: ${STORAGE_IMAGE}
          imagePullPolicy: IfNotPresent
          args:
            - server
            - /data
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_ROOT_SECRET}
                  key: root-user
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_ROOT_SECRET}
                  key: root-password
          ports:
            - name: s3
              containerPort: 9000
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: s3
            initialDelaySeconds: 3
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 24
          livenessProbe:
            httpGet:
              path: /minio/health/live
              port: s3
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 6
          volumeMounts:
            - name: data
              mountPath: /data
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: ${STORAGE_STATEFULSET}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: ${STORAGE_STATEFULSET}-ingress
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage
    devint.profile: ${PROFILE_ID}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: ${APP_LABEL}
      app.kubernetes.io/component: object-storage
      devint.profile: ${PROFILE_ID}
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchExpressions:
              - key: app.kubernetes.io/component
                operator: In
                values:
                  - api
                  - object-storage-maintenance
      ports:
        - protocol: TCP
          port: 9000
EOF
}

wait_for_storage_ready() {
  kubectl_cmd -n "${NAMESPACE}" rollout status \
    "statefulset/${STORAGE_STATEFULSET}" --timeout=180s
}

provision_storage() {
  kubectl_cmd -n "${NAMESPACE}" delete job "${STORAGE_PROVISION_JOB}" \
    --ignore-not-found=true >/dev/null
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${STORAGE_PROVISION_JOB}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-maintenance
    devint.profile: ${PROFILE_ID}
spec:
  backoffLimit: 2
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: object-storage-maintenance
        devint.profile: ${PROFILE_ID}
    spec:
      serviceAccountName: ${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}
      restartPolicy: Never
      containers:
        - name: provision
          image: ${STORAGE_CLIENT_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - /bin/sh
            - -ec
          args:
            - |
              until mc alias set storage "${STORAGE_ENDPOINT}" "\${STORAGE_ROOT_USER}" "\${STORAGE_ROOT_PASSWORD}" >/dev/null 2>&1; do
                sleep 2
              done
              mc ready storage
              mc mb --ignore-existing "storage/${STORAGE_BUCKET}"
              mc version enable "storage/${STORAGE_BUCKET}" >/dev/null
              cat >/tmp/api-policy.json <<'POLICY'
              {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket"],"Resource":["arn:aws:s3:::${STORAGE_BUCKET}"]},{"Effect":"Allow","Action":["s3:GetObject","s3:GetObjectVersion","s3:PutObject"],"Resource":["arn:aws:s3:::${STORAGE_BUCKET}/*"]}]}
              POLICY
              mc admin policy create storage wgcf-evidence-api /tmp/api-policy.json >/dev/null 2>&1 || true
              mc admin user add storage "\${STORAGE_APP_ACCESS_KEY}" "\${STORAGE_APP_SECRET_KEY}" >/dev/null
              mc admin policy attach storage wgcf-evidence-api --user "\${STORAGE_APP_ACCESS_KEY}" >/dev/null
              seed_created=false
              if ! mc stat "storage/${STORAGE_BUCKET}/${STORAGE_SEED_KEY}" >/dev/null 2>&1; then
                mc cp /seed/evidence-custody-v1.json "storage/${STORAGE_BUCKET}/${STORAGE_SEED_KEY}" >/dev/null
                seed_created=true
              fi
              actual_digest="\$(mc cat "storage/${STORAGE_BUCKET}/${STORAGE_SEED_KEY}" | sha256sum)"
              actual_digest="\${actual_digest%% *}"
              printf 'bucket=%s\nobject_key=%s\nsha256=%s\nseed_created=%s\nversioning=%s\n' \
                '${STORAGE_BUCKET}' '${STORAGE_SEED_KEY}' "\${actual_digest}" "\${seed_created}" enabled
          env:
            - name: STORAGE_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_ROOT_SECRET}
                  key: root-user
            - name: STORAGE_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_ROOT_SECRET}
                  key: root-password
            - name: STORAGE_APP_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_APP_SECRET}
                  key: access-key
            - name: STORAGE_APP_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  name: ${STORAGE_APP_SECRET}
                  key: secret-key
          volumeMounts:
            - name: seed
              mountPath: /seed
              readOnly: true
      volumes:
        - name: seed
          configMap:
            name: ${STORAGE_STATEFULSET}-seed
EOF
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=complete \
    "job/${STORAGE_PROVISION_JOB}" --timeout=180s
  kubectl_cmd -n "${NAMESPACE}" logs "job/${STORAGE_PROVISION_JOB}" \
    >"${STORAGE_PROVISION_FILE}"
  if ! grep -q "sha256=$(storage_seed_digest)" "${STORAGE_PROVISION_FILE}"; then
    cat "${STORAGE_PROVISION_FILE}" >&2
    echo "Provisioned storage seed digest does not match the profile contract" >&2
    return 1
  fi
  if ! grep -q '^versioning=enabled$' "${STORAGE_PROVISION_FILE}"; then
    cat "${STORAGE_PROVISION_FILE}" >&2
    echo "Evidence storage versioning is not enabled" >&2
    return 1
  fi
}

prove_storage_version_preservation() {
  local accepted_version_id=""
  if [[ -f "${STORAGE_RECEIPT_FILE}" ]]; then
    accepted_version_id="$(python3 - "${STORAGE_RECEIPT_FILE}" <<'PY'
import json
import pathlib
import sys

receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
version_id = receipt.get("object_version_id")
if isinstance(version_id, str) and version_id and version_id != "null":
    print(version_id)
PY
)"
  fi
  kubectl_cmd -n "${NAMESPACE}" exec -i "deployment/${API_DEPLOYMENT}" -- \
    python - preserve-overwrite "$(storage_seed_digest)" "${STORAGE_SEED_KEY}" \
    "${accepted_version_id}" \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py" \
    >"${STORAGE_VERSION_PROOF_FILE}"
}

verify_storage_seed() {
  local binding_source="${1:-proof}"
  local binding_file=""
  case "${binding_source}" in
    proof)
      binding_file="${STORAGE_VERSION_PROOF_FILE}"
      ;;
    receipt)
      binding_file="${STORAGE_RECEIPT_FILE}"
      ;;
    *)
      echo "Unknown storage verification binding source: ${binding_source}" >&2
      return 2
      ;;
  esac
  if [[ ! -f "${binding_file}" ]]; then
    echo "Storage ${binding_source} binding is missing; run the profile up action" >&2
    return 1
  fi
  local accepted_version_id
  accepted_version_id="$(python3 - "${binding_file}" "${binding_source}" <<'PY'
import json
import pathlib
import sys

proof = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
field = "accepted_version_id" if sys.argv[2] == "proof" else "object_version_id"
version_id = proof.get(field)
if not isinstance(version_id, str) or not version_id or version_id == "null":
    raise SystemExit(f"storage {sys.argv[2]} binding has no accepted version ID")
print(version_id)
PY
)"
  kubectl_cmd -n "${NAMESPACE}" exec -i "deployment/${API_DEPLOYMENT}" -- \
    python - verify "$(storage_seed_digest)" "${STORAGE_SEED_KEY}" \
    "${accepted_version_id}" \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py" \
    >"${STORAGE_VERIFICATION_FILE}"
}

write_storage_receipt() {
  python3 - "${STORAGE_RECEIPT_FILE}" "${STORAGE_VERIFICATION_FILE}" \
    "${STORAGE_VERSION_PROOF_FILE}" "${STORAGE_NETWORK_ENFORCEMENT_FILE}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" "${STORAGE_SEED_KEY}" \
    "$(storage_seed_digest)" "${STORAGE_APP_SECRET}" "${COMPONENT_NAME}" \
    "${STORAGE_ISOLATION_FILE}" <<'PY'
from datetime import datetime, timezone
import json
import pathlib
import sys
from urllib.parse import quote

verification = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
proof = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
receipt_path = pathlib.Path(sys.argv[1])
prior_receipt = None
if receipt_path.is_file():
    prior_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
network_proof = pathlib.Path(sys.argv[4]).read_text(encoding="utf-8").splitlines()
isolation = json.loads(pathlib.Path(sys.argv[12]).read_text(encoding="utf-8"))
expected_network_proof = {
    "maintenance_storage_connectivity=allowed",
    "unauthorized_storage_connectivity=denied",
    "api_storage_connectivity=allowed-by-version-read",
}
if set(network_proof) != expected_network_proof:
    raise SystemExit("storage network-enforcement proof is incomplete")
if isolation.get("oos_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OOS credential")
if isolation.get("openproject_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OpenProject credential")
expected_digest = sys.argv[9]
accepted_version_id = proof.get("accepted_version_id")
if verification.get("accepted_sha256") != expected_digest:
    raise SystemExit("storage verification does not bind the expected content digest")
if not isinstance(accepted_version_id, str) or not accepted_version_id:
    raise SystemExit("storage version proof does not bind an accepted object version")
if verification.get("accepted_version_id") != accepted_version_id:
    raise SystemExit("storage verification and version proof bind different object versions")
if not proof.get("same_key_overwrite_proved") or not proof.get("accepted_version_preserved"):
    raise SystemExit("storage version proof does not preserve accepted evidence after overwrite")
version_query = quote(accepted_version_id, safe="-_.~")
payload = {
    "schema_version": 2,
    "receipt_type": "dev-integration-storage",
    "profile_id": sys.argv[5],
    "kubernetes_namespace": sys.argv[6],
    "bucket": sys.argv[7],
    "object_key": sys.argv[8],
    "object_version_id": accepted_version_id,
    "content_sha256": expected_digest,
    "storage_ref": (
        f"wgcf-storage://{sys.argv[5]}/{sys.argv[7]}/{sys.argv[8]}"
        f"?versionId={version_query}"
    ),
    "service_identity_ref": f"kubernetes://{sys.argv[6]}/serviceaccount/{sys.argv[11]}",
    "application_secret_ref": f"kubernetes://{sys.argv[6]}/secret/{sys.argv[10]}",
    "root_credential_exposed_to_api": False,
    "oos_credential_issued": False,
    "openproject_credential_issued": False,
    "network_exposure": "namespace-local-network-policy",
    "credential_isolation_verified_at": isolation.get("verified_at"),
    "network_enforcement": {
        "api_allowed": True,
        "maintenance_allowed": True,
        "unauthorized_pod_denied": True,
    },
    "object_versioning": "enabled",
    "version_preservation": {
        "same_key_overwrite_proved": True,
        "accepted_version_preserved": True,
        "overwrite_version_id": proof["overwrite_version_id"],
        "restored_version_id": proof["restored_version_id"],
    },
    "transport_encryption": "not-governed-dev-integration-http",
    "at_rest_encryption": "not-governed-local-path-pvc",
    "governed_stage_or_prod_claim": False,
    "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
if (
    isinstance(prior_receipt, dict)
    and prior_receipt.get("object_version_id") == accepted_version_id
    and prior_receipt.get("content_sha256") == expected_digest
):
    for field in ("restore_supersession", "pre_restore_version_preservation"):
        if field in prior_receipt:
            payload[field] = prior_receipt[field]
receipt_path.write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

refresh_storage_receipt_isolation() {
  python3 - "${STORAGE_RECEIPT_FILE}" "${STORAGE_ISOLATION_FILE}" <<'PY'
import json
import pathlib
import sys

receipt_path = pathlib.Path(sys.argv[1])
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
isolation = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
if isolation.get("oos_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OOS credential")
if isolation.get("openproject_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OpenProject credential")
receipt["root_credential_exposed_to_api"] = False
receipt["oos_credential_issued"] = False
receipt["openproject_credential_issued"] = False
receipt["credential_isolation_verified_at"] = isolation.get("verified_at")
receipt_path.write_text(
    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

verify_storage_isolation() {
  local pods_file="${STATE_ROOT}/storage-isolation-pods.json"
  local network_policy_file="${STATE_ROOT}/storage-isolation-network-policy.json"
  local deployment_file="${STATE_ROOT}/storage-isolation-api-deployment.json"
  local replica_sets_file="${STATE_ROOT}/storage-isolation-api-replica-sets.json"
  local stateful_set_file="${STATE_ROOT}/storage-isolation-stateful-set.json"
  local jobs_file="${STATE_ROOT}/storage-isolation-jobs.json"
  kubectl_cmd -n "${NAMESPACE}" get pods -o json >"${pods_file}"
  kubectl_cmd -n "${NAMESPACE}" get networkpolicy \
    "${STORAGE_STATEFULSET}-ingress" -o json >"${network_policy_file}"
  kubectl_cmd -n "${NAMESPACE}" get deployment "${API_DEPLOYMENT}" -o json \
    >"${deployment_file}"
  kubectl_cmd -n "${NAMESPACE}" get replicasets -o json >"${replica_sets_file}"
  kubectl_cmd -n "${NAMESPACE}" get statefulset "${STORAGE_STATEFULSET}" -o json \
    >"${stateful_set_file}"
  kubectl_cmd -n "${NAMESPACE}" get jobs -o json >"${jobs_file}"
  python3 "${PROFILE_ROOT}/scripts/lib/verify_storage_isolation.py" \
    "${STORAGE_ISOLATION_FILE}" "${pods_file}" \
    "${network_policy_file}" "${deployment_file}" "${replica_sets_file}" \
    "${stateful_set_file}" "${jobs_file}" "${API_DEPLOYMENT}" "${STORAGE_STATEFULSET}" \
    "${STORAGE_PROVISION_JOB}" "${COMPONENT_NAME}" \
    "${STORAGE_SERVICE_ACCOUNT}" "${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}" \
    "${STORAGE_APP_SECRET}" "${STORAGE_ROOT_SECRET}" "${APP_LABEL}" "${PROFILE_ID}"
  rm -f "${pods_file}" "${network_policy_file}" "${deployment_file}" \
    "${replica_sets_file}" "${stateful_set_file}" "${jobs_file}"
}

verify_storage_network_enforcement() {
  local allow_job="${PROFILE_ID}-storage-net-allow"
  local deny_job="${PROFILE_ID}-storage-net-deny"
  kubectl_cmd -n "${NAMESPACE}" delete job "${allow_job}" "${deny_job}" \
    --ignore-not-found=true >/dev/null
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${allow_job}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-maintenance
    devint.profile: ${PROFILE_ID}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: object-storage-maintenance
        devint.profile: ${PROFILE_ID}
    spec:
      serviceAccountName: ${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}
      restartPolicy: Never
      containers:
        - name: network-allow
          image: ${API_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - python
            - -c
          args:
            - |
              import socket
              import time
              for attempt in range(10):
                  try:
                      with socket.create_connection(("${STORAGE_SERVICE}", 9000), timeout=3):
                          pass
                  except OSError:
                      if attempt == 9:
                          raise
                      time.sleep(1)
                  else:
                      print("maintenance_storage_connectivity=allowed")
                      break
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
---
apiVersion: batch/v1
kind: Job
metadata:
  name: ${deny_job}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-denial-proof
    devint.profile: ${PROFILE_ID}
spec:
  backoffLimit: 0
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: object-storage-denial-proof
        devint.profile: ${PROFILE_ID}
    spec:
      restartPolicy: Never
      containers:
        - name: network-deny
          image: ${API_IMAGE}
          imagePullPolicy: IfNotPresent
          command:
            - python
            - -c
          args:
            - |
              import socket
              try:
                  connection = socket.create_connection(("${STORAGE_SERVICE}", 9000), timeout=5)
              except OSError:
                  print("unauthorized_storage_connectivity=denied")
              else:
                  connection.close()
                  raise SystemExit("unselected pod unexpectedly reached evidence storage")
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
EOF
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=complete \
    "job/${allow_job}" --timeout=90s
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=complete \
    "job/${deny_job}" --timeout=90s
  {
    kubectl_cmd -n "${NAMESPACE}" logs "job/${allow_job}"
    kubectl_cmd -n "${NAMESPACE}" logs "job/${deny_job}"
    printf 'api_storage_connectivity=allowed-by-version-read\n'
  } >"${STORAGE_NETWORK_ENFORCEMENT_FILE}"
  verify_storage_network_proof
  kubectl_cmd -n "${NAMESPACE}" delete job "${allow_job}" "${deny_job}" \
    --ignore-not-found=true >/dev/null
}

verify_storage_network_proof() {
  if [[ ! -f "${STORAGE_NETWORK_ENFORCEMENT_FILE}" ]]; then
    echo "Storage network-enforcement proof is missing; run the profile up action" >&2
    return 1
  fi
  grep -qx 'maintenance_storage_connectivity=allowed' \
    "${STORAGE_NETWORK_ENFORCEMENT_FILE}"
  grep -qx 'unauthorized_storage_connectivity=denied' \
    "${STORAGE_NETWORK_ENFORCEMENT_FILE}"
  grep -qx 'api_storage_connectivity=allowed-by-version-read' \
    "${STORAGE_NETWORK_ENFORCEMENT_FILE}"
}

create_storage_transfer_pod() {
  local credential_kind="$1"
  local secret_name access_key_field secret_key_field
  case "${credential_kind}" in
    application)
      secret_name="${STORAGE_APP_SECRET}"
      access_key_field="access-key"
      secret_key_field="secret-key"
      ;;
    root)
      secret_name="${STORAGE_ROOT_SECRET}"
      access_key_field="root-user"
      secret_key_field="root-password"
      ;;
    *)
      echo "unknown storage transfer credential kind: ${credential_kind}" >&2
      return 2
      ;;
  esac

  kubectl_cmd -n "${NAMESPACE}" delete pod "${STORAGE_TRANSFER_POD}" \
    --ignore-not-found=true --wait=true >/dev/null
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${STORAGE_TRANSFER_POD}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-maintenance
    devint.profile: ${PROFILE_ID}
spec:
  serviceAccountName: ${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}
  restartPolicy: Never
  securityContext:
    fsGroup: 10001
  containers:
    - name: transfer
      image: ${STORAGE_CLIENT_IMAGE}
      imagePullPolicy: IfNotPresent
      command:
        - /bin/sh
        - -ec
      args:
        - sleep 3600
      env:
        - name: STORAGE_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: ${secret_name}
              key: ${access_key_field}
        - name: STORAGE_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: ${secret_name}
              key: ${secret_key_field}
      volumeMounts:
        - name: transfer
          mountPath: /transfer
    - name: archive
      image: busybox:1.36.1
      imagePullPolicy: IfNotPresent
      command:
        - /bin/sh
        - -ec
      args:
        - sleep 3600
      volumeMounts:
        - name: transfer
          mountPath: /transfer
    - name: rebind
      image: ${API_IMAGE}
      imagePullPolicy: IfNotPresent
      command:
        - /bin/sh
        - -ec
      args:
        - sleep 3600
      env:
        - name: WGCF_EVIDENCE_STORAGE_ENDPOINT
          value: ${STORAGE_ENDPOINT}
        - name: WGCF_EVIDENCE_STORAGE_BUCKET
          value: ${STORAGE_BUCKET}
        - name: WGCF_EVIDENCE_STORAGE_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: ${STORAGE_APP_SECRET}
              key: access-key
        - name: WGCF_EVIDENCE_STORAGE_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: ${STORAGE_APP_SECRET}
              key: secret-key
      volumeMounts:
        - name: transfer
          mountPath: /transfer
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
            - ALL
  volumes:
    - name: transfer
      emptyDir: {}
EOF
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=Ready \
    "pod/${STORAGE_TRANSFER_POD}" --timeout=120s
}

delete_storage_transfer_pod() {
  kubectl_cmd -n "${NAMESPACE}" delete pod "${STORAGE_TRANSFER_POD}" \
    --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
}

validate_backup_output_path() {
  local backup_path="$1"
  python3 - "${backup_path}" "${BACKUPS_DIR}" <<'PY'
import pathlib
import sys

backup = pathlib.Path(sys.argv[1])
backup_root = pathlib.Path(sys.argv[2]).resolve()
if not backup.is_absolute():
    raise SystemExit("WGCF evidence backup path must be absolute")
resolved_parent = backup.parent.resolve()
if resolved_parent != backup_root:
    raise SystemExit("WGCF evidence backups must be written directly under the profile backups directory")
if not backup.name.endswith(".tar.gz"):
    raise SystemExit("WGCF evidence backup path must end with .tar.gz")
if backup.is_symlink():
    raise SystemExit("WGCF evidence backup path may not be a symbolic link")
manifest = pathlib.Path(f"{backup}.manifest.json")
if backup.exists() or manifest.exists() or manifest.is_symlink():
    raise SystemExit("WGCF evidence backup target or manifest already exists")
print(backup_root / backup.name)
PY
}

stage_receipt_bound_evidence() {
  local -a receipt_fields=()
  if [[ ! -f "${STORAGE_RECEIPT_FILE}" ]]; then
    echo "Storage receipt is missing; run the profile up action before backup" >&2
    return 1
  fi
  mapfile -t receipt_fields < <(python3 - "${STORAGE_RECEIPT_FILE}" <<'PY'
import json
import pathlib
import sys

receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
required = ["object_key", "object_version_id", "content_sha256"]
for field in required:
    value = receipt.get(field)
    if not isinstance(value, str) or not value or "\n" in value:
        raise SystemExit(f"storage receipt has invalid {field}")
if receipt.get("schema_version") != 2 or receipt.get("receipt_type") != "dev-integration-storage":
    raise SystemExit("storage receipt is not a version-bound WGCF receipt")
for field in required:
    print(receipt[field])
PY
  )
  if [[ "${#receipt_fields[@]}" -ne 3 ]]; then
    echo "Storage receipt fields could not be staged" >&2
    return 1
  fi
  local object_key="${receipt_fields[0]}"
  local version_id="${receipt_fields[1]}"
  local expected_digest="${receipt_fields[2]}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    mkdir -p /transfer/package/receipt-bound /transfer/package/receipt-records
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    /bin/sh -ec 'cat > /transfer/package/receipt-records/storage-receipt.json' \
    <"${STORAGE_RECEIPT_FILE}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- \
    /bin/sh -ec \
    'mc alias set storage "$1" "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc cp --quiet --version-id "$2" "$3" "$4"' \
    sh "${STORAGE_ENDPOINT}" "${version_id}" "storage/${STORAGE_BUCKET}/${object_key}" \
    /transfer/package/receipt-bound/storage-receipt.bin
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    /bin/sh -ec 'test "$(sha256sum "$1" | cut -d " " -f 1)" = "$2"' \
    sh /transfer/package/receipt-bound/storage-receipt.bin "${expected_digest}"
}

write_backup_manifest() {
  local backup_path="$1"
  local receipt_path="$2"
  local published_path="$3"
  python3 - "${backup_path}" "${receipt_path}" "${PROFILE_ID}" \
    "${NAMESPACE}" "${STORAGE_BUCKET}" "${published_path}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1]).resolve()
published_archive = pathlib.Path(sys.argv[6]).resolve()
members = {}
with tarfile.open(archive, "r:gz") as bundle:
    for member in sorted(bundle.getmembers(), key=lambda item: item.name):
        if not member.isfile():
            continue
        name = member.name.removeprefix("./")
        path = pathlib.PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name in members:
            raise SystemExit(f"backup contains an unsafe or duplicate member: {name}")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"backup member cannot be read: {name}")
        members[name] = source.read()

objects = []
for name, body in sorted(members.items()):
    if not name.startswith("current/"):
        continue
    object_key = name.removeprefix("current/")
    object_path = pathlib.PurePosixPath(object_key)
    if not object_key or object_path.is_absolute() or ".." in object_path.parts:
        raise SystemExit(f"backup contains an unsafe object key: {object_key}")
    objects.append({
            "object_key": object_key,
            "archive_path": name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        })
if not objects:
    raise SystemExit("storage backup contains no objects")

receipt_name = "storage-receipt"
receipt_archive_path = "receipt-records/storage-receipt.json"
body_archive_path = "receipt-bound/storage-receipt.bin"
if receipt_archive_path not in members or body_archive_path not in members:
    raise SystemExit("storage backup does not contain receipt-bound evidence")
receipt_record = json.loads(members[receipt_archive_path].decode())
object_key = receipt_record.get("object_key")
current = next((item for item in objects if item["object_key"] == object_key), None)
if current is None:
    raise SystemExit("receipt-bound object has no current backup member")
bound_digest = hashlib.sha256(members[body_archive_path]).hexdigest()
if bound_digest != receipt_record.get("content_sha256"):
    raise SystemExit("receipt-bound backup bytes do not match their receipt")
receipt_bindings = [{
    "receipt_name": receipt_name,
    "receipt_archive_path": receipt_archive_path,
    "body_archive_path": body_archive_path,
    "current_archive_path": current["archive_path"],
    "object_key": object_key,
    "prior_object_version_id": receipt_record.get("object_version_id"),
    "prior_storage_ref": receipt_record.get("storage_ref"),
    "content_sha256": bound_digest,
    "body_size": len(members[body_archive_path]),
    "receipt_record_sha256": hashlib.sha256(members[receipt_archive_path]).hexdigest(),
    "receipt_record_size": len(members[receipt_archive_path]),
}]
archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
manifest = {
    "schema_version": 2,
    "profile_id": sys.argv[3],
    "kubernetes_namespace": sys.argv[4],
    "bucket": sys.argv[5],
    "backup_path": str(published_archive),
    "archive_sha256": archive_digest,
    "objects": objects,
    "receipt_bindings": receipt_bindings,
    "version_ids_preserved": False,
    "restore_requires_receipt_rebinding": True,
    "credentials_included": False,
    "created_at": created_at,
}
pathlib.Path(f"{archive}.manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
receipt = {
    "schema_version": 2,
    "receipt_type": "dev-integration-storage-backup",
    "profile_id": sys.argv[3],
    "kubernetes_namespace": sys.argv[4],
    "bucket": sys.argv[5],
    "backup_path": str(published_archive),
    "archive_sha256": archive_digest,
    "object_count": len(objects),
    "receipt_binding_count": len(receipt_bindings),
    "content_addresses": sorted({
        *[item["sha256"] for item in objects],
        *[item["content_sha256"] for item in receipt_bindings],
    }),
    "version_ids_preserved": False,
    "restore_requires_receipt_rebinding": True,
    "credentials_included": False,
    "completed_at": created_at,
}
pathlib.Path(sys.argv[2]).write_text(
    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

backup_evidence_storage() {
  local backup_path="$1"
  local receipt_path="$2"
  local backup_name=""
  backup_path="$(validate_backup_output_path "${backup_path}")"
  wait_for_storage_ready
  mkdir -p "${BACKUPS_DIR}"
  backup_name="$(basename "${backup_path}")"
  STORAGE_BACKUP_STAGING_ARCHIVE="$(mktemp "${BACKUPS_DIR}/.${backup_name}.XXXXXX.partial")"
  STORAGE_BACKUP_STAGING_MANIFEST="${STORAGE_BACKUP_STAGING_ARCHIVE}.manifest.json"
  STORAGE_BACKUP_STAGING_RECEIPT="$(mktemp "${BACKUPS_DIR}/.${backup_name}.receipt.XXXXXX.partial")"
  create_storage_transfer_pod application
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; rm -rf /transfer/package; mkdir -p /transfer/package/current; mc mirror --overwrite storage/'"'${STORAGE_BUCKET}'"' /transfer/package/current >/dev/null'
  stage_receipt_bound_evidence
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    tar -C /transfer/package -czf - . >"${STORAGE_BACKUP_STAGING_ARCHIVE}"
  delete_storage_transfer_pod
  write_backup_manifest "${STORAGE_BACKUP_STAGING_ARCHIVE}" \
    "${STORAGE_BACKUP_STAGING_RECEIPT}" "${backup_path}"
  ln -- "${STORAGE_BACKUP_STAGING_MANIFEST}" "${backup_path}.manifest.json"
  if ! ln -- "${STORAGE_BACKUP_STAGING_ARCHIVE}" "${backup_path}"; then
    rm -f -- "${backup_path}.manifest.json"
    return 1
  fi
  mv -- "${STORAGE_BACKUP_STAGING_RECEIPT}" "${receipt_path}"
  cleanup_storage_backup_staging
}

archive_storage_backups() {
  local archive_path=""
  local -a backup_files=()
  if [[ -d "${BACKUPS_DIR}" ]]; then
    while IFS= read -r -d '' backup_file; do
      backup_files+=("${backup_file}")
    done < <(
      find "${BACKUPS_DIR}" -maxdepth 1 -type f \
        \( -name '*.tar.gz' -o -name '*.tar.gz.manifest.json' \) -print0
    )
  fi
  if [[ "${#backup_files[@]}" -eq 0 ]]; then
    return
  fi

  archive_path="${ARCHIVE_ROOT}/reset-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "${archive_path}"
  for backup_file in "${backup_files[@]}"; do
    mv -- "${backup_file}" "${archive_path}/"
  done
  if [[ -f "${STORAGE_BACKUP_RECEIPT_FILE}" ]]; then
    cp "${STORAGE_BACKUP_RECEIPT_FILE}" "${archive_path}/latest-backup-receipt.json"
  fi
  printf '%s\n' "${archive_path}"
}

validate_backup_for_restore() {
  local backup_path="$1"
  python3 - "${backup_path}" "${STATE_ROOT}" "${ARCHIVE_ROOT}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" <<'PY'
import hashlib
import json
import pathlib
import sys
import tarfile

backup = pathlib.Path(sys.argv[1]).resolve()
allowed_roots = [pathlib.Path(value).resolve() for value in sys.argv[2:4]]
if not backup.is_file():
    raise SystemExit(f"restore backup does not exist: {backup}")
if not any(root == backup or root in backup.parents for root in allowed_roots):
    raise SystemExit("restore backup must stay under the operator profile state or reset archive")
manifest_path = pathlib.Path(f"{backup}.manifest.json")
if not manifest_path.is_file():
    raise SystemExit(f"restore backup manifest is missing: {manifest_path}")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema_version") != 2:
    raise SystemExit("restore backup must use the version-bound schema")
if manifest.get("archive_sha256") != hashlib.sha256(backup.read_bytes()).hexdigest():
    raise SystemExit("restore backup digest does not match its manifest")
recorded_backup_path = manifest.get("backup_path")
if not isinstance(recorded_backup_path, str) or not pathlib.Path(recorded_backup_path).is_absolute():
    raise SystemExit("restore backup manifest has invalid original-path provenance")
if manifest.get("profile_id") != sys.argv[4]:
    raise SystemExit("restore backup belongs to a different profile")
if manifest.get("kubernetes_namespace") != sys.argv[5]:
    raise SystemExit("restore backup belongs to a different Kubernetes namespace")
if manifest.get("bucket") != sys.argv[6]:
    raise SystemExit("restore backup belongs to a different bucket")
if manifest.get("credentials_included") is not False:
    raise SystemExit("restore backup may not contain credentials")
if manifest.get("version_ids_preserved") is not False:
    raise SystemExit("restore manifest has an invalid version-preservation claim")
if manifest.get("restore_requires_receipt_rebinding") is not True:
    raise SystemExit("restore manifest does not require receipt rebinding")
expected = {}
for item in manifest.get("objects") or []:
    object_key = item.get("object_key")
    archive_path = item.get("archive_path")
    digest = item.get("sha256")
    size = item.get("size")
    if not isinstance(object_key, str) or not object_key or object_key in expected:
        raise SystemExit("restore manifest contains an invalid or duplicate object key")
    if archive_path != f"current/{object_key}":
        raise SystemExit(f"restore manifest contains an invalid current object path: {object_key}")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(size, int) or size < 0:
        raise SystemExit(f"restore manifest contains invalid object evidence: {object_key}")
    expected[archive_path] = (digest, size)
if not expected:
    raise SystemExit("restore manifest contains no objects")

receipt_names = set()
for binding in manifest.get("receipt_bindings") or []:
    receipt_name = binding.get("receipt_name")
    object_key = binding.get("object_key")
    if not isinstance(receipt_name, str) or not receipt_name or receipt_name in receipt_names:
        raise SystemExit("restore manifest contains an invalid or duplicate receipt binding")
    receipt_names.add(receipt_name)
    if binding.get("current_archive_path") != f"current/{object_key}":
        raise SystemExit(f"restore manifest receipt binding has no current object: {receipt_name}")
    body_path = binding.get("body_archive_path")
    receipt_path = binding.get("receipt_archive_path")
    if body_path != f"receipt-bound/{receipt_name}.bin":
        raise SystemExit(f"restore manifest contains an invalid bound body path: {receipt_name}")
    if receipt_path != f"receipt-records/{receipt_name}.json":
        raise SystemExit(f"restore manifest contains an invalid receipt path: {receipt_name}")
    expected[body_path] = (binding.get("content_sha256"), binding.get("body_size"))
    expected[receipt_path] = (
        binding.get("receipt_record_sha256"),
        binding.get("receipt_record_size"),
    )
if not receipt_names:
    raise SystemExit("restore manifest contains no receipt-bound evidence")
actual = {}
seen_paths = set()
with tarfile.open(backup, "r:gz") as bundle:
    for member in bundle.getmembers():
        archive_path = member.name.removeprefix("./")
        member_path = pathlib.PurePosixPath(archive_path)
        if member_path.is_absolute() or ".." in member_path.parts or archive_path in seen_paths:
            raise SystemExit(f"restore archive contains an unsafe or duplicate path: {archive_path}")
        seen_paths.add(archive_path)
        if member.isdir():
            continue
        if not member.isfile():
            raise SystemExit(f"restore archive contains an unsupported member: {archive_path}")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"restore archive member cannot be read: {archive_path}")
        body = source.read()
        actual[archive_path] = (hashlib.sha256(body).hexdigest(), len(body))
if actual != expected:
    raise SystemExit("restore archive objects do not match the signed manifest evidence")
PY
}

restore_evidence_storage() {
  local backup_path="$1"
  local verification_archive="${STATE_ROOT}/restore-verification.tar.gz"
  wait_for_storage_ready
  create_storage_transfer_pod root
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c archive -- /bin/sh -ec \
    'cat >/transfer/restore.tar.gz; rm -rf /transfer/restore /transfer/verify; mkdir -p /transfer/restore /transfer/verify; tar -C /transfer/restore -xzf /transfer/restore.tar.gz; mkdir -p /transfer/restore/rebound-receipts; chgrp -R 10001 /transfer/restore /transfer/verify; chmod -R g+rwX /transfer/restore /transfer/verify' \
    <"${backup_path}"
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c archive -- /bin/sh -ec \
    'cat >/transfer/restore-manifest.json' <"${backup_path}.manifest.json"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    /bin/sh -ec 'touch /transfer/receipt-rebindings.json; chgrp 10001 /transfer/receipt-rebindings.json; chmod g+rw /transfer/receipt-rebindings.json'
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc mirror --overwrite --remove /transfer/restore/current storage/'"'${STORAGE_BUCKET}'"' >/dev/null'
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c rebind -- \
    python - rebind /transfer/restore /transfer/restore-manifest.json \
    /transfer/receipt-rebindings.json \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c rebind -- \
    cat /transfer/receipt-rebindings.json >"${STORAGE_RECEIPT_REBINDING_FILE}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c rebind -- \
    cat /transfer/restore/rebound-receipts/storage-receipt.json >"${STORAGE_RECEIPT_FILE}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc mirror --overwrite storage/'"'${STORAGE_BUCKET}'"' /transfer/verify >/dev/null'
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    tar -C /transfer/verify -czf - . >"${verification_archive}"
  delete_storage_transfer_pod
  python3 - "${backup_path}.manifest.json" "${verification_archive}" <<'PY'
import hashlib
import json
import pathlib
import sys
import tarfile

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = {
    item["object_key"]: (item["sha256"], item["size"])
    for item in manifest["objects"]
}
actual = {}
with tarfile.open(sys.argv[2], "r:gz") as bundle:
    for member in bundle.getmembers():
        if not member.isfile():
            continue
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"restored member cannot be read: {member.name}")
        body = source.read()
        actual[member.name.removeprefix("./")] = (hashlib.sha256(body).hexdigest(), len(body))
if actual != expected:
    raise SystemExit("restored object set does not preserve the backup content addresses")
PY
  rm -f "${verification_archive}"
}

write_restore_receipt() {
  local backup_path="$1"
  local pre_restore_path="$2"
  python3 - "${STORAGE_RESTORE_RECEIPT_FILE}" "${backup_path}" \
    "${pre_restore_path}" "${STORAGE_RECEIPT_REBINDING_FILE}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys

backup = pathlib.Path(sys.argv[2]).resolve()
pre_restore = pathlib.Path(sys.argv[3]).resolve()
rebindings = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
if rebindings.get("receipt_identity_rebound") is not True:
    raise SystemExit("restore receipt rebinding proof is incomplete")
payload = {
    "schema_version": 2,
    "receipt_type": "dev-integration-storage-restore",
    "profile_id": sys.argv[5],
    "kubernetes_namespace": sys.argv[6],
    "bucket": sys.argv[7],
    "restored_from": str(backup),
    "restored_archive_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "pre_restore_backup": str(pre_restore),
    "pre_restore_archive_sha256": hashlib.sha256(pre_restore.read_bytes()).hexdigest(),
    "content_addresses_preserved": True,
    "version_ids_preserved": False,
    "receipt_identity_rebound": True,
    "receipt_rebindings": rebindings["receipt_rebindings"],
    "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}
