#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
set -Eeuo pipefail

if [[ -z "${AGENT_WORK_CHECKOUT:-}" ]]; then
  echo "stopped: AGENT_WORK_CHECKOUT is not set" >&2
  exit 2
fi
configured_gitlab_host="${GITLAB_HOST:-}"
case "$configured_gitlab_host" in
  ''|*://*|*/*)
    echo "stopped: GITLAB_HOST must be a bare hostname" >&2
    exit 2
    ;;
esac
configured_gitlab_project_id="${GITLAB_PROJECT_ID:-}"
case "$configured_gitlab_project_id" in
  ''|0|00*|*[!0-9]*)
    echo "stopped: GITLAB_PROJECT_ID must be a positive integer" >&2
    exit 2
    ;;
esac
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
if [[ -n "${AGENT_WORK_DIRENV:-}" && ( ! -f "$AGENT_WORK_CHECKOUT/.envrc" || -L "$AGENT_WORK_CHECKOUT/.envrc" ) ]]; then
  echo "stopped: AGENT_WORK_DIRENV requires a regular AGENT_WORK_CHECKOUT/.envrc" >&2
  exit 2
fi

cd "$AGENT_WORK_CHECKOUT"
if [[ -n "${AGENT_WORK_DIRENV:-}" ]]; then
  runner=(
    "$AGENT_WORK_DIRENV" exec "$AGENT_WORK_CHECKOUT" /usr/bin/env
    "GITLAB_HOST=$configured_gitlab_host"
    "GITLAB_PROJECT_ID=$configured_gitlab_project_id"
    "$AGENT_WORK_UV"
  )
else
  runner=("$AGENT_WORK_UV")
fi

if [[ "${1:-}" == "--apply" ]]; then
  exec "${runner[@]}" run agent-work loop run --apply --json
fi
exec "${runner[@]}" run agent-work loop run --json
