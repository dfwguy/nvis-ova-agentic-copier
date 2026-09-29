# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for source-bound JUnit timing decisions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

BASE_TREE = "a" * 40
CANDIDATE_TREE = "b" * 40
SEED = "73119"
CASE = "tests/test_speed.py::test_existing"
NEW_CASE = "tests/test_speed.py::test_added[param]"
ENVIRONMENTS = ("py311", "py314")


def _full_run(tree: str, environment: str, cases: dict[str, float]) -> dict[str, Any]:
    return {
        "source_tree": tree,
        "environment": environment,
        "prepared_environment": f"prepared-{environment}",
        "argv": ("uv", "run", "tox", "run-parallel"),
        "seed": SEED,
        "junit_sha256": "c" * 64,
        "test_count": len(cases),
        "timing_complete": True,
        "cases": cases,
    }


def _runs(
    baseline_cases: dict[str, float], candidate_cases: dict[str, float]
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    return (
        {env: _full_run(BASE_TREE, env, baseline_cases.copy()) for env in ENVIRONMENTS},
        {
            env: _full_run(CANDIDATE_TREE, env, candidate_cases.copy())
            for env in ENVIRONMENTS
        },
    )


def _focused(**request: Any) -> dict[str, Any]:
    return {
        "source_tree": request["source_tree"],
        "environment": request["environment"],
        "prepared_environment": request["prepared_environment"],
        "argv": request["argv"],
        "seed": request["seed"],
        "cases": {
            name: {"setup": 0.01, "call": 0.19, "teardown": 0.01}
            for name in request["case_names"]
        },
    }


def _review(
    baseline_cases: dict[str, float],
    candidate_cases: dict[str, float],
    *,
    changed_tests: tuple[str, ...] = (),
    affected_by_helper: tuple[str, ...] = (),
    production_changed: bool = False,
    focused: Callable[..., dict[str, Any]] = _focused,
) -> Any:
    from agent_work.test_timing import review_test_timings

    baseline, candidate = _runs(baseline_cases, candidate_cases)
    return review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=changed_tests,
        affected_by_helper=affected_by_helper,
        production_changed=production_changed,
        run_focused=focused,
    )


def test_touched_case_just_over_100ms_is_reported_in_both_versions() -> None:
    result = _review({CASE: 0.099}, {CASE: 0.101}, changed_tests=(CASE,))

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, "touched_slow") for env in ENVIRONMENTS
    }


def test_new_slow_parameter_case_requires_correction_and_focused_review() -> None:
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review(
        {CASE: 0.08},
        {CASE: 0.08, NEW_CASE: 0.11},
        changed_tests=("tests/test_speed.py::test_added",),
        focused=focused,
    )

    assert result.outcome == "correction_required"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, NEW_CASE, "new_slow") for env in ENVIRONMENTS
    }
    assert {
        (request["environment"], request["source_tree"]) for request in requests
    } == {(env, CANDIDATE_TREE) for env in ENVIRONMENTS}
    assert all(request["case_names"] == (NEW_CASE,) for request in requests)


def test_inherited_one_second_case_is_reported_without_correction() -> None:
    result = _review({CASE: 1.05}, {CASE: 1.05})

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, "inherited_slow") for env in ENVIRONMENTS
    }


@pytest.mark.parametrize(
    ("baseline_seconds", "candidate_seconds"),
    ((0.22, 1.22), (1.22, 2.02)),
)
def test_unaffected_existing_slowdown_is_inherited_without_focused_repeats(
    baseline_seconds: float, candidate_seconds: float
) -> None:
    result = _review(
        {CASE: baseline_seconds},
        {CASE: candidate_seconds},
        focused=lambda **_request: pytest.fail("unaffected case was focused"),
    )

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, "inherited_slow") for env in ENVIRONMENTS
    }


