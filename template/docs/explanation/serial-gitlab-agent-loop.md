# Why the agent loop is serial and GitLab-native

*Diátaxis category: Explanation.*

The agent loop has one source of work: explicitly opted-in GitLab issues in one
configured numeric project. A bounded poll selects at most one issue, prepares
one dedicated checkout, and drives the repository workflow until it opens or
updates a merge request for human review.

```mermaid
flowchart LR
    issue[Ready GitLab issue] --> poll[One bounded poll]
    poll --> checkout[Dedicated checkout]
    checkout --> workflow[Agent workflow]
    workflow --> mr[Feature branch and MR]
    mr --> human[Human approval and merge]
```

GitLab already records the issue and merge-request handoff. Keeping that
boundary avoids a second queue, filesystem inbox, webhook endpoint, and the
failure-recovery machinery those systems need. The service is deliberately
serial: one `run-next` invocation processes at most one item and exits, while a
user-level systemd timer decides when to start another invocation.

Polling works with ordinary Developer access to the project. It has no inbound
listener or webhook-management requirement. The configured `GITLAB_HOST` and
numeric `GITLAB_PROJECT_ID` are authority inputs; the loop must use
`projects/$GITLAB_PROJECT_ID` and must not infer a project from Git remote
configuration. An issue body remains untrusted task data, not authority to
change the checkout, credentials, sandbox, or repository policy.

The dedicated checkout protects the operator's own working tree and makes each
run's branch and publication evidence clear. A completed mutating run pushes a
feature branch and opens an MR, then hands off at
`awaiting_human`. The GitLab projection is `agent::ready` to `agent::running`,
then `agent::awaiting-human` or `agent::failed`; the deterministic branch is
`agent/issue-<IID>`. The runner accepts a handoff only after exact same-project
MR, branch, default-target, local/remote tip, and `Closes #IID` verification.
The loop never approves or merges that MR.

Serial is a deliberate local safety boundary, not distributed coordination. A
nonblocking local project lock returns exit `75` when another local run holds
it. There is no GitLab compare-and-set claim, so a second host or timer can
race label changes; operators must run exactly one timer instance per project.

Unattended publication is an explicit operational decision, not an effect of
installing a timer. It requires a constrained service identity and confirmed
independent MR approval safeguards. A Developer account may receive `403` for
the Maintainer-only safeguard settings; that result is inconclusive, so a human
Maintainer must attest to the required protections before the timer is enabled.

Filesystem intake, webhooks, durable local queues, retry/reconciliation
machinery, multiple workers, and automatic approval or merge are intentionally
deferred. No legacy SQLite migration is required. See the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md)
for the operator-facing contract.
