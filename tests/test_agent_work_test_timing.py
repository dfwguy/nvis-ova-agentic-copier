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
from hypothesis import given, settings
from hypothesis import strategies as st

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
        run_focused=focused,
    )


def test_review_rejects_retired_production_changed_keyword() -> None:
    from agent_work.test_timing import review_test_timings

    baseline, candidate = _runs({CASE: 0.8}, {CASE: 1.3})
    retired_keyword: dict[str, Any] = {"production_changed": True}
    with pytest.raises(
        TypeError, match="unexpected keyword argument 'production_changed'"
    ):
        review_test_timings(
            baseline_tree=BASE_TREE,
            candidate_tree=CANDIDATE_TREE,
            baseline_runs=baseline,
            candidate_runs=candidate,
            changed_tests=(),
            affected_by_helper=(),
            run_focused=_focused,
            **retired_keyword,
        )


def test_touched_case_just_over_100ms_is_reported_in_both_versions() -> None:
    result = _review({CASE: 0.099}, {CASE: 0.101}, changed_tests=(CASE,))

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, "touched_slow") for env in ENVIRONMENTS
    }


@pytest.mark.parametrize(
    ("seconds", "expected_outcome", "focused_runs"),
    (
        (0.101, "report_only", 0),
        (1.0, "report_only", 0),
        (1.001, "correction_required", 2),
    ),
)
def test_new_parameter_case_blocks_only_above_one_second(
    seconds: float, expected_outcome: str, focused_runs: int
) -> None:
    """A pending candidate's 100 ms suggestion never blocks its Judge result."""
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review(
        {CASE: 0.08},
        {CASE: 0.08, NEW_CASE: seconds},
        changed_tests=("tests/test_speed.py::test_added",),
        focused=focused,
    )

    assert result.outcome == expected_outcome
    assert {(item.environment, item.name) for item in result.findings} == {
        (env, NEW_CASE) for env in ENVIRONMENTS
    }
    assert len(requests) == focused_runs
    assert all(
        request["source_tree"] == CANDIDATE_TREE
        and request["case_names"] == (NEW_CASE,)
        for request in requests
    )


def test_inherited_one_second_case_is_reported_without_correction() -> None:
    result = _review({CASE: 1.05}, {CASE: 1.05})

    assert result.outcome == "report_only"
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, "inherited_slow") for env in ENVIRONMENTS
    }


def test_production_linked_unchanged_case_over_one_second_remains_advisory() -> None:
    """Only a changed test case crosses the one-second correction gate."""
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        seconds = 1.3 if request["source_tree"] == CANDIDATE_TREE else 0.8
        evidence = _focused(**request)
        evidence["cases"] = {CASE: {"setup": 0.0, "call": seconds, "teardown": 0.0}}
        return evidence

    inherited = _review({CASE: 0.8}, {CASE: 1.3}, focused=focused)
    assert requests == []
    assert {(item.environment, item.reason) for item in inherited.findings} == {
        (env, "inherited_slow") for env in ENVIRONMENTS
    }
    changed = _review({CASE: 0.8}, {CASE: 1.3}, changed_tests=(CASE,), focused=focused)

    assert inherited.outcome == "report_only"
    assert changed.outcome == "correction_required"


def test_many_inherited_slow_cases_do_not_exhaust_critical_budget() -> None:
    cases = {f"tests/test_speed.py::test_inherited_{index}": 1.2 for index in range(70)}

    result = _review(
        cases,
        cases,
        focused=lambda **_request: pytest.fail("inherited case was focused"),
    )

    assert result.outcome == "report_only"
    assert len(result.findings) == 128
    assert result.omitted_findings == 12
    assert all(item.reason == "inherited_slow" for item in result.findings)


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
        == "inherited_slow"
    )
    assert (
        next(item.reason for item in result.findings if item.environment == "py314")
        == "inherited_slow"
    )
    assert all(item.phase_deltas is None for item in result.findings)
    assert requests == []

    affected = review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=(),
        affected_by_helper=(case,),
        run_focused=focused,
    )

    assert affected.outcome == "report_only"
    assert requests == []
    assert any(
        item.environment == "py311"
        and item.name == case
        and item.reason == "touched_slow"
        for item in affected.findings
    )


