---
title: How to set up the recoverable serial GitLab agent loop
type: how-to
summary: Set up a recoverable serial agent loop.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-10-02
related:
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the commands, lifecycle, JSON, and exit contract.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains why the loop uses durable serial recovery.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the state machine and migration rationale.
  - path: update-a-generated-project.md
    why: Describes the separate opt-in template-update timer and its preview default.
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

This guide configures the loop.
Follow it in order.
Read each step before you act.
Use the safe route.
Stop if unsure.
Check the saved state.
Keep the timer stopped.
Wait for clear proof.
Keep proof.
Stop.
Use one clear plan.

Router chooses the lane and checks capacity. It delegates work, leads fixes,
and makes final decisions. Architect is its independent read-only design child.

Before using the service or enabling its timer, check the installed worker
prompt. If it still tells the legacy workflow Architect to combine proposals
and own the whole workflow, stop. Return the conflict to
Router/Main. Resume only after an independently reviewed executable migration.
The service behavior below does not prove that migration. It does not authorize
startup with an incompatible prompt.

The installed service can process consecutive terminally handed-off issues in
one run. It also checks for ready work after it fixes prerequisite labels.
Each issue has its own bounded run. Empty queues and saved retry waits return
to the 20-minute timer. If the default-branch pipeline is valid but still
active, the run checks exact MRs and may choose new work. The timer
checks the pipeline again. A new exact MR observer moves from `PUBLISHING` to `OBSERVING`, then returns
`activation_complete/pipeline_wait`
without polling. The wrapper previews once. Later, it parks a retained observer
only when its SHA, owner, worktree, project, and target are exact. That parking
can start another item or yield idle to the timer.

Use one set checkout and one user timer for one GitLab project. Starting the
project timer authorizes the loop. It selects only ready issues. They must be
open and non-confidential. They must have no lifecycle projection or ledger
record. The loop never approves or merges. It never applies or drops a restore
stash.

Before starting the loop, ensure setup adds the scoped GitLab priority labels
`agent-priority::high` and `agent-priority::low`. Also add the manual-tracker
marker `agent-work::manual` and lifecycle labels `agent::needs_repair`
(`#D4A72C`), `agent::repair_failed` (`#C91C00`), and
`agent::suggest_close` (`#6F42C1`).
Use their set definitions and reference meanings. Do not reuse these labels for
a local flow. For fresh ready work, the loop orders exact high, normal (no
known exact priority value), then exact low. Each group uses absolute creation
time and IID. Apply high to speed an issue or low to defer it. Remove either
label to return to normal order. Active or resumable work and all ready checks
come first. Do not label old issues after the fact.

To keep an open issue as a human-run tracker or restore checklist, add the
exact `agent-work::manual` label in the GitLab UI. The loop skips that issue in
fresh selection. This is not an `agent::…` lifecycle label. It creates no
ledger record or worktree, does not affect prerequisite work, and is not input
to orphan reconciliation. Do not use it for an error or lifecycle state, or to
override a prerequisite. Remove the exact label when the tracker is ready for
normal work. An otherwise ready open issue then returns to fresh selection.

The loop creates and keeps `agent-prereq_<IID>` labels only when a checked split
child has an open trusted need. A need can be a direct prerequisite or an open
child or descendant that must finish for a closed, decomposed prerequisite.
These non-scoped labels let one child show several open needs at once. They are
a user-visible projection, not a queue control. Do not add, remove, or use them
to bypass a dependency. Validated split markers and one complete snapshot of
live issue state remain the source of truth.

## Check needs and Developer-readable guards.

Run the loop from its target repo. It uses the set GitLab host and numeric
project ID. It checks Git and API access on their own. It stores saved state
below `XDG_STATE_HOME` (or the platform default). Do not print, export, or put
tokens in this setup.

Choose `codex` (default), `claude`, or `pi`. Check Git, `glab`, `jq`, and the
chosen agent. Set a dir-scoped `GITLAB_TOKEN` that can access the set project.
Require a non-empty `GITLAB_HOST` and positive numeric `GITLAB_PROJECT_ID`.
The API and Git checks are separate. The same token must pass both. Neither
check replaces the other.

```bash
export AGENT_WORK_AGENT=claude  # Or: pi. Omit for the codex default.
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *) exit 2;; esac
command -v git glab "$agent_name" jq
direnv exec . /usr/bin/env bash -c 'exec glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"' >/dev/null
git remote get-url origin
```

