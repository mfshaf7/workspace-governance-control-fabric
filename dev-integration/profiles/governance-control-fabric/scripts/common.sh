#!/usr/bin/env bash
set -euo pipefail

readonly PROFILE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly PROFILE_ID="${DEVINT_PROFILE_ID:?DEVINT_PROFILE_ID is required}"
readonly NAMESPACE="${DEVINT_NAMESPACE:?DEVINT_NAMESPACE is required}"
readonly OPERATOR="${DEVINT_OPERATOR:?DEVINT_OPERATOR is required}"
readonly STATE_ROOT="${DEVINT_STATE_ROOT:?DEVINT_STATE_ROOT is required}"
readonly SESSION_FILE="${DEVINT_SESSION_FILE:?DEVINT_SESSION_FILE is required}"
readonly WORKSPACE_ROOT="${DEVINT_WORKSPACE_ROOT:?DEVINT_WORKSPACE_ROOT is required}"
readonly OWNER_REPO_ROOT="${DEVINT_OWNER_REPO_ROOT:?DEVINT_OWNER_REPO_ROOT is required}"
readonly PROFILE_JSON="${DEVINT_PROFILE_JSON:?DEVINT_PROFILE_JSON is required}"
readonly PROMOTION_REPORT="${DEVINT_PROMOTION_REPORT:?DEVINT_PROMOTION_REPORT is required}"
readonly PROFILE_FILE="${DEVINT_PROFILE_FILE:?DEVINT_PROFILE_FILE is required}"
readonly DEVINT_KUBECONFIG_PATH="${DEVINT_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

export KUBECONFIG="${DEVINT_KUBECONFIG_PATH}"

read -r -a KUBECTL_CMD <<<"${DEVINT_KUBECTL:-k3s kubectl}"

readonly COMPONENT_NAME="workspace-governance-control-fabric-api"
readonly APP_LABEL="workspace-governance-control-fabric"
readonly API_DEPLOYMENT="${DEVINT_WGCF_DEPLOYMENT:-${COMPONENT_NAME}}"
readonly API_SERVICE="${DEVINT_WGCF_SERVICE:-${COMPONENT_NAME}}"
readonly API_CONTAINER_PORT="${DEVINT_WGCF_CONTAINER_PORT:-8080}"
readonly API_LOCAL_PORT="${DEVINT_WGCF_LOCAL_PORT:-18090}"
readonly POSTGRES_STATEFULSET="${DEVINT_WGCF_POSTGRES_STATEFULSET:-workspace-governance-control-fabric-postgresql}"
readonly POSTGRES_SERVICE="${DEVINT_WGCF_POSTGRES_SERVICE:-workspace-governance-control-fabric-postgresql}"
readonly POSTGRES_IMAGE="${DEVINT_WGCF_POSTGRES_IMAGE:-postgres:16-alpine}"
readonly POSTGRES_DATABASE="${DEVINT_WGCF_POSTGRES_DATABASE:-wgcf}"
readonly POSTGRES_USER="${DEVINT_WGCF_POSTGRES_USER:-wgcf}"
readonly POSTGRES_PASSWORD="${DEVINT_WGCF_POSTGRES_PASSWORD:-wgcf-devint-local}"
readonly POSTGRES_VOLUME_SIZE="${DEVINT_WGCF_POSTGRES_VOLUME_SIZE:-2Gi}"
readonly DATABASE_URL="postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_SERVICE}:5432/${POSTGRES_DATABASE}"
readonly DEFAULT_IMAGE_REPO="ghcr.io/mfshaf7/workspace-governance-control-fabric"
readonly DEFAULT_IMAGE_TAG="sha-$(git -C "${OWNER_REPO_ROOT}" rev-parse --short=7 HEAD)"
readonly API_IMAGE="${DEVINT_WGCF_IMAGE:-${DEFAULT_IMAGE_REPO}:${DEFAULT_IMAGE_TAG}}"
readonly LOGS_DIR="${STATE_ROOT}/logs"
readonly RENDERED_DIR="${STATE_ROOT}/rendered"
readonly SESSION_ARTIFACT="${STATE_ROOT}/control-fabric-session.yaml"
readonly API_HEALTH_FILE="${STATE_ROOT}/api-health.json"
readonly READINESS_FILE="${STATE_ROOT}/readiness.json"
readonly STATUS_FILE="${STATE_ROOT}/status.json"
readonly GRAPH_QUERY_FILE="${STATE_ROOT}/graph-query.json"
readonly VALIDATION_PLAN_FILE="${STATE_ROOT}/validation-plan.json"
readonly RECEIPTS_FILE="${STATE_ROOT}/receipts.json"
readonly DATABASE_MIGRATION_FILE="${STATE_ROOT}/database-migration.txt"
readonly SMOKE_SUMMARY="${STATE_ROOT}/smoke-summary.txt"
readonly ACCESS_FILE="${STATE_ROOT}/access.txt"
readonly PROFILE_PROMOTION_NOTES="${STATE_ROOT}/profile-promotion-notes.md"
readonly RUNTIME_MANIFEST="${RENDERED_DIR}/wgcf-api-runtime.yaml"

