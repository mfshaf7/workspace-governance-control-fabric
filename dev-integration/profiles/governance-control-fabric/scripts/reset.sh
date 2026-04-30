#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
kubectl_cmd delete namespace "${NAMESPACE}" --ignore-not-found=true
if [[ -d "${STATE_ROOT}" ]]; then
  find "${STATE_ROOT}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
ensure_state_dirs
echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "state reset: ${STATE_ROOT}"
