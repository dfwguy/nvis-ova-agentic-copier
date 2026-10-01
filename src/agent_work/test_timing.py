# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure, source-bound decisions from complete JUnit timing inventories."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from xml.etree import ElementTree

_ENVIRONMENTS = ("py311", "py314")
_TREE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PHASES = ("setup", "call", "teardown")
MAX_JUNIT_BYTES: int = 4_194_304
_MAX_CASES = 8_192
_MAX_FINDINGS = 128
_MAX_FOCUSED_RUNS = 32
_MAX_FOCUSED_CASES = 32
_SAFE_CASE_ID = re.compile(
    r"^tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py"
    r"(?:::Test[A-Za-z0-9_]+)*::test_[A-Za-z0-9_]+"
    r"(?:\[[A-Za-z0-9_.-]+\])?$"
)


def is_safe_timing_case_id(name: str) -> bool:
    """Accept qualified pytest case IDs with bounded safe class/parameter parts."""
    return (
        len(name.encode("utf-8")) <= 256 and _SAFE_CASE_ID.fullmatch(name) is not None
    )


def _safe_case_id(raw_name: str) -> str:
    if is_safe_timing_case_id(raw_name):
        return raw_name
    base, marker, parameter = raw_name.partition("[")
    if not marker or not parameter.endswith("]") or not is_safe_timing_case_id(base):
        raise ValueError("full JUnit case name is invalid")
    digest = hashlib.sha256(raw_name.encode()).hexdigest()
    safe = f"{base}[param_{digest}]"
    if not is_safe_timing_case_id(safe):
        raise ValueError("full JUnit case name is oversized")
    return safe


def _qualified_name(classname: str, test_name: str) -> str:
    if classname.startswith("tests/") and classname.endswith(".py"):
        return f"{classname}::{test_name}"
    parts = classname.split(".")
    if len(parts) < 2 or parts[0] != "tests":
        return f"{classname}::{test_name}"
    test_module = next(
        (index for index, part in enumerate(parts) if part.startswith("test_")),
        None,
    )
    if test_module is None:
        return f"{classname}::{test_name}"
    path = f"{'/'.join(parts[: test_module + 1])}.py"
    suffix = "::".join(parts[test_module + 1 :])
    return f"{path}::{suffix}::{test_name}" if suffix else f"{path}::{test_name}"


def safe_timing_case_id(classname: str, test_name: str) -> str:
    """Use one safe source case ID across private inventory and serial summaries."""
    return _safe_case_id(_qualified_name(classname, test_name))


def parse_full_junit_records(
    artifact: bytes,
) -> tuple[dict[str, float], dict[str, str]]:
    """Read safe case IDs and their private exact pytest selectors."""
    if not artifact or len(artifact) > MAX_JUNIT_BYTES:
        raise ValueError("full JUnit artifact is missing or oversized")
    if b"<!DOCTYPE" in artifact.upper() or b"<!ENTITY" in artifact.upper():
        raise ValueError("full JUnit artifact contains XML declarations")
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError as error:
        raise ValueError("full JUnit XML is malformed") from error
    if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
        raise ValueError("full JUnit root is invalid")
    suites = [
        element
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "testsuite"
    ]
    if not suites or any(
        element.get(field, "0") not in {"0", ""}
        for element in suites
        for field in ("failures", "errors")
    ):
        raise ValueError("full JUnit suite is not successful")
    cases: dict[str, float] = {}
    selectors: dict[str, str] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        if any(
            child.tag.rsplit("}", 1)[-1] in {"failure", "error"} for child in element
        ):
            raise ValueError("full JUnit case is not successful")
        classname = element.get("classname", "")
        test_name = element.get("name", "")
        raw_name = _qualified_name(classname, test_name)
        name = safe_timing_case_id(classname, test_name)
        raw_seconds = element.get("time", "")
        if (
            name in cases
            or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", raw_seconds) is None
            or len(cases) >= _MAX_CASES
        ):
            raise ValueError("full JUnit case is ambiguous or invalid")
        seconds = float(raw_seconds)
        if not math.isfinite(seconds) or seconds > 1_000_000:
            raise ValueError("full JUnit case duration is invalid")
        cases[name] = seconds
        selectors[name] = raw_name
    try:
        declared = sum(int(element.get("tests", "")) for element in suites)
    except ValueError as error:
        raise ValueError("full JUnit count is invalid") from error
    if not cases or declared != len(cases):
        raise ValueError("full JUnit inventory is incomplete")
    return cases, selectors


