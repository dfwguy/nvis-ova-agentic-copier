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

Use this skill for one GitLab project's recoverable `loop run` state machine.
It installs no webhook. It does not widen `AGENTS.md` authority. Standing
factory authority covers unattended publication. It covers user-timer actions.
It needs no per-command confirmation. Its Developer route permits only
effective API access 30. It then checks repository safeguards.

1. Validate bare `GITLAB_HOST` and numeric `GITLAB_PROJECT_ID`. Run the full
   Developer readiness preflight. Use only installed Git, `glab`, the chosen
   coding agent, and `jq`. Use current noninteractive sessions. Never print a
   token. Never put one in a command, environment file, or log:

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
2. Make a clean, dedicated checkout. It must not be an operator's active
   checkout. Run the runner there. It gets its checkout and project from the
   current directory and `origin`. Keep durable state below XDG state. Keep the
   single-project lock there. Keep each active issue worktree there, outside the checkout. Stashes
   are emergency evidence. They are not normal persistence.
3. Check `uv`, the chosen `codex`, `claude`, or `pi`, and user `systemd`. In the
   dedicated checkout, run `uv run agent-work loop canary --json`. This is a
   non-claiming smoke preview. Do not use a separately installed bare
   `agent-work`; it may be stale. Install and enable the documented,
   project-named user timer only with relevant `AGENTS.md` standing authority.
   Or use separate explicit authority. Save the agent name in its environment file.
   Save absolute `uv` and agent paths there. The runner calls the saved agent path.
   Never use service-manager PATH. Save bare `GITLAB_HOST`, positive numeric
   `GITLAB_PROJECT_ID`, and those paths in the mode-600 file. Never save a
   token. For a directory-scoped `direnv` token, require a regular
   checkout-local `.envrc`. Save an absolute executable `direnv` path. Run with
   `direnv exec <checkout>`. The wrapper rejects a configured route without the
   file. `direnv` supplies credentials. The wrapper then restores saved host and project
   ID. Before timer enablement, run its no-`--apply` preview with
   `env -i`. Use exactly public service variables. Use the least service baseline.
   An interactive credential must not mask a bad route. Nor may an identity. The user
   manager does not inherit interactive `direnv` values. Never store its token.
   Do not use `glab auth status`; it checks stored `glab` logins only.

   ```bash
   unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
   uv_path="$(command -v uv)"
   agent_name="${AGENT_WORK_AGENT:-codex}"
   agent_path="$(command -v "$agent_name")"
   gitlab_host="${GITLAB_HOST:-}"
   gitlab_project_id="${GITLAB_PROJECT_ID:-}"
   factory_username="${AGENT_WORK_FACTORY_USERNAME:-}"
   case "$gitlab_host" in ''|*://*|*/*) exit 1;; esac
   case "$gitlab_project_id" in ''|0|00*|*[!0-9]*) exit 1;; esac
   [[ "$factory_username" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || exit 1
   direnv_path=""
   if [[ -f "$PWD/.envrc" ]]; then
     direnv_path="$(command -v direnv || true)"
     [[ "$direnv_path" == /* && -x "$direnv_path" ]] || exit 1
   fi
   git_path="$(command -v git)"; glab_path="$(command -v glab)"; jq_path="$(command -v jq)"; flock_path="$(command -v flock)"; timeout_path="$(command -v timeout)"
   [[ "$git_path" == /* && -x "$git_path" && "$glab_path" == /* && -x "$glab_path" && "$jq_path" == /* && -x "$jq_path" && "$flock_path" == /* && -x "$flock_path" && "$timeout_path" == /* && -x "$timeout_path" && "$uv_path" == /* && -x "$uv_path" ]] || exit 1
   case "$agent_name" in codex|claude|pi) ;; *) exit 1;; esac
   [[ "$agent_path" == /* && -x "$agent_path" ]] || exit 1
   install -D -m 0644 "systemd/$unit_name.service" "$HOME/.config/systemd/user/$unit_name.service"
   install -D -m 0644 "systemd/$unit_name.timer" "$HOME/.config/systemd/user/$unit_name.timer"
   install -d -m 700 "$HOME/.config/$unit_name"
   printf 'AGENT_WORK_ENV_VERSION=%q\nAGENT_WORK_CHECKOUT=%q\nAGENT_WORK_GIT=%q\nAGENT_WORK_GLAB=%q\nAGENT_WORK_JQ=%q\nAGENT_WORK_FLOCK=%q\nAGENT_WORK_TIMEOUT=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\nAGENT_WORK_BASE_BRANCH=%q\nAGENT_WORK_FACTORY_USERNAME=%q\nGITLAB_HOST=%q\nGITLAB_PROJECT_ID=%q\n' 1 "$PWD" "$git_path" "$glab_path" "$jq_path" "$flock_path" "$timeout_path" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" agent/work-base "$factory_username" "$gitlab_host" "$gitlab_project_id" >"$HOME/.config/$unit_name/agent-work.env"
   chmod 600 "$HOME/.config/$unit_name/agent-work.env"

   env -i HOME="$HOME" PATH="$PATH" XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}" \
     AGENT_WORK_ENV_VERSION=1 AGENT_WORK_CHECKOUT="$PWD" AGENT_WORK_GIT="$git_path" AGENT_WORK_GLAB="$glab_path" AGENT_WORK_JQ="$jq_path" AGENT_WORK_FLOCK="$flock_path" AGENT_WORK_TIMEOUT="$timeout_path" AGENT_WORK_UV="$uv_path" AGENT_WORK_AGENT="$agent_name" \
     AGENT_WORK_AGENT_PATH="$agent_path" AGENT_WORK_DIRENV="$direnv_path" \
     AGENT_WORK_BASE_BRANCH=agent/work-base AGENT_WORK_FACTORY_USERNAME="$factory_username" GITLAB_HOST="$gitlab_host" \
     GITLAB_PROJECT_ID="$gitlab_project_id" \
     bash "$PWD/scripts/agent-work-systemd-run.sh"
   ```

   The wrapper writes once to stdout/stderr. systemd records it. Operators use
   `journalctl --user -u <project-slug>-agent-work.service`. After an isolated
   preview succeeds, run `systemctl --user enable --now
   <project-slug>-agent-work.timer`. The timer runs 20 minutes after the prior
   service ends. Its random delay is stable. Require linger with `sudo
   loginctl enable-linger "$USER"`. Check `Linger=yes` with `loginctl show-user
   "$USER" --property=Linger`.

   After the wrapper first merges, stop the timer. Manually fast-forward the
   dedicated checkout. Run the isolated preview. Review and apply one exact
   `loop canary --plan-file` checkpoint. Restart the timer. Later applied runs
   check the configured GitLab project. Refresh default branch before Python
   starts. Reject local Git executable/rewrite config. Reject target filter
   attributes. Disable hooks. System/global credential and transport config is
   the operator-managed service boundary.
