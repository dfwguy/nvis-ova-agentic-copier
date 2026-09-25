---
title: Testing strategy
type: reference
summary: Look up test layers, case timing targets, and the register of long-running tests.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-25
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

Tests should give maintainers a fast answer during a change and still cover the
supported environments before publication. The [validation guide](../how-to/validate-template-changes.md)
lists the commands and CI jobs. This page records local test timing goals and
known long-running cases.

## Test layers

| Layer | Purpose and evidence |
| --- | --- |
| Focused tests | Run the relevant behavior tests while developing a change. Keep setup small enough that one failure can be diagnosed quickly. |
| Full supported matrix | Run the complete pytest suite on Python 3.11 and 3.14 through tox and the validation hooks. The Judge's JUnit files provide case counts and durations for each environment. |
| CI | Run the supported matrix in native pytest shards and the other validation jobs. Shards distribute work but do not shorten an individual test or fixture. |

For local tests, aim for each ordinary case to finish **below 1 second**.
**Below 100 ms** is the ideal when the behavior permits it. A local case at or
above 1 second triggers review and entry in the register; it is not an
automatic failure. Measure the JUnit `testcase` `time` value, which includes
setup, the test body, and teardown attributed to that case. A session fixture
can make its first consuming case look slow. Inspect fixture use and repeat the
local measurement before assigning a cause. Apply the goals in each supported
Python environment. CI case times help diagnose CI load and environment
effects, but the local timing goals are not CI gates.

Keep a documented integration case that needs more time in the register below.
Record why it needs that time and how its cost will be reduced or reviewed.
An inherited slow local case remains tracked debt. Diagnose a newly observed
ordinary case at or above 1 second and check repeat evidence before calling it
a regression. A corroborated, unexplained regression is an actionable review
finding. Apply any tighter task-specific limit as well. A documented
integration exception still needs a plan and must fit the task's budget.

The project's **60-second preferred and 120-second hard pipeline limits**
measure the longest terminal job's execution. They are separate from the
per-test target. Image pulls, job setup, and scheduling can lengthen a job or
pipeline without increasing a JUnit case duration. Queue time is excluded from
the job limit. Use job evidence for those costs rather than assigning them to a
test.

## Long-running local test register

The local Judge JUnit snapshot for issue #249 was recorded on 2026-09-25 at
22:01 UTC for tree `b3d87ef7ea12757c877a4f682ce2c4cf28ea6d2d`. Its
Python 3.11 and 3.14 JUnit SHA-256 digests begin `0203262df3f6` and
`40a83c24d109`, respectively. Each file has 2,763 cases. On Python 3.11,
2,638 cases (95.5%) finished below 100 ms and five reached 1 second. On
Python 3.14, 2,647 (95.8%) finished below 100 ms and four reached 1 second.
This is one local observation, not a stable baseline.

