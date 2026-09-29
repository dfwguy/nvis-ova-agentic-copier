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

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import ascii_letters, digits
from typing import cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work import validation
from agent_work.cheap_gate import check_candidate_gate
from agent_work.cli import _read_validation_artifact
from agent_work.ledger import state_home
from agent_work.ports import CommandResult as PortCommandResult
from agent_work.repository import CommandResult
from agent_work.test_timing import parse_full_junit_records
from agent_work.timing_admission import TimingAdmissionLease
from agent_work.timing_capture import (
    FocusedTimingCaptureError,
    TimingCaptureDependencies,
    TimingCaptureError,
    capture_serial_timing_review,
)
from agent_work.validation import (
    FilesystemJUnitSnapshotStore,
    JudgeValidationDependencies,
    JudgeValidationError,
    observe_judge_validation,
    run_judge_validation,
)

_CANDIDATE = "a" * 40
_OTHER_CANDIDATE = "b" * 40
_PARAM_ID_FIRST = ascii_letters + digits + "_-"
_PARAM_ID_REST = _PARAM_ID_FIRST + "."


def _timing_transcript() -> dict[str, object]:
    digest = hashlib.sha256(
        b"<testsuite tests='1' failures='0' errors='0'/>"
    ).hexdigest()
    return {
        "baseline_tree": _OTHER_CANDIDATE,
        "candidate_tree": _CANDIDATE,
        "outcome": "clear",
        "runs": {
            environment: {
                "candidate": {
                    "sha256": digest,
                    "test_count": 1,
                    "timing_complete": True,
                }
            }
            for environment in ("py311", "py314")
        },
    }


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


def _passing_gate(
    dependencies: JudgeValidationDependencies,
) -> JudgeValidationDependencies:
    return replace(
        dependencies,
        check_candidate_gate=lambda _tree: CommandResult(0, "", ""),
    )


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

    def capture(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in artifacts
        )

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: current_tree,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=monotonic_clock,
        sleeper=sleeper,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='0' errors='0'/>",
        capture_junit_artifacts=capture,
    )
    return (
        _passing_gate(dependencies),
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

    def capture(
        candidate_tree: str, captured: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in captured
        )

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        capture_junit_artifacts=capture,
    )
    return (
        _passing_gate(dependencies),
        calls,
        artifact_reads,
    )


def _preflight_payload(
    outcome: str, violations: list[dict[str, str]] | None = None
) -> str:
    return json.dumps(
        {"schema_version": 1, "outcome": outcome, "violations": violations or []}
    )


@pytest.mark.parametrize("gate_returncode", (0, 1))
def test_judge_runs_cheap_gate_on_exact_candidate_before_full_hook(
    gate_returncode: int,
) -> None:
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    events: list[str] = []
    dependencies = replace(
        dependencies,
        check_candidate_gate=lambda tree: (
            events.append(f"gate:{tree}") or CommandResult(gate_returncode, "", "")
        ),
        run_hook=lambda _command, _lease: (
            events.append("hook") or CommandResult(0, "", "")
        ),
        remove_junit_artifact=lambda _path: events.append("junit cleanup"),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert events[0] == f"gate:{_CANDIDATE}"
    assert ("hook" in events) is (gate_returncode == 0)
    assert (report.state == "terminal-success") is (gate_returncode == 0)
    if gate_returncode:
        assert "junit cleanup" not in events
        assert state.writes == []


def test_judge_changed_candidate_requires_fresh_cheap_gate() -> None:
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    current = [_CANDIDATE]
    gated: list[str] = []
    dependencies = replace(
        dependencies,
        current_staged_tree=lambda: current[0],
        check_candidate_gate=lambda tree: (
            gated.append(tree) or CommandResult(1, "", "")
        ),
    )

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    current[0] = _OTHER_CANDIDATE
    second = run_judge_validation(
        candidate_tree=_OTHER_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert first.state != "terminal-success"
    assert second.state != "terminal-success"
    assert gated == [_CANDIDATE, _OTHER_CANDIDATE]


def test_judge_rejects_candidate_changed_during_cheap_gate_before_hook() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    current = [_CANDIDATE]

    def gate(_tree: str) -> CommandResult:
        current[0] = _OTHER_CANDIDATE
        return CommandResult(0, "", "")

    dependencies = replace(
        dependencies,
        current_staged_tree=lambda: current[0],
        check_candidate_gate=gate,
    )

    with pytest.raises(JudgeValidationError, match="staged tree"):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )

    assert hooks == []
    assert state.writes == []


def test_judge_reports_safe_failing_check_without_tool_output() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    dependencies = replace(
        dependencies,
        check_candidate_gate=lambda _tree: CommandResult(
            1, "private stdout", "ruff-check failed: private stderr"
        ),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "preflight-failure"
    assert report.hook_failures[0].hook_id == "ruff-check"
    assert "private" not in str(report)
    assert hooks == []
    assert state.writes == []


@pytest.mark.parametrize("error", (RuntimeError("setup path"), TimeoutError("token")))
def test_judge_gate_exception_fails_closed_before_hook(error: Exception) -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)

    def failed_gate(_tree: str) -> CommandResult:
        raise error

    dependencies = replace(dependencies, check_candidate_gate=failed_gate)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "preflight-failure"
    assert hooks == []
    assert state.writes == []
    assert "token" not in str(report)
    assert "setup path" not in str(report)


def test_judge_missing_gate_fails_closed_before_record_junit_or_hook() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    removed: list[Path] = []
    dependencies = replace(
        dependencies,
        check_candidate_gate=None,
        remove_junit_artifact=removed.append,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "preflight-failure"
    assert state.writes == []
    assert removed == []
    assert hooks == []


@pytest.mark.parametrize("initial_hook", ("missing", "not-executable"))
def test_hook_preflight_allows_same_tree_retry_after_install(
    initial_hook: str, tmp_path: Path
) -> None:
    state = ValidationStateFake()
    dependencies, _ = _dependencies(state)
    source = tmp_path / "source"
    (source / ".git" / "objects").mkdir(parents=True)
    hook_path = source / "configured-hooks" / "pre-commit"
    hook_path.parent.mkdir()
    if initial_hook == "not-executable":
        hook_path.write_text("#!/bin/sh\nexit 0\n")
        hook_path.chmod(0o644)

    cheap_check_fails = [True]
    cheap_commands: list[tuple[str, ...]] = []
    hook_lookups: list[tuple[str, ...]] = []
    hook_runs: list[tuple[str, ...]] = []
    removed: list[Path] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        directory = kwargs.get("current_directory")
        if command == ("git", "rev-parse", "HEAD"):
            assert directory == source
            return CommandResult(0, _OTHER_CANDIDATE, "")
        if command == ("git", "rev-parse", "--git-common-dir"):
            assert directory == source
            return CommandResult(0, ".git", "")
        if command == ("git", "rev-parse", "--git-path", "hooks/pre-commit"):
            assert directory == source
            hook_lookups.append(command)
            return CommandResult(0, "configured-hooks/pre-commit\n", "")
        if command == ("git", "init", "--quiet"):
            assert isinstance(directory, Path)
            (directory / ".git").mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, _CANDIDATE, "")
        if command == ("git", "update-ref", "HEAD", _OTHER_CANDIDATE):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, "", "")
        if command == ("git", "read-tree", _CANDIDATE):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, "", "")
        if command == ("git", "checkout-index", "--all"):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, "", "")
        if command == ("git", "status", "--porcelain", "--untracked-files=all"):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, "", "")
        if command == ("git", "ls-files", "-z"):
            assert isinstance(directory, Path) and directory != source
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            hook_runs.append(command)
            return CommandResult(0, "", "")
        if command[0] == "git":
            pytest.fail(f"unexpected Git command: {command}")
        cheap_commands.append(command)
        return CommandResult(1 if cheap_check_fails[0] else 0, "", "")

    dependencies = replace(
        dependencies,
        run_command=run,
        remove_junit_artifact=removed.append,
        check_candidate_gate=lambda candidate: check_candidate_gate(
            candidate,
            source=source,
            current_staged_tree=lambda: _CANDIDATE,
            run_command=run,
        ),
    )

    cheap_failure = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert cheap_failure.state == "preflight-failure"
    assert cheap_failure.hook_failures[0].hook_id == "yamllint"
    assert hook_lookups == []
    assert len(cheap_commands) == 1
    assert state.writes == []
    assert removed == []
    assert hook_runs == []

    cheap_check_fails[0] = False
    missing_report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert missing_report.state == "preflight-failure"
    assert missing_report.hook_failures[0].hook_id == "pre-commit-hook"
    assert hook_lookups
    assert state.writes == []
    assert removed == []
    assert hook_runs == []

    hook_path.write_text("#!/bin/sh\nexit 0\n")
    hook_path.chmod(0o755)
    installed_report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert installed_report.state == "terminal-success"
    assert state.records[_CANDIDATE]["state"] == "terminal-success"
    assert hook_runs == [("git", "hook", "run", "pre-commit")]