def test_untouched_inherited_slow_case_with_opposite_version_deltas_reports_only() -> (
    None
):
    case = "tests/test_template.py::test_copier_update_preserves_legacy_project_context"
    baseline, candidate = _runs({}, {})
    baseline["py311"]["cases"] = {case: 1.22}
    candidate["py311"]["cases"] = {case: 1.479614}
    baseline["py314"]["cases"] = {case: 1.52}
    candidate["py314"]["cases"] = {case: 1.273201}
    for run in (*baseline.values(), *candidate.values()):
        run["test_count"] = 1

    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        evidence = _focused(**request)
        calls = {
            ("py311", BASE_TREE): 1.2,
            ("py311", CANDIDATE_TREE): 1.459614,
            ("py314", BASE_TREE): 1.5,
            ("py314", CANDIDATE_TREE): 1.253201,
        }
        evidence["cases"] = {
            case: {
                "setup": 0.01,
                "call": calls[(request["environment"], request["source_tree"])],
                "teardown": 0.01,
            }
        }
        return evidence

    from agent_work.test_timing import review_test_timings

    result = review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=(),
        affected_by_helper=(),
        production_changed=True,
        run_focused=focused,
    )

    assert result.outcome == "report_only"
    assert candidate["py311"]["cases"][case] - baseline["py311"]["cases"][case] == (
        pytest.approx(0.259614)
    )
    assert candidate["py314"]["cases"][case] - baseline["py314"]["cases"][case] == (
        pytest.approx(-0.246799)
    )
    assert {(item.environment, item.name) for item in result.findings} == {
        (environment, case) for environment in ENVIRONMENTS
    }
    assert all(
        item.reason not in {"new_slow", "corroborated_regression"}
        for item in result.findings
    )
    assert (
        next(item.reason for item in result.findings if item.environment == "py311")
        == "inherited_variance"
    )
    assert (
        next(item.reason for item in result.findings if item.environment == "py314")
        == "inherited_slow"
    )
    assert next(
        item.phase_deltas for item in result.findings if item.environment == "py311"
    ) == pytest.approx({"setup": 0.0, "call": 0.259614, "teardown": 0.0})
    assert len(requests) == 4
    assert all(
        request["environment"] == "py311"
        and request["seed"] == SEED
        and request["case_names"] == (case,)
        for request in requests
    )
    assert {request["source_tree"] for request in requests} == {
        BASE_TREE,
        CANDIDATE_TREE,
    }

    affected = review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=(),
        affected_by_helper=(case,),
        run_focused=focused,
    )

    assert affected.outcome == "correction_required"
    assert any(
        item.environment == "py311"
        and item.name == case
        and item.reason == "corroborated_regression"
        for item in affected.findings
    )


@pytest.mark.parametrize(
    ("full_increase", "focused_increase"),
    ((1.0, 1.0), (1.0, 0.75), (0.75, 1.0)),
)
def test_inherited_variance_reaching_one_second_requires_correction(
    full_increase: float, focused_increase: float
) -> None:
    def focused(**request: Any) -> dict[str, Any]:
        evidence = _focused(**request)
        evidence["cases"] = {
            CASE: {
                "setup": 0.0,
                "call": (
                    1.25 + focused_increase
                    if request["source_tree"] == CANDIDATE_TREE
                    else 1.25
                ),
                "teardown": 0.0,
            }
        }
        return evidence

    result = _review(
        {CASE: 1.25},
        {CASE: 1.25 + full_increase},
        production_changed=True,
        focused=focused,
    )

    assert result.outcome == "correction_required"
    assert any(item.reason == "corroborated_regression" for item in result.findings)