The final setup route gives Git the token only when it requests HTTPS
credentials from the repository credential-protocol helper. Do not place a
token or derived Basic-authentication header in `origin` or Git configuration.
Do not place either in the service environment file, a command line,
diagnostic, ledger, or retained record. Keep `origin` as a credential-free
HTTPS URL. Service and ordinary interactive publication use the same helper.
Inherited credential helpers and related ambient Git configuration cannot
override it. Prompts stay disabled.

Before enabling the timer, run the supported non-mutating Git readiness check
from the [Git Flow preflight](../../.agents/skills/git-flow/SKILL.md). Run it
separately from the numeric-project API check above. Both must pass before a
remote mutation. Missing configuration blocks setup first. Treat an invalid or
expired token as an authentication failure. Treat a token that authenticates
but cannot push as an authorization failure. Retry only a known temporary transport
failure. Report only the safe classification; never paste command output or
credential material into an issue, log, or ledger.

This guide does not introduce an operator CLI or token-bearing command.

Before enabling `--apply`, run `codex login status` or `claude auth status`.
Pi has no equivalent non-secret status command; verify its provider credential
with Pi. The unattended path requires exactly Developer access (`30`) for the
`glab` identity. This fail-closed readiness check does not claim that a role
alone proves repository policy.

For Codex, verify that `codex exec --help` advertises both `--add-dir` and
`--enable` before enabling `--apply`. Also confirm that
`codex features list` reports exactly `multi_agent stable true`. The worker
starts Codex with `--enable multi_agent` for delegation. Its legacy Architect
workflow prompt must pass the compatibility check above. It grants the
prepared issue checkout's `.agents`
and `template/.agents` directories through `--add-dir`. During one invocation,
it also grants a fresh private broker directory. The producer writes its bounded
input there. It then connects to the controller-owned socket. No controller
state directory is granted.

If the `multi_agent` feature is unavailable, the controller gate stops before
claiming new work and retains an existing record for retry. Upgrade Codex or
select another supported agent, then run the loop again after the capability is
available. The gate reports `codex multi-agent capability unavailable` when
the feature check fails.

The worker cannot write the linked checkout's Git data. It asks the controller
to stage changed files through a private socket. The controller then validates
the staged tree. Do not give the worker access to the shared Git directory for
`git add`, `git fetch`, or `git pull`. If staging or validation fails, inspect
the saved worktree and controller evidence before retrying.

```bash
codex exec --help | grep -Eq '(^|[[:space:]])--add-dir([[:space:]]|$)'
codex exec --help | grep -Eq '(^|[[:space:]])--enable([[:space:]]|$)'
feature_output="$(codex features list)" &&
  test "$(grep -Ec '^[[:space:]]*multi_agent([[:space:]]|$)' <<<"$feature_output")" = 1 &&
  grep -Eq '^[[:space:]]*multi_agent[[:space:]]+stable[[:space:]]+true[[:space:]]*$' \
    <<<"$feature_output"
```

### Verify Developer-readable safeguards.

Before enabling `--apply`, run this guarded block. It exits nonzero and prints
actionable missing evidence whenever an applied operation must be blocked:

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

The policy skips only the Maintainer-only `/approvals` audit and repeat
Maintainer attestation. The seen branch and global approval checks above must
pass. The runtime never approves or merges. The parentheses keep strict shell
options apart and keep the block's status.

## Preview or file an upstream bug.

Use a set key file to check an upstream-finding manifest for a clear upstream
target. First run the safe preview. It does strict local checks and bounded
GET-only exact-marker deduplication. It creates no issue and changes no
checkout.

```bash
uv run agent-work upstream-finding preview --manifest-file finding.yaml \
  --upstream-host gitlab.example.com --upstream-project-id 123 \
  --upstream-credential-file /secure/upstream-token
```

Review `would-create` before filing. `--apply` is the only write switch. It
repeats exact-marker deduplication just before at most one create request.

```bash
uv run agent-work upstream-finding preview --manifest-file finding.yaml \
  --upstream-host gitlab.example.com --upstream-project-id 123 \
  --upstream-credential-file /secure/upstream-token --apply
```

