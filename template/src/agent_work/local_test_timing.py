# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Compare bounded local JUnit and pytest phase evidence for exact Git trees."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .validation import is_safe_junit_test_name

_MAX_ARTIFACT_BYTES = 1_048_576
_MAX_CASES = 1_024
_MAX_FULL_CASES = 20_000
_TREE = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]{1,6})?\Z")
_PHASE_LINE = re.compile(
    r"(?P<seconds>[0-9]+(?:\.[0-9]{1,6})?)s "
    r"(?P<phase>setup|call|teardown) +(?P<name>.+)\Z"
)
_PHASES = ("setup", "call", "teardown")


def _canonical_test_id(raw: str) -> str | None:
    """Keep safe IDs, or hash an unsafe parameter as in JUnit validation."""
    if len(raw.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
        return None
    if is_safe_junit_test_name(raw):
        return raw if len(raw.encode("utf-8")) <= 256 else None
    base, separator, _parameter = raw.partition("[")
    if not separator or not raw.endswith("]") or not is_safe_junit_test_name(base):
        return None
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    safe = f"{base}[param_{digest}]"
    return safe if len(safe.encode("utf-8")) <= 256 else None


class _Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str
    sha256: str
    test_count: int | None = Field(default=None, ge=0, le=_MAX_FULL_CASES)


class _Run(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tree: str
    environment: str
    interpreter: str
    command: tuple[str, ...]
    seed: int
    junit: _Artifact
    phase_log: _Artifact


class _Environment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidate_full_junit: _Artifact
    baseline_runs: tuple[_Run, ...]
    candidate_runs: tuple[_Run, ...]


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_tree: str
    candidate_tree: str
    changed_test_ids: tuple[str, ...]
    environments: dict[str, _Environment]


@dataclass(frozen=True)
class ComparedCase:
    """One safe test ID and the observed repeated timing evidence."""

    environment: str
    name: str
    classification: Literal[
        "inherited", "introduced", "consistent_slowdown", "inconclusive"
    ]
    changed: bool
    baseline_seconds: tuple[float, ...]
    candidate_seconds: tuple[float, ...]
    signed_delta_seconds: tuple[float, ...]
    dominant_phase: str | None
    phase_seconds: Mapping[str, tuple[tuple[float, ...], tuple[float, ...]]]


@dataclass(frozen=True)
class TimingComparison:
    """Read-only comparison and complete candidate slow-case inventory."""

    status: Literal["clear", "review", "inconclusive"]
    reasons: tuple[str, ...]
    compared_cases: tuple[ComparedCase, ...]
    over_100ms_cases: Mapping[str, tuple[tuple[str, float], ...]]
    at_least_1s_cases: Mapping[str, tuple[tuple[str, float], ...]]


def _seconds(value: str) -> float:
    if _SECONDS.fullmatch(value) is None:
        raise ValueError("invalid duration")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds > 1_000_000:
        raise ValueError("invalid duration")
    return seconds


def _read_artifact(artifact: _Artifact, read_bytes: Callable[[Path], bytes]) -> bytes:
    path = Path(artifact.path)
    if (
        not path.is_absolute()
        or artifact.path.startswith("//")
        or str(path) != artifact.path
        or ".." in path.parts
        or _DIGEST.fullmatch(artifact.sha256) is None
    ):
        raise ValueError("invalid artifact identity")
    data = read_bytes(path)
    if not isinstance(data, bytes) or len(data) > _MAX_ARTIFACT_BYTES:
        raise ValueError("invalid artifact bytes")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("artifact digest mismatch")
    return data


def _junit_cases(
    data: bytes, count: int | None, *, max_cases: int = _MAX_CASES
) -> dict[str, float]:
    if count is None or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("invalid JUnit proof")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise ValueError("invalid JUnit XML") from error
    if root.tag not in {"testsuite", "testsuites"}:
        raise ValueError("invalid JUnit root")
    if any(
        suite.get(field, "0") != "0"
        for suite in root.iter("testsuite")
        for field in ("failures", "errors", "skipped")
    ) or any(
        case.find(tag) is not None
        for case in root.iter("testcase")
        for tag in ("failure", "error", "skipped")
    ):
        raise ValueError("JUnit contains unsuccessful cases")
    cases: dict[str, float] = {}
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        test_name = case.get("name", "")
        if not classname.startswith("tests."):
            raise ValueError("unsafe JUnit class")
        name = _canonical_test_id(f"{classname.replace('.', '/')}.py::{test_name}")
        if name is None or name in cases:
            raise ValueError("unsafe or duplicate JUnit test ID")
        cases[name] = _seconds(case.get("time", ""))
        if len(cases) > max_cases:
            raise ValueError("too many JUnit cases")
    if len(cases) != count or not cases:
        raise ValueError("JUnit count mismatch")
    return cases


def _phase_cases(data: bytes) -> dict[str, dict[str, float]]:
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("invalid phase log encoding") from error
    cases: dict[str, dict[str, float]] = {}
    for line in lines:
        match = _PHASE_LINE.fullmatch(line)
        if match is None:
            continue
        name = _canonical_test_id(match["name"])
        if name is None:
            raise ValueError("unsafe phase test ID")
        phases = cases.setdefault(name, {})
        phase = match["phase"]
        if phase in phases:
            raise ValueError("duplicate phase")
        phases[phase] = _seconds(match["seconds"])
        if len(cases) > _MAX_CASES:
            raise ValueError("too many phase cases")
    if (
        not any("slowest durations" in line for line in lines)
        or not cases
        or any(set(phases) != set(_PHASES) for phases in cases.values())
    ):
        raise ValueError("incomplete phase log")
    return cases


def _run_cases(
    run: _Run,
    *,
    tree: str,
    environment: str,
    read_bytes: Callable[[Path], bytes],
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    command = run.command
    if (
        run.tree != tree
        or run.environment != environment
        or not run.interpreter.endswith(f"/{environment}/bin/python")
        or len(command) < 13
        or command[:8]
        != (
            "uv",
            "run",
            "--no-project",
            "--python",
            run.interpreter,
            "python",
            "-m",
            "pytest",
        )
        or command[8] not in {"-q", "-vv"}
        or (command[8] == "-q" and "-vv" in command[9:])
        or "--no-cov" not in command
        or "--durations=0" not in command
        or sum(arg.startswith("--randomly-seed=") for arg in command) != 1
        or f"--randomly-seed={run.seed}" not in command
        or sum(arg.startswith("--junitxml=") for arg in command) != 1
        or f"--junitxml={run.junit.path}" not in command
    ):
        raise ValueError("focused run identity mismatch")
    cases = _junit_cases(_read_artifact(run.junit, read_bytes), run.junit.test_count)
    phases = _phase_cases(_read_artifact(run.phase_log, read_bytes))
    selected = [name for arg in command[9:] if (name := _canonical_test_id(arg))]
    allowed_options = {
        "--no-cov",
        "--durations=0",
        "-vv",
        f"--randomly-seed={run.seed}",
    }
    if (
        cases.keys() != phases.keys()
        or set(selected) != set(cases)
        or len(selected) != len(cases)
        or any(
            arg not in allowed_options
            and not arg.startswith("--junitxml=")
            and _canonical_test_id(arg) is None
            for arg in command[9:]
        )
    ):
        raise ValueError("focused run case mismatch")
    if any(
        abs(sum(phases[name].values()) - seconds) > 0.016
        for name, seconds in cases.items()
    ):
        raise ValueError("JUnit and phase durations disagree")
    return cases, phases


def _run_identity(run: _Run) -> tuple[str, int, tuple[str, ...]]:
    """Compare controls while allowing side-specific selected test IDs."""
    return (
        run.interpreter,
        run.seed,
        tuple(
            "--junitxml=<artifact>" if arg.startswith("--junitxml=") else arg
            for arg in run.command
            if _canonical_test_id(arg) is None
        ),
    )


def _compare_case(
    environment: str,
    name: str,
    baseline: tuple[tuple[dict[str, float], dict[str, dict[str, float]]], ...],
    candidate: tuple[tuple[dict[str, float], dict[str, dict[str, float]]], ...],
    changed: bool,
) -> ComparedCase:
    baseline_seconds = tuple(cases[name] for cases, _ in baseline if name in cases)
    candidate_seconds = tuple(cases[name] for cases, _ in candidate if name in cases)
    phase_seconds = {
        phase: (
            tuple(phases[name][phase] for _, phases in baseline if name in phases),
            tuple(phases[name][phase] for _, phases in candidate if name in phases),
        )
        for phase in _PHASES
    }
    deltas = tuple(
        round(after - before, 6)
        for before, after in zip(baseline_seconds, candidate_seconds, strict=False)
    )
    dominant_phase: str | None = None
    if baseline_seconds and candidate_seconds:
        phase_deltas = {
            phase: sum(after) / len(after) - sum(before) / len(before)
            for phase, (before, after) in phase_seconds.items()
        }
        dominant_phase = max(_PHASES, key=lambda phase: abs(phase_deltas[phase]))
    if len(candidate_seconds) < 2 or (baseline_seconds and len(baseline_seconds) < 2):
        classification = "inconclusive"
    elif not baseline_seconds:
        classification = "introduced"
    elif all(delta > 0 for delta in deltas):
        classification = "consistent_slowdown"
    elif any(delta > 0 for delta in deltas) and any(delta < 0 for delta in deltas):
        classification = "inconclusive"
    else:
        classification = "inherited"
    return ComparedCase(
        environment=environment,
        name=name,
        classification=classification,
        changed=changed,
        baseline_seconds=baseline_seconds,
        candidate_seconds=candidate_seconds,
        signed_delta_seconds=deltas,
        dominant_phase=dominant_phase,
        phase_seconds=phase_seconds,
    )


def compare_local_test_timings(
    manifest: Mapping[str, object],
    *,
    expected_baseline_tree: str,
    expected_candidate_tree: str,
    read_bytes: Callable[[Path], bytes],
) -> TimingComparison:
    """Verify artifacts and compare repeated cases without inferring causation."""
    try:
        proof = _Manifest.model_validate(manifest)
        if (
            _TREE.fullmatch(expected_baseline_tree) is None
            or _TREE.fullmatch(expected_candidate_tree) is None
            or proof.baseline_tree != expected_baseline_tree
            or proof.candidate_tree != expected_candidate_tree
            or set(proof.environments) != {"py311", "py314"}
            or any(_canonical_test_id(name) is None for name in proof.changed_test_ids)
        ):
            raise ValueError("manifest identity mismatch")
        changed_ids = {_canonical_test_id(name) for name in proof.changed_test_ids}
        compared: list[ComparedCase] = []
        over_100ms: dict[str, tuple[tuple[str, float], ...]] = {}
        at_least_1s: dict[str, tuple[tuple[str, float], ...]] = {}
        artifact_paths: set[str] = set()
        for environment, evidence in sorted(proof.environments.items()):
            if environment not in {"py311", "py314"}:
                raise ValueError("unsupported environment")
            for artifact in (
                evidence.candidate_full_junit,
                *(
                    item
                    for run in (*evidence.baseline_runs, *evidence.candidate_runs)
                    for item in (run.junit, run.phase_log)
                ),
            ):
                if artifact.path in artifact_paths:
                    raise ValueError("reused timing artifact path")
                artifact_paths.add(artifact.path)
            full = _junit_cases(
                _read_artifact(evidence.candidate_full_junit, read_bytes),
                evidence.candidate_full_junit.test_count,
                max_cases=_MAX_FULL_CASES,
            )
            over_100ms[environment] = tuple(
                (name, seconds) for name, seconds in full.items() if seconds > 0.100
            )
            at_least_1s[environment] = tuple(
                (name, seconds) for name, seconds in full.items() if seconds >= 1.000
            )
            if len(evidence.baseline_runs) < 2 or len(evidence.candidate_runs) < 2:
                raise ValueError("at least two runs per tree are required")
            baseline = tuple(
                _run_cases(
                    run,
                    tree=proof.baseline_tree,
                    environment=environment,
                    read_bytes=read_bytes,
                )
                for run in evidence.baseline_runs
            )
            candidate = tuple(
                _run_cases(
                    run,
                    tree=proof.candidate_tree,
                    environment=environment,
                    read_bytes=read_bytes,
                )
                for run in evidence.candidate_runs
            )
            if (
                len(
                    {
                        _run_identity(run)
                        for run in (*evidence.baseline_runs, *evidence.candidate_runs)
                    }
                )
                != 1
            ):
                raise ValueError("focused run commands or seeds differ")
            if any(set(cases) != set(baseline[0][0]) for cases, _ in baseline) or any(
                set(cases) != set(candidate[0][0]) for cases, _ in candidate
            ):
                raise ValueError("focused repeat case sets differ")
            if any(
                name not in full or any(name not in cases for cases, _ in candidate)
                for name in changed_ids
            ):
                raise ValueError("changed case lacks candidate timing evidence")
            names = set().union(*(set(cases) for cases, _ in (*baseline, *candidate)))
            for name in sorted(names):
                compared.append(
                    _compare_case(
                        environment, name, baseline, candidate, name in changed_ids
                    )
                )
        if any(case.classification == "inconclusive" for case in compared):
            status = "inconclusive"
        elif any(
            (
                case.classification == "introduced"
                and all(seconds > 0.100 for seconds in case.candidate_seconds)
            )
            or case.classification == "consistent_slowdown"
            for case in compared
        ):
            status = "review"
        else:
            status = "clear"
        return TimingComparison(status, (), tuple(compared), over_100ms, at_least_1s)
    except (ValidationError, ValueError, KeyError, OSError, TypeError) as error:
        return TimingComparison("inconclusive", (str(error)[:200],), (), {}, {})
