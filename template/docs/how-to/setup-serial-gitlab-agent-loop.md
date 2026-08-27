# Set up the serial GitLab agent loop

*Diátaxis category: How-to guide.*

Use this procedure to configure one serial agent loop for one GitLab project.
It uses Developer-compatible polling, not a webhook, and it does not authorize
unattended publication until the explicit gate below is complete.

## Check prerequisites and confirm the current checkout

Run the loop from the repository it should operate on. The runner itself confirms Git
access with `git pull --ff-only`, derives the GitLab host and project path from
`origin`, resolves that exact path through the GitLab API, and confirms the
numeric project endpoint. Do not print, export, or put tokens in this setup.

Choose one supported coding agent: `codex` (the default), `claude`, or `pi`.
Check that agent, Git, and `glab`, then verify that the active credential route
can call the GitLab API. This supports either a stored
`glab` login or a directory-scoped token such as `GITLAB_TOKEN`:

To use Claude Code or Pi for this installation or repair, set the non-secret
profile before running the applicable block:

```bash
export AGENT_WORK_AGENT=claude  # Or: pi. Omit for the codex default.
```

```bash
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *) exit 2;; esac
command -v git glab "$agent_name" jq
glab api --hostname "$GITLAB_HOST" user >/dev/null
git pull --ff-only
git remote get-url origin
```

If any confirmation fails, repair the repository's Git remote/credential,
active GitLab API credential route, or selected agent authentication, then
rerun the command. A Developer
account can poll eligible issues; it does not need permission to install a
webhook or an inbound service.

Run `codex login status` or `claude auth status` for those profiles before the
readiness block. Pi has no equivalent non-secret status command; verify its
configured provider credential with Pi before enabling `--apply`.

The fail-closed readiness block obtains the numeric
project representation once. It classifies the `glab` identity from the maximum
numeric project/group access value; no numeric value is `unknown`. Only exactly
30 (Developer) can continue. This check concerns the `glab` credential only;
Git may use a different credential and is checked with `git ls-remote`.

The runner uses the current checkout and stores its fixed, gitignored
`.agent-work.lock` alongside it. Start only one loop for a checkout. The
checkout must be dedicated: the service returns it to the default branch after
each pass, so never use it for interactive development or another agent session.

## Verify Developer-readable safeguards

Before enabling `--apply`, paste this one guarded block. It exits nonzero and
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

The selected Developer readiness policy deliberately skips only the
Maintainer-only `/approvals` audit and recurring Maintainer attestation. A
Developer cannot read or prove GitLab's author/committer self-approval or
per-MR approval-rule-override settings; those remain Maintainer policy. This is
not a role-only claim: the observable checks above must pass, while the
workflow and runtime never approve or merge. The parentheses isolate the strict
shell options, function, and failure exit from the operator's shell while
preserving this block's nonzero status.

## Install and start the systemd user timer

Run the runner through the repository's own UV environment. This prevents one
repository's version from replacing another repository's runner:

```bash
command -v uv logger systemctl loginctl
uv run agent-work --help
```

The user service manager does not inherit an interactive shell's `PATH`.
Record the chosen agent and the absolute executable paths for it and `uv` in
the environment file instead of relying on a later command lookup. The runner
invokes that recorded absolute agent path directly.

The user service manager also does not inherit a checkout's `direnv` variables.
When `GITLAB_TOKEN` is supplied through `direnv`, record the absolute `direnv`
path below. The wrapper then uses `direnv exec` for the runner process only; it
does not write the token to the environment file. `glab auth status` reports
only stored `glab` logins, so it is not a valid test for this token route.

Run the read-only preview from that checkout:

```bash
uv run agent-work run-next --json
```

The runner has built-in noninteractive profiles for `codex`, `claude`, and
`pi`. Codex receives its prompt on standard input. Claude Code and Pi receive
it as one literal argument in print mode with ephemeral session settings. All
profiles must follow the same prompt contract: edit and validate only the
prepared branch, never commit, push, approve, or merge, and emit exactly one
delimited merge-request JSON draft. This worker exception takes precedence over
the repository-wide publication requirement; the trusted runner remains the
only publisher.

