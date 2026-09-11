---
title: How to set up the recoverable serial GitLab agent loop
type: how-to
summary: Install, inspect, recover, migrate, and enable one serial agent loop.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-11
related:
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the commands, lifecycle, JSON, and exit contract.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains why the loop uses durable serial recovery.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the state machine and migration rationale.
---

# How to set up the recoverable serial GitLab agent loop

Use one dedicated checkout and one user timer for one GitLab project. The loop
is authorized by enabling the project-level timer. It selects eligible open,
non-confidential issues without a lifecycle projection or ledger record, never
approves or merges, and never applies or drops a recovery stash.

## Check prerequisites and Developer-readable safeguards

Run the loop from the repository it operates on. It confirms Git access,
derives the GitLab host and project from `origin`, resolves the numeric project
endpoint, and stores durable state below `XDG_STATE_HOME` (or the platform
default). Do not print, export, or put tokens in this setup.

Choose `codex` (default), `claude`, or `pi`; verify Git, `glab`, `jq`, and the
selected agent. A stored `glab` login or directory-scoped `GITLAB_TOKEN` is
supported. Git and the API can use separate credentials.

```bash
export AGENT_WORK_AGENT=claude  # Or: pi. Omit for the codex default.
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *) exit 2;; esac
command -v git glab "$agent_name" jq
glab api --hostname "$GITLAB_HOST" user >/dev/null
git pull --ff-only
git remote get-url origin
```

Run `codex login status` or `claude auth status` before enabling `--apply`.
Pi has no equivalent non-secret status command; verify its provider credential
with Pi. The unattended path requires exactly Developer access (`30`) for the
`glab` identity. This is a fail-closed readiness check, not a claim that a role
alone proves repository policy.

### Verify Developer-readable safeguards

Before enabling `--apply`, paste this guarded block. It exits nonzero and
prints actionable missing evidence whenever applied operation must be blocked:

```bash
(
set -Eeuo pipefail

block_apply() {
  printf 'BLOCK --apply: %s\n' "$*" >&2
  exit 1
}

project_json="$(glab api --hostname "$GITLAB_HOST" \
  "projects/$GITLAB_PROJECT_ID")" \
  || block_apply 'missing evidence: cannot read the configured numeric project'
effective_access="$(jq -r '
  [
    .permissions.project_access.access_level,
    .permissions.group_access.access_level
  ]
  | map(select(type == "number"))
  | if length == 0 then "unknown" else max | tostring end
' <<<"$project_json")" \
  || block_apply 'missing evidence: cannot classify effective project/group access'
case "$effective_access" in
  30) ;;
  unknown)
    block_apply 'missing evidence: effective access is unknown; use a constrained Developer credential' ;;
  [0-9]|[12][0-9])
    block_apply 'effective access is less than 30; grant the constrained identity Developer access' ;;
  4[0-9]|[5-9][0-9]|[1-9][0-9][0-9]*)
    block_apply 'effective access is 40 or greater; switch to a constrained Developer credential' ;;
  *)
    block_apply 'effective access is not exactly 30; use the constrained Developer readiness path' ;;
esac

default_branch="$(jq -er '.default_branch | strings | select(length > 0)' \
  <<<"$project_json")" \
  || block_apply 'missing evidence: project has no readable default branch'
default_branch_uri="$(jq -rn --arg value "$default_branch" '$value | @uri')" \
  || block_apply 'missing evidence: cannot encode the default branch for the API'
effective_branch_json="$(glab api --hostname "$GITLAB_HOST" \
  "projects/$GITLAB_PROJECT_ID/repository/branches/$default_branch_uri")" \
  || block_apply 'missing evidence: cannot read the effective default-branch settings'
jq -e --arg default_branch "$default_branch" '
  .name == $default_branch
  and .default == true
  and .protected == true
  and .developers_can_push == false
  and .developers_can_merge == false
  and .can_push == false
' <<<"$effective_branch_json" >/dev/null \
  || block_apply 'missing evidence: effective default branch must be protected, non-pushable, and non-mergeable for Developers'

approval_rules_json="$(glab api --hostname "$GITLAB_HOST" \
  "projects/$GITLAB_PROJECT_ID/approval_rules")" \
  || block_apply 'missing evidence: cannot read approval_rules'
jq -e '
  type == "array"
  and any(
    .[]?;
    ((.approvals_required? // 0) | type == "number" and . >= 1)
    and (
      (has("protected_branches") | not)
      or ((.protected_branches | type == "array") and (.protected_branches | length == 0))
      or (.applies_to_all_protected_branches == true)
    )
  )
' <<<"$approval_rules_json" >/dev/null \
  || block_apply 'missing evidence: require >=1 approval from a global/applies-all approval rule'
printf '%s\n' 'Effective API access: Developer (30); Maintainer-only approval audit and attestation not required for this readiness policy.'
)
```