def test_license_violation_preflight_keeps_candidate_without_session_or_hook() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    events: list[str] = []
    dependencies = replace(
        dependencies,
        run_license_preflight=lambda tree: (
            events.append(f"preflight:{tree}")
            or CommandResult(
                1,
                _preflight_payload(
                    "violations",
                    [
                        {
                            "path": "template/bad.py",
                            "message": "missing NVIDIA license header",
                        }
                    ],
                ),
                "",
            )
        ),
        remove_junit_artifact=lambda path: events.append(f"remove:{path}"),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.candidate_tree == _CANDIDATE
    assert report.state == "preflight-failure"
    assert report.hook_failures[0].hook_id == "license-check"
    assert state.locks == [_CANDIDATE]
    assert state.writes == []
    assert events == [f"preflight:{_CANDIDATE}"]
    assert hooks == []


def test_project_admission_covers_license_rejection_without_running_state() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    events: list[str] = []

    @contextmanager
    def project_admission() -> Iterator[None]:
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    dependencies = replace(
        dependencies,
        project_admission=project_admission,
        run_license_preflight=lambda _tree: (
            events.append("preflight")
            or CommandResult(
                1,
                _preflight_payload(
                    "violations",
                    [{"path": "bad.py", "message": "missing NVIDIA license header"}],
                ),
                "",
            )
        ),
        run_hook=lambda _command, _lease: (
            events.append("hook") or CommandResult(0, "", "")
        ),
        remove_junit_artifact=lambda _path: events.append("cleanup"),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "preflight-failure"
    assert events == ["admitted", "preflight", "released"]
    assert state.writes == []
    assert hooks == []


def test_compliant_license_preflight_precedes_one_full_hook() -> None:
    state = ValidationStateFake()
    dependencies, _hooks = _dependencies(state)
    events: list[str] = []
    dependencies = replace(
        dependencies,
        run_license_preflight=lambda tree: (
            events.append(f"preflight:{tree}")
            or CommandResult(0, _preflight_payload("compliant"), "")
        ),
        run_command=lambda command, **_kwargs: (
            events.append(" ".join(command)) or CommandResult(0, "", "")
        ),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "terminal-success"
    assert events == [f"preflight:{_CANDIDATE}", "git hook run pre-commit"]
    assert len(state.writes) >= 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    (
        (0, "not-json"),
        (0, _preflight_payload("violations", [{"path": "bad.py", "message": "bad"}])),
        (1, _preflight_payload("compliant")),
        (2, '{"schema_version":1,"outcome":"error","error":"unavailable"}'),
        (0, '{"schema_version":1,"outcome":"compliant","violations":[],"extra":1}'),
    ),
    ids=(
        "malformed",
        "mismatched-violation",
        "mismatched-compliant",
        "error",
        "extra-field",
    ),
)
def test_invalid_license_preflight_fails_closed_before_session_or_hook(
    returncode: int, stdout: str
) -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    removals: list[Path] = []
    dependencies = replace(
        dependencies,
        run_license_preflight=lambda _tree: CommandResult(returncode, stdout, "secret"),
        remove_junit_artifact=removals.append,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "preflight-failure"
    assert report.candidate_tree == _CANDIDATE
    assert state.writes == []
    assert removals == []
    assert hooks == []
    assert "secret" not in str(report)


def test_staged_tree_drift_after_preflight_rejects_without_session_or_hook() -> None:
    state = ValidationStateFake()
    dependencies, hooks = _dependencies(state)
    current_tree = iter((_CANDIDATE, _OTHER_CANDIDATE))
    dependencies = replace(
        dependencies,
        current_staged_tree=lambda: next(current_tree),
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
    )

    with pytest.raises(JudgeValidationError, match="staged tree"):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )

    assert state.writes == []
    assert hooks == []


def test_corrected_tree_runs_after_bad_tree_and_existing_record_is_idempotent() -> None:
    state = ValidationStateFake()
    hooks: list[tuple[str, ...]] = []
    preflights: list[str] = []
    current = [_CANDIDATE]
    dependencies, _ = _dependencies(state)

    def preflight(tree: str) -> CommandResult:
        preflights.append(tree)
        return CommandResult(
            1 if tree == _CANDIDATE else 0,
            _preflight_payload(
                "violations" if tree == _CANDIDATE else "compliant",
                [{"path": "bad.py", "message": "missing NVIDIA license header"}]
                if tree == _CANDIDATE
                else [],
            ),
            "",
        )

    dependencies = replace(
        dependencies,
        current_staged_tree=lambda: current[0],
        run_license_preflight=preflight,
        run_command=lambda command, **_kwargs: (
            hooks.append(command) or CommandResult(0, "", "")
        ),
    )
    rejected = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    current[0] = _OTHER_CANDIDATE
    accepted = run_judge_validation(
        candidate_tree=_OTHER_CANDIDATE, apply=True, dependencies=dependencies
    )
    existing = run_judge_validation(
        candidate_tree=_OTHER_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert rejected.state == "preflight-failure"
    assert _CANDIDATE not in state.records
    assert accepted.state == existing.state == "terminal-success"
    assert existing.action == "existing"
    assert preflights == [_CANDIDATE, _OTHER_CANDIDATE]
    assert hooks == [("git", "hook", "run", "pre-commit")]


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


def test_project_admission_covers_running_record_hook_artifacts_and_terminal_record() -> (
    None
):
    state = ValidationStateFake()
    events: list[str] = []
    dependencies, _ = _dependencies(state)

    @contextmanager
    def project_admission() -> Iterator[None]:
        events.append("admitted")
        try:
            yield
        finally:
            events.append("released")

    def write_record(tree: str, record: dict[str, object]) -> None:
        events.append(f"record:{record['state']}")
        state.atomic_write(tree, record)

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command == ("git", "hook", "run", "pre-commit")
        events.append("hook")
        return CommandResult(0, "", "")

    def capture(
        _tree: str, _artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        events.append("capture")
        return (
            Path("/snapshots") / _CANDIDATE / "test-results-py311.xml",
            Path("/snapshots") / _CANDIDATE / "test-results-py314.xml",
        )

    dependencies = replace(
        dependencies,
        project_admission=project_admission,
        atomic_write_record=write_record,
        remove_junit_artifact=lambda _path: events.append("cleanup"),
        run_command=run,
        read_artifact=lambda _path: (
            events.append("artifact_read")
            or b"<testsuite tests='1' failures='0' errors='0'/>"
        ),
        capture_junit_artifacts=capture,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "terminal-success"
    assert events[0] == "admitted"
    assert events[-1] == "released"
    assert (
        events.index("record:running") < events.index("cleanup") < events.index("hook")
    )
    assert (
        events.index("hook") < events.index("artifact_read") < events.index("capture")
    )
    assert events.index("capture") < events.index("record:terminal-success")


def test_project_admission_contention_leaves_candidate_and_hook_untouched() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    cleanup: list[Path] = []

    @contextmanager
    def busy_admission() -> Iterator[None]:
        raise JudgeValidationError("project timing admission busy")
        yield

    dependencies = replace(
        dependencies,
        project_admission=busy_admission,
        remove_junit_artifact=cleanup.append,
    )

    with pytest.raises(JudgeValidationError, match="admission busy"):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )

    assert state.writes == []
    assert state.records == {}
    assert state.locks == []
    assert cleanup == []
    assert calls == []


def test_project_admission_serializes_separate_candidates_before_either_hook() -> None:
    first_state = ValidationStateFake()
    second_state = ValidationStateFake()
    held = False
    second_calls: list[tuple[str, ...]] = []

    @contextmanager
    def shared_admission() -> Iterator[None]:
        nonlocal held
        if held:
            raise JudgeValidationError("project timing admission busy")
        held = True
        try:
            yield
        finally:
            held = False

    first_dependencies, _ = _dependencies(first_state)
    second_dependencies, _ = _dependencies(second_state, current_tree=_OTHER_CANDIDATE)
    second_dependencies = replace(
        second_dependencies,
        project_admission=shared_admission,
        run_command=lambda command, **_kwargs: (
            second_calls.append(command) or CommandResult(0, "", "")
        ),
    )

    def first_hook(_command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        with pytest.raises(JudgeValidationError, match="admission busy"):
            run_judge_validation(
                candidate_tree=_OTHER_CANDIDATE,
                apply=True,
                dependencies=second_dependencies,
            )
        assert second_state.writes == []
        assert second_calls == []
        return CommandResult(0, "", "")

    first_dependencies = replace(
        first_dependencies,
        project_admission=shared_admission,
        run_command=first_hook,
    )

    first_report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=first_dependencies
    )
    second_report = run_judge_validation(
        candidate_tree=_OTHER_CANDIDATE, apply=True, dependencies=second_dependencies
    )

    assert first_report.state == second_report.state == "terminal-success"
    assert second_calls == [("git", "hook", "run", "pre-commit")]


def test_project_admission_keeps_distinct_projects_independent() -> None:
    first_state = ValidationStateFake()
    second_state = ValidationStateFake()
    first_dependencies, _ = _dependencies(first_state)
    second_dependencies, second_calls = _dependencies(
        second_state, current_tree=_OTHER_CANDIDATE
    )
    first_held = False
    second_held = False

    @contextmanager
    def first_admission() -> Iterator[None]:
        nonlocal first_held
        assert not first_held
        first_held = True
        try:
            yield
        finally:
            first_held = False

    @contextmanager
    def second_admission() -> Iterator[None]:
        nonlocal second_held
        assert not second_held
        second_held = True
        try:
            yield
        finally:
            second_held = False

    second_dependencies = replace(
        second_dependencies, project_admission=second_admission
    )

    def first_hook(_command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nested = run_judge_validation(
            candidate_tree=_OTHER_CANDIDATE,
            apply=True,
            dependencies=second_dependencies,
        )
        assert nested.state == "terminal-success"
        assert first_held and not second_held
        return CommandResult(0, "", "")

    first_dependencies = replace(
        first_dependencies,
        project_admission=first_admission,
        run_command=first_hook,
    )
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=first_dependencies
    )

    assert report.state == "terminal-success"
    assert second_calls == [("git", "hook", "run", "pre-commit")]


@pytest.mark.parametrize(
    ("phase", "returncode", "expected_stage", "expected_reason", "expected_code"),
    (
        ("before", 0, "pre_hook_cleanup", "os_error", None),
        ("failed_read", 7, "post_hook_failed_evidence_read", "os_error", 7),
        ("failed_cleanup", 7, "post_hook_failed_cleanup", "os_error", 7),
        ("success_cleanup", 0, "post_hook_success_cleanup", "os_error", 0),
        ("snapshot", 0, "immutable_snapshot", "os_error", 0),
    ),
)
def test_artifact_io_failure_persists_bounded_stage_and_actual_hook_code(
    phase: str,
    returncode: int,
    expected_stage: str,
    expected_reason: str,
    expected_code: int | None,
) -> None:
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    removed = 0
    read = 0

    def remove(_path: Path) -> None:
        nonlocal removed
        removed += 1
        if (phase == "before" and removed == 1) or (
            phase in {"failed_cleanup", "success_cleanup"} and removed == 3
        ):
            raise OSError("PRIVATE_TOKEN=cleanup-secret /home/private")

    def read_artifact(_path: Path) -> bytes:
        nonlocal read
        read += 1
        if phase in {"failed_read", "success_read"}:
            raise OSError("PRIVATE_TOKEN=read-secret /home/private")
        return b"<testsuite tests='1' failures='1' errors='0'/>"

    def capture(_tree: str, _artifacts: object) -> tuple[Path, ...]:
        if phase == "snapshot":
            raise OSError("PRIVATE_TOKEN=snapshot-secret /home/private")
        return ()

    dependencies, _ = _dependencies(state, returncode=returncode)
    dependencies = replace(
        dependencies,
        run_command=lambda command, **_kwargs: (
            calls.append(command)
            or CommandResult(returncode, "PRIVATE_TOKEN=hook-secret", "")
        ),
        remove_junit_artifact=remove,
        read_artifact=read_artifact,
        capture_junit_artifacts=capture,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert report.state == status.state == "terminal-artifact-failure"
    assert report.returncode == status.returncode == expected_code
    assert report.artifact_failure is not None
    assert status.artifact_failure is not None
    assert (
        report.artifact_failure.stage == status.artifact_failure.stage == expected_stage
    )
    assert report.artifact_failure.reason == expected_reason
    assert report.artifact_failure.environment == (
        None if phase == "snapshot" else "py311"
    )
    assert report.artifact_failure.path == (
        None if phase == "snapshot" else Path("test-results-py311.xml")
    )
    assert state.records[_CANDIDATE]["artifact_failure"] == {
        "stage": expected_stage,
        "reason": expected_reason,
        **(
            {}
            if phase == "snapshot"
            else {"environment": "py311", "path": "test-results-py311.xml"}
        ),
    }
    assert len(calls) == (0 if phase == "before" else 1)
    assert "PRIVATE_TOKEN" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    ("artifact", "reason"),
    (
        (b"<testsuite>", "invalid_xml"),
        (b"x" * 1_048_577, "oversized"),
        (b"<testsuite tests='unknown'/>", "invalid_count"),
    ),
    ids=("malformed", "oversized", "invalid-count"),
)
def test_successful_hook_classifies_unusable_fixed_xml_without_raw_bytes(
    artifact: bytes | None, reason: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.returncode == 0
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == "post_hook_success_evidence_read"
    assert report.artifact_failure.reason == reason
    assert report.artifact_failure.environment == "py311"
    assert report.artifact_failure.path == Path("test-results-py311.xml")
    assert len(str(state.records[_CANDIDATE]).encode()) < 2048


@pytest.mark.parametrize(
    ("unavailable", "reason", "environment"),
    (
        (None, "missing", "py311"),
        (OSError("PRIVATE_TOKEN=read-secret /private/path"), "os_error", "py314"),
    ),
)
def test_successful_hook_with_unreadable_fixed_junit_records_unknown_timing(
    unavailable: bytes | OSError | None, reason: str, environment: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)

    def read(path: Path) -> bytes | None:
        if path.name == f"test-results-{environment}.xml":
            if isinstance(unavailable, OSError):
                raise unavailable
            return unavailable
        return b"<testsuite tests='1' failures='0' errors='0'/>"

    dependencies = replace(dependencies, read_artifact=read)
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == status.state == "terminal-success"
    assert report.exit_code == status.exit_code == 0
    assert report.timing_status == status.timing_status == "unknown"
    assert (
        report.timing_unknown
        == status.timing_unknown
        == {
            "environment": environment,
            "reason": reason,
        }
    )
    assert report.junit_artifacts == status.junit_artifacts == ()
    assert report.junit_timings == status.junit_timings == ()
    assert state.records[_CANDIDATE]["timing_status"] == "unknown"
    assert state.records[_CANDIDATE]["timing_unknown"] == {
        "environment": environment,
        "reason": reason,
    }
    assert "PRIVATE_TOKEN" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    ("unavailable", "unusable", "reason"),
    (
        (None, b"<testsuite>", "invalid_xml"),
        (None, b"x" * 1_048_577, "oversized"),
        (None, b"<testsuite tests='unknown'/>", "invalid_count"),
        (OSError("PRIVATE_TOKEN=read-secret"), b"<testsuite>", "invalid_xml"),
    ),
    ids=(
        "missing-malformed",
        "missing-oversized",
        "missing-invalid-count",
        "read-error-malformed",
    ),
)
def test_unusable_second_junit_remains_terminal_when_first_is_unavailable(
    unavailable: OSError | None, unusable: bytes, reason: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    reads: list[Path] = []
    removals: list[Path] = []
    captures: list[object] = []

    def read(path: Path) -> bytes | None:
        reads.append(path)
        if path.name == "test-results-py311.xml":
            if isinstance(unavailable, OSError):
                raise unavailable
            return None
        return unusable

    dependencies = replace(
        dependencies,
        read_artifact=read,
        remove_junit_artifact=removals.append,
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert reads == [Path("test-results-py311.xml"), Path("test-results-py314.xml")]
    assert removals == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert captures == []
    assert report.state == "terminal-artifact-failure"
    assert report.returncode == 0
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == "post_hook_success_evidence_read"
    assert report.artifact_failure.reason == reason
    assert report.artifact_failure.environment == "py314"
    assert report.artifact_failure.path == Path("test-results-py314.xml")
    assert "timing_unknown" not in state.records[_CANDIDATE]
    assert "PRIVATE_TOKEN" not in str(state.records[_CANDIDATE])


def test_actual_failed_xml_file_is_classified_through_fixed_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("test-results-py311.xml").write_bytes(
        b"<testsuite tests='private-count'><secret>PRIVATE_TOKEN</secret></testsuite>"
    )
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    dependencies = replace(
        dependencies,
        read_artifact=_read_validation_artifact,
        remove_junit_artifact=lambda _path: None,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert report.artifact_failure.reason == "invalid_count"
    assert report.artifact_failure.path == Path("test-results-py311.xml")
    assert "PRIVATE_TOKEN" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    ("cleanup_fails", "expected_stage"),
    (
        (False, "post_hook_failed_evidence_read"),
        (True, "post_hook_failed_cleanup"),
    ),
)
def test_failed_hook_read_error_still_attempts_both_fixed_cleanup_paths(
    cleanup_fails: bool, expected_stage: str
) -> None:
    state = ValidationStateFake()
    removed: list[Path] = []
    dependencies, calls = _dependencies(state, returncode=7)

    def remove(path: Path) -> None:
        removed.append(path)
        if cleanup_fails and len(removed) == 3:
            raise OSError("PRIVATE_TOKEN=cleanup-secret")

    def read(_path: Path) -> bytes:
        raise OSError("PRIVATE_TOKEN=read-secret")

    dependencies = replace(
        dependencies, remove_junit_artifact=remove, read_artifact=read
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert removed == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert report.state == "terminal-artifact-failure"
    assert report.returncode == 7
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == expected_stage
    assert report.artifact_failure.path == Path("test-results-py311.xml")
    assert "PRIVATE_TOKEN" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize("schema_version", (None, 2), ids=("legacy", "schema2"))
def test_old_artifact_failure_without_detail_stays_readable(
    schema_version: int | None,
) -> None:
    old: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "state": "terminal-artifact-failure",
        "returncode": None,
    }
    if schema_version is not None:
        old["schema_version"] = schema_version
    state = ValidationStateFake(records={_CANDIDATE: old})
    dependencies, calls = _dependencies(state)

    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert status.state == "terminal-artifact-failure"
    assert status.artifact_failure is None
    assert calls == []
    assert state.writes == []


def test_pre_hook_cleanup_retry_is_dry_run_then_one_append_only_attempt() -> None:
    state = ValidationStateFake()
    commands: list[tuple[str, ...]] = []
    retry_records: dict[str, dict[str, object]] = {}
    retry_writes: list[dict[str, object]] = []
    retained_lease = TimingAdmissionLease(123)
    hook_leases: list[TimingAdmissionLease | None] = []
    initial_dependencies, _ = _dependencies(state)
    initial_dependencies = replace(
        initial_dependencies,
        remove_junit_artifact=lambda _path: (_ for _ in ()).throw(
            OSError("private cleanup failure")
        ),
    )
    original = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=initial_dependencies
    )
    original_record = json.loads(json.dumps(state.records[_CANDIDATE]))

    def write_retry(tree: str, record: dict[str, object]) -> None:
        assert tree == _CANDIDATE
        assert not retry_records or retry_records[tree]["state"] == "running"
        retry_records[tree] = record
        retry_writes.append(record)

    def run_hook(
        command: tuple[str, ...], lease: TimingAdmissionLease | None
    ) -> CommandResult:
        commands.append(command)
        hook_leases.append(lease)
        return CommandResult(0, "", "")

    retry_dependencies = replace(
        initial_dependencies,
        remove_junit_artifact=lambda _path: None,
        project_admission=lambda: nullcontext(retained_lease),
        run_hook=run_hook,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=write_retry,
    )

    preview = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=retry_dependencies
    )
    assert preview.action == "retry_preview"
    assert retry_records == {}
    assert commands == []

    retried = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    repeated = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    ordinary_run = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=retry_dependencies
    )

    assert original.returncode is None
    assert retried.state == repeated.state == status.state == "terminal-success"
    assert repeated.action == "existing_retry"
    assert ordinary_run.state == "terminal-artifact-failure"
    assert commands == [("git", "hook", "run", "pre-commit")]
    assert hook_leases == [retained_lease]
    assert state.records[_CANDIDATE] == original_record
    assert len(state.writes) == 2
    assert retry_writes[0]["state"] == "running"
    assert retry_records[_CANDIDATE]["candidate_tree"] == _CANDIDATE
    assert (
        retry_records[_CANDIDATE]["original_sha256"]
        == hashlib.sha256(
            json.dumps(original_record, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_retry_contention_does_not_write_retry_record_or_run_hook() -> None:
    state = ValidationStateFake()
    initial_dependencies, _ = _dependencies(state)
    initial_dependencies = replace(
        initial_dependencies,
        remove_junit_artifact=lambda _path: (_ for _ in ()).throw(
            OSError("cleanup failed before hook")
        ),
    )
    original = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=initial_dependencies
    )
    original_record = json.loads(json.dumps(state.records[_CANDIDATE]))
    retry_writes: list[dict[str, object]] = []
    calls: list[tuple[str, ...]] = []

    @contextmanager
    def busy_admission() -> Iterator[None]:
        raise JudgeValidationError("project timing admission busy")
        yield

    retry_dependencies = replace(
        initial_dependencies,
        project_admission=busy_admission,
        remove_junit_artifact=lambda _path: None,
        atomic_write_retry_record=lambda _tree, record: retry_writes.append(record),
        run_command=lambda command, **_kwargs: (
            calls.append(command) or CommandResult(0, "", "")
        ),
    )

    with pytest.raises(JudgeValidationError, match="admission busy"):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
        )

    assert original.state == "terminal-artifact-failure"
    assert state.records[_CANDIDATE] == original_record
    assert retry_writes == []
    assert calls == []


@pytest.mark.parametrize(
    ("stage", "returncode"),
    (
        ("post_hook_failed_evidence_read", 7),
        ("post_hook_success_evidence_read", 0),
        ("post_hook_success_cleanup", 0),
        ("immutable_snapshot", 0),
    ),
)
def test_retry_rejects_every_post_hook_artifact_failure(
    stage: str, returncode: int
) -> None:
    original: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": "terminal-artifact-failure",
        "returncode": returncode,
        "artifact_failure": {
            "stage": stage,
            "reason": "os_error",
            "environment": "py311",
            "path": "test-results-py311.xml",
        },
    }
    state = ValidationStateFake(records={_CANDIDATE: original})
    dependencies, calls = _dependencies(state)
    retry_records: dict[str, dict[str, object]] = {}
    dependencies = replace(
        dependencies,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=lambda tree, record: retry_records.update(
            {tree: record}
        ),
    )

    with pytest.raises(JudgeValidationError, match="retry"):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )

    assert calls == []
    assert retry_records == {}
    assert state.records[_CANDIDATE] == original


def test_retry_requires_exact_current_staged_tree_and_preserves_running_attempt() -> (
    None
):
    original: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": "terminal-artifact-failure",
        "artifact_failure": {
            "stage": "pre_hook_cleanup",
            "reason": "os_error",
            "environment": "py311",
            "path": "test-results-py311.xml",
        },
    }
    state = ValidationStateFake(records={_CANDIDATE: original})
    retry_records: dict[str, dict[str, object]] = {}
    dependencies, calls = _dependencies(state, current_tree=_OTHER_CANDIDATE)
    dependencies = replace(
        dependencies,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=lambda tree, record: retry_records.update(
            {tree: record}
        ),
    )

    with pytest.raises(JudgeValidationError, match="staged tree"):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )
    assert retry_records == {}
    assert calls == []

    retry_records[_CANDIDATE] = {
        "candidate_tree": _CANDIDATE,
        "state": "running",
        "original_sha256": hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    dependencies = replace(dependencies, current_staged_tree=lambda: _CANDIDATE)
    result = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert result.state == "running"
    assert calls == []
    assert state.records[_CANDIDATE] == original


def test_status_wait_polls_the_retry_record_until_it_is_terminal() -> None:
    original: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": "terminal-artifact-failure",
        "artifact_failure": {
            "stage": "pre_hook_cleanup",
            "reason": "os_error",
            "environment": "py311",
            "path": "test-results-py311.xml",
        },
    }
    state = ValidationStateFake(records={_CANDIDATE: original})
    original_sha256 = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    retry_records: dict[str, dict[str, object]] = {
        _CANDIDATE: {
            "candidate_tree": _CANDIDATE,
            "state": "running",
            "original_sha256": original_sha256,
        }
    }
    clock = 0.0

    def now() -> float:
        return clock

    def sleep(seconds: float) -> None:
        nonlocal clock
        clock += seconds
        retry_records[_CANDIDATE] = {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 7,
            "original_sha256": original_sha256,
        }

    dependencies, calls = _dependencies(state, monotonic_clock=now, sleeper=sleep)
    dependencies = replace(dependencies, read_retry_record=retry_records.get)

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies, wait_seconds=2
    )

    assert report.state == "terminal-failure"
    assert report.returncode == 7
    assert calls == []
    assert state.records[_CANDIDATE] == original


