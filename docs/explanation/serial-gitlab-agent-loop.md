# Why the agent loop is serial and GitLab-native

*Diátaxis category: Explanation.*

The agent loop has one source of work: eligible open GitLab issues in one
configured numeric project. A bounded poll selects at most one issue, prepares
one checkout, and drives the repository workflow until it opens or
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
listener or webhook-management requirement. The loop derives its target from
the current checkout's `origin`, confirms the exact namespace through GitLab,
then uses the confirmed numeric `projects/<id>` endpoint for all later calls.
An issue body remains untrusted task data, not authority to
change the checkout, credentials, sandbox, or repository policy.

The checkout-scoped lock and exact publication evidence make each run's branch
and publication state clear. A completed mutating run pushes a
feature branch and opens an MR, then hands off at
`awaiting_human`. The GitLab projection is an open issue to `agent::running`,
then `agent::awaiting-human` or `agent::failed`; the deterministic branch is
`agent/issue-<IID>`. The runner accepts a handoff only after exact same-project
MR, branch, default-target, local/remote tip, and `Closes #IID` verification.
The loop never approves or merges that MR. A later pass checks those open
handoffs for GitLab-reported merge conflicts; a conflicted MR is reclaimed on
its same deterministic branch for one remediation pass, then returns to human
review only after it is conflict-free again.

Serial is a deliberate local safety boundary, not distributed coordination. A
nonblocking local project lock is an explicit temporary failure: `run-next`
prints the held-lock error and returns exit `75` when another local run holds
it. Each project owns a user-level `<project-slug>-agent-work.timer`, which
starts one bounded service pass at a time. Its fixed randomized delay spreads
the initial activation for different repositories over a 60-second window.
The wrapper sends lifecycle and runner output through `logger` with the same
project-specific tag, which operators can inspect with `journalctl --user -t
<project-slug>-agent-work`. Linger keeps that user service manager available after logout, so
operators enable it with `sudo loginctl enable-linger "$USER"` and verify
`Linger=yes`. There is no GitLab compare-and-set claim, so a second host or
timer can race label changes; operators must run exactly one timer instance per
project.

Unattended publication is an explicit, one-time operational decision, not an
effect of installing a timer. Its least-privilege readiness signal is that the
`glab` identity has effective API access exactly Developer (`30`) in the
configured project. That value is
`max(project_access_level, group_access_level)` from the project API response,
with nonnumeric values ignored. If neither access value is numeric, the
classification is `unknown`; that result, a value less than 30, and 40 or
greater must block `--apply`. Exactly 30 is the only accepted Developer
readiness value. A Maintainer-or-higher identity (40 or greater) is deliberately
not substituted for the constrained path; use a constrained Developer credential
instead.

The fast path avoids only the inaccessible Maintainer-only `/approvals` audit
and recurring attestation; it does not infer that a role proves all policy.
Instead, it uses safeguards a Developer can read: the numeric API's
effective branch endpoint must report the exact default branch as default and
protected, with `developers_can_push`, `developers_can_merge`, and `can_push`
all false. Unlike a collection of protection rules, that result represents the
effective behavior after GitLab applies wildcard or multiple rules. An
`approval_rules` rule must require at least one approval and be global: it must
apply to all protected branches or omit `protected_branches` or provide a typed
empty `protected_branches` array. Branch-scoped rules are not evidence for this
workflow.

This is intentionally narrower than a full policy audit. A Developer cannot
read or prove author/committer self-approval or per-MR approval-rule-override
settings, which require Maintainer policy. The selected readiness policy skips
those inaccessible checks rather than treating a repository document as live
evidence. Its runtime boundary remains independent: the loop never approves or
merges, even where GitLab might permit a Developer to approve.

This is a readiness signal, not a proof that every GitLab control is safe.
Developers may approve, and project protections control push, merge, and self-approval.
The loop's own boundary is narrower and remains enforceable by its workflow:
it only publishes feature branches and never approves or merges an MR. Also,
`glab` and Git can select different credentials, so API/MR identity and Git
publication identity must each be intentionally constrained and checked.

Filesystem intake, webhooks, durable local queues, retry/reconciliation
machinery, multiple workers, and automatic approval or merge are intentionally
deferred. No legacy SQLite migration is required. See the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md)
for the operator-facing contract.
