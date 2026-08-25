#!/usr/bin/env bash
set -Eeuo pipefail

log_tag="${AGENT_WORK_LOG_TAG:-agent-work}"

log() {
  logger -s -t "$log_tag" -- "$*"
}

run_logged() {
  local statuses

  set +e
  "$@" 2>&1 | logger -s -t "$log_tag"
  statuses=("${PIPESTATUS[@]}")
  set -e

  if (( statuses[1] != 0 )); then
    return "${statuses[1]}"
  fi
  return "${statuses[0]}"
}

if [[ -z "${AGENT_WORK_CHECKOUT:-}" ]]; then
  log "stopped: AGENT_WORK_CHECKOUT is not set"
  exit 2
fi
if [[ -z "${AGENT_WORK_UV:-}" ]]; then
  log "stopped: AGENT_WORK_UV is not set"
  exit 2
fi
if [[ ! -x "$AGENT_WORK_UV" ]]; then
  log "stopped: AGENT_WORK_UV is not executable: $AGENT_WORK_UV"
  exit 2
fi
if [[ -z "${AGENT_WORK_CODEX:-}" ]]; then
  log "stopped: AGENT_WORK_CODEX is not set"
  exit 2
fi
if [[ ! -x "$AGENT_WORK_CODEX" ]]; then
  log "stopped: AGENT_WORK_CODEX is not executable: $AGENT_WORK_CODEX"
  exit 2
fi

cd "$AGENT_WORK_CHECKOUT"
codex_directory="$(dirname "$AGENT_WORK_CODEX")"
export PATH="$codex_directory:$PATH"
log "starting applied pass in $AGENT_WORK_CHECKOUT"

if run_logged "${AGENT_WORK_UV}" run agent-work run-next --apply --json; then
  :
else
  status=$?
  log "stopped: pass exited $status"
  exit "$status"
fi

log "pass completed; returning dedicated checkout to main"
if run_logged git switch main; then
  :
else
  status=$?
  log "stopped: could not switch to main (exit $status)"
  exit "$status"
fi
if run_logged git pull --ff-only; then
  :
else
  status=$?
  log "stopped: could not fast-forward main (exit $status)"
  exit "$status"
fi

log "pass completed; next timer activation follows the configured interval"
