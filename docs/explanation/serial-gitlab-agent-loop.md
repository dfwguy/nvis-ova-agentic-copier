---
title: Why the agent loop is serial and GitLab-native
type: explanation
summary: Understand why bounded workflow sessions use GitLab projection and local durable state.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-15
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

This is a simple, serial software factory. Humans supply product and policy
input, then review, approve, and merge the result. The deterministic controller
and the AI workflow do the routine work, but neither may approve or merge.
GitLab is the shared, operator-visible work record; the local ledger and
retained worktree make one host's execution recoverable rather than becoming a
second workflow engine or queue.

## One issue, one bounded activation

For each activation, the controller performs these stages in order:

1. **Lock and refresh** — lock the configured project and refresh its dedicated
   checkout.
2. **Reconcile** — compare the ledger with GitLab issues and merge requests,
   Git branches, and any retained worktree. Recoverable work always comes
   before new work.
3. **Observe pipeline safety** — before fresh selection, inspect the newest
   default-branch pipeline and each open merge request's newest pipeline for
   its current source SHA. Missing, malformed, or inaccessible evidence blocks;
   a nonterminal pipeline yields; a terminal failure takes priority over
   ordinary work.
4. **Select** — after any safe resumable work and the pipeline gate, select at
   most one eligible issue, or stop when no safe item is available. Fresh
   selection is deterministic: exact high priority, then normal, then exact
   low priority; creation time and IID break ties.
5. **Prepare worktree** — create or recover that issue's retained worktree.
   Only after selection and preparation does the controller invoke AI.
6. **Run the workflow session** — one bounded, workflow-owning AI session
   handles the issue.
7. **Validate and publish** — validate the bounded result against the ledger,
   Git, required receipts, and configured project. When justified, publish or
   update the same branch and merge request, then verify the exact handoff.
8. **Record and exit** — persist evidence and the next safe action, then exit
   rather than taking another issue.

The controller updates current `main` before publication through normal Git
reconciliation in the retained worktree. It does not discard work to refresh
the base. Pipeline observation, a future external retry, no progress, a budget
limit, or a human-facing state ends the activation; retryable worker failures
can continue within it when time remains.

The same gate protects manual applied runs and timer-driven next activations,
so one entry path cannot start unrelated work while the other has detected a
pipeline problem. A failed merge-request pipeline can resume bounded repair
only through a retained owner with exact project, merge-request, branch,
current-source-SHA, and worktree evidence. A failed default-branch pipeline or
an ownerless failed merge request instead produces an actionable human block.
This intentionally favors visible, attributable recovery over guessing which
issue should repair shared or ambiguous pipeline evidence. The [reference
contract](../reference/serial-gitlab-agent-loop.md#preselection-pipeline-gate)
defines the statuses and exact checks.

## The work stays inside one AI session

The controller starts one session only after it has selected the issue and
prepared its worktree. A live Architect owns that session and decides whether
the issue is one reviewable, mergeable outcome within the budget before any
worktree mutation.

| Lane | What runs, and when |
| --- | --- |
| Coding | The Architect delegates Red behavior tests and the Diátaxis Writer in parallel. After their receipts, Green is the sole mutator: it implements and reconciles documentation, then coordinates review and repair before returning final validation to the Architect. |
| Documentation and agent policy | The Architect delegates the Documentation Writer as the sole mutator, then the Documentation Finalizer. Evidence-based repair/finalization cycles continue only while material progress exists; no arbitrary repair count applies. |

Architecture, behavior tests, implementation, documentation, every applicable
review role, license review, and final validation therefore stay nested in the
same session. The controller does not restore an individual Architect, writer,
implementer, reviewer, or internal phase after a crash.

After implementation, Green starts the parallel review fan-out: the
Non-functional Reviewer, Implementation Judge, Diátaxis Judge, Quality Judge,
Test Judge, CI/CD Judge, and README Reviewer. The CLI Syntax Judge, Filesystem
Structure Judge, and Agent-policy Judge join that fan-out only when their
subjects apply. Once every initially applicable reviewer returns, Green
consolidates and repairs actionable findings. The License Checker runs after
those reviews, not in the parallel fan-out. The Architect then receives the
complete result and delegates the Finalizer. This order makes documentation
writing happen before implementation, implementation happen before reviews,
and final validation happen after review and license evidence.

## Recovery is coarse and observable

An activation is bounded, but its evidence is durable. The ledger records the
selected issue, coarse workflow state, retry information, retained worktree,
branch and merge-request evidence, receipts, transition intent, and a redacted
next action. GitLab labels project a simple status while the ledger holds the
transaction detail.

After an interruption before one valid final workflow receipt, the controller
reconciles observable repository state and preserves the retained worktree. It
starts a fresh complete workflow session at the coarse boundary and repeats
validation when necessary. It does not restore individual agents, their
prompts, chain-of-thought, mailboxes, or an internal phase. An oversized issue
is instead handed back as a structured split plan; the trusted controller
creates any child issues only after validating that plan.

The concise audit is durable only in the ledger; status is deliberately an
operational projection over that record plus best-effort live evidence. This
keeps a past retry or failure from being presented as the current condition and
lets unavailable, stale, or conflicting observations remain honestly unknown
instead of being mistaken for current truth. The exact fields, bounded output,
and state mapping belong in the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md).

## Publication separates observation from repair

After controller publication creates or updates an MR, the controller cheaply
waits only for the exact source-SHA pipeline to reach a terminal state. It then
starts a fresh Pipeline Performance Judge context. That Fast/low role evaluates
the live evidence without changing code, the MR, jobs, approvals, or merge.
Healthy evidence has no external side effect. Concrete, repository-actionable
evidence creates or reuses one deduplicated GitLab problem issue; incomplete or
unverified evidence does not manufacture work.

The controller selects that problem issue only in a later ordinary
reconciliation. Its retained or newly prepared worktree begins a fresh complete
workflow session, rather than a direct continuation of pipeline observation.
This preserves an auditable recovery boundary and does not promise same-session
repair.

## Credentials, audit, and human handoff

The deterministic controller, AI workflow, and its nested roles share the same
scoped Developer GitLab credential. Their separation exists for deterministic,
recoverable execution—not as a security boundary. The credential remains in
the checkout-local environment rather than the service file, journal, or
ledger.

The bounded audit records decisions, evidence, tests, reviewers, correction
outcomes, branch and merge-request identity, and the next action. It excludes
credentials, prompts, chain-of-thought, and unbounded output. The worker
returns only a versioned bounded result, which the controller validates before
publication. A verified merge request is the handoff to a human reviewer; only
the human approves or merges it.

For exact lifecycle states, commands, recovery procedures, and protocol fields,
use the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md)
and [setup guide](../how-to/setup-serial-gitlab-agent-loop.md). For the deeper
controller design and AI role routing, see [the recovery design](serial-agent-loop-recovery-design.md)
and [the agent workflow explanation](agentic-workflow.md).
