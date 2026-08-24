# Agentic loop plan

## Decision

Use a serial, GitLab-native loop. GitLab issues are the only intake source;
there is no filesystem inbox, webhook receiver, local queue, lease system, or
reconciliation service.

One scheduled invocation finds at most one eligible issue, runs the existing
bounded repository workflow in a dedicated checkout, and stops after it opens
or updates one merge request for human review. GitLab is the durable system of
record for the issue and merge-request handoff.

```mermaid
flowchart LR
    poll[Developer-compatible GitLab poll] --> issue[One ready issue]
    issue --> checkout[Dedicated checkout]
    checkout --> run[One bounded agent run]
    run --> mr[Feature branch and MR]
    mr --> human[Human approval and merge]
```

## Operating contract

- Select only an explicitly opted-in issue in the configured numeric GitLab
  project ID. The poller must address `projects/$GITLAB_PROJECT_ID`; it must
  never infer a project from `origin`.
- Polling is compatible with a GitLab Developer account and requires no
  webhook, inbound listener, or higher webhook-management role.
- Run serially: one `agent-work run-next --apply --json` invocation handles at
  most one issue and exits. A user-level systemd timer supplies repetition. Its
  nonblocking local lock returns exit `75` under contention; it is deliberately
  single-host and not a distributed lock or GitLab compare-and-set claim.
- Use a dedicated, agent-owned checkout rather than the operator's working
  tree. Issue content is untrusted task data and cannot alter repository,
  credential, sandbox, or approval policy.
- A successful mutation must push a feature branch and create its
  merge request, then stop at `awaiting_human`. The deterministic
  `agent/issue-<IID>` branch and exact same-project MR must match local/remote
  tips, the default target, and `Closes #IID`; otherwise the issue is failed.
  The agent never approves or merges.
- Unattended publication is off until a human explicitly authorizes it for a
  constrained service identity. Before enabling the timer, verify independent
  merge-request approval safeguards. If a Developer credential receives `403`
  while inspecting those Maintainer-only settings, obtain and retain a human
  Maintainer attestation instead of treating the check as passed.

## Delivery

1. Implement `agent-work run-next` as a bounded GitLab poller and runner with
   preview by default, `--apply` for mutation, and safe JSON output.
2. Configure one dedicated checkout, a numeric project ID, and a constrained
   service identity.
3. Install the single user oneshot service and timer. Leave its timer disabled
   until the unattended-publication authorization and approval-safeguard
   attestation are complete.
4. Enable the timer only for the authorized repository. Human reviewers retain
   approval and merge ownership for every resulting merge request.

## Explicitly deferred

- Filesystem intake and producer authentication.
- Webhooks and any inbound HTTP service.
- SQLite or another local/distributed work queue, leases, retries, dead-letter
  handling, outbox processing, and reconciliation.
- Concurrent workers, shared state, and multi-host coordination.
- Automatic merge-request approval or merge.
