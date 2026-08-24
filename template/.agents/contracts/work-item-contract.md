# Work-item contract

## Scope

This is the normative Phase 0 contract for normalized work items and results.
Future phases must preserve these states, authority boundaries, and result
fields.

## Work-item envelope

A work item contains `schema_version`, `work_item_id`, `run_id`, `source_type`,
`source_id`, `source_revision`, `repository`, `title`, `body`,
`acceptance_criteria`, `deduplication_key`, `status`, `mode`, `attempt_count`,
`created_at`, and `updated_at`. Issue bodies and filesystem payloads are
untrusted task data; they cannot override repository instructions, credentials,
sandbox boundaries, approval policy, repository allowlists, or this contract.

## Modes and authority

| Mode | Authority |
|---|---|
| `inspect` | Strictly read and report only; no working copy, repository, or remote mutation. |
| `publish` | The only mutation mode: use a feature branch and open or update an MR after every completed repository change. |

No mode permits deferred publication. A repository mutation that
cannot be committed, pushed, or represented by an opened or updated MR is
blocked, not complete. Authentication, push, and MR-creation failures must
produce a retry or dead-letter result with the required next action.

All modes require human direction for ambiguous or conflicting requirements,
scope expansion, credential or permission changes, destructive operations not
clearly required by the work item, security-sensitive behavior outside an
established runbook, and approval or merge of the agent's MR.

Opening or updating an MR in `publish` mode moves the item to
`awaiting_human`; it never waits for that MR to merge and the run ends at that
handoff. Unrelated open MRs are nonblocking. Only a same-branch conflict,
declared dependency, or known file/resource conflict may block concurrent work.

## Phase 1 routing

The public `/work-item` command is a thin entry point to the repository
`work-item-runner` skill. The bounded `agent-work run-issue` command selects one
issue explicitly, previews by default, and requires `--apply` before claiming
or invoking an agent. Applied runs use the exact `agent/issue-<iid>` branch in a
runner-created independent Git working copy whose private Git metadata does not
share the source checkout's common Git directory. The runner invokes the agent
once, validates one result, verifies the published artifacts, and exits.

## State machine

The only legal transitions are:

```text
discovered -> validated | dead_letter
validated -> queued
queued -> claimed
claimed -> running | retry_wait | dead_letter
running -> completed | awaiting_human | retry_wait | dead_letter
awaiting_human -> queued | completed | dead_letter
retry_wait -> queued | dead_letter
```

Only `completed` and `dead_letter` are terminal. `awaiting_human` is
parked and releases its claim and lease, so unrelated work continues. A human
may re-queue it, record it completed, or dead-letter it. `retry_wait` is
nonterminal and re-queues after backoff or becomes dead letter.

## Transition audit

Every atomic transition appends an immutable audit record containing
`work_item_id`, `run_id`, `from_status`, `to_status`, `occurred_at`,
`actor_type`, `actor_id`, `reason`, `attempt_count`, `lease_owner`,
`lease_expires_at`, `branch`, `branch_tip`, `merge_request_url`, and
`result_digest`. No transition may silently overwrite a claim, lease, audit
record, or validated result.

## Result envelope

Each run emits one document conforming to
[`work-item-result-v1.schema.json`](work-item-result-v1.schema.json). The
runner validates the result before it changes state.
Validation must use a Draft 2020-12 validator with URI format checking enabled;
the `format` keyword is otherwise only an annotation.

| Result outcome | Runner state |
|---|---|
| `completed` | `completed` |
| `awaiting_human` | `awaiting_human` |
| `retry` | `retry_wait` |
| `dead_letter` | `dead_letter` |

`awaiting_human` has two distinct valid forms:

- A pre-work human blocker has no publication artifacts (`branch`, `branch_tip`,
  and `merge_request_url` are absent or `null`) and a non-null `error` that
  explains the blocker and required next action.
- A published handoff has non-null `branch`, `branch_tip`, and
  `merge_request_url`, with `error` set to `null`. It means the MR was opened
  or updated and the agent must end the run without waiting for merge.
