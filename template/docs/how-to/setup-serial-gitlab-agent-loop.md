---
title: How to set up the recoverable serial GitLab agent loop
type: how-to
summary: Install, inspect, plan recovery, and enable one serial agent loop.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-17
related:
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the commands, lifecycle, JSON, and exit contract.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains why the loop uses durable serial recovery.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the state machine and migration rationale.
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

# How to set up the recoverable serial GitLab agent loop

The installed service may process consecutive terminally handed-off issues in
one invocation. Each issue still has its own bounded activation; empty queues
and durable retry waits yield back to the 20-minute timer. An applied activation
waits for nonterminal pipeline evidence in place.

Use one dedicated checkout and one user timer for one GitLab project. The loop
is authorized by enabling the project-level timer. It selects eligible open,
non-confidential issues without a lifecycle projection or ledger record, never
approves or merges, and never applies or drops a recovery stash.

Before enabling the loop, ensure setup provisions the scoped GitLab priority
labels `agent-priority::high` and `agent-priority::low`, plus lifecycle labels
`agent::needs_repair` (`#D4A72C`), `agent::repair_failed` (`#C91C00`), and
`agent::suggest_close` (`#6F42C1`). Use plain descriptions matching the
reference meanings; do not repurpose these labels for a local workflow. For
fresh eligible work, the loop orders exact high, normal (no recognized exact
priority value), then exact low; within each group it uses absolute creation
time and IID. Apply high to expedite an issue, low to defer it, or remove
either label to return it to normal ordering. Active/resumable work and all
eligibility checks still take precedence. Do not retroactively label existing
issues.

The loop creates and maintains `agent::prereq_<IID>` labels only when a
validated split child has an open trusted prerequisite. These labels are an
operator-visible projection, not a way to control the queue. Do not add,
remove, or use them to bypass a dependency: the split markers and the live
state of prerequisite issues remain authoritative.

## Check prerequisites and Developer-readable safeguards

Run the loop from the repository it operates on. It uses the configured GitLab
host and numeric project ID, confirms Git and API access independently, and
stores durable state below `XDG_STATE_HOME` (or the platform default). Do not
print, export, or put tokens in this setup.

Choose `codex` (default), `claude`, or `pi`; verify Git, `glab`, `jq`, and the
selected agent. Configure a directory-scoped `GITLAB_TOKEN` that can access
the configured project. Require a non-empty `GITLAB_HOST` and positive numeric
`GITLAB_PROJECT_ID`. The API and Git checks are separate: the same token must
pass both, but neither check substitutes for the other.

```bash
export AGENT_WORK_AGENT=claude  # Or: pi. Omit for the codex default.
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *) exit 2;; esac
command -v git glab "$agent_name" jq
direnv exec . /usr/bin/env bash -c 'exec glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"' >/dev/null
git remote get-url origin
```

The final setup route gives Git the token only when it asks the repository
credential-protocol helper for HTTPS credentials. Do not place a token or a
derived Basic-authentication header in `origin`, Git configuration, the service
environment file, a command line, a diagnostic, a ledger, or another retained
record. Keep `origin` as a credential-free HTTPS URL. The service and ordinary
interactive publication paths use the same helper; inherited credential
helpers and related ambient Git configuration cannot override it, and prompts
remain disabled.

Before enabling the timer, run the supported non-mutating Git readiness check
from the [Git Flow preflight](../../.agents/skills/git-flow/SKILL.md), separately
from the numeric-project API check above. Both checks must pass before a remote
mutation. Missing configuration stops setup before that mutation. Treat an
invalid or expired token as an authentication failure; treat a token that
authenticates but cannot push as an authorization failure. Only a known
temporary transport failure is retryable. Preserve only the safe classification
in reports; never paste command output or credential material into an issue,
log, or ledger.

This guide intentionally does not introduce a new operator CLI or a
token-bearing command.

Run `codex login status` or `claude auth status` before enabling `--apply`.
Pi has no equivalent non-secret status command; verify its provider credential
with Pi. The unattended path requires exactly Developer access (`30`) for the
`glab` identity. This is a fail-closed readiness check, not a claim that a role
alone proves repository policy.

For Codex, also verify that `codex exec --help` advertises `--add-dir` before
enabling `--apply`. The worker grants only the prepared issue checkout's
`.agents` and `template/.agents` directories through that option, allowing the
approved workflow policy while keeping the writable scope bounded. Upgrade
Codex or select another supported agent if the option is absent.

