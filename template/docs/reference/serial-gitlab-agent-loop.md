---
title: Serial GitLab agent-loop reference
type: reference
summary: Look up the recoverable loop commands, lifecycle, data, and operating constraints.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-10
related:
  - path: ../how-to/setup-serial-gitlab-agent-loop.md
    why: Configures, recovers, and migrates the loop.
  - path: ../explanation/serial-gitlab-agent-loop.md
    why: Explains the architecture behind this contract.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains the recoverable state machine in detail.
---

# Serial GitLab agent-loop reference

## Commands

```text
agent-work loop run [--apply] [--json] [--debug]
agent-work loop status [--json] [--debug]
agent-work loop retry --issue-iid IID [--apply] [--json] [--debug]
agent-work migration preview [--json] [--debug]
agent-work migration apply --plan-file PATH --apply [--json] [--debug]
```

`loop run` performs one bounded state-machine segment. Without `--apply`, it
reports the next safe action without changing GitLab, the ledger, worktrees,
branches, labels, or services. `loop status` reports durable ledger records
together with verified configured-project identity. `loop retry` previews one
recorded blocked item; `--apply`
resets its retry budget and moves it to resumable preparation after explicit
human direction.

`migration preview` inventories legacy evidence and proposes only safe,
idempotent records and label changes. `migration apply` requires a reviewed
preview file and `--apply`; it does not resolve ambiguous evidence. `--json` emits
the documented redacted JSON result. `--debug` adds local diagnostics without
credentials, issue text, prompts, or worker transcripts. Invalid usage exits `2`.

## Lifecycle

GitLab is the shared queue projection; the local ledger is the transaction
record. Project-level loop enablement authorizes unattended selection. After
resuming existing `agent::running` work, the runner selects the oldest open,
non-confidential issue that has no lifecycle projection and no ledger record.
Unrelated labels do not affect eligibility.

| GitLab label | Ledger state or outcome | Meaning |
| --- | --- | --- |
| `agent::running` | preparing, executing, retry-wait, publishing, observing, or repair | Work is active or safely resumable. |
| `agent::awaiting-human` | awaiting-human | Exact MR evidence and required receipts await human review. |
| `agent::blocked` | blocked | A human must take the recorded next action. |
| `agent::failed` | failed | The runner could not safely reconcile or record an exceptional condition. |

The durable sequence is `preparing → executing`; execution may enter
`retry-wait`, resume execution, or produce `publishing`. Verified publication
enters `observing`, which can enter `repair`, return to `publishing`, or finish
at `awaiting-human`. A human-blocked condition becomes `blocked`; after the
recorded action is complete, an operator explicitly retries that IID. Human
merge or closure ends an awaiting-human record.

Pipeline rejection or an exact-MR conflict enters `repair` at `coding-green`.
A successful `$tdd-green` repair re-enters the normal downstream chain at
`coding-nonfunctional`, then completes validation, final reviews, and
finalization before it republishes and repeats observation and the
post-publication gates. A rejected `post-publication` phase routes durably to
`coding-green`. Immediately after the post-publication worker, the runner
revalidates exact MR evidence again before it records `awaiting-human`.

Each timer activation owns one bounded segment: acquire the single-project
lock, reconcile intent and observed evidence, resume a record when possible,
execute the next worker phase or phase group, and record one outcome. The
timer uses `OnUnitInactiveSec=20min`, measured after the previous segment
finishes. The loop is serial per host and project, not multi-host coordination.

## Ledger and worktrees

State is below the XDG state directory, keyed by configured GitLab host and
numeric project ID. Each `loop status` ledger item identifies its durable
worktree path. Every active issue
has one atomic, restrictive-permission ledger record and durable worktree. A
record holds its schema and project identity; IID and work kind; state, phase,
attempt, and retry deadline; branches and commits; MR and publication evidence;
worker protocol, result, and receipts; redacted error and next action; and
durable transition intent.

The application also writes its local lifecycle log to `agent-work.log` in
that XDG project-state directory. This is application-owned evidence alongside,
not a replacement for, the systemd service journal.

