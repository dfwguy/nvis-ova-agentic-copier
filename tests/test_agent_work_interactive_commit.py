# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Real Git contracts for retained interactive Red and Green commits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import build_parser, main
from agent_work.interactive_commit import (
    InteractiveCommitDependencies,
    InteractiveCommitError,
    InteractiveCommitRequest,
    commit_interactive_green,
    run_git,
)
from agent_work.repository import CommandResult

BRANCH = "clanker/issue323-boundary"
BRANCH_REF = f"refs/heads/{BRANCH}"
RED_REF = "refs/agent-work/red/issue323-boundary"
OBJECT_ID = re.compile(r"[0-9a-f]{40}")


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "author@example.invalid",
        "GIT_COMMITTER_NAME": "Test Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsync",
        "GIT_CONFIG_VALUE_0": "none",
    }


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=False,
        env=_environment(),
        timeout=10,
    )
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", BRANCH)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "widget.py").write_text("VALUE = 0\n")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md", "src/widget.py")
    _git(repo, "commit", "--no-verify", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _invoke(
    args: list[str],
    *,
    candidate_gate: Callable[[str], CommandResult] | None = (
        lambda _tree: CommandResult(0, "", "")
    ),
    reject_gate_tool: bool = False,
    license_violation: bool = False,
) -> tuple[int, list[str], list[str], list[tuple[str, ...]]]:
    out: list[str] = []
    err: list[str] = []
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        commands.append(command)
        if command[:2] == (
            sys.executable,
            "scripts/check_licenses.py",
        ):
            return CommandResult(
                1 if license_violation else 0,
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": "violations" if license_violation else "compliant",
                        "violations": (
                            [
                                {
                                    "path": "src/widget.py",
                                    "message": "missing NVIDIA license header",
                                }
                            ]
                            if license_violation
                            else []
                        ),
                    }
                ),
                "",
            )
        if reject_gate_tool and command[:2] == ("uv", "run"):
            return CommandResult(1, "", "rejected gate tool")
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            env=kwargs.get("environment", _environment()),
            cwd=kwargs.get("current_directory"),
            timeout=kwargs.get("timeout_seconds", 30),
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)

    injected_gate = (
        {"candidate_gate": candidate_gate} if candidate_gate is not None else {}
    )
    code = cast(Callable[..., int], main)(
        args,
        run_command=run,
        environment=_environment(),
        write_stdout=out.append,
        write_stderr=err.append,
        **injected_gate,
    )
    return code, out, err, commands


def _args(repo: Path, base: str, action: str) -> list[str]:
    return [
        "interactive-commit",
        action,
        "--worktree",
        str(repo),
        "--branch-ref",
        BRANCH_REF,
        "--base-sha",
        base,
        "--red-ref",
        RED_REF,
    ]


def _message(tmp_path: Path) -> Path:
    message = tmp_path / "message.txt"
    message.write_text("reviewed change\n")
    return message


def _make_red(repo: Path, base: str) -> str:
    (repo / "tests" / "test_widget.py").write_text(
        "def test_widget():\n    assert 1 == 2\n"
    )
    _git(repo, "add", "tests/test_widget.py")
    _git(repo, "commit", "--no-verify", "-m", "Red")
    red = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--parents", "-n", "1", red) == f"{red} {base}"
    _git(repo, "update-ref", RED_REF, red, "")
    return red


def _make_green(repo: Path, red: str) -> tuple[str, str]:
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    tree = _git(repo, "write-tree")
    _git(repo, "commit", "--no-verify", "-m", "Green")
    green = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--parents", "-n", "1", green) == (f"{green} {red}")
    return green, tree


def _staged_green_request(tmp_path: Path) -> tuple[Path, InteractiveCommitRequest]:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    return repo, InteractiveCommitRequest(
        worktree=repo,
        branch_ref=BRANCH_REF,
        base_sha=base,
        red_ref=RED_REF,
        red_sha=red,
        passing_sha=red,
        judge_tree=_git(repo, "write-tree"),
        message_file=_message(tmp_path),
    )


