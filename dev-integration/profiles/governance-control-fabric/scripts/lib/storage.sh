#!/usr/bin/env bash

readonly STORAGE_ROOT_USER_ID="wgcf-root"
readonly STORAGE_APP_ACCESS_KEY_ID="wgcf-evidence-api"

STORAGE_BACKUP_STAGING_ARCHIVE=""
STORAGE_BACKUP_STAGING_MANIFEST=""
STORAGE_BACKUP_STAGING_RECEIPT=""
STORAGE_BACKUP_PUBLISHED_ARCHIVE=""
STORAGE_BACKUP_PUBLISHED_MANIFEST=""
STORAGE_CREDENTIAL_ROTATION_DETECTED="false"
STORAGE_RETIRED_ROOT_USER=""
STORAGE_RETIRED_ROOT_PASSWORD=""
STORAGE_RETIRED_APP_ACCESS_KEY=""
STORAGE_RETIRED_APP_SECRET_KEY=""

cleanup_storage_backup_staging() {
  local path=""
  for path in "${STORAGE_BACKUP_PUBLISHED_ARCHIVE:-}" \
    "${STORAGE_BACKUP_PUBLISHED_MANIFEST:-}"; do
    if [[ -n "${path}" ]]; then
      rm -f -- "${path}"
    fi
  done
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
  STORAGE_BACKUP_PUBLISHED_ARCHIVE=""
  STORAGE_BACKUP_PUBLISHED_MANIFEST=""
}

cleanup_storage_credential_retirement() {
  if [[ "${STORAGE_CREDENTIAL_ROTATION_DETECTED:-false}" == "true" ]]; then
    kubectl_cmd -n "${NAMESPACE}" delete pod "${STORAGE_CREDENTIAL_RETIREMENT_POD}" \
      --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
  fi
  STORAGE_CREDENTIAL_ROTATION_DETECTED="false"
  STORAGE_RETIRED_ROOT_USER=""
  STORAGE_RETIRED_ROOT_PASSWORD=""
  STORAGE_RETIRED_APP_ACCESS_KEY=""
  STORAGE_RETIRED_APP_SECRET_KEY=""
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
STORAGE_ROOT_USER=${STORAGE_ROOT_USER_ID}
STORAGE_ROOT_PASSWORD=$(generate_storage_secret)
STORAGE_APP_ACCESS_KEY=${STORAGE_APP_ACCESS_KEY_ID}
STORAGE_APP_SECRET_KEY=$(generate_storage_secret)
EOF
}

load_storage_credentials() {
  ensure_storage_credentials
  # shellcheck disable=SC1090
  source "${STORAGE_CREDENTIALS_ENV}"
  if [[ "${STORAGE_ROOT_USER:-}" != "${STORAGE_ROOT_USER_ID}" ]]; then
    echo "WGCF storage root identity is immutable; rotate only its secret" >&2
    return 1
  fi
  if [[ "${STORAGE_APP_ACCESS_KEY:-}" != "${STORAGE_APP_ACCESS_KEY_ID}" ]]; then
    echo "WGCF storage application identity is immutable; rotate only its secret" >&2
    return 1
  fi
}

read_live_storage_secret() {
  local secret_name="$1"
  shift
  local secret_json=""
  if ! secret_json="$(
    kubectl_cmd -n "${NAMESPACE}" get secret "${secret_name}" \
      -o json --ignore-not-found
  )"; then
    return 1
  fi
  if [[ -z "${secret_json}" ]]; then
    return 3
  fi
  python3 -c '
import base64
import json
import sys

payload = json.load(sys.stdin)
data = payload.get("data") or {}
for key in sys.argv[1:]:
    encoded = data.get(key)
    if not isinstance(encoded, str) or not encoded:
        raise SystemExit(f"live storage Secret is missing {key}")
    print(base64.b64decode(encoded).decode("utf-8"))
' "$@" <<<"${secret_json}"
}

load_pending_storage_credential_rotation() {
  local -a pending_values=()
  local pending_output=""
  local read_status=0
  local desired_digest=""

  if pending_output="$(
    read_live_storage_secret "${STORAGE_CREDENTIAL_RETIREMENT_SECRET}" \
      target-credentials-sha256 \
      retired-root-user \
      retired-root-password \
      retired-app-access-key \
      retired-app-secret-key
  )"; then
    read_status=0
  else
    read_status=$?
  fi

  if [[ "${read_status}" -eq 0 ]]; then
    if [[ -z "${pending_output}" ]]; then
      echo "pending storage credential rotation Secret is empty" >&2
      return 1
    fi
    mapfile -t pending_values <<<"${pending_output}"
    if [[ "${#pending_values[@]}" -ne 5 ]]; then
      echo "pending storage credential rotation has an invalid shape" >&2
      return 1
    fi
    desired_digest="$(storage_credentials_digest)"
    if [[ "${pending_values[0]}" != "${desired_digest}" ]]; then
      echo "pending storage credential rotation targets different replacement credentials" >&2
      return 1
    fi
    STORAGE_CREDENTIAL_ROTATION_DETECTED="true"
    STORAGE_RETIRED_ROOT_USER="${pending_values[1]}"
    STORAGE_RETIRED_ROOT_PASSWORD="${pending_values[2]}"
    STORAGE_RETIRED_APP_ACCESS_KEY="${pending_values[3]}"
    STORAGE_RETIRED_APP_SECRET_KEY="${pending_values[4]}"
    return 0
  fi

  if [[ "${read_status}" -eq 3 ]]; then
    return 3
  fi
  return "${read_status}"
}

