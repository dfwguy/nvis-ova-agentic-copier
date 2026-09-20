---
title: Why the agent loop is serial and GitLab-native
type: explanation
summary: Learn why the loop uses GitLab and local state.
audience: maintainer
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-19
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
the shared work record that operators can see. A local ledger and retained
worktree let one host recover work without becoming another workflow engine or
queue.

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

The saved worktree has a simple role. It keeps the work in one known place. It
lets the next run look at the same files and Git state. It does not turn into a
new queue. It does not decide which work is most worth doing. The controller
still makes that choice from the checked state.

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
| **Observe pipeline safety** | Inspect the newest default-branch pipeline and each open merge request's newest pipeline for its current source SHA. Missing, malformed, or inaccessible evidence blocks work. In an applied activation, poll nonterminal evidence in place with a short, bounded exponential delay. A terminal failure takes priority over ordinary work. |
| **Select** | After safe resumable work and the pipeline gate, select at most one eligible item, or stop. Consider exact failed-pipeline repairs before fresh issues. If the first repair is deferred, scan for another eligible exact repair. Fresh selection is deterministic: exact high priority, normal, then exact low priority; creation time and IID break ties. |
| **Prepare worktree** | Create or recover the selected issue's retained worktree. Invoke AI only after selection and preparation. |
| **Run the workflow session** | One bounded AI session owns the issue. |
| **Validate and publish** | Validate the bounded result against the ledger, Git, required receipts, and configured project. When justified, publish or update the same branch and merge request, then verify that handoff. |
| **Record and exit** | Persist evidence and the next safe action. Exit rather than take another issue. |

The controller keeps selection and recovery safe and visible.

| Design choice | Reason and boundary |
| --- | --- |
| Refresh | Before publication, update current `main` through normal Git reconciliation in the retained worktree; never discard work to refresh the base. Keep watching nonterminal default-branch or exact current-MR pipeline evidence, and resume when it is terminal. A future external retry, no progress, a budget limit, or a human-facing state ends an activation; retryable worker failures may continue while time remains. |
| Shared gate | The same gate protects manual applied runs and timer-driven activations, so one path cannot start unrelated work after the other finds a pipeline problem. |
| Repair limits | Resume a failed merge-request pipeline only with a retained owner and exact project, merge-request, branch, current-source-SHA, and worktree evidence. Each owner gets at most three automatic repair attempts. Record a durable 20-minute per-item backoff after each failed attempt. A later activation can scan for another eligible repair or ordinary work. After three failed attempts, quarantine or defer the item for human review. Keep it visible in status and preview, but do not create a loop-wide checkpoint. A failed default-branch pipeline or ownerless failed merge request creates an actionable human block. |
| Attributable recovery | Favor visible, attributable recovery over guessing which issue should repair shared or unclear pipeline evidence. The [reference contract](../reference/serial-gitlab-agent-loop.md#preselection-pipeline-gate) defines statuses and exact checks. |
| Safe diagnostics | For an eligible repair, select one current failed job from the exact pipeline and ignore superseded retries. Read only that job's trace, remove secret-like text and control sequences, and keep a bounded diagnostic. Validate and save it before repair begins, so a restart uses the same verified context. Never pass a raw trace to the ledger, report, or workflow prompt. Missing, ambiguous, stale, or malformed job or trace evidence stops repair; the workflow does not guess, retry, or cancel a job or pipeline. |

## The work stays inside one AI session.

The controller starts one session only after it selects the issue and prepares its worktree. A live Architect owns that session. Before any worktree mutation, the Architect decides whether the issue is one reviewable, mergeable outcome within the budget.

| Lane | What runs, and when |
| --- | --- |
| Coding | The Architect delegates Red behavior tests and the Diátaxis Writer in parallel. After their receipts, Green is the sole mutator. It implements and reconciles documentation, coordinates review and repair, then returns final validation to the Architect. |
| Documentation and agent policy | The Architect delegates the Documentation Writer as sole mutator, then the Documentation Finalizer. Evidence-based repair and finalization continue only while material progress exists; there is no arbitrary repair count. |

Architecture, behavior tests, implementation, documentation, every applicable review role, license review, and final validation stay in the same session. The controller does not restore an individual Architect, writer, implementer, reviewer, or internal phase after a crash.

After implementation, Green starts the parallel review fan-out. It includes the Non-functional Reviewer, Implementation Judge, Diátaxis Judge, Quality Judge, Test Judge, CI/CD Judge, README Reviewer, and License Checker. The CLI Syntax Judge, Filesystem Structure Judge, and Agent-policy Judge join only when their subjects apply. Every initially applicable reviewer sees the same stable post-Green diff. Green waits for every receipt before it consolidates and repairs actionable findings. The License Checker does not wait for another receipt. The Architect then receives the full result and delegates the Finalizer. Documentation comes before implementation, implementation before reviews, and final validation after current license evidence.

## Recovery is coarse and observable.

An activation is bounded, but its evidence lasts. The ledger records the selected issue, coarse workflow state, retry data, retained worktree, branch and merge-request evidence, receipts, transition intent, and a redacted next action. GitLab labels give a simple status. The ledger holds transaction detail.

After an interruption before one valid final workflow receipt, the controller reconciles observable repository state and preserves the retained worktree. It starts a fresh, complete workflow session at the coarse boundary. It repeats validation when needed. It does not restore individual agents, prompts, chain-of-thought, mailboxes, or an internal phase. An oversized issue returns as a structured split plan. The controller validates that plan, reuses marker-matched children, creates only missing children, then notes and closes the parent. A later retry reruns workflow and marker searches. It does not resume a saved decomposition.

Only the ledger keeps the concise audit. Status is an operational projection of that record plus best-effort live evidence. This avoids presenting an old retry or failure as current. It also keeps unavailable, stale, or conflicting observations honestly unknown. The exact fields, bounded output, and state mapping are in the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md).