@pytest.mark.parametrize("gate_returncode", (0, 1))
def test_interactive_green_checks_exact_tree_before_installed_commit(
    tmp_path: Path, gate_returncode: int
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    tree = _git(repo, "write-tree")
    request = InteractiveCommitRequest(
        worktree=repo,
        branch_ref=BRANCH_REF,
        base_sha=base,
        red_ref=RED_REF,
        red_sha=red,
        passing_sha=red,
        judge_tree=tree,
        message_file=_message(tmp_path),
    )
    events: list[str] = []

    def commit(worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        events.append("commit")
        return CommandResult(0, _git(worktree, *argv), "")

    dependencies = InteractiveCommitDependencies(
        run_git=run_git, commit_worktree=commit
    )
    dependencies = replace(
        dependencies,
        run_license_preflight=lambda _tree: CommandResult(
            0,
            '{"schema_version":1,"outcome":"compliant","violations":[]}',
            "",
        ),
        check_candidate_gate=lambda candidate: (
            events.append(f"gate:{candidate}") or CommandResult(gate_returncode, "", "")
        ),
    )

    if gate_returncode:
        with pytest.raises(InteractiveCommitError):
            commit_interactive_green(request, dependencies, apply=True)
    else:
        report = commit_interactive_green(request, dependencies, apply=True)
        assert report.applied

    assert events[0] == f"gate:{tree}"
    assert ("commit" in events) is (gate_returncode == 0)


def test_interactive_green_requires_license_preflight_before_gate(
    tmp_path: Path,
) -> None:
    _repo, request = _staged_green_request(tmp_path)
    events: list[str] = []
    dependencies = replace(
        InteractiveCommitDependencies(
            run_git=run_git,
            commit_worktree=lambda _path, _argv: (
                events.append("commit") or CommandResult(0, "", "")
            ),
        ),
        check_candidate_gate=lambda _tree: (
            events.append("gate") or CommandResult(0, "", "")
        ),
    )

    with pytest.raises(InteractiveCommitError):
        commit_interactive_green(request, dependencies, apply=True)

    assert events == []


def test_interactive_cli_injects_gate_for_exact_green_tree_before_commit(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    tree = _git(repo, "write-tree")
    gated: list[str] = []
    arguments = _args(repo, base, "green") + [
        "--red-sha",
        red,
        "--judge-tree",
        tree,
        "--message-file",
        str(_message(tmp_path)),
        "--apply",
    ]

    code, _out, _err, commands = _invoke(
        arguments,
        candidate_gate=lambda candidate: (
            gated.append(candidate) or CommandResult(0, "", "")
        ),
    )

    assert code == 0
    assert gated == [tree]
    assert any(command[-3:-1] == ("commit", "-F") for command in commands)


def test_interactive_cli_default_runs_real_gate_before_commit(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    tree = _git(repo, "write-tree")
    arguments = _args(repo, base, "green") + [
        "--red-sha",
        red,
        "--judge-tree",
        tree,
        "--message-file",
        str(_message(tmp_path)),
        "--apply",
    ]

    code, _out, _err, commands = _invoke(
        arguments, candidate_gate=None, reject_gate_tool=True
    )

    assert code == 1
    assert any(command[:2] == ("uv", "run") for command in commands)
    assert not any(command[-3:-1] == ("commit", "-F") for command in commands)


def test_interactive_cli_license_violation_stops_gate_and_commit(
    tmp_path: Path,
) -> None:
    repo, request = _staged_green_request(tmp_path)
    gated: list[str] = []
    arguments = _args(repo, request.base_sha, "green") + [
        "--red-sha",
        request.red_sha,
        "--judge-tree",
        request.judge_tree,
        "--message-file",
        str(request.message_file),
        "--apply",
    ]

    code, _out, _err, commands = _invoke(
        arguments,
        candidate_gate=lambda tree: gated.append(tree) or CommandResult(0, "", ""),
        license_violation=True,
    )

    checker = [
        command
        for command in commands
        if command[:2] == (sys.executable, "scripts/check_licenses.py")
    ]
    assert code == 1
    assert len(checker) == 1
    assert checker[0][-3:] == (
        "--candidate-tree",
        request.judge_tree,
        "--json",
    )
    assert gated == []
    assert not any(command[-3:-1] == ("commit", "-F") for command in commands)


@pytest.mark.parametrize(
    "failure",
    ("invalid-license", "violated-license", "gate-id", "setup-error", "timeout"),
)
def test_interactive_green_failure_stops_before_commit_without_private_output(
    tmp_path: Path, failure: str
) -> None:
    repo, request = _staged_green_request(tmp_path)
    events: list[str] = []

    def license_preflight(tree: str) -> CommandResult:
        events.append(f"license:{tree}")
        if failure == "invalid-license":
            return CommandResult(0, "not-json", "")
        if failure == "violated-license":
            return CommandResult(1, "private stdout", "private stderr")
        return CommandResult(
            0, '{"schema_version":1,"outcome":"compliant","violations":[]}', ""
        )

    def gate(tree: str) -> CommandResult:
        events.append(f"gate:{tree}")
        if failure == "setup-error":
            raise RuntimeError("private setup path")
        if failure == "timeout":
            raise TimeoutError("private token")
        return CommandResult(1, "private stdout", "ruff-check failed: private stderr")

    dependencies = replace(
        InteractiveCommitDependencies(
            run_git=run_git,
            commit_worktree=lambda _path, _argv: (
                events.append("commit") or CommandResult(0, "", "")
            ),
        ),
        run_license_preflight=license_preflight,
        check_candidate_gate=gate,
    )

    with pytest.raises(InteractiveCommitError) as error:
        commit_interactive_green(request, dependencies, apply=True)

    assert events == (
        [f"license:{request.judge_tree}"]
        if failure.endswith("license")
        else [f"license:{request.judge_tree}", f"gate:{request.judge_tree}"]
    )
    assert "private" not in str(error.value)
    if failure == "gate-id":
        assert "ruff-check" in str(error.value)
    assert _git(repo, "rev-parse", "HEAD") == request.red_sha


def _ref(repo: Path, name: str) -> str | None:
    value = _git(repo, "rev-parse", "-q", "--verify", name, check=False)
    return value or None


def _object_store(repo: Path) -> tuple[tuple[str, str], ...]:
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    objects = (
        git_dir.parent / "objects"
        if git_dir.name == "worktrees"
        else git_dir / "objects"
    )
    if not objects.exists():
        common_dir = Path(_git(repo, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        objects = common_dir / "objects"
    return tuple(
        (str(path.relative_to(objects)), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(objects.rglob("*"))
        if path.is_file()
    )


def _state(repo: Path, hook_marker: Path) -> tuple[object, ...]:
    index = Path(_git(repo, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = repo / index
    files = tuple(
        (str(path.relative_to(repo)), path.read_bytes())
        for path in sorted(repo.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(repo).parts
    )
    return (
        _git(repo, "symbolic-ref", "HEAD"),
        _git(repo, "rev-parse", "HEAD"),
        _ref(repo, RED_REF),
        index.read_bytes(),
        files,
        hook_marker.read_bytes() if hook_marker.exists() else None,
    )


def _snapshot(repo: Path, hook_marker: Path) -> tuple[object, ...]:
    return _state(repo, hook_marker), _object_store(repo)


def _assert_observational(repo: Path, commands: list[tuple[str, ...]]) -> None:
    for command in commands:
        assert command[:5] == (
            "git",
            "-C",
            str(repo),
            "-c",
            "core.fsmonitor=false",
        )
        arguments = command[5:]
        if arguments in {
            ("symbolic-ref", "HEAD"),
            ("rev-parse", "-q", "--verify", "MERGE_HEAD"),
            ("status", "--porcelain=v1", "--untracked-files=all"),
        }:
            continue
        if arguments[:2] == ("rev-parse", "--verify") and len(arguments) == 3:
            revision = arguments[2]
            assert revision in {"HEAD", BRANCH_REF, RED_REF} or (
                revision.endswith("^{tree}")
                and OBJECT_ID.fullmatch(revision.removesuffix("^{tree}"))
            )
            continue
        if (
            len(arguments) == 4
            and arguments[:2] == ("show", "-s")
            and arguments[2] in {"--format=%P", "--format=%T"}
        ):
            assert OBJECT_ID.fullmatch(arguments[3])
            continue
        if arguments[:6] == (
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-z",
        ):
            assert len(arguments) == 7
            assert OBJECT_ID.fullmatch(arguments[6])
            continue
        if (
            len(arguments) == 6
            and arguments[:2] == ("diff", "--quiet")
            and OBJECT_ID.fullmatch(arguments[2])
            and arguments[3:] == ("--", "tests", "template/tests")
        ):
            continue
        pytest.fail(f"non-observational Git argv: {arguments!r}")


def _assert_rejected_without_changes(
    repo: Path,
    hook_marker: Path,
    arguments: list[str],
    *,
    observational: bool = False,
) -> None:
    before = _snapshot(repo, hook_marker)
    code, _, _, commands = _invoke(arguments)
    assert code == 1
    if observational:
        _assert_observational(repo, commands)
    assert _snapshot(repo, hook_marker) == before


def _verify_args(repo: Path, base: str, red: str, green: str, tree: str) -> list[str]:
    return _args(repo, base, "verify") + [
        "--red-sha",
        red,
        "--green-sha",
        green,
        "--judge-tree",
        tree,
    ]


def test_interactive_cli_exposes_retained_stages_and_rejects_old_routes() -> None:
    parser = build_parser()
    for action in ("red", "green", "verify"):
        with pytest.raises(SystemExit) as result:
            parser.parse_args(["interactive-commit", action, "--help"])
        assert result.value.code == 0
    for obsolete in (("interactive-commit", "compact"), ("review-handoff",)):
        with pytest.raises(SystemExit) as result:
            parser.parse_args([*obsolete, "--help"])
        assert result.value.code == 2


def test_interactive_red_green_apply_and_verify_real_git_history(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    message = _message(tmp_path)
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text("def test_widget():\n    assert 1 == 2\n")
    _git(repo, "add", "tests/test_widget.py")
    red_arguments = _args(repo, base, "red") + [
        "--red-sha",
        base,
        "--red-tree",
        _git(repo, "write-tree"),
        "--message-file",
        str(message),
        "--apply",
    ]

    code, _, _, commands = _invoke(red_arguments)

    assert code == 0
    red1 = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--parents", "-n", "1", red1) == (f"{red1} {base}")
    assert _git(repo, "rev-parse", RED_REF) == red1
    assert any("--no-verify" in command for command in commands)

    test_file.write_text("def test_widget():\n    assert 1 == 3\n")
    _git(repo, "add", "tests/test_widget.py")
    red_arguments[red_arguments.index("--red-sha") + 1] = red1
    red_arguments[red_arguments.index("--red-tree") + 1] = _git(repo, "write-tree")
    assert _invoke(red_arguments)[0] == 0
    red2 = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--parents", "-n", "1", red2) == (f"{red2} {red1}")
    assert _git(repo, "rev-parse", RED_REF) == red2

    hook_marker = tmp_path / "hook-runs"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf x >> '{hook_marker}'\n")
    hook.chmod(0o755)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    tree = _git(repo, "write-tree")
    green_arguments = _args(repo, base, "green") + [
        "--red-sha",
        red2,
        "--judge-tree",
        tree,
        "--message-file",
        str(message),
        "--apply",
    ]

    code, _, _, commands = _invoke(green_arguments)

    assert code == 0
    green = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-list", "--parents", "-n", "1", green) == (f"{green} {red2}")
    assert hook_marker.read_text() == "x"
    assert not any("--no-verify" in command for command in commands)
    code, _, _, commands = _invoke(_verify_args(repo, base, red2, green, tree))
    assert code == 0
    _assert_observational(repo, commands)


@pytest.mark.parametrize("action", ["red", "green"])
def test_interactive_commit_default_is_dry_run_without_side_effects(
    tmp_path: Path, action: str
) -> None:
    repo, base = _repo(tmp_path)
    message = _message(tmp_path)
    hook_marker = tmp_path / "hook-runs"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf x >> '{hook_marker}'\n")
    hook.chmod(0o755)
    if action == "red":
        (repo / "tests" / "test_widget.py").write_text(
            "def test_widget():\n    assert 1 == 2\n"
        )
        _git(repo, "add", "tests/test_widget.py")
        red = base
        tree_option = "--red-tree"
    else:
        red = _make_red(repo, base)
        (repo / "src" / "widget.py").write_text("VALUE = 1\n")
        _git(repo, "add", "src/widget.py")
        tree_option = "--judge-tree"
    arguments = _args(repo, base, action) + [
        "--red-sha",
        red,
        tree_option,
        _git(repo, "write-tree"),
        "--message-file",
        str(message),
    ]
    before = _state(repo, hook_marker)

    code, _, _, _ = _invoke(arguments)

    assert code == 0
    assert _state(repo, hook_marker) == before


def test_recovered_red_rejects_non_test_successor_before_ref_update(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 7\n")
    _git(repo, "add", "src/widget.py")
    _git(repo, "commit", "--no-verify", "-m", "invalid Red successor")
    invalid = _git(repo, "rev-parse", "HEAD")
    arguments = _args(repo, base, "red") + [
        "--red-sha",
        red,
        "--red-tree",
        _git(repo, "rev-parse", "HEAD^{tree}"),
        "--message-file",
        str(_message(tmp_path)),
        "--apply",
    ]

    _assert_rejected_without_changes(repo, tmp_path / "hook-runs", arguments)
    assert _git(repo, "rev-parse", "HEAD") == invalid
    assert _git(repo, "rev-parse", RED_REF) == red


def test_green_rejects_preexisting_commit_without_hook_proof(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    green, tree = _make_green(repo, red)
    hook_marker = tmp_path / "hook-runs"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf x >> '{hook_marker}'\n")
    hook.chmod(0o755)
    arguments = _args(repo, base, "green") + [
        "--red-sha",
        red,
        "--judge-tree",
        tree,
        "--message-file",
        str(_message(tmp_path)),
        "--apply",
    ]
    _assert_rejected_without_changes(repo, hook_marker, arguments)
    assert _git(repo, "rev-parse", "HEAD") == green


@pytest.mark.parametrize("test_change", ["staged", "unstaged"])
def test_green_rejects_test_changes_without_hook(
    tmp_path: Path, test_change: str
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    (repo / "src" / "widget.py").write_text("VALUE = 1\n")
    _git(repo, "add", "src/widget.py")
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text("def test_widget():\n    assert 1 == 3\n")
    if test_change == "staged":
        _git(repo, "add", "tests/test_widget.py")
    tree = _git(repo, "write-tree")
    hook_marker = tmp_path / "hook-runs"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf x >> '{hook_marker}'\n")
    hook.chmod(0o755)
    arguments = _args(repo, base, "green") + [
        "--red-sha",
        red,
        "--judge-tree",
        tree,
        "--message-file",
        str(_message(tmp_path)),
        "--apply",
    ]

    _assert_rejected_without_changes(repo, hook_marker, arguments)
    assert not hook_marker.exists()


def test_verify_rejects_wrong_green_sole_parent_in_isolation(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    tree = _git(repo, "rev-parse", f"{red}^{{tree}}")
    wrong_green = _git(repo, "commit-tree", tree, "-p", base, "-m", "wrong parent")
    _git(repo, "reset", "--hard", wrong_green)

    _assert_rejected_without_changes(
        repo,
        tmp_path / "hook-runs",
        _verify_args(repo, base, red, wrong_green, tree),
        observational=True,
    )


def test_verify_rejects_checked_out_branch_mismatch_in_isolation(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    green, tree = _make_green(repo, red)
    _git(repo, "branch", "clanker/other", green)
    _git(repo, "switch", "clanker/other")

    _assert_rejected_without_changes(
        repo,
        tmp_path / "hook-runs",
        _verify_args(repo, base, red, green, tree),
        observational=True,
    )


@pytest.mark.parametrize("stale_identity", ["red-ref", "green-sha", "judge-tree"])
def test_verify_rejects_each_stale_identity_without_writes(
    tmp_path: Path, stale_identity: str
) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    green, tree = _make_green(repo, red)
    arguments = _verify_args(repo, base, red, green, tree)
    if stale_identity == "red-ref":
        _git(repo, "update-ref", RED_REF, base, red)
    elif stale_identity == "green-sha":
        arguments[arguments.index("--green-sha") + 1] = base
    else:
        arguments[arguments.index("--judge-tree") + 1] = _git(
            repo, "rev-parse", f"{base}^{{tree}}"
        )

    _assert_rejected_without_changes(
        repo,
        tmp_path / "hook-runs",
        arguments,
        observational=True,
    )


def test_verify_uses_only_observational_git_and_writes_nothing(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path)
    red = _make_red(repo, base)
    green, tree = _make_green(repo, red)
    hook_marker = tmp_path / "hook-runs"
    before = _state(repo, hook_marker)
    objects_before = _object_store(repo)

    code, _, _, commands = _invoke(_verify_args(repo, base, red, green, tree))

    assert code == 0
    _assert_observational(repo, commands)
    assert _state(repo, hook_marker) == before
    assert _object_store(repo) == objects_before


def test_verify_rejects_test_changes_in_green_and_each_red_commit(
    tmp_path: Path,
) -> None:
    repo, base = _repo(tmp_path)
    red1 = _make_red(repo, base)
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text("def test_widget():\n    assert 1 == 3\n")
    _git(repo, "add", "tests/test_widget.py")
    _git(repo, "commit", "--no-verify", "-m", "Red correction")
    red2 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", RED_REF, red2, red1)
    green, tree = _make_green(repo, red2)
    assert _invoke(_verify_args(repo, base, red2, green, tree))[0] == 0

    _git(repo, "reset", "--hard", red2)
    test_file.write_text("def test_widget():\n    assert 1 == 4\n")
    _git(repo, "add", "tests/test_widget.py")
    _git(repo, "commit", "--no-verify", "-m", "wrong Green")
    wrong_green = _git(repo, "rev-parse", "HEAD")
    wrong_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _assert_rejected_without_changes(
        repo,
        tmp_path / "hook-runs",
        _verify_args(repo, base, red2, wrong_green, wrong_tree),
        observational=True,
    )

    _git(repo, "reset", "--hard", red1)
    (repo / "src" / "widget.py").write_text("VALUE = 8\n")
    _git(repo, "add", "src/widget.py")
    _git(repo, "commit", "--no-verify", "-m", "wrong Red")
    wrong_red = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", RED_REF, wrong_red, red2)
    wrong_green, wrong_tree = _make_green(repo, wrong_red)
    _assert_rejected_without_changes(
        repo,
        tmp_path / "hook-runs",
        _verify_args(repo, base, wrong_red, wrong_green, wrong_tree),
        observational=True,
    )