@pytest.mark.parametrize(
    ("changed_tests", "affected_by_helper"),
    (((CASE,), ()), ((), (CASE,))),
)
def test_worsened_inherited_slow_case_repeats_both_sources_and_reports_phases(
    changed_tests: tuple[str, ...], affected_by_helper: tuple[str, ...]
) -> None:
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        evidence = _focused(**request)
        evidence["cases"] = {
            CASE: {
                "setup": 0.01,
                "call": 2.0 if request["source_tree"] == CANDIDATE_TREE else 1.2,
                "teardown": 0.01,
            }
        }
        return evidence

    result = _review(
        {CASE: 1.22},
        {CASE: 2.02},
        changed_tests=changed_tests,
        affected_by_helper=affected_by_helper,
        focused=focused,
    )

    assert result.outcome == "correction_required"
    assert len(requests) == 8
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("corroborated_regression", "touched_slow")
    }
    assert all(
        item.phase_deltas == pytest.approx({"setup": 0, "call": 0.8, "teardown": 0})
        for item in result.findings
        if item.reason == "corroborated_regression"
    )


@pytest.mark.parametrize(
    ("changed_tests", "affected_by_helper"),
    (((CASE,), ()), ((), (CASE,))),
)
def test_mixed_slow_inventory_focuses_only_affected_case(
    changed_tests: tuple[str, ...], affected_by_helper: tuple[str, ...]
) -> None:
    unrelated_case = "tests/test_other.py::test_unrelated"
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        evidence = _focused(**request)
        evidence["cases"] = {
            name: {
                "setup": 0.01,
                "call": 2.0 if request["source_tree"] == CANDIDATE_TREE else 1.2,
                "teardown": 0.01,
            }
            for name in request["case_names"]
        }
        return evidence

    baseline = {CASE: 1.22, unrelated_case: 1.22}
    candidate = {CASE: 2.02, unrelated_case: 2.02}
    result = _review(
        baseline,
        candidate,
        changed_tests=changed_tests,
        affected_by_helper=affected_by_helper,
        focused=focused,
    )

    assert len(requests) == 8
    assert all(request["case_names"] == (CASE,) for request in requests)
    assert result.outcome == "correction_required"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, reason)
        for env in ENVIRONMENTS
        for reason in ("corroborated_regression", "touched_slow")
    } | {(env, unrelated_case, "inherited_slow") for env in ENVIRONMENTS}


def test_production_change_keeps_threshold_impact_within_each_python_version() -> None:
    from agent_work.test_timing import review_test_timings

    baseline, candidate = _runs({CASE: 0.22}, {CASE: 1.22})
    candidate["py314"]["cases"] = {CASE: 0.5}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        evidence = _focused(**request)
        evidence["cases"] = {
            CASE: {
                "setup": 0.01,
                "call": 1.2 if request["source_tree"] == CANDIDATE_TREE else 0.2,
                "teardown": 0.01,
            }
        }
        return evidence

    result = review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=(),
        affected_by_helper=(),
        production_changed=True,
        run_focused=focused,
    )

    assert len(requests) == 4
    assert all(request["environment"] == "py311" for request in requests)
    assert result.outcome == "correction_required"
    assert {(item.environment, item.reason) for item in result.findings} == {
        ("py311", "corroborated_regression"),
        ("py311", "touched_slow"),
    }


def test_newly_crossing_one_second_repeats_each_exact_source_twice_per_version() -> (
    None
):
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        call_seconds = 1.2 if request["source_tree"] == CANDIDATE_TREE else 0.2
        evidence = _focused(**request)
        evidence["cases"] = {
            CASE: {"setup": 0.01, "call": call_seconds, "teardown": 0.01}
        }
        return evidence

    result = _review({CASE: 0.22}, {CASE: 1.22}, changed_tests=(CASE,), focused=focused)

    assert len(requests) == 8
    assert {
        (request["environment"], request["source_tree"]) for request in requests
    } == {(env, tree) for env in ENVIRONMENTS for tree in (BASE_TREE, CANDIDATE_TREE)}
    assert all(
        sum(
            request["environment"] == env and request["source_tree"] == tree
            for request in requests
        )
        == 2
        for env in ENVIRONMENTS
        for tree in (BASE_TREE, CANDIDATE_TREE)
    )
    assert all(
        request["seed"] == SEED
        and request["prepared_environment"] == f"prepared-{request['environment']}"
        and request["case_names"] == (CASE,)
        for request in requests
    )
    assert result.outcome == "correction_required"
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("corroborated_regression", "touched_slow")
    }
    assert all(
        item.phase_deltas == pytest.approx({"setup": 0.0, "call": 1.0, "teardown": 0.0})
        for item in result.findings
        if item.reason == "corroborated_regression"
    )


