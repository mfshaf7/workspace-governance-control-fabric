#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
kubectl_cmd -n "${NAMESPACE}" rollout status "deployment/${API_DEPLOYMENT}" --timeout=180s
write_access_file

echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "image: ${API_IMAGE}"
echo "WGCF API: http://127.0.0.1:${API_LOCAL_PORT}"
echo "health: http://127.0.0.1:${API_LOCAL_PORT}/healthz"
echo "readiness: http://127.0.0.1:${API_LOCAL_PORT}/readyz"
echo "status: http://127.0.0.1:${API_LOCAL_PORT}/v1/status"
echo "graph query: http://127.0.0.1:${API_LOCAL_PORT}/v1/graph/query?scope=repo:workspace-governance-control-fabric"
echo
echo "Access details written to ${ACCESS_FILE}"
echo "Keep this process running while you inspect the dev-integration API service."
echo "Press Ctrl-C to close the access session; the k3s Deployment keeps running until devint-down."
echo

exec "${KUBECTL_CMD[@]}" -n "${NAMESPACE}" port-forward "svc/${API_SERVICE}" "${API_LOCAL_PORT}:${API_CONTAINER_PORT}"
