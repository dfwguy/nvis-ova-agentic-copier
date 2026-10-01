# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Focused feedback preserves authentic Red identity and reports only known facts."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from os import stat_result
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work import interactive_commit as owner
from agent_work.repository import CommandResult

BRANCH = "refs/heads/clanker/diagnostic-fixture"
RED_REF = "refs/agent-work/red/diagnostic-fixture"
MODULE = "tests/test_candidate.py"
BASELINE = "tests/test_existing.py"
PRIVATE = "PRIVATE_FAILURE_MESSAGE_AND_PARAMETER"


def _environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }


class _Candidate:
    """Real fixture-owned Git history with an unchanged approved baseline module."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        (root / "tests").mkdir()
        (root / "src").mkdir()
        (root / "src/widget.py").write_text("VALUE = 0\n")
        (root / BASELINE).write_text("def test_existing():\n    assert True\n")
        self.commands: list[tuple[str, ...]] = []
        self.git(("init", "-b", BRANCH.removeprefix("refs/heads/")))
        self.git(("add", "."))
        self.git(("commit", "--no-verify", "-m", "base"))
        self.base = self.git(("rev-parse", "HEAD"))
        (root / MODULE).write_text(
            "import pytest\nfrom widget import VALUE\ndef test_candidate():\n    assert VALUE == 1\n@pytest.mark.parametrize('value', ['"
            + PRIVATE
            + "'])\ndef test_failure(value):\n    assert False, '"
            + PRIVATE
            + "'\n"
        )
        self.git(("add", MODULE))
        self.git(("commit", "--no-verify", "-m", "Red"))
        self.red = self.git(("rev-parse", "HEAD"))
        self.git(("update-ref", RED_REF, self.red))
        (root / "src/widget.py").write_text("VALUE = 1\n")
        self.git(("add", "src/widget.py"))
        self.tree = self.git(("write-tree",))
        self.commands.clear()

    def command(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        self.commands.append(command)
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds or 10,
            env=dict(environment or _environment()),
            cwd=current_directory,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def git(self, argv: tuple[str, ...]) -> str:
        result = self.command(("/usr/bin/git", "-C", str(self.root), *argv))
        assert result.returncode == 0
        return result.stdout.strip()

    def run_git(self, worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        return owner.run_git(
            worktree, argv, run_command=self.command, environment=_environment()
        )

    def request(self, baseline: tuple[str, ...] = ()) -> owner.FocusedDiagnosticRequest:
        return owner.FocusedDiagnosticRequest(
            worktree=self.root,
            branch_ref=BRANCH,
            base_sha=self.base,
            red_ref=RED_REF,
            red_sha=self.red,
            candidate_tree=self.tree,
            baseline_modules=baseline,
        )

    def dependencies(
        self,
        runner: owner.FocusedDiagnosticRunner | None,
        approved: tuple[str, ...] = (),
    ) -> owner.FocusedDiagnosticDependencies:
        return owner.FocusedDiagnosticDependencies(
            git=owner.InteractiveCommitDependencies(run_git=self.run_git),
            approved_baseline_modules=approved,
            runner=runner,
        )


class _Runner:
    """Only typed safe facts leave this injected execution boundary."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[Path, tuple[str, ...], tuple[tuple[str, str], ...]]] = []

    def __call__(
        self,
        worktree: Path,
        modules: tuple[str, ...],
        known_cases: tuple[tuple[str, str], ...],
    ) -> owner.FocusedDiagnosticExecution:
        self.calls.append((worktree, modules, known_cases))
        return owner.FocusedDiagnosticExecution(returncode=self.returncode)