After a human explicitly authorizes unattended publication, install the
one-shot service and its five-minute timer. The service runs the repository's
wrapper, which sends the runner output and lifecycle messages through `logger`
with the `agent-work` tag.

Use this path only when the readiness check reports effective Developer access
exactly `30`; it does not replace the documented branch-protection or approval
evidence.

Set the project-specific systemd unit name:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
```

Then capture the executable paths and install the service files:

```bash
uv_path="$(command -v uv)"
if [[ "$uv_path" != /* || ! -x "$uv_path" ]]; then
  printf '%s\n' 'uv must resolve to an executable absolute path' >&2
  exit 1
fi
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *)
  printf '%s\n' 'AGENT_WORK_AGENT must be codex, claude, or pi' >&2
  exit 1
  ;;
esac
agent_path="$(command -v "$agent_name")"
if [[ "$agent_path" != /* || ! -x "$agent_path" ]]; then
  printf '%s\n' 'agent must resolve to an executable absolute path' >&2
  exit 1
fi
direnv_path="$(command -v direnv || true)"
if [[ -n "$direnv_path" && ( "$direnv_path" != /* || ! -x "$direnv_path" ) ]]; then
  printf '%s\n' 'direnv must resolve to an executable absolute path' >&2
  exit 1
fi
install -D -m 0644 "systemd/$unit_name.service" "$HOME/.config/systemd/user/$unit_name.service"
install -D -m 0644 "systemd/$unit_name.timer" "$HOME/.config/systemd/user/$unit_name.timer"
install -d -m 700 "$HOME/.config/$unit_name"
printf 'AGENT_WORK_CHECKOUT=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\n' "$PWD" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" >"$HOME/.config/$unit_name/agent-work.env"
chmod 600 "$HOME/.config/$unit_name/agent-work.env"
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" --property=Linger
if [[ -z "${XDG_RUNTIME_DIR:-}" || ! -S "${XDG_RUNTIME_DIR}/bus" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
```

The linger check must report `Linger=yes`. Linger keeps the user service
manager available after logout, so the timer can continue to run. Use a
dedicated checkout, not an operator's active checkout. The rendered unit name
is `<project-slug>-agent-work`; this repository's is
`agentic-copier-agent-work`.

If `systemctl --user` still reports `Failed to connect to bus`, stop the setup:
the user manager socket is unavailable. Do not retry through another
credential, host, or execution environment. Report that first non-secret
diagnostic to the operator.

### Repair an installation missing the selected agent configuration

For a timer installed by an earlier version, stop it before rewriting its
environment file, then enable it again:

Set the same project-specific systemd unit name:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
```

Then stop and restart the timer:

```bash
if [[ -z "${XDG_RUNTIME_DIR:-}" || ! -S "${XDG_RUNTIME_DIR}/bus" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
systemctl --user disable --now "$unit_name.timer"
uv_path="$(command -v uv)"
if [[ "$uv_path" != /* || ! -x "$uv_path" ]]; then
  printf '%s\n' 'uv must resolve to an executable absolute path' >&2
  exit 1
fi
agent_name="${AGENT_WORK_AGENT:-codex}"
case "$agent_name" in codex|claude|pi) ;; *)
  printf '%s\n' 'AGENT_WORK_AGENT must be codex, claude, or pi' >&2
  exit 1
  ;;
esac
agent_path="$(command -v "$agent_name")"
if [[ "$agent_path" != /* || ! -x "$agent_path" ]]; then
  printf '%s\n' 'agent must resolve to an executable absolute path' >&2
  exit 1
fi
direnv_path="$(command -v direnv || true)"
if [[ -n "$direnv_path" && ( "$direnv_path" != /* || ! -x "$direnv_path" ) ]]; then
  printf '%s\n' 'direnv must resolve to an executable absolute path' >&2
  exit 1
fi
printf 'AGENT_WORK_CHECKOUT=%q\nAGENT_WORK_UV=%q\nAGENT_WORK_AGENT=%q\nAGENT_WORK_AGENT_PATH=%q\nAGENT_WORK_DIRENV=%q\n' "$PWD" "$uv_path" "$agent_name" "$agent_path" "$direnv_path" >"$HOME/.config/$unit_name/agent-work.env"
chmod 600 "$HOME/.config/$unit_name/agent-work.env"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
```

Each pass checks GitLab for `agent::running` and conflicted
`agent::awaiting-human` merge requests before it claims one eligible open
issue, and refetches the running list. A conflicted handoff is reclaimed on
its existing deterministic branch for one remediation pass. It begins work only when
the selected issue is the sole running lock. Published work becomes
`agent::awaiting-human`; failures become `agent::failed`. The loop never
approves or merges an MR.

Each applied pass also checks the newest default-branch pipeline. If it is
`failed` or `canceled`, the worker repairs it in the selected issue's merge
request instead of leaving it only reported; the runner still never approves
or merges that MR.

Each failed transition leaves the safe failure reason as an issue comment;
worker output is tagged in the user journal. Every pass logs its start, worker
diagnostic, exit status, and waiting state. A runner blocker fails the service
so its status is visible to systemd; the timer schedules the next pass normally.
An issue-level failure that the runner has successfully marked as
`agent::failed` exits `1`, preserving the failure for systemd and the journal.
The timer continues to schedule future passes. Other runner-level blockers also
exit nonzero.
If another local
process holds `.agent-work.lock`, `run-next` exits `75`; the service records
the temporary failure and the timer remains enabled.

Every bounded pass attempts to leave the dedicated checkout on `main` before
the service exits, including a pass that failed after creating a local issue
commit. It fast-forwards `main` while preserving that issue branch for recovery
and human review. If the reset fails (for example, because the checkout is
dirty), the service fails and records the diagnostic in the journal. A direct
`agent-work` launch from an issue branch still intentionally fails rather than
silently changing branches.

## Restart the loop

Restart the loop after a transient user-manager or service failure without
changing the checkout, credentials, or environment file. If the one-shot
service is active, let its bounded pass finish before restarting it when
possible. Stopping an active pass interrupts it; the next pass will use the
runner's normal running-issue recovery or safe failure handling.

Set the unit name, ensure the user-manager bus is reachable, then stop both
units, reload their definitions, enable the timer again, and start one
immediate bounded pass:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
if [[ -z "${XDG_RUNTIME_DIR:-}" || ! -S "${XDG_RUNTIME_DIR}/bus" ]]; then
  export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
systemctl --user stop "$unit_name.timer" "$unit_name.service"
systemctl --user daemon-reload
systemctl --user enable --now "$unit_name.timer"
systemctl --user start "$unit_name.service"
systemctl --user status --no-pager "$unit_name.timer" "$unit_name.service"
```

The final `start` runs one pass immediately; the enabled timer schedules later
passes. If the user-manager bus is unavailable, stop and resolve that first
non-secret diagnostic rather than attempting a different credential or host.

## Monitor activity and inspect failures

Set the project-specific systemd unit name before querying its logs or status:

```bash
unit_name="$(basename "$(find systemd -maxdepth 1 -type f -name '*-agent-work.timer' -print -quit)" .timer)"
```

Follow activity and inspect failures with `journalctl`:

```bash
journalctl --user -t "$unit_name" -f
journalctl --user -u "$unit_name.service" --since today
systemctl --user status "$unit_name.timer" "$unit_name.service"
```

### Repair unavailable user-journal access

The wrapper writes through `logger`, which stores the unit's messages in the
host journal. If `journalctl --user` reports insufficient permissions, the
timer can still be working: the account cannot read the journal files. Inspect
the logs immediately with sudo:

```bash
sudo journalctl -t "$unit_name" --since today
```

To grant the current user lasting journal read access, ask the host
administrator to add it to `systemd-journal`. This group can read host-wide
journal messages, not only this unit's logs. Start a new login session after
the change, then verify the membership and retry the normal commands:

```bash
sudo usermod -aG systemd-journal "$USER"
# Log out and back in before continuing.
id -nG
journalctl --user -t "$unit_name" --since today
```

If the new group does not appear in `id -nG`, do not change journald storage
settings; resolve the account/session membership with the host administrator.

Stop the timer from another terminal:

```bash
systemctl --user disable --now "$unit_name.timer"
```