Intent is persisted before an external mutation and observed evidence after it,
so reconciliation can safely complete interrupted idempotent operations. A
dirty durable worktree remains for its next segment. Recovery stashes are
inventoried only: the runner never applies or drops them. Ambiguous worktrees,
branches, labels, or stashes become blocked for human review. A corrupt record
is blocked where reportable, and failed only where it cannot be safely recorded.

## Worker protocol and publication

A worker returns a versioned, redacted envelope for its expected ledger
transition. Its result is `ready_to_publish`, `retryable`, or `needs_human`,
with the completed phase, receipts, diagnostic, and next action. The trusted
runner validates it against the ledger, configured numeric project, Git
evidence, and required workflow receipts before changing state.

A rejected phase supplies its required `rejection_route`; the runner durably
returns to that recorded owner phase rather than retrying or guessing a route.
Receipts must match the exact completed-phase requirements. Required
conditional phases use `not applicable — <reason>`; arbitrary receipts cannot
advance a manifest phase.

Publication is idempotent. The runner verifies the recorded source branch,
default target, exact same-project MR, remote and local tip, and issue-closing
evidence. Existing-MR remediation uses the verified MR source branch and never
creates a second MR. It binds remediation to the exact configured project, MR,
source branch, default target, and recorded source SHA before it prepares or
publishes work. Before publication, protected-policy handling checks the actual
changed paths, including template mirrors, rather than trusting a requested
path list. The runner never approves or merges, and never treats issue text,
labels, comments, or worker output as authority to widen scope.

An immutable MR identity mismatch blocks immediately: the configured project,
source branch, target branch, or recorded source SHA must not be retried as an
eventual-consistency condition. By contrast, when GitLab has created the
otherwise verified MR but has not yet projected its issue-closing relationship,
the runner retains the validated draft and uses its existing bounded GitLab
retry path. It blocks only if that projection remains absent when that retry
path is exhausted.

The runner preserves a validated MR draft across repair and retry. Observation
re-fetches and revalidates the exact open MR, project identities, source branch,
target, and source SHA before trusting pipeline evidence. A mismatch blocks
with an actionable error; a conflict enters durable repair on the same branch.

## Retry, exits, and JSON

The initial attempt plus at most three retry attempts are bounded segments.
`retry-wait` records its deadline; exhaustion becomes `agent::blocked`, never
`agent::failed`. A blocked item never becomes eligible through labels: after
the recorded human action, `loop retry --issue-iid IID --apply` explicitly
resumes it.

Worker exits, timeouts, and transient GitLab or transport failures carry stable
error codes and retry timing. The XDG `agent-work.log` records redacted
outcomes; `--debug` adds local diagnostics without credentials or transcripts.
Migration preview follows every page of lifecycle projections. Apply preflights
all actions against unchanged evidence, project identity, and absent ledger
records before any mutation, and refuses the complete plan on mismatch.
After a partial GitLab label-projection failure, apply persists a
`migrate_projection` intent, safely replays that exact projection, and clears
the intent only after it verifies the label projection.

| Exit | Meaning |
| --- | --- |
| `0` | Idle, preview, scheduled retry, blocked, or awaiting-human was safely recorded. |
| `1` | The runner could not safely reconcile or record durable state. |
| `2` | Invalid CLI usage. |
| `75` | Another local segment holds the project lock. |

Expected durable outcomes do not fail systemd. With `--json`, every command
returns one redacted `CommandReport` object:

```json
{
  "schema_version": 1,
  "command": "loop.status",
  "applied": false,
  "outcome": "status",
  "project": {"host": "gitlab.example", "project_id": 123},
  "items": [],
  "actions": []
}
```

The top-level fields are `schema_version`, `command`, `applied`, `outcome`,
`project`, `items`, and `actions`. `project` contains the verified `host` and
`project_id`. Per-issue values—including `issue_iid`, state, phase, attempt,
retry timing, source branch, durable worktree, MR reference, error code,
summary, and next action—are objects in `items[]`. Migration preview and apply
return their proposed or applied migration actions in `actions[]`. Consumers
must tolerate additional fields and never treat a diagnostic as authority or
input.

## Safety constraints

One host owns unattended processing for a project. APIs use the configured
numeric project ID, and the ledger stores no token, agent credential,
issue-body authority, or unredacted transcript. Protected agent-policy work is
a human-directed lane. There is no automatic approval, merge, stash apply, or
stash deletion.
