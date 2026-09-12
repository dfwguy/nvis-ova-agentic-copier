---
name: setup-agent-loop
description: Configure one guarded, serial GitLab polling loop; not webhooks, queues, or automatic merge.
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Set up agent loop

Use this skill to configure one GitLab project for the recoverable `loop run` state machine.
It neither installs a webhook nor implicitly authorizes unattended publication.
Its Developer-readable readiness path permits exactly 30 effective API access
before checking repository safeguards.

1. Establish and validate a bare `GITLAB_HOST` and numeric
   `GITLAB_PROJECT_ID` first. Then run the complete Developer-readable
   readiness preflight using only the preinstalled Git, `glab`, selected coding
   agent, and
   `jq` tools and their existing noninteractive sessions. Do not print or
   place tokens in commands, environment files, or logs:

   ```bash
   set -Eeuo pipefail
   block_apply() {
     printf '%s\n' "missing evidence: $1; block --apply. Action: correct the readiness configuration, then rerun this preflight." >&2
     exit 1
   }
   trap 'status=$?; trap - ERR; block_apply "preflight command failed (exit $status)"' ERR
   case "${GITLAB_HOST:-}" in ''|*://*|*/*) block_apply 'GITLAB_HOST must be a bare hostname';; esac
   case "${GITLAB_PROJECT_ID:-}" in ''|*[!0-9]*) block_apply 'GITLAB_PROJECT_ID must be numeric';; esac
   agent_name="${AGENT_WORK_AGENT:-codex}"
   case "$agent_name" in codex|claude|pi) ;; *) block_apply 'AGENT_WORK_AGENT must be codex, claude, or pi';; esac
   if ! command -v git glab "$agent_name" jq >/dev/null; then
     block_apply 'install the required Git, glab, selected coding agent, and jq tools'
   fi
   case "$agent_name" in
     codex) codex login status >/dev/null || block_apply 'authenticate Codex';;
     claude) claude auth status >/dev/null || block_apply 'authenticate Claude Code';;
     pi) ;;
   esac
   if ! glab api --hostname "$GITLAB_HOST" user >/dev/null; then
     block_apply 'authenticate the active GitLab API credential route for the configured host'
   fi
   if ! project_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID")"; then
     block_apply 'read the configured numeric GitLab project'
   fi
   if ! jq -e . >/dev/null <<<"$project_json"; then
     block_apply 'return a valid project JSON response'
   fi
   if ! effective_access="$(
     jq -er '
       [
         .permissions.project_access.access_level,
         .permissions.group_access.access_level
       ]
       | map(select(type == "number"))
       | if length == 0 then "unknown" else max | tostring end
     ' <<<"$project_json"
   )"; then
     block_apply 'return numeric project or group API access'
   fi
   if [[ "$effective_access" != 30 ]]; then
     block_apply "effective API access is $effective_access; only Developer (30) may use this readiness path"
   fi
   if ! default_branch="$(jq -er '.default_branch | strings | select(length > 0)' <<<"$project_json")"; then
     block_apply 'return a nonempty default branch in the project response'
   fi
   default_branch_uri="$(jq -rn --arg value "$default_branch" '$value | @uri')"
   if ! effective_branch_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/repository/branches/$default_branch_uri")"; then
     block_apply 'read effective protection for the encoded default branch from the numeric project endpoint'
   fi
   if ! jq -e --arg default_branch "$default_branch" '
     (.name | type == "string" and . == $default_branch)
     and (.default | type == "boolean" and . == true)
     and (.protected | type == "boolean" and . == true)
     and (.developers_can_push | type == "boolean" and . == false)
     and (.developers_can_merge | type == "boolean" and . == false)
     and (.can_push | type == "boolean" and . == false)
   ' >/dev/null <<<"$effective_branch_json"; then
     block_apply 'protect the effective default branch from direct or Developer push and merge'
   fi
   if ! approval_rules_json="$(glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID/approval_rules")"; then
     block_apply 'read approval rules from the numeric project endpoint'
   fi
   if ! jq -e --arg default_branch "$default_branch" '
     type == "array" and any(.[]?;
       ((.approvals_required? // 0) | type == "number" and . >= 1)
       and (
         (has("protected_branches") | not)
         or ((.protected_branches | type == "array") and (.protected_branches | length == 0))
         or (.applies_to_all_protected_branches == true)
       )
     )
   ' >/dev/null <<<"$approval_rules_json"; then
     block_apply 'require at least one approval from an unambiguous global rule'
   fi
   printf '%s\n' 'Effective API access: Developer (30); Maintainer-only approval audit and attestation not required for this readiness policy.'
   ```
