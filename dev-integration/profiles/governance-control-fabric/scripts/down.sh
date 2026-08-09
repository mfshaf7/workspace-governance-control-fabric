#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
scale_runtime 0
echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "deployment: ${API_DEPLOYMENT}"
echo "object storage: ${STORAGE_STATEFULSET}"
echo "replicas: 0"
echo "state preserved: ${STATE_ROOT}"
