---
title: Why the agent loop is serial and GitLab-native
type: explanation
summary: Understand why bounded workflow sessions use GitLab projection and local durable state.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-13
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
3. **Select** — select at most one eligible issue, or stop when no safe item is
   available. Fresh selection is deterministic: exact high priority, then
   normal, then exact low priority; creation time and IID break ties.
4. **Prepare worktree** — create or recover that issue's retained worktree.
   Only after selection and preparation does the controller invoke AI.
5. **Run the workflow session** — one bounded, workflow-owning AI session
   handles the issue.
6. **Validate and publish** — validate the bounded result against the ledger,
   Git, required receipts, and configured project. When justified, publish or
   update the same branch and merge request, then verify the exact handoff.
7. **Record and exit** — persist evidence and the next safe action, then exit
   rather than taking another issue.

The controller updates current `main` before publication through normal Git
reconciliation in the retained worktree. It does not discard work to refresh
the base. Pipeline observation, a future external retry, no progress, a budget
limit, or a human-facing state ends the activation; retryable worker failures
can continue within it when time remains.

## The work stays inside one AI session

The controller starts one session only after it has selected the issue and
prepared its worktree. A live Architect owns that session and decides whether
the issue is one reviewable, mergeable outcome within the budget before any
worktree mutation.

| Lane | What runs, and when |
| --- | --- |
| Coding | The Architect delegates Red behavior tests and the Diátaxis Writer in parallel. After their receipts, Green is the sole mutator: it implements and reconciles documentation, then coordinates review and repair before returning final validation to the Architect. |
| Documentation and agent policy | The Architect delegates the Documentation Writer as the sole mutator, then the Documentation Finalizer. The writer may make one consolidated repair pass. |

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

After an interruption, the controller reconciles observable repository state
and preserves the retained worktree. It restarts at the coarse workflow-session
boundary and repeats validation or publication checks when necessary. It does
not restore individual agents, their prompts, chain-of-thought, mailboxes, or
unbounded internal output. An oversized issue is instead handed back as a
structured split plan; the trusted controller creates any child issues only
after validating that plan.

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
