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

Check the preinstalled Git, `glab`, and Codex tools, then verify that the
active credential route can call the GitLab API. This supports either a stored
`glab` login or a directory-scoped token such as `GITLAB_TOKEN`:

```bash
command -v git glab codex jq
codex login status
glab api --hostname "$GITLAB_HOST" user >/dev/null
git pull --ff-only
git remote get-url origin
```

If any confirmation fails, repair the repository's Git remote/credential or
the active GitLab API credential route, then rerun the command. A Developer
account can poll eligible issues; it does not need permission to install a
webhook or an inbound service.

The fail-closed readiness block obtains the numeric
project representation once. It classifies the `glab` identity from the maximum
numeric project/group access value; no numeric value is `unknown`. Only exactly
30 (Developer) can continue. This check concerns the `glab` credential only;
Git may use a different credential and is checked with `git ls-remote`.

The runner uses the current checkout and stores its fixed, gitignored
`.agent-work.lock` alongside it. Start only one loop for a checkout.

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

## Install and start the tmux loop

Run the runner through the repository's own UV environment. This prevents one
repository's version from replacing another repository's runner:

```bash
command -v uv tmux
uv run agent-work --help
```

Run the read-only preview from that checkout:

```bash
uv run agent-work run-next --json
```

After a human explicitly authorizes unattended publication, start the Ralph loop
in one lingering tmux session:

Use this path only when the readiness check reports effective Developer access
exactly `30`; it does not replace the documented branch-protection or approval
evidence.

```bash
log_dir="$PWD/.agent-work-logs"
mkdir -p -m 700 "$log_dir"
log_file="$log_dir/agent-work-$(date -u +%Y%m%dT%H%M%SZ).log"
tmux new-session -d -s agent-work
tmux pipe-pane -t agent-work -o "cat >> '$log_file'"
tmux send-keys -t agent-work 'while :; do printf "[%s] agent-work: starting pass\n" "$(date -Is)"; uv run agent-work run-next --apply --json; status=$?; printf "[%s] agent-work: pass exited %s\n" "$(date -Is)" "$status"; if [ "$status" -ne 0 ]; then if [ "$status" -eq 75 ]; then printf "[%s] agent-work: stopped; checkout lock is held\n" "$(date -Is)" >&2; else printf "[%s] agent-work: stopped after failure; see the diagnostic above and $log_file\n" "$(date -Is)" >&2; fi; break; fi; printf "[%s] agent-work: next pass in 300 seconds\n" "$(date -Is)"; sleep 300; done' C-m
tmux attach -t agent-work
```

Each pass checks GitLab for `agent::running` and conflicted
`agent::awaiting-human` merge requests before it claims one eligible open
issue, and refetches the running list. A conflicted handoff is reclaimed on
its existing deterministic branch for one remediation pass. It begins work only when
the selected issue is the sole running lock. Published work becomes
`agent::awaiting-human`; failures become `agent::failed`. The loop never
approves or merges an MR. Each failed transition leaves the safe failure reason
as an issue comment; worker output remains only in the private log. Every pass prints its start, worker diagnostic, exit
status, and waiting state. The tmux shell stays open after a failure and
`pipe-pane` appends all output to the private log path printed by the wrapper.
An issue-level failure that the runner has successfully marked as
`agent::failed` exits `0`, so the loop proceeds to its next pass and can select
another issue. Only a runner-level blocker exits nonzero and stops the loop.
If another local
process holds `.agent-work.lock`, `run-next` exits `75`; the wrapper reports
that error and stops instead of quietly retrying against the held lock.

Stop the loop with `Ctrl-C`, or from another terminal:

```bash
tmux kill-session -t agent-work
```
