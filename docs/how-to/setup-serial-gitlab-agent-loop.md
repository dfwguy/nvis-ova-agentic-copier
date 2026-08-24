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
command -v git glab codex
glab auth status --hostname "$GITLAB_HOST"
codex login status
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Every later GitLab operation must address this numeric endpoint, never a path
inferred from `origin`. A Developer account can poll eligible issues. It does
not need permission to install a webhook or an inbound service.

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

An applied pass uses the deterministic branch `agent/issue-<IID>` and changes
labels from `agent::ready` to `agent::running`. It reaches
`agent::awaiting-human` only after verifying an exact same-project MR: source
and target project IDs, deterministic source branch, default target branch,
and MR SHA must match both local and remote branch tips, and GitLab must report
that the MR closes the issue (`Closes #IID`). Missing or mismatched evidence
ends at `agent::failed`.

## Authorize and enable applied operation

Before enabling the timer, obtain explicit unattended-publication authorization
for this repository and confirm the constrained service identity. Verify the
independent MR safeguards: at least one approval, no author self-approval, and
no per-MR approval-rule overrides. These settings need Maintainer access. A
Developer `403` is inconclusive; require a human Maintainer attestation before
continuing. The loop can push and open an MR, but never approves or
merges it.

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
