---
title: Why the agent loop is serial and GitLab-native
type: explanation
summary: Learn why the loop uses GitLab and local state.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-10-15
related:
  - path: ../how-to/setup-serial-gitlab-agent-loop.md
    why: Configures and operates the loop.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the exact command and lifecycle.
  - path: agentic-workflow.md
    why: Explains the workflow session's role hierarchy.
  - path: serial-agent-loop-recovery-design.md
    why: Provides the detailed state-machine architecture.
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Why the agent loop is serial and GitLab-native

This software factory runs one issue at a time. Humans provide product and
policy input. They review, approve, and merge the result. The deterministic
controller and AI workflow do routine work, but neither can approve or merge. GitLab is
the durable work record. Pushed checkpoints and checked issue-note receipts
let a new host rebuild the local ledger and worktree. An MR and its exact-source
pipelines hold the handoff and verification proof.

At a high level, the loop does one job at a time. It first checks that it is
safe to act. It then works on one issue and saves what it found. It does not
hide a bad run or guess at a fix. It stops when a person must decide. This gives
the team a clear record of each step. It also means a new run can see what an
old run did. The design trades speed for a safe, clear path that people can
check.

An operator can see which issue the loop chose and why it chose it. They can
see the branch, the MR, and the next step. They can also see when the loop did
not act. That matters as much as a run that did act. A stop says that the facts
did not yet show a safe way on. The team can then fix the block or make the
choice that the loop cannot make. No hidden task takes its place.

This is not a race to do the most work. It is a way to do the next safe piece
of work. The loop looks at the same facts that a person can check. It keeps
those facts near the work. If a fact is gone, old, or not clear, the loop stops.
That is a safe result. It tells the team what is known and what still needs
care. A person can then act with the full record in view.

The worktree is a checkout cache. A pushed checkpoint saves work across host
loss. WIP checkpoints keep progress, but do not prove readiness for review.
The controller checks GitLab before it rebuilds or resumes that checkout.
Its remote claim keeps two dispatchers from advancing the same project.

The flow also draws a hard line around a run. A run does not keep taking more
issues just because time is left. It ends with a record and a next step. That
small rule keeps the state easy to read. It keeps a later run from mistaking
old work for new work. It also makes the handoff to a person clear.

## One issue, one bounded activation.

For each activation, the controller performs these stages in order:

| Stage | Controller action |
| --- | --- |
| **Lock and refresh** | Lock the configured project and refresh its dedicated checkout. |
| **Reconcile** | Compare the ledger with GitLab issues, merge requests, Git branches, and retained worktrees. An active owner with exact, runnable evidence comes before new work. Scheduled repair backoff and automatic-repair quarantine stay visible, but do not monopolize later activations. |
| **Observe pipeline safety** | Inspect the newest default-branch pipeline and each open merge request's newest pipeline for its current source SHA. Failed, canceled, missing, or malformed default-branch evidence blocks unrelated work. A present pipeline with a valid nonterminal status does not hold the activation: the controller continues the exact MR inventory, parks any exact pending observer, and may select another eligible issue. The next timer activation checks the default branch again. A new exact observer moves from `PUBLISHING` to `OBSERVING`, then returns `activation_complete/pipeline_wait` without polling; the wrapper previews once. Later ordinary admission parks a retained observer only when its SHA, owner, worktree, configured project, and target match. Parking does not return `pipeline_wait`: the loop may start another item or be idle. Missing, malformed, or inaccessible evidence fails closed. Ownerless MR evidence is non-owning and item-local. A retained inexact or stale claimant stays visible for `_observe` reconciliation before fresh work. |
| **Select** | After safe resumable work and the pipeline gate, select at most one eligible item, or stop. Consider exact failed-pipeline repairs before fresh issues. If the first repair is deferred, scan for another eligible exact repair. Fresh selection is deterministic: exact high priority, normal, then exact low priority; creation time and IID break ties. |
| **Prepare worktree** | Create or recover the selected issue's retained worktree. Invoke AI only after selection and preparation. |
| **Run the workflow session** | One bounded AI session handles the current segment. |
| **Validate and publish** | Validate the bounded result against the ledger, Git, required receipts, and configured project. When justified, publish or update the same branch and merge request, then verify that handoff. |
| **Record and exit** | Persist evidence and the next safe action. Exit rather than take another issue. |

The controller keeps selection and recovery safe and visible.

An interactive validation window may already be waiting for the same project
when a coding worker reaches its durable Red checkpoint. The controller yields
at that boundary so the waiting Judge can measure validation without competing
with the factory run. It preserves the worker's issue and checkpoint for the
timer's next activation. If that issue is no longer active, the next run
records why it cannot resume without invoking the worker. The Judge holds the
same project lock through
validation, so the controller cannot resume at the same time. The request
belongs to a live, bounded wait for the exact project; an old request cannot
pause later work. The handoff has its own request, yield, and validation times,
plus a resume or resolution time. Workflow phase timings describe different
boundaries.