```bash
codex exec --help | grep -Eq '(^|[[:space:]])--add-dir([[:space:]]|$)'
```

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

## Preview an upstream finding

Validate a supplied upstream-finding manifest against an explicit upstream
target using a dedicated credential file. The command is read-only: it performs
GET-only checks: no issue is posted and it never changes the checkout.

```bash
uv run agent-work upstream-finding preview --manifest-file finding.yaml \
  --upstream-host gitlab.example.com --upstream-project-id 123 \
  --upstream-credential-file /secure/upstream-token --json
```

See the complete syntax and exit behavior in the
[reference](../reference/serial-gitlab-agent-loop.md).

## Install and inspect

From the dedicated checkout, ensure Git, `glab`, the selected coding agent,
and UV are available. Confirm Git and GitLab API access without exposing a
token, then inspect the recoverable state before enabling a timer:

```bash
# Run the command-scoped credential-helper Git readiness check above.
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
uv run agent-work loop canary --json
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
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
```

The service manager does not inherit an interactive `PATH` or checkout `direnv`
variables. The environment file records the configured bare GitLab host and
positive numeric project ID, but never a token. Use `direnv` only when this
dedicated checkout has its own regular `.envrc`; the wrapper refuses any other
configured `direnv` route. It runs `direnv` only to obtain directory-scoped
credentials and then restores the configured host and project ID. Record
absolute executable paths, install the units, and create a mode-600 environment
file:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
uv_path="$(command -v uv)"
git_path="$(command -v git)"
glab_path="$(command -v glab)"
jq_path="$(command -v jq)"
flock_path="$(command -v flock)"
timeout_path="$(command -v timeout)"
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
[[ "$git_path" == /* && -x "$git_path" && "$glab_path" == /* && -x "$glab_path" && "$jq_path" == /* && -x "$jq_path" && "$flock_path" == /* && -x "$flock_path" && "$timeout_path" == /* && -x "$timeout_path" && "$uv_path" == /* && -x "$uv_path" ]] || exit 1
case "$agent_name" in codex|claude|pi) ;; *) exit 1;; esac
[[ "$agent_path" == /* && -x "$agent_path" ]] || exit 1
install -D -m 0644 "systemd/$unit_name.service" "$HOME/.config/systemd/user/$unit_name.service"
install -D -m 0644 "systemd/$unit_name.timer" "$HOME/.config/systemd/user/$unit_name.timer"
install -d -m 700 "$HOME/.config/$unit_name"
printf 'AGENT_WORK_ENV_VERSION=%q\nAGENT_WORK_CHECKOUT=%q\nAGENT_WORK_GIT=%q\nAGENT_WORK_GLAB=%q\nAGENT_WORK_JQ=%q\nAGENT_WORK_FLOCK=%q\nAGENT_WORK_TIMEOUT=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\nAGENT_WORK_BASE_BRANCH=%q\nAGENT_WORK_FACTORY_USERNAME=%q\nGITLAB_HOST=%q\nGITLAB_PROJECT_ID=%q\n' 1 "$PWD" "$git_path" "$glab_path" "$jq_path" "$flock_path" "$timeout_path" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" agent/work-base "$factory_username" "$gitlab_host" "$gitlab_project_id" >"$HOME/.config/$unit_name/agent-work.env"
chmod 600 "$HOME/.config/$unit_name/agent-work.env"

env -i HOME="$HOME" PATH="$PATH" XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}" XDG_STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}" AGENT_WORK_ENV_VERSION=1 \
  AGENT_WORK_CHECKOUT="$PWD" AGENT_WORK_GIT="$git_path" AGENT_WORK_GLAB="$glab_path" AGENT_WORK_JQ="$jq_path" AGENT_WORK_FLOCK="$flock_path" AGENT_WORK_TIMEOUT="$timeout_path" AGENT_WORK_UV="$uv_path" AGENT_WORK_AGENT="$agent_name" \
  AGENT_WORK_AGENT_PATH="$agent_path" AGENT_WORK_DIRENV="$direnv_path" \
  AGENT_WORK_BASE_BRANCH=agent/work-base AGENT_WORK_FACTORY_USERNAME="$factory_username" GITLAB_HOST="$gitlab_host" \
  GITLAB_PROJECT_ID="$gitlab_project_id" \
  /usr/bin/env bash "$PWD/scripts/agent-work-systemd-run.sh"
```

This isolated preview validates the exact service wrapper and credential route
without inheriting an interactive GitLab identity. It must succeed before
enabling the timer. Enable linger for the operator so the user manager survives
logout, then reload and enable the timer:

Initial installation writes the factory-managed service-environment contract at
version `1`. Its deterministic required keys include `AGENT_WORK_TIMEOUT`.
Keep project-owned `AGENT_WORK_AGENT`, `AGENT_WORK_BASE_BRANCH`,
`AGENT_WORK_FACTORY_USERNAME`, `GITLAB_HOST`, and `GITLAB_PROJECT_ID` under
operator control. A later repository upgrade can detect an older, incomplete,
contract; a missing file requires the supported setup procedure to recreate it.

After this wrapper change first merges, stop the timer, manually fast-forward
the dedicated checkout once, run this preview (and any canary), then restart
the timer. Later applied activations refresh the verified default branch before
they invoke Python. Local executable or rewrite-capable Git configuration and
target filter attributes are rejected; repository hooks are disabled for the
refresh. System/global transport and credential configuration is the
operator-managed service boundary.

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
systemctl --user status "$unit_name.timer"
```

The timer schedules the next bounded activation 20 minutes after the service
becomes inactive, to check for newly arrived work only when the factory is
genuinely idle with no eligible or resumable work. It is not the cadence for an
active pipeline wait. Within an activation, one worker owns the issue's complete
workflow session; its Architect, Red, Green, and read-only judges hand context
directly to one another. The runner recovers only the durable worktree and
coarse `WORKFLOW` state, not a sidecar checkpoint for each internal role. The
service has a 190-minute outer timeout: the approved 180-minute worker budget
plus a 10-minute margin for wrapper checkout, GitLab, publication, and
reporting work. Other wrapper housekeeping calls have a 120-second deadline,
while the applied worker remains governed by its remaining 10,800-second
budget. On expiry systemd terminates the whole control group, gives it 30
seconds to exit, and records the timeout in the journal; the durable ledger
and issue worktree remain for normal retry or human diagnosis. Run only one
timer host for a project. The service's environment and XDG state directory
must be writable only by the operator.
If `systemctl --user` reports `Failed to connect to bus`, stop and repair the
user-manager socket; do not retry using a different credential, host, or
environment. Systemd captures standard output and error under its
project-specific `SyslogIdentifier`; the wrapper does not duplicate output via
`logger`.

### Establish a validated user-bus boundary

The runtime applies this boundary before every executable
`systemctl --user` command: it preserves valid explicit
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` values. Only when either key
is absent, derive `XDG_RUNTIME_DIR=/run/user/<effective-uid>` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<effective-uid>/bus`. The
boundary then requires an absolute runtime directory with the effective user's
ownership and an absolute existing bus socket owned by that user. It fails
closed if any check fails; do not substitute another host, credential, or bus
address.

Credential rotation and service-environment migration use that same
boundary. Their isolated previews run the intentionally mode-`0644`
wrapper with argv,
not a shell command: `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh`.
Service-environment migration adds `--validate-service-environment`. Do not
add an executable bit to the wrapper.

### Repair an installed service configuration

Stop the timer before changing its environment file. Re-run the environment
file creation block above, then reload and enable the timer. Do not change
credentials, worktrees, ledger files, branches, or stashes merely to make the
service start.

### Migrate a service environment after a repository upgrade

Before enabling an upgraded timer, migrate its installed environment file. The
bare command is a non-mutating, non-claiming validation and preview: it does
not select an issue, create a ledger record, or run normal loop work. It
compares the installed contract with the current version and required keys.

Stop both units first, then review the dry-run result. Replace the paths and
unit name with the installed values.

```bash
environment_file="$HOME/.config/$unit_name/agent-work.env"
systemctl --user disable --now "$unit_name.timer"
systemctl --user stop "$unit_name.service"
systemctl --user is-active --quiet "$unit_name.timer" && exit 1
systemctl --user is-active --quiet "$unit_name.service" && exit 1
uv run agent-work service-environment migrate \
  --environment-file "$environment_file" --unit-name "$unit_name" --dry-run --json
```

Review the redacted preview. If it reports a stale or missing factory-managed
contract, apply the same reviewed operation. Migration validates the existing
configured Developer identity, configured project, default-branch protection,
and approval safeguards through the checkout's `direnv` and password-store
credential route. It does not accept, print, or rebind a credential.

```bash
uv run agent-work service-environment migrate \
  --environment-file "$environment_file" --unit-name "$unit_name" \
  --apply --json
```

Applied migration regenerates only the factory-managed contract version,
checkout, and tool-path fields; it preserves the five project-owned values
listed above. It atomically replaces the mode-600 file and runs an isolated,
non-claiming validation. Both units remain stopped for human review. If a
pre-write check fails, the file is unchanged. If a later step fails, retain the
stopped units and use the command's sanitized error to restore or inspect the
byte-recoverable prior file; do not hand-edit the contract to bypass a failed
check.

After a successful migration, review the result, run the normal canary
checkpoint, and explicitly re-enable the timer only after the canary is
understood. Migration never enables or starts either unit.

### Rotate the directory-scoped GitLab credential

Use the credential-rotation command after the directory-scoped credential used
by the installed wrapper changes ownership. It updates only the configured
`AGENT_WORK_FACTORY_USERNAME`; the credential remains in its existing
directory-scoped route and is never a command argument, environment-file
value, diagnostic, or log entry.

First stop unattended work and inspect the dry-run result. Supply the exact
mode-600 service environment file and the installed unit name. The dry run
does not write the file and leaves the timer and service stopped.

```bash
systemctl --user disable --now "$unit_name.timer"
systemctl --user stop "$unit_name.service"
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --json
```

After reviewing the redacted result, apply the same command. It checks the
configured bare host and numeric project through the newly configured
credential, requires exactly Developer access, strict effective default-branch
protection, and at least one global approval rule before it changes the
expected username entry atomically.

```bash
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --apply --json
```

Do not re-enable the timer as part of credential rotation. On success, inspect
the redacted result, perform the normal explicit operator review and bounded
canary in [Canary and re-enable](#canary-and-re-enable), then re-enable the
timer only when that review succeeds. Ordered canary execution and retained
record recovery are outside this command.

If the command reports an invalid, expired, unauthorized, or mismatched
credential, leave the timer and service stopped. The environment file is
byte-identical unless all preconditions pass. If the bounded post-update
wrapper preview fails, the command makes one exact-byte rollback attempt and
returns a sanitized corrective handoff. If rollback cannot be confirmed, it
reports configuration uncertainty for human correction. Preserve the stopped services and the
environment file, act on that handoff, and rerun a dry run after correcting the
directory-scoped credential; do not manually edit the username to bypass it.

## Run and inspect an activation

Preview the next action before authorizing mutation. A preview must not change
GitLab, labels, branches, worktrees, ledger records, or systemd state.

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop canary --json
uv run agent-work loop status --json
```

After a human has completed the repository's unattended-publication readiness
gate, run one applied activation or let the timer do so. The defaults permit up
to 16 workflow attempts within one 10,800-second (three-hour) overall work
budget. Each workflow invocation receives the positive, ceil-rounded time
remaining from that budget. A fresh issue attempts its complete applicable
workflow; a recovered issue resumes from its durable worktree and coarse
`WORKFLOW` state. Existing legacy phase-ledger values
finish their recorded route; internal Architect, Red, Green, and judge handoffs are not
separately persisted:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop run --apply --json
systemctl --user status "$unit_name.service"
```

Use `--max-segments` or `--max-activation-seconds` to apply smaller operational
bounds. The activation advances only one issue. With `--apply`, it polls
nonterminal default-branch or exact current-MR pipeline evidence in the same
activation after 5, 10, then 20 seconds, capped at 30 seconds, and resumes when
the evidence becomes terminal. A future retry deadline, a human or safety
block, missing, failed, or unrecognized evidence, an idle/no-eligible-or-
resumable-work result, or an exhausted bound remains a handoff. Preview is
read-only and immediate: it does not poll. The JSON `items` list records
workflow results chronologically; `segments_run` and `stop_reason` summarize
the activation.

If the overall budget is exhausted, inspect the recorded retry or
durable-worktree recovery outcome in the same way as any other timeout.

Use `--debug` when the redacted status needs local diagnostic context. Do not
copy debug output containing private operational details into an issue.

## Retry or recover retained work

First inspect the recorded state and next action. A retry is appropriate only
for the named blocked item after explicit human direction; it resets that
item's retry budget and returns it to resumable preparation.

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
uv run agent-work loop retry --issue-iid IID --json
uv run agent-work loop retry --issue-iid IID --apply --json
```

The initial attempt plus three retries are automatic workflow attempts. An
activation yields when a retry deadline is in the future. When retries are
exhausted, the issue is `agent::blocked`; follow its recorded next action, then
explicitly retry that exact IID after a human has resolved the condition. Do
not change labels to resume blocked work.
Leave a dirty durable worktree in place. If status inventories a stash,
preserve it for review: do not use `git stash apply` or `git stash drop` as an
automated recovery step.

An exact failed current-source merge-request pipeline with one uniquely
corroborated retained owner repairs automatically. Keep the timer enabled, or
run one ordinary applied activation; the loop runs one eligible repair workflow
without a reviewed checkpoint:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop run --apply --json
```

Do not replace this with a broad label edit or a manual recovery command. An
unsuccessful automatic repair records a 20-minute backoff for that item. The
next activation can select another eligible exact repair or ordinary work; it
does not stop the whole factory for this item. After three unsuccessful
automatic repairs, the item is quarantined or deferred for human review. Use
`loop status --issue-iid IID --json` or the non-mutating preview to inspect its
attempt count, backoff or quarantine, exact evidence, and next action.

Use targeted `loop recover` only for interrupted, ambiguous, or otherwise
unsafe retained work. It is an exceptional reviewed path for one exact IID
with live corroboration; it does not spend or reset the automatic repair
budget. Ambiguous or inexact identity, active ownership, credential, policy,
or authority conflicts remain fail-closed. Neither issue nor merge-request
text can set repair priority, the budget, or the backoff. The loop never
approves or merges. A blocked record can likewise be reclassified only one IID
at a time with live corroboration.

An `agent::suggest_close` label is a structured recommendation for human
closure, never an automated close. Review its duplicate, superseded,
already-satisfied, or no-longer-applicable evidence. Only a human may close the
issue; that closure reconciles the record to `COMPLETE` and clears the label.
The loop never approves, merges, or closes issues.

### Observe an automated `split_required` decomposition

When an Architect returns `result_kind=split_required`, the trusted runner
handles the validated plan in the current executing segment. It searches for
each exact child marker, reuses a matching child, and creates only missing
children. Each new child has a full description with links to the parent and
its dependencies. Once every child exists, the controller adds one parent note
and closes the parent.

If that handling is interrupted, the next attempt reruns the workflow and the
same marker searches recover children already created. There is no separate
decomposition state, saved plan or progress record, checkpoint restoration, or
special queue to repair.

For an open child, the runner projects every open trusted prerequisite as one
`agent::prereq_<IID>` label, ordered by numeric IID. It removes a projection
when the prerequisite closes, the dependency is removed from the trusted
marker, or the child closes. Preview `loop run` and `recovery inventory` show
the current and intended labels without writing them. An applied `loop run`
repairs missing or stale labels before normal selection. If it repairs any
projection drift, that pass does only the repair and does not claim work,
create a ledger record or worktree, or start a worker. A later run resumes
normal selection.

Let each approved child issue enter the normal eligibility queue and produce
its own merge request in dependency order. Leave a tracking parent blocked or
close it according to the project's issue practice. If the parent itself was
narrowed to a suitable outcome, explicitly retry that IID only after the edit
is complete:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop retry --issue-iid IID --json
uv run agent-work loop retry --issue-iid IID --apply --json
```

Do not remove lifecycle or prerequisite-projection labels by hand or increase
worker budgets to bypass the sizing decision. The explicit retry preserves the
durable recovery path;
the next worker session reassesses the revised scope before mutation.

## Migrate existing loop evidence

Stop the timer and ensure no service segment is active before migration. The
preview inventories running, awaiting-human, failed, blocked, and closed GitLab
projections; exact branch, worktree, and stash evidence; and the resulting
safe ledger actions without modifying them.

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
systemctl --user disable --now "$unit_name.timer"
systemctl --user status "$unit_name.service"
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work migration preview --json >agent-work-migration-plan.json
```

Review every proposed record, label transition, retained artifact, and blocked
decision. Do not apply a preview with ambiguous evidence. Apply refuses a
stale or tampered plan, changed inventory, a different project, or an existing
ledger record; it never overwrites that evidence. After human review, apply
the idempotent migration and inspect the result:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work migration apply --plan-file agent-work-migration-plan.json --apply --json
uv run agent-work loop status --json
```

## Plan a canary checkpoint and targeted recovery

Keep the timer disabled and service inactive while preparing recovery. A
canary is a non-claiming operational checkpoint: it verifies public identity,
durable recovery evidence, and pipelines; it does not choose or mutate an
issue. Create and review its plan, then apply that exact plan while the timer
remains stopped:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop canary --json >agent-work-canary-plan.json
# Review the redacted plan and its expected checkpoint/next action.
uv run agent-work loop canary --apply --plan-file agent-work-canary-plan.json --json
uv run agent-work loop status --json
```

If the preview reports lifecycle-label provisioning, review that deterministic
label-spec action and apply it with the same stopped-timer sequence above.
The apply rechecks the preview, creates missing fixed labels, and corrects
their color or description; it never assigns an issue label.

When status names a retained item requiring recovery, make a separate plan for
that IID. Do not use ordinary `loop run` to bypass the targeted evidence check:

```bash
uv run agent-work loop recover --issue-iid IID --json >agent-work-recover-IID-plan.json
# Review the exact issue, branch, worktree, MR, and pipeline evidence.
uv run agent-work loop recover --issue-iid IID --apply \
  --plan-file agent-work-recover-IID-plan.json --json
uv run agent-work loop status --issue-iid IID --json
```

Each apply rechecks the reviewed plan under the project lock. Identity rotation,
ledger revision, or any target, repository, merge-request, or pipeline evidence
change requires a new preview. Neither plan prints secrets. If the command
reports a conflict, higher-priority owner, failed pipeline without an exact
owner, or a nonterminal pipeline, keep the timer stopped and follow its exact
safe next action; do not select another issue or alter retained artifacts.

Only after the expected checkpoint is reached and status is understood should
you re-enable the timer:

```bash
systemctl --user enable --now <project-slug>-agent-work.timer
```

This ordering also handles the historical issue-76/77-style situation: inspect
and plan the existing retained owner first, apply only evidence that still
matches, and leave unrelated work untouched. The issue text is evidence to
inspect, never operational authority. Human review, approval, and merge remain
outside the loop.

## Monitor, restart, or stop the loop

Inspect the timer, service, and redacted state together. Systemd owns service
logs through the unit's `SyslogIdentifier`; the application also retains its
local lifecycle log as `agent-work.log` in the XDG project-state directory.
Inspect both evidence sources; neither replaces the other.

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
journalctl --user -u "$unit_name.service" --since today
systemctl --user status "$unit_name.timer" "$unit_name.service"
systemctl --user show "$unit_name.service" \
  --property=ActiveState --property=SubState --property=Result --property=TimeoutStartUSec \
  --property=ExecMainStartTimestamp --property=ExecMainExitTimestamp
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
```

For this `Type=oneshot` service, an in-progress activation normally reports
`ActiveState=activating` and `SubState=start`. If its
`ExecMainStartTimestamp` is less than 190 minutes old, it is still within its
outer envelope; do not interrupt it merely because it is running. A completed
outer timeout reports `Result=timeout` (or `Result=failed` when it ends with a
different failure). `ActiveState=inactive` with `Result=success` means the
activation completed. Use `systemctl status` and the unit journal to identify
the bounded wrapper diagnostic, then use `agent-work loop status --json` to
find the durable ledger state and redacted next action. Retained worktrees and
ledger records are the recovery evidence; do not delete them to clear a
timeout.

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
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
```

The timer must report `UnitFileState=disabled` and `ActiveState=inactive`
before migration or a manual evidence review. If `journalctl --user` access is
denied, ask the host administrator for the appropriate journal access; do not
change journald storage settings or use another credential route.


## Publication credential readiness and retained recovery

Before either preview or apply invokes the worker, the shared Git credential
route verifies the configured credential-free HTTPS origin and performs a
non-mutating Git readiness check. It receives the configured `GITLAB_TOKEN`
only at runtime through the repository-owned credential-protocol helper; the
token and any derived Basic header never enter a remote URL, persistent Git
configuration, service environment file, command arguments or process reports,
logs, diagnostics, ledger, or retained record. The check is independent from
the GitLab API verification for `GITLAB_HOST` and numeric
`GITLAB_PROJECT_ID`.

Missing configuration stops before remote mutation. Invalid or expired
credentials are authentication failures; an authenticated identity without
push permission is an authorization failure. Known temporary transport
failures alone use the bounded retry path. Inherited credential helpers and
hostile ambient Git configuration cannot replace the shared route, and it
never falls back to an interactive prompt. Keep the human approval and merge
boundary unchanged.

For retained publication recovery, first inspect status, the source branch, local `HEAD`, recorded source SHA, and the exact pending publication intent. Run preview, then explicitly run `agent-work loop retry --issue-iid IID --apply`. A retained publish intent moves directly back to publishing: it keeps the same branch, SHA, draft evidence, and intent, and replays the clean committed head without a second commit or merge request. Do not copy credentials or command output into the ledger or issue.
