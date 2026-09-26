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

Job duration is useful diagnostic evidence, but it is not a publication
gate. Image pulls, job setup, and scheduling can lengthen a job or pipeline
without increasing a JUnit case duration. Use job evidence for those costs
rather than assigning them to a test. The controller's human handoff requires
at least one current required job and success from every current required job
for the exact MR source SHA.

## Long-running local test register

The local Judge JUnit snapshot for issue #249 was recorded on 2026-09-25 at
22:01 UTC for tree `b3d87ef7ea12757c877a4f682ce2c4cf28ea6d2d`. Its
Python 3.11 and 3.14 JUnit SHA-256 digests begin `0203262df3f6` and
`40a83c24d109`, respectively. Each file has 2,763 cases. On Python 3.11,
2,638 cases (95.5%) finished below 100 ms and five reached 1 second. On
Python 3.14, 2,647 (95.8%) finished below 100 ms and four reached 1 second.
This is one local observation, not a stable baseline.

Issue #251's immutable Python 3.11 JUnit tree
`becbc74cf4fa74cd37aac6084c38c5314e3a55db` has SHA-256
`ba8c6b0d37745d3efa81d896cf8455e964aa8de35df3002cdcaa0bcc019b494e`.
On 2026-09-26 it recorded the two new cases below. This is one observation,
not a regression.

The later immutable tree `b9a0655612683e205ce0b8d47978cb6faef4932d`
has Python 3.11 JUnit SHA-256
`d5bd1d2a15433ef73494a753bf1621edba78ab5a7cc67092341238b09f1a4944`
and Python 3.14 JUnit SHA-256
`14f38269bb4aba7066b8862fbab730c07a8ad685b050272e45b007aaf928471a`.
Its 2026-09-26 times below are one observation, not a regression.

| Case or shared fixture | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_generated_pre_commit_uses_only_local_hooks` | 3.11 | 1.783 s | Local #249 JUnit; 2026-09-25 | Generated-project check; phase cost unmeasured. | Test maintainers | Separate setup from call time, then reduce avoidable work. |
| `tests.test_template::test_copier_update_preserves_legacy_project_context` | 3.11 | 3.558 s | Local #249 JUnit; 2026-09-25 | Real Copier update; phase cost unmeasured. | Test maintainers | Review integration need and measure setup and update cost. |
| `tests.test_agent_work_worker_result_broker::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 3.11 | 1.003 s (historical) | Local #249 JUnit; 2026-09-25. Focused #267 repeats; 2026-09-26. | The 1 s close join caused the old duration; focused repeats now take 0.013 / 0.012 s. | Test maintainers | The test uses a 0.01 s close join; monitor later full-suite JUnit. Production retains the 1.0 s default. |
| `tests.test_agent_work_worker_result_broker::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 3.11 | 1.019 s (historical) | Local #249 JUnit; 2026-09-25. Focused #267 repeats; 2026-09-26. | The 1 s close join caused the old duration; focused repeats now take 0.022 / 0.023 s. | Test maintainers | The test uses a 0.01 s close join; monitor later full-suite JUnit. Production retains the 1.0 s default. |
| `tests.test_agent_work_state_machine::test_decision_review_draft_bounds_the_fully_rendered_description` | 3.11 | 2.000 s | Local #249 JUnit; 2026-09-25 | Description validation; phase cost unmeasured. | Test maintainers | Profile the case and reduce avoidable work. |
| `tests.test_licenses::test_repository_template_and_rendered_project_satisfy_license_policy` | 3.14 | 2.640 s | Local #249 JUnit; 2026-09-25 | Repository and rendered-project scan; phase cost unmeasured. | Test maintainers | Measure scan and setup costs separately. |
| `tests.test_agent_work_worker_result_broker::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 3.14 | 1.005 s (historical) | Local #249 JUnit; 2026-09-25. Focused #267 repeats; 2026-09-26. | The 1 s close join caused the old duration; focused repeats now take 0.013 / 0.013 s. | Test maintainers | The test uses a 0.01 s close join; monitor later full-suite JUnit. Production retains the 1.0 s default. |
| `tests.test_agent_work_worker_result_broker::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 3.14 | 1.012 s (historical) | Local #249 JUnit; 2026-09-25. Focused #267 repeats; 2026-09-26. | The 1 s close join caused the old duration; focused repeats now take 0.022 / 0.022 s. | Test maintainers | The test uses a 0.01 s close join; monitor later full-suite JUnit. Production retains the 1.0 s default. |
| `tests.test_template::test_copier_update_preserves_legacy_project_context` | 3.14 | 2.300 s | Local #249 JUnit; 2026-09-25 | Real Copier update; phase cost unmeasured. | Test maintainers | Review integration need and measure setup and update cost. |
| `tests.test_template::test_generated_lock_does_not_depend_on_its_own_project` | 3.11 | 1.917 s | Issue #251 immutable JUnit; 2026-09-26 | Unmeasured; fixture or setup may contribute. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce avoidable work or document an exception. |
| `tests.test_template::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/runtime.py]` | 3.11 | 1.435 s | Issue #251 immutable JUnit; 2026-09-26 | Unmeasured; fixture or setup may contribute. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce avoidable work or document an exception. |
| `tests.test_template::test_trusted_generated_project_runs_setup_tasks` | 3.11 | 2.056 s | Immutable JUnit tree `b9a0655612683e205ce0b8d47978cb6faef4932d`; 2026-09-26 | Phase unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce work or document an exception. |
| `tests.test_template::test_tests_init_rendered` | 3.11 | 1.108 s | Immutable JUnit tree `b9a0655612683e205ce0b8d47978cb6faef4932d`; 2026-09-26 | Phase unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce work or document an exception. |
| `tests.test_template::test_agent_loop_wrapper_refreshes_advanced_default_branch_before_next_activation` | 3.11 | 1.036 s | Immutable JUnit tree `b9a0655612683e205ce0b8d47978cb6faef4932d`; 2026-09-26 | Phase unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce work or document an exception. |
| `tests.test_template::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/systemd_smoke.py]` | 3.14 | 1.028 s | Immutable JUnit tree `b9a0655612683e205ce0b8d47978cb6faef4932d`; 2026-09-26 | Phase unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown. Then reduce work or document an exception. |
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

The Judge snapshot for tree `4ad4674c9f50facf75296041b4d17edfa6b39ea8`
found one more Python 3.11 case at or above 1 second on 2026-09-26.
Its JUnit SHA-256 digest is
`c0993b899732fc8b5b34e7ffc80ac0ec20e543f58e5cf3068ebf26c0e889343e`.
This is one local observation, not a proven regression. Its cause and phase
costs are unmeasured.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_portable_ci_keeps_one_shared_setup_job` | 3.11 | 1.101 s | Local Judge JUnit; 2026-09-26 | Cause and phase costs unmeasured. | Test maintainers | Repeat and measure setup, body, and teardown; reduce avoidable work. |

