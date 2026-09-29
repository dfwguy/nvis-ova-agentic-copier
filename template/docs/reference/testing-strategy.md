---
title: Testing strategy
type: reference
summary: Look up test design, validation layers, and local case timing rules.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-29
related:
  - path: ../how-to/validate-template-changes.md
    why: Shows how to run the local and CI validation checks.
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

# Testing strategy

Tests should give a fast answer during a change. They must also cover the
supported Python versions before publication. The [validation guide](../how-to/validate-template-changes.md)
lists the commands and CI jobs. This page gives the test design and timing rules.

## What to test

Test behavior and the contracts people use. Cover normal results, errors,
and recovery at the boundary that owns each rule. For this repository, that
includes the agent workflow, Copier output, and the generated project's
setup and validation. Check root, template, and rendered output when a shared
contract must match. Check executable behavior at its owning interface; do
not add tests that lock docs wording or Markdown shape.

Choose a focused unit test when an input and result can be checked in
isolation. Use an integration test when real file, Git, process, or Copier
behavior matters. Render the template without setup tasks for file and config
checks. Use the trusted setup path only for behavior that needs those tasks.
Use a broader workflow test when the result depends on several boundaries
working together. Keep each test as narrow as its claim allows.

Make tests repeatable. Use fixed inputs and controlled time or process
boundaries for speed claims. Give each test its own temp state. Keep shared
fixtures small, and split costly setup from simple render checks where safe.
First run the focused tests for the change. Then run the full supported
matrix and other checks before the review handoff.

## Validation layers

| Layer | Purpose and evidence |
| --- | --- |
| Focused tests | Run relevant behavior tests while developing a change. Keep setup small enough to diagnose a failure. |
| Full supported matrix | Run the complete pytest suite on Python 3.11 and 3.14 through tox and validation hooks. The Judge's JUnit files give case counts and durations for each environment. |
| CI | Run the full pytest suite once per supported Python version, alongside the other validation jobs. Each run publishes its own test and coverage reports. |

## Local case timing

The local goal is **100 ms or less** per test case in each supported Python
version. Exactly 100 ms meets the goal. Use the JUnit `testcase` `time`, which
includes setup, call, and teardown charged to that case. Each test parameter
is a distinct case. A session fixture can charge setup to its first case.
List every case above 100 ms. Report every case at or above **1 second**
by Python version, even when another finding needs a fix. Keep its proof,
classification, owner, and plan to cut cost or review an exception in the
durable audit or linked issue record. An inherited case that is no slower
does not block an unrelated change on cost alone.

For complete serial timing, the controller checks baseline and candidate
proof for Python 3.11 and 3.14. It binds each full run to its source tree.
It checks case lists, environments, commands, and seeds. Missing or
mismatched readable proof stops review. A successful hook with a missing or
`OSError`-unreadable fixed JUnit file reports timing `UNKNOWN`. Review
continues without a timing pass, times, or counts. With complete timing, the
reviewer gets bounded identities, findings, an outcome, and the full candidate
list above 100 ms. The three slowest cases are not a complete list. A touched
case above 100 ms is report-only unless repeats corroborate a regression.
New slow cases and corroborated regressions need correction unless an
approved integration exception applies. Affected existing cases are changed
tests or cases explicitly mapped as consumers of changed fixtures in
`tests/**/conftest.py`. An affected existing case gets two focused repeats
on each exact tree when its candidate time is at least 1 second. Its baseline
must be below 1 second, or its candidate time must exceed the baseline. The
`corroborated_regression` or `uncorroborated_spike` finding reports setup,
call, and teardown changes. A changed `.py` file not in either `tests/` or
`template/tests/` also marks each case meeting this rule as potentially affected.
Unaffected existing cases stay in the full candidate
inventory. Those at or above 1 second get an `inherited_slow` report-only
finding; subsecond cases get no individual finding. Neither group gets
focused repeats or automatic correction.
Missing repeat proof blocks a decision that needs it.