2. Create a clean dedicated checkout that is not an operator's active checkout.
   Run the runner from that checkout. It derives the checkout and GitLab project
   from the current directory and `origin`.
   Durable state, the single-project lock, and one worktree per active issue
   live below the XDG state directory, outside the checkout. Stashes are
   emergency evidence only and are never the normal persistence mechanism.
3. Check `uv`, the selected `codex`, `claude`, or `pi` agent, and user-level `systemd`, then run `uv run agent-work loop run --json` from the
   dedicated checkout as a preview smoke test. Do not use a separately installed
   bare `agent-work` executable, which can be stale. Install and enable the
   documented project-named user-level timer only after explicit authorization.
   Record the selected agent name and absolute executable paths for both `uv`
   and the agent in its environment file; the runner invokes that recorded
   absolute agent path directly, rather than relying on the service manager PATH.
   Record the bare `GITLAB_HOST` and positive numeric `GITLAB_PROJECT_ID` in
   the mode-600 environment file with those paths, but never a token. If the
   GitLab token is directory-scoped through `direnv`, select it only when the
   dedicated checkout has a regular checkout-local `.envrc`, record an absolute
   executable `direnv` path, and run the runner through `direnv exec
   <checkout>`. The wrapper rejects a configured `direnv` route without that
   local file and restores the recorded host and project ID after `direnv`
   supplies credentials. Before enabling the timer, execute the wrapper's
   no-`--apply` preview through `env -i` with exactly the public service
   variables and minimal service baseline so an interactive credential or
   identity cannot mask the configured route. The user manager does not inherit
   interactive `direnv` variables; never put the token in the environment file
   or use `glab auth status` to test this credential route, because it reports
   only stored `glab` logins.

   ```bash
   unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
   uv_path="$(command -v uv)"
   agent_name="${AGENT_WORK_AGENT:-codex}"
   agent_path="$(command -v "$agent_name")"
   gitlab_host="${GITLAB_HOST:-}"
   gitlab_project_id="${GITLAB_PROJECT_ID:-}"
   case "$gitlab_host" in ''|*://*|*/*) exit 1;; esac
   case "$gitlab_project_id" in ''|0|00*|*[!0-9]*) exit 1;; esac
   direnv_path=""
   if [[ -f "$PWD/.envrc" ]]; then
     direnv_path="$(command -v direnv || true)"
     [[ "$direnv_path" == /* && -x "$direnv_path" ]] || exit 1
   fi
   git_path="$(command -v git)"; glab_path="$(command -v glab)"; jq_path="$(command -v jq)"; flock_path="$(command -v flock)"
   [[ "$git_path" == /* && -x "$git_path" && "$glab_path" == /* && -x "$glab_path" && "$jq_path" == /* && -x "$jq_path" && "$flock_path" == /* && -x "$flock_path" && "$uv_path" == /* && -x "$uv_path" ]] || exit 1
   case "$agent_name" in codex|claude|pi) ;; *) exit 1;; esac
   [[ "$agent_path" == /* && -x "$agent_path" ]] || exit 1
   install -D -m 0644 "systemd/$unit_name.service" "$HOME/.config/systemd/user/$unit_name.service"
   install -D -m 0644 "systemd/$unit_name.timer" "$HOME/.config/systemd/user/$unit_name.timer"
   install -d -m 700 "$HOME/.config/$unit_name"
   printf 'AGENT_WORK_CHECKOUT=%q\nAGENT_WORK_GIT=%q\nAGENT_WORK_GLAB=%q\nAGENT_WORK_JQ=%q\nAGENT_WORK_FLOCK=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\nAGENT_WORK_BASE_BRANCH=%q\nGITLAB_HOST=%q\nGITLAB_PROJECT_ID=%q\n' "$PWD" "$git_path" "$glab_path" "$jq_path" "$flock_path" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" agent/work-base "$gitlab_host" "$gitlab_project_id" >"$HOME/.config/$unit_name/agent-work.env"
   chmod 600 "$HOME/.config/$unit_name/agent-work.env"

   env -i HOME="$HOME" PATH="$PATH" XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}" \
     AGENT_WORK_CHECKOUT="$PWD" AGENT_WORK_GIT="$git_path" AGENT_WORK_GLAB="$glab_path" AGENT_WORK_JQ="$jq_path" AGENT_WORK_FLOCK="$flock_path" AGENT_WORK_UV="$uv_path" AGENT_WORK_AGENT="$agent_name" \
     AGENT_WORK_AGENT_PATH="$agent_path" AGENT_WORK_DIRENV="$direnv_path" \
     AGENT_WORK_BASE_BRANCH=agent/work-base GITLAB_HOST="$gitlab_host" \
     GITLAB_PROJECT_ID="$gitlab_project_id" \
     bash "$PWD/scripts/agent-work-systemd-run.sh"
   ```

   The wrapper emits once to stdout/stderr and systemd records that output, so
   operators use `journalctl --user -u <project-slug>-agent-work.service`.
   Only after that isolated preview succeeds may the operator run
   `systemctl --user enable --now <project-slug>-agent-work.timer`.
   The timer schedules 20 minutes after the previous service completion and
   uses a stable randomized delay to spread starts. Require linger
   with `sudo loginctl enable-linger "$USER"`, then verify `Linger=yes` using
   `loginctl show-user "$USER" --property=Linger`.

   After this wrapper first merges, stop the timer, manually fast-forward the
   dedicated checkout, run the isolated preview and a canary, then restart the
   timer. Later applied activations verify the configured GitLab project and
   refresh the default branch before Python starts. Local executable/rewrite
   Git configuration and target filter attributes are rejected, hooks are
   disabled, and system/global credential and transport configuration remains
   the operator-managed service boundary.
