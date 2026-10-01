# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The real CLI composes trusted focused execution without creating commit proof."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_work import cli
from agent_work import interactive_commit as owner
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult
from tests.test_agent_work_focused_diagnostics import (
    BASELINE,
    BRANCH,
    MODULE,
    PRIVATE,
    RED_REF,
    _Artifacts,
    _Candidate,
    _environment,
    _fault_matches,
    _io_error,
    _IOFamily,
    _IOPhase,
    _real_execution,
    _Runner,
    _SnapshotGit,
)


class _Main(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        run_command: CommandRunner,
        environment: Mapping[str, str],
        write_stdout: Callable[[str], None],
        write_stderr: Callable[[str], None],
        approved_diagnostic_modules: tuple[str, ...] = (),
        focused_runner: owner.FocusedDiagnosticRunner | None = None,
    ) -> int: ...


def _arguments(candidate: _Candidate) -> list[str]:
    return [
        "interactive-commit",
        "diagnose",
        "--worktree",
        str(candidate.root),
        "--branch-ref",
        BRANCH,
        "--base-sha",
        candidate.base,
        "--red-ref",
        RED_REF,
        "--red-sha",
        candidate.red,
        "--candidate-tree",
        candidate.tree,
    ]


def _invoke(
    candidate: _Candidate,
    arguments: list[str],
    runner: owner.FocusedDiagnosticRunner | None,
    approved: tuple[str, ...] = (),
) -> tuple[int, list[str], list[str]]:
    out: list[str] = []
    err: list[str] = []
    code = cast(_Main, cli.main)(
        arguments,
        run_command=candidate.command,
        environment=_environment(),
        write_stdout=out.append,
        write_stderr=err.append,
        approved_diagnostic_modules=approved,
        focused_runner=runner,
    )
    return code, out, err


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("json_output", [False, True])
def test_actual_main_composes_approved_owner_and_preview_apply_report(
    tmp_path: Path, *, apply: bool, json_output: bool
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    arguments = [*_arguments(candidate), "--baseline-module", BASELINE]
    if apply:
        arguments.append("--apply")
    if json_output:
        arguments.append("--json")
    code, out, err = _invoke(candidate, arguments, runner, (BASELINE,))
    assert code == 0 and err == [] and len(runner.calls) == int(apply)
    if json_output:
        payload = json.loads("\n".join(out))
        assert payload["outcome"] == ("passed" if apply else "preview")
        assert payload["modules"] == sorted((MODULE, BASELINE))
        assert payload["applied"] is apply
        assert not set(payload).intersection(
            {"stdout", "stderr", "new_commit", "validation_proof"}
        )
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red


@pytest.mark.parametrize("debug", [False, True])
def test_actual_main_real_producer_failed_known_case_suppresses_private_output(
    tmp_path: Path, *, debug: bool
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    artifacts = _Artifacts(tmp_path / "artifacts")

    def runner(
        root: Path, modules: tuple[str, ...], known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        return _real_execution(root, modules, known, artifacts)

    arguments = [*_arguments(candidate), "--apply", "--json"]
    if debug:
        arguments.append("--debug")
    code, out, err = _invoke(candidate, arguments, runner)
    assert code == 1 and err == []
    payload = json.loads("\n".join(out))
    assert payload["outcome"] == "failed" and payload["returncode"] == 1
    assert payload["failed_cases"] == [
        {"module": MODULE, "test_name": "test_failure", "phase": "call"}
    ]
    assert PRIVATE not in repr((out, err)) and artifacts.closed is True


@pytest.mark.parametrize("fault", ["unapproved", "identity", "runtime", "malformed"])
@pytest.mark.parametrize("debug", [False, True])
def test_actual_main_safe_failure_has_fixed_exit_and_no_private_traceback(
    tmp_path: Path, fault: str, *, debug: bool
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    arguments = [*_arguments(candidate), "--apply", "--json"]
    runner: owner.FocusedDiagnosticRunner | None = _Runner()
    if fault == "unapproved":
        arguments.extend(("--baseline-module", BASELINE))
    elif fault == "identity":
        (candidate.root / "src/widget.py").write_text("VALUE = 9\n")
    elif fault == "runtime":

        def unavailable(
            _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
        ) -> owner.FocusedDiagnosticExecution:
            raise owner.FocusedDiagnosticError("runtime_unavailable")

        runner = unavailable
    else:

        def malformed(
            _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
        ) -> owner.FocusedDiagnosticExecution:
            raise owner.FocusedDiagnosticError("invalid_result")

        runner = malformed
    if debug:
        arguments.append("--debug")
    code, out, err = _invoke(candidate, arguments, runner)
    assert code == 1 and err == []
    payload = json.loads("\n".join(out))
    expected = {
        "unapproved": "invalid_selection",
        "identity": "identity_changed",
        "runtime": "unavailable",
        "malformed": "invalid_result",
    }[fault]
    assert payload["outcome"] == expected and payload["returncode"] is None
    assert PRIVATE not in repr((out, err)) and "Traceback" not in repr((out, err))


def test_diagnose_help_is_available_and_invalid_usage_does_not_execute(
    tmp_path: Path,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    assert (
        _invoke(candidate, ["interactive-commit", "diagnose", "--help"], runner)[0] == 0
    )
    assert _invoke(candidate, ["interactive-commit", "diagnose"], runner)[0] == 2
    invalid = _arguments(candidate)
    invalid[invalid.index("--base-sha") + 1] = "invalid"
    code, out, err = _invoke(candidate, invalid, runner)
    assert code == 2 and runner.calls == []
    assert PRIVATE not in repr((out, err))


def test_human_failed_report_is_safe_and_has_no_commit_authority(
    tmp_path: Path,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner(1)
    code, out, err = _invoke(candidate, [*_arguments(candidate), "--apply"], runner)
    assert code == 1 and len(runner.calls) == 1
    assert PRIVATE not in repr((out, err)) and "Traceback" not in repr((out, err))
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red


@pytest.mark.parametrize(
    "family", ["decode", "encode", "translate", "os", "subprocess", "timeout"]
)
@pytest.mark.parametrize("phase", ["baseline", "identity", "selection"])
@pytest.mark.parametrize("json_output,debug", [(True, False), (False, True)])
def test_actual_main_known_git_io_has_fixed_stage_reason_and_safe_output(
    tmp_path: Path,
    phase: _IOPhase,
    family: _IOFamily,
    *,
    json_output: bool,
    debug: bool,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    arguments = [*_arguments(candidate), "--baseline-module", BASELINE]
    assert _invoke(candidate, [*arguments, "--json"], runner, (BASELINE,))[0] == 0
    assert (
        _invoke(candidate, [*arguments, "--apply", "--json"], runner, (BASELINE,))[0]
        == 0
    )
    runner.calls.clear()
    candidate.commands.clear()
    faults: list[tuple[str, ...]] = []

    def command(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        if _fault_matches(command[5:], phase, candidate.base, candidate.tree):
            if phase == "identity":
                assert (
                    sum(
                        "ls-tree" in recorded and BASELINE in recorded
                        for recorded in candidate.commands
                    )
                    == 2
                )
            if phase == "selection":
                assert any(
                    recorded[-2:] == ("symbolic-ref", "HEAD")
                    for recorded in candidate.commands
                )
            faults.append(command)
            raise _io_error(family)
        return candidate.command(
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            environment=environment,
            current_directory=current_directory,
        )

    arguments.append("--apply")
    if json_output:
        arguments.append("--json")
    if debug:
        arguments.append("--debug")
    out: list[str] = []
    err: list[str] = []
    code = cast(_Main, cli.main)(
        arguments,
        run_command=command,
        environment=_environment(),
        write_stdout=out.append,
        write_stderr=err.append,
        approved_diagnostic_modules=(BASELINE,),
        focused_runner=runner,
    )
    reason = "identity_changed" if phase == "identity" else "invalid_selection"
    assert code == 1 and len(faults) == 1 and runner.calls == []
    assert PRIVATE not in repr((out, err)) and "Traceback" not in repr((out, err))
    if json_output:
        assert err == []
        payload = json.loads("\n".join(out))
        assert payload["outcome"] == reason and payload["stop_reason"] == reason
        assert (
            payload["applied"] is False
            and payload["returncode"] is None
            and payload["failed_cases"] == []
        )
    else:
        assert out == []
        assert all(
            token in "\n".join(err)
            for token in (f"outcome={reason}", "applied=false", f"stop_reason={reason}")
        )
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red
    assert candidate.git(("write-tree",)) == candidate.tree
    assert not any(
        "commit" in argv or "update-ref" in argv for argv in candidate.commands
    )


@pytest.mark.parametrize("phase", ["baseline", "identity", "selection"])
@settings(max_examples=6)
@given(
    family=st.sampled_from(
        ("decode", "encode", "translate", "os", "subprocess", "timeout")
    ),
    selected_runner=st.booleans(),
    json_output=st.booleans(),
    debug=st.booleans(),
)
def test_generated_actual_main_known_io_phase_invariant_never_executes_or_leaks(
    phase: _IOPhase,
    family: _IOFamily,
    *,
    selected_runner: bool,
    json_output: bool,
    debug: bool,
) -> None:
    git = _SnapshotGit((MODULE,))
    root = Path("/synthetic-fixture")
    runner = _Runner()
    arguments = [
        "interactive-commit",
        "diagnose",
        "--worktree",
        str(root),
        "--branch-ref",
        BRANCH,
        "--base-sha",
        git.source_sha,
        "--red-ref",
        RED_REF,
        "--red-sha",
        git.red_sha,
        "--candidate-tree",
        git.tree,
        "--baseline-module",
        BASELINE,
    ]
    calls: list[tuple[str, ...]] = []
    faults: list[tuple[str, ...]] = []
    enabled = False

    def command(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        del stdin, timeout_seconds, environment, current_directory
        selected = command[5:]
        if enabled and _fault_matches(selected, phase, git.source_sha, git.tree):
            faults.append(selected)
            raise _io_error(family)
        calls.append(selected)
        return git(root, selected)

    def invoke(
        selected: list[str], chosen: owner.FocusedDiagnosticRunner | None
    ) -> tuple[int, list[str], list[str]]:
        out: list[str] = []
        err: list[str] = []
        code = cast(_Main, cli.main)(
            selected,
            run_command=command,
            environment=_environment(),
            write_stdout=out.append,
            write_stderr=err.append,
            approved_diagnostic_modules=(BASELINE,),
            focused_runner=chosen,
        )
        return code, out, err

    assert invoke([*arguments, "--json"], runner)[0] == 0
    assert invoke([*arguments, "--apply", "--json"], runner)[0] == 0
    runner.calls.clear()
    calls.clear()
    enabled = True
    selected = [*arguments, "--apply"]
    if json_output:
        selected.append("--json")
    if debug:
        selected.append("--debug")
    code, out, err = invoke(selected, runner if selected_runner else None)
    reason = "identity_changed" if phase == "identity" else "invalid_selection"
    assert code == 1 and len(faults) == 1 and runner.calls == []
    assert PRIVATE not in repr((out, err)) and "Traceback" not in repr((out, err))
    if json_output:
        assert err == []
        payload = json.loads("\n".join(out))
        assert payload["outcome"] == reason and payload["stop_reason"] == reason
        assert (
            payload["applied"] is False
            and payload["returncode"] is None
            and payload["failed_cases"] == []
        )
    else:
        assert out == []
        assert f"outcome={reason}" in "\n".join(
            err
        ) and f"stop_reason={reason}" in "\n".join(err)
    if phase == "identity":
        assert len(calls) == 2 and all(call[0] == "ls-tree" for call in calls)
    if phase == "selection":
        assert ("symbolic-ref", "HEAD") in calls