4. Make no second project request. Do not query membership. Step 1 uses local
   `jq` for the `max` of `.permissions.project_access.access_level` and
   `.permissions.group_access.access_level`. Ignore absent values. Missing
   access is `unknown`. So is nonnumeric access. Permit only 30. `unknown`, less than 30, and
   40 or more block `--apply`.
5. Access 30 is the Developer readiness fast path. Use the constrained service
   identity and relevant `AGENTS.md` standing authority. Use it for unattended
   publication. Do not ask again for each command. In step 1, encoded
   `repository/branches/$default_branch_uri` is authoritative for the effective
   default branch. It must return the exact default-branch name. Boolean
   `default` must be true. `protected` must be true. Boolean `developers_can_push`,
   `developers_can_merge`, and `can_push` must be false. This handles wildcards
   and many protection rules without inference. `approval_rules` needs one
   unambiguously global rule with numeric `approvals_required >= 1`: no
   protected branches, no configured protected branches, or
   `applies_to_all_protected_branches` true. A branch-scoped or malformed rule
   is missing evidence. It blocks `--apply`.

   If a preflight command fails, or evidence is absent or inaccessible, report
   `missing evidence`. Block `--apply`. A Maintainer must fix the named
   safeguard. Then rerun preflight. After all checks pass, report exactly:
   `Effective API access: Developer (30); Maintainer-only approval audit and attestation not required for this readiness policy.`
   At 30, skip only the Maintainer-only GET `/approvals` audit and repeat
   Maintainer attestation. Keep every other identity, publication, checkout,
   origin, and preview check.
6. At 40 or more, use a constrained Developer credential before retrying. This
   readiness path blocks privileged values.
7. This role test shows readiness. It does not prove policy. Developer access
   cannot prove no self-approval or no per-MR approval override. This route
   skips those Maintainer-only checks. It relies on workflow. It relies on settings.
   It relies on runtime never-approve/merge guards. Developers may approve MRs. Project
   settings control pushes. They control merges and self-approval. Git and `glab` creds may
   differ. Keep authority, constrained identity, dedicated checkout, origin
   checks, and preview. Never approve or merge.
8. Before changing checkout, credentials, or GitLab labels, stop the timer:
   `systemctl --user disable --now <project-slug>-agent-work.timer`. Inventory
   old labels, branches, worktrees, and stashes with `uv run agent-work
   migration preview --json`. Apply only the exact reviewed plan. Before timer
   enablement, create, review, and apply one exact non-claiming canary plan. For
   retained work, use exact-IID `loop recover --issue-iid IID --apply --plan-file
   PATH`. Do not use ordinary `loop run` as a recovery canary. A canary may
   propose fixed lifecycle-label setup. While stopped, review that plan. Apply that
   exact plan. Then checkpoint before re-enabling. It assigns no issue labels.

The guarded loop resumes active work first. It then selects new,
open, non-confidential issues with no lifecycle projection, ledger record, or
open split prerequisite. It projects active work as `agent::running`. It uses
`agent::awaiting-human` only with same-project MR evidence, matching local and
remote tips, default target, and `Closes #IID`. Expected human action uses
`agent::blocked`. `agent::failed` is only for exceptional cases. It is only for unrecoverable cases.
Ordinary branches are `agent/issue-<IID>`. Expected durable outcomes exit zero.
Exit one means state could not be recorded safely. Or reconciled safely. Exit two means
bad CLI use. The lock is single-host. It is not a distributed GitLab
compare-and-set claim. Never approve or merge its MR.

## Credential rotation and repair

Do not manually edit `AGENT_WORK_FACTORY_USERNAME` when the configured
directory-scoped GitLab credential changes. Keep the service stopped. Use this
guarded command from the dedicated checkout:

```bash
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --json
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --apply --json
```

The bare command checks non-secret config only. It makes no change. With
`--apply`, it disables and stops the timer and service. It proves both
inactive. It verifies the
directory-scoped credential through the configured checkout and numeric project
route. It changes only a changed username. It does so atomically. Then it runs the
isolated wrapper preview. It never accepts, writes, or renders a token. A
failed post-write preview rolls back exact prior environment bytes once.
Both units stay stopped for retained-record review. They stay stopped for canary and explicit
re-enable.
