#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

ensure_state_dirs

cat >"${PROFILE_PROMOTION_NOTES}" <<EOF
# $(profile_summary) Stage Handoff Notes

Session manifest:
- ${SESSION_FILE}

Profile session artifact:
- ${SESSION_ARTIFACT}

Generic promotion report:
- ${PROMOTION_REPORT}

Before governed stage rehearsal:

1. Turn the winning local control-fabric changes into reviewed commits in \`workspace-governance-control-fabric\`.
2. Land platform access and release-gate changes in \`platform-engineering\`.
3. Land active-profile admission truth in \`workspace-governance\`.
4. Keep security boundary evidence in \`security-architecture\` aligned with the profile.
5. Rehearse the final candidate on governed stage only after the profile-owned checks are proven:
$(stage_handoff_required_checks_markdown)

Dev-integration is local evidence only. It is not an approved stage or prod
deployment and must not be promoted directly.
EOF

if [[ -f "${PROMOTION_REPORT}" ]]; then
  cat "${PROMOTION_REPORT}"
  echo
fi
cat "${PROFILE_PROMOTION_NOTES}"