@pytest.mark.parametrize("retry_case", ("wrong_sha", "forged_success"))
def test_retry_record_rejects_unbound_or_forged_terminal_evidence(
    retry_case: str,
) -> None:
    original: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": "terminal-artifact-failure",
        "artifact_failure": {
            "stage": "pre_hook_cleanup",
            "reason": "os_error",
            "environment": "py311",
            "path": "test-results-py311.xml",
        },
    }
    state = ValidationStateFake(records={_CANDIDATE: original})
    original_sha256 = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    retry_record: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "state": "running" if retry_case == "wrong_sha" else "terminal-success",
        "original_sha256": "b" * 64 if retry_case == "wrong_sha" else original_sha256,
    }
    if retry_case == "forged_success":
        retry_record["returncode"] = 0
        retry_record["schema_version"] = 2
    dependencies, calls = _dependencies(state)
    dependencies = replace(
        dependencies,
        read_retry_record=lambda _tree: retry_record,
    )

    with pytest.raises(JudgeValidationError, match="malformed"):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)
    with pytest.raises(JudgeValidationError, match="malformed"):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )
    assert calls == []
    assert state.records[_CANDIDATE] == original


def test_snapshot_py314_write_failure_names_only_its_fixed_artifact(
    tmp_path: Path,
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)

    def write_bytes(path: Path, contents: bytes) -> None:
        if path.name == "test-results-py314.xml":
            raise OSError("secret py314 path")
        path.write_bytes(contents)

    store = FilesystemJUnitSnapshotStore(tmp_path, write_bytes=write_bytes)
    dependencies = replace(
        dependencies,
        capture_junit_artifacts=lambda tree, artifacts: store.capture(
            candidate_tree=tree, artifacts=artifacts
        ),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == "immutable_snapshot"
    assert report.artifact_failure.reason == "os_error"
    assert report.artifact_failure.environment == "py314"
    assert report.artifact_failure.path == Path("test-results-py314.xml")
    assert "secret" not in str(state.records[_CANDIDATE])


def test_snapshot_invalid_candidate_has_no_false_artifact_attribution() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    dependencies = replace(
        dependencies,
        capture_junit_artifacts=lambda _tree, _artifacts: (_ for _ in ()).throw(
            JudgeValidationError("private existing snapshot")
        ),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == "immutable_snapshot"
    assert report.artifact_failure.reason == "invalid_snapshot"
    assert report.artifact_failure.environment is None
    assert report.artifact_failure.path is None
    assert "private" not in str(state.records[_CANDIDATE])


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


def test_failed_junit_names_and_digests_survive_source_artifact_cleanup() -> None:
    """A later status read retains bounded test identity without source XML."""
    state = ValidationStateFake()
    artifact = (
        b"<testsuite failures='1' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_known_failure'>"
        b"<failure message='secret detail'/></testcase></testsuite>"
    )
    artifacts: dict[Path, bytes | OSError] = {Path("test-results-py311.xml"): artifact}
    dependencies, calls, _reads = _diagnostic_dependencies(
        state, artifacts=artifacts, returncode=7
    )

    run_report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    artifacts.clear()
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        run_report.failed_tests
        == status.failed_tests
        == ("tests.test_module::test_known_failure",)
    )
    assert status.failed_junit_digests == (hashlib.sha256(artifact).hexdigest(),)
    assert "secret detail" not in str(state.records[_CANDIDATE])


def test_failed_junit_names_remain_bounded_across_both_artifacts() -> None:
    state = ValidationStateFake()
    first_path = Path("test-results-py311.xml")
    second_path = Path("test-results-py314.xml")
    names = tuple(f"tests.test_module::test_failure_{index}" for index in range(16))
    first = (
        "<testsuite failures='16' errors='0'>"
        + "".join(
            f"<testcase classname='tests.test_module' name='test_failure_{index}'>"
            "<failure/></testcase>"
            for index in range(16)
        )
        + "</testsuite>"
    ).encode()
    second = (
        b"<testsuite failures='1' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_extra_failure'>"
        b"<failure/></testcase></testsuite>"
    )
    artifacts: dict[Path, bytes | OSError] = {}
    dependencies, calls, _reads = _diagnostic_dependencies(
        state, artifacts=artifacts, returncode=7
    )
    original_run = dependencies.run_command
    removals: list[Path] = []

    def run_with_artifacts(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> PortCommandResult:
        artifacts.update({first_path: first, second_path: second})
        return original_run(
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            environment=environment,
            current_directory=current_directory,
        )

    def remove_artifact(path: Path) -> None:
        removals.append(path)
        artifacts.pop(path, None)

    dependencies = replace(
        dependencies,
        run_command=run_with_artifacts,
        remove_junit_artifact=remove_artifact,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == status.state == "terminal-failure"
    assert report.failed_tests == status.failed_tests == names
    assert (
        report.failed_junit_digests
        == status.failed_junit_digests
        == (
            hashlib.sha256(first).hexdigest(),
            hashlib.sha256(second).hexdigest(),
        )
    )
    assert (
        report.diagnostic
        == status.diagnostic
        == (
            "git hook run pre-commit failed with exit code 7; "
            "JUnit py311 failures=16 errors=0; py314 failures=1 errors=0"
        )
    )
    assert removals == [first_path, second_path, first_path, second_path]
    assert artifacts == {}


def test_success_timing_is_durable_and_status_does_not_rerun_hook() -> None:
    """A yielded Judge handle can recover the same controller timing evidence."""
    state = ValidationStateFake()
    readings = iter((10.0, 11.25))
    dependencies, calls = _dependencies(state, monotonic_clock=lambda: next(readings))

    ran = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert ran.elapsed_seconds == status.elapsed_seconds == 1.25
    assert calls == [("git", "hook", "run", "pre-commit")]


def test_required_source_bound_capture_missing_after_successful_hook_is_unknown() -> (
    None
):
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    capture_calls: list[tuple[str, tuple[tuple[str, bytes, int], ...], str]] = []

    def missing_capture(
        candidate_tree: str,
        artifacts: tuple[tuple[str, bytes, int], ...],
        hook_output: str,
    ) -> None:
        capture_calls.append((candidate_tree, artifacts, hook_output))

    dependencies = replace(
        dependencies,
        read_artifact=lambda _path: (
            b"<testsuite tests='1' failures='0' errors='0'>"
            b"<testcase classname='tests.test_speed' name='test_existing' time='0.2'/>"
            b"</testsuite>"
        ),
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_timing_review=missing_capture,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert len(capture_calls) == 1
    assert capture_calls[0][0] == _CANDIDATE
    assert {environment for environment, _artifact, _count in capture_calls[0][1]} == {
        "py311",
        "py314",
    }
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )
    assert report.state == status.state == "terminal-success"
    assert report.timing_status == status.timing_status == "complete"
    assert getattr(report, "timing_comparison_status", None) == "unknown"
    assert getattr(status, "timing_comparison_reason", None) == "baseline_unavailable"
    record = state.records[_CANDIDATE]
    assert record["timing_comparison_status"] == "unknown"
    assert record["timing_comparison_reason"] == "baseline_unavailable"
    assert "timing_transcript_sha256" not in record
    assert len(report.junit_artifacts) == len(report.junit_timings) == 2
    assert all(item.timing_complete for item in report.junit_timings)


def test_focused_budget_exhaustion_has_distinct_diagnostic_and_keeps_snapshots() -> (
    None
):
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    snapshots: dict[str, tuple[tuple[str, bytes, int], ...]] = {}

    def capture_snapshots(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        snapshots[candidate_tree] = artifacts
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in artifacts
        )

    def exhausted(
        _candidate_tree: str,
        _artifacts: tuple[tuple[str, bytes, int], ...],
        _output: str,
    ) -> None:
        raise FocusedTimingCaptureError("focused timing budget exhausted")

    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_junit_artifacts=capture_snapshots,
        capture_timing_review=exhausted,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert (report.artifact_failure.stage, report.artifact_failure.reason) == (
        "timing_review",
        "focused_budget_exhausted",
    )
    assert snapshots[_CANDIDATE] == (
        ("py311", b"<testsuite tests='1' failures='0' errors='0'/>", 1),
        ("py314", b"<testsuite tests='1' failures='0' errors='0'/>", 1),
    )


def test_finding_budget_exhaustion_has_distinct_diagnostic_and_keeps_snapshots() -> (
    None
):
    from agent_work.timing_capture import FindingBudgetCaptureError

    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    snapshots: dict[str, tuple[tuple[str, bytes, int], ...]] = {}

    def capture_snapshots(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        snapshots[candidate_tree] = artifacts
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in artifacts
        )

    def exhausted(
        _candidate_tree: str,
        _artifacts: tuple[tuple[str, bytes, int], ...],
        _output: str,
    ) -> None:
        raise FindingBudgetCaptureError("finding budget exhausted")

    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_junit_artifacts=capture_snapshots,
        capture_timing_review=exhausted,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert (report.artifact_failure.stage, report.artifact_failure.reason) == (
        "timing_review",
        "finding_budget_exhausted",
    )
    assert snapshots[_CANDIDATE] == (
        ("py311", b"<testsuite tests='1' failures='0' errors='0'/>", 1),
        ("py314", b"<testsuite tests='1' failures='0' errors='0'/>", 1),
    )


class _BaselineHookCaptureError(TimingCaptureError):
    code = "baseline_hook_failed"


class _FocusedCommandCaptureError(TimingCaptureError):
    code = "focused_timing_command_failed"


@pytest.mark.parametrize(
    "capture_error",
    (
        _BaselineHookCaptureError("PRIVATE_TOKEN=secret /private/hook"),
        OSError("PRIVATE_TOKEN=secret /private/path"),
    ),
)
def test_baseline_capture_failure_preserves_candidate_junit_and_reports_unknown(
    capture_error: Exception,
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    junit = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_existing' time='0.2'/>"
        b"</testsuite>"
    )

    def capture(
        _tree: str, _artifacts: tuple[tuple[str, bytes, int], ...], _output: str
    ) -> None:
        raise capture_error

    dependencies = replace(
        dependencies,
        read_artifact=lambda _path: junit,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        expected_baseline_sha="c" * 40,
        capture_timing_review=capture,
    )
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == status.state == "terminal-success"
    assert report.timing_status == status.timing_status == "complete"
    assert getattr(report, "timing_comparison_status", None) == "unknown"
    assert getattr(status, "timing_comparison_reason", None) == "baseline_unavailable"
    assert {item.environment for item in report.junit_artifacts} == {"py311", "py314"}
    assert all(
        item.sha256 == hashlib.sha256(junit).hexdigest()
        for item in report.junit_artifacts
    )
    assert {item.environment for item in report.junit_timings} == {"py311", "py314"}
    assert all(item.timing_complete for item in report.junit_timings)
    assert all(item.over_100ms_cases for item in report.junit_timings)
    record = state.records[_CANDIDATE]
    assert record["timing_comparison_reason"] == "baseline_unavailable"
    assert "timing_transcript_sha256" not in record
    assert "PRIVATE_TOKEN" not in str(record)
    record["timing_comparison_reason"] = "PRIVATE_TOKEN=forged"
    with pytest.raises(JudgeValidationError):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)


@pytest.mark.parametrize(
    ("baseline_artifact", "comparison_unknown"),
    (
        ("missing", True),
        ("read_error", True),
        ("malformed", False),
        ("oversized", False),
    ),
)
def test_real_baseline_junit_read_preserves_candidate_validation_only_when_unavailable(
    tmp_path: Path, baseline_artifact: str, comparison_unknown: bool
) -> None:
    """Only an absent baseline result makes the paired comparison unknown."""
    state = ValidationStateFake()
    dependencies, candidate_hooks = _dependencies(state)
    candidate_junit = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_existing' time='0.2'/>"
        b"</testsuite>"
    )
    baseline_hooks: list[tuple[str, ...]] = []
    candidate_checkout = tmp_path / "candidate"
    base_sha, base_tree = "c" * 40, "d" * 40

    def run_baseline(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = cast(Path, kwargs["current_directory"])
        if command == ("git", "write-tree"):
            return CommandResult(
                0, base_tree if cwd.name == "baseline" else _CANDIDATE, ""
            )
        if command == ("git", "rev-parse", f"{base_sha}^{{tree}}"):
            return CommandResult(0, base_tree, "")
        if command == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, base_sha, "")
        if command == ("git", "rev-parse", "HEAD^{tree}"):
            return CommandResult(0, base_tree, "")
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "src/example.py\n", "")
        if command[:3] == ("git", "worktree", "add"):
            Path(command[4]).mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            baseline_hooks.append(command)
            if baseline_artifact != "missing":
                (cwd / "test-results-py311.xml").write_bytes(
                    b"<testsuite>"
                    if baseline_artifact == "malformed"
                    else candidate_junit
                )
            (cwd / "test-results-py314.xml").write_bytes(candidate_junit)
            return CommandResult(0, "", "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
            ("git", "status", "--porcelain"),
        } or command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected baseline command: {command}")

    def read_baseline(path: Path) -> bytes:
        if baseline_artifact == "read_error" and path.name == "test-results-py311.xml":
            raise OSError("PRIVATE_TOKEN=baseline-read")
        return path.read_bytes()

    def size_baseline(path: Path) -> int:
        if baseline_artifact == "oversized" and path.name == "test-results-py311.xml":
            return 2_000_000
        return path.stat().st_size

    def write_private(path: Path, data: bytes) -> None:
        path.write_bytes(data)

    capture_dependencies = TimingCaptureDependencies(
        run_command=run_baseline,
        monotonic_clock=lambda: 0.0,
        candidate_worktree=candidate_checkout,
        state_root=tmp_path / "timing-state",
        environment={},
        atomic_write=write_private,
        read_bytes=read_baseline,
        size=size_baseline,
    )
    dependencies = replace(
        dependencies,
        read_artifact=lambda _path: candidate_junit,
        timing_review_required=True,
        expected_baseline_tree=base_tree,
        expected_baseline_sha=base_sha,
        capture_timing_review=lambda tree, artifacts, _output: (
            capture_serial_timing_review(
                baseline_sha=base_sha,
                candidate_tree=tree,
                candidate_artifacts=artifacts,
                changed_paths=("src/example.py",),
                dependencies=capture_dependencies,
            )
        ),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert candidate_hooks == baseline_hooks == [("git", "hook", "run", "pre-commit")]
    if comparison_unknown:
        assert report.state == "terminal-success"
        assert report.timing_status == "complete"
        assert report.timing_comparison_status == "unknown"
        assert report.timing_comparison_reason == "baseline_unavailable"
        assert len(report.junit_artifacts) == len(report.junit_timings) == 2
        assert all(item.timing_complete for item in report.junit_timings)
        assert "timing_transcript_sha256" not in state.records[_CANDIDATE]
    else:
        assert report.state == "terminal-artifact-failure"
        assert report.artifact_failure is not None
        assert report.artifact_failure.stage == "timing_review"
        assert report.timing_comparison_status is None


def test_present_mismatched_paired_timing_proof_still_blocks_validation() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    transcript = _timing_transcript()
    runs = cast(dict[str, object], transcript["runs"])
    py311 = cast(dict[str, object], runs["py311"])
    candidate = cast(dict[str, object], py311["candidate"])
    candidate["sha256"] = "0" * 64
    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_timing_review=lambda _tree, _artifacts, _output: transcript,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-artifact-failure"
    assert report.artifact_failure is not None
    assert report.artifact_failure.stage == "timing_review"
    assert getattr(report, "timing_comparison_status", None) != "unknown"


def _failed_serial_timing_session(
    capture_error: Exception | None = None,
    snapshot_root: Path = Path("/snapshots"),
) -> tuple[
    ValidationStateFake,
    JudgeValidationDependencies,
    dict[Path, bytes],
    list[tuple[str, ...]],
]:
    state = ValidationStateFake()
    dependencies, commands = _dependencies(state)
    snapshots: dict[Path, bytes] = {}
    junit = b"<testsuite tests='1' failures='0' errors='0'/>"

    def capture(
        tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        paths = tuple(
            snapshot_root / tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in artifacts
        )
        snapshots.update(
            {
                path: contents
                for path, (_environment, contents, _count) in zip(paths, artifacts)
            }
        )
        return paths

    def read(path: Path) -> bytes | None:
        return (
            junit
            if path.name in {"test-results-py311.xml", "test-results-py314.xml"}
            and not path.is_absolute()
            else snapshots.get(path)
        )

    dependencies = replace(
        dependencies,
        read_artifact=read,
        capture_junit_artifacts=capture,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        expected_baseline_sha="c" * 40,
        capture_timing_review=lambda _tree, _artifacts, _output: (_ for _ in ()).throw(
            capture_error
            or _FocusedCommandCaptureError("PRIVATE_TOKEN=timing-secret /private/path")
        ),
    )
    result = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert result.state == "terminal-artifact-failure"
    return state, dependencies, snapshots, commands


@pytest.mark.parametrize(
    ("capture_error", "expected_code"),
    [
        (
            _FocusedCommandCaptureError("PRIVATE_TOKEN=secret /private/focused"),
            "focused_timing_command_failed",
        ),
    ],
)
def test_timing_capture_failure_has_safe_code_and_preserves_retry_evidence(
    capture_error: Exception, expected_code: str
) -> None:
    state, dependencies, snapshots, commands = _failed_serial_timing_session(
        capture_error
    )
    record = state.records[_CANDIDATE]
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert commands == [("git", "hook", "run", "pre-commit")]
    assert (
        record["timing_failure_code"]
        == getattr(status, "timing_failure_code", None)
        == expected_code
    )
    assert record["candidate_tree"] == status.candidate_tree == _CANDIDATE
    assert record["requested_baseline_tree"] == _OTHER_CANDIDATE
    assert record["requested_baseline_sha"] == "c" * 40
    assert record["returncode"] == status.returncode == 0
    artifacts = cast(list[dict[str, object]], record["junit_artifacts"])
    assert {item["environment"] for item in artifacts} == {"py311", "py314"}
    assert all(Path(cast(str, item["path"])) in snapshots for item in artifacts)
    assert "PRIVATE_TOKEN" not in str(record)
    assert "/private/path" not in str(record)
    assert "/snapshots" not in str(status)


def test_status_rejects_forged_timing_failure_code() -> None:
    state, dependencies, _snapshots, _commands = _failed_serial_timing_session(
        _FocusedCommandCaptureError("PRIVATE_TOKEN=secret /private/focused")
    )
    state.records[_CANDIDATE]["timing_failure_code"] = "PRIVATE_TOKEN=/private/path"

    with pytest.raises(JudgeValidationError):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)


def test_timing_failure_retains_bounded_detail_and_both_snapshot_identities() -> None:
    state, dependencies, snapshots, commands = _failed_serial_timing_session()
    record = state.records[_CANDIDATE]
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert commands == [("git", "hook", "run", "pre-commit")]
    assert status.artifact_failure is not None
    assert (status.artifact_failure.stage, status.artifact_failure.reason) == (
        "timing_review",
        "invalid_timing",
    )
    assert record["requested_baseline_sha"] == "c" * 40
    assert record["requested_baseline_tree"] == _OTHER_CANDIDATE
    artifacts = cast(list[dict[str, object]], record["junit_artifacts"])
    assert {item["environment"] for item in artifacts} == {
        "py311",
        "py314",
    }
    for item in artifacts:
        assert (
            item["sha256"]
            == hashlib.sha256(snapshots[Path(cast(str, item["path"]))]).hexdigest()
        )
        assert item["test_count"] == 1
    assert "PRIVATE_TOKEN" not in str(record)
    assert "/private/path" not in str(status)
    assert "/snapshots" not in str(status)


def test_timing_retry_uses_saved_junit_without_rerunning_hook_or_preflight() -> None:
    state, dependencies, snapshots, commands = _failed_serial_timing_session()
    original = json.loads(json.dumps(state.records[_CANDIDATE]))
    retry_records: dict[str, dict[str, object]] = {}
    captured: list[tuple[tuple[str, bytes, int], ...]] = []
    transcript = _timing_transcript() | {"baseline_sha": "c" * 40}

    def capture(
        _tree: str, artifacts: tuple[tuple[str, bytes, int], ...], _output: str
    ) -> dict[str, object]:
        captured.append(artifacts)
        return transcript

    retry_dependencies = replace(
        dependencies,
        run_license_preflight=lambda _tree: (_ for _ in ()).throw(
            AssertionError("preflight reran")
        ),
        run_command=lambda _command, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hook reran")
        ),
        capture_timing_review=capture,
        read_timing_transcript=lambda _tree: transcript,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=lambda tree, record: retry_records.update(
            {tree: record}
        ),
    )
    preview = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=retry_dependencies
    )
    assert preview.action == "retry_preview"
    assert captured == []
    assert retry_records == {}
    retried = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    repeated = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=retry_dependencies
    )

    assert preview.action == "retry_preview"
    assert retried.action == "retried"
    assert retried.state == repeated.state == status.state == "terminal-success"
    assert repeated.action == "existing_retry"
    assert len(captured) == 1
    assert captured[0] == tuple(
        (
            environment,
            snapshots[
                Path("/snapshots") / _CANDIDATE / f"test-results-{environment}.xml"
            ],
            1,
        )
        for environment in ("py311", "py314")
    )
    assert commands == [("git", "hook", "run", "pre-commit")]
    assert state.records[_CANDIDATE] == original
    assert (
        retry_records[_CANDIDATE]["original_sha256"]
        == hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_timing_retry_succeeds_without_prior_transcript() -> None:
    state, dependencies, _snapshots, commands = _failed_serial_timing_session()
    original = json.loads(json.dumps(state.records[_CANDIDATE]))
    retry_records: dict[str, dict[str, object]] = {}
    writes: list[str] = []
    captures: list[tuple[tuple[str, bytes, int], ...]] = []
    transcript = _timing_transcript() | {"baseline_sha": "c" * 40}

    def write_retry(tree: str, record: dict[str, object]) -> None:
        retry_records[tree] = record
        writes.append(cast(str, record["state"]))

    retry_dependencies = replace(
        dependencies,
        read_timing_transcript=lambda _tree: None,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=write_retry,
        capture_timing_review=lambda _tree, artifacts, _output: (
            captures.append(artifacts) or transcript
        ),
        run_command=lambda _command, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hook reran")
        ),
    )

    retried = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )

    assert retried.action == "retried"
    assert retried.state == "terminal-success"
    assert retry_records[_CANDIDATE]["state"] == "terminal-success"
    assert writes == ["running", "terminal-success"]
    assert len(captures) == 1
    assert state.records[_CANDIDATE] == original
    assert commands == [("git", "hook", "run", "pre-commit")]


