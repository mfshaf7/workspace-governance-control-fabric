#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
need_cmd python3
need_cmd sha256sum
require_storage_authority_contract
confirm_exact "${CONFIRM:-}" "restore-wgcf-evidence" "WGCF evidence restore"

backup_path="${DEVINT_BACKUP_FILE:-}"
if [[ -z "${backup_path}" ]]; then
  echo "refused: set DEVINT_BACKUP_FILE to an operator-scoped WGCF evidence backup" >&2
  exit 2
fi
validate_backup_for_restore "${backup_path}"

trap delete_storage_transfer_pod EXIT
pre_restore_path="${BACKUPS_DIR}/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
backup_evidence_storage "${pre_restore_path}" "${pre_restore_path}.receipt.json"
restore_evidence_storage "${backup_path}"
write_restore_receipt "${backup_path}" "${pre_restore_path}"
verify_storage_seed
write_storage_receipt
trap - EXIT

printf 'WGCF evidence restore completed from %s\n' "${backup_path}"
printf 'Pre-restore backup written to %s\n' "${pre_restore_path}"
printf 'Restore receipt written to %s\n' "${STORAGE_RESTORE_RECEIPT_FILE}"