class _Artifacts:
    """Fixture-owned, exclusive regular result inode and descriptor-free reader."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir()
        self.closed = False

    @contextmanager
    def temporary_directory(self) -> Iterator[Path]:
        try:
            yield self.root
        finally:
            for path in self.root.iterdir():
                path.unlink()
            self.closed = True

    def create_result(self, path: Path) -> stat_result:
        with path.open("xb"):
            pass
        path.chmod(0o600)
        return path.lstat()

    def read_result(self, path: Path, expected: stat_result) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            actual = os.fstat(descriptor)
            if (
                not stat.S_ISREG(actual.st_mode)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                or actual.st_size > 65_536
            ):
                raise OSError("unsafe fixture artifact")
            return os.read(descriptor, 65_537)
        finally:
            os.close(descriptor)


def _stop_process(process: owner.FocusedProcessPort) -> None:
    if process.poll() is None:
        raise RuntimeError("fixture process was not reaped")


def _real_execution(
    root: Path,
    modules: tuple[str, ...],
    known: tuple[tuple[str, str], ...],
    artifacts: _Artifacts,
) -> owner.FocusedDiagnosticExecution:
    return owner.run_focused_pytest(
        root,
        modules,
        known_cases=known,
        interpreter=Path(sys.executable),
        environment={"PATH": "/usr/bin:/bin"},
        temporary_directory=artifacts.temporary_directory,
        create_result=artifacts.create_result,
        read_result=artifacts.read_result,
        open_process=subprocess.Popen,
        stop_process=_stop_process,
    )


def test_actual_existing_green_preview_proves_authentic_red_staged_and_frozen_controls(
    tmp_path: Path,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    request = owner.InteractiveCommitRequest(
        worktree=candidate.root,
        branch_ref=BRANCH,
        base_sha=candidate.base,
        red_ref=RED_REF,
        red_sha=candidate.red,
        passing_sha=candidate.red,
        judge_tree=candidate.tree,
    )
    report = owner.commit_interactive_green(
        request,
        owner.InteractiveCommitDependencies(run_git=candidate.run_git),
        apply=False,
    )
    assert report.applied is False and report.new_commit is None
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red
    # This existing behavior control also requires the new diagnostic preview.
    runner = _Runner()
    focused = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(runner)
    )
    assert focused.outcome == "preview" and focused.applied is False
    assert runner.calls == []


@pytest.mark.parametrize("apply", [False, True])
def test_diagnostic_selects_retained_red_and_approved_unchanged_owner_without_authority(
    tmp_path: Path, *, apply: bool
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    report = owner.run_focused_diagnostic(
        candidate.request((BASELINE,)),
        candidate.dependencies(runner, (BASELINE,)),
        apply=apply,
    )
    assert report.modules == tuple(sorted((MODULE, BASELINE)))
    assert report.outcome == ("passed" if apply else "preview")
    assert report.applied is apply and report.returncode == (0 if apply else None)
    assert report.failed_cases == () and report.stop_reason is None
    assert len(runner.calls) == int(apply)
    if apply:
        assert set(runner.calls[0][2]) == {
            (MODULE, "test_candidate"),
            (MODULE, "test_failure"),
            (BASELINE, "test_existing"),
        }
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red
    assert not any(
        "commit" in command or "update-ref" in command for command in candidate.commands
    )


@pytest.mark.parametrize(
    "fault",
    [
        "branch",
        "base",
        "red_ref",
        "red_sha",
        "head",
        "candidate_tree",
        "freeze",
        "unstaged",
        "merge",
    ],
)
def test_independent_identity_fault_refuses_before_execution(
    tmp_path: Path, fault: str
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    request = candidate.request()
    assert (
        owner.run_focused_diagnostic(request, candidate.dependencies(runner)).outcome
        == "preview"
    )
    data = request.model_dump()
    if fault == "branch":
        data["branch_ref"] = "refs/heads/clanker/another"
    elif fault in {"base", "red_sha", "candidate_tree"}:
        data[
            {
                "base": "base_sha",
                "red_sha": "red_sha",
                "candidate_tree": "candidate_tree",
            }[fault]
        ] = "f" * 40
    elif fault == "red_ref":
        data["red_ref"] = "refs/agent-work/red/absent"
    elif fault == "head":
        candidate.git(("commit", "--no-verify", "-m", "unexpected"))
    elif fault == "freeze":
        (candidate.root / MODULE).write_text("def test_replaced():\n    pass\n")
        candidate.git(("add", MODULE))
        data["candidate_tree"] = candidate.git(("write-tree",))
    elif fault == "unstaged":
        (candidate.root / "src/widget.py").write_text("VALUE = 2\n")
    else:
        (candidate.root / ".git/MERGE_HEAD").write_text(candidate.base + "\n")
    report = owner.run_focused_diagnostic(
        owner.FocusedDiagnosticRequest.model_validate(data),
        candidate.dependencies(runner),
        apply=True,
    )
    assert (
        report.outcome == "identity_changed"
        and report.stop_reason == "identity_changed"
    )
    assert runner.calls == [] and report.returncode is None


@pytest.mark.parametrize(
    "fault",
    [
        "escape",
        "option",
        "node",
        "non-test",
        "missing",
        "symlink",
        "changed",
        "unapproved",
    ],
)
def test_independent_baseline_selection_fault_cannot_grant_execution(
    tmp_path: Path, fault: str
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    assert (
        owner.run_focused_diagnostic(
            candidate.request((BASELINE,)), candidate.dependencies(runner, (BASELINE,))
        ).outcome
        == "preview"
    )
    selected = {
        "escape": "../tests/test_existing.py",
        "option": "--help",
        "node": BASELINE + "::test_existing",
        "non-test": "src/widget.py",
        "missing": "tests/test_absent.py",
    }.get(fault, BASELINE)
    approved = (selected,)
    if fault == "symlink":
        path = candidate.root / BASELINE
        path.unlink()
        path.symlink_to(candidate.root / "src/widget.py")
        candidate.git(("add", BASELINE))
    elif fault == "changed":
        (candidate.root / BASELINE).write_text(
            "def test_existing():\n    assert False\n"
        )
        candidate.git(("add", BASELINE))
    elif fault == "unapproved":
        approved = ()
    request = candidate.request((selected,))
    if fault in {"symlink", "changed"}:
        request = request.model_copy(
            update={"candidate_tree": candidate.git(("write-tree",))}
        )
    report = owner.run_focused_diagnostic(
        request, candidate.dependencies(runner, approved), apply=True
    )
    assert (
        report.outcome == "invalid_selection"
        and report.stop_reason == "invalid_selection"
    )
    assert runner.calls == [] and report.returncode is None


@pytest.mark.parametrize("returncode", [0, 1])
def test_post_execution_identity_drift_invalidates_success_and_failure(
    tmp_path: Path, returncode: int
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner(returncode)
    assert (
        owner.run_focused_diagnostic(
            candidate.request(), candidate.dependencies(runner)
        ).outcome
        == "preview"
    )

    def drift(
        root: Path, modules: tuple[str, ...], known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        result = runner(root, modules, known)
        (root / "src/widget.py").write_text("VALUE = 9\n")
        return result

    report = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(drift), apply=True
    )
    assert (
        report.outcome == "identity_changed"
        and report.stop_reason == "identity_changed"
    )
    assert report.returncode is None and report.failed_cases == ()
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "reason", ["runtime_unavailable", "timeout", "invalid_result", "cleanup_failed"]
)
def test_execution_error_has_fixed_non_authoritative_outcome(
    tmp_path: Path,
    reason: Literal[
        "runtime_unavailable", "timeout", "invalid_result", "cleanup_failed"
    ],
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    assert (
        owner.run_focused_diagnostic(
            candidate.request(), candidate.dependencies(_Runner())
        ).outcome
        == "preview"
    )

    def fail(
        _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        raise owner.FocusedDiagnosticError(reason)

    report = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(fail), apply=True
    )
    assert report.outcome == (
        "invalid_result" if reason == "invalid_result" else "unavailable"
    )
    assert report.stop_reason == reason and report.returncode is None
    assert PRIVATE not in repr(report)


def test_missing_runner_never_falls_back_to_host_process(tmp_path: Path) -> None:
    candidate = _Candidate(tmp_path / "repo")
    assert (
        owner.run_focused_diagnostic(
            candidate.request(), candidate.dependencies(_Runner())
        ).outcome
        == "preview"
    )
    report = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(None), apply=True
    )
    assert (
        report.outcome == "unavailable" and report.stop_reason == "runtime_unavailable"
    )
    assert report.returncode is None


@pytest.mark.parametrize("module, expected", [(BASELINE, 0), (MODULE, 1)])
def test_actual_fixed_pytest_producer_returns_only_known_static_failed_case(
    tmp_path: Path, module: str, expected: int
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    artifacts = _Artifacts(tmp_path / "artifacts")
    known = (
        (BASELINE, "test_existing"),
        (MODULE, "test_candidate"),
        (MODULE, "test_failure"),
    )
    result = _real_execution(candidate.root, (module,), known, artifacts)
    assert result.returncode == expected and artifacts.closed is True
    if expected:
        assert tuple(
            (case.module, case.test_name, case.phase) for case in result.failed_cases
        ) == ((MODULE, "test_failure", "call"),)
    else:
        assert result.failed_cases == ()
    assert PRIVATE not in repr(result) and result.details_unavailable is False


@pytest.mark.parametrize("fault", ["malformed", "oversized", "missing", "symlink"])
def test_actual_producer_rejects_untrusted_artifact_after_real_success_control(
    tmp_path: Path, fault: str
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    known = ((BASELINE, "test_existing"),)
    control = _real_execution(
        candidate.root, (BASELINE,), known, _Artifacts(tmp_path / "control")
    )
    assert control.returncode == 0 and control.failed_cases == ()
    artifacts = _Artifacts(tmp_path / "fault")

    def read(path: Path, expected: stat_result) -> bytes:
        if fault == "malformed":
            return b'{"raw_private_exception":"' + PRIVATE.encode() + b'"}'
        if fault == "oversized":
            return b"x" * 65_537
        if fault == "missing":
            path.unlink()
        else:
            other = artifacts.root / "synthetic-other"
            other.write_bytes(b"{}")
            path.unlink()
            path.symlink_to(other)
        return artifacts.read_result(path, expected)

    with pytest.raises(owner.FocusedDiagnosticError) as caught:
        owner.run_focused_pytest(
            candidate.root,
            (BASELINE,),
            known_cases=known,
            interpreter=Path(sys.executable),
            environment={"PATH": "/usr/bin:/bin"},
            temporary_directory=artifacts.temporary_directory,
            create_result=artifacts.create_result,
            read_result=read,
            open_process=subprocess.Popen,
            stop_process=_stop_process,
        )
    assert caught.value.reason == "invalid_result" and PRIVATE not in str(caught.value)
    assert artifacts.closed is True


class _SnapshotGit:
    """Injected immutable Git projection for generated public diagnostic invariants."""

    def __init__(self, modules: tuple[str, ...]) -> None:
        self.modules = modules
        self.source_sha = "a" * 40
        self.red_sha = "b" * 40
        self.tree = "c" * 40
        self.head = self.red_sha
        self.function = "test_known"

    def __call__(self, _root: Path, argv: tuple[str, ...]) -> CommandResult:
        if argv[0] == "symbolic-ref":
            return CommandResult(0, BRANCH, "")
        if argv == ("write-tree",):
            return CommandResult(0, self.tree, "")
        if argv[:3] == ("rev-parse", "-q", "--verify") and argv[-1] == "MERGE_HEAD":
            return CommandResult(1, "", "")
        if argv[0] == "rev-parse":
            selected = argv[-1]
            answer = (
                self.head
                if selected in {"HEAD", BRANCH}
                else self.red_sha
                if selected == RED_REF
                else self.tree
                if "tree" in selected
                else self.source_sha
            )
            return CommandResult(0, answer, "")
        if argv[0] == "status":
            return CommandResult(0, "M  src/widget.py\n", "")
        if argv[0] == "diff-tree":
            return CommandResult(0, "\0".join(self.modules) + "\0", "")
        if argv[0] == "show":
            if "--format=%P" in argv:
                return CommandResult(0, self.source_sha, "")
            if "--format=%T" in argv:
                return CommandResult(0, self.tree, "")
            return CommandResult(0, f"def {self.function}():\n    assert True\n", "")
        if argv[0] in {"ls-files", "ls-tree"}:
            path = argv[-1]
            return CommandResult(
                0,
                "100644 " + "d" * 40 + " 0\t" + path + "\0"
                if argv[0] == "ls-files"
                else "100644 blob " + "d" * 40 + "\t" + path + "\0",
                "",
            )
        if argv[0] == "diff":
            return CommandResult(0, "", "")
        raise AssertionError(argv)

    def request(self, baseline: tuple[str, ...] = ()) -> owner.FocusedDiagnosticRequest:
        return owner.FocusedDiagnosticRequest(
            worktree=Path("/synthetic-fixture"),
            branch_ref=BRANCH,
            base_sha=self.source_sha,
            red_ref=RED_REF,
            red_sha=self.red_sha,
            candidate_tree=self.tree,
            baseline_modules=baseline,
        )


@settings(max_examples=15)
@given(
    names=st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
        min_size=1,
        max_size=5,
        unique=True,
    )
)
def test_generated_selection_is_canonical_deterministic_and_contains_only_modules(
    names: list[str],
) -> None:
    modules = tuple(f"tests/test_{name}.py" for name in names)
    git = _SnapshotGit(modules)
    runner = _Runner()
    dependencies = owner.FocusedDiagnosticDependencies(
        git=owner.InteractiveCommitDependencies(run_git=git), runner=runner
    )
    report = owner.run_focused_diagnostic(git.request(), dependencies)
    assert report.outcome == "preview" and report.modules == tuple(sorted(modules))
    assert owner.run_focused_diagnostic(git.request(), dependencies) == report
    assert runner.calls == [] and report.returncode is None
    bad = git.request(("../tests/test_escape.py",))
    refused = owner.run_focused_diagnostic(
        bad,
        replace(dependencies, approved_baseline_modules=("../tests/test_escape.py",)),
        apply=True,
    )
    assert refused.outcome == "invalid_selection" and runner.calls == []


@settings(max_examples=15)
@given(digit=st.sampled_from(tuple("0123456789abcdef")))
def test_generated_shared_identity_comparison_refuses_one_head_change(
    digit: str,
) -> None:
    git = _SnapshotGit((MODULE,))
    runner = _Runner()
    dependencies = owner.FocusedDiagnosticDependencies(
        git=owner.InteractiveCommitDependencies(run_git=git), runner=runner
    )
    assert (
        owner.run_focused_diagnostic(git.request(), dependencies).outcome == "preview"
    )
    git.head = digit * 40 if digit != "b" else "f" * 40
    report = owner.run_focused_diagnostic(git.request(), dependencies, apply=True)
    assert (
        report.outcome == "identity_changed"
        and report.stop_reason == "identity_changed"
    )
    assert runner.calls == []


@settings(max_examples=15)
@given(
    returncode=st.integers(min_value=0, max_value=5),
    name=st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12),
)
def test_generated_outcome_and_known_case_projection_are_non_authoritative(
    returncode: int, name: str
) -> None:
    git = _SnapshotGit((MODULE,))
    function = "test_" + name
    git.function = function

    def run(
        _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        return owner.FocusedDiagnosticExecution(
            returncode=returncode,
            failed_cases=(
                owner.FocusedFailureCase(
                    module=MODULE, test_name=function, phase="call"
                ),
            )
            if returncode
            else (),
        )

    dependencies = owner.FocusedDiagnosticDependencies(
        git=owner.InteractiveCommitDependencies(run_git=git), runner=run
    )
    report = owner.run_focused_diagnostic(git.request(), dependencies, apply=True)
    assert report.returncode == returncode
    assert report.outcome == ("passed" if returncode == 0 else "failed")
    assert tuple(
        (case.module, case.test_name, case.phase) for case in report.failed_cases
    ) == (((MODULE, function, "call"),) if returncode else ())
    assert report.stop_reason is None
    declared = owner.FocusedFailureCase(module=MODULE, test_name=function, phase="call")
    assert declared.test_name == function

    def unknown(
        _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        return owner.FocusedDiagnosticExecution(
            returncode=1,
            failed_cases=(
                owner.FocusedFailureCase(
                    module=MODULE, test_name="test_not_declared" + name, phase="call"
                ),
            ),
        )

    refused = owner.run_focused_diagnostic(
        git.request(), replace(dependencies, runner=unknown), apply=True
    )
    assert refused.outcome == "invalid_result" and refused.failed_cases == ()
    with pytest.raises(ValidationError):
        owner.FocusedFailureCase.model_validate(
            {
                "module": MODULE,
                "test_name": function,
                "phase": "call",
                "raw_exception": PRIVATE,
            }
        )


class _Process:
    """Minimal host process boundary with explicit timeout and cleanup observations."""

    pid = 12345

    def wait(self, *, timeout: float) -> int:
        assert timeout == 10_800
        return 0

    def poll(self) -> int | None:
        return 0


@settings(max_examples=15)
@given(injection=st.text(alphabet="abc012 -/=", min_size=1, max_size=60))
def test_generated_fixed_recipe_and_environment_cannot_be_overridden(
    injection: str,
) -> None:
    root = Path("/synthetic-fixture")
    inspected: list[tuple[object, ...]] = []
    process = _Process()
    metadata = stat_result((stat.S_IFREG | 0o600, 123, 1, 1, 0, 0, 0, 0, 0, 0))

    def spawn(*args: object, **kwargs: object) -> owner.FocusedProcessPort:
        inspected.append(args)
        command = args[0]
        assert isinstance(command, (tuple, list))
        assert command[0] == sys.executable and command[1] == "-I"
        assert "uv" not in command and any(
            "--capture=no" in str(part) for part in command
        )
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert (
            kwargs["stdout"] == subprocess.DEVNULL
            and kwargs["stderr"] == subprocess.DEVNULL
        )
        assert (
            kwargs.get("shell", False) is False and kwargs["start_new_session"] is True
        )
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert all(
            name not in environment
            for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH", "PYTHONHOME")
        )
        assert injection not in command
        return process

    @contextmanager
    def temporary() -> Iterator[Path]:
        yield root / "artifact"

    def create(_path: Path) -> stat_result:
        return metadata

    def read(_path: Path, _expected: stat_result) -> bytes:
        raise owner.FocusedDiagnosticError("invalid_result")

    def stop(observed: owner.FocusedProcessPort) -> None:
        assert observed is process

    with pytest.raises(owner.FocusedDiagnosticError) as caught:
        owner.run_focused_pytest(
            root,
            (MODULE,),
            known_cases=((MODULE, "test_known"),),
            interpreter=Path(sys.executable),
            environment={
                "PATH": "/usr/bin:/bin",
                "PYTEST_ADDOPTS": injection,
                "PYTEST_PLUGINS": injection,
                "PYTHONPATH": injection,
                "PYTHONHOME": injection,
            },
            temporary_directory=temporary,
            create_result=create,
            read_result=read,
            open_process=spawn,
            stop_process=stop,
        )
    assert caught.value.reason == "invalid_result" and len(inspected) == 1


def test_selected_real_executor_policy_refuses_before_host_runner(
    tmp_path: Path,
) -> None:
    from agent_work import test_executor

    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    dependencies = candidate.dependencies(runner)
    assert (
        owner.run_focused_diagnostic(candidate.request(), dependencies).outcome
        == "preview"
    )

    def prepare(
        _request: test_executor.TestExecutionRequest,
    ) -> test_executor.PreparedTestExecution:
        raise AssertionError("unsupported policy must not prepare payload")

    def command(_prepared: test_executor.PreparedTestExecution) -> CommandResult:
        raise AssertionError("unsupported policy must not execute payload")

    paths = tuple(
        tmp_path / name for name in ("auth", "source", "controller", "git", "sibling")
    )
    policy = test_executor.create_test_execution_policy(
        test_executor.TestExecutorPorts(
            nonce=lambda: "a" * 32,
            prepare=prepare,
            probe=command,
            execute=command,
            attest_assets=lambda _prepared: False,
            close_inputs=lambda _prepared: None,
            stop_and_reap=lambda _prepared: True,
            cleanup=lambda _prepared: True,
        ),
        protected_paths=test_executor.TestProtectedPaths(*paths),
    )
    report = owner.run_focused_diagnostic(
        candidate.request(),
        replace(dependencies, test_execution_policy=policy),
        apply=True,
    )
    assert (
        report.outcome == "unsupported_policy"
        and report.stop_reason == "unsupported_policy"
    )
    assert runner.calls == [] and report.returncode is None


def test_after_guard_identity_drift_takes_priority_over_process_timeout(
    tmp_path: Path,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    assert (
        owner.run_focused_diagnostic(
            candidate.request(), candidate.dependencies(_Runner())
        ).outcome
        == "preview"
    )

    def timeout(
        root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        (root / "src/widget.py").write_text("VALUE = 9\n")
        raise owner.FocusedDiagnosticError("timeout")

    report = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(timeout), apply=True
    )
    assert (
        report.outcome == "identity_changed"
        and report.stop_reason == "identity_changed"
    )
    assert report.returncode is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_sha", "bad"),
        ("red_sha", True),
        ("candidate_tree", "a" * 39),
        ("branch_ref", "refs/heads/main"),
        ("red_ref", "refs/heads/main"),
        ("extra", True),
    ],
)
def test_strict_request_rejects_single_invalid_identity_dimension(
    field: str, value: object
) -> None:
    git = _SnapshotGit((MODULE,))
    request = git.request()
    original = request.model_dump()
    assert owner.FocusedDiagnosticRequest.model_validate(original) == request
    altered = {**original, field: value}
    with pytest.raises(ValidationError):
        owner.FocusedDiagnosticRequest.model_validate(altered)


def test_actual_producer_replaced_result_inode_is_never_truncated(
    tmp_path: Path,
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    known = ((BASELINE, "test_existing"),)
    assert (
        _real_execution(
            candidate.root, (BASELINE,), known, _Artifacts(tmp_path / "control")
        ).returncode
        == 0
    )
    artifacts = _Artifacts(tmp_path / "replaced")
    replaced: list[Path] = []
    sentinel = b"SYNTHETIC_RESULT_MUST_REMAIN_UNCHANGED"

    @contextmanager
    def temporary() -> Iterator[Path]:
        try:
            yield artifacts.root
        finally:
            assert len(replaced) == 1 and replaced[0].read_bytes() == sentinel
            for path in artifacts.root.iterdir():
                path.unlink()

    def create(path: Path) -> stat_result:
        expected = artifacts.create_result(path)
        path.rename(path.with_suffix(".original"))
        path.write_bytes(sentinel)
        replaced.append(path)
        assert path.lstat().st_ino != expected.st_ino
        return expected

    with pytest.raises(owner.FocusedDiagnosticError) as caught:
        owner.run_focused_pytest(
            candidate.root,
            (BASELINE,),
            known_cases=known,
            interpreter=Path(sys.executable),
            environment={"PATH": "/usr/bin:/bin"},
            temporary_directory=temporary,
            create_result=create,
            read_result=artifacts.read_result,
            open_process=subprocess.Popen,
            stop_process=_stop_process,
        )
    assert caught.value.reason == "invalid_result"


@pytest.mark.parametrize("fault", ["interpreter", "spawn", "timeout", "cleanup"])
def test_process_boundary_refusal_reaps_and_never_returns_diagnostic_success(
    tmp_path: Path, fault: str
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    known = ((BASELINE, "test_existing"),)
    assert (
        _real_execution(
            candidate.root, (BASELINE,), known, _Artifacts(tmp_path / "control")
        ).returncode
        == 0
    )
    artifacts = _Artifacts(tmp_path / "fault")
    events: list[str] = []

    class Process:
        pid = 12345

        def wait(self, *, timeout: float) -> int:
            events.append("wait")
            if fault == "timeout":
                raise subprocess.TimeoutExpired("fixed pytest", timeout)
            return 0

        def poll(self) -> int | None:
            return 0

    process = Process()

    def spawn(*_args: object, **_kwargs: object) -> owner.FocusedProcessPort:
        events.append("spawn")
        if fault in {"spawn", "interpreter"}:
            raise FileNotFoundError(PRIVATE)
        return process

    def stop(observed: owner.FocusedProcessPort) -> None:
        assert observed is process
        events.append("stop")
        if fault == "cleanup":
            raise OSError(PRIVATE)

    def read(_path: Path, _expected: stat_result) -> bytes:
        return b'{"failed_cases":[],"details_unavailable":false,"truncated":false}'

    with pytest.raises(owner.FocusedDiagnosticError) as caught:
        owner.run_focused_pytest(
            candidate.root,
            (BASELINE,),
            known_cases=known,
            interpreter=Path(sys.executable)
            if fault != "interpreter"
            else tmp_path / "absent-python",
            environment={"PATH": "/usr/bin:/bin"},
            temporary_directory=artifacts.temporary_directory,
            create_result=artifacts.create_result,
            read_result=read,
            open_process=spawn,
            stop_process=stop,
        )
    assert (
        caught.value.reason
        == {
            "interpreter": "runtime_unavailable",
            "spawn": "runtime_unavailable",
            "timeout": "timeout",
            "cleanup": "cleanup_failed",
        }[fault]
    )
    assert PRIVATE not in str(caught.value) and artifacts.closed is True
    if fault in {"timeout", "cleanup"}:
        assert events == ["spawn", "wait", "stop"]


@pytest.mark.parametrize(
    "fault",
    ["boolean-code", "unknown-module", "parameter-name", "success-with-failure"],
)
def test_malformed_injected_execution_never_projects_success_or_unknown_cases(
    tmp_path: Path, fault: str
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    assert (
        owner.run_focused_diagnostic(
            candidate.request(), candidate.dependencies(runner)
        ).outcome
        == "preview"
    )
    valid = owner.FocusedDiagnosticExecution(
        returncode=1,
        failed_cases=(
            owner.FocusedFailureCase(
                module=MODULE, test_name="test_failure", phase="call"
            ),
        ),
    )

    def malformed(
        _root: Path, _modules: tuple[str, ...], _known: tuple[tuple[str, str], ...]
    ) -> owner.FocusedDiagnosticExecution:
        if fault == "boolean-code":
            return valid.model_copy(update={"returncode": True})
        if fault == "success-with-failure":
            return valid.model_copy(update={"returncode": 0})
        case = valid.failed_cases[0].model_copy(
            update={"module": "tests/test_unknown.py"}
            if fault == "unknown-module"
            else {"test_name": "test_failure[" + PRIVATE + "]"}
        )
        return valid.model_copy(update={"failed_cases": (case,)})

    report = owner.run_focused_diagnostic(
        candidate.request(), candidate.dependencies(malformed), apply=True
    )
    assert report.outcome == "invalid_result" and report.stop_reason == "invalid_result"
    assert report.returncode is None and report.failed_cases == ()
    assert PRIVATE not in repr(report)


_IOFamily = Literal["decode", "encode", "translate", "os", "subprocess", "timeout"]
_IOPhase = Literal["baseline", "identity", "selection"]


def _io_error(family: _IOFamily) -> UnicodeError | OSError | subprocess.SubprocessError:
    if family == "decode":
        return UnicodeDecodeError("utf-8", PRIVATE.encode(), 0, 1, PRIVATE)
    if family == "encode":
        return UnicodeEncodeError(
            "ascii", PRIVATE + "界", len(PRIVATE), len(PRIVATE) + 1, PRIVATE
        )
    if family == "translate":
        return UnicodeTranslateError(PRIVATE, 0, 1, PRIVATE)
    if family == "os":
        return OSError(PRIVATE)
    if family == "timeout":
        return subprocess.TimeoutExpired("fixed Git", 30, output=PRIVATE)
    return subprocess.SubprocessError(PRIVATE)


def _fault_matches(
    argv: tuple[str, ...], phase: _IOPhase, base: str, tree: str
) -> bool:
    targets = {
        "baseline": ("ls-tree", "-z", base, "--", BASELINE),
        "identity": ("symbolic-ref", "HEAD"),
        "selection": ("show", f"{tree}:{MODULE}"),
    }
    return argv == targets[phase]


@pytest.mark.parametrize(
    "family", ["decode", "encode", "translate", "os", "subprocess", "timeout"]
)
@pytest.mark.parametrize("phase", ["baseline", "identity", "selection"])
def test_known_initial_git_io_family_has_stage_specific_safe_refusal(
    tmp_path: Path, phase: _IOPhase, family: _IOFamily
) -> None:
    candidate = _Candidate(tmp_path / "repo")
    runner = _Runner()
    request = candidate.request((BASELINE,))
    dependencies = candidate.dependencies(runner, (BASELINE,))
    assert owner.run_focused_diagnostic(request, dependencies).outcome == "preview"
    assert (
        owner.run_focused_diagnostic(request, dependencies, apply=True).outcome
        == "passed"
    )
    runner.calls.clear()
    candidate.commands.clear()
    faults: list[tuple[str, ...]] = []

    def git(root: Path, argv: tuple[str, ...]) -> CommandResult:
        if _fault_matches(argv, phase, candidate.base, candidate.tree):
            if phase == "identity":
                assert (
                    sum(
                        "ls-tree" in command and BASELINE in command
                        for command in candidate.commands
                    )
                    == 2
                )
            if phase == "selection":
                assert any(
                    command[-2:] == ("symbolic-ref", "HEAD")
                    for command in candidate.commands
                )
            faults.append(argv)
            raise _io_error(family)
        return candidate.run_git(root, argv)

    report = owner.run_focused_diagnostic(
        request,
        replace(dependencies, git=owner.InteractiveCommitDependencies(run_git=git)),
        apply=True,
    )
    reason = "identity_changed" if phase == "identity" else "invalid_selection"
    assert report.outcome == reason and report.stop_reason == reason
    assert (
        report.applied is False
        and report.returncode is None
        and report.failed_cases == ()
    )
    assert len(faults) == 1 and runner.calls == [] and PRIVATE not in repr(report)
    assert candidate.git(("rev-parse", "HEAD")) == candidate.red
    assert candidate.git(("write-tree",)) == candidate.tree
    assert not any(
        "commit" in command or "update-ref" in command for command in candidate.commands
    )
