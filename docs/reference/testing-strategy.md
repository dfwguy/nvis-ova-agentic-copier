---
title: Testing strategy
type: reference
summary: Look up test design, validation layers, and local case timing rules.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-26
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
version. Exactly 100 ms meets the goal. Use the JUnit `testcase` `time`.
It includes setup, the test body, and teardown charged to that case.
Keep each test parameter as a distinct case. A session fixture can charge
shared setup to its first case. Check phase proof before naming the cause.

List **every** local case above 100 ms and at or above **1 second** for
each supported Python version. Each case at or above 1 second needs a full
report and review. Report each case and version even if another timing
finding sends the change back. Keep a slow inherited case in view.
If it is no slower, its cost alone does not reject the change. One inherited
setup spike does not prove a regression or block an unrelated change.

An added ordinary test that stays above 100 ms in repeat runs goes back to
its owner unless it has an approved integration exception. A changed test
goes back if it adds a cost above 100 ms. It also goes back for a proven
slowdown caused by the test, fixture, or code. Include cases that use a
changed shared fixture or helper when they can be found. Map the affected
cases before approval. Apply any tighter task limit as well.

In interactive work, compare at least two focused runs on each exact
baseline and candidate tree. Use equal run counts and the same environment.
To claim a regression, show repeat slowdown, phase proof, like environments,
and a diff that backs the cause. A comparator `review` flag or one run does
not prove cause.
Missing, mismatched, or partial paired proof blocks approval. Full paired
proof may show mixed case trends. Keep those cases for diagnosis. Such doubt
alone does not block an unrelated change if its impact is clear and no other
rule blocks it. Do not call a mixed case a pass or a regression.

When the active serial worker supplies candidate-only timing proof, a touched
case above 100 ms goes back to the test owner unless it has a documented
integration exception. Candidate-only proof cannot show a baseline or compare
phases. Require the full case list and its `timing_complete` flag for each
Python version. The three slowest cases do not show the full list.

## Slow cases and exceptions

Keep the full list for each run in the durable audit or linked issue record.
Keep JUnit digests, tree and Python version, phase proof, and the review
decision there too. Record a newly seen ordinary case at or above 1 second
there. Then repeat the run and find the cause. Give confirmed inherited debt
an owner and a plan to cut its cost. An integration exception needs its case,
Python version, reason for the cost, owner, plan to cut or review it, and
review trigger in that record. It must still fit the task budget. One run
does not set a stable baseline.

Generated project setup, real Copier updates, repo scans, and shared
fixtures may make a case slow. Measure setup, call, and teardown before
blaming the test body. Keep run times and case decisions with their proof.
