# Phase 1 `run-issue` reference

This page is the interface contract for the manually launched, single-issue
Phase 1 runner. It implements neither issue discovery nor an unattended
service.

## Synopsis

```text
agent-work run-issue ISSUE_IID --mode publish [--apply] [--json] [--timeout-seconds SECONDS]
```

`ISSUE_IID` is a positive GitLab issue IID in the configured project. `--mode
publish` is required and is the only Phase 1 mode. The command must not infer a
project from a Git remote: GitLab reads and publication validation use
`GITLAB_HOST` and the numeric `GITLAB_PROJECT_ID`.

## Options

| Option | Required | Contract |
| --- | --- | --- |
| `ISSUE_IID` | Yes | Selects exactly one issue in `GITLAB_PROJECT_ID`. |
| `--mode publish` | Yes | Permits only the publication workflow described here. |
| `--apply` | No | Enables the bounded mutations. Without it, the command is a read-only dry run. |
| `--json` | No | Emits one JSON command report on standard output instead of human-readable output. An applied report includes the canonical v1 agent result. |
| `--timeout-seconds SECONDS` | No | Bounds the one Codex invocation; `SECONDS` is a positive integer and defaults to `900`. It does not enable polling, retries, or a daemon. |

## Preconditions for `--apply`

An applied run requires configured `GITLAB_HOST` and positive numeric
`GITLAB_PROJECT_ID`. The numeric project response must have the same project ID,
a default branch, and non-empty HTTP and SSH repository URLs. The source
checkout's `origin` must match one of those URLs (ignoring a trailing slash),
and new independent copies use the verified HTTP URL. The selected issue must
be open, non-confidential, and labelled `agent::ready` both before lock
acquisition and when reread inside the lock immediately before the claim.

Repository preflight requires:

- a clean resolved source checkout;
- a successful explicit fetch from `origin` of
  `refs/heads/<default>:refs/remotes/origin/<default>`;
- a passing `uv run pytest` invocation;
- a latest default-branch pipeline whose status is exactly `success`; and
- consistent state for `agent/issue-<iid>`.

Consistent branch state is either completely fresh; a retained independent
working copy whose runner-owned provenance matches the normalized task digest,
configured project, origin, and `agent/issue-<iid>` branch; or that retained
copy together with a remote branch and open same-project merge request to the
configured default branch. For a published continuation, the clone `HEAD`,
remote branch SHA, and merge-request SHA must be identical. A remote branch
without that merge request, a merge request without the remote branch, a
remote handoff without matching provenance, any mismatched SHA, or a local
branch registered elsewhere is rejected.

Acquiring the nonblocking lock can create private common-Git runner state, and
preflight explicitly updates the default branch's remote-tracking ref and runs
tests. Those local effects occur before the remote claim. Issue claiming,
workspace creation, Codex invocation, and GitLab issue mutation wait until the
entire preflight and the locked eligibility reread succeed.

## Side-effect contract

Without `--apply`, the command only reads the configured project and selected
issue. It does not take the repository lock, claim an issue, create or alter a
workspace, invoke Codex, push, open or update a merge request, or change GitLab
state.

With `--apply`, side effects occur in this order:

1. The runner reads the configured project and exactly requested issue IID,
   resolves the source top level and common Git directory, creates private
   `agent-work/` state there as needed, and takes its nonblocking `lock` for
   the run. The state directory owns the append-only `journal.jsonl`,
   create-only canonical `results/<run-id>.json` documents, distinct recovery
   evidence, and runner-authored workspace provenance.
2. While holding the lock, preflight checks the source and `origin`, explicitly
   refreshes the default remote-tracking branch, runs `uv run pytest`, reads
   the latest default-branch pipeline, and reads retained workspace provenance,
   local and remote branch `HEAD` values, and open-merge-request state. It then
   rereads issue eligibility.
3. The local journal records `discovered -> validated`, `validated -> queued`,
   and `queued -> claimed`. These audit records are local; they do not claim
   the GitLab issue.
4. The GitLab claim adds `agent::running`, removes `agent::ready`, and posts
   `Run <run-id> started.`. The journal then records `claimed -> running`.