def test_timing_retry_persists_running_before_capture_and_never_recaptures_after_interrupt() -> (
    None
):
    state, dependencies, _snapshots, commands = _failed_serial_timing_session()
    original = json.loads(json.dumps(state.records[_CANDIDATE]))
    original_digest = hashlib.sha256(
        json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    retry_records: dict[str, dict[str, object]] = {}
    writes: list[dict[str, object]] = []
    captures: list[object] = []

    def write_retry(tree: str, record: dict[str, object]) -> None:
        assert tree == _CANDIDATE
        retry_records[tree] = record
        writes.append(json.loads(json.dumps(record)))

    def interrupt_capture(
        _tree: str, _artifacts: tuple[tuple[str, bytes, int], ...], _output: str
    ) -> dict[str, object]:
        captures.append(True)
        assert len(writes) == 1
        assert writes[0]["state"] == "running"
        assert writes[0]["original_sha256"] == original_digest
        raise KeyboardInterrupt

    retry_dependencies = replace(
        dependencies,
        read_timing_transcript=lambda _tree: None,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=write_retry,
        capture_timing_review=interrupt_capture,
        run_command=lambda _command, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hook reran")
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
        )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=retry_dependencies
    )
    repeated = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )

    assert status.state == repeated.state == "running"
    assert repeated.action == "existing_retry"
    assert len(captures) == len(writes) == 1
    assert state.records[_CANDIDATE] == original
    assert commands == [("git", "hook", "run", "pre-commit")]