Scheduled repair backoff and repair quarantine are item-local. They are not `RETRY_WAIT`, the ordinary workflow retry state, and are not global blockers. By contrast, ambiguous or inexact identity, active ownership, credential, policy, or authority conflicts fail closed. Issue and merge-request text cannot set repair priority, the attempt budget, or a backoff.

## Publication separates observation from repair.

After publication creates or updates an MR, the controller waits cheaply only for the exact source-SHA pipeline to become terminal. It then starts a fresh Pipeline Performance Judge context. This Fast/low role evaluates live evidence without changing code, the MR, jobs, approvals, or merge. Healthy evidence has no external effect. Concrete, repository-actionable evidence creates or reuses one deduplicated GitLab problem issue. Incomplete or unverified evidence does not manufacture work.

The controller selects that problem issue only during later ordinary reconciliation. Its retained or newly prepared worktree starts a fresh complete workflow session. Pipeline observation never promises same-session repair.

## Credentials, audit, and human handoff.

The deterministic controller, AI workflow, and nested roles share one scoped Developer GitLab credential. Their separation supports deterministic, recoverable execution; it is not a security boundary. The credential stays in the checkout-local environment, not the service file, journal, or ledger.

The bounded audit records decisions, evidence, tests, reviewers, correction outcomes, branch and merge-request identity, and the next action. It excludes credentials, prompts, chain-of-thought, and unbounded output. The worker returns one versioned, bounded result. The controller validates it before publication. A verified merge request hands work to a human reviewer. Only that human may approve or merge.

For lifecycle states, commands, recovery procedures, and protocol fields, use the [serial agent-loop reference](../reference/serial-gitlab-agent-loop.md) and [setup guide](../how-to/setup-serial-gitlab-agent-loop.md). For controller design and AI role routing, see [the recovery design](serial-agent-loop-recovery-design.md) and [the agent workflow explanation](agentic-workflow.md).
