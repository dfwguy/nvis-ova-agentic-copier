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

The command operates relative to the current Git checkout. It confirms
`git pull --ff-only`, reads `origin`, resolves the exact namespace path through
the GitLab projects API, then confirms the resulting numeric project endpoint.
All later endpoints use that confirmed numeric project ID. The command always
derives its workspace from the current directory. Failed Git or GitLab
confirmations do. Open,
non-confidential issues are eligible unless their labels show active or terminal
agent work; issue title, body, and labels are untrusted task data.

## Execution boundaries

| Boundary | Contract |
| --- | --- |
| Source | GitLab only; no filesystem inbox or webhook receiver. |
| Selection | At most one eligible open issue per invocation. |
| Workspace | The current repository checkout; its gitignored `.agent-work.lock` is relative to that checkout. |
| Concurrency | Serial; every local process using a checkout contends for its fixed, gitignored `.agent-work.lock`. A held lock is an error with temporary-failure exit `75`; start one tmux loop for the project. |
| Successful publication | Push one feature branch and create one MR, then return `awaiting_human`. |
| Human gate | The agent never approves or merges. |
| `glab` readiness | Effective API access is `max(project_access_level, group_access_level)` from numeric project API access values, ignoring nonnumeric values; it is `unknown` when neither is numeric and must be exactly 30 (Developer) for the unattended `--apply` path. |

The applied lifecycle is an open issue → `agent::running` →
`agent::awaiting-human` or `agent::failed`. Its branch is deterministically
`agent/issue-<IID>`. Before the human handoff, the runner verifies an exact MR:
the source and target project IDs equal the configured project, the source
branch is the deterministic branch, the target is the configured default
branch, and the MR SHA equals both local and remote branch tips. GitLab must
also report that the MR closes the selected issue, which the prompt requires as
`Closes #IID`. A missing or mismatched publication becomes `agent::failed`.
The runner posts its safe failure reason as an issue comment; redacted worker
diagnostics stay in the operator's private log.

The `agent::running` label is the GitLab lock. `agent::running`,
`agent::awaiting-human`, and `agent::failed` exclude an issue from selection.
After adding the lock, the runner
refetches all running issues and starts work only when its selected issue is
the sole lock. Do not start another tmux loop for the same project.

JSON output contains only `outcome`, `issue_iid`, `branch`, and
`merge_request_url`; it never includes issue text, prompts, command output, or
credentials. Exit `0` means idle, previewed, or verified for human review;
exit `1` means a safe failure or blocker; a held local lock exits `75`
(temporary failure); invalid command usage exits `2`. The interactive tmux loop
must print a visible pass state before it sleeps so an attached pane is never
blank.

## Unattended publication gate

Do not start the tmux Ralph loop for `--apply` until all of these are true:

- A human has explicitly authorized unattended publication for the repository.
- The loop uses a constrained identity and one checkout-scoped lock.
- The identity used by `glab` has effective API access exactly Developer
  (`30`) in the configured project.
- The effective default-branch endpoint reports the requested branch name as
  default and protected, with `developers_can_push`, `developers_can_merge`,
  and `can_push` all false.
- `approval_rules` has an at-least-one-approval global rule: its
  `protected_branches` value is omitted or a typed empty array, or
  `applies_to_all_protected_branches` is true. A branch-scoped rule does not
  satisfy this gate.

The effective access classification is a one-time readiness gate for this
constrained path. It must accept exactly 30 as Developer and reject `unknown`,
a value less than 30, and `40` or greater; rejected access must block `--apply`.
An access level of `40` or greater must switch to a constrained Developer
credential; it does not satisfy this gate by being more permissive. Any other
access value is rejected, which preserves the exactly-30 contract.

For the Developer fast path, the authoritative endpoint is
`projects/$GITLAB_PROJECT_ID/repository/branches/$default_branch_uri`, where
`default_branch_uri` is the `jq @uri` encoding of the configured default branch.
It must report the exact branch name, `default` and `protected` as true, and
`developers_can_push`, `developers_can_merge`, and `can_push` as false. This
observes the effective branch behavior, including settings derived from wildcard
or multiple protection rules. `approval_rules` must contain a numeric
at-least-one-approval global rule; branch-scoped rules are not evidence for
this gate. These are required observable safeguards, not a claim that the
Developer role alone proves project policy.

The selected readiness policy intentionally skips only the Maintainer-only
`/approvals` audit and recurring Maintainer attestation. A Developer cannot
read or prove author/committer self-approval or per-MR approval-rule-override
settings; those require Maintainer policy. The agent contract nevertheless
remains that it never approves or merges.

Developer access is not proof that the agent cannot push to `main`, merge, or
self-approve: Developers may approve, while project protections control push, merge, and self-approval. The agent contract remains that it never approves or merges.
The `glab` credential used for API access/MR creation and the Git credential
used for publication may differ; validate each identity and its access
separately.

The loop is started only by an operator who has satisfied this gate.

## Deferred capabilities

No filesystem intake, GitLab webhook, inbound HTTP service, local queue,
leases, retries, dead-letter state, reconciliation, multi-host coordination,
parallel worker, automatic approval, or automatic merge is part of this
contract. There is no legacy SQLite state to migrate.