persist_pending_storage_credential_rotation() {
  local desired_digest=""
  desired_digest="$(storage_credentials_digest)"
  rm -f -- "${STORAGE_CREDENTIAL_RETIREMENT_FILE}"
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: v1
kind: Secret
metadata:
  name: ${STORAGE_CREDENTIAL_RETIREMENT_SECRET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-credential-retirement
    devint.profile: ${PROFILE_ID}
type: Opaque
stringData:
  target-credentials-sha256: "${desired_digest}"
  retired-root-user: "${STORAGE_RETIRED_ROOT_USER}"
  retired-root-password: "${STORAGE_RETIRED_ROOT_PASSWORD}"
  retired-app-access-key: "${STORAGE_RETIRED_APP_ACCESS_KEY}"
  retired-app-secret-key: "${STORAGE_RETIRED_APP_SECRET_KEY}"
EOF
}

capture_storage_credentials_for_rotation() {
  local -a live_values=()
  local live_output=""
  local read_status=0
  load_storage_credentials

  if load_pending_storage_credential_rotation; then
    return
  else
    read_status=$?
    if [[ "${read_status}" -ne 3 ]]; then
      return "${read_status}"
    fi
  fi

  if live_output="$(
    read_live_storage_secret "${STORAGE_ROOT_SECRET}" root-user root-password
  )" && [[ -n "${live_output}" ]]; then
    mapfile -t live_values <<<"${live_output}"
    if [[ "${#live_values[@]}" -ne 2 ]]; then
      echo "live storage root Secret has an invalid shape" >&2
      return 1
    fi
    if [[ "${live_values[0]}" != "${STORAGE_ROOT_USER}" \
      || "${live_values[1]}" != "${STORAGE_ROOT_PASSWORD}" ]]; then
      STORAGE_CREDENTIAL_ROTATION_DETECTED="true"
      STORAGE_RETIRED_ROOT_USER="${live_values[0]}"
      STORAGE_RETIRED_ROOT_PASSWORD="${live_values[1]}"
    fi
  else
    read_status=$?
    if [[ "${read_status}" -ne 3 ]]; then
      return "${read_status}"
    fi
  fi

  live_values=()
  live_output=""
  if live_output="$(
    read_live_storage_secret "${STORAGE_APP_SECRET}" access-key secret-key
  )" && [[ -n "${live_output}" ]]; then
    mapfile -t live_values <<<"${live_output}"
    if [[ "${#live_values[@]}" -ne 2 ]]; then
      echo "live storage application Secret has an invalid shape" >&2
      return 1
    fi
    if [[ "${live_values[0]}" != "${STORAGE_APP_ACCESS_KEY}" \
      || "${live_values[1]}" != "${STORAGE_APP_SECRET_KEY}" ]]; then
      STORAGE_CREDENTIAL_ROTATION_DETECTED="true"
      STORAGE_RETIRED_APP_ACCESS_KEY="${live_values[0]}"
      STORAGE_RETIRED_APP_SECRET_KEY="${live_values[1]}"
    fi
  else
    read_status=$?
    if [[ "${read_status}" -ne 3 ]]; then
      return "${read_status}"
    fi
  fi

  if [[ "${STORAGE_CREDENTIAL_ROTATION_DETECTED}" == "true" ]]; then
    if [[ -z "${STORAGE_RETIRED_ROOT_USER}" \
      || -z "${STORAGE_RETIRED_ROOT_PASSWORD}" \
      || -z "${STORAGE_RETIRED_APP_ACCESS_KEY}" \
      || -z "${STORAGE_RETIRED_APP_SECRET_KEY}" ]]; then
      echo "WGCF storage root and application secrets must rotate together" >&2
      return 1
    fi
    persist_pending_storage_credential_rotation
  fi
}

create_storage_credential_retirement_pod() {
  kubectl_cmd -n "${NAMESPACE}" delete pod "${STORAGE_CREDENTIAL_RETIREMENT_POD}" \
    --ignore-not-found=true --wait=true >/dev/null
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata:
  name: ${STORAGE_CREDENTIAL_RETIREMENT_POD}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: object-storage-maintenance
    devint.profile: ${PROFILE_ID}
spec:
  serviceAccountName: ${STORAGE_MAINTENANCE_SERVICE_ACCOUNT}
  restartPolicy: Never
  containers:
    - name: verifier
      image: ${API_IMAGE}
      imagePullPolicy: IfNotPresent
      command:
        - /bin/sh
        - -ec
      args:
        - sleep 3600
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop:
            - ALL
EOF
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=Ready \
    "pod/${STORAGE_CREDENTIAL_RETIREMENT_POD}" --timeout=120s
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_CREDENTIAL_RETIREMENT_POD}" -- \
    python -c 'import pathlib, sys; pathlib.Path("/tmp/verify-storage-versioning.py").write_bytes(sys.stdin.buffer.read())' \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py"
}

prove_retired_storage_credential_denied() {
  local access_key="$1"
  local secret_key="$2"
  local output_path="$3"
  {
    printf '%s\n' "${access_key}"
    printf '%s\n' "${secret_key}"
  } | kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_CREDENTIAL_RETIREMENT_POD}" -- \
    env WGCF_EVIDENCE_STORAGE_ENDPOINT="${STORAGE_ENDPOINT}" \
      WGCF_EVIDENCE_STORAGE_BUCKET="${STORAGE_BUCKET}" \
      python /tmp/verify-storage-versioning.py expect-denied-stdin "${STORAGE_SEED_KEY}" \
      >"${output_path}"
}