The policy deliberately skips only Maintainer-only `/approvals` audit and
recurring Maintainer attestation. The observable branch and global approval
checks above must pass; the runtime independently never approves or merges.
The parentheses isolate strict shell options and preserve the block's status.

## Install and inspect

From the dedicated checkout, ensure Git, `glab`, the selected coding agent,
and UV are available. Confirm Git and GitLab API access without exposing a
token, then inspect the recoverable state before enabling a timer:

```bash
git pull --ff-only
uv run agent-work loop status --json
uv run agent-work loop run --json
```

Install the repository's rendered user service and timer using the project's
systemd assets. Create a dedicated service checkout first. Its
`agent/work-base` branch tracks `origin/main`; the FSM creates one durable
issue worktree below the XDG state directory rather than returning this
checkout to an issue branch.

```bash
agent_checkout="$(dirname "$PWD")/$(basename "$PWD")-agent-work"
git worktree add -b agent/work-base "$agent_checkout" origin/main
git -C "$agent_checkout" branch --set-upstream-to=origin/main agent/work-base
cd "$agent_checkout"
command -v uv systemctl loginctl
uv run agent-work loop status --json
```

The service manager does not inherit an interactive `PATH` or checkout `direnv`
variables. Record absolute executable paths and, when used, the absolute
`direnv` path; the wrapper invokes `direnv exec` without writing a token to the
environment file. Set the rendered unit name, install the units, and create a
mode-600 environment file:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
uv_path="$(command -v uv)"
agent_name="${AGENT_WORK_AGENT:-codex}"
agent_path="$(command -v "$agent_name")"
direnv_path="$(command -v direnv || true)"
[[ "$uv_path" == /* && -x "$uv_path" ]] || exit 1
case "$agent_name" in codex|claude|pi) ;; *) exit 1;; esac
[[ "$agent_path" == /* && -x "$agent_path" ]] || exit 1
[[ -z "$direnv_path" || ( "$direnv_path" == /* && -x "$direnv_path" ) ]] || exit 1
install -D -m 0644 "systemd/$unit_name.service" "$HOME/.config/systemd/user/$unit_name.service"
install -D -m 0644 "systemd/$unit_name.timer" "$HOME/.config/systemd/user/$unit_name.timer"
install -d -m 700 "$HOME/.config/$unit_name"
printf 'AGENT_WORK_CHECKOUT=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\nAGENT_WORK_BASE_BRANCH=%q\n' "$PWD" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" agent/work-base >"$HOME/.config/$unit_name/agent-work.env"
chmod 600 "$HOME/.config/$unit_name/agent-work.env"
```

Enable linger for the operator so the user manager survives logout, then reload
and enable the timer:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
systemctl --user status "$unit_name.timer"
```

The timer schedules the next bounded activation 20 minutes after the service
becomes inactive. Within that activation, the runner advances one issue through
immediately available, individually persisted segments. The installed service
allows 90 minutes for an activation. Run only one timer host for a project. The
service's environment and XDG state directory must be writable only by the
operator.
If `systemctl --user` reports `Failed to connect to bus`, stop and repair the
user-manager socket; do not retry using a different credential, host, or
environment. Systemd captures standard output and error under its
project-specific `SyslogIdentifier`; the wrapper does not duplicate output via
`logger`.

### Repair an installed service configuration

Stop the timer before changing its environment file. Re-run the environment
file creation block above, then reload and enable the timer. Do not change
credentials, worktrees, ledger files, branches, or stashes merely to make the
service start.

## Run and inspect an activation

Preview the next action before authorizing mutation. A preview must not change
GitLab, labels, branches, worktrees, ledger records, or systemd state.

```bash
uv run agent-work loop run --json
uv run agent-work loop status --json
```

After a human has completed the repository's unattended-publication readiness
gate, run one applied activation or let the timer do so. The defaults permit up
to 16 segments and 3,600 seconds for the activation, with a 1,800-second limit
for each worker segment. A fresh issue attempts its complete applicable
workflow; a recovered issue resumes at its saved checkpoint and completes the
remaining phases, including any backward rejection route:

```bash
uv run agent-work loop run --apply --json
systemctl --user status "$unit_name.service"
```

Use `--max-segments`, `--max-activation-seconds`, or `--timeout-seconds` to
apply smaller operational bounds. The activation advances only one issue and
stops when it is idle, waiting for external progress or a retry deadline,
reaches a terminal or human-facing state, or exhausts a configured bound. Its
JSON `items` list records segment results chronologically; `segments_run` and
`stop_reason` summarize the activation.

The runner caps each worker timeout to the activation time that remains. If a
worker exhausts that effective timeout, inspect the recorded retry or recovery
outcome in the same way as any other worker timeout.

Use `--debug` when the redacted status needs local diagnostic context. Do not
copy debug output containing private operational details into an issue.

## Retry or recover running work

First inspect the recorded state and next action. A retry is appropriate only
for the named blocked item after explicit human direction; it resets that
item's retry budget and returns it to resumable preparation.

```bash
uv run agent-work loop status --json
uv run agent-work loop retry --issue-iid IID --json
uv run agent-work loop retry --issue-iid IID --apply --json
```

The initial attempt plus three retries are automatic bounded segments. An
activation yields when a retry deadline is in the future. When retries are
exhausted, the issue is `agent::blocked`; follow its recorded next action, then
explicitly retry that exact IID after a human has resolved the condition. Do
not change labels to resume blocked work.
Leave a dirty durable worktree in place. If status inventories a stash,
preserve it for review: do not use `git stash apply` or `git stash drop` as an
automated recovery step.

## Migrate existing loop evidence

Stop the timer and ensure no service segment is active before migration. The
preview inventories running, awaiting-human, failed, blocked, and closed GitLab
projections; exact branch, worktree, and stash evidence; and the resulting
safe ledger actions without modifying them.

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
systemctl --user disable --now "$unit_name.timer"
systemctl --user status "$unit_name.service"
uv run agent-work migration preview --json >agent-work-migration-plan.json
```

Review every proposed record, label transition, retained artifact, and blocked
decision. Do not apply a preview with ambiguous evidence. Apply refuses a
stale or tampered plan, changed inventory, a different project, or an existing
ledger record; it never overwrites that evidence. After human review, apply
the idempotent migration and inspect the result:

```bash
uv run agent-work migration apply --plan-file agent-work-migration-plan.json --apply --json
uv run agent-work loop status --json
```

## Canary and re-enable

Choose one open, non-confidential issue with no `agent::running`,
`agent::awaiting-human`, `agent::blocked`, or `agent::failed` label and no
ledger record. Verify that its request is suitable for the normal repository
workflow, then run bounded activations until status reports
`agent::awaiting-human` with exact merge-request evidence and all required
receipts. Human review, approval, and merge remain outside the loop.

```bash
uv run agent-work loop run --apply --json
uv run agent-work loop status --json
systemctl --user enable --now <project-slug>-agent-work.timer
```

If the canary blocks or fails, keep the timer disabled, retain all worktrees
and stashes, and act on the redacted `next_action` from status. See the
[reference](../reference/serial-gitlab-agent-loop.md) for exit meanings and the
complete command contract.

## Monitor, restart, or stop the loop

Inspect the timer, service, and redacted state together. Systemd owns service
logs through the unit's `SyslogIdentifier`; the application also retains its
local lifecycle log as `agent-work.log` in the XDG project-state directory.
Inspect both evidence sources; neither replaces the other.

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
journalctl --user -u "$unit_name.service" --since today
systemctl --user status "$unit_name.timer" "$unit_name.service"
uv run agent-work loop status --json
```

To restart after a transient user-manager failure, let an active bounded
activation finish when possible. Stopping it can interrupt a segment; the
ledger and durable worktree make the next `loop run` reconcile the last durable
boundary.

```bash
systemctl --user stop "$unit_name.timer" "$unit_name.service"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
systemctl --user start "$unit_name.service"
```

To stop future unattended work, disable the timer and stop an active service.
This preserves the ledger, worktrees, branches, stashes, environment file, and
journal for inventory and review.

```bash
systemctl --user disable --now "$unit_name.timer"
systemctl --user stop "$unit_name.service"
systemctl --user show "$unit_name.timer" "$unit_name.service" \
  --property=UnitFileState --property=ActiveState
uv run agent-work loop status --json
```

The timer must report `UnitFileState=disabled` and `ActiveState=inactive`
before migration or a manual evidence review. If `journalctl --user` access is
denied, ask the host administrator for the appropriate journal access; do not
change journald storage settings or use another credential route.
