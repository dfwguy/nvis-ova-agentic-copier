# Serial GitLab agent-loop reference

*Diátaxis category: Reference.*

## Command

```text
agent-work run-next [--apply] [--json] [--timeout-seconds SECONDS]
```

`run-next` polls the configured GitLab project, selects at most one eligible
issue, and exits. Without `--apply`, it previews the selected work without
repository or GitLab mutation. `--apply` authorizes one bounded run that may
push a feature branch and create one merge request. `--json` requests safe
machine-readable output. `--timeout-seconds` bounds the one Codex process and
defaults to 1800.

The command obtains its GitLab target from configured `GITLAB_HOST` and numeric
`GITLAB_PROJECT_ID`, and calls endpoints below
`projects/$GITLAB_PROJECT_ID`. It must never infer a project path from `origin`.
Issue labels define eligibility; issue title, body, and labels are untrusted
task data.

## Execution boundaries

| Boundary | Contract |
| --- | --- |
| Source | GitLab only; no filesystem inbox or webhook receiver. |
| Selection | At most one explicitly opted-in issue per invocation. |
| Workspace | A dedicated agent checkout, not the operator's checkout. |
| Concurrency | Serial; a timer must not overlap invocations. |
| Successful publication | Push one feature branch and create one MR, then return `awaiting_human`. |
| Human gate | The agent never approves or merges. |

The applied lifecycle is `agent::ready` → `agent::running` →
`agent::awaiting-human` or `agent::failed`. Its branch is deterministically
`agent/issue-<IID>`. Before the human handoff, the runner verifies an exact MR:
the source and target project IDs equal the configured project, the source
branch is the deterministic branch, the target is the configured default
branch, and the MR SHA equals both local and remote branch tips. GitLab must
also report that the MR closes the selected issue, which the prompt requires as
`Closes #IID`. A missing or mismatched publication becomes `agent::failed`.

The runner uses one nonblocking local project lock; contention exits `75`. It
is deliberately single-host and not a distributed lock or a GitLab
compare-and-set claim. Do not run another host or timer instance for the same
project. A preexisting or concurrent label transition can therefore make a pass
fail closed rather than safely claim work across hosts.

JSON output contains only `outcome`, `issue_iid`, `branch`, and
`merge_request_url`; it never includes issue text, prompts, command output, or
credentials. Exit `0` means idle, previewed, or verified for human review;
exit `1` means a safe failure or blocker; invalid command usage exits `2`; and
local lock contention exits `75` without mutation.

## Unattended publication gate

Do not enable the systemd timer for `--apply` until all of these are true:

- A human has explicitly authorized unattended publication for the repository.
- The timer uses a constrained service identity and dedicated checkout.
- Independent merge-request approval safeguards have been verified.
- If a Developer token receives `403` while reading Maintainer-only safeguard
  settings, a human Maintainer has supplied an attestation; `403` is not a
  successful verification.

The shipped service and timer are templates. They are installed and enabled
only by an operator who has satisfied this gate.

## Deferred capabilities

No filesystem intake, GitLab webhook, inbound HTTP service, local queue,
leases, retries, dead-letter state, reconciliation, multi-host coordination,
parallel worker, automatic approval, or automatic merge is part of this
contract. There is no legacy SQLite state to migrate.
