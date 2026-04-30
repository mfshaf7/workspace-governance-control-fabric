#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
ensure_state_dirs
write_access_file

if ! namespace_status="$(kubectl_cmd get namespace "${NAMESPACE}" 2>&1)"; then
  if [[ "${namespace_status}" == *"NotFound"* ]]; then
    echo "profile: ${PROFILE_ID}"
    echo "namespace: ${NAMESPACE}"
    echo "status: not-created"
    echo "state root: ${STATE_ROOT}"
    echo "access artifact: ${ACCESS_FILE}"
    exit 0
  fi
  echo "${namespace_status}" >&2
  exit 1
fi

echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "operator: ${OPERATOR}"
echo "state root: ${STATE_ROOT}"
echo "image: ${API_IMAGE}"
echo "access artifact: ${ACCESS_FILE}"
echo
kubectl_cmd -n "${NAMESPACE}" get deploy,pods,svc -l "devint.profile=${PROFILE_ID}"
echo
kubectl_cmd -n "${NAMESPACE}" get statefulset,pvc,job -l "devint.profile=${PROFILE_ID}"