`would-create` means a dry run found no matching open issue. `match` means an
old or restored exact marker. `created` means one checked new upstream issue.
After a safe uncertain create failure, rerun the same clear command. Its next
marker scan restores a created issue as `match`, or creates at most one issue
when no exact marker exists. It creates no checkout, local ledger, labels,
assignments, approvals, or merges. A new open, non-confidential issue enters
the clear upstream project's normal guarded flow only when that project starts
one. A human still approves and merges any resulting merge request.

See the complete syntax and exit behavior in the
[reference](../reference/serial-gitlab-agent-loop.md).

## Install and inspect.

From the set checkout, ensure Git, `glab`, the chosen coding agent, and UV are
there. Check Git and GitLab API access without exposing a token. Then inspect
saved state before starting a timer:

```bash
# Run the command-scoped credential-helper Git readiness check above.
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
uv run agent-work loop canary --json
```

Install the rendered user service and timer from the project's systemd assets.
First create a set service checkout. Its `agent/work-base` branch tracks
`origin/main`. The FSM creates one saved issue worktree below the XDG state dir.
It does not return this checkout to an issue branch.

```bash
agent_checkout="$(dirname "$PWD")/$(basename "$PWD")-agent-work"
git worktree add -b agent/work-base "$agent_checkout" origin/main
git -C "$agent_checkout" branch --set-upstream-to=origin/main agent/work-base
cd "$agent_checkout"
command -v uv systemctl loginctl
uv run pre-commit install
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
```

This installs the project's commit hook in the set checkout. A normal Git
commit runs the hook.

The service manager does not inherit an interactive `PATH` or checkout `direnv`
vars. Its env file records the set bare GitLab host and positive numeric project
ID, never a token. Use `direnv` only when this checkout has its own `.envrc`.
The wrapper refuses all other set `direnv` routes. It uses `direnv` only for
dir-scoped keys, then restores the set host and project ID. Record full
executable paths, install the units, and create a mode-600 env file:

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
install -D -m 0644 "systemd/$unit_name-smoke.service" "$HOME/.config/systemd/user/$unit_name-smoke.service"
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

This isolated preview checks the exact service wrapper and key route without
inheriting an interactive GitLab user. It must pass before starting the timer.
Enable user linger so the user manager survives logout. Then reload the user
units. A Codex setup starts its timer only after the companion smoke succeeds.
Claude and Pi setups use the existing service and timer checks.

Install writes version `1` of the factory-managed service-env contract. Its
fixed required keys include `AGENT_WORK_TIMEOUT`. Keep project-owned
`AGENT_WORK_AGENT`, `AGENT_WORK_BASE_BRANCH`, `AGENT_WORK_FACTORY_USERNAME`,
`GITLAB_HOST`, and `GITLAB_PROJECT_ID` under user control. A later repo update
finds an old, incomplete contract. Recreate a lost file with this setup.