4. Do not make a second project request or query membership. The step 1
   preflight uses local `jq` to classify effective API access as the
   `max` of `.permissions.project_access.access_level` and
   `.permissions.group_access.access_level`, ignoring missing values. Missing
   or nonnumeric access is `unknown`. Only exactly 30 is permitted. An
   `unknown` result, an access level less than 30, or 40 or greater must block
   `--apply`.
5. An access level of exactly 30 is the Developer readiness fast path. Obtain
   the one-time unattended-publication authorization and use the constrained
   service identity. The encoded
   `repository/branches/$default_branch_uri` endpoint checked in step 1 is
   authoritative for the effective default branch and must return the exact
   default-branch name, strict Boolean `default` and `protected` values of
   true, and strict Boolean `developers_can_push`, `developers_can_merge`, and
   `can_push` values of false. This handles wildcard and multiple protection
   rules without trying to infer their effective result. `approval_rules` must
   contain at least one rule with numeric `approvals_required >= 1` that is
   unambiguously global: it has no protected branches, no protected branches
   are configured, or `applies_to_all_protected_branches` is true. An explicit
   branch-scoped or malformed rule is missing evidence and must block
   `--apply`.

   If any preflight command fails, evidence is inaccessible, or evidence is
   absent, report `missing evidence` and block `--apply`. The actionable
   remediation is: have a Maintainer correct the reported project safeguard,
   then rerun this preflight. After every check passes, report exactly:
   `Effective API access: Developer (30); Maintainer-only approval audit and attestation not required for this readiness policy.`
   For exactly 30, skip only the Maintainer-only GET `/approvals` audit and
   recurring Maintainer attestation; keep every other identity, publication,
   checkout, origin, and preview check.
6. For 40 or greater, use a constrained Developer credential before retrying;
   the executable readiness path blocks privileged values.
7. This role classification is readiness, not hard proof of repository policy.
   Developer access cannot prove self-approval prevention or per-MR approval
   rule overrides, so this readiness selection skips those Maintainer-only
   checks and relies on workflow, settings, and runtime never-approve/merge
   guardrails. Developers may approve merge requests, and project settings
   control pushes, merges, and self-approval. Git and `glab` credentials may
   differ. Preserve the one-time authorization, constrained identity,
   dedicated checkout and origin checks, and preview; never approve or merge.
8. Stop the user timer before changing the checkout, credentials, or GitLab
   labels: `systemctl --user disable --now <project-slug>-agent-work.timer`.
   Inventory legacy labels, branches, worktrees, and stashes with
   `uv run agent-work migration preview --json`; apply only the exact reviewed
   plan, then validate one canary before re-enabling the timer.

Enabling the guarded loop authorizes it to resume active or automatically
resumable work before selecting fresh open, non-confidential issues without a
lifecycle projection, ledger record, or open split prerequisite. Fresh work
ranks exact high, normal (no recognized exact priority label), then exact low;
absolute creation time and IID break each rank. It projects active work as
`agent::running`, and then uses
`agent::awaiting-human` only after exact same-project MR evidence, matching
local/remote branch tips, default target, and `Closes #IID`. Expected human
action uses `agent::blocked`; `agent::failed` is reserved for exceptional,
unrecoverable cases. Ordinary branches are deterministic: `agent/issue-<IID>`.
Expected durable outcomes exit zero; exit one means state could not be safely
recorded or reconciled, and exit two means invalid CLI usage. The lock is
single-host and is not a distributed GitLab compare-and-set claim. Never
approve or merge a resulting MR.
