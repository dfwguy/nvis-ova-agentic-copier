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

return_checkout_to_base_branch() {
  local base_branch="${AGENT_WORK_BASE_BRANCH:-main}"
  local stash_ref

  if [[ -n "$(git status --porcelain)" ]]; then
    log "preserving uncommitted worker changes in an agent-work recovery stash"
    if run_logged git stash push --include-untracked --message "agent-work recovery"; then
      :
    else
      status=$?
      log "stopped: could not preserve worker changes (exit $status)"
      return "$status"
    fi
    if [[ -n "$(git status --porcelain)" ]]; then
      log "stopped: worker changes remain after recovery stash"
      return 1
    fi
    stash_ref="$(git rev-parse --verify -q refs/stash)"
    log "preserved worker changes in recovery stash $stash_ref"
  fi

  log "returning dedicated checkout to $base_branch"
  if run_logged git switch "$base_branch"; then
    :
  else
    status=$?
    log "stopped: could not switch to $base_branch (exit $status)"
    return "$status"
  fi
  if run_logged git pull --ff-only; then
    :
  else
    status=$?
    log "stopped: could not fast-forward $base_branch (exit $status)"
    return "$status"
  fi
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
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in
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
if [[ -n "${AGENT_WORK_DIRENV:-}" && ! -x "$AGENT_WORK_DIRENV" ]]; then
  log "stopped: AGENT_WORK_DIRENV is not executable: $AGENT_WORK_DIRENV"
  exit 2
fi

cd "$AGENT_WORK_CHECKOUT"
log "starting applied $agent_name pass in $AGENT_WORK_CHECKOUT"

if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  runner=("$AGENT_WORK_DIRENV" exec "$AGENT_WORK_CHECKOUT" "$AGENT_WORK_UV")
else
  runner=("$AGENT_WORK_UV")
fi
pass_status=0
if run_logged "${runner[@]}" run agent-work run-next --apply --json; then
  :
else
  pass_status=$?
  log "pass failed (exit $pass_status); attempting checkout recovery"
fi

if return_checkout_to_base_branch; then
  :
else
  cleanup_status=$?
  exit "$cleanup_status"
fi
if (( pass_status != 0 )); then
  exit "$pass_status"
fi

log "pass completed; next timer activation follows the configured interval"
