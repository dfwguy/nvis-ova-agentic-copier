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

from collections.abc import Callable, Iterator
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


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    ((0, "terminal-success"), (1, "terminal-failure")),
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
    assert state.records[_CANDIDATE] == {
        "candidate_tree": _CANDIDATE,
        "state": expected_state,
        "returncode": returncode,
    }


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
        [],
    ),
    ids=(
        "missing",
        "mismatched-tree",
        "noninteger-code",
        "inconsistent-terminal-code",
        "unbounded-output",
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