The issue #271 Judge snapshot for tree
`b5329cf41ba1def31b1b02efaab6f75237daf672` has 2,866 cases in each
supported environment. Its Python 3.11 and 3.14 JUnit SHA-256 digests are
`896d5e4282546371119723d00c072a6c5aede004b620099b0b7fdc8b18c9c3e8`
and `4fc89a84a30f26c5b189b557788d59ef2631bf71fd92ede3a72b22e6ff36a7ed`.
The three new entries below are single observations from 2026-09-26. A
concurrent issue #270 tox run confounded host load; neither a cause nor a
regression is confirmed.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_portable_ci_is_mirrored_without_the_private_root_image` | 3.11 | 1.812 s | Issue #271 Judge JUnit; 2026-09-26 | Cause unconfirmed; phase cost unmeasured. | Test maintainers | Repeat under quiet load and measure setup and call under issue #263. |
| `tests.test_agent_work_serial_runtime::test_status_dispatch_caps_sorted_listing_and_exact_lookup_bypasses_listing` | 3.11 | 1.032 s | Issue #271 Judge JUnit; 2026-09-26 | Cause unconfirmed; phase cost unmeasured. | Test maintainers | Repeat under quiet load and measure setup and call under issue #263. |
| `tests.test_template::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/state_machine.py]` | 3.14 | 1.541 s | Issue #271 Judge JUnit; 2026-09-26 | Cause unconfirmed; phase cost unmeasured. | Test maintainers | Repeat under quiet load and measure setup and call under issue #263. |

The later issue #271 Judge snapshot for tree
`b300459b5077160061f000691d20f07c8cd92ec3` recorded 2,875 Python
3.11 cases. Its JUnit SHA-256 digest is
`633e056ce0d6bab46dd79be8f12ce358b85eafc4b38a47551f883780f10fa739`.
The case below is one observation from 2026-09-26. Its cause and phase costs
are unconfirmed; this run does not establish shared-fixture attribution.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/serial.py]` | 3.11 | 1.306 s | Issue #271 Judge JUnit; 2026-09-26 | Cause unconfirmed; phase cost unmeasured. | Test maintainers | Repeat under quiet load and measure setup, body, and teardown under issue #263. |

The issue #271 Judge snapshot for staged tree
`686a80568b85139bca69be969766742eab83cbfd` has Python 3.11 JUnit
SHA-256 `38985735a7c958b50bcc3f2e8f7310e9ea33e1f906974c9fc7d76b0d47fc539d`.
The entry below is one observation from 2026-09-26; its cause and phase costs
are unconfirmed, and it does not establish a regression.

