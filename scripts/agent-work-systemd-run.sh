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
if [[ -z "${AGENT_WORK_AGENT:-}" ]]; then
  log "stopped: AGENT_WORK_AGENT is not set"
  exit 2
fi
case "$AGENT_WORK_AGENT" in
  codex|claude|pi) ;;
  *)
    log "stopped: AGENT_WORK_AGENT must be one of: codex, claude, pi"
    exit 2
    ;;
esac
if [[ -z "${AGENT_WORK_AGENT_PATH:-}" ]]; then
  log "stopped: AGENT_WORK_AGENT_PATH is not set"
  exit 2
fi
if [[ ! -x "$AGENT_WORK_AGENT_PATH" ]]; then
  log "stopped: AGENT_WORK_AGENT_PATH is not executable: $AGENT_WORK_AGENT_PATH"
  exit 2
fi

cd "$AGENT_WORK_CHECKOUT"
log "starting applied $AGENT_WORK_AGENT pass in $AGENT_WORK_CHECKOUT"

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