def parse_full_junit_inventory(artifact: bytes) -> dict[str, float]:
    """Read every safe qualified case and duration from full JUnit XML."""
    cases, _selectors = parse_full_junit_records(artifact)
    return cases


@dataclass(frozen=True)
class TimingFinding:
    """One source-bound test timing classification."""

    environment: str
    name: str
    reason: str
    phase_deltas: Mapping[str, float] | None = None


@dataclass(frozen=True)
class TimingReview:
    """Complete supported-version timing decision."""

    outcome: Literal["clear", "report_only", "correction_required"]
    findings: tuple[TimingFinding, ...]
    omitted_findings: int = 0


class FocusedTimingBudgetExhausted(ValueError):
    """The exact-source focused comparison would exceed its run budget."""


class FindingBudgetExhausted(ValueError):
    """The timing review cannot retain every critical finding."""


def _run(
    value: Mapping[str, Any], *, tree: str, environment: str
) -> tuple[dict[str, float], str, str, tuple[str, ...]]:
    if (
        value.get("source_tree") != tree
        or value.get("environment") != environment
        or not isinstance(value.get("prepared_environment"), str)
        or not value["prepared_environment"]
        or not isinstance(value.get("seed"), str)
        or not value["seed"]
        or not isinstance(value.get("argv"), (tuple, list))
        or not value["argv"]
        or not all(isinstance(part, str) and part for part in value["argv"])
        or not isinstance(value.get("junit_sha256"), str)
        or _DIGEST.fullmatch(value["junit_sha256"]) is None
        or value.get("timing_complete") is not True
    ):
        raise ValueError("incomplete or unbound full JUnit evidence")
    cases = value.get("cases")
    if not isinstance(cases, Mapping) or not isinstance(value.get("test_count"), int):
        raise TypeError("invalid full JUnit inventory")
    if len(cases) != value["test_count"] or len(cases) > 100_000:
        raise ValueError("incomplete full JUnit inventory")
    parsed: dict[str, float] = {}
    for name, seconds in cases.items():
        if (
            not isinstance(name, str)
            or not is_safe_timing_case_id(name)
            or "::" not in name
            or not isinstance(seconds, (float, int))
            or isinstance(seconds, bool)
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise ValueError("invalid full JUnit case")
        parsed[name] = float(seconds)
    return (
        parsed,
        value["seed"],
        value["prepared_environment"],
        tuple(value["argv"]),
    )


def _focused(
    run_focused: Callable[..., Mapping[str, Any]],
    *,
    source_tree: str,
    environment: str,
    prepared_environment: str,
    argv: tuple[str, ...],
    seed: str,
    case_names: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    request = {
        "source_tree": source_tree,
        "environment": environment,
        "prepared_environment": prepared_environment,
        "argv": argv,
        "seed": seed,
        "case_names": case_names,
    }
    result = run_focused(**request)
    if any(
        result.get(key) != value
        for key, value in request.items()
        if key != "case_names"
    ):
        raise ValueError("focused JUnit evidence has different source or environment")
    cases = result.get("cases")
    if not isinstance(cases, Mapping) or set(cases) != set(case_names):
        raise ValueError("focused JUnit inventory is incomplete")
    parsed: dict[str, dict[str, float]] = {}
    for name, phases in cases.items():
        if not isinstance(phases, Mapping) or set(phases) != set(_PHASES):
            raise ValueError("focused JUnit phases are incomplete")
        parsed[name] = {}
        for phase in _PHASES:
            seconds = phases[phase]
            if (
                not isinstance(seconds, (float, int))
                or isinstance(seconds, bool)
                or not math.isfinite(seconds)
                or seconds < 0
            ):
                raise ValueError("invalid focused JUnit phase")
            parsed[name][phase] = float(seconds)
    return parsed


def _changed(name: str, changed_tests: tuple[str, ...]) -> bool:
    return any(
        name == item or name.startswith((f"{item}[", f"{item}::"))
        for item in changed_tests
    )


def review_test_timings(
    *,
    baseline_tree: str,
    candidate_tree: str,
    baseline_runs: Mapping[str, Mapping[str, Any]],
    candidate_runs: Mapping[str, Mapping[str, Any]],
    changed_tests: tuple[str, ...],
    affected_by_helper: tuple[str, ...],
    run_focused: Callable[..., Mapping[str, Any]],
) -> TimingReview:
    """Compare complete version inventories and bounded exact-source repeats."""
    if (
        _TREE.fullmatch(baseline_tree) is None
        or _TREE.fullmatch(candidate_tree) is None
        or baseline_tree == candidate_tree
        or set(baseline_runs) != set(_ENVIRONMENTS)
        or set(candidate_runs) != set(_ENVIRONMENTS)
        or any(not is_safe_timing_case_id(name) for name in affected_by_helper)
        or any(
            not (
                is_safe_timing_case_id(name)
                or name.startswith("tests/")
                and name.endswith(".py")
            )
            for name in changed_tests
        )
        or any(name.endswith("/conftest.py") for name in changed_tests)
        and not affected_by_helper
    ):
        raise ValueError("timing review identity or impact is incomplete")
    critical_findings: list[TimingFinding] = []
    subsecond_findings: list[TimingFinding] = []
    focused_runs = 0

    def add_finding(finding: TimingFinding, seconds: float) -> None:
        directly_changed = _changed(finding.name, changed_tests)
        if seconds > 1 and (
            finding.reason == "new_slow"
            or directly_changed
            and finding.reason in {"corroborated_regression", "touched_slow"}
        ):
            critical_findings.append(finding)
        else:
            subsecond_findings.append(finding)

    def run_bounded_focused(**request: Any) -> dict[str, dict[str, float]]:
        nonlocal focused_runs
        if focused_runs >= _MAX_FOCUSED_RUNS:
            raise FocusedTimingBudgetExhausted("focused timing budget exhausted")
        focused_runs += 1
        return _focused(run_focused, **request)

    def focused_batches(
        names: list[str],
        *,
        source_tree: str,
        environment: str,
        prepared: str,
        argv: tuple[str, ...],
        seed: str,
    ) -> dict[str, dict[str, float]]:
        evidence: dict[str, dict[str, float]] = {}
        for offset in range(0, len(names), _MAX_FOCUSED_CASES):
            evidence.update(
                run_bounded_focused(
                    source_tree=source_tree,
                    environment=environment,
                    prepared_environment=prepared,
                    argv=argv,
                    seed=seed,
                    case_names=tuple(names[offset : offset + _MAX_FOCUSED_CASES]),
                )
            )
        return evidence

    for environment in _ENVIRONMENTS:
        baseline, base_seed, base_prepared, base_argv = _run(
            baseline_runs[environment], tree=baseline_tree, environment=environment
        )
        candidate, seed, prepared, argv = _run(
            candidate_runs[environment], tree=candidate_tree, environment=environment
        )
        if (base_seed, base_prepared, base_argv) != (seed, prepared, argv):
            raise ValueError("full JUnit environments are incomparable")
        new_names = [
            name
            for name, seconds in candidate.items()
            if name not in baseline and seconds > 1
        ]
        regression_names = [
            name
            for name, seconds in candidate.items()
            if name in baseline
            and seconds > 1
            and (baseline[name] <= 1 or seconds > baseline[name])
            and _changed(name, changed_tests)
        ]
        new_focus_capacity = (_MAX_FOCUSED_RUNS - focused_runs) * _MAX_FOCUSED_CASES
        focused_batches(
            new_names[:new_focus_capacity],
            source_tree=candidate_tree,
            environment=environment,
            prepared=prepared,
            argv=argv,
            seed=seed,
        )
        regression_focus_capacity = (
            (_MAX_FOCUSED_RUNS - focused_runs) // 4 * _MAX_FOCUSED_CASES
        )
        regression_names = regression_names[:regression_focus_capacity]
        focused_names = set(regression_names)
        baseline_repeats = [
            focused_batches(
                regression_names,
                source_tree=baseline_tree,
                environment=environment,
                prepared=prepared,
                argv=argv,
                seed=seed,
            )
            for _ in range(2)
        ]
        candidate_repeats = [
            focused_batches(
                regression_names,
                source_tree=candidate_tree,
                environment=environment,
                prepared=prepared,
                argv=argv,
                seed=seed,
            )
            for _ in range(2)
        ]
        for name, seconds in candidate.items():
            original = baseline.get(name)
            directly_affected = (
                _changed(name, changed_tests) or name in affected_by_helper
            )
            touched = directly_affected
            if original is None and seconds > 0.100:
                reason = "new_slow" if seconds > 1 else "new_advisory"
                add_finding(TimingFinding(environment, name, reason), seconds)
            elif (
                original is not None
                and seconds > 1
                and (original <= 1 or seconds > original)
                and touched
                and name in focused_names
            ):
                paired_totals = tuple(
                    (
                        sum(baseline_repeat[name].values()),
                        sum(candidate_repeat[name].values()),
                    )
                    for baseline_repeat, candidate_repeat in zip(
                        baseline_repeats, candidate_repeats, strict=True
                    )
                )
                corroborated = all(
                    (original > 1 or prior <= 1)
                    and current > 1
                    and current > prior
                    and (directly_affected or original <= 1 or current - prior > 0.100)
                    for prior, current in paired_totals
                )
                deltas = {
                    phase: round(
                        sum(item[name][phase] for item in candidate_repeats) / 2
                        - sum(item[name][phase] for item in baseline_repeats) / 2,
                        6,
                    )
                    for phase in _PHASES
                }
                focused_increase = (
                    sum(current - prior for prior, current in paired_totals) / 2
                )
                inherited_variance = (
                    corroborated
                    and not directly_affected
                    and original > 1
                    and seconds - original < 1
                    and focused_increase < 1
                )
                add_finding(
                    TimingFinding(
                        environment,
                        name,
                        (
                            "inherited_variance"
                            if inherited_variance
                            else "corroborated_regression"
                            if corroborated
                            else "uncorroborated_spike"
                        ),
                        deltas,
                    ),
                    seconds,
                )
                add_finding(TimingFinding(environment, name, "touched_slow"), seconds)
            elif touched and seconds > 0.100:
                add_finding(TimingFinding(environment, name, "touched_slow"), seconds)
            elif original is not None and seconds > 1:
                add_finding(TimingFinding(environment, name, "inherited_slow"), seconds)
    if len(critical_findings) > _MAX_FINDINGS:
        raise FindingBudgetExhausted("finding budget exhausted")
    findings = (
        critical_findings + subsecond_findings[: _MAX_FINDINGS - len(critical_findings)]
    )
    omitted_findings = len(subsecond_findings) - (
        len(findings) - len(critical_findings)
    )
    if critical_findings:
        outcome = "correction_required"
    elif findings:
        outcome = "report_only"
    else:
        outcome = "clear"
    return TimingReview(outcome, tuple(findings), omitted_findings)
