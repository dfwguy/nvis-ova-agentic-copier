---
title: How to resolve factory issue recovery
type: how-to
summary: Check saved proof for a blocked issue and pick a safe next step.
audience: maintainer
tags: [agent-workflow]
status: draft
last_reviewed: 2026-09-27
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

Begin with aggregate `total`, `open`, and `closed`. Then inspect `active`,
`blocked`, `dependency_gated`, `human_action`, and `no_action` counts. For one
candidate, inspect its active state and `human_action_required`. Read its
bounded cause, open needs, and exact next action. An open need can be a direct
prerequisite. It can also be a required child or descendant of a closed split
parent. The inventory lists failed main and open-MR pipelines first. It lists
ordinary retained work next. The inventory reports fresh items but never
selects them.

Closing a direct prerequisite is not enough when it was split. The inventory
stays `dependency_gated` while any required descendant is open. Let the normal
timer check again. Do not start a tight retry or worker. When all required
descendants close, the saved record can become `safely_retryable`. It then
continues through the reviewed recovery path.

Treat `closed_no_action` as settled for a closed recordless split issue or a
closed `COMPLETE` record. Worktree, head, MR, or pipeline drift does not
change the settled result. A closed nonterminal record needs human review. An
untargeted inventory lists settled closed items, while `--issue-iid` gives
exact evidence.

Treat stale running work as `needs_human_decision`, never as a retry target.
Use `agent-work loop recover` for retained state. Retry a retained `BLOCKED`
record only when inventory marks it `safely_retryable` and an explicit retry
preview accepts its current evidence. Automatic retry uses shared recovery
checks. It also needs exactly one `safely_retryable` inventory result.
Inventory checks dirty retained artifacts, including eligible legacy forms.
It shares those rules with retry preview. Its result alone does not pass all
preview checks. For `needs_human_decision`, inspect the retained worktree and
ledger. Read the bounded reason before choosing a resolution. Manual
`recovery resolve --choice retry` can preview evidence for human review even
when `loop retry` rejects it. A valid retry keeps retained publication
replay and retry history.

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
Operator: Issue #89 was dependency-gated. Inventory now shows every required descendant closed; should I retry it?
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

## 5. Resume a safely retryable blocked issue.

Use this route only when inventory marks the exact blocked issue as
`safely_retryable`. `loop retry` runs the shared recovery checks. It also
requires exactly one `safely_retryable` inventory result. A passed preview
needs no new human approval to unblock it. An ineligible preview changes no
local or GitLab state.

First save and review the exact retry plan:

```bash
uv run agent-work loop retry --issue-iid 89 --json \
  >agent-work-loop-retry-89-plan.json
```

Continue only after `retry_preview`. It contains the exact eligible plan.
`retry_ineligible` means known evidence is ineligible and changes no local or
GitLab state. Correct that evidence, then use `recovery resolve --choice retry`
to make a new preview. An unavailable, ambiguous, stale, or otherwise
operationally uncertain check exits with an error instead of
`retry_ineligible`; stop without applying a plan.

Apply only that reviewed plan. A valid plan may schedule a blocked record.
It may also activate an eligible saved candidate or other retained correction.
Follow the evidence steps above for `retry_ineligible` or an error. Use
`recovery resolve --choice retry` when an explicit resolution is needed.

```bash
uv run agent-work loop retry --issue-iid 89 --apply \
  --plan-file agent-work-loop-retry-89-plan.json --json
```

If apply stops midway, rerun the same `loop retry` command with `--apply` and
the same `--plan-file`. If the original blocked command was `loop recover`,
rerun that command with its same plan. While the matching intent is `BLOCKED`,
apply rebuilds inventory and checks the full plan under the lock before any
effect. If that exact intent reached `PREPARING` or authored `EXECUTING`, apply
resumes it idempotently. Stale or mismatched evidence still fails.

`retry_scheduled` means the record is resumable; follow its reported retained
workflow. `correction_activated` means the command activated an eligible
retained correction; complete the reported correction and validation before
publication. See the [retry outcome reference](../reference/serial-gitlab-agent-loop.md#retry-outcomes) before taking either next action.

## 6. Recover a clean interrupted claim for issue #35.

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

## 7. Hand off a stale dirty claim for issue #36.

Use this route when a retained `EXECUTING`/`WORKFLOW` claim has a stale or
missing owner and its saved worktree contains changes. The lane may be
`UNKNOWN` or `CODING`. A Red checkpoint may be present or absent; the plan
binds that fact. The recorded source `HEAD` may differ from the current main
commit. There must be no publication or merge request. Keep the checkout as
it is. Do not clean, stash, or restage files, or replace an existing Red
checkpoint. Review a plan for that exact issue:

```bash
uv run agent-work loop recover --issue-iid 36 --json >agent-work-recover-36-plan.json
```

Confirm the plan's action is `handoff_stale_dirty_claim`. Check the issue,
owner, branch, worktree, `HEAD`, staged or unstaged changes, ledger revision,
and whether a saved Red checkpoint exists. Apply only that reviewed plan:

```bash
uv run agent-work loop recover --issue-iid 36 --apply \
  --plan-file agent-work-recover-36-plan.json --json
```

The `dirty_claim_handed_off` result records a blocked handoff for manual
recovery of issue #36. It keeps the branch, worktree, index, unstaged
changes, and any existing Red checkpoint. Its durable receipt binds the
reviewed plan and dirty worktree snapshot. The blocked issue releases the
active claim. A later loop pass can select independent work. The command does
not run a worker or publish a merge request.

If the owner or any reviewed evidence changes, apply rejects the plan before
changing the claim. Inspect the current inventory and create a fresh plan;
do not edit the old one. Use the one-issue resolution procedure above when a
human has decided how to resume or close the retained work.

## 8. Resume a saved candidate after a failed publish check.

Use this route only when the inventory finds a staged-only candidate held after
an eligible `worker_protocol_invalid`, or the exact
`state_reconciliation_failed` candidate-broker condition. Keep it in its
worktree. Do not make a new candidate. Do not replace its original Red check.
Do not run Red again.

Make a retry preview for the exact issue.

```bash
uv run agent-work recovery resolve --issue-iid 221 --choice retry \
  --reason "The retained authored candidate was reviewed after publication reconciliation failed" \
  --json >agent-work-recovery-221-plan.json
```

Read the plan before you apply it. If the candidate, original Red check, or
other saved proof changed, do not apply it. Make and review a new plan from the
current inventory.

Apply only the reviewed plan. The controller checks the saved proof under the
project lock before it resumes the issue.

```bash
uv run agent-work recovery resolve --issue-iid 221 --choice retry \
  --reason "The retained authored candidate was reviewed after publication reconciliation failed" \
  --plan-file agent-work-recovery-221-plan.json --apply --json
```

Before resume, the controller checks the branch, head, staged index, one
original parent, and Red check. It also requires an `unknown` audit marker and
a saved merge request draft. It keeps the test scope from the Red check. The
workflow completes validation before publication. Stop for human review if
proof is missing, changed, or unclear.

You can replay the same plan after an interrupted apply. The controller checks
the same identity and returns the saved recovery. It does not publish the
candidate.
