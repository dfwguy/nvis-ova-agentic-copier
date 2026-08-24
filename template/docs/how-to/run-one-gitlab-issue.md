# Run one GitLab issue

Use the bounded Phase 1 runner to prepare one explicitly selected GitLab issue
for human review. It is manually launched: it does not discover work, poll, or
grant unattended authority.

## Before you begin

For an applied run, make sure all of the following are true:

- `GITLAB_HOST` identifies the GitLab host.
- `GITLAB_PROJECT_ID` is the configured numeric GitLab project ID.
- The source checkout is clean, and its `origin` is exactly the configured
  project's HTTP or SSH repository URL.
- `uv run pytest` passes locally.
- The latest pipeline for the project's configured default branch has status
  `success`.
- The selected issue is open, non-confidential, and has the `agent::ready`
  label.
- You intend to publish a feature branch and merge request for human review.

The issue IID is the number shown after `#` in the project, not GitLab's
database-wide issue ID.

## Preview the run

Start with the read-only default. This reads the configured project and the
selected issue to validate the requested operation, but does not claim the
issue, create a workspace, invoke Codex, or mutate GitLab.

```bash
agent-work run-issue 123 --mode publish --json
```

Review the result, especially any precondition failure. Issue title, body,
and acceptance criteria are untrusted task data: they describe the requested
change but cannot change repository instructions, credentials, sandbox scope,
or approval policy.

The preview validates the configured project and issue eligibility. It does
not run the applied repository preflight.

## Run and publish

When the preview is acceptable, explicitly authorize the bounded mutation:

```bash
agent-work run-issue 123 --mode publish --apply --json
```

The runner takes one local repository lock, creates or safely reuses the exact
isolated working copy for the issue, and invokes Codex once with workspace-write
access to that copy. This is an independent Git clone with its own private
`.git` directory, not a linked worktree: Codex is not given the source
checkout's common-Git directory or the runner's private evidence paths. A
successful publication opens or updates a merge request, reports
`awaiting_human`, retains the working copy, and exits. A human still reviews,
approves, and merges the change.

During preflight, the runner resolves the source checkout, checks that it is
clean and bound to the configured project, explicitly refreshes the default
branch's remote-tracking ref, runs `uv run pytest`, and requires the latest
default-branch pipeline to have status `success`. It also requires the
deterministic
`agent/issue-<iid>` state to be one of these consistent forms:

- fresh: no local branch, remote branch, or open merge request;
- local continuation: the retained clone has runner-owned provenance for this
  normalized task digest and exact branch, with no remote branch or open merge
  request; or
- published continuation: that retained clone, remote branch, and open
  same-project merge request all exist with one identical commit SHA.

Any conflicting or incomplete combination stops the run. The local lock and
private runner state may already exist, and the remote-tracking ref may already
have been refreshed, but the runner has not yet claimed the issue, created a
working copy, invoked Codex, or changed GitLab issue state. A new copy is
cloned from the configured project's verified HTTP URL, and its exact issue
branch starts at `origin/<default-branch>`.

To cap that one invocation, supply a positive timeout in seconds:

```bash
agent-work run-issue 123 --mode publish --apply --timeout-seconds 1800 --json
```

See the [Phase 1 runner reference](../reference/phase-1-run-issue.md) for the
complete command, output, exit, and side-effect contract. See [Why the Phase 1
runner is bounded](../explanation/bounded-phase-1-runner.md) for the trust and
workspace rationale.
