#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-unknown}"

cat <<MSG
governance-control-fabric dev-integration profile is proposed, not active.

Action requested: ${ACTION}

This profile records the runtime-lane decision only. It is not self-serve
launchable until platform acceptance, required security review, active
workspace registry state, and runnable local-k3s commands are landed.
MSG

exit 2
