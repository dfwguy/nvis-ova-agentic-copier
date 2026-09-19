# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for durable Judge validation sessions."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agent_work.ledger import state_home
from agent_work.repository import CommandResult
from agent_work.validation import (
    JudgeValidationDependencies,
    JudgeValidationError,
    observe_judge_validation,
    run_judge_validation,
)

_CANDIDATE = "a" * 40
_OTHER_CANDIDATE = "b" * 40


@pytest.mark.parametrize("environment", ({}, {"XDG_STATE_HOME": ""}))
def test_empty_or_unset_xdg_state_home_uses_the_safe_default(
    environment: dict[str, str],
) -> None:
    assert state_home(environment=environment) == Path.home() / ".local" / "state"


@dataclass
class ValidationStateFake:
    """Injected bounded durable-state and candidate-lock boundary."""

    records: dict[str, dict[str, object]] = field(default_factory=dict)
    writes: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)

    def read(self, candidate_tree: str) -> dict[str, object] | None:
        return self.records.get(candidate_tree)

    def atomic_write(self, candidate_tree: str, record: dict[str, object]) -> None:
        self.records[candidate_tree] = record
        self.writes.append((candidate_tree, record))

    @contextmanager
    def candidate_lock(self, candidate_tree: str) -> Iterator[None]:
        self.locks.append(candidate_tree)
        yield


def _dependencies(
    state: ValidationStateFake,
    *,
    current_tree: str = _CANDIDATE,
    returncode: int = 0,
    monotonic_clock: Callable[[], float] = lambda: 0.0,
    sleeper: Callable[[float], None] = lambda _seconds: None,
) -> tuple[JudgeValidationDependencies, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(returncode, "unbounded stdout must not persist", "stderr")

    return (
        JudgeValidationDependencies(
            current_staged_tree=lambda: current_tree,
            read_record=state.read,
            atomic_write_record=state.atomic_write,
            candidate_lock=state.candidate_lock,
            run_command=run,
            monotonic_clock=monotonic_clock,
            sleeper=sleeper,
        ),
        calls,
    )


def _diagnostic_dependencies(
    state: ValidationStateFake,
    *,
    artifacts: Mapping[Path, bytes | OSError],
    returncode: int = 1,
    stdout: str = "sensitive hook stdout",
    stderr: str = "sensitive hook stderr",
) -> tuple[JudgeValidationDependencies, list[tuple[str, ...]], list[Path]]:
    calls: list[tuple[str, ...]] = []
    artifact_reads: list[Path] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(returncode, stdout, stderr)

    def read_artifact(path: Path) -> bytes | None:
        artifact_reads.append(path)
        value = artifacts.get(path)
        if isinstance(value, OSError):
            raise value
        return value

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
    )
    return dependencies, calls, artifact_reads


def test_preview_never_runs_hook_or_mutates_durable_session() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=dependencies
    )

    assert report.state == "preview"
    assert calls == []
    assert state.writes == []
    assert state.locks == []


def test_terminal_failure_persists_only_aggregate_supported_junit_counts() -> None:
    state = ValidationStateFake()
    dependencies, calls, artifact_reads = _diagnostic_dependencies(
        state,
        returncode=7,
        stdout="stdout API_TOKEN=hook-secret",
        stderr="stderr password=hook-secret",
        artifacts={
            Path("test-results-py311.xml"): b"""
                <testsuites>
                  <testsuite name="private-suite" failures="1" errors="1">
                    <testcase name="secret-test-name">
                      <failure message="API_TOKEN=xml-secret">xml payload secret</failure>
                    </testcase>
                  </testsuite>
                  <testsuite name="second-private-suite" failures="1" errors="0" />
                </testsuites>
            """,
            Path("test-results-py314.xml"): b"""
                <testsuite tests="9" failures="4" errors="3">
                  <testcase name="another-secret-name" />
                </testsuite>
            """,
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        "git hook run pre-commit failed with exit code 7; "
        "JUnit py311 failures=2 errors=1; py314 failures=4 errors=3"
    )
    assert calls == [("git", "hook", "run", "pre-commit")]
    assert artifact_reads == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert report.diagnostic == expected
    assert state.records[_CANDIDATE]["diagnostic"] == expected
    persisted = str(state.records[_CANDIDATE])
    assert "hook-secret" not in persisted
    assert "xml-secret" not in persisted
    assert "secret-test-name" not in persisted


def test_terminal_failure_without_usable_artifacts_persists_fixed_fallback() -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, artifacts={}, returncode=2
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        "git hook run pre-commit failed with exit code 2; "
        "no failing JUnit summary available"
    )
    assert report.diagnostic == expected
    assert state.records[_CANDIDATE]["diagnostic"] == expected


