#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
need_cmd python3
need_cmd sha256sum
require_storage_authority_contract

backup_path="${DEVINT_BACKUP_FILE:-${BACKUPS_DIR}/wgcf-evidence-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}"
trap 'delete_storage_transfer_pod; cleanup_storage_backup_staging' EXIT
backup_evidence_storage "${backup_path}" "${STORAGE_BACKUP_RECEIPT_FILE}"
trap - EXIT

printf 'WGCF evidence backup written to %s\n' "${backup_path}"
printf 'Backup manifest written to %s.manifest.json\n' "${backup_path}"
printf 'Backup receipt written to %s\n' "${STORAGE_BACKUP_RECEIPT_FILE}"