| Design choice | Reason and boundary |
| --- | --- |
| Refresh | Before publication, update current `main` through normal Git reconciliation in the retained worktree; never discard work to refresh the base. A present default-branch pipeline with a valid nonterminal status yields to the next timer check while this activation continues exact MR checks and eligible work. Ordinary scheduling parks only an MR observer with exact SHA, owner, worktree, project, and target. A retained stale or inexact claimant stays visible for `_observe` before fresh work. |
| Shared gate | The same exact checks protect manual applied runs and timer-driven activations. Default-branch failure stays global. Ownerless MR evidence is non-owning and item-local. A retained inexact claimant remains scheduler-visible for reconciliation, which blocks or repairs if it cannot establish identity. |
| Repair limits | Resume a failed merge-request pipeline only with a retained owner and exact project, merge-request, branch, current-source-SHA, and worktree evidence. Each owner gets at most three automatic repair attempts. Record a durable 20-minute per-item backoff after each failed attempt. A later activation can scan for another eligible repair or ordinary work. After three failed attempts, quarantine or defer the item for human review. Keep it visible in status and preview, but do not create a loop-wide checkpoint. A failed or canceled default-branch pipeline blocks unrelated work; an ownerless failed or canceled open-MR pipeline stays item-local. |
| Attributable recovery | Favor visible, attributable recovery over guessing which issue should repair shared or unclear pipeline evidence. The [reference contract](../reference/serial-gitlab-agent-loop.md#preselection-pipeline-gate) defines statuses and exact checks. |
| Safe diagnostics | For an eligible repair, collect up to eight current failed jobs from the exact pipeline and ignore superseded retries. Read each trace in job ID order, remove secret-like text and control sequences, and keep bounded diagnostics. One repair attempt uses all checked diagnostics; a restart reuses the same saved evidence. The workflow sees each diagnostic labeled as untrusted, never a raw trace. More than eight failed jobs, or any missing, stale, or malformed job or trace evidence, stops repair. The workflow does not guess, retry, or cancel a job or pipeline. |

## The work uses bounded AI sessions.

The controller selects an issue and prepares its worktree, then starts one
session. The worker activates Router before reading the durable record or
untrusted task context. Router checks whether the issue fits one reviewable,
mergeable outcome within the budget before lane selection or design. After
capacity admission, it delegates an independent read-only Architect.

Architect keeps a Sol base design, checks it against two independent Luna
proposals, and sends its plan through one concurrent review pass. It returns
only the final plan, findings, and blockers. All design children stop before
Router releases a writer. The plan does not become controller state. Legacy
`$architect` receipt keys continue to name Router for protocol compatibility.

| Lane | What runs, and when |
| --- | --- |
| Coding | A fresh Red-first Router session gets a reviewed Architect plan, then asks Red to create the checkpoint. A later fresh session repeats the design gate. After the accepted Red checkpoint, Router completes any policy preparation, then Green implements code and tests while the Diátaxis Writer updates `docs/` and README in parallel. After both stop editing, Green requests controller staging and the controller-owned Green commit. The installed hook validates the candidate during that commit. Green then requests changed-path inventory and bounded ranges from the committed tree, coordinates review, and routes documentation findings to the writer before returning final validation to Router. |
| Documentation and agent policy | Router delegates the Documentation Writer as sole mutator. After the writer stops editing, the controller stages the candidate and gives Consistency Judge bounded, read-only evidence from that tree. The judge checks copied facts when applicable, and the Documentation Finalizer records the verdict. Evidence-based repair and finalization continue only while material progress exists; there is no arbitrary repair count. |

The Red-first coding session ends after the trusted checkpoint. A fresh session
then handles policy preparation, concurrent Green and documentation work,
review, and final validation. Content work uses one session. The controller
does not restore an individual Router, Architect, writer, implementer, reviewer, or
internal workflow stage after a crash. It rechecks Red and Green identities
from Git and GitLab. A changed ordinary candidate creates a new Green commit
and requires fresh receipts. Ordinary coding publication uses the verified
Green SHA.
If the target advances first, the controller validates and reviews the joined
tree, then makes an ordered two-parent commit with Green first and target
second. It publishes that reviewed commit after checking its exact identity.

After Green and the Diátaxis Writer finish, Green asks the private controller
socket to stage their combined changes. The controller returns the tree ID.
Judge first uses the socket to ask the controller to commit that tree with its
installed hook and return the Green SHA, tree, and bounded evidence. Once the
commit succeeds, Consistency Judge uses separate reads through that socket to compare changed
copied facts against their source. The controller binds each read to the
current issue, repository, base, and candidate tree. It provides a canonical
changed-path inventory with old and new file metadata, plus independent
bounded byte ranges from either side of a changed file. The judge can read
ranges in any order, repeat them, or read before requesting the inventory.
Each range identifies its path, side, offset, length, blob OID, and size.
There is no read cursor or completion call. The Non-functional Reviewer then
checks the JUnit summary and the committed tree. Green waits
for both receipts before it starts the parallel specialist review. This review includes
the Diátaxis, Quality, Test, and CI/CD Judges, the read-only README Reviewer,
and the License Checker. The CLI Syntax, Filesystem Structure, and Agent-policy
Judges join when their topics apply. Each active specialist sees the same
Green SHA, tree, diff, and Judge evidence. For a pending target merge, the
same reviewers instead use the validated joined tree and exact source and
target identities. Green waits for every receipt and routes
findings to their file owners. Router then gets the full result and
asks the Finalizer to act. Final validation uses current license evidence.

## Recovery is coarse and observable.

An activation is bounded, but its checked progress lasts in GitLab. Versioned
factory-authored issue notes bind each checkpoint to its run, operation, claim,
and full commit SHA. Existing `agent::` labels show workflow state. The local
ledger projects that evidence into the state machine; it is no longer the sole
recovery history.

If a run stops before a valid final result, the controller checks saved work
against GitLab. It keeps any local work that has not yet been pushed.
Genuine unfinished work starts a fresh full workflow at the coarse boundary. Router checks intake and slots,
then asks a fresh Architect to review the plan before any child edits files.
A fresh Red-first segment follows the same rule. It reruns checks as needed.
It does not restore agents, prompts, private reasoning, or messages.

A published candidate can sit behind later bookkeeping WIP in the history.
If independent content and worktree checks prove no unfinished work, recovery
observes that same MR and candidate with fresh pipeline and job proof. Dirty,
changed, or unknown work stays separate and needs fresh workflow validation.
A passing candidate pipeline cannot approve unfinished WIP.

An issue that is too large returns as a split plan. The controller checks the
plan, reuses children with exact markers, and creates only missing children.
It then notes and closes the parent. A retry runs the workflow and marker
searches again. It does not restore a saved split session.

A clean blocked issue can start again after exact pre-checkpoint proof. Dirty,
unclear, or published work keeps its reviewed recovery path.

Status projects the checked record and current remote evidence. It reports
stale, unavailable, or conflicting proof as unknown. After local state loss,
the controller rebuilds from remote checkpoints and receipts, then repeats
verification. An old passing receipt cannot authorize publication. The
[reference](../reference/serial-gitlab-agent-loop.md) defines the fields and
state mapping; the [setup guide](../how-to/setup-serial-gitlab-agent-loop.md)
explains reconstruction and migration.

Scheduled repair backoff and repair quarantine are item-local. They are not `RETRY_WAIT`, the ordinary workflow retry state, and are not global blockers. By contrast, ambiguous or inexact identity, active ownership, credential, policy, or authority conflicts fail closed. Issue and merge-request text cannot set repair priority, the attempt budget, or a backoff.

## Publication separates observation from repair.

After publication creates or updates an MR, the controller observes the exact
current source SHA and a bounded inventory of its current jobs. It requires at
least one current required job and success from every such job before it
records the human handoff. A failed pipeline uses the existing exact-owner
repair path when evidence is safe. If a successful pipeline has a pending,
failed, canceled, skipped, or other non-success required job, the controller
checks again up to three times. It does the same when the inventory has no
required job. If proof is still absent, it marks the issue `needs-repair`
without starting a worker. A source-identity mismatch blocks immediately;
transport errors retry.

## Credentials, audit, and human handoff.

The trusted controller uses the scoped Developer GitLab credential. Worker
processes receive no service `GITLAB_TOKEN`. They request bounded actions
through MCP; the controller checks identity and makes Git and GitLab changes.
The account still has its configured permissions. These tool boundaries do
not create a same-user security boundary. Keep the credential in the approved
checkout-local route, out of service files, notes, journals, and ledger data.

The bounded audit records decisions, evidence, tests, reviewers, correction outcomes, branch and merge-request identity, and the next action. It excludes credentials, prompts, chain-of-thought, and unbounded output. The worker returns one versioned, bounded result. The controller validates it before publication. A verified merge request and successful required-job proof hand work to a human reviewer. Only that human may approve or merge.

For lifecycle states, commands, recovery procedures, and protocol fields, use the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md) and [setup guide](../how-to/setup-serial-gitlab-agent-loop.md). For controller design and AI role routing, see [the recovery design](serial-agent-loop-recovery-design.md) and [the agent workflow explanation](agentic-workflow.md).