def test_production_only_slow_inventory_does_not_use_focused_budget() -> None:
    """Existing test costs stay advisory even across a large production change."""
    cases = {f"tests/test_speed.py::test_existing_{index}": 0.8 for index in range(520)}
    candidate_cases = dict.fromkeys(cases, 1.2)
    baseline, candidate = _runs(cases, candidate_cases)
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    from agent_work.test_timing import review_test_timings

    result = review_test_timings(
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        baseline_runs=baseline,
        candidate_runs=candidate,
        changed_tests=(),
        affected_by_helper=(),
        run_focused=focused,
    )

    assert result.outcome == "report_only"
    assert len(result.findings) <= 128
    assert result.omitted_findings > 0
    assert requests == []


@pytest.mark.parametrize(
    ("full_increase", "focused_increase"),
    ((1.0, 1.0), (1.0, 0.75), (0.75, 1.0)),
)
def test_inherited_variance_reaching_one_second_remains_advisory(
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
        focused=focused,
    )

    assert result.outcome == "report_only"
    assert all(item.reason == "inherited_slow" for item in result.findings)


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

    assert result.outcome == ("correction_required" if changed_tests else "report_only")
    assert len(requests) == (8 if changed_tests else 0)
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in (
            ("corroborated_regression", "touched_slow")
            if changed_tests
            else ("touched_slow",)
        )
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

    assert len(requests) == (8 if changed_tests else 0)
    assert all(request["case_names"] == (CASE,) for request in requests)
    assert result.outcome == ("correction_required" if changed_tests else "report_only")
    assert {(item.environment, item.name, item.reason) for item in result.findings} == {
        (env, CASE, reason)
        for env in ENVIRONMENTS
        for reason in (
            ("corroborated_regression", "touched_slow")
            if changed_tests
            else ("touched_slow",)
        )
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
        run_focused=focused,
    )

    assert requests == []
    assert result.outcome == "report_only"
    assert {(item.environment, item.reason) for item in result.findings} == {
        ("py311", "inherited_slow"),
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
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
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
        "correction_required" if impact == "changed" else "report_only"
    )
    if impact == "affected":
        assert requests == []
        assert all(item.reason == "touched_slow" for item in result.findings)
    if impact == "none":
        assert all(item.reason == "inherited_slow" for item in result.findings)
    elif impact == "changed":
        assert len(requests) == 8
        assert all(
            item.phase_deltas
            == pytest.approx({"setup": 0.036, "call": 0.0, "teardown": 0.0})
            for item in result.findings
            if item.reason == "corroborated_regression"
        )


def test_directly_changed_one_second_spike_requires_correction() -> None:
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
    assert result.outcome == "correction_required"
    assert {(item.environment, item.reason) for item in result.findings} == {
        (env, reason)
        for env in ENVIRONMENTS
        for reason in ("uncorroborated_spike", "touched_slow")
    }