| Case | Python | Observed duration | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | --- | --- | --- | --- |
| `tests.test_template::test_ci_trusts_only_the_exact_checkout_directory[root]` | 3.11 | 1.311 s | Issue #271 Judge JUnit; 2026-09-26 | Cause unconfirmed; phase cost unmeasured. | Test maintainers | Repeat under quiet load and measure setup, call, and teardown under issue #272. |

The later issue #271 full-hook JUnit for tree
`9321a35bf7f911f5edc7fee5ce6edb0596021101` has Python 3.11 and 3.14
SHA-256 digests
`7157ecc093a0468eda0548382aea75e2a27b3bb41da8b5a3a52a364a9525fc30`
and `c5f35c0f025a03f84a0f956de83e5addc8bec03a9342091bc9f6045db772d732`.
The quiet-repeat JUnit digests are
`704d201220d79c2d6591681d1a0678d5e22d3d5502022313a4b4404f875334f7`
and `fd4f228a0acccbeeda89a30e2a65bea5ec93268fc3297416c6ae83fa679754ea`,
respectively. These are observations from 2026-09-26, not a regression claim.

| Case | Python | Full-hook duration | Quiet repeat | Evidence and date | Cause or interpretation | Owner | Reduction or exception plan |
| --- | --- | ---: | ---: | --- | --- | --- | --- |
| `tests.test_template::test_ci_trusts_only_the_exact_checkout_directory[rendered]` | 3.11 | 1.452 s | 0.662 s | Issue #271 JUnit; 2026-09-26 | Cause and phase costs unconfirmed. | Test maintainers | Measure setup, call, and teardown under issue #272. |
| `tests.test_template::test_agent_loop_wrapper_refreshes_advanced_default_branch_before_next_activation` | 3.14 | 1.172 s | 5.914 s | Issue #271 JUnit; 2026-09-26 | Cause and phase costs unconfirmed. | Test maintainers | Measure setup, call, and teardown under issue #272. |

Pytest's duration report can show setup, call, and teardown as separate
phases. The current JUnit `testcase` time combines them. Keep the combined
time for the review trigger and use phase evidence to diagnose a slow case.

### Local timing observation on 2026-09-26

The full-suite runs below used committed baseline
`75cfe541b2c671cb4b32cb11a9773cdfbbbda5b7` (Git tree
`9a60ecaf6ac175a1fd5a665373d91400946d18cc`) between 02:30 and
02:35 UTC. Unstaged documentation edits were present during measurement;
host load was not quantified, although no other issue checkout was known to
be running full tox. The commands were
`PYTEST_ADDOPTS='--randomly-seed=262' uv run tox -e py311` and the same
command with `py314`, but tox did not pass that value to isolated pytest.
Pytest reported seed `895328052` for Python 3.11 and `1546474014` for
Python 3.14. Both ran on the prepared tox environments. All 2,787
cases passed in each environment, with no failures or errors. This is one
local full-suite observation, not a stable baseline. An earlier Python 3.11
run overlapped other host tests and is excluded from this inventory.

| Python | Full JUnit SHA-256 | Cases above 100 ms | Cases at or above 1 s |
| --- | --- | ---: | ---: |
| 3.11 | `5dde105e59f274006a8a256b4b17bffeaf7feb3fadbfbe84743b9f74e4626bfa` | 114 | 7 |
| 3.14 | `54fbfc3d3b8c085ed288ef31e1ca4e252e68ecb67da807a0c637fe42fee47e85` | 110 | 6 |

Every case at or above 1 second in those full JUnit files appears below.
No test case was changed by this documentation-only work, so the changed-case
100 ms rule selected no additional cases. The two focused repeats used all
listed node IDs in each environment and the same prepared interpreter. Both
used explicit seed 262, `--no-cov`, and `--durations=0`; the full tox runs
used the seeds reported above. Focused times diagnose phases; they do not
replace the full JUnit inventory. The focused JUnit SHA-256 digests were
`a8a9715999f5973e7a65d5611f3c32a7f2a6074baa0c9faccf827e860e5abd50`
and `7a20552c0b96353a8aacc078a789215340855d543bad7fa8541c21bb21ade47b`
for Python 3.11, then
`8d5e4d383ec80ad0e42c19da6723445a4bc2225b525e8a6e53757fd287fb4efb`
and `f826cfcc14a6dc13cadd084500931c70d69b7818e146b9485f56e64e3d6c6462`
for Python 3.14.

