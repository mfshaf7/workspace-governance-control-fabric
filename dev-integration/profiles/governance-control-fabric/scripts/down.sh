#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
scale_api 0
echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "deployment: ${API_DEPLOYMENT}"
echo "replicas: 0"
echo "state preserved: ${STATE_ROOT}"