verify_retired_storage_credentials() {
  local root_proof=""
  local app_proof=""
  if [[ "${STORAGE_CREDENTIAL_ROTATION_DETECTED}" != "true" ]]; then
    return
  fi

  root_proof="$(mktemp "${STATE_ROOT}/.retired-root.XXXXXX.json")"
  app_proof="$(mktemp "${STATE_ROOT}/.retired-app.XXXXXX.json")"
  create_storage_credential_retirement_pod
  prove_retired_storage_credential_denied \
    "${STORAGE_RETIRED_ROOT_USER}" "${STORAGE_RETIRED_ROOT_PASSWORD}" "${root_proof}"
  prove_retired_storage_credential_denied \
    "${STORAGE_RETIRED_APP_ACCESS_KEY}" "${STORAGE_RETIRED_APP_SECRET_KEY}" "${app_proof}"
  python3 - "${STORAGE_CREDENTIAL_RETIREMENT_FILE}" "${root_proof}" "${app_proof}" <<'PY'
from datetime import datetime, timezone
import json
import pathlib
import sys

def denied(path_value: str):
    path = pathlib.Path(path_value)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("authentication_denied") is True

root_denied = denied(sys.argv[2])
app_denied = denied(sys.argv[3])
if root_denied is not True or app_denied is not True:
    raise SystemExit("retired storage credential denial proof is incomplete")
payload = {
    "schema_version": 1,
    "rotation_detected": True,
    "retired_root_credential_authentication_denied": root_denied,
    "retired_application_credential_authentication_denied": app_denied,
    "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
pathlib.Path(sys.argv[1]).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  rm -f -- "${root_proof}" "${app_proof}"
  kubectl_cmd -n "${NAMESPACE}" delete secret \
    "${STORAGE_CREDENTIAL_RETIREMENT_SECRET}" --wait=true >/dev/null
  cleanup_storage_credential_retirement
}

storage_credentials_digest() {
  load_storage_credentials
  sha256sum "${STORAGE_CREDENTIALS_ENV}" | awk '{print $1}'
}

require_storage_security_review() {
  local repo_paths_json="${DEVINT_REPO_PATHS_JSON:-}"
  if [[ -z "${repo_paths_json}" ]]; then
    repo_paths_json='{}'
  fi
  python3 - "${PROFILE_JSON}" "${WORKSPACE_ROOT}" "${repo_paths_json}" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

profile = json.loads(sys.argv[1])
workspace_root = pathlib.Path(sys.argv[2]).resolve()
repo_paths = json.loads(sys.argv[3])
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
repo_root = pathlib.Path(repo_paths.get(expected_repo, workspace_root / expected_repo)).resolve()
review_path = repo_root / expected_path
if not (repo_root / ".git").exists():
    raise SystemExit(f"WGCF evidence storage Security repository is unavailable: {repo_root}")
landed = subprocess.run(
    [
        "git",
        "-C",
        str(repo_root),
        "merge-base",
        "--is-ancestor",
        source_commit,
        "refs/remotes/origin/main",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if landed.returncode != 0:
    raise SystemExit("WGCF evidence storage Security review revision is not landed on origin/main")
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
  local repo_paths_json="${DEVINT_REPO_PATHS_JSON:-}"
  if [[ -z "${repo_paths_json}" ]]; then
    repo_paths_json='{}'
  fi
  python3 - "${PROFILE_JSON}" "${WORKSPACE_ROOT}" "${repo_paths_json}" <<'PY'
import hashlib
import json
import pathlib
import subprocess
import sys

import yaml

profile = json.loads(sys.argv[1])
workspace_root = pathlib.Path(sys.argv[2]).resolve()
repo_paths = json.loads(sys.argv[3])
binding = (profile.get("authority") or {}).get("activation_contract") or {}
required_binding_fields = {
    "repo",
    "path",
    "profile_id",
    "authority_source_commit",
    "authority_content_sha256",
    "platform_acceptance_ref",
    "platform_acceptance_source_commit",
    "platform_acceptance_content_sha256",
    "required_actions",
    "required_stage_checks",
}
if set(binding) != required_binding_fields:
    raise SystemExit("WGCF evidence storage profile has an incomplete authority binding")

governance_repo = pathlib.Path(
    repo_paths.get(binding["repo"], workspace_root / binding["repo"])
).resolve()
if not (governance_repo / ".git").exists():
    raise SystemExit(f"WGCF evidence storage authority repository is unavailable: {governance_repo}")
authority_commit = binding["authority_source_commit"]
authority_digest = binding["authority_content_sha256"]
if not isinstance(authority_commit, str) or len(authority_commit) != 40:
    raise SystemExit("WGCF evidence storage authority has no immutable source commit")
if not isinstance(authority_digest, str) or len(authority_digest) != 64:
    raise SystemExit("WGCF evidence storage authority has no content digest")
landed = subprocess.run(
    [
        "git",
        "-C",
        str(governance_repo),
        "merge-base",
        "--is-ancestor",
        authority_commit,
        "refs/remotes/origin/main",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if landed.returncode != 0:
    raise SystemExit("WGCF evidence storage authority revision is not landed on origin/main")
result = subprocess.run(
    ["git", "-C", str(governance_repo), "show", f"{authority_commit}:{binding['path']}"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if result.returncode != 0:
    raise SystemExit("WGCF evidence storage authority registry is unavailable at its pinned commit")
if hashlib.sha256(result.stdout).hexdigest() != authority_digest:
    raise SystemExit("WGCF evidence storage authority commit does not match its pinned digest")
registry = yaml.safe_load(result.stdout) or {}
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
    pathlib.Path(
        repo_paths.get("platform-engineering", workspace_root / "platform-engineering")
    ).resolve()
    / binding["platform_acceptance_ref"][len(acceptance_prefix):]
).resolve()
platform_repo = pathlib.Path(
    repo_paths.get("platform-engineering", workspace_root / "platform-engineering")
).resolve()
source_commit = binding["platform_acceptance_source_commit"]
expected_digest = binding["platform_acceptance_content_sha256"]
if not isinstance(source_commit, str) or len(source_commit) != 40:
    raise SystemExit("WGCF evidence storage Platform acceptance has no immutable source commit")
if not isinstance(expected_digest, str) or len(expected_digest) != 64:
    raise SystemExit("WGCF evidence storage Platform acceptance has no content digest")
if not (platform_repo / ".git").exists():
    raise SystemExit(f"WGCF evidence storage Platform repository is unavailable: {platform_repo}")
try:
    acceptance_relpath = acceptance_path.relative_to(platform_repo).as_posix()
except ValueError as error:
    raise SystemExit("WGCF evidence storage Platform acceptance escapes its owner repo") from error
landed = subprocess.run(
    [
        "git",
        "-C",
        str(platform_repo),
        "merge-base",
        "--is-ancestor",
        source_commit,
        "refs/remotes/origin/main",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    check=False,
)
if landed.returncode != 0:
    raise SystemExit("WGCF evidence storage Platform acceptance revision is not landed on origin/main")
result = subprocess.run(
    ["git", "-C", str(platform_repo), "show", f"{source_commit}:{acceptance_relpath}"],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
if result.returncode != 0:
    raise SystemExit("WGCF evidence storage Platform acceptance is unavailable at its pinned commit")
actual_digest = hashlib.sha256(result.stdout).hexdigest()
if actual_digest != expected_digest:
    raise SystemExit("WGCF evidence storage Platform acceptance commit does not match its pinned digest")

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

require_no_pending_storage_credential_rotation() {
  local pending_secret=""
  if ! pending_secret="$(
    kubectl_cmd -n "${NAMESPACE}" get secret \
      "${STORAGE_CREDENTIAL_RETIREMENT_SECRET}" \
      -o name --ignore-not-found
  )"; then
    return 1
  fi
  if [[ -n "${pending_secret}" ]]; then
    echo "Storage credential retirement is pending; rerun the profile up action" >&2
    return 1
  fi
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
  cat <<EOF | kubectl_cmd apply -f - -o json >"${STORAGE_PROVISION_JOB_IDENTITY_FILE}"
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
  local staged_path
  staged_path="$(mktemp "${STORAGE_RECEIPT_FILE}.XXXXXX.tmp")"
  chmod 600 "${staged_path}"
  if ! python3 - "${staged_path}" "${STORAGE_RECEIPT_FILE}" \
    "${STORAGE_VERIFICATION_FILE}" \
    "${STORAGE_VERSION_PROOF_FILE}" "${STORAGE_NETWORK_ENFORCEMENT_FILE}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" "${STORAGE_SEED_KEY}" \
    "$(storage_seed_digest)" "${STORAGE_APP_SECRET}" "${COMPONENT_NAME}" \
    "${STORAGE_ISOLATION_FILE}" "${STORAGE_CREDENTIAL_RETIREMENT_FILE}" <<'PY'
from datetime import datetime, timezone
import json
import pathlib
import sys
from urllib.parse import quote

receipt_path = pathlib.Path(sys.argv[1])
prior_receipt_path = pathlib.Path(sys.argv[2])
verification = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
proof = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
prior_receipt = None
if prior_receipt_path.is_file():
    prior_receipt = json.loads(prior_receipt_path.read_text(encoding="utf-8"))
network_proof = pathlib.Path(sys.argv[5]).read_text(encoding="utf-8").splitlines()
isolation = json.loads(pathlib.Path(sys.argv[13]).read_text(encoding="utf-8"))
rotation_proof_path = pathlib.Path(sys.argv[14])
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
expected_digest = sys.argv[10]
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
    "profile_id": sys.argv[6],
    "kubernetes_namespace": sys.argv[7],
    "bucket": sys.argv[8],
    "object_key": sys.argv[9],
    "object_version_id": accepted_version_id,
    "content_sha256": expected_digest,
    "storage_ref": (
        f"wgcf-storage://{sys.argv[6]}/{sys.argv[8]}/{sys.argv[9]}"
        f"?versionId={version_query}"
    ),
    "service_identity_ref": f"kubernetes://{sys.argv[7]}/serviceaccount/{sys.argv[12]}",
    "application_secret_ref": f"kubernetes://{sys.argv[7]}/secret/{sys.argv[11]}",
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
if rotation_proof_path.is_file():
    rotation_proof = json.loads(rotation_proof_path.read_text(encoding="utf-8"))
    if rotation_proof.get("rotation_detected") is not True:
        raise SystemExit("storage credential rotation proof is invalid")
    for field in (
        "retired_root_credential_authentication_denied",
        "retired_application_credential_authentication_denied",
    ):
        if rotation_proof.get(field) not in {None, True}:
            raise SystemExit("storage credential rotation did not revoke every prior credential")
    if not any(
        rotation_proof.get(field) is True
        for field in (
            "retired_root_credential_authentication_denied",
            "retired_application_credential_authentication_denied",
        )
    ):
        raise SystemExit("storage credential rotation proof contains no retired credential")
    payload["credential_rotation"] = rotation_proof
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
  then
    rm -f "${staged_path}"
    return 1
  fi
  if ! read_storage_receipt_binding "${staged_path}" >/dev/null; then
    rm -f "${staged_path}"
    return 1
  fi
  mv -f -- "${staged_path}" "${STORAGE_RECEIPT_FILE}"
}

refresh_storage_receipt_isolation() {
  local staged_path
  staged_path="$(mktemp "${STORAGE_RECEIPT_FILE}.XXXXXX.tmp")"
  chmod 600 "${staged_path}"
  if ! python3 - "${STORAGE_RECEIPT_FILE}" "${STORAGE_ISOLATION_FILE}" \
    "${STORAGE_NETWORK_ENFORCEMENT_FILE}" "${staged_path}" <<'PY'
import json
import pathlib
import sys

receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
isolation = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
network_proof = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8").splitlines()
if set(network_proof) != {
    "maintenance_storage_connectivity=allowed",
    "unauthorized_storage_connectivity=denied",
    "api_storage_connectivity=allowed-by-version-read",
}:
    raise SystemExit("storage network-enforcement proof is incomplete")
if isolation.get("oos_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OOS credential")
if isolation.get("openproject_credential_issued") is not False:
    raise SystemExit("storage isolation proof issued an OpenProject credential")
receipt["root_credential_exposed_to_api"] = False
receipt["oos_credential_issued"] = False
receipt["openproject_credential_issued"] = False
receipt["credential_isolation_verified_at"] = isolation.get("verified_at")
receipt["network_enforcement"] = {
    "api_allowed": True,
    "maintenance_allowed": True,
    "unauthorized_pod_denied": True,
}
pathlib.Path(sys.argv[4]).write_text(
    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  then
    rm -f "${staged_path}"
    return 1
  fi
  if ! read_storage_receipt_binding "${staged_path}" >/dev/null; then
    rm -f "${staged_path}"
    return 1
  fi
  mv -f -- "${staged_path}" "${STORAGE_RECEIPT_FILE}"
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
    "${stateful_set_file}" "${jobs_file}" "${STORAGE_PROVISION_JOB_IDENTITY_FILE}" \
    "${API_DEPLOYMENT}" "${STORAGE_STATEFULSET}" \
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
        - name: WGCF_EVIDENCE_PROFILE_ID
          value: ${PROFILE_ID}
        - name: WGCF_EVIDENCE_KUBERNETES_NAMESPACE
          value: ${NAMESPACE}
        - name: WGCF_EVIDENCE_SERVICE_IDENTITY_REF
          value: kubernetes://${NAMESPACE}/serviceaccount/${COMPONENT_NAME}
        - name: WGCF_EVIDENCE_APPLICATION_SECRET_REF
          value: kubernetes://${NAMESPACE}/secret/${STORAGE_APP_SECRET}
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

read_storage_receipt_binding() {
  local receipt_file="${1:-${STORAGE_RECEIPT_FILE}}"
  python3 - "${receipt_file}" "${PROFILE_ID}" "${NAMESPACE}" \
    "${STORAGE_BUCKET}" "${STORAGE_SEED_KEY}" "${COMPONENT_NAME}" \
    "${STORAGE_APP_SECRET}" <<'PY'
import json
import pathlib
import sys
from urllib.parse import quote

receipt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
required = ["object_key", "object_version_id", "content_sha256"]
for field in required:
    value = receipt.get(field)
    if not isinstance(value, str) or not value or "\n" in value:
        raise SystemExit(f"storage receipt has invalid {field}")
if receipt.get("schema_version") != 2 or receipt.get("receipt_type") != "dev-integration-storage":
    raise SystemExit("storage receipt is not a version-bound WGCF receipt")
if receipt.get("profile_id") != sys.argv[2]:
    raise SystemExit("storage receipt belongs to a different profile")
if receipt.get("kubernetes_namespace") != sys.argv[3]:
    raise SystemExit("storage receipt belongs to a different Kubernetes namespace")
if receipt.get("bucket") != sys.argv[4]:
    raise SystemExit("storage receipt belongs to a different bucket")
if receipt.get("object_key") != sys.argv[5]:
    raise SystemExit("storage receipt belongs to a different evidence object")
expected_ref = (
    f"wgcf-storage://{sys.argv[2]}/{sys.argv[4]}/{sys.argv[5]}"
    f"?versionId={quote(receipt['object_version_id'], safe='-_.~')}"
)
if receipt.get("storage_ref") != expected_ref:
    raise SystemExit("storage receipt has an invalid version-qualified storage reference")
expected_service_identity = f"kubernetes://{sys.argv[3]}/serviceaccount/{sys.argv[6]}"
if receipt.get("service_identity_ref") != expected_service_identity:
    raise SystemExit("storage receipt has an invalid service identity reference")
expected_secret_ref = f"kubernetes://{sys.argv[3]}/secret/{sys.argv[7]}"
if receipt.get("application_secret_ref") != expected_secret_ref:
    raise SystemExit("storage receipt has an invalid application Secret reference")
for field in required:
    print(receipt[field])
PY
}

capture_rebound_json_atomically() {
  local pod_path="$1"
  local target_path="$2"
  local validation_mode="$3"
  local staged_path
  staged_path="$(mktemp "${target_path}.XXXXXX.tmp")"
  chmod 600 "${staged_path}"
  if ! kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c rebind -- \
    cat "${pod_path}" >"${staged_path}"; then
    rm -f "${staged_path}"
    return 1
  fi
  case "${validation_mode}" in
    receipt-rebindings)
      if ! python3 - "${staged_path}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit("restore receipt-rebinding output must be an object")
if payload.get("receipt_identity_rebound") is not True:
    raise SystemExit("restore receipt-rebinding output is not complete")
if not isinstance(payload.get("receipt_rebindings"), list) or not payload["receipt_rebindings"]:
    raise SystemExit("restore receipt-rebinding output contains no bindings")
PY
      then
        rm -f "${staged_path}"
        return 1
      fi
      ;;
    storage-receipt)
      if ! read_storage_receipt_binding "${staged_path}" >/dev/null; then
        rm -f "${staged_path}"
        return 1
      fi
      ;;
    *)
      rm -f "${staged_path}"
      echo "unsupported rebound JSON validation mode: ${validation_mode}" >&2
      return 1
      ;;
  esac
  mv -f -- "${staged_path}" "${target_path}"
}

stage_receipt_bound_evidence() {
  local -a receipt_fields=()
  if [[ ! -f "${STORAGE_RECEIPT_FILE}" ]]; then
    echo "Storage receipt is missing; run the profile up action before backup" >&2
    return 1
  fi
  mapfile -t receipt_fields < <(read_storage_receipt_binding)
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
  STORAGE_BACKUP_PUBLISHED_ARCHIVE="${backup_path}"
  STORAGE_BACKUP_PUBLISHED_MANIFEST="${backup_path}.manifest.json"
  ln -- "${STORAGE_BACKUP_STAGING_ARCHIVE}" "${STORAGE_BACKUP_PUBLISHED_ARCHIVE}"
  ln -- "${STORAGE_BACKUP_STAGING_MANIFEST}" "${STORAGE_BACKUP_PUBLISHED_MANIFEST}"
  STORAGE_BACKUP_PUBLISHED_ARCHIVE=""
  STORAGE_BACKUP_PUBLISHED_MANIFEST=""
  mv -- "${STORAGE_BACKUP_STAGING_RECEIPT}" "${receipt_path}"
  cleanup_storage_backup_staging
}

probe_live_receipt_version() {
  local -a receipt_fields=()
  mapfile -t receipt_fields < <(read_storage_receipt_binding)
  if [[ "${#receipt_fields[@]}" -ne 3 ]]; then
    echo "Storage receipt fields could not be probed" >&2
    return 1
  fi
  kubectl_cmd -n "${NAMESPACE}" exec -i "deployment/${API_DEPLOYMENT}" -- \
    python - probe-version "${receipt_fields[0]}" "${receipt_fields[1]}" \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py"
}

require_empty_storage_for_receipt_loss() {
  local stored_versions=""
  create_storage_transfer_pod root
  if ! stored_versions="$(
    kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- \
      /bin/sh -ec \
      'mc alias set storage "$1" "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc ls --versions --recursive "storage/$2"' \
      sh "${STORAGE_ENDPOINT}" "${STORAGE_BUCKET}"
  )"; then
    delete_storage_transfer_pod
    return 1
  fi
  delete_storage_transfer_pod
  if [[ -n "${stored_versions}" ]]; then
    echo "Receipt-bound version is missing but live storage is not empty; restore refused" >&2
    return 1
  fi
}

archive_storage_backups() {
  local archive_path=""
  local backup_file=""
  local manifest_file=""
  local backup_count=0
  if [[ ! -d "${BACKUPS_DIR}" ]]; then
    return
  fi
  while IFS= read -r -d '' backup_file; do
    manifest_file="${backup_file}.manifest.json"
    if [[ ! -f "${manifest_file}" ]]; then
      echo "WGCF evidence backup manifest is missing: ${manifest_file}" >&2
      return 1
    fi
    validate_backup_for_restore "${backup_file}"
    backup_count=$((backup_count + 1))
  done < <(find "${BACKUPS_DIR}" -maxdepth 1 -type f -name '*.tar.gz' -print0)
  while IFS= read -r -d '' manifest_file; do
    backup_file="${manifest_file%.manifest.json}"
    if [[ ! -f "${backup_file}" ]]; then
      echo "WGCF evidence backup archive is missing: ${backup_file}" >&2
      return 1
    fi
  done < <(
    find "${BACKUPS_DIR}" -maxdepth 1 -type f -name '*.tar.gz.manifest.json' -print0
  )
  if [[ "${backup_count}" -eq 0 ]]; then
    return
  fi

  archive_path="${ARCHIVE_ROOT}/reset-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mkdir -p "${ARCHIVE_ROOT}"
  if [[ -e "${archive_path}" ]]; then
    echo "WGCF evidence reset archive already exists: ${archive_path}" >&2
    return 1
  fi
  mv -- "${BACKUPS_DIR}" "${archive_path}"
  if [[ -f "${STORAGE_BACKUP_RECEIPT_FILE}" ]]; then
    cp "${STORAGE_BACKUP_RECEIPT_FILE}" "${archive_path}/latest-backup-receipt.json"
  fi
  printf '%s\n' "${archive_path}"
}

validate_backup_for_restore() {
  local backup_path="$1"
  local manifest_path="${2:-${backup_path}.manifest.json}"
  local input_mode="${3:-path}"
  python3 - "${backup_path}" "${manifest_path}" "${STATE_ROOT}" "${ARCHIVE_ROOT}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" \
    "${COMPONENT_NAME}" "${STORAGE_APP_SECRET}" \
    "${STORAGE_SEED_KEY}" "$(storage_seed_digest)" \
    "${PROFILE_ROOT}/scripts/lib" "${input_mode}" <<'PY'
import fcntl
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tarfile
from urllib.parse import quote

backup = pathlib.Path(sys.argv[1])
manifest_path = pathlib.Path(sys.argv[2])
allowed_roots = [pathlib.Path(value).resolve() for value in sys.argv[3:5]]
sys.path.insert(0, sys.argv[12])
from verify_storage_versioning import validate_storage_receipt

def require_sealed_memfd(path: pathlib.Path, label: str) -> None:
    match = re.fullmatch(r"/proc/self/fd/([0-9]+)", str(path))
    if match is None:
        raise SystemExit(f"sealed restore {label} is not descriptor-bound")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SystemExit(f"sealed restore {label} is not a regular file")
        required = (
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SEAL
        )
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != required:
            raise SystemExit(f"sealed restore {label} is not immutable")
    finally:
        os.close(descriptor)

input_mode = sys.argv[13]
if input_mode == "sealed":
    require_sealed_memfd(backup, "archive")
    require_sealed_memfd(manifest_path, "manifest")
elif input_mode == "path":
    backup = backup.resolve()
    manifest_path = manifest_path.resolve()
    if not backup.is_file():
        raise SystemExit(f"restore backup does not exist: {backup}")
    if not any(root == backup or root in backup.parents for root in allowed_roots):
        raise SystemExit("restore backup must stay under the operator profile state or reset archive")
    if not manifest_path.is_file():
        raise SystemExit(f"restore backup manifest is missing: {manifest_path}")
    if not any(root == manifest_path or root in manifest_path.parents for root in allowed_roots):
        raise SystemExit("restore manifest must stay under the operator profile state or reset archive")
else:
    raise SystemExit("restore input mode is invalid")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema_version") != 2:
    raise SystemExit("restore backup must use the version-bound schema")
if manifest.get("archive_sha256") != hashlib.sha256(backup.read_bytes()).hexdigest():
    raise SystemExit("restore backup digest does not match its manifest")
recorded_backup_path = manifest.get("backup_path")
if not isinstance(recorded_backup_path, str) or not pathlib.Path(recorded_backup_path).is_absolute():
    raise SystemExit("restore backup manifest has invalid original-path provenance")
if manifest.get("profile_id") != sys.argv[5]:
    raise SystemExit("restore backup belongs to a different profile")
if manifest.get("kubernetes_namespace") != sys.argv[6]:
    raise SystemExit("restore backup belongs to a different Kubernetes namespace")
if manifest.get("bucket") != sys.argv[7]:
    raise SystemExit("restore backup belongs to a different bucket")
if manifest.get("credentials_included") is not False:
    raise SystemExit("restore backup may not contain credentials")
if manifest.get("version_ids_preserved") is not False:
    raise SystemExit("restore manifest has an invalid version-preservation claim")
if manifest.get("restore_requires_receipt_rebinding") is not True:
    raise SystemExit("restore manifest does not require receipt rebinding")
expected = {}
object_keys = set()
seed_key = sys.argv[10]
seed_digest = sys.argv[11]
seed_object_seen = False
for item in manifest.get("objects") or []:
    object_key = item.get("object_key")
    archive_path = item.get("archive_path")
    digest = item.get("sha256")
    size = item.get("size")
    object_path = pathlib.PurePosixPath(object_key) if isinstance(object_key, str) else None
    canonical_object_key = object_path.as_posix() if object_path is not None else None
    if (
        not isinstance(object_key, str)
        or not object_key
        or canonical_object_key != object_key
        or canonical_object_key in object_keys
        or object_path is None
        or object_path.is_absolute()
        or ".." in object_path.parts
    ):
        raise SystemExit("restore manifest contains an invalid or duplicate object key")
    object_keys.add(canonical_object_key)
    if archive_path != f"current/{object_key}":
        raise SystemExit(f"restore manifest contains an invalid current object path: {object_key}")
    if not isinstance(digest, str) or len(digest) != 64 or not isinstance(size, int) or size < 0:
        raise SystemExit(f"restore manifest contains invalid object evidence: {object_key}")
    if object_key == seed_key:
        seed_object_seen = True
    expected[archive_path] = (digest, size)
if not expected:
    raise SystemExit("restore manifest contains no objects")
if not seed_object_seen:
    raise SystemExit("restore manifest does not contain the configured seed object")

receipt_names = set()
receipt_bindings = {}
primary_seed_receipt_seen = False
for binding in manifest.get("receipt_bindings") or []:
    receipt_name = binding.get("receipt_name")
    object_key = binding.get("object_key")
    if (
        not isinstance(receipt_name, str)
        or not receipt_name
        or receipt_name in receipt_names
        or "/" in receipt_name
        or receipt_name in {".", ".."}
    ):
        raise SystemExit("restore manifest contains an invalid or duplicate receipt binding")
    receipt_names.add(receipt_name)
    object_path = pathlib.PurePosixPath(object_key) if isinstance(object_key, str) else None
    canonical_object_key = object_path.as_posix() if object_path is not None else None
    if (
        not isinstance(object_key, str)
        or not object_key
        or object_path is None
        or canonical_object_key != object_key
        or object_path.is_absolute()
        or ".." in object_path.parts
    ):
        raise SystemExit(f"restore manifest contains an invalid bound object: {receipt_name}")
    if binding.get("current_archive_path") != f"current/{object_key}":
        raise SystemExit(f"restore manifest receipt binding has no current object: {receipt_name}")
    if binding.get("current_archive_path") not in expected:
        raise SystemExit(f"restore manifest receipt binding targets an unknown object: {receipt_name}")
    body_path = binding.get("body_archive_path")
    receipt_path = binding.get("receipt_archive_path")
    if body_path != f"receipt-bound/{receipt_name}.bin":
        raise SystemExit(f"restore manifest contains an invalid bound body path: {receipt_name}")
    if receipt_path != f"receipt-records/{receipt_name}.json":
        raise SystemExit(f"restore manifest contains an invalid receipt path: {receipt_name}")
    content_digest = binding.get("content_sha256")
    body_size = binding.get("body_size")
    receipt_digest = binding.get("receipt_record_sha256")
    receipt_size = binding.get("receipt_record_size")
    prior_version_id = binding.get("prior_object_version_id")
    prior_storage_ref = binding.get("prior_storage_ref")
    if (
        not isinstance(content_digest, str)
        or len(content_digest) != 64
        or not isinstance(body_size, int)
        or body_size < 0
        or not isinstance(receipt_digest, str)
        or len(receipt_digest) != 64
        or not isinstance(receipt_size, int)
        or receipt_size < 0
        or not isinstance(prior_version_id, str)
        or not prior_version_id
        or not isinstance(prior_storage_ref, str)
        or not prior_storage_ref
    ):
        raise SystemExit(f"restore manifest contains invalid receipt evidence: {receipt_name}")
    if body_path in expected or receipt_path in expected:
        raise SystemExit(f"restore manifest contains duplicate receipt paths: {receipt_name}")
    expected[body_path] = (content_digest, body_size)
    expected[receipt_path] = (receipt_digest, receipt_size)
    receipt_bindings[receipt_name] = binding
    if receipt_name == "storage-receipt":
        if object_key != seed_key or content_digest != seed_digest:
            raise SystemExit("restore primary storage receipt does not bind the configured seed")
        primary_seed_receipt_seen = True
if not receipt_names:
    raise SystemExit("restore manifest contains no receipt-bound evidence")
if not primary_seed_receipt_seen:
    raise SystemExit("restore manifest does not contain the primary storage receipt")
actual = {}
archive_bodies = {}
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
        archive_bodies[archive_path] = body
        actual[archive_path] = (hashlib.sha256(body).hexdigest(), len(body))
if actual != expected:
    raise SystemExit("restore archive objects do not match the signed manifest evidence")

for receipt_name, binding in receipt_bindings.items():
    receipt_path = binding["receipt_archive_path"]
    try:
        receipt = json.loads(archive_bodies[receipt_path].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"restore receipt record is invalid: {receipt_name}") from error
    object_key = binding["object_key"]
    prior_version_id = binding["prior_object_version_id"]
    expected_ref = (
        f"wgcf-storage://{sys.argv[5]}/{sys.argv[7]}/{object_key}"
        f"?versionId={quote(prior_version_id, safe='-_.~')}"
    )
    if binding["prior_storage_ref"] != expected_ref:
        raise SystemExit(f"restore manifest has an invalid prior storage reference: {receipt_name}")
    validate_storage_receipt(
        receipt,
        active_scope={
            "profile_id": sys.argv[5],
            "kubernetes_namespace": sys.argv[6],
            "bucket": sys.argv[7],
            "service_identity_ref": (
                f"kubernetes://{sys.argv[6]}/serviceaccount/{sys.argv[8]}"
            ),
            "application_secret_ref": (
                f"kubernetes://{sys.argv[6]}/secret/{sys.argv[9]}"
            ),
        },
        object_key=object_key,
        version_id=prior_version_id,
        content_digest=binding["content_sha256"],
        receipt_name=receipt_name,
    )
PY
}

restore_evidence_storage() {
  local backup_path="$1"
  local manifest_path="$2"
  local verification_archive="${STATE_ROOT}/restore-verification.tar.gz"
  wait_for_storage_ready
  create_storage_transfer_pod root
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c archive -- /bin/sh -ec \
    'cat >/transfer/restore.tar.gz; rm -rf /transfer/restore /transfer/verify; mkdir -p /transfer/restore /transfer/verify; tar -C /transfer/restore -xzf /transfer/restore.tar.gz; mkdir -p /transfer/restore/rebound-receipts; chgrp -R 10001 /transfer/restore /transfer/verify; chmod -R g+rwX /transfer/restore /transfer/verify' \
    <"${backup_path}"
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c archive -- /bin/sh -ec \
    'cat >/transfer/restore-manifest.json' <"${manifest_path}"
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    /bin/sh -ec 'touch /transfer/receipt-rebindings.json; chgrp 10001 /transfer/receipt-rebindings.json; chmod g+rw /transfer/receipt-rebindings.json'
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc mirror --overwrite --remove /transfer/restore/current storage/'"'${STORAGE_BUCKET}'"' >/dev/null'
  kubectl_cmd -n "${NAMESPACE}" exec -i "pod/${STORAGE_TRANSFER_POD}" -c rebind -- \
    python - rebind /transfer/restore /transfer/restore-manifest.json \
    /transfer/receipt-rebindings.json \
    <"${PROFILE_ROOT}/scripts/lib/verify_storage_versioning.py"
  capture_rebound_json_atomically \
    /transfer/receipt-rebindings.json \
    "${STORAGE_RECEIPT_REBINDING_FILE}" \
    receipt-rebindings
  capture_rebound_json_atomically \
    /transfer/restore/rebound-receipts/storage-receipt.json \
    "${STORAGE_RECEIPT_FILE}" \
    storage-receipt
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c transfer -- /bin/sh -ec \
    'mc alias set storage '"'${STORAGE_ENDPOINT}'"' "$STORAGE_ACCESS_KEY" "$STORAGE_SECRET_KEY" >/dev/null; mc mirror --overwrite storage/'"'${STORAGE_BUCKET}'"' /transfer/verify >/dev/null'
  kubectl_cmd -n "${NAMESPACE}" exec "pod/${STORAGE_TRANSFER_POD}" -c archive -- \
    tar -C /transfer/verify -czf - . >"${verification_archive}"
  delete_storage_transfer_pod
  python3 - "${manifest_path}" "${verification_archive}" <<'PY'
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
  local selected_backup_path="$3"
  python3 - "${STORAGE_RESTORE_RECEIPT_FILE}" "${backup_path}" \
    "${pre_restore_path}" "${STORAGE_RECEIPT_REBINDING_FILE}" \
    "${PROFILE_ID}" "${NAMESPACE}" "${STORAGE_BUCKET}" \
    "${selected_backup_path}" "${4}" <<'PY'
from datetime import datetime, timezone
import hashlib
import json
import os
import pathlib
import sys
import tempfile

backup = pathlib.Path(sys.argv[2]).resolve()
pre_restore_value = sys.argv[3]
pre_restore = pathlib.Path(pre_restore_value).resolve() if pre_restore_value else None
selected_backup = pathlib.Path(sys.argv[8]).resolve()
pre_restore_state = sys.argv[9]
if pre_restore_state not in {"backup-created", "empty-live-store"}:
    raise SystemExit("restore receipt has an invalid pre-restore state")
if (pre_restore is None) != (pre_restore_state == "empty-live-store"):
    raise SystemExit("restore receipt pre-restore evidence does not match its state")
rebindings = json.loads(pathlib.Path(sys.argv[4]).read_text(encoding="utf-8"))
if rebindings.get("receipt_identity_rebound") is not True:
    raise SystemExit("restore receipt rebinding proof is incomplete")
payload = {
    "schema_version": 2,
    "receipt_type": "dev-integration-storage-restore",
    "profile_id": sys.argv[5],
    "kubernetes_namespace": sys.argv[6],
    "bucket": sys.argv[7],
    "restored_from": str(selected_backup),
    "restored_archive_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "pre_restore_state": pre_restore_state,
    "pre_restore_backup": str(pre_restore) if pre_restore is not None else None,
    "pre_restore_archive_sha256": (
        hashlib.sha256(pre_restore.read_bytes()).hexdigest()
        if pre_restore is not None
        else None
    ),
    "content_addresses_preserved": True,
    "version_ids_preserved": False,
    "receipt_identity_rebound": True,
    "receipt_rebindings": rebindings["receipt_rebindings"],
    "completed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
}
target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
fd, staged_name = tempfile.mkstemp(
    prefix=f"{target.name}.",
    suffix=".tmp",
    dir=target.parent,
)
staged = pathlib.Path(staged_name)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    validated = json.loads(staged.read_text(encoding="utf-8"))
    if validated != payload:
        raise SystemExit("staged restore receipt does not match its validated payload")
    os.replace(staged, target)
    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
except BaseException:
    staged.unlink(missing_ok=True)
    raise
PY
}
