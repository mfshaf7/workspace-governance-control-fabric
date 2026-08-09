#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
need_cmd python3
need_cmd sha256sum
require_storage_authority_contract
ensure_state_dirs
kubectl_cmd -n "${NAMESPACE}" rollout status "deployment/${API_DEPLOYMENT}" --timeout=180s
wait_for_storage_ready

pf_pid="$(start_port_forward smoke-port-forward.log)"
trap 'stop_port_forward "${pf_pid}"' EXIT

request_json "${API_HEALTH_FILE}" get /healthz >/dev/null
request_json "${READINESS_FILE}" get /readyz >/dev/null
request_json "${STATUS_FILE}" get /v1/status >/dev/null
request_json "${GRAPH_QUERY_FILE}" get "/v1/graph/query?scope=repo:workspace-governance-control-fabric" >/dev/null
request_json "${VALIDATION_PLAN_FILE}" post-plan >/dev/null
request_json "${RECEIPTS_FILE}" get /v1/receipts >/dev/null
write_access_file
verify_storage_seed receipt
verify_storage_isolation
verify_storage_network_proof

cat >"${SMOKE_SUMMARY}" <<EOF
governance-control-fabric dev-integration smoke (read-only)

profile: ${PROFILE_ID}
namespace: ${NAMESPACE}
image: ${API_IMAGE}
deployment: ${API_DEPLOYMENT}
service: ${API_SERVICE}
postgres: ${POSTGRES_SERVICE}
object storage: ${STORAGE_SERVICE}
storage bucket: ${STORAGE_BUCKET}

checks:
- API health: ${API_HEALTH_FILE}
- API readiness: ${READINESS_FILE}
- status read: ${STATUS_FILE}
- component inventory graph read: ${GRAPH_QUERY_FILE}
- database migration: ${DATABASE_MIGRATION_FILE}
- validation planner dry run: ${VALIDATION_PLAN_FILE}
- receipt and ledger metadata read: ${RECEIPTS_FILE}
- version-bound evidence object digest read: ${STORAGE_VERIFICATION_FILE}
- same-key overwrite preservation proof: ${STORAGE_VERSION_PROOF_FILE}
- storage credential and network isolation: ${STORAGE_ISOLATION_FILE}
- live storage network enforcement: ${STORAGE_NETWORK_ENFORCEMENT_FILE}
- storage receipt: ${STORAGE_RECEIPT_FILE}

raw validation execution is intentionally not run by shared smoke because this
profile is persistent and its smoke mutation mode is read-only.
EOF

cat "${SMOKE_SUMMARY}"