def test_touched_inherited_integration_spike_over_one_second_requires_correction() -> (
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
    assert result.outcome == "correction_required"
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


def test_many_subsecond_new_cases_do_not_spend_focused_budget() -> None:
    cases = {f"tests/test_speed.py::test_case_{index}": 0.11 for index in range(513)}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review({}, cases, focused=focused)

    assert result.outcome == "report_only"
    assert requests == []
    assert len(result.findings) == 128
    assert result.omitted_findings == 898


def test_many_new_subsecond_cases_remain_bounded_advisories() -> None:
    cases = {f"tests/test_speed.py::test_case_{index}": 0.11 for index in range(86)}
    requests: list[dict[str, Any]] = []

    def focused(**request: Any) -> dict[str, Any]:
        requests.append(request)
        return _focused(**request)

    result = _review({}, cases, changed_tests=("tests/test_speed.py",), focused=focused)

    assert result.outcome == "report_only"
    assert requests == []
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


def test_crossing_one_second_with_slow_baseline_repeat_still_blocks() -> None:
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

    assert result.outcome == "correction_required"
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


@pytest.mark.parametrize(
    "size", [1_048_577, 4_194_304, 4_194_305, 6_291_456, 33_554_432, 33_554_433]
)
def test_full_junit_public_parsers_apply_inclusive_xml_cap(size: int) -> None:
    from agent_work.test_timing import (
        parse_full_junit_inventory,
        parse_full_junit_records,
    )

    artifact = b"<testsuite tests='1'><testcase classname='tests.test_speed' name='test_existing' time='0.08'/></testsuite>"
    artifact += b" " * (size - len(artifact))
    if size > 33_554_432:
        for parser in (parse_full_junit_inventory, parse_full_junit_records):
            with pytest.raises(ValueError):
                parser(artifact)
    else:
        assert parse_full_junit_inventory(artifact) == {CASE: 0.08}
        assert parse_full_junit_records(artifact) == ({CASE: 0.08}, {CASE: CASE})


def test_full_junit_cap_has_one_public_xml_owner() -> None:
    from agent_work.test_timing import MAX_JUNIT_BYTES

    assert MAX_JUNIT_BYTES == 33_554_432


@settings(max_examples=20)
@given(
    milliseconds=st.lists(
        st.integers(min_value=0, max_value=999), min_size=1, max_size=10
    ),
    padding=st.integers(min_value=0, max_value=80),
    reverse=st.booleans(),
)
def test_generated_full_inventory_preserves_identity_times_order_and_padding(
    milliseconds: list[int], padding: int, *, reverse: bool
) -> None:
    from agent_work.test_timing import parse_full_junit_records

    cases = [
        f"<testcase classname='tests.test_speed' name='test_case_{index}' time='{value / 1000:.3f}'/>".encode()
        for index, value in enumerate(milliseconds)
    ]
    expected = {
        f"tests/test_speed.py::test_case_{index}": value / 1000
        for index, value in enumerate(milliseconds)
    }
    prefix = f"<testsuite tests='{len(cases)}'>".encode()
    artifact = (
        prefix
        + b"".join(reversed(cases) if reverse else cases)
        + b"</testsuite>"
        + b" " * padding
    )
    assert parse_full_junit_records(artifact) == (
        expected,
        dict(zip(expected, expected, strict=True)),
    )
    for invalid in (
        prefix + b"".join(cases) + cases[0] + b"</testsuite>",
        f"<testsuite tests='{len(cases) + 1}'>".encode()
        + b"".join(cases)
        + b"</testsuite>",
        artifact.replace(b"time='", b"time='-", 1).replace(
            b"time='-0.000'", b"time='NaN'", 1
        ),
    ):
        with pytest.raises(ValueError):
            parse_full_junit_records(invalid)


@pytest.mark.parametrize("count", [8_192, 8_193])
def test_full_junit_inventory_keeps_existing_case_count_boundary(count: int) -> None:
    from agent_work.test_timing import parse_full_junit_inventory

    artifact = (
        f"<testsuite tests='{count}'>".encode()
        + b"".join(
            f"<testcase classname='tests.test_speed' name='test_case_{index}' time='0.01'/>".encode()
            for index in range(count)
        )
        + b"</testsuite>"
    )
    if count > 8_192:
        with pytest.raises(ValueError):
            parse_full_junit_inventory(artifact)
    else:
        assert parse_full_junit_inventory(artifact) == {
            f"tests/test_speed.py::test_case_{index}": 0.01 for index in range(count)
        }