@pytest.mark.parametrize(
    "damage",
    (
        "missing",
        "tampered",
        "malformed",
        "staged_tree",
        "source_tree",
        "baseline_sha",
        "baseline_tree",
        "collision",
        "existing_transcript",
        "forged_snapshot_root",
    ),
)
def test_timing_retry_rejects_drift_or_tampered_evidence_without_hook(
    damage: str,
) -> None:
    state, dependencies, snapshots, commands = _failed_serial_timing_session()
    assert len(cast(list[object], state.records[_CANDIDATE]["junit_artifacts"])) == 2
    retry_records: dict[str, dict[str, object]] = {}
    captures: list[object] = []
    changes: dict[str, object] = {}
    if damage == "missing":
        snapshots.pop(next(iter(snapshots)))
    elif damage == "tampered":
        snapshots[next(iter(snapshots))] = b"<testsuite tests='2'/>"
    elif damage == "malformed":
        snapshots[next(iter(snapshots))] = b"<testsuite"
    elif damage == "staged_tree":
        changes["current_staged_tree"] = lambda: _OTHER_CANDIDATE
    elif damage == "source_tree":
        changes["verify_candidate_source"] = lambda _tree: False
    elif damage == "baseline_sha":
        changes["expected_baseline_sha"] = "d" * 40
    elif damage == "baseline_tree":
        changes["expected_baseline_tree"] = "d" * 40
    elif damage == "existing_transcript":
        changes["read_timing_transcript"] = lambda _tree: {
            **_timing_transcript(),
            "outcome": "report_only",
        }
    elif damage == "forged_snapshot_root":
        artifacts = cast(
            list[dict[str, object]], state.records[_CANDIDATE]["junit_artifacts"]
        )
        artifacts[0]["path"] = str(
            Path("/forged") / _CANDIDATE / "test-results-py311.xml"
        )
        changes["read_artifact"] = lambda _path: (_ for _ in ()).throw(
            AssertionError("forged snapshot was read")
        )
    else:
        retry_records[_CANDIDATE] = {
            "candidate_tree": _CANDIDATE,
            "state": "running",
            "original_sha256": "0" * 64,
        }
    retry_dependencies = replace(
        dependencies,
        read_retry_record=retry_records.get,
        atomic_write_retry_record=lambda tree, record: retry_records.update(
            {tree: record}
        ),
        capture_timing_review=lambda _tree, _artifacts, _output: (
            captures.append(True) or _timing_transcript()
        ),
        **changes,
    )

    with pytest.raises(JudgeValidationError):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
        )
    assert commands == [("git", "hook", "run", "pre-commit")]
    assert captures == []


