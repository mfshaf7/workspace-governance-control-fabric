#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

need_cmd k3s
need_cmd python3
need_cmd sha256sum
require_storage_authority_contract
confirm_exact "${CONFIRM:-}" "restore-wgcf-evidence" "WGCF evidence restore"
require_no_pending_storage_credential_rotation

backup_path="${DEVINT_BACKUP_FILE:-}"
if [[ -z "${backup_path}" ]]; then
  echo "refused: set DEVINT_BACKUP_FILE to an operator-scoped WGCF evidence backup" >&2
  exit 2
fi

if [[ "${WGCF_RESTORE_INPUTS_SEALED:-}" != "1" ]]; then
  exec python3 "${PROFILE_ROOT}/scripts/lib/run_with_sealed_restore_inputs.py" \
    "${backup_path}" "${STATE_ROOT}" "${ARCHIVE_ROOT}" "$0"
fi

readonly STORAGE_RESTORE_VALIDATED_ARCHIVE="/proc/self/fd/${WGCF_RESTORE_ARCHIVE_FD:?}"
readonly STORAGE_RESTORE_VALIDATED_MANIFEST="/proc/self/fd/${WGCF_RESTORE_MANIFEST_FD:?}"
trap 'delete_storage_transfer_pod; cleanup_storage_backup_staging' EXIT
validate_backup_for_restore \
  "${STORAGE_RESTORE_VALIDATED_ARCHIVE}" \
  "${STORAGE_RESTORE_VALIDATED_MANIFEST}" \
  sealed
verify_storage_isolation
verify_storage_network_enforcement
pre_restore_path="${BACKUPS_DIR}/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
pre_restore_state="$(probe_live_receipt_version)"
case "${pre_restore_state}" in
  present)
    backup_evidence_storage "${pre_restore_path}" "${pre_restore_path}.receipt.json"
    pre_restore_state="backup-created"
    ;;
  missing)
    require_empty_storage_for_receipt_loss
    pre_restore_path=""
    pre_restore_state="empty-live-store"
    ;;
  *)
    echo "Unexpected live receipt-version state: ${pre_restore_state}" >&2
    exit 1
    ;;
esac
restore_evidence_storage \
  "${STORAGE_RESTORE_VALIDATED_ARCHIVE}" \
  "${STORAGE_RESTORE_VALIDATED_MANIFEST}"
verify_storage_isolation
verify_storage_network_enforcement
refresh_storage_receipt_isolation
verify_storage_seed receipt
write_restore_receipt "${STORAGE_RESTORE_VALIDATED_ARCHIVE}" "${pre_restore_path}" \
  "${pre_restore_state}"
trap - EXIT

printf 'WGCF evidence restore completed from sealed content-addressed input\n'
printf 'Pre-restore state: %s\n' "${pre_restore_state}"
if [[ -n "${pre_restore_path}" ]]; then
  printf 'Pre-restore backup written to %s\n' "${pre_restore_path}"
fi
printf 'Restore receipt written to %s\n' "${STORAGE_RESTORE_RECEIPT_FILE}"