@pytest.mark.parametrize("impact", ("none", "changed", "affected"))
def test_small_focused_setup_rise_needs_causal_review(
    impact: str,
) -> None:
    def focused(**request: Any) -> dict[str, Any]:
        evidence = _focused(**request)
        evidence["cases"] = {
            CASE: {
                "setup": 0.046 if request["source_tree"] == CANDIDATE_TREE else 0.01,
                "call": 0.98,
                "teardown": 0.0,
            }
        }
        return evidence

    result = _review(
        {CASE: 0.99},
        {CASE: 1.026},
        changed_tests=(CASE,) if impact == "changed" else (),
        affected_by_helper=(CASE,) if impact == "affected" else (),
        focused=focused,
    )

    assert result.outcome == (
        "report_only" if impact == "none" else "correction_required"
    )
    if impact == "none":
        assert all(item.reason == "inherited_slow" for item in result.findings)
    else:
        assert all(
            item.phase_deltas
            == pytest.approx({"setup": 0.036, "call": 0.0, "teardown": 0.0})
            for item in result.findings
            if item.reason == "corroborated_regression"
        )


def test_uncorroborated_one_second_spike_is_report_only() -> None:
    attempts = {env: 0 for env in ENVIRONMENTS}

    def focused(**request: Any) -> dict[str, Any]:
        evidence = _focused(**request)
        if request["source_tree"] == CANDIDATE_TREE:
            attempts[request["environment"]] += 1
        evidence["cases"] = {
            CASE: {
                "setup": 0.01,
                "call": 1.2
                if request["source_tree"] == CANDIDATE_TREE
                and attempts[request["environment"]] == 1
                else 0.2,
                "teardown": 0.01,
            }
        }
        return evidence

    result = _review({CASE: 0.22}, {CASE: 1.22}, changed_tests=(CASE,), focused=focused)

    assert attempts == {env: 2 for env in ENVIRONMENTS}
    assert result.outcome == "report_only"
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("uncorroborated_spike", "touched_slow")
    }


def test_touched_inherited_integration_spike_with_faster_repeats_is_report_only() -> (
    None
):
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        evidence = _focused(**request)
        call_seconds = 1.18 if request["source_tree"] == BASE_TREE else 1.08
        evidence["cases"] = {
            CASE: {"setup": 0.01, "call": call_seconds, "teardown": 0.01}
        }
        return evidence

    result = _review({CASE: 1.20}, {CASE: 1.22}, changed_tests=(CASE,), focused=focused)

    assert len(requests) == 8
    assert all(
        sum(
            request["environment"] == env and request["source_tree"] == tree
            for request in requests
        )
        == 2
        for env in ENVIRONMENTS
        for tree in (BASE_TREE, CANDIDATE_TREE)
    )
    assert result.outcome == "report_only"
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("uncorroborated_spike", "touched_slow")
    }


def test_changed_shared_helper_requires_explicit_consumer_impact() -> None:
    with pytest.raises(ValueError):
        _review({CASE: 0.2}, {CASE: 0.2}, changed_tests=("tests/conftest.py",))


def test_helper_affected_existing_subsecond_case_uses_full_junit_pair() -> None:
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review(
        {CASE: 0.101},
        {CASE: 0.101},
        affected_by_helper=(CASE,),
        focused=focused,
    )

    assert result.outcome == "report_only"
    assert requests == []
    assert all(item.reason == "touched_slow" for item in result.findings)