After this wrapper change first merges, stop the timer and fast-forward the set
checkout once by hand. Run this preview and any canary, then restart the timer.
Later applied runs refresh the checked default branch before Python starts. They
reject local executable or rewrite-capable Git setup and target filter
attributes. Repo hooks are off for the refresh. System/global transport and key
setup is the user-run service boundary.

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
```

### Run the companion smoke service.

The smoke service needs Codex. Do not start it when `AGENT_WORK_AGENT` is
`claude` or `pi`; use the existing service and timer checks for those agents.
For Codex, this smoke is required before timer enablement. To change a service
to Codex, rerun the install block with `AGENT_WORK_AGENT=codex` and the Codex
path. Then reload the user units and keep the timer off until the smoke passes.

Run this Codex smoke after install, after a Codex or service change, or after
repeated agent handoff blocks. It uses the timer service's installed
`EnvironmentFile`. It has no timer. It does no normal loop work.

Keep the main timer off. Start the companion service once. Check its current
JSON report and journal before you enable the timer:

```bash
smoke_service="$unit_name-smoke.service"
systemctl --user daemon-reload
systemctl --user start "$smoke_service"
systemctl --user status "$smoke_service"
smoke_started_at="$(systemctl --user show "$smoke_service" --property=ExecMainStartTimestamp --value)"
journalctl --user -u "$smoke_service" --since "$smoke_started_at" --output=cat
```

The smoke shows live Codex handoff, the needed child sessions, and broker
acceptance. It uses a private work space. It does not change an issue, merge
request, normal-loop ledger record, or saved worktree.

The companion service invokes `agent-work loop smoke --apply --json` through
the installed wrapper. Its JSON report is written to its journal.

The current pass report is:

```json
{"schema_version":1,"command":"loop.smoke","applied":true,"outcome":"passed","broker_accepted":true}
```

Do not use an exit code, agent prose, or a partial report as proof. Read the
exact redacted JSON and journal for this run. An old pass is not proof. For a
Codex setup, enable the timer only when the current result shows success.

For a capacity or handoff failure, add live agent slots or fix the Codex
feature, then run the smoke again. For a broker failure, read the short broker
error and service journal, fix that path, and run the smoke again. Keep the
timer off until the result is clear and passes.

### Enable the unattended timer.

For Codex, complete the smoke above first. For Claude or Pi, complete the
existing service and timer checks instead. Then enable and inspect the timer:

```bash
systemctl --user enable --now "$unit_name.timer"
systemctl --user status "$unit_name.timer"
```

The timer schedules the next bounded run 20 minutes after the service goes
inactive. That run checks for new and resumable work. It also rechecks a
valid default-branch pipeline that was still active. A later activation may
poll a retained exact MR observer. In a run, one worker owns the issue's full
flow session. Router owns that workflow; Architect supplies the reviewed design.
Red, Green, and read-only judges pass context through the applicable route.
The installed prompt must pass the compatibility check above. The runner
restores the saved
worktree and coarse `WORKFLOW` state, not a sidecar checkpoint for each role.
The service has a 190-minute outer timeout. It includes the approved 180-minute
worker budget and 10 minutes for wrapper checkout, GitLab, publication, and
reporting work. Other wrapper housekeeping calls have a 120-second deadline.
The applied worker remains governed by its remaining 10,800-second budget.
If the outer timeout expires, systemd
terminates the whole control group. It gives the group 30 seconds to exit and
records the timeout in the journal. The durable ledger and
issue worktree remain for normal retry or human diagnosis. Run only one timer
host for a project. The service's environment and XDG state directory must be
writable only by the operator. If `systemctl --user`
reports `Failed to connect to bus`, stop and fix the user-manager socket. Do
not retry with another key, host, or env. Systemd saves standard output and
error under its project-specific `SyslogIdentifier`. The wrapper does not also
send output through `logger`.

### Establish a validated user-bus boundary

The runtime applies this boundary before every executable `systemctl --user`
command. It preserves valid explicit `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS` values. Only when either is absent, it derives
`XDG_RUNTIME_DIR=/run/user/<effective-uid>` and
`DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<effective-uid>/bus`. It then
requires an absolute runtime directory and existing bus socket, both owned by
the effective user. It fails closed if any check fails. Do not substitute
another host, credential, or bus address.

Credential rotation and service-environment migration use the same boundary.
Their isolated previews run the intentionally mode-`0644` wrapper with argv,
not a shell command: `/usr/bin/env bash <checkout>/scripts/agent-work-systemd-run.sh`.
Service-environment migration adds `--validate-service-environment`. Do not add
an executable bit to the wrapper.

### Repair an installed service configuration.

Stop the timer before changing its environment file. Re-run the creation block
above, then reload and enable the timer. Do not change credentials, worktrees,
ledger files, branches, or stashes merely to start the service.

### Update a service env after a repo upgrade.

Before enabling an upgraded timer, migrate its installed environment file. The
bare command is a non-mutating, non-claiming validation and preview. It does
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
contract, apply that reviewed operation. Migration validates the configured
Developer identity and project. It also validates default-branch protection and
approval safeguards through the checkout's `direnv` and password-store
credential route. It does not accept, print, or rebind a credential.

```bash
uv run agent-work service-environment migrate \
  --environment-file "$environment_file" --unit-name "$unit_name" \
  --apply --json
