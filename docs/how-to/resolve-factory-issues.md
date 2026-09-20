---
title: How to resolve factory issue recovery
type: how-to
summary: Fix one issue.
audience: maintainer
tags: [agent-workflow]
status: draft
last_reviewed: 2026-09-17
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

Resolve one issue at a time.

Use this guide when retained recovery is interrupted, unclear, or unsafe. A
human makes one bounded lifecycle decision. The guide never approves or merges
a merge request. It does not claim fresh work, discard a retained worktree, or
change project policy.

Do not use this procedure for an exact failed current-source merge-request
pipeline with one uniquely corroborated retained owner. Use an applied ordinary
`agent-work loop run --apply` activation instead. It transitions that owner to
`repair` and runs the repair workflow without a human checkpoint.

## 1. Inspect the non-claiming inventory.

Run the inventory before making a decision. The non-claiming audit correlates
lifecycle, ledger, worktree, remote, and local controller evidence without
changing any of those records.

```bash
uv run agent-work recovery inventory --issue-iid 89 --json
```

Begin with aggregate `total`, `open`, and `closed`, then inspect `active`,
`blocked`, `dependency_gated`, `human_action`, and `no_action` counts. For a
candidate, inspect its active state, `human_action_required` and the bounded
cause, named open trusted prerequisites, and the exact next action. The
inventory lists failed main and open-MR pipelines first and ordinary retained
work afterward. The inventory reports fresh items but never selects them.

Treat `closed_no_action` as settled for a closed recordless split issue or a
closed `COMPLETE` record. Worktree, head, MR, or pipeline drift does not
change the settled result. A closed nonterminal record needs human review. An
untargeted inventory lists settled closed items, while `--issue-iid` gives
exact evidence.

Treat stale running work as `needs_human_decision`, never as a retry target.
Use `agent-work loop recover` for retained state. Retry only a retained
`BLOCKED` record whose trusted prerequisites are all closed or absent; this
retry keeps retained publication replay and retry history.

The supervisor finds recovery that requires action and
`$software-factory-supervisor` hands one IID to `$resolve-factory-issues`.
Keep no-action inventories with the supervisor, and never bulk-resolve a
count-only report.

Immediate owners are `PREPARING`, `EXECUTING`, `PUBLISHING`, `OBSERVING`, or
`REPAIR`. A `BLOCKED` owner requires action and is not
runnable. A `RETRY_WAIT` owner is queued and non-competing. A `RETRY_WAIT`
owner never competes with a selected IID. A reviewed retry changes only the selected IID
from `BLOCKED` to `PREPARING` and retains `PUBLISHING` for publication replay;
untouched blocked and queued records remain non-competing. Stop for a
different immediate owner, when multiple owners must reconcile, or when the
owner state is unclear.

## 2. Choose and preview one resolution.

Choose one action supported by inventory evidence.
Preview writes an evidence-bound plan.
Preview changes neither GitLab nor local lifecycle state.

```bash
uv run agent-work recovery resolve --issue-iid 89 --choice retry \
  --reason "The dependency MR is merged and the retained worktree is still current" \
  --evidence-url https://gitlab.example.com/group/project/-/merge_requests/42 \
  --json >agent-work-recovery-89-plan.json
```

Use `close`.
A close needs a reason.
A close needs evidence.
The evidence can be a link or replacement issue.
Use `supersede` with `--replacement-issue-iid`.
Provide a reason and evidence.
Use `relabel` only with a canonical lifecycle label.
Provide that label through `--target-label`.
`close`, `supersede`, and `relabel` create durable intents.
Each intent is replayable.
Each closure records one idempotent receipt.
The closure receipt records the reason, evidence, and replacement.
The closure receipt clears the lifecycle projection.
The closure receipt terminalizes the ledger.
Use `reopen` only for a closed issue without a retained record.
A reopen atomically clears lifecycle labels.
A reopen leaves fresh ordinary work unclaimed.
Each plan binds the exact MR project and target.
Each plan also binds source SHA and pipeline ID.
Each plan binds trusted dependencies.
Each plan makes one explicit issue decision.

## 3. Review the preview with the responsible human.

Confirm the planned issue, choice, reason, and evidence.
Confirm observed and proposed states.
Make the decision and authority visible.

```text
Operator: Issue #89 was dependency-gated. The dependency merged; should I retry it?
Maintainer: Yes. Retry only #89. The linked MR is the evidence; do not claim another issue.
Operator: I will apply the reviewed retry plan and report the before/after receipt.
```

Stop for expired credentials or configured-bot mismatch.
Stop before preview or apply.
Run `agent-work credential rotate` for issue #84.
Complete the credential-recovery handoff.
Then create a new inventory and plan.

## 4. Apply the exact reviewed plan.

Apply requires the reviewed plan file and `--apply`.
It rechecks current evidence under the project lock.
It rejects stale or drifted plans.
Create a new preview.
Never edit or reuse the rejected plan.

```bash
uv run agent-work recovery resolve --issue-iid 89 --choice retry \
  --reason "The dependency MR is merged and the retained worktree is still current" \
  --evidence-url https://gitlab.example.com/group/project/-/merge_requests/42 \
  --plan-file agent-work-recovery-89-plan.json --apply --json
```

Preview and applied receipts are redacted.
Their field names define the planned contract.
Use actual command values as durable evidence.

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

Record the receipt with recovery evidence.
If evidence changes, retain the old plan.
It remains review history.
Return to the inventory step.
Never manually change labels, branches, pipelines, or artifacts.

## 5. Recover a clean interrupted claim for issue #35.

Use this route only with exact evidence.
Issue #35 needs a clean interrupted `EXECUTING`/`WORKFLOW` claim.
Never edit the ledger manually.
First create and review the exact recovery plan:

```bash
uv run agent-work loop recover --issue-iid 35 --json >agent-work-recover-35-plan.json
```

Apply that exact reviewed plan only when safe.
It must normalize the claim to `BLOCKED`:

```bash
uv run agent-work loop recover --issue-iid 35 --apply \
  --plan-file agent-work-recover-35-plan.json --json
```

Then use inventory and a one-IID retry sequence.
Review every result.
These commands do not authorize a choice for another issue.

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

The reviewed retry hands off one selected IID.
Multi-segment continuation remains pinned to #35.
Stop for human review without verified normalization.
This route never approves a merge request.
This route never merges a merge request.