def test_changed_test_file_marks_its_parameterized_cases_as_touched() -> None:
    result = _review(
        {NEW_CASE: 0.08}, {NEW_CASE: 0.11}, changed_tests=("tests/test_speed.py",)
    )

    assert result.outcome == "report_only"
    assert {(item.environment, item.name) for item in result.findings} == {
        (env, NEW_CASE) for env in ENVIRONMENTS
    }
    assert all(item.reason == "touched_slow" for item in result.findings)


def test_many_existing_subsecond_cases_in_changed_file_need_no_focused_runs() -> None:
    cases = {f"tests/test_speed.py::test_case_{index}": 0.101 for index in range(40)}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review(
        cases, cases, changed_tests=("tests/test_speed.py",), focused=focused
    )

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (environment, name, "touched_slow")
        for environment in ENVIRONMENTS
        for name in cases
    }
    assert requests == []


def test_many_touched_slow_cases_keep_bounded_findings_and_omission_count() -> None:
    cases = {f"tests/test_speed.py::test_case_{index}": 0.101 for index in range(86)}

    result = _review(
        cases,
        cases,
        changed_tests=("tests/test_speed.py",),
        focused=lambda **_request: pytest.fail("existing subsecond case was focused"),
    )

    assert result.outcome == "report_only"
    assert len(result.findings) == 128
    assert result.omitted_findings == 44
    assert all(item.reason == "touched_slow" for item in result.findings)


def test_critical_cases_across_versions_are_retained_before_subsecond_findings() -> (
    None
):
    subsecond_cases = {
        f"tests/test_speed.py::test_case_{index}": 0.101 for index in range(64)
    }
    critical_case = "tests/test_speed.py::test_critical"
    result = _review(
        subsecond_cases,
        subsecond_cases | {critical_case: 1.001},
        changed_tests=("tests/test_speed.py",),
    )

    assert result.outcome == "correction_required"
    assert len(result.findings) == 128
    assert result.omitted_findings == 2
    assert {(item.environment, item.name) for item in result.findings} >= {
        (environment, critical_case) for environment in ENVIRONMENTS
    }


def test_more_than_128_critical_findings_fails_closed() -> None:
    from agent_work.test_timing import FindingBudgetExhausted

    critical_cases = {
        f"tests/test_speed.py::test_critical_{index}": 1.001 for index in range(65)
    }

    with pytest.raises(FindingBudgetExhausted, match="finding budget exhausted"):
        _review({}, critical_cases)


def test_focused_budget_counts_invocations_across_both_versions() -> None:
    from agent_work.test_timing import FocusedTimingBudgetExhausted

    cases = {f"tests/test_speed.py::test_case_{index}": 0.11 for index in range(513)}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    with pytest.raises(FocusedTimingBudgetExhausted, match="budget exhausted"):
        _review({}, cases, focused=focused)

    assert len(requests) == 32
    assert all(1 <= len(request["case_names"]) <= 32 for request in requests)


def test_many_new_cases_are_focused_in_bounded_batches() -> None:
    cases = {f"tests/test_speed.py::test_case_{index}": 0.11 for index in range(86)}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review({}, cases, changed_tests=("tests/test_speed.py",), focused=focused)

    assert result.outcome == "correction_required"
    assert len(requests) == 6
    assert all(request["source_tree"] == CANDIDATE_TREE for request in requests)
    assert {name for request in requests for name in request["case_names"]} == set(
        cases
    )
    assert len(result.findings) == 128
    assert result.omitted_findings == 44