| Case or shared fixture | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_generated_pre_commit_uses_only_local_hooks` | 3.11 | 1.783 s | Local #249 JUnit; 2026-09-25 | Generated-project check; phase cost unmeasured. | Test maintainers | Separate setup from call time, then reduce avoidable work. |
| `tests.test_template::test_copier_update_preserves_legacy_project_context` | 3.11 | 3.558 s | Local #249 JUnit; 2026-09-25 | Real Copier update; phase cost unmeasured. | Test maintainers | Review integration need and measure setup and update cost. |
| `tests.test_agent_work_worker_result_broker::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 3.11 | 1.003 s | Local #249 JUnit; 2026-09-25 | Blocked-callback cleanup test; timing cause unconfirmed. | Test maintainers | Measure waits and check for a shorter controlled boundary. |
| `tests.test_agent_work_worker_result_broker::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 3.11 | 1.019 s | Local #249 JUnit; 2026-09-25 | Blocked-callback cleanup test; timing cause unconfirmed. | Test maintainers | Measure waits and check for a shorter controlled boundary. |
| `tests.test_agent_work_state_machine::test_decision_review_draft_bounds_the_fully_rendered_description` | 3.11 | 2.000 s | Local #249 JUnit; 2026-09-25 | Description validation; phase cost unmeasured. | Test maintainers | Profile the case and reduce avoidable work. |
| `tests.test_licenses::test_repository_template_and_rendered_project_satisfy_license_policy` | 3.14 | 2.640 s | Local #249 JUnit; 2026-09-25 | Repository and rendered-project scan; phase cost unmeasured. | Test maintainers | Measure scan and setup costs separately. |
| `tests.test_agent_work_worker_result_broker::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 3.14 | 1.005 s | Local #249 JUnit; 2026-09-25 | Blocked-callback cleanup test; timing cause unconfirmed. | Test maintainers | Measure waits and check for a shorter controlled boundary. |
| `tests.test_agent_work_worker_result_broker::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 3.14 | 1.012 s | Local #249 JUnit; 2026-09-25 | Blocked-callback cleanup test; timing cause unconfirmed. | Test maintainers | Measure waits and check for a shorter controlled boundary. |
| `tests.test_template::test_copier_update_preserves_legacy_project_context` | 3.14 | 2.300 s | Local #249 JUnit; 2026-09-25 | Real Copier update; phase cost unmeasured. | Test maintainers | Review integration need and measure setup and update cost. |
| `generated_project` and `template_source` session fixtures (`tests/conftest.py`) | Both | Not measured separately | Fixture definitions, lines 32 and 50; 2026-09-25 | Setup is charged to the first consuming JUnit case. | Test maintainers | Issue #260 owns the render-only and trusted-task fixture split and setup timing. |

Pytest's duration report can show setup, call, and teardown as separate
phases. The current JUnit `testcase` time combines them. Keep the combined
time for the review trigger and use phase evidence to diagnose a slow case.

## CI timing context

This is **one CI observation** from merge request !314, pipeline
69850880, on 2026-09-25. Its GitLab test report is at
`projects/310967/pipelines/69850880/test_report`. It reported 2,732 cases per
Python version. The distribution was:

| Python | Below 100 ms | At or above 1 second | 90th percentile | 95th percentile | 99th percentile | Maximum |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3.11 | 2,468 (90.34%) | 60 (2.20%) | 0.093 s | 0.468 s | 5.652 s | 32.546 s |
| 3.14 | 2,560 (93.70%) | 24 (0.88%) | 0.049 s | 0.197 s | 0.743 s | 10.501 s |

The rows below show the slowest CI cases. They are not stable baselines or a
claim that the shared fixture caused every slow case. CI load, image, and
environment differences can affect them. Use this data to diagnose CI, not to
enforce the local case target. Repeat the measurement before inferring change.

| Case or shared fixture | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/gitlab_safeguards.py]` | 3.11 | 32.546 s | !314 pipeline 69850880; 2026-09-25 | May include first-use fixture setup; attribution needs checking. | Test maintainers | Under issue #260, measure render-only and trusted-task setup separately, then the case again. |
| `test_generated_package_directory` | 3.11 | 28.636 s | !314 pipeline 69850880; 2026-09-25 | Generated-project work or shared setup may contribute; unconfirmed. | Test maintainers | Profile case and fixture separately; reduce repeated generation where safe. |
| `test_portable_markdown_mirrors_match_generated_output` | 3.11 | 26.483 s | !314 pipeline 69850880; 2026-09-25 | Generated-output comparison; cause unconfirmed. | Test maintainers | Inspect setup attribution and reduce repeated work. |
| `test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/state_machine.py]` | 3.11 | 25.714 s | !314 pipeline 69850880; 2026-09-25 | Generated-output comparison; cause unconfirmed. | Test maintainers | Inspect setup attribution and reduce repeated work. |
| `test_no_jinja_extension_in_output` | 3.11 | 25.569 s | !314 pipeline 69850880; 2026-09-25 | Generated-output check; cause unconfirmed. | Test maintainers | Inspect setup attribution and reduce repeated work. |
| `test_repository_template_and_rendered_project_satisfy_license_policy` | 3.14 | 10.501 s | !314 pipeline 69850880; 2026-09-25 | Repository and rendered-project scan; cause unconfirmed. | Test maintainers | Separate scan from fixture cost and reduce avoidable work. |
| `test_generated_pre_commit_uses_only_local_hooks` | 3.14 | 10.339 s | !314 pipeline 69850880; 2026-09-25 | Generated-project check; cause unconfirmed. | Test maintainers | Inspect setup attribution and reduce repeated work. |
| `test_ci_trusts_only_the_exact_checkout_directory[rendered]` | 3.14 | 10.221 s | !314 pipeline 69850880; 2026-09-25 | Rendered-project check; cause unconfirmed. | Test maintainers | Inspect setup attribution and reduce repeated work. |

For the full current local list, read the latest supported-version Judge JUnit
proof and select each `testcase` whose numeric `time` is at least 1 second.
For CI diagnostics, use the project's
`pipelines/<pipeline-id>/test_report` GitLab API endpoint. Compare cases by
full name and Python environment. Record the proof or pipeline ID, date,
duration, interpretation, owner, and next action when updating a table. Do
not infer improvement from one run alone. The serial agent receives only the
three slowest names per Python environment, so use full JUnit or the GitLab
report for a complete inventory in the matching setting.
