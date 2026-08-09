#!/usr/bin/env bash

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
import json
import pathlib
import sys

profile = json.loads(sys.argv[1])
workspace_root = pathlib.Path(sys.argv[2]).resolve()
expected = {
    "repo": "security-architecture",
    "path": "docs/reviews/components/2026-08-09-art-evidence-custody-and-source-provenance.md",
}
refs = (profile.get("security") or {}).get("activation_review_refs") or []
if expected not in refs:
    raise SystemExit("WGCF evidence storage requires the approved Security activation review reference")
review_path = workspace_root / expected["repo"] / expected["path"]
if not review_path.is_file():
    raise SystemExit(f"WGCF evidence storage Security review is unavailable: {review_path}")
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
              {"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetBucketLocation","s3:ListBucket"],"Resource":["arn:aws:s3:::${STORAGE_BUCKET}"]},{"Effect":"Allow","Action":["s3:GetObject","s3:PutObject"],"Resource":["arn:aws:s3:::${STORAGE_BUCKET}/*"]}]}
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

verify_storage_seed() {
  kubectl_cmd -n "${NAMESPACE}" exec -i "deployment/${API_DEPLOYMENT}" -- \
    python - "$(storage_seed_digest)" >"${STATE_ROOT}/storage-verification.json" <<'PY'
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import sys
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

endpoint = os.environ["WGCF_EVIDENCE_STORAGE_ENDPOINT"].rstrip("/")
bucket = os.environ["WGCF_EVIDENCE_STORAGE_BUCKET"]
access_key = os.environ["WGCF_EVIDENCE_STORAGE_ACCESS_KEY"]
secret_key = os.environ["WGCF_EVIDENCE_STORAGE_SECRET_KEY"]
object_key = "profile-proof/evidence-custody-v1.json"
expected_digest = sys.argv[1]

assert "MINIO_ROOT_USER" not in os.environ
assert "MINIO_ROOT_PASSWORD" not in os.environ

parsed = urlsplit(endpoint)
host = parsed.netloc
canonical_uri = f"/{quote(bucket, safe='')}/{quote(object_key, safe='/')}"
now = datetime.now(timezone.utc)
amz_date = now.strftime("%Y%m%dT%H%M%SZ")
date_stamp = now.strftime("%Y%m%d")
payload_hash = hashlib.sha256(b"").hexdigest()
canonical_headers = (
    f"host:{host}\n"
    f"x-amz-content-sha256:{payload_hash}\n"
    f"x-amz-date:{amz_date}\n"
)
signed_headers = "host;x-amz-content-sha256;x-amz-date"
scope = f"{date_stamp}/us-east-1/s3/aws4_request"

def sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()

def signed_request(method: str) -> Request:
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    date_key = sign(("AWS4" + secret_key).encode(), date_stamp)
    region_key = sign(date_key, "us-east-1")
    service_key = sign(region_key, "s3")
    signing_key = sign(service_key, "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return Request(
        f"{endpoint}{canonical_uri}",
        headers={
            "Authorization": authorization,
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        },
        method=method,
    )

with urlopen(signed_request("GET"), timeout=20) as response:
    body = response.read()
actual_digest = hashlib.sha256(body).hexdigest()
if actual_digest != expected_digest:
    raise SystemExit(f"storage seed digest mismatch: {actual_digest}")
delete_denied = False
try:
    urlopen(signed_request("DELETE"), timeout=20)
except HTTPError as error:
    delete_denied = error.code == 403
if not delete_denied:
    raise SystemExit("application storage credential unexpectedly permits object deletion")
print(json.dumps({
    "bucket": bucket,
    "object_key": object_key,
    "sha256": actual_digest,
    "application_credential_read": True,
    "application_credential_delete_denied": delete_denied,
    "root_credential_absent": True,
}, indent=2, sort_keys=True))
PY
}

