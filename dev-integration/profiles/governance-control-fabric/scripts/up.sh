#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

trap cleanup_storage_credential_retirement EXIT

need_cmd k3s
need_cmd python3
need_cmd sha256sum
deploy_api
write_access_file
cleanup_storage_credential_retirement
trap - EXIT

echo "profile: ${PROFILE_ID}"
echo "namespace: ${NAMESPACE}"
echo "operator: ${OPERATOR}"
echo "image: ${API_IMAGE}"
echo "deployment: ${API_DEPLOYMENT}"
echo "service: ${API_SERVICE}"
echo "postgres: ${POSTGRES_SERVICE}"
echo "object storage: ${STORAGE_SERVICE}"
echo "storage bucket: ${STORAGE_BUCKET}"
echo "storage receipt: ${STORAGE_RECEIPT_FILE}"
echo "temporal activity worker: ${TEMPORAL_WORKER_DEPLOYMENT}"
echo "temporal activity worker image: ${TEMPORAL_WORKER_IMAGE}"
echo "temporal activity worker enabled: ${TEMPORAL_WORKER_ENABLED}"
echo "runtime manifest: ${RUNTIME_MANIFEST}"
echo "worker status artifact: ${TEMPORAL_WORKER_STATUS_FILE}"
echo "database migration artifact: ${DATABASE_MIGRATION_FILE}"
echo "access artifact: ${ACCESS_FILE}"