def test_many_one_second_regressions_keep_paired_focused_evidence() -> None:
    baseline = {f"tests/test_speed.py::test_case_{index}": 0.9 for index in range(32)}
    candidate = {name: 1.1 for name in baseline}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        result = _focused(**request)
        result["cases"] = {
            name: {
                "setup": 0.01,
                "call": 1.08 if request["source_tree"] == CANDIDATE_TREE else 0.88,
                "teardown": 0.01,
            }
            for name in request["case_names"]
        }
        return result

    result = _review(
        baseline,
        candidate,
        changed_tests=("tests/test_speed.py",),
        focused=focused,
    )

    assert len(requests) == 8
    assert result.outcome == "correction_required"
    assert len(result.findings) == 128
    assert result.omitted_findings == 0
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("corroborated_regression", "touched_slow")
    }
    for environment in ENVIRONMENTS:
        per_source = {
            source: [
                request
                for request in requests
                if request["environment"] == environment
                and request["source_tree"] == source
            ]
            for source in (BASE_TREE, CANDIDATE_TREE)
        }
        assert all(len(group) == 2 for group in per_source.values())
        assert all(
            sorted(name for request in group for name in request["case_names"])
            == sorted(list(baseline) * 2)
            for group in per_source.values()
        )


def test_bounded_findings_retain_touched_slow_report_after_many_reports() -> None:
    inherited = {
        f"tests/test_speed.py::test_inherited_{index}": 1.1 for index in range(63)
    }
    cases = inherited | {CASE: 0.101}

    result = _review(
        cases,
        cases,
        changed_tests=(CASE,),
        focused=lambda **_request: pytest.fail("no focused run expected"),
    )

    assert result.outcome == "report_only"
    assert len(result.findings) == 128
    assert result.omitted_findings == 0
    assert any(item.reason == "touched_slow" for item in result.findings)


def test_crossing_one_second_with_slow_baseline_repeat_is_spike() -> None:
    attempts = {env: 0 for env in ENVIRONMENTS}

    def focused(**request: Any) -> dict[str, Any]:
        evidence = _focused(**request)
        if request["source_tree"] == BASE_TREE:
            attempts[request["environment"]] += 1
        baseline_spike = (
            request["source_tree"] == BASE_TREE
            and attempts[request["environment"]] == 1
        )
        evidence["cases"] = {
            CASE: {
                "setup": 0.01,
                "call": (
                    1.5
                    if request["source_tree"] == CANDIDATE_TREE
                    else 1.2
                    if baseline_spike
                    else 0.2
                ),
                "teardown": 0.01,
            }
        }
        return evidence

    result = _review({CASE: 0.22}, {CASE: 1.22}, changed_tests=(CASE,), focused=focused)

    assert result.outcome == "report_only"
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("uncorroborated_spike", "touched_slow")
    }


@pytest.mark.parametrize(
    "damage",
    (
        "wrong_tree",
        "missing_version",
        "missing_digest",
        "incomplete",
        "wrong_count",
        "wrong_seed",
        "wrong_prepared_environment",
        "ambiguous_name",
    ),
)
def test_partial_or_incomparable_full_junit_evidence_fails_closed(
    damage: str,
) -> None:
    from agent_work.test_timing import review_test_timings

    baseline, candidate = _runs({CASE: 0.2}, {CASE: 0.2})
    if damage == "wrong_tree":
        candidate["py311"]["source_tree"] = BASE_TREE
    elif damage == "missing_version":
        del candidate["py314"]
    elif damage == "missing_digest":
        candidate["py311"]["junit_sha256"] = ""
    elif damage == "incomplete":
        candidate["py311"]["timing_complete"] = False
    elif damage == "wrong_count":
        candidate["py311"]["test_count"] = 2
    elif damage == "wrong_seed":
        candidate["py311"]["seed"] = "other"
    elif damage == "wrong_prepared_environment":
        candidate["py311"]["prepared_environment"] = "other"
    else:
        candidate["py311"]["cases"] = {"test_existing": 0.2}

    with pytest.raises(ValueError):
        review_test_timings(
            baseline_tree=BASE_TREE,
            candidate_tree=CANDIDATE_TREE,
            baseline_runs=baseline,
            candidate_runs=candidate,
            changed_tests=(),
            affected_by_helper=(),
            run_focused=_focused,
        )


