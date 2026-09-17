---
title: How to resolve factory issue recovery
type: how-to
summary: Inspect exceptional recovery evidence and apply one reviewed resolution for a retained factory issue.
audience: maintainer
tags: [agent-workflow]
status: draft
last_reviewed: 2026-09-16
related:
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the recovery inventory and resolution command contract.
  - path: ../explanation/serial-agent-loop-recovery-design.md
    why: Explains why exceptional recovery uses retained evidence and an explicit decision.
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

# How to resolve factory issue recovery

Use this procedure when a factory issue has interrupted, ambiguous, or unsafe
retained recovery evidence and a human needs to make one bounded lifecycle
decision. It does not approve or merge a merge request, claim fresh work,
discard a retained worktree, or alter project policy.

Do not use this procedure for an exact failed current-source merge-request
pipeline with one uniquely corroborated retained owner. An applied ordinary
`agent-work loop run --apply` activation transitions that owner to `repair` and
runs the repair workflow without a human checkpoint.

## 1. Inspect the non-claiming inventory

Run the inventory before deciding what to do. It correlates the issue's
lifecycle projection with its ledger, retained branch or worktree, merge
request and pipeline evidence, and local controller evidence without changing
any of them.

```bash
uv run agent-work recovery inventory --issue-iid 89 --json
```

Start with the aggregate `total`, `open`, `closed`, `active`, `blocked`,
`dependency_gated`, `human_action`, and `no_action` counts. Use a detailed
candidate's active state, `human_action_required`, bounded blocked cause,
named open trusted prerequisites, and exact next action to decide whether to
act. A cleared trusted split prerequisite recommends an explicit `retry`; it
does not start new work. Failed main and open-MR pipelines appear before
ordinary retained work. Do not use this command to select fresh work: it only
reports fresh items.

Treat `closed_no_action` as settled. It applies to a closed recordless split
issue and a closed `COMPLETE` record even if retained worktree, head,
merge-request, or pipeline evidence has drifted. A closed nonterminal record
still needs human review. An untargeted inventory summarizes settled closed
items; use `--issue-iid` when you need its exact evidence.

A stale running item is an early `needs_human_decision`, not a retry target:
use `agent-work loop recover` for its exact retained state. Retry is limited to
a retained `BLOCKED` record whose trusted dependencies are all closed (or
absent); it preserves any retained publication replay and retry history.

When `$software-factory-supervisor` identifies action-required recovery, it
hands exactly one IID to `$resolve-factory-issues`. Keep no-action inventories
with the supervisor; do not turn a count-only report into bulk resolutions.

An immediate owner is one record in `PREPARING`, `EXECUTING`, `PUBLISHING`,
`OBSERVING`, `REPAIR`, or `DECOMPOSING`. `BLOCKED` is action-required, not
runnable; `RETRY_WAIT` is queued and does not compete with an already selected
IID. A reviewed retry changes only its selected `BLOCKED` IID to `PREPARING`,
or retains `PUBLISHING` for a publication replay. Untouched blocked and queued
records remain non-competing. Stop if a different immediate owner exists or if
more than one immediate owner needs reconciliation.

## 2. Choose and preview one resolution

Choose only the action supported by the inventory evidence. Preview writes an
evidence-bound plan and does not change GitLab or local lifecycle state.

```bash
uv run agent-work recovery resolve --issue-iid 89 --choice retry \
  --reason "The dependency MR is merged and the retained worktree is still current" \
  --evidence-url https://gitlab.example.com/group/project/-/merge_requests/42 \
  --json >agent-work-recovery-89-plan.json
```

Use `close` only with a reason plus an evidence link or replacement issue.
Use `supersede` with `--replacement-issue-iid` and the supporting reason and
evidence. Use `relabel` only with a canonical lifecycle label through
`--target-label`. Close, supersede, and relabel use replayable durable intents;
closure records one idempotent reason/evidence/replacement receipt, clears the
lifecycle projection, and terminalizes the ledger. `reopen` is only for a
closed issue with no retained record; it atomically clears lifecycle labels and
leaves fresh ordinary work unclaimed. Each plan binds exact MR project/target,
source SHA, pipeline ID, and trusted dependencies. A plan has one explicit
decision for one issue.

## 3. Review the preview with the responsible human

Confirm the planned issue, choice, reason, evidence links, observed state, and
proposed state. For example, the interaction should make the decision and its
authority visible:

```text
Operator: Issue #89 was dependency-gated. The dependency merged; should I retry it?
Maintainer: Yes. Retry only #89. The linked MR is the evidence; do not claim another issue.
Operator: I will apply the reviewed retry plan and report the before/after receipt.
```

If inventory reports an expired credential or configured-bot mismatch, stop
before preview or apply. Run `agent-work credential rotate` and complete the
credential-recovery handoff for issue #84; then make a new inventory and plan.

## 4. Apply the exact reviewed plan

Apply requires both the reviewed plan file and `--apply`. It rechecks current
evidence while holding the project lock. A stale or drifted plan is rejected;
make a new preview instead of editing or reusing it.

```bash
uv run agent-work recovery resolve --issue-iid 89 --choice retry \
  --reason "The dependency MR is merged and the retained worktree is still current" \
  --evidence-url https://gitlab.example.com/group/project/-/merge_requests/42 \
  --plan-file agent-work-recovery-89-plan.json --apply --json
```

The preview and applied receipts are redacted. Their field names below are the
planned contract; use the command's actual values as the durable evidence.

```json
{
  "command": "recovery resolve",
  "outcome": "planned",
  "issue_iid": 89,
  "choice": "retry",
  "plan_file": "agent-work-recovery-89-plan.json",
  "before": {"classification": "dependency-gated"},
  "after": {"classification": "planned"}
}
```

```json
{
  "command": "recovery resolve",
  "outcome": "applied",
  "issue_iid": 89,
  "choice": "retry",
  "before": {"classification": "dependency-gated"},
  "after": {"classification": "safely-retryable"}
}
```

Record the receipt with the issue's recovery evidence. If application fails
because evidence changed, retain the old plan as review history and start again
at the inventory step. Do not work around the rejection by changing labels,
branches, pipelines, or retained artifacts manually.

## 5. Recover a clean interrupted claim for issue #35

Use this route only when the exact evidence shows that issue #35 has a clean,
interrupted `EXECUTING`/`WORKFLOW` claim. Do not edit the ledger manually.
First create and review the exact recovery plan:

```bash
uv run agent-work loop recover --issue-iid 35 --json >agent-work-recover-35-plan.json
```

Apply that exact reviewed plan only when it confirms the claim can be safely
normalized to `BLOCKED`:

```bash
uv run agent-work loop recover --issue-iid 35 --apply \
  --plan-file agent-work-recover-35-plan.json --json
```

Then use the normal inventory and one-IID retry-plan sequence. Review every
result; these commands do not authorize a choice for another issue.

```bash
uv run agent-work recovery inventory --issue-iid 35 --json
uv run agent-work recovery resolve --issue-iid 35 --choice retry \
  --reason "The reviewed recovery plan normalized the clean interrupted claim" \
  --json >agent-work-recovery-35-retry-plan.json
uv run agent-work recovery resolve --issue-iid 35 --choice retry \
  --reason "The reviewed recovery plan normalized the clean interrupted claim" \
  --plan-file agent-work-recovery-35-retry-plan.json --apply --json
uv run agent-work loop run --apply --json
```

The reviewed retry is the durable handoff to the one selected IID; a
multi-segment continuation remains pinned to #35. If normalization is not
verified, stop for human review. This route never approves or merges a merge
request.