def test_timing_retry_rejects_symlink_snapshot_before_read_or_capture(
    tmp_path: Path,
) -> None:
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    _state, dependencies, snapshots, commands = _failed_serial_timing_session(
        snapshot_root=snapshot_root
    )
    outside = tmp_path / "outside.xml"
    outside.write_bytes(next(iter(snapshots.values())))
    snapshot = next(iter(snapshots))
    snapshot.parent.mkdir()
    snapshot.symlink_to(outside)
    reads: list[Path] = []
    captures: list[object] = []

    def read_saved_snapshot(path: Path) -> bytes | None:
        reads.append(path)
        raise AssertionError("saved snapshot symlink was followed")

    retry_dependencies = replace(
        dependencies,
        expected_snapshot_path=lambda tree, environment: (
            snapshot_root / tree / f"test-results-{environment}.xml"
        ),
        read_saved_snapshot=read_saved_snapshot,
        capture_timing_review=lambda _tree, _artifacts, _output: (
            captures.append(True) or _timing_transcript()
        ),
    )

    with pytest.raises(JudgeValidationError, match="snapshot"):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
        )
    assert reads == []
    assert captures == []
    assert commands == [("git", "hook", "run", "pre-commit")]


def test_timing_retry_capture_failure_is_bounded_and_status_reports_retry() -> None:
    _state, dependencies, _snapshots, commands = _failed_serial_timing_session()
    retry_records: dict[str, dict[str, object]] = {}
    writes: list[str] = []

    def write_retry(tree: str, record: dict[str, object]) -> None:
        retry_records[tree] = record
        writes.append(cast(str, record["state"]))

    retry_dependencies = replace(
        dependencies,
        capture_timing_review=lambda _tree, _artifacts, _output: (_ for _ in ()).throw(
            FocusedTimingCaptureError("PRIVATE_TOKEN=retry-secret /private/path")
        ),
        read_retry_record=retry_records.get,
        atomic_write_retry_record=write_retry,
    )
    retried = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=retry_dependencies
    )
    repeated = validation.retry_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=retry_dependencies
    )

    assert retried.action == "retried"
    assert status.state == repeated.state == "terminal-artifact-failure"
    assert repeated.action == "existing_retry"
    assert writes == ["running", "terminal-artifact-failure"]
    assert status.artifact_failure is not None
    assert (status.artifact_failure.stage, status.artifact_failure.reason) == (
        "timing_review",
        "focused_budget_exhausted",
    )
    assert "PRIVATE_TOKEN" not in str(retry_records)
    assert "/private/path" not in str(status)
    assert commands == [("git", "hook", "run", "pre-commit")]


def test_status_rejects_missing_required_timing_transcript_for_successful_tree() -> (
    None
):
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    original = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert original.state == "terminal-success"
    status_dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        read_timing_transcript=lambda _candidate_tree: None,
    )

    with pytest.raises(JudgeValidationError):
        observe_judge_validation(
            candidate_tree=_CANDIDATE, dependencies=status_dependencies
        )

    assert calls == [("git", "hook", "run", "pre-commit")]


def test_required_timing_transcript_replays_only_for_unchanged_source_pair() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    transcript = _timing_transcript()
    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_timing_review=lambda _tree, _artifacts, _output: transcript,
        read_timing_transcript=lambda _tree: transcript,
    )

    ran = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert ran.state == status.state == "terminal-success"
    assert calls == [("git", "hook", "run", "pre-commit")]
    assert state.records[_CANDIDATE]["timing_baseline_tree"] == _OTHER_CANDIDATE
    assert isinstance(state.records[_CANDIDATE]["timing_transcript_sha256"], str)

    transcript["outcome"] = "report_only"
    with pytest.raises(JudgeValidationError):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)


@pytest.mark.parametrize("action", ("status", "replayed_run"))
def test_persisted_serial_success_cannot_bypass_proof_without_baseline_flag(
    action: str,
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    transcript = _timing_transcript()
    serial = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_timing_review=lambda _tree, _artifacts, _output: transcript,
        read_timing_transcript=lambda _tree: transcript,
    )
    assert (
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=serial
        ).state
        == "terminal-success"
    )
    omitted_flag = replace(
        serial,
        timing_review_required=False,
        read_timing_transcript=lambda _tree: None,
    )

    with pytest.raises(JudgeValidationError, match="timing"):
        if action == "status":
            observe_judge_validation(
                candidate_tree=_CANDIDATE, dependencies=omitted_flag
            )
        else:
            run_judge_validation(
                candidate_tree=_CANDIDATE, apply=True, dependencies=omitted_flag
            )

    assert calls == [("git", "hook", "run", "pre-commit")]


def test_serial_candidate_slow_ids_match_full_inventory_ids() -> None:
    artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_case[plain]' time='0.2'/>"
        b"<testcase classname='tests.test_speed' name='test_case[unsafe / value]' time='0.3'/>"
        b"</testsuite>"
    )
    cases, _selectors = parse_full_junit_records(artifact)
    summary = validation._junit_timing_summary("py311", artifact, serial=True)

    assert summary is not None and summary.timing_complete
    assert {case.name for case in summary.over_100ms_cases} == set(cases)
    assert all(name.startswith("tests/test_speed.py::") for name in cases)


def test_required_timing_review_passes_explicit_seed_to_candidate_hook() -> None:
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    environments: list[object] = []
    transcript = _timing_transcript()

    def run(_command: tuple[str, ...], **kwargs: object) -> CommandResult:
        environments.append(kwargs.get("environment"))
        return CommandResult(0, "", "")

    dependencies = replace(
        dependencies,
        run_command=run,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        hook_environment={"PYTEST_ADDOPTS": "--randomly-seed=12345"},
        capture_timing_review=lambda _tree, _artifacts, _output: transcript,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "terminal-success"
    assert environments == [{"PYTEST_ADDOPTS": "--randomly-seed=12345"}]


def test_existing_success_rechecks_required_private_timing_transcript() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)
    transcript = _timing_transcript()
    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        capture_timing_review=lambda _tree, _artifacts, _output: transcript,
        read_timing_transcript=lambda _tree: transcript,
    )
    run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    damaged = replace(dependencies, read_timing_transcript=lambda _tree: None)

    with pytest.raises(JudgeValidationError):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=damaged
        )

    assert calls == [("git", "hook", "run", "pre-commit")]


def test_pre_hook_failure_retry_rejects_changed_timing_baseline() -> None:
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    dependencies = replace(
        dependencies,
        timing_review_required=True,
        expected_baseline_tree=_OTHER_CANDIDATE,
        remove_junit_artifact=lambda _path: (_ for _ in ()).throw(OSError("failed")),
    )
    failed = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert failed.state == "terminal-artifact-failure"

    with pytest.raises(JudgeValidationError):
        validation.retry_judge_validation(
            candidate_tree=_CANDIDATE,
            apply=True,
            dependencies=replace(dependencies, expected_baseline_tree="c" * 40),
        )


def test_success_junit_timing_summary_is_bounded_and_durable() -> None:
    """Nonfunctional review can read total and slowest tests after source cleanup."""
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    artifact = (
        b"<testsuite tests='3' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_fast' time='0.5'/>"
        b"<testcase classname='tests.test_module' name='test_slow' time='3.0'/>"
        b"<testcase name='private-secret' time='0.25'/></testsuite>"
    )
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert [item.total_seconds for item in status.junit_timings] == [3.75, 3.75]
    assert status.junit_timings[0].slowest_cases[0].name == (
        "tests.test_module::test_slow"
    )
    assert "private-secret" not in str(state.records[_CANDIDATE])


def test_junit_timing_summary_retains_every_case_strictly_over_100ms() -> None:
    """Review sees a qualifying case even when it falls below the top three."""
    artifact = (
        b"<testsuite tests='6' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_one' time='3'/>"
        b"<testcase classname='tests.test_module' name='test_two' time='2'/>"
        b"<testcase classname='tests.test_module' name='test_three' time='1'/>"
        b"<testcase classname='tests.test_module' name='test_four' time='0.100001'/>"
        b"<testcase classname='tests.test_module' name='test_boundary' time='0.100'/>"
        b"<testcase classname='tests.test_module' name='test_fast' time='0.099999'/>"
        b"</testsuite>"
    )

    summary = validation._junit_timing_summary("py311", artifact)

    assert summary is not None
    assert summary.timing_complete is True
    assert [case.name for case in summary.over_100ms_cases] == [
        "tests.test_module::test_one",
        "tests.test_module::test_two",
        "tests.test_module::test_three",
        "tests.test_module::test_four",
    ]
    assert summary.over_100ms_cases[-1].seconds > 0.100
    assert len(summary.slowest_cases) == 3


@pytest.mark.parametrize(
    "case",
    [
        b"<testcase classname='/home/private' name='test_secret' time='1.2'/>",
        b"<testcase classname='tests.test_module' name='test_unknown' time='bogus'/>",
        b"<testcase classname='tests.test_module' name='test_unbounded' time='1000001'/>",
    ],
)
def test_junit_timing_summary_marks_relevant_unusable_case_incomplete(
    case: bytes,
) -> None:
    """Omitted or unsafe timing cannot be represented as a complete review list."""
    artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_known' time='1'/>"
        + case
        + b"</testsuite>"
    )

    summary = validation._junit_timing_summary("py311", artifact)

    assert summary is not None
    assert summary.timing_complete is False
    assert [item.name for item in summary.over_100ms_cases] == [
        "tests.test_module::test_known"
    ]
    assert b"/home/private" not in str(summary).encode()


def test_junit_timing_summary_marks_case_list_overflow_incomplete() -> None:
    """A bounded report states when it cannot enumerate every qualifying test."""
    artifact = (
        b"<testsuite tests='1025' failures='0' errors='0'>"
        + b"".join(
            f"<testcase classname='tests.test_module' name='test_{index}' time='1'/>".encode()
            for index in range(1025)
        )
        + b"</testsuite>"
    )

    summary = validation._junit_timing_summary("py311", artifact)

    assert summary is not None
    assert summary.timing_complete is False
    assert len(summary.over_100ms_cases) < 1025
    assert all(
        validation.is_safe_junit_test_name(item.name)
        for item in summary.over_100ms_cases
    )


def test_junit_timing_summary_uses_opaque_ids_for_private_parameter_values() -> None:
    """Ordinary parameter values keep timing complete without exposing inputs."""
    artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b'<testcase classname="tests.test_module" '
        b"name='test_case[{&quot;path&quot;:&quot;/home/private&quot;}]' time='0.101'/>"
        b'<testcase classname="tests.test_module" '
        b"name='test_case[{&quot;path&quot;:&quot;/home/other&quot;}]' time='0.102'/>"
        b"</testsuite>"
    )

    summary = validation._junit_timing_summary("py311", artifact)

    assert summary is not None
    assert summary.timing_complete is True
    names = [case.name for case in summary.over_100ms_cases]
    assert len(set(names)) == 2
    assert all(name.startswith("tests.test_module::test_case[param_") for name in names)
    assert all(validation.is_safe_junit_test_name(name) for name in names)
    assert "/home/" not in str(summary)


def test_junit_timing_summary_marks_declared_case_mismatch_incomplete() -> None:
    """A declared case without a testcase element cannot be timed completely."""
    artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_present' time='0.2'/>"
        b"</testsuite>"
    )

    summary = validation._junit_timing_summary("py311", artifact)

    assert summary is not None
    assert summary.timing_complete is False
    assert [case.name for case in summary.over_100ms_cases] == [
        "tests.test_module::test_present"
    ]


