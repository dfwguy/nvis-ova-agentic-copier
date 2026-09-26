---
title: Testing strategy
type: reference
summary: Look up test layers, case timing targets, and the register of long-running tests.
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

For each added or modified test case, including its parameterizations, a local
JUnit duration **greater than 100 ms** in either supported Python environment
returns the change to the test owner for correction. Apply this rule also to
identifiable cases affected by a changed shared fixture or helper. If you
cannot tell which cases are affected, flag the gap for action before approval.
Exactly 100 ms is allowed. Measure the JUnit `testcase` `time` value, which
includes setup, the test body, and teardown attributed to that case. A session
fixture can make its first consuming case look slow. Inspect fixture use before
assigning a cause. CI case times help diagnose load and environment effects;
this local rule is not a CI gate.

Independently, every local case at or above **1 second** triggers review and
entry in the long-running register for each supported Python environment.
Report every missing case and environment even if another timing finding
already returns the change for correction. This register trigger alone does
not reject the change. One timing observation does not prove a regression;
compare repeat evidence before making that claim.

Keep a documented integration case that needs more time in the register below.
Record why it needs that time and how its cost will be reduced or reviewed.
An inherited slow local case remains tracked debt. Diagnose a newly observed
ordinary case at or above 1 second. A regression shown by repeat runs with no
clear cause is a review finding. Apply any tighter task-specific limit as well.
A documented integration exception still needs a plan and must fit the task's
budget.

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

The Judge snapshot for tree `1f3d65ed1a90155ae18dc90336b3a467afd40587`
was recorded on 2026-09-26 at 00:46:42 UTC. Its Python 3.11 and 3.14 JUnit
SHA-256 digests are `541abf9ce249f92757ed451497921675a289cc296a08cb97867c5e825294487f`
and `d4d6c7f7f96927b0e275359cf4094e09ddaa58f36c404ae55f8ee1c4db80e565`.
It found these further register entries. The durations are single local
observations; setup, body, and teardown costs have not been split.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_agent_work_state_machine::test_decision_review_renderer_neutralizes_all_heading_levels_and_markers` | 3.11 | 2.808 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Measure setup, body, and teardown; reduce avoidable work. |
| `tests.test_licenses::test_repository_template_and_rendered_project_satisfy_license_policy` | 3.11 | 2.495 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Measure fixture, scan, and teardown costs; reduce avoidable work. |
| `tests.test_template::test_serial_runner_ships_completion_relative_user_systemd_units` | 3.11 | 1.172 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Measure setup, body, and teardown; reduce avoidable work. |
| `tests.test_template::test_trusted_generated_project_runs_setup_tasks` | 3.14 | 1.714 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Measure setup, task, and teardown costs; reduce avoidable work. |

Another Judge snapshot for tree `e4488acfcc8c8347fe425da4dddf4e54e3408749`
found one more Python 3.11 case at or above 1 second on 2026-09-26. Its JUnit
SHA-256 digest is
`1f5d21a8f214fe094393fc2e141c4cf5ce8429a87e1a8ea899b388b0b4f29aeb`.
This is one local observation; its cause and phase costs are unmeasured.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_copier_answers_file_generated` | 3.11 | 1.364 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown; reduce avoidable cost before judging whether a long run is needed. |

A Judge snapshot for tree `8eb34b3eb673486ec4a96716aabab823a50de981`
on 2026-09-26 found two further local entries. The Python 3.11 JUnit SHA-256
digest is `9d373d73ccd9e07f0499851c45d2c20dfe0c2bcb603d44e67f9972ce537a6f05`.
The Python 3.14 digest is
`8701da144e614291b507828b850f78c8e3ae05ed9f021fbec330299906b24211`.
These are single observations with no measured cause or phase split.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_trusted_generated_project_runs_setup_tasks` | 3.11 | 2.017 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown; reduce avoidable cost before judging whether a long run is needed. |
| `tests.test_template::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/template_update_publication.py]` | 3.14 | 1.026 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown; reduce avoidable cost before judging whether a long run is needed. |

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

For the full current local list in interactive work, read the latest
supported-version Judge JUnit proof and select each `testcase` whose numeric
`time` is at least 1 second. In serial work, use the complete trusted
`over_100ms_cases` list for each supported environment only when its
`timing_complete` flag is true. An absent field or false flag makes the proof
incomplete and rejects the review. The three slowest names cannot establish a
complete inventory.

Timing evidence may show an unsafe JUnit parameter as a stable `[param_<hash>]`
suffix on its safe base test name. The hash has 64 lowercase SHA-256 hex
digits. Each suffix identifies a distinct case. Match the emitted case ID and
Python environment when checking the register. Do not request the raw
parameter text.

For CI diagnostics, use the project's
`pipelines/<pipeline-id>/test_report` GitLab API endpoint. Compare cases by
full name and Python environment. Record the proof or pipeline ID, date,
duration, interpretation, owner, and next action when updating a table. Do
not infer improvement from one run alone.