```

Applied migration regenerates only the factory-managed contract version,
checkout, and tool-path fields. It preserves the five project-owned values
listed above. It atomically replaces the mode-600 file and runs isolated,
non-claiming validation. Both units remain stopped for human review. If a
pre-write check fails, the file is unchanged. If a later step fails, keep the
units stopped. Use the command's sanitized error to restore or inspect the
byte-recoverable prior file. Do not hand-edit the contract to bypass a failed
check.

After a successful migration, review the result and run the normal canary
checkpoint. Re-enable the timer only after the canary is understood. Migration
never enables or starts either unit.

### Rotate the dir-scoped GitLab key.

Use credential rotation after ownership changes for the directory-scoped
credential used by the installed wrapper. It updates only the configured
`AGENT_WORK_FACTORY_USERNAME`. The credential remains in its directory-scoped
route and is never a command argument, environment-file value, diagnostic, or
log entry.

First stop unattended work and inspect the dry-run result. Supply the exact
mode-600 service environment file and installed unit name. The dry run does not
write the file and leaves the timer and service stopped.

```bash
systemctl --user disable --now "$unit_name.timer"
systemctl --user stop "$unit_name.service"
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --json
```

After reviewing the redacted result, apply that command. It checks the
configured bare host and numeric project through the newly configured
credential. It requires exactly Developer access and strict effective
default-branch protection. It also requires at least one global approval rule
before it atomically changes the expected username entry.

```bash
uv run agent-work credential rotate \
  --environment-file "$HOME/.config/$unit_name/agent-work.env" \
  --unit-name "$unit_name" --apply --json
```

Do not re-enable the timer during credential rotation. On success, inspect the
redacted result. Complete the normal explicit operator review and bounded canary
in [Canary and re-enable](#canary-and-re-enable). Re-enable the timer only when
that review succeeds. Ordered canary execution and retained record recovery are
outside this command.

If the command reports an invalid, expired, unauthorized, or mismatched
credential, leave the timer and service stopped. The environment file remains
byte-identical unless all preconditions pass. If the bounded post-update wrapper
preview fails, the command makes one exact-byte rollback attempt and returns a
sanitized corrective handoff. If rollback cannot be confirmed, it reports
configuration uncertainty for human correction. Preserve the stopped services
and environment file, act on that handoff, and rerun a dry run after correcting
the directory-scoped credential. Do not manually edit the username to bypass it.

## Run and inspect.

Preview the next action before you allow a change. A preview must not change
GitLab, labels, branches, worktrees, ledger records, or systemd state.

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop canary --json
uv run agent-work loop status --json
```

After a human completes the repo's automatic-push readiness gate, run an
applied run or let the timer do so. The default allows up to 16 flow attempts
in one 10,800-second (three-hour) work budget. Each flow call gets the
positive, ceil-rounded time left. A fresh issue tries its full fit flow. A
restored issue resumes from its saved worktree and coarse `WORKFLOW` state. Old
phase-ledger values finish their saved route. Internal Router, Architect, Red,
Green, and judge handoffs are not saved apart. The compatibility stop above
still applies:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop run --apply --json
systemctl --user status "$unit_name.service"
```

Use `--max-segments` or `--max-activation-seconds` for smaller work limits.
The run advances one issue. With `--apply`, it does not wait for a valid,
active default-branch pipeline. It checks every open MR against its source SHA
and may choose another issue. The timer checks the pipeline again. A
current-SHA MR pipeline moves from `PUBLISHING` to `OBSERVING` only when its
owner, worktree, project, and target are exact. That activation hands off as
`outcome=activation_complete` and `stop_reason=pipeline_wait` without polling.
The service pass previews once. Later, ordinary admission parks that retained
exact observer from ownership. Parking does not create `pipeline_wait`: it can
start another item or be idle and yield to the timer. A retained inexact or stale
claimant stays visible for `_observe` before fresh work. It blocks or repairs if
identity is unclear. Preview is read-only and fast. It does not poll.

An interactive Judge waiting for this project's validation window can request
the controller to yield. After the controller saves a coding Red checkpoint, an
applied run can return `stop_reason=interactive_validation_yield` while the
issue stays in `EXECUTING`. The service exits and its wrapper stops this pass;
the timer's next activation resumes the saved worker if the issue remains
active. If that exact issue is `COMPLETE`, the next applied run reports
`validation_admission.resolution=closed_without_resume`. For another non-active
state, such as `BLOCKED`, it reports `ineligible_without_resume`. Both return
`stop_reason=validation_admission_resolved` without invoking the worker. Check
`resolved_at` and `issue_iid`; `resume_at` is null. Either report consumes the
request and permits a new window. The waiting validation
window takes the project lock and timing admission within one bounded wait. It
holds both during validation. If it reports
`reason=project_timing_admission_busy`, validation did not start. The request
is no longer live, so later controller runs are not held by it. Check the
next applied run's `validation_admission` for its outcome. A resumed report
has yield, validation, and resume times. A resolved report has yield and
validation times plus `resolved_at`. An unavailable report has
`unavailable=true`. [Phase timing](../reference/serial-gitlab-agent-loop.md#ledger-worktrees-and-audit)
tracks workflow phases and roles separately.
Wait for that admission report before starting another validation window for
this project; a new window is deferred while the earlier request is outstanding.

If the full budget is used, inspect the saved retry or worktree restore result
as you would any other timeout.

Use `--debug` when the safe status needs local diagnostic context. Do not copy
debug output with private work details into an issue.

## Retry or recover retained work.

Inspect the saved state and next action first. Retry a named blocked item only
when the shared `recovery resolve --choice retry` predicate accepts its current
evidence. This resets its retry budget and returns it to resumable setup. It
does not need a new human unblocking approval.

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
uv run agent-work loop retry --issue-iid IID --json >agent-work-loop-retry-IID-plan.json
uv run agent-work loop retry --issue-iid IID --apply \
  --plan-file agent-work-loop-retry-IID-plan.json --json
```