write_storage_receipt() {
  python3 - "${STORAGE_RECEIPT_FILE}" "${STATE_ROOT}/storage-verification.json" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" "${STORAGE_SEED_KEY}" \
    "$(storage_seed_digest)" "${STORAGE_APP_SECRET}" "${COMPONENT_NAME}" <<'PY'
from datetime import datetime, timezone
import json
import pathlib
import sys

verification = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
expected_digest = sys.argv[7]
if verification.get("sha256") != expected_digest:
    raise SystemExit("storage verification does not bind the expected content digest")
payload = {
    "schema_version": 1,
    "receipt_type": "dev-integration-storage",
    "profile_id": sys.argv[3],
    "kubernetes_namespace": sys.argv[4],
    "bucket": sys.argv[5],
    "object_key": sys.argv[6],
    "content_sha256": expected_digest,
    "storage_ref": f"wgcf-storage://{sys.argv[3]}/{sys.argv[5]}/{sys.argv[6]}",
    "service_identity_ref": f"kubernetes://{sys.argv[4]}/serviceaccount/{sys.argv[9]}",
    "application_secret_ref": f"kubernetes://{sys.argv[4]}/secret/{sys.argv[8]}",
    "root_credential_exposed_to_api": False,
    "oos_credential_issued": False,
    "openproject_credential_issued": False,
    "network_exposure": "namespace-local-network-policy",
    "object_versioning": "enabled",
    "transport_encryption": "not-governed-dev-integration-http",
    "at_rest_encryption": "not-governed-local-path-pvc",
    "governed_stage_or_prod_claim": False,
    "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

verify_storage_isolation() {
  local pods_file="${STATE_ROOT}/storage-isolation-pods.json"
  local network_policy_file="${STATE_ROOT}/storage-isolation-network-policy.json"
  kubectl_cmd -n "${NAMESPACE}" get pods -o json >"${pods_file}"
  kubectl_cmd -n "${NAMESPACE}" get networkpolicy \
    "${STORAGE_STATEFULSET}-ingress" -o json >"${network_policy_file}"
  python3 "${PROFILE_ROOT}/scripts/lib/verify_storage_isolation.py" \
    "${STORAGE_ISOLATION_FILE}" "${pods_file}" \
    "${network_policy_file}" "${API_DEPLOYMENT}" "${STORAGE_STATEFULSET}" \
    "${STORAGE_PROVISION_JOB}" "${STORAGE_TRANSFER_POD}" "${COMPONENT_NAME}" \
    "${STORAGE_SERVICE_ACCOUNT}" "${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}" \
    "${STORAGE_APP_SECRET}" "${STORAGE_ROOT_SECRET}" "${APP_LABEL}" "${PROFILE_ID}"
  rm -f "${pods_file}" "${network_policy_file}"
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

write_backup_manifest() {
  local backup_path="$1"
  local receipt_path="$2"
  python3 - "${backup_path}" "${receipt_path}" "${PROFILE_ID}" \
    "${NAMESPACE}" "${STORAGE_BUCKET}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1]).resolve()
objects = []
with tarfile.open(archive, "r:gz") as bundle:
    for member in sorted(bundle.getmembers(), key=lambda item: item.name):
        if not member.isfile():
            continue
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"backup member cannot be read: {member.name}")
        body = source.read()
        objects.append({
            "object_key": member.name.removeprefix("./"),
            "sha256": hashlib.sha256(body).hexdigest(),
            "size": len(body),
        })
if not objects:
    raise SystemExit("storage backup contains no objects")
archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
manifest = {
    "schema_version": 1,
    "profile_id": sys.argv[3],
    "kubernetes_namespace": sys.argv[4],
    "bucket": sys.argv[5],
    "backup_path": str(archive),
    "archive_sha256": archive_digest,
    "objects": objects,
    "credentials_included": False,
    "created_at": created_at,
}
pathlib.Path(f"{archive}.manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
receipt = {
    "schema_version": 1,
    "receipt_type": "dev-integration-storage-backup",
    "profile_id": sys.argv[3],
    "kubernetes_namespace": sys.argv[4],
    "bucket": sys.argv[5],
    "backup_path": str(archive),
    "archive_sha256": archive_digest,
    "object_count": len(objects),
    "content_addresses": [item["sha256"] for item in objects],
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
  wait_for_storage_ready
  mkdir -p "$(dirname "${backup_path}")"
  create_storage_transfer_pod application
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; rm -rf /transfer/objects; mkdir -p /transfer/objects; mc mirror --overwrite storage/'"'${STORAGE_BUCKET}'"' /transfer/objects >/dev/null'
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    tar -C /transfer/objects -czf - . >"${backup_path}"
  delete_storage_transfer_pod
  write_backup_manifest "${backup_path}" "${receipt_path}"
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
expected = {}
for item in manifest.get("objects") or []:
    object_key = item.get("object_key")
    digest = item.get("sha256")
    size = item.get("size")
    if not isinstance(object_key, str) or not object_key or object_key in expected:
        raise SystemExit("restore manifest contains an invalid or duplicate object key")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(size, int) or size < 0:
        raise SystemExit(f"restore manifest contains invalid object evidence: {object_key}")
    expected[object_key] = (digest, size)
if not expected:
    raise SystemExit("restore manifest contains no objects")
actual = {}
with tarfile.open(backup, "r:gz") as bundle:
    for member in bundle.getmembers():
        if not member.isfile():
            continue
        object_key = member.name.removeprefix("./")
        object_path = pathlib.PurePosixPath(object_key)
        if object_path.is_absolute() or ".." in object_path.parts or object_key in actual:
            raise SystemExit(f"restore archive contains an unsafe or duplicate object key: {object_key}")
        source = bundle.extractfile(member)
        if source is None:
            raise SystemExit(f"restore archive member cannot be read: {object_key}")
        body = source.read()
        actual[object_key] = (hashlib.sha256(body).hexdigest(), len(body))
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
    'cat >/transfer/restore.tar.gz; rm -rf /transfer/restore /transfer/verify; mkdir -p /transfer/restore /transfer/verify; tar -C /transfer/restore -xzf /transfer/restore.tar.gz' \
    <"${backup_path}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc mirror --overwrite --remove /transfer/restore storage/'"'${STORAGE_BUCKET}'"' >/dev/null; mc mirror --overwrite storage/'"'${STORAGE_BUCKET}'"' /transfer/verify >/dev/null'
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
    "${pre_restore_path}" "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import pathlib
import sys

backup = pathlib.Path(sys.argv[2]).resolve()
pre_restore = pathlib.Path(sys.argv[3]).resolve()
payload = {
    "schema_version": 1,
    "receipt_type": "dev-integration-storage-restore",
    "profile_id": sys.argv[4],
    "kubernetes_namespace": sys.argv[5],
    "bucket": sys.argv[6],
    "restored_from": str(backup),
    "restored_archive_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "pre_restore_backup": str(pre_restore),
    "pre_restore_archive_sha256": hashlib.sha256(pre_restore.read_bytes()).hexdigest(),
    "content_addresses_preserved": True,
    "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}
