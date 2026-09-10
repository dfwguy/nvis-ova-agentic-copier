#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${AGENT_WORK_CHECKOUT:-}" ]]; then
  echo "stopped: AGENT_WORK_CHECKOUT is not set" >&2
  exit 2
fi
if [[ -z "${AGENT_WORK_UV:-}" ]]; then
  echo "stopped: AGENT_WORK_UV is not set" >&2
  exit 2
fi
if [[ ! -x "$AGENT_WORK_UV" ]]; then
  echo "stopped: AGENT_WORK_UV is not executable: $AGENT_WORK_UV" >&2
  exit 2
fi

agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in
  codex|claude|pi) ;;
  *)
    echo "stopped: AGENT_WORK_AGENT must be one of: codex, claude, pi" >&2
    exit 2
    ;;
esac
if [[ -z "${AGENT_WORK_AGENT_PATH:-}" ]]; then
  echo "stopped: AGENT_WORK_AGENT_PATH is not set" >&2
  exit 2
fi
if [[ ! -x "$AGENT_WORK_AGENT_PATH" ]]; then
  echo "stopped: AGENT_WORK_AGENT_PATH is not executable: $AGENT_WORK_AGENT_PATH" >&2
  exit 2
fi
if [[ -n "${AGENT_WORK_DIRENV:-}" && ! -x "$AGENT_WORK_DIRENV" ]]; then
  echo "stopped: AGENT_WORK_DIRENV is not executable: $AGENT_WORK_DIRENV" >&2
  exit 2
fi

cd "$AGENT_WORK_CHECKOUT"
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  runner=("$AGENT_WORK_DIRENV" exec "$AGENT_WORK_CHECKOUT" "$AGENT_WORK_UV")
else
  runner=("$AGENT_WORK_UV")
fi

if [[ "${1:-}" == "--apply" ]]; then
  exec "${runner[@]}" run agent-work loop run --apply --json
fi
exec "${runner[@]}" run agent-work loop run --json