@pytest.mark.parametrize(
    "artifact",
    (
        b"<testsuite failures='not-a-number' errors='1'/>",
        b"<testsuite failures='-1' errors='1'/>",
        b"<testsuite failures='0' errors='0'/>",
        (b"<testsuite failures='" + (b"9" * 600) + b"' errors='1'/>"),
        b"<testsuite><broken></testsuite>",
        b"\xff\xfe\xfd",
        b"x" * 1_048_577,
        PermissionError("artifact secret path is unreadable"),
    ),
    ids=(
        "nonnumeric-count",
        "negative-count",
        "no-failures",
        "unbounded-count",
        "malformed-xml",
        "invalid-utf8",
        "oversized-xml",
        "unreadable-xml",
    ),
)
def test_unusable_junit_artifact_fails_safely_to_generic_evidence(
    artifact: bytes | OSError,
) -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): artifact,
            Path("untrusted-result.xml"): (
                b"<testsuite failures='99' errors='99' name='secret'/>"
            ),
        },
        returncode=9,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic == (
        "git hook run pre-commit failed with exit code 9; "
        "no failing JUnit summary available"
    )
    assert "99" not in report.diagnostic
    assert "secret" not in report.diagnostic
    assert Path("untrusted-result.xml") not in _artifact_reads


@pytest.mark.parametrize("returncode", (-15, 1, 255))
def test_diagnostic_is_one_safe_line_with_a_strict_utf8_byte_bound(
    returncode: int,
) -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): (b"<testsuite failures='4' errors='1'/>")
        },
        returncode=returncode,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic is not None
    assert len(report.diagnostic.encode("utf-8")) <= 512
    assert report.diagnostic.isprintable()
    assert report.diagnostic.splitlines() == [report.diagnostic]


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    ((0, "terminal-success"), (1, "terminal-failure"), (-15, "terminal-failure")),
)
def test_apply_runs_one_fixed_hook_and_persists_its_terminal_code(
    returncode: int, expected_state: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state, returncode=returncode)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert state.locks == [_CANDIDATE]
    assert report.state == expected_state
    assert report.returncode == returncode
    expected_record: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "state": expected_state,
        "returncode": returncode,
    }
    if returncode != 0:
        expected_record["diagnostic"] = (
            f"git hook run pre-commit failed with exit code {returncode}; "
            "no failing JUnit summary available"
        )
    assert state.records[_CANDIDATE] == expected_record


@pytest.mark.parametrize(
    ("state_name", "returncode"),
    (("terminal-success", 0), ("terminal-failure", 1)),
)
def test_repeat_apply_for_terminal_candidate_does_not_duplicate_the_hook(
    state_name: str, returncode: int
) -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": state_name,
                "returncode": returncode,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == state_name
    assert calls == []
    assert state.writes == []


def test_repeat_failure_returns_stored_diagnostic_without_hook_or_rewrite() -> None:
    diagnostic = (
        "git hook run pre-commit failed with exit code 1; "
        "JUnit py311 failures=2 errors=0"
    )
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-failure",
                "returncode": 1,
                "diagnostic": diagnostic,
            }
        }
    )
    dependencies, calls, artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): (b"<testsuite failures='99' errors='99'/>")
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.action == "existing"
    assert report.diagnostic == diagnostic
    assert calls == []
    assert artifact_reads == []
    assert state.writes == []


def test_legacy_terminal_failure_without_diagnostic_remains_readable() -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-failure",
                "returncode": 1,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert report.state == "terminal-failure"
    assert report.diagnostic is None
    assert calls == []
    assert state.writes == []


def test_preview_success_and_running_reports_never_expose_diagnostics() -> None:
    preview_state = ValidationStateFake()
    preview_dependencies, _preview_calls = _dependencies(preview_state)
    preview = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=preview_dependencies
    )

    success_state = ValidationStateFake()
    success_dependencies, _success_calls, success_artifact_reads = (
        _diagnostic_dependencies(
            success_state,
            artifacts={
                Path("test-results-py311.xml"): (
                    b"<testsuite failures='4' errors='3'/>"
                )
            },
            returncode=0,
        )
    )
    success = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=success_dependencies
    )

    running_state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    running_dependencies, _running_calls = _dependencies(running_state)
    running = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=running_dependencies
    )

    assert preview.diagnostic is None
    assert success.diagnostic is None
    assert running.diagnostic is None
    assert success_artifact_reads == []