def test_junit_timing_completion_and_cases_survive_durable_status() -> None:
    """The reviewer can recover the complete list after validation exits."""
    artifact = (
        b"<testsuite tests='4' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_one' time='4'/>"
        b"<testcase classname='tests.test_module' name='test_two' time='3'/>"
        b"<testcase classname='tests.test_module' name='test_three' time='2'/>"
        b"<testcase classname='tests.test_module' name='test_four' time='1'/>"
        b"</testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert len(status.junit_timings) == 2
    assert all(item.timing_complete for item in status.junit_timings)
    assert all(len(item.over_100ms_cases) == 4 for item in status.junit_timings)
    persisted_timings = cast(
        list[dict[str, object]], state.records[_CANDIDATE]["junit_timings"]
    )
    assert all(
        isinstance(cases := item.get("over_100ms_cases"), list)
        and len(cases) == 4
        and item.get("timing_complete") is True
        for item in persisted_timings
    )


@pytest.mark.parametrize(
    ("safe_name", "unsafe_name"),
    [
        ("test_case[linux-x86_64]", "test_case[/home/private]"),
        ("tests.test_module::test_case[py3.11]", "test_case[../private]"),
        ("tests/test_module.py::test_case[case_2]", "test_case[foo/../bar]"),
        ("test_case[42]", "test_case[.hidden]"),
        ("test_case[alpha]", "test_case[foo bar]"),
        ("test_case[alpha]", "test_case[foo\nbar]"),
        ("test_case[alpha]", "test_case[foo\\bar]"),
        ("test_case[alpha]", "test_case[foo]suffix"),
        ("test_case[alpha]", "test_case[[nested]]"),
        ("test_case[alpha]", "test_case[unclosed"),
        ("test_case[alpha]", "test_case[]"),
        ("test_case[alpha]", "test_case[" + "x" * 256 + "]"),
        ("test_case[alpha]", "tests/../private::test_case[alpha]"),
        ("test_case[alpha]", "/private::test_case[alpha]"),
    ],
)
def test_parameterized_junit_name_accepts_safe_id_and_rejects_unsafe_variant(
    safe_name: str, unsafe_name: str
) -> None:
    assert validation.is_safe_junit_test_name(safe_name)
    assert not validation.is_safe_junit_test_name(unsafe_name)


@given(
    first=st.sampled_from(tuple(_PARAM_ID_FIRST)),
    rest=st.text(alphabet=_PARAM_ID_REST, max_size=148),
    unsafe=st.sampled_from(("/", "\\", " ", "\t", "\n", "[", "]")),
)
def test_generated_parameter_id_safety_invariant(
    first: str, rest: str, unsafe: str
) -> None:
    safe_id = first + rest
    assert validation.is_safe_junit_test_name(f"test_case[{safe_id}]")
    assert not validation.is_safe_junit_test_name(f"test_case[{first}{unsafe}{rest}]")
    assert not validation.is_safe_junit_test_name(f"test_case[.{safe_id}]")


def test_parameterized_junit_failure_evidence_retains_only_safe_names() -> None:
    artifact = (
        b"<testsuite tests='4' failures='4' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_case[linux-x86_64]'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_case[../private]'>"
        b"<failure/></testcase>"
        b"<testcase classname='/private' name='test_case[alpha]'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_case[unclosed'>"
        b"<failure/></testcase></testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls, _reads = _diagnostic_dependencies(
        state, artifacts={Path("test-results-py311.xml"): artifact}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.failed_tests == ("tests.test_module::test_case[linux-x86_64]",)
    assert state.records[_CANDIDATE]["failed_tests"] == [
        "tests.test_module::test_case[linux-x86_64]"
    ]


def test_parameterized_junit_timing_evidence_hides_unsafe_parameter_values() -> None:
    artifact = (
        b"<testsuite tests='4' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_case[linux-x86_64]' time='4'/>"
        b"<testcase classname='tests.test_module' name='test_case[../private]' time='3'/>"
        b"<testcase classname='/private' name='test_case[alpha]' time='2'/>"
        b"<testcase classname='tests.test_module' name='test_case[unclosed' time='1'/>"
        b"</testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.junit_timings[0].total_seconds == 10.0
    names = [case.name for case in report.junit_timings[0].slowest_cases]
    assert names[0] == "tests.test_module::test_case[linux-x86_64]"
    assert names[1].startswith("tests.test_module::test_case[param_")
    assert all(validation.is_safe_junit_test_name(name) for name in names)
    assert "../private" not in str(report.junit_timings)


def test_junit_evidence_drops_absolute_and_traversal_classnames() -> None:
    """Raw JUnit classnames cannot reveal a controller-private path."""
    artifact = (
        b"<testsuite tests='3' failures='3' errors='0'>"
        b"<testcase classname='/home/private/state' name='test_secret' time='3'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests/../private' name='test_secret' time='2'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_safe' time='1'>"
        b"<failure/></testcase></testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls, _reads = _diagnostic_dependencies(
        state,
        artifacts={Path("test-results-py311.xml"): artifact},
        returncode=1,
    )
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert report.failed_tests == ("tests.test_module::test_safe",)
    assert "/home/private" not in str(state.records[_CANDIDATE])

    success_state = ValidationStateFake()
    success_dependencies, _success_calls = _dependencies(success_state)
    success_dependencies = replace(
        success_dependencies, read_artifact=lambda _path: artifact
    )
    success = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=success_dependencies
    )
    assert success.junit_timings[0].total_seconds == 6.0
    assert [case.name for case in success.junit_timings[0].slowest_cases] == [
        "tests.test_module::test_safe"
    ]


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
    assert report.hook_failures == ()


def test_terminal_failure_projects_only_allowlisted_hook_evidence_in_command_order() -> (
    None
):
    state = ValidationStateFake()
    dependencies, calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
readability-check.....................................................Failed
- hook id: readability-check
docs/guide.md: grade 9.2
ruff..................................................................Failed
- hook id: ruff
src/agent_work/example.py:7:3: F401 unused import
ty....................................................................Failed
- hook id: ty
error[invalid-assignment]: private source text
--> src/agent_work/example.py:8:2
tox...................................................................Failed
- hook id: tox
""",
        artifacts={
            Path("test-results-py311.xml"): b"<testsuite failures='2' errors='1'/>",
            Path("test-results-py314.xml"): b"<testsuite failures='4' errors='3'/>",
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        {
            "hook_id": "readability-check",
            "diagnostic_lines": ["docs/guide.md: grade 9.2"],
        },
        {
            "hook_id": "ruff",
            "diagnostic_lines": ["src/agent_work/example.py:7:3: F401"],
        },
        {
            "hook_id": "ty",
            "diagnostic_lines": ["src/agent_work/example.py:8:2: invalid-assignment"],
        },
        {
            "hook_id": "tox",
            "diagnostic_lines": [
                "JUnit py311 failures=2 errors=1; py314 failures=4 errors=3"
            ],
        },
    )
    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        tuple(
            {"hook_id": item.hook_id, "diagnostic_lines": list(item.diagnostic_lines)}
            for item in report.hook_failures
        )
        == expected
    )
    assert state.records[_CANDIDATE]["hook_failures"] == list(expected)


def test_readability_hook_projects_highest_safe_grades_before_lower_rows() -> None:
    """Readability evidence makes the highest safe grades visible first."""
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
readability-check.....................................................Failed
- hook id: readability-check
docs/lower.md: grade 7.8
docs/higher.md: grade 8.1
docs/tie-b.md: grade 8.0
docs/tie-a.md: grade 8.0
""",
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.hook_failures[0].diagnostic_lines == (
        "docs/higher.md: grade 8.1",
        "docs/tie-a.md: grade 8.0",
        "docs/tie-b.md: grade 8.0",
        "docs/lower.md: grade 7.8",
    )


def test_terminal_failure_drops_hostile_output_and_uses_fixed_generic_hook_detail() -> (
    None
):
    state = ValidationStateFake()
    hostile = (
        "\x1b[31mAPI_TOKEN=do-not-store\x1b[0m\n"
        "PASSWORD=do-not-store\n"
        "enter credential now> \n"
        "/absolute/private.py:1:1: F401 source text\n"
        "../escape.py:1:1: F401 source text\n"
        "ruff..................................................................Failed\n"
        "- hook id: ruff\n"
        "private_source.py:1:1: F401 arbitrary source and secret-like text\n"
        "shellcheck............................................................Failed\n"
        "- hook id: shellcheck\n"
        "free-form message with private source context and a caret ^^^\n"
        "spoof.................................................................Failed\n"
        "- hook id: ../../not-safe\n"
        + "oversized..............................................................Failed\n"
        + "- hook id: "
        + ("x" * 129)
        + "\n"
    )
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, stdout=hostile, stderr=hostile, artifacts={}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert tuple(
        {"hook_id": item.hook_id, "diagnostic_lines": list(item.diagnostic_lines)}
        for item in report.hook_failures
    ) == (
        {
            "hook_id": "ruff",
            "diagnostic_lines": ["private_source.py:1:1: F401"],
        },
        {
            "hook_id": "shellcheck",
            "diagnostic_lines": ["No safe structured diagnostic available."],
        },
    )
    persisted = str(state.records[_CANDIDATE])
    for forbidden in (
        "API_TOKEN",
        "PASSWORD",
        "credential",
        "do-not-store",
        "private source",
        "free-form message",
        "\x1b",
        "../../not-safe",
    ):
        assert forbidden not in persisted


def test_unsupported_hook_never_projects_ruff_shaped_output() -> None:
    """Unsupported hooks retain only their ID and the fixed fallback."""
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
unknown-hook..........................................................Failed
- hook id: unknown-hook
safe/path.py:1:2: E101 private message
""",
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.hook_failures[0].hook_id == "unknown-hook"
    assert report.hook_failures[0].diagnostic_lines == (
        "No safe structured diagnostic available.",
    )


def test_overlong_recognized_hook_line_falls_back_without_stranding_record() -> None:
    """An overlong canonical detail cannot leave a durable session running."""
    state = ValidationStateFake()
    long_path = f"pkg/{'a' * 250}.py"
    dependencies, calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout=(
            "ruff..................................................................Failed\n"
            "- hook id: ruff\n"
            f"{long_path}:1:2: E101 private message\n"
        ),
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-failure"
    assert report.hook_failures[0].diagnostic_lines == (
        "No safe structured diagnostic available.",
    )
    assert state.records[_CANDIDATE]["state"] == "terminal-failure"


def test_hook_failure_evidence_keeps_only_the_earliest_bounded_safe_entries() -> None:
    state = ValidationStateFake()
    safe_path = f"pkg/{'a' * 231}.py"
    ruff_frame = "\n".join(
        (
            "ruff..................................................................Failed",
            "- hook id: ruff",
            *(
                f"{safe_path}:{line}:2: E{100 + line} ignored source text"
                for line in range(1, 7)
            ),
        )
    )
    unsupported_frames = "\n".join(
        f"hook-{index:02d}.......................................................Failed\n"
        f"- hook id: hook-{index:02d}\n"
        f"{safe_path}:1:2: E101 private source text\n"
        "free-form private message"
        for index in range(17)
    )
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, stdout=f"{ruff_frame}\n{unsupported_frames}", artifacts={}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert len(report.hook_failures) == 16
    assert [item.hook_id for item in report.hook_failures] == [
        "ruff",
        *(f"hook-{index:02d}" for index in range(15)),
    ]
    assert len(report.hook_failures[0].diagnostic_lines) == 4
    assert all(
        item.diagnostic_lines == ("No safe structured diagnostic available.",)
        for item in report.hook_failures[1:]
    )
    assert all(
        item.hook_id.isascii() and len(item.hook_id.encode()) <= 128
        for item in report.hook_failures
    )
    assert all(
        line.isprintable() and len(line.encode("utf-8")) <= 256
        for item in report.hook_failures
        for line in item.diagnostic_lines
    )
    serialized = json.dumps(
        state.records[_CANDIDATE]["hook_failures"], separators=(",", ":")
    ).encode("utf-8")
    assert len(serialized) <= 4096
    assert b"private source" not in serialized
    assert b"free-form" not in serialized


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
        "schema_version": 2,
        "state": expected_state,
        "returncode": returncode,
        "elapsed_seconds": 0.0,
    }
    if returncode != 0:
        expected_record["diagnostic"] = (
            f"git hook run pre-commit failed with exit code {returncode}; "
            "no failing JUnit summary available"
        )
    else:
        expected_record["junit_artifacts"] = [
            {
                "environment": environment,
                "path": f"/snapshots/{_CANDIDATE}/test-results-{environment}.xml",
                "sha256": hashlib.sha256(
                    b"<testsuite tests='1' failures='0' errors='0'/>"
                ).hexdigest(),
                "test_count": 1,
            }
            for environment in ("py311", "py314")
        ]
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
    assert report.hook_failures == ()
    assert report.junit_artifacts == ()
    assert calls == []
    assert state.writes == []


def test_successful_apply_captures_exact_supported_junit_bytes_and_status_reuses_evidence(
    tmp_path: Path,
) -> None:
    """A completed candidate retains immutable evidence without rereading checkout XML."""
    state = ValidationStateFake()
    artifact_bytes = {
        Path(
            "test-results-py311.xml"
        ): b"<testsuites><testsuite tests='3' failures='0' errors='0'/></testsuites>",
        Path(
            "test-results-py314.xml"
        ): b"<testsuite tests='5' failures='0' errors='0'/>",
    }
    artifact_reads: list[Path] = []
    captures: list[tuple[str, tuple[tuple[str, bytes, int], ...]]] = []

    def read_artifact(path: Path) -> bytes:
        artifact_reads.append(path)
        return artifact_bytes[path]

    def capture_junit_artifacts(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        captures.append((candidate_tree, artifacts))
        return tuple(
            tmp_path / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _test_count in artifacts
        )

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: CommandResult(0, "", ""),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        capture_junit_artifacts=capture_junit_artifacts,
    )
    dependencies = _passing_gate(dependencies)

    applied = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert artifact_reads == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert captures == [
        (
            _CANDIDATE,
            (
                ("py311", artifact_bytes[Path("test-results-py311.xml")], 3),
                ("py314", artifact_bytes[Path("test-results-py314.xml")], 5),
            ),
        )
    ]
    assert tuple(
        (item.environment, item.path, item.sha256, item.test_count)
        for item in applied.junit_artifacts
    ) == (
        (
            "py311",
            tmp_path / _CANDIDATE / "test-results-py311.xml",
            hashlib.sha256(artifact_bytes[Path("test-results-py311.xml")]).hexdigest(),
            3,
        ),
        (
            "py314",
            tmp_path / _CANDIDATE / "test-results-py314.xml",
            hashlib.sha256(artifact_bytes[Path("test-results-py314.xml")]).hexdigest(),
            5,
        ),
    )

    status_dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: pytest.fail("status must not run hook"),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: pytest.fail("status must not read mutable XML"),
        capture_junit_artifacts=lambda *_args: pytest.fail("status must not capture"),
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=status_dependencies
    )

    assert status.junit_artifacts == applied.junit_artifacts


@pytest.mark.parametrize(
    "junit_artifacts",
    (
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            }
        ],
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py312",
                "path": f"/snapshots/{_CANDIDATE}/py312.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py311",
                "path": "relative/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_OTHER_CANDIDATE}/py311.xml",
                "sha256": "A" * 64,
                "test_count": -1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
    ),
    ids=("partial", "duplicate", "wrong-environment", "relative-path", "unsafe"),
)
def test_observation_rejects_partial_duplicate_or_unsafe_junit_evidence(
    junit_artifacts: list[dict[str, object]],
) -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-success",
                "returncode": 0,
                "junit_artifacts": junit_artifacts,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    with pytest.raises(JudgeValidationError, match="validation record is malformed"):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)

    assert calls == []
    assert state.writes == []


@pytest.mark.parametrize(
    "record",
    (
        {"candidate_tree": _CANDIDATE, "state": "terminal-success", "returncode": 0},
        {
            "candidate_tree": _CANDIDATE,
            "schema_version": 2,
            "state": "terminal-success",
            "returncode": 0,
        },
        {
            "candidate_tree": _CANDIDATE,
            "schema_version": 2,
            "state": "terminal-success",
            "returncode": 0,
            "junit_artifacts": [],
        },
    ),
    ids=("legacy-omitted", "current-missing", "current-empty"),
)
def test_success_evidence_version_discriminates_legacy_from_current(
    record: dict[str, object],
) -> None:
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, _calls = _dependencies(state)

    if "schema_version" not in record:
        report = observe_judge_validation(
            candidate_tree=_CANDIDATE, dependencies=dependencies
        )
        assert report.junit_artifacts == ()
    else:
        with pytest.raises(
            JudgeValidationError, match="validation record is malformed"
        ):
            observe_judge_validation(
                candidate_tree=_CANDIDATE, dependencies=dependencies
            )


def test_hook_failure_keeps_existing_diagnostics_without_junit_capture() -> None:
    state = ValidationStateFake()
    captures: list[object] = []
    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: CommandResult(7, "", ""),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='1' errors='0'/>",
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )
    dependencies = _passing_gate(dependencies)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic == (
        "git hook run pre-commit failed with exit code 7; "
        "JUnit py311 failures=1 errors=0; py314 failures=1 errors=0"
    )
    assert report.junit_artifacts == ()
    assert "junit_artifacts" not in state.records[_CANDIDATE]
    assert captures == []


def test_artifact_capture_failure_is_terminal_without_rerunning_the_hook() -> None:
    """A successful hook cannot be reported as a usable validation without snapshots."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='0' errors='0'/>",
        capture_junit_artifacts=lambda *_args: (_ for _ in ()).throw(OSError("secret")),
    )
    dependencies = _passing_gate(dependencies)

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    observed = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        first.state == repeated.state == observed.state == "terminal-artifact-failure"
    )
    assert first.returncode == repeated.returncode == observed.returncode == 0
    assert (
        first.junit_artifacts
        == repeated.junit_artifacts
        == observed.junit_artifacts
        == ()
    )
    assert "secret" not in str(state.records[_CANDIDATE])