`retry_preview` is the only preview that supplies an eligible plan. An apply
that returns `retry_scheduled` resumes the reported retained workflow.
`correction_activated` requires the reported correction and validation before
publication. `retry_ineligible` reports known ineligible evidence and changes
nothing; correct the evidence and create an exact `recovery resolve --choice
retry` preview. If a check is unavailable, unclear, stale, or uncertain, it
exits with an error, not `retry_ineligible`; do not apply a plan. See the
[retry outcome reference](../reference/serial-gitlab-agent-loop.md#retry-outcomes).

The first try and three retries are automatic. A run yields when a retry due
time is in the future. When retries run out, the issue is `agent::blocked`.
An applied run may resume one clean, untouched ordinary blocked record before
the Red checkpoint, but only after exact failure and recovery checks under the
project and issue locks. A repeated identical failure adds a 20-minute
cooldown. For other blocked records, follow the saved next action. After
correcting the evidence, create and apply a reviewed retry plan for that exact
IID. A failed predicate changes nothing. Do not
change labels to resume blocked work. Leave a dirty saved worktree in place. If
status lists a stash, keep it for review. Do not use `git stash apply` or `git
stash drop` as an automatic restore step.

An exact failed current-source merge-request pipeline repairs itself when it
has one unique backed retained owner. Keep the timer on or run one normal
applied run. The loop runs one ready repair flow without a reviewed checkpoint:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop run --apply --json
```

Do not use a broad label edit or manual restore command instead. A failed
automatic repair saves a 20-minute backoff for that item. The next run can pick
another ready exact repair or normal work. It does not stop the full factory for
this item. After three failed automatic repairs, the item is held or deferred
for human review. Use `loop status --issue-iid IID --json` or the read-only
preview. Inspect the try count, backoff or hold, exact proof, and next action.

Use targeted `loop recover` only for cut off, unclear, or unsafe retained work.
It is an unusual, reviewed path for one exact IID with live backing. It does
not spend or reset the automatic repair budget. Unclear or inexact user, active
owner, key, policy, or authority conflicts stay fail-closed. Neither issue nor
merge-request text can set repair priority, the budget, or backoff. The loop
never approves or merges. For a blocked target, it uses the same retry
predicate and rejects ineligible evidence without changes. Reclassify one
blocked record at a time with live backing.

An `agent::suggest_close` label is a structured recommendation for human
closure, never an automated close. Review its duplicate, superseded,
already-satisfied, or no-longer-applicable evidence. Only a human may close an
issue. Closure reconciles the record to `COMPLETE` and clears the label. The
loop never approves, merges, or closes issues.

### Observe an automated `split_required` decomposition.

When the legacy workflow Architect returns `result_kind=split_required`, the
trusted runner handles the validated plan in the current segment. It searches for each exact
child marker. It reuses a matching child and creates only missing children.
Each new child has a full description that links to the parent and its
dependencies. Once every child exists, the controller adds one parent note and
closes it.

If this handling is interrupted, the next attempt reruns the workflow. The
same marker searches recover children that already exist. There is no separate
decomposition state, saved plan or progress record, checkpoint restoration, or
special queue to repair.

For an open child, the runner projects each open trusted prerequisite as one
`agent-prereq_<IID>` label, ordered by numeric IID. When a literal prerequisite
closes after decomposition, the same dependency continues through that parent
to every required open child or descendant. The dependent stays blocked and
shows those descendant IIDs until they all close. The closed parent's label is
replaced by descendant labels, so the dependency projection remains visible.
During an interrupted controller decomposition, the parent remains open and is
still the direct blocker. Only a closed parent expands to its observed,
marker-bound children.

Selection, label projection, and recovery use the same marker-derived proof
from one complete live issue snapshot. A missing referenced dependency,
duplicate child identity, malformed or ambiguous marker, self-reference, or
cycle fails closed. While a required descendant is open, the runner does not
claim the dependent, start its worker, or schedule a tight retry. Retained
recovery reports `dependency_gated`. After all required descendants close, it
can report the existing retained work as `safely_retryable`; the work and human
merge-request review remain in place.

The runner removes a projection when the complete proof has no open required
need, the trusted marker removes the dependency, or the dependent closes.
Preview `loop run` and `recovery inventory` show current and intended labels
without writing them. An applied `loop run` repairs missing or stale labels
before normal selection. It creates or normalizes canonical project-label
definitions, removes exact legacy `agent::prereq_<IID>` labels from affected
issues, and adds the full ordered canonical set. It deletes matching legacy
project-label definitions only after every issue-label transition succeeds. A
partial transition keeps those definitions for a safe retry. A pass that
repairs projection drift only repairs. It does not claim work, create a ledger
record or worktree, or start a worker. After that pass, the service wrapper
previews the queue. It starts a new applied run only if work is ready. It reads
GitLab again before it picks an issue. If none is ready, the service exits and
the timer checks later.

Let each approved child issue enter the normal eligibility queue and produce
its own merge request in dependency order. Leave a tracking parent blocked or
close it under the project's issue practice. If the parent is narrowed to a
suitable outcome, explicitly retry that IID only after the edit is complete:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop retry --issue-iid IID --json >agent-work-loop-retry-IID-plan.json
uv run agent-work loop retry --issue-iid IID --apply \
  --plan-file agent-work-loop-retry-IID-plan.json --json
```

Do not remove lifecycle or prerequisite-projection labels by hand. Do not
increase worker budgets to bypass the sizing decision. The explicit retry
preserves the durable recovery path. The next worker session reassesses the
revised scope before mutation.

## Move old loop proof.

Before moving old data, stop the timer and ensure no service segment is active.
The preview lists running, awaiting-human, failed, blocked, and closed GitLab
projections; exact branch, worktree, and stash proof; and safe ledger actions.
It changes nothing.

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
ledger record. It never overwrites that evidence. After human review, apply
the idempotent migration and inspect the result:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work migration apply --plan-file agent-work-migration-plan.json --apply --json
uv run agent-work loop status --json
```

## Plan a canary check and focused restore.

### Canary and re-enable.

Keep the timer off and the service inactive while you prepare restore. A canary
is a non-claiming checkpoint. It checks public user, saved restore proof, and
pipelines without picking or changing an issue.

First, create and review the plan. Apply that same plan only while the timer
stays stopped:

```bash
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop canary --json >agent-work-canary-plan.json
# Review the redacted plan and its expected checkpoint/next action.
uv run agent-work loop canary --apply --plan-file agent-work-canary-plan.json --json
uv run agent-work loop status --json
```

If the preview instead proposes lifecycle-label or manual-tracker-marker setup,
review that fixed label-spec action and use the same stopped-timer order. Apply
checks the preview again, creates lost fixed labels or the `agent-work::manual`
marker, and fixes their color or text. It never assigns a label to an issue.

When status finds a nonblocked retained item that needs restore, make a separate
plan for that exact IID. Do not use normal `loop run`; it would skip the
targeted proof check. For a blocked item, use the `loop retry` plan above; it
shares the `recovery resolve --choice retry` predicate.

```bash
uv run agent-work loop recover --issue-iid IID --json >agent-work-recover-IID-plan.json
# Review the exact issue, branch, worktree, MR, and pipeline evidence.
uv run agent-work loop recover --issue-iid IID --apply \
  --plan-file agent-work-recover-IID-plan.json --json
uv run agent-work loop status --issue-iid IID --json
```

Each apply rechecks the reviewed plan under the project lock. Create a new
preview after an identity rotation, ledger revision, or a change to target,
repository, merge-request, or pipeline evidence. Neither plan prints secrets.

Keep the timer stopped if the command reports a conflict, a higher-priority
owner, a blocked default-branch pipeline, or unresolved pipeline evidence.
A valid, active default-branch pipeline alone does not require a timer stop.
Follow the reported safe next action. For a block, do not select another issue
or change retained artifacts.

Re-enable the timer only when the expected checkpoint has been reached and you
understand the reported status:

```bash
systemctl --user enable --now <project-slug>-agent-work.timer
```

This order also handles the old issue-76/77-style case. Inspect and plan the
old retained owner first. Apply only matching proof. Leave other work alone.
Issue text is proof to inspect, never work authority. Human review, approval,
and merge stay outside the loop.

## Monitor, restart, or stop the loop.

### Check user-systemd access before supervision.

From a non-login or API shell, apply the checked user-bus boundary before each
`systemctl --user` or `journalctl --user` command. Keep clear
`XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` values. Derive
`/run/user/<effective-uid>` and its `bus` socket only if they are absent. Check
full paths, real-user owner, dir and socket types. Fail closed on a mismatch.
The [software-factory-supervisor skill](../../.agents/skills/software-factory-supervisor/SKILL.md)
needs this procedure when it supervises.

Checked derived values mean the shell lacked env vars, not that systemd is down.
On check failure, report the user-manager dir or socket as lost, blocked, bad,
or wrongly owned. Do not call `Failed to connect to bus: No medium found` an
outage until the check fails.

Inspect the timer, service, and safe state together. Systemd owns service logs
through the unit's `SyslogIdentifier`. The app keeps its local lifecycle log,
`agent-work.log`, in the XDG project-state dir. Inspect both sources. Neither
replaces the other.

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

For this `Type=oneshot` service, an active run normally reports
`ActiveState=activating` and `SubState=start`. If `ExecMainStartTimestamp` is
less than 190 minutes old, it is within its outer limit. Do not stop it just
because it runs. A done outer timeout reports `Result=timeout`. Another fault
reports `Result=failed`. `ActiveState=inactive` with `Result=success` means the
run ended. Use `systemctl status` and the unit journal to find the bounded
wrapper diagnostic. Then use `agent-work loop status --json` for the saved
ledger state and safe next action. Retained worktrees and ledger records are
restore proof. Do not delete them to clear a timeout.

After a short user-manager fault, let an active bounded run finish if it can.
Stopping it can cut off a segment. The ledger and saved worktree let the next
`loop run` match the last saved boundary.

```bash
systemctl --user stop "$unit_name.timer" "$unit_name.service"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
systemctl --user start "$unit_name.service"
```

To stop future automatic work, turn off the timer and stop an active service.
This keeps the ledger, worktrees, branches, stashes, env file, and journal for
listing and review.

```bash
systemctl --user disable --now "$unit_name.timer"
systemctl --user stop "$unit_name.service"
systemctl --user show "$unit_name.timer" "$unit_name.service" \
  --property=UnitFileState --property=ActiveState
export AGENT_WORK_BASE_BRANCH=agent/work-base
uv run agent-work loop status --json
```

Before a move or manual proof review, the timer must report
`UnitFileState=disabled` and `ActiveState=inactive`. If `journalctl --user`
access is denied, ask the host admin for fit journal access. Do not change
journald storage settings or use another key route.


## Publication credential readiness and retained recovery.

Before either preview or apply invokes the worker, the shared Git credential
route verifies the configured credential-free HTTPS origin. It performs a
non-mutating Git readiness check. It receives the configured `GITLAB_TOKEN`
only at runtime through the repository-owned credential-protocol helper. The
token and any derived Basic header never enter a remote URL or persistent Git
configuration. They never enter the service environment file, command
arguments or process reports, logs, diagnostics, ledger, or retained record.
The check is independent from
the GitLab API verification for `GITLAB_HOST` and numeric
`GITLAB_PROJECT_ID`.

Missing configuration stops before remote mutation. Invalid or expired
credentials are authentication failures. An authenticated identity without
push permission is an authorization failure. Known temporary transport
failures alone use the bounded retry path. Inherited credential helpers and
hostile ambient Git configuration cannot replace the shared route. It never
falls back to an interactive prompt. Keep the human approval and merge
boundary unchanged.

For retained publication recovery, first inspect status, the source branch,
local `HEAD`, recorded source SHA, and the exact pending publication intent.
Save and review the retry preview. Then apply that exact `loop retry` plan with
`--plan-file`.
A retained publish intent moves directly back to publishing. It keeps the same
branch, SHA, draft evidence, and intent. It replays the clean committed head
without a second commit or merge request. Do not copy credentials or command
output into the ledger or issue.