def test_focused_repeat_with_wrong_source_binding_fails_closed() -> None:
    def wrong_source(**request: Any) -> dict[str, Any]:
        evidence = _focused(**request)
        evidence["source_tree"] = BASE_TREE
        return evidence

    with pytest.raises(ValueError):
        _review({CASE: 0.2}, {CASE: 1.2}, changed_tests=(CASE,), focused=wrong_source)


def test_full_junit_inventory_keeps_qualified_parameterized_case_times() -> None:
    from agent_work.test_timing import parse_full_junit_inventory

    artifact = (
        b"<testsuite tests='2'>"
        b"<testcase classname='tests/test_speed.py' name='test_existing' time='0.08'/>"
        b"<testcase classname='tests/test_speed.py' name='test_added[param]' time='0.12'/>"
        b"</testsuite>"
    )

    assert parse_full_junit_inventory(artifact) == {CASE: 0.08, NEW_CASE: 0.12}


def test_full_junit_inventory_normalizes_pytest_classname_to_node_path() -> None:
    from agent_work.test_timing import parse_full_junit_inventory

    artifact = b"<testsuite tests='1'><testcase classname='tests.test_speed' name='test_existing' time='0.08'/></testsuite>"

    assert parse_full_junit_inventory(artifact) == {CASE: 0.08}


def test_full_junit_inventory_keeps_safe_class_node_id() -> None:
    from agent_work.test_timing import parse_full_junit_inventory

    artifact = b"<testsuite tests='1'><testcase classname='tests.test_speed.TestWidget' name='test_existing' time='0.08'/></testsuite>"

    assert parse_full_junit_inventory(artifact) == {
        "tests/test_speed.py::TestWidget::test_existing": 0.08
    }


def test_full_junit_inventory_hashes_unsafe_parameter_but_retains_private_selector() -> (
    None
):
    from agent_work.test_timing import parse_full_junit_records

    artifact = b"<testsuite tests='2'><testcase classname='tests.test_speed' name='test_added[a b]' time='0.08'/><testcase classname='tests.test_speed' name='test_added[a/b]' time='0.09'/></testsuite>"

    cases, selectors = parse_full_junit_records(artifact)

    assert len(cases) == len(selectors) == 2
    assert all(
        name.startswith("tests/test_speed.py::test_added[param_") for name in cases
    )
    assert set(selectors.values()) == {
        "tests/test_speed.py::test_added[a b]",
        "tests/test_speed.py::test_added[a/b]",
    }


@pytest.mark.parametrize(
    "artifact",
    (
        b"<testsuite tests='2'><testcase classname='tests/test_speed.py' name='test_existing' time='0.1'/></testsuite>",
        b"<testsuite tests='1'><testcase name='test_existing' time='0.1'/></testsuite>",
        b"<testsuite tests='1'><testcase classname='tests/test_speed.py' name='test_existing' time='NaN'/></testsuite>",
        b"<!DOCTYPE testsuite [<!ENTITY x 'bad'>]><testsuite tests='1'><testcase classname='tests.test_speed' name='test_existing' time='0.1'/></testsuite>",
        b"<invalid tests='1'><testcase classname='tests.test_speed' name='test_existing' time='0.1'/></invalid>",
        b"<testsuite tests='1' failures='1'><testcase classname='tests.test_speed' name='test_existing' time='0.1'><failure/></testcase></testsuite>",
    ),
)
def test_full_junit_inventory_rejects_incomplete_or_ambiguous_xml(
    artifact: bytes,
) -> None:
    from agent_work.test_timing import parse_full_junit_inventory

    with pytest.raises(ValueError):
        parse_full_junit_inventory(artifact)