def test_apply_removes_stale_junit_sources_before_hook_and_in_finally_cleanup(
    tmp_path: Path,
) -> None:
    """Only XML produced by this one hook invocation can become durable evidence."""
    state = ValidationStateFake()
    events: list[str] = []

    def remove_junit_artifact(path: Path) -> None:
        events.append(f"remove:{path}")

    def read_artifact(path: Path) -> bytes:
        events.append(f"read:{path}")
        return b"<testsuite tests='1' failures='0' errors='0'/>"

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        events.append(f"hook:{' '.join(command)}")
        return CommandResult(0, "", "")

    def capture_junit_artifacts(
        _candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        events.append("capture")
        return tuple(
            tmp_path / _CANDIDATE / f"test-results-{environment}.xml"
            for environment, *_rest in artifacts
        )

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        remove_junit_artifact=remove_junit_artifact,
        capture_junit_artifacts=capture_junit_artifacts,
    )
    dependencies = _passing_gate(dependencies)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    fixed_paths = (Path("test-results-py311.xml"), Path("test-results-py314.xml"))
    assert report.state == "terminal-success"
    assert events == [
        *(f"remove:{path}" for path in fixed_paths),
        "hook:git hook run pre-commit",
        *(f"read:{path}" for path in fixed_paths),
        *(f"remove:{path}" for path in fixed_paths),
        "capture",
    ]


def test_post_hook_junit_cleanup_failure_is_terminal_and_never_retries_hook(
    tmp_path: Path,
) -> None:
    """Cleanup errors fail closed after the one completed hook rather than rerunning it."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    removals = 0
    captures: list[object] = []

    def remove_junit_artifact(_path: Path) -> None:
        nonlocal removals
        removals += 1
        if removals == 4:
            raise OSError("private cleanup path")

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='0' errors='0'/>",
        remove_junit_artifact=remove_junit_artifact,
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )
    dependencies = _passing_gate(dependencies)

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert first.state == repeated.state == "terminal-artifact-failure"
    assert first.junit_artifacts == repeated.junit_artifacts == ()
    assert captures == []
    assert "private cleanup path" not in str(state.records[_CANDIDATE])


def test_pre_hook_junit_cleanup_failure_is_terminal_without_a_hook_return_code() -> (
    None
):
    """A stale source that cannot be removed cannot authenticate a new candidate."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    captures: list[object] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: pytest.fail("pre-hook failure must not read XML"),
        remove_junit_artifact=lambda _path: (_ for _ in ()).throw(
            OSError("private stale source")
        ),
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )
    dependencies = _passing_gate(dependencies)

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    observed = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == []
    assert captures == []
    assert (
        first.state == repeated.state == observed.state == "terminal-artifact-failure"
    )
    assert first.returncode == repeated.returncode == observed.returncode is None
    assert (
        first.junit_artifacts
        == repeated.junit_artifacts
        == observed.junit_artifacts
        == ()
    )
    assert "private stale source" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    "artifact",
    (
        b"<testsuite tests='1' failures='0' errors='0'>",
        b"x" * 1_048_577,
    ),
    ids=("malformed", "oversized"),
)
def test_unusable_success_junit_xml_is_terminal_without_capture_or_hook_retry(
    artifact: bytes,
) -> None:
    """Malformed or oversized XML remains a terminal artifact failure."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    removals: list[Path] = []
    captures: list[object] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: artifact,
        remove_junit_artifact=removals.append,
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )
    dependencies = _passing_gate(dependencies)

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert captures == []
    assert removals == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert first.state == repeated.state == "terminal-artifact-failure"
    assert first.returncode == repeated.returncode == 0
    assert first.junit_artifacts == repeated.junit_artifacts == ()


def test_filesystem_junit_snapshot_store_publishes_readonly_candidate_evidence(
    tmp_path: Path,
) -> None:
    """The public store atomically retains the exact payloads under their candidate."""
    state_root = tmp_path / "agent-work-validation" / "repository"
    store = validation.FilesystemJUnitSnapshotStore(root=state_root)
    artifacts = (
        ("py311", b"<testsuite tests='3' failures='0' errors='0'/>", 3),
        ("py314", b"<testsuite tests='5' failures='0' errors='0'/>", 5),
    )

    paths = store.capture(candidate_tree=_CANDIDATE, artifacts=artifacts)

    assert paths == (
        state_root / _CANDIDATE / "test-results-py311.xml",
        state_root / _CANDIDATE / "test-results-py314.xml",
    )
    assert tuple(path.read_bytes() for path in paths) == tuple(
        contents for _environment, contents, _count in artifacts
    )
    assert all(path.is_absolute() for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in paths)
    assert (state_root / _CANDIDATE).stat().st_mode & 0o777 == 0o555


def test_filesystem_junit_snapshot_store_removes_partial_temporary_state(
    tmp_path: Path,
) -> None:
    """A failed atomic publication leaves no candidate or temporary evidence behind."""
    state_root = tmp_path / "agent-work-validation" / "repository"
    writes = 0

    def write_bytes(path: Path, contents: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected write failure")
        path.write_bytes(contents)

    store = validation.FilesystemJUnitSnapshotStore(
        root=state_root, write_bytes=write_bytes
    )

    with pytest.raises((JudgeValidationError, OSError)):
        store.capture(
            candidate_tree=_CANDIDATE,
            artifacts=(
                ("py311", b"first", 1),
                ("py314", b"second", 1),
            ),
        )

    assert not (state_root / _CANDIDATE).exists()
    assert not state_root.exists() or not tuple(state_root.iterdir())


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
    assert preview.hook_failures == success.hook_failures == running.hook_failures == ()
    assert success_artifact_reads == [Path("test-results-py311.xml")]


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
        run_license_preflight=lambda _tree: CommandResult(
            0, _preflight_payload("compliant"), ""
        ),
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=interrupted_run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )
    dependencies = _passing_gate(dependencies)

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
        "schema_version": 2,
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


def test_observation_fails_closed_for_malformed_hook_failure_evidence() -> None:
    valid_line = "pkg/example.py:1:2: E101"
    valid_entry = {"hook_id": "ruff", "diagnostic_lines": [valid_line]}
    records: tuple[dict[str, object], ...] = (
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [{"hook_id": "x" * 129, "diagnostic_lines": [valid_line]}],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "../../unsafe", "diagnostic_lines": [valid_line]}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "ruff", "diagnostic_lines": ["arbitrary secret text"]}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [{"hook_id": "ruff", "diagnostic_lines": ["x" * 257]}],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [valid_entry for _ in range(17)],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "ruff", "diagnostic_lines": [valid_line] * 5}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "hook_failures": [valid_entry],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {
                    "hook_id": f"hook-{index}",
                    "diagnostic_lines": [
                        f"pkg/{'a' * 231}.py:1:2: E101" for _ in range(4)
                    ],
                }
                for index in range(16)
            ],
        },
    )

    for record in records:
        state = ValidationStateFake(records={_CANDIDATE: record})
        dependencies, calls = _dependencies(state)

        with pytest.raises(JudgeValidationError):
            observe_judge_validation(
                candidate_tree=_CANDIDATE, dependencies=dependencies
            )

        assert calls == []
        assert state.writes == []