def test_recovery_never_starts_a_second_hook_for_existing_running_candidate() -> None:
    record: dict[str, object] = {"candidate_tree": _CANDIDATE, "state": "running"}
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "running"
    assert calls == []
    assert state.writes == []


def test_interrupted_hook_preserves_running_record_for_nonduplicating_recovery() -> (
    None
):
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []

    def interrupted_run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise RuntimeError("frontend handle lost")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=interrupted_run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="frontend handle lost"):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )
    recovered = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert state.records[_CANDIDATE] == {
        "candidate_tree": _CANDIDATE,
        "state": "running",
    }
    assert recovered.state == "running"


def test_observation_reports_running_then_terminal_without_hook_or_state_mutation() -> (
    None
):
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    dependencies, calls = _dependencies(state)

    running = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )
    state.records[_CANDIDATE] = {
        "candidate_tree": _CANDIDATE,
        "state": "terminal-failure",
        "returncode": 1,
    }
    terminal = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert running.state == "running" and running.returncode is None
    assert terminal.state == "terminal-failure" and terminal.returncode == 1
    assert calls == []
    assert state.writes == []


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    ((0, "terminal-success"), (1, "terminal-failure")),
)
def test_observation_waits_for_terminal_record_without_running_a_hook(
    returncode: int, expected_state: str
) -> None:
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        state.records[_CANDIDATE] = {
            "candidate_tree": _CANDIDATE,
            "state": expected_state,
            "returncode": returncode,
        }

    dependencies, calls = _dependencies(
        state, monotonic_clock=lambda: now[0], sleeper=sleep
    )

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies, wait_seconds=3
    )

    assert report.state == expected_state
    assert report.returncode == returncode
    assert sleeps == [1.0]
    assert calls == []
    assert state.writes == []


def test_observation_wait_stops_at_its_injected_monotonic_deadline() -> None:
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    dependencies, calls = _dependencies(
        state, monotonic_clock=lambda: now[0], sleeper=sleep
    )

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies, wait_seconds=3
    )

    assert report.state == "running"
    assert sleeps == [1.0, 1.0, 1.0]
    assert calls == []


@pytest.mark.parametrize(
    ("candidate_tree", "current_tree"),
    (("not-a-tree", _CANDIDATE), (_CANDIDATE, _OTHER_CANDIDATE)),
    ids=("malformed", "stale"),
)
def test_apply_rejects_malformed_or_noncurrent_candidate_before_state_or_hook(
    candidate_tree: str, current_tree: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state, current_tree=current_tree)

    with pytest.raises(JudgeValidationError):
        run_judge_validation(
            candidate_tree=candidate_tree, apply=True, dependencies=dependencies
        )

    assert calls == []
    assert state.writes == []
    assert state.locks == []


@pytest.mark.parametrize(
    "record",
    (
        {},
        {"candidate_tree": _OTHER_CANDIDATE, "state": "running"},
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": "1",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 1,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "stdout": "x" * 2_001,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "arbitrary persisted failure detail",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 1; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": -15,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 15; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code -5; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 0; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "git hook run pre-commit failed\nsecret",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "é" * 257,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 1; "
                "no failing JUnit summary available"
            ),
        },
        [],
    ),
    ids=(
        "missing",
        "mismatched-tree",
        "noninteger-code",
        "inconsistent-terminal-code",
        "unbounded-output",
        "noncanonical-diagnostic",
        "mismatched-positive-code",
        "mismatched-negative-record-code",
        "mismatched-negative-diagnostic-code",
        "zero-diagnostic-code",
        "multiline-diagnostic",
        "utf8-oversized-diagnostic",
        "success-with-diagnostic",
        "non-object-record",
    ),
)
def test_observation_fails_closed_for_malformed_or_unbounded_record(
    record: dict[str, object],
) -> None:
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, calls = _dependencies(state)

    with pytest.raises(JudgeValidationError):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)

    assert calls == []
    assert state.writes == []