When only production code changes, an existing slow case may vary by less
than 1 second. Paired runs can report this as `inherited_variance`. The
reviewer checks for a causal link. Unrelated variance does not block work.
A case newly above 1 second, a rise of at least 1 second, or a changed test
still needs correction and exact timing proof.

For an existing touched case that stays above 100 ms and below 1 second, the
controller uses the complete baseline and candidate JUnit inventories to mark
it `touched_slow`. This finding alone does not require correction. A changed
test file can mark many cases this way without using focused runs. New cases
above 100 ms get one candidate focused check.
The controller groups up to 32 cases per
focused invocation within each Python version. It allows at most 32 focused
invocations and 900 seconds for the review. The worker receives up to 128
timing findings and an `omitted_findings` count. Every candidate finding at
or above 1 second is retained before subsecond findings. More than 128 such
findings fail validation closed. The stable artifact reason is
`finding_budget_exhausted`. Validation retains the full-suite JUnit snapshots.
The private proof keeps the full case inventories. The worker reviews omitted
subsecond cases in the complete candidate list. An exhausted invocation or
time budget stops timing review with the stable reason
`focused_budget_exhausted`.

For interactive work, read the fixed full candidate JUnit files for both
supported Python versions after a successful hook. If a fixed file is missing
or `OSError`-unreadable, timing is `UNKNOWN`; continue without a timing pass,
times, or counts. A malformed or mismatched file that is present needs repair.
With valid files, a new or modified case strictly above 1.000 seconds is a
hard stop. Exactly 1.000 seconds is allowed by this rule. Unchanged inherited
cases do not trigger it. Cases above 100 ms remain advisory: record them for a
separate owner and follow-up. Apply any tighter controlled task limit.

[Paired local timing investigation](../how-to/validate-template-changes.md#investigate-local-test-timing)
can help assign cause. If paired proof is absent but candidate JUnit is valid,
the comparison is `unknown` and does not block by itself. Present malformed or
mismatched paired proof is `inconclusive` and blocks approval until repaired.
An `unknown` comparison never waives the candidate JUnit hard gate.
The comparison still lists candidate cases above 100 ms and at or above 1
second. A `review` status applies only to new or modified cases strictly above
1.000 seconds; it does not, by itself, prove a regression. Mixed case trends
need diagnosis.

Job duration helps diagnosis; it is not a publication gate. Image pulls,
setup, and scheduling may lengthen jobs without increasing JUnit case time.
The controller's human handoff requires at least one current required job
and success from every current required job for the exact MR source SHA.

## Slow cases and exceptions

Keep full per-run case lists, JUnit digests, trees, Python versions, phase
proof, and decisions in the durable audit or linked issue record. Give
confirmed inherited debt an owner and reduction plan. An integration
exception needs its case, Python version, reason for cost, owner, plan to
reduce or review it, and review trigger. It must fit the task budget.
One run does not set a stable baseline.

In this template's source repository, the case
`tests/test_template.py::test_copier_update_preserves_legacy_project_context`
has a human-approved integration exception on Python 3.11 and 3.14. It may
exceed 1 second because it runs a real Copier update. The maintainer owns this
exception and reviews it when the Copier or template update path changes, or
when case timing regresses. Keep reporting the case in the timing inventory.

| Integration case | Versions | Cost and owner | Review trigger |
| --- | --- | --- | --- |
| `tests/test_agent_work_mcp_server.py::test_candidate_mcp_stdio_serves_live_broker_without_exposing_private_inputs` | Python 3.11 and 3.14 | A trusted interpreter starts through `uv`, then an MCP stdio process connects to a live broker. Process startup and full-suite contention can push the case over 1 second. The maintainer owns cost reduction. | Review when this process path changes or a focused call reaches 1 second. Keep the case in the timing inventory. |
