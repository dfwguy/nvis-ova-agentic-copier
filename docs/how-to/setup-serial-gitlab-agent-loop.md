# Set up the serial GitLab agent loop

*Diátaxis category: How-to guide.*

Use this procedure to configure one serial agent loop for one GitLab project.
It uses Developer-compatible polling, not a webhook, and it does not authorize
unattended publication until the explicit gate below is complete.

## Check prerequisites and bind the project

Set and validate the non-secret GitLab target before checking its authenticated
session. Do not print, export, or put tokens in this setup file:

```bash
export GITLAB_HOST=gitlab.example.com
export GITLAB_PROJECT_ID=12345
case "$GITLAB_HOST" in ''|*://*|*/*) exit 2;; esac
case "$GITLAB_PROJECT_ID" in ''|*[!0-9]*) exit 2;; esac
```

Check the preinstalled Git, `glab`, and Codex tools, then verify the existing
`glab` and Codex sessions. From the dedicated checkout after it exists,
`git ls-remote origin HEAD` is the noninteractive Git access check:

```bash
command -v git glab codex jq
glab auth status --hostname "$GITLAB_HOST"
codex login status
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Every later GitLab operation must address this numeric endpoint, never a path
inferred from `origin`. A Developer account can poll eligible issues. It does
not need permission to install a webhook or an inbound service.

The fail-closed readiness block after creating the checkout obtains the numeric
project representation once. It classifies the `glab` identity from the maximum
numeric project/group access value; no numeric value is `unknown`. Only exactly
30 (Developer) can continue. This check concerns the `glab` credential only;
Git may use a different credential and is checked with `git ls-remote`.

## Create the dedicated checkout

Choose a path owned by the service user and outside any active operator
checkout. Clone the verified repository URL into it, then check that it is
clean and rooted at the configured directory:

```bash
export AGENT_WORK_CHECKOUT="$HOME/.local/share/agent-work/PROJECT"
install -d -m 0700 "$(dirname "$AGENT_WORK_CHECKOUT")"
git clone REPOSITORY_URL "$AGENT_WORK_CHECKOUT"
git -C "$AGENT_WORK_CHECKOUT" rev-parse --show-toplevel
git -C "$AGENT_WORK_CHECKOUT" status --porcelain
git -C "$AGENT_WORK_CHECKOUT" ls-remote origin HEAD
```

Compare `origin` with the verified project's HTTP or SSH URL. An existing,
dirty, incorrectly rooted, or wrong-origin checkout must be repaired before an
applied run; do not repurpose an operator's checkout.

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

## Install the command and user units

Install `agent-work` at the stable user path used by the service, then install
the one templated oneshot service and its one timer:

```bash
command -v uv
uv tool install --force .
command -v agent-work
install -d -m 0700 "$HOME/.config/systemd/user" "$HOME/.config/agent-work" "$HOME/.local/state/agent-work"
install -m 0644 systemd/agent-work@.service systemd/agent-work@.timer "$HOME/.config/systemd/user/"
install -m 0600 /dev/null "$HOME/.config/agent-work/PROJECT.env"
systemctl --user daemon-reload
```

Write exactly these four non-secret settings to `PROJECT.env`. The checkout and
lock-file paths must be absolute. Credentials remain in the service user's
existing Git and `glab` credential mechanisms, not in this file:

```text
GITLAB_HOST=gitlab.example.com
GITLAB_PROJECT_ID=12345
AGENT_WORK_CHECKOUT=/home/USER/.local/share/agent-work/PROJECT
AGENT_WORK_LOCK_FILE=/home/USER/.local/state/agent-work/PROJECT.lock
```

## Run the preview smoke test

Load the non-secret instance settings and run the read-only default. It either
reports `idle`, a ready item, or an already-running item; it does not change
GitLab, Git, or invoke Codex:

```bash
set -a
. "$HOME/.config/agent-work/PROJECT.env"
set +a
agent-work run-next --json
```

An applied pass uses the deterministic feature branch `agent/issue-<IID>` and changes
labels from `agent::ready` to `agent::running`. It reaches
`agent::awaiting-human` only after verifying an exact same-project MR. Its
machine outcome is `awaiting_human`: source
and target project IDs, deterministic source branch, default target branch,
and MR SHA must match both local and remote branch tips, and GitLab must report
that the MR closes the issue (`Closes #IID`). Missing or mismatched evidence
ends at `agent::failed`.

## Authorize and enable applied operation

Before enabling the timer, obtain explicit unattended-publication authorization
for this repository and confirm the constrained service identity. If the
effective `glab` access level checked above is exactly Developer (`30`), that
one-time authorization is sufficient to enable this unattended publication
path. The Developer-readable safeguards above replace the inaccessible
Maintainer-only `/approvals` audit and recurring attestation. They do not prove
self-approval or per-MR approval-rule override policy; those settings require a
Maintainer. The loop can push and open an MR, but never approves or merges it.

Developer access is a readiness signal, not proof of all resulting controls.
Developers may approve in GitLab, and project protections control push, merge, and self-approval. Keep the service identity constrained and verify that the
Git credential used for publication is the intended identity; it can differ
from the `glab` credential used to create the MR. The loop never approves or merges.

Then enable exactly one timer instance and inspect it:

```bash
systemctl --user enable --now agent-work@PROJECT.timer
systemctl --user status agent-work@PROJECT.timer
systemctl --user list-timers 'agent-work@*'
journalctl --user-unit agent-work@PROJECT.service --since '30 minutes ago'
```

Each invocation is bounded and serial. If its local project lock is busy, the
command exits `75`; let the next timer event retry rather than starting another
worker. This is deliberately a single-host local lock, not a distributed lock
or GitLab compare-and-set claim: do not run another host or timer instance for
the same project.

## Pause or repair the loop

Disable the timer before changing the service checkout, environment file,
credentials, or GitLab labels:

```bash
systemctl --user disable --now agent-work@PROJECT.timer
systemctl --user reset-failed agent-work@PROJECT.service
```

Resolve an `agent::failed` or stranded `agent::running` issue under human
direction, confirm the checkout is clean and on the verified project, rerun the
preview, and enable the timer again only after the publication gate remains
satisfied. No legacy SQLite queue, lease, or state migration is needed: this
loop's durable work and handoff are the GitLab issue and MR labels. For command
and boundary details, see the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md).