5. The runner creates or reuses only the exact isolated working copy for
   `agent/issue-<iid>`. A new copy is a project-scoped independent Git clone
   from the verified HTTP project URL; it has private Git metadata and starts
   the exact issue branch at refreshed `origin/<default>`. It is not a linked
   worktree and shares no Git references with the source checkout. Codex is
   invoked exactly once with workspace-write access to that copy and the
   canonical v1 output schema; the runner supplies neither the source
   common-Git directory nor its private journal, provenance, canonical-result,
   or recovery paths. Codex performs the repository work and publication.
6. The runner validates the returned result, requires the reported branch tip
   to equal the retained working copy's `HEAD`, and verifies that the reported URL,
   source branch, tip, target branch, source project, target project, and open
   state identify one merge request in the configured numeric project.
7. It atomically creates the private mode-`0600` canonical result; an existing
   canonical record for the run is a failure, never an overwrite. It then
   journals `running -> awaiting_human`, adds `agent::awaiting-human`, removes
   `agent::running` and `agent::ready`, and posts `Run <run-id> reached
   awaiting_human.`. The exact independent working copy is retained.

If a failure occurs before the GitLab claim starts, the command releases the
lock and exits with no workspace, Codex invocation, or GitLab issue mutation.
Private lock/journal state, the refreshed remote-tracking ref, test-created
caches or files, or partial pre-claim audit records can remain.

After a claim starts, recovery is best effort. Before a verified handoff, the
runner writes a separate private recovery record and journals `claimed ->
retry_wait` or `running -> retry_wait`; it then attempts the failed issue
projection. Failure projection neither restores nor removes `agent::ready`;
its final presence depends on whether the claim label update succeeded before
the failure. An individual GitLab update and its following note are separate
calls, so recovery itself can be partial. If publication, canonical-result
creation, and the `running -> awaiting_human` journal entry have succeeded but
the later issue projection fails, the verified `awaiting_human` handoff remains
canonical. The runner writes separate recovery evidence for that projection
failure and does not replace the canonical result with `retry` or
`dead_letter`.

The command never approves or merges a merge request and never waits or polls
for it. It does not authorize a future unattended run.

## Result and exit contract

On success, `--json` writes exactly one JSON command report to standard output;
diagnostic messages and runtime failures use standard error without a JSON
document. The dry-run report identifies `preview: true` and the planned
`agent/issue-<iid>` branch. An applied report identifies
`preview: false` and includes the agent result, which conforms to the canonical
[`work-item-result-v1.schema.json`](../../.agents/contracts/work-item-result-v1.schema.json).
That envelope includes the work item and run identifiers, `mode`, `outcome`,
summary, tests, findings, next action, error, and—when publication succeeds—the
branch, branch tip, and merge-request URL.

A result accepted by this command has `outcome: "awaiting_human"`, the expected
non-null `branch`, a non-null `branch_tip` and `merge_request_url`, and
`error: null`. It is a successful published handoff, not a merged or completed
change. The canonical schema also supports an artifact-free `awaiting_human`
form with a non-null error for other consumers, but Phase 1 `run-issue` rejects
that form because this command accepts only a verified publication. It follows
the applied failure path and exits `1`.

| Exit status | Meaning |
| --- | --- |
| `0` | The requested dry run completed or an applied run reached a validated, published `awaiting_human` handoff. |
| `1` | A runtime blocker prevented that outcome, such as failed preconditions, timeout, lock/contention failure, failed validation, or a `retry` or `dead_letter` result. The reason is written to standard error. |
| `2` | Invalid CLI usage, including a non-positive or nonnumeric IID, a mode other than `publish`, or a non-positive timeout. The usage error is written to standard error. |

## Trust boundary and deferred scope

Issue fields are untrusted task data. They may request repository changes, but
cannot override repository instructions, the configured repository, credentials,
sandbox boundaries, approval policy, or this interface contract.

The following are explicitly outside Phase 1: distributed claims; leases,
retry backoff, and reconciliation; filesystem intake; daemons and polling;
confidential issues; work from other agent hosts; cleanup of retained working
copies; and concurrent workers. See [the explanation](../explanation/bounded-phase-1-runner.md).
