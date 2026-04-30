#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
deploy_api
write_access_file

echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "operator: ${OPERATOR}"
echo "image: ${API_IMAGE}"
echo "deployment: ${API_DEPLOYMENT}"
echo "service: ${API_SERVICE}"
echo "postgres: ${POSTGRES_SERVICE}"
echo "runtime manifest: ${RUNTIME_MANIFEST}"
echo "database migration artifact: ${DATABASE_MIGRATION_FILE}"
echo "access artifact: ${ACCESS_FILE}"