kubectl_cmd() {
  "${KUBECTL_CMD[@]}" "$@"
}

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

ensure_state_dirs() {
  mkdir -p "${STATE_ROOT}" "${LOGS_DIR}" "${RENDERED_DIR}"
}

write_session_artifact() {
  if [[ -f "${SESSION_FILE}" ]]; then
    cp "${SESSION_FILE}" "${SESSION_ARTIFACT}"
  else
    cat >"${SESSION_ARTIFACT}" <<EOF
schema_version: 1
lane: dev-integration
profile_id: ${PROFILE_ID}
operator: ${OPERATOR}
namespace: ${NAMESPACE}
owner_repo: workspace-governance-control-fabric
runtime_owner: platform-engineering
EOF
  fi
}

profile_summary() {
  python3 - "$PROFILE_JSON" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["summary"])
PY
}

stage_handoff_required_checks_markdown() {
  python3 - "$PROFILE_JSON" <<'PY'
import json
import sys
for check_name in json.loads(sys.argv[1])["stage_handoff"]["required_checks"]:
    print(f"   - `{check_name}`")
PY
}

render_runtime_manifest() {
  ensure_state_dirs
  write_session_artifact
  cat >"${RUNTIME_MANIFEST}" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    app.kubernetes.io/part-of: dev-integration
    devint.profile: ${PROFILE_ID}
---
apiVersion: v1
kind: Secret
metadata:
  name: ${POSTGRES_STATEFULSET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: postgresql
    devint.profile: ${PROFILE_ID}
type: Opaque
stringData:
  POSTGRES_DB: ${POSTGRES_DATABASE}
  POSTGRES_USER: ${POSTGRES_USER}
  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
---
apiVersion: v1
kind: Service
metadata:
  name: ${POSTGRES_SERVICE}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: postgresql
    devint.profile: ${PROFILE_ID}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: postgresql
    devint.profile: ${PROFILE_ID}
  ports:
    - name: postgresql
      port: 5432
      targetPort: postgresql
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: ${POSTGRES_STATEFULSET}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: postgresql
    devint.profile: ${PROFILE_ID}
spec:
  serviceName: ${POSTGRES_SERVICE}
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${APP_LABEL}
      app.kubernetes.io/component: postgresql
      devint.profile: ${PROFILE_ID}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: postgresql
        devint.profile: ${PROFILE_ID}
    spec:
      containers:
        - name: postgresql
          image: ${POSTGRES_IMAGE}
          imagePullPolicy: IfNotPresent
          ports:
            - name: postgresql
              containerPort: 5432
          envFrom:
            - secretRef:
                name: ${POSTGRES_STATEFULSET}
          env:
            - name: PGDATA
              value: /var/lib/postgresql/data/pgdata
          readinessProbe:
            exec:
              command:
                - pg_isready
                - -U
                - ${POSTGRES_USER}
                - -d
                - ${POSTGRES_DATABASE}
            initialDelaySeconds: 5
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 12
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
          resources:
            requests:
              cpu: 50m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes:
          - ReadWriteOnce
        resources:
          requests:
            storage: ${POSTGRES_VOLUME_SIZE}
---
apiVersion: v1
kind: Secret
metadata:
  name: ${COMPONENT_NAME}-database
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: api
    devint.profile: ${PROFILE_ID}
type: Opaque
stringData:
  WGCF_DATABASE_URL: ${DATABASE_URL}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${COMPONENT_NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: api
    devint.profile: ${PROFILE_ID}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${API_DEPLOYMENT}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: api
    devint.profile: ${PROFILE_ID}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: ${APP_LABEL}
      app.kubernetes.io/component: api
      devint.profile: ${PROFILE_ID}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: api
        devint.profile: ${PROFILE_ID}
    spec:
      serviceAccountName: ${COMPONENT_NAME}
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      containers:
        - name: api
          image: ${API_IMAGE}
          imagePullPolicy: Always
          ports:
            - name: http
              containerPort: ${API_CONTAINER_PORT}
          env:
            - name: WGCF_RUNTIME_PROFILE
              value: dev-integration
            - name: WGCF_RUNTIME_OWNER
              value: workspace-governance-control-fabric
            - name: WGCF_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: ${COMPONENT_NAME}-database
                  key: WGCF_DATABASE_URL
          readinessProbe:
            httpGet:
              path: /readyz
              port: http
            initialDelaySeconds: 3
            periodSeconds: 5
            timeoutSeconds: 2
            failureThreshold: 12
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 2
            failureThreshold: 6
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
---
apiVersion: v1
kind: Service
metadata:
  name: ${API_SERVICE}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: api
    devint.profile: ${PROFILE_ID}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: api
    devint.profile: ${PROFILE_ID}
  ports:
    - name: http
      port: ${API_CONTAINER_PORT}
      targetPort: http
EOF
}

deploy_api() {
  render_runtime_manifest
  kubectl_cmd apply -f "${RUNTIME_MANIFEST}"
  kubectl_cmd -n "${NAMESPACE}" rollout status "statefulset/${POSTGRES_STATEFULSET}" --timeout=180s
  run_database_migration
  kubectl_cmd -n "${NAMESPACE}" rollout status "deployment/${API_DEPLOYMENT}" --timeout=180s
}

scale_api() {
  local replicas="$1"
  if kubectl_cmd -n "${NAMESPACE}" get "deployment/${API_DEPLOYMENT}" >/dev/null 2>&1; then
    kubectl_cmd -n "${NAMESPACE}" scale "deployment/${API_DEPLOYMENT}" --replicas="${replicas}" >/dev/null
  fi
  if kubectl_cmd -n "${NAMESPACE}" get "statefulset/${POSTGRES_STATEFULSET}" >/dev/null 2>&1; then
    kubectl_cmd -n "${NAMESPACE}" scale "statefulset/${POSTGRES_STATEFULSET}" --replicas="${replicas}" >/dev/null
  fi
}

run_database_migration() {
  local job_name="${COMPONENT_NAME}-migrate"
  kubectl_cmd -n "${NAMESPACE}" delete job "${job_name}" --ignore-not-found=true >/dev/null
  cat <<EOF | kubectl_cmd apply -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job_name}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/name: ${APP_LABEL}
    app.kubernetes.io/component: migration
    devint.profile: ${PROFILE_ID}
spec:
  backoffLimit: 1
  template:
    metadata:
      labels:
        app.kubernetes.io/name: ${APP_LABEL}
        app.kubernetes.io/component: migration
        devint.profile: ${PROFILE_ID}
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
      restartPolicy: Never
      containers:
        - name: migrate
          image: ${API_IMAGE}
          imagePullPolicy: Always
          command:
            - alembic
            - upgrade
            - head
          env:
            - name: WGCF_DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: ${COMPONENT_NAME}-database
                  key: WGCF_DATABASE_URL
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
EOF
  kubectl_cmd -n "${NAMESPACE}" wait --for=condition=complete "job/${job_name}" --timeout=180s
  kubectl_cmd -n "${NAMESPACE}" logs "job/${job_name}" >"${DATABASE_MIGRATION_FILE}" 2>&1 || true
}

start_port_forward() {
  local log_name="$1"
  kubectl_cmd -n "${NAMESPACE}" port-forward "svc/${API_SERVICE}" "${API_LOCAL_PORT}:${API_CONTAINER_PORT}" >"${LOGS_DIR}/${log_name}" 2>&1 &
  local pf_pid=$!
  sleep 3
  if ! kill -0 "${pf_pid}" >/dev/null 2>&1; then
    echo "Port-forward for ${API_SERVICE} failed; see ${LOGS_DIR}/${log_name}" >&2
    return 1
  fi
  echo "${pf_pid}"
}

stop_port_forward() {
  local pid="$1"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    kill "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" >/dev/null 2>&1 || true
  fi
}

request_json() {
  local output_file="$1"
  local operation="$2"
  shift 2
  python3 - "http://127.0.0.1:${API_LOCAL_PORT}" "${output_file}" "${operation}" "$@" <<'PY'
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

base_url = sys.argv[1].rstrip("/")
output_file = pathlib.Path(sys.argv[2])
operation = sys.argv[3]
args = sys.argv[4:]


def request_json(path, *, method="GET", body=None):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            text = response.read().decode("utf-8")
            return response.status, json.loads(text) if text else {}
    except urllib.error.HTTPError as error:
        text = error.read().decode("utf-8")
        raise SystemExit(f"{method} {path} failed with {error.code}: {text or error.reason}") from error


if operation == "get":
    status, payload = request_json(args[0])
elif operation == "post-plan":
    status, payload = request_json(
        "/v1/validation-plans",
        method="POST",
        body={
            "scope": "repo:workspace-governance-control-fabric",
            "tier": "smoke",
        },
    )
else:
    raise SystemExit(f"unknown operation: {operation}")

record = {"status_code": status, "payload": payload}
output_file.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(record, sort_keys=True))
PY
}

write_access_file() {
  cat >"${ACCESS_FILE}" <<EOF
profile: ${PROFILE_ID}
namespace: ${NAMESPACE}
image: ${API_IMAGE}
service: ${API_SERVICE}
deployment: ${API_DEPLOYMENT}
postgres_service: ${POSTGRES_SERVICE}
local_url: http://127.0.0.1:${API_LOCAL_PORT}
health: http://127.0.0.1:${API_LOCAL_PORT}/healthz
readiness: http://127.0.0.1:${API_LOCAL_PORT}/readyz
status: http://127.0.0.1:${API_LOCAL_PORT}/v1/status
graph_query: http://127.0.0.1:${API_LOCAL_PORT}/v1/graph/query?scope=repo:workspace-governance-control-fabric
state_root: ${STATE_ROOT}
runtime_manifest: ${RUNTIME_MANIFEST}
EOF
}