| Python | Case (pytest node ID) | Full JUnit | Focused 1 / 2 | Phase finding | Owner and reduction or exception plan |
| --- | --- | ---: | ---: | --- | --- |
| 3.11 | `tests/test_template.py::test_copier_update_preserves_legacy_project_context` | 2.898 s | 2.923 / 2.586 s | Call dominates both repeats. | Test maintainers: review the needed Copier integration work and reduce avoidable update cost. |
| 3.11 | `tests/test_licenses.py::test_repository_template_and_rendered_project_satisfy_license_policy` | 1.979 s | 1.979 / 2.007 s | Setup is 1.59 / 1.62 s; call is about 0.39 s. | Test maintainers: use the fixture split recorded in issue #260 to reduce shared work. |
| 3.11 | `tests/test_agent_work_state_machine.py::test_decision_review_draft_bounds_the_fully_rendered_description` | 1.894 s | 0.404 / 0.394 s | Focused call is about 0.40 s. | Test maintainers: repeat under comparable full-suite load before assigning a regression. |
| 3.11 | `tests/test_template.py::test_agent_loop_wrapper_fast_forwards_before_applied_worker` | 1.125 s | 0.544 / 0.528 s | Call dominates focused repeats. | Test maintainers: queue serial wrapper diagnosis through issue #263. |
| 3.11 | `tests/test_template.py::test_serial_runner_assets_have_exact_root_template_and_generated_parity[src/agent_work/repository.py]` | 1.056 s | 0.643 / 0.649 s | Focused setup is 0.64 / 0.65 s. | Test maintainers: inspect first-use setup against the fixture split recorded in issue #260. |
| 3.11 | `tests/test_agent_work_worker_result_broker.py::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 1.013 s (historical) | 1.012 / 1.012 s (historical); 0.022 / 0.023 s after #267 | The old call held near 1.01 s because of the close join; focused repeats on 2026-09-26 show the shorter test bound removes that wait. | Test maintainers: monitor later full-suite JUnit; production retains the 1.0 s default. |
| 3.11 | `tests/test_agent_work_worker_result_broker.py::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 1.002 s (historical) | 1.003 / 1.003 s (historical); 0.013 / 0.012 s after #267 | The old call held near 1.00 s because of the close join; focused repeats on 2026-09-26 show the shorter test bound removes that wait. | Test maintainers: monitor later full-suite JUnit; production retains the 1.0 s default. |
| 3.14 | `tests/test_template.py::test_copier_update_preserves_legacy_project_context` | 2.753 s | 8.169 / 2.733 s | Call dominates; the first focused spike is unexplained. | Test maintainers: repeat under quiet conditions and inspect Copier update work before claiming a regression. |
| 3.14 | `tests/test_licenses.py::test_repository_template_and_rendered_project_satisfy_license_policy` | 2.211 s | 1.999 / 1.979 s | Setup is 1.59 / 1.55 s; call is 0.41 / 0.43 s. | Test maintainers: use the fixture split recorded in issue #260 to reduce shared work. |
| 3.14 | `tests/test_template.py::test_readability_dependency_and_generated_ci_helper_are_rendered_portably` | 1.084 s | 0.680 / 0.686 s | Focused setup is about 0.68 s. | Test maintainers: inspect first-use setup against the fixture split recorded in issue #260. |
| 3.14 | `tests/test_template.py::test_agent_loop_wrapper_stops_on_refresh_command_failure[merge]` | 1.039 s | 0.484 / 0.560 s | Call dominates focused repeats. | Test maintainers: queue serial wrapper diagnosis through issue #263. |
| 3.14 | `tests/test_agent_work_worker_result_broker.py::test_candidate_broker_deferred_cleanup_waits_for_blocked_callback` | 1.012 s (historical) | 1.012 / 1.013 s (historical); 0.022 / 0.022 s after #267 | The old call held near 1.01 s because of the close join; focused repeats on 2026-09-26 show the shorter test bound removes that wait. | Test maintainers: monitor later full-suite JUnit; production retains the 1.0 s default. |
| 3.14 | `tests/test_agent_work_worker_result_broker.py::test_broker_eventually_cleans_up_after_blocked_builder_returns` | 1.002 s (historical) | 1.003 / 1.003 s (historical); 0.013 / 0.013 s after #267 | The old call held near 1.00 s because of the close join; focused repeats on 2026-09-26 show the shorter test bound removes that wait. | Test maintainers: monitor later full-suite JUnit; production retains the 1.0 s default. |

The full-suite outliers that fell below 1 second in both focused repeats need
more comparable runs before anyone calls them regressions. Issue
[#255](https://gitlab-master.nvidia.com/nvis-ova/nvis-ova-agentic-copier/-/issues/255)
records nested outer UV fixture work, issue
[#260](https://gitlab-master.nvidia.com/nvis-ova/nvis-ova-agentic-copier/-/issues/260)
records the fixture split, and issue
[#263](https://gitlab-master.nvidia.com/nvis-ova/nvis-ova-agentic-copier/-/issues/263)
will automate the repair loop and queue follow-up diagnosis. The [local timing method](../how-to/validate-template-changes.md#investigate-local-test-timing)
explains how to collect the next observation.

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
