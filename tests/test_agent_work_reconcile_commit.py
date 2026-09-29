# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior of an exact two-parent reconciliation commit boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work import interactive_commit as commit_module
from agent_work.cli import build_parser, main
from agent_work.interactive_commit import (
    InteractiveCommitDependencies,
    InteractiveCommitError,
)
from agent_work.repository import CommandResult

BRANCH_REF = "refs/heads/clanker/reconcile-boundary"
RED_REF = "refs/agent-work/red-checkpoints/81/checkpoint"
reconcile_api: Any = commit_module


def _request(tmp_path: Path, *, red_ref: str = RED_REF) -> Any:
    return reconcile_api.ReconcileCommitRequest(
        worktree=tmp_path,
        branch_ref=BRANCH_REF,
        source_sha="a" * 40,
        target_sha="b" * 40,
        red_ref=red_ref,
        red_sha="c" * 40,
        judge_tree="d" * 40,
        message_file=tmp_path / "message.txt",
    )


def _fake_merge(
    request: Any,
) -> tuple[
    dict[tuple[str, ...], CommandResult], list[str], InteractiveCommitDependencies
]:
    values = {
        ("rev-parse", "--verify", "HEAD"): request.source_sha,
        ("rev-parse", "--verify", BRANCH_REF): request.source_sha,
        ("rev-parse", "--verify", "MERGE_HEAD"): request.target_sha,
        ("rev-parse", "--verify", "refs/remotes/origin/main"): request.target_sha,
        ("rev-parse", "--verify", request.red_ref): request.red_sha,
        ("rev-parse", "--verify", f"{request.judge_tree}^{{tree}}"): request.judge_tree,
        ("symbolic-ref", "HEAD"): BRANCH_REF,
        ("ls-files", "-u"): "",
        ("write-tree",): request.judge_tree,
        ("status", "--porcelain=v1", "--untracked-files=all"): "M  src.txt\n",
    }
    results: dict[tuple[str, ...], CommandResult] = {
        action: CommandResult(0, output, "") for action, output in values.items()
    }
    events: list[str] = []

    def read_git(worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        assert worktree == request.worktree
        assert argv in results, f"unexpected Git action: {argv}"
        return results[argv]

    dependencies = InteractiveCommitDependencies(
        run_git=read_git,
        run_license_preflight=lambda _tree: (
            events.append("license")
            or CommandResult(
                0, '{"schema_version":1,"outcome":"compliant","violations":[]}', ""
            )
        ),
        check_candidate_gate=lambda _tree: (
            events.append("gate") or CommandResult(0, "", "")
        ),
        commit_worktree=lambda _worktree, _argv: (
            events.append("commit") or CommandResult(1, "", "hook rejected")
        ),
    )
    return results, events, dependencies


def _cli_args(request: Any, action: str) -> list[str]:
    args = [
        "reconcile-commit",
        action,
        "--worktree",
        str(request.worktree),
        "--branch-ref",
        request.branch_ref,
        "--source-sha",
        request.source_sha,
        "--target-sha",
        request.target_sha,
        "--red-ref",
        request.red_ref,
        "--red-sha",
        request.red_sha,
        "--judge-tree",
        request.judge_tree,
    ]
    if action == "commit":
        args.extend(("--message-file", str(request.message_file)))
    return args


def test_reconcile_cli_help_and_required_inputs() -> None:
    parser = build_parser()
    for action in ("commit", "verify"):
        with pytest.raises(SystemExit) as help_exit:
            parser.parse_args(["reconcile-commit", action, "--help"])
        assert help_exit.value.code == 0
        with pytest.raises(SystemExit) as missing_exit:
            parser.parse_args(["reconcile-commit", action])
        assert missing_exit.value.code == 2


@pytest.mark.parametrize("red_ref", (RED_REF, "refs/agent-work/red/reconcile-boundary"))
def test_reconcile_preview_preserves_pending_merge_and_skips_gates(
    tmp_path: Path, red_ref: str
) -> None:
    request = _request(tmp_path, red_ref=red_ref)
    results, events, dependencies = _fake_merge(request)

    report = reconcile_api.commit_reconcile(request, dependencies, apply=False)

    assert report.applied is False
    assert report.new_commit is None
    assert events == []
    assert results[("rev-parse", "--verify", "MERGE_HEAD")].stdout == request.target_sha


@pytest.mark.parametrize(
    ("drift", "action", "output"),
    (
        ("head", ("rev-parse", "--verify", "HEAD"), "c" * 40),
        ("branch", ("symbolic-ref", "HEAD"), "refs/heads/target"),
        ("merge_head", ("rev-parse", "--verify", "MERGE_HEAD"), "c" * 40),
        ("target_ref", ("rev-parse", "--verify", "refs/remotes/origin/main"), "c" * 40),
        ("tree", ("write-tree",), "c" * 40),
        (
            "unstaged",
            ("status", "--porcelain=v1", "--untracked-files=all"),
            " M src.txt\n",
        ),
        (
            "untracked",
            ("status", "--porcelain=v1", "--untracked-files=all"),
            "?? extra.txt\n",
        ),
        ("red_ref", ("rev-parse", "--verify", RED_REF), "a" * 40),
        ("conflict", ("ls-files", "-u"), "100644 abc 1\tsrc.txt\n"),
    ),
)
def test_reconcile_rejects_drift_without_commit(
    tmp_path: Path, drift: str, action: tuple[str, ...], output: str
) -> None:
    request = _request(tmp_path)
    results, events, dependencies = _fake_merge(request)
    results[action] = CommandResult(0, output, "")

    with pytest.raises(InteractiveCommitError):
        reconcile_api.commit_reconcile(request, dependencies, apply=True)

    assert "commit" not in events, drift


@pytest.mark.parametrize("failure", ("license", "gate"))
def test_reconcile_preflight_failure_preserves_pending_merge(
    tmp_path: Path, failure: str
) -> None:
    request = _request(tmp_path)
    results, events, dependencies = _fake_merge(request)
    if failure == "license":
        dependencies = replace(
            dependencies,
            run_license_preflight=lambda _tree: (
                events.append("license") or CommandResult(1, "", "")
            ),
        )
    else:
        dependencies = replace(
            dependencies,
            check_candidate_gate=lambda _tree: (
                events.append("gate") or CommandResult(1, "", "")
            ),
        )

    with pytest.raises(InteractiveCommitError):
        reconcile_api.commit_reconcile(request, dependencies, apply=True)

    assert "commit" not in events
    assert results[("rev-parse", "--verify", "MERGE_HEAD")].stdout == request.target_sha


@pytest.mark.parametrize("during", ("license", "gate"))
@pytest.mark.parametrize("drift", ("tree", "merge_head", "red_ref"))
def test_reconcile_rechecks_identity_after_preflight(
    tmp_path: Path, drift: str, during: str
) -> None:
    request = _request(tmp_path)
    results, events, dependencies = _fake_merge(request)
    action = {
        "tree": ("write-tree",),
        "merge_head": ("rev-parse", "--verify", "MERGE_HEAD"),
        "red_ref": ("rev-parse", "--verify", RED_REF),
    }[drift]

    def change_identity(stage: str) -> CommandResult:
        events.append(stage)
        if stage == during:
            results[action] = CommandResult(0, request.source_sha, "")
        if stage == "license":
            return CommandResult(
                0, '{"schema_version":1,"outcome":"compliant","violations":[]}', ""
            )
        return CommandResult(0, "", "")

    dependencies = replace(
        dependencies,
        run_license_preflight=lambda _tree: change_identity("license"),
        check_candidate_gate=lambda _tree: change_identity("gate"),
    )

    with pytest.raises(InteractiveCommitError):
        reconcile_api.commit_reconcile(request, dependencies, apply=True)

    assert "commit" not in events
    assert events[0] == "license"


def test_reconcile_hook_failure_keeps_pending_merge(tmp_path: Path) -> None:
    request = _request(tmp_path)
    results, events, dependencies = _fake_merge(request)

    with pytest.raises(
        InteractiveCommitError,
        match="installed pre-commit rejected reconciliation.*hook rejected",
    ):
        reconcile_api.commit_reconcile(request, dependencies, apply=True)

    assert events == ["license", "gate", "commit"]
    assert results[("rev-parse", "--verify", "MERGE_HEAD")].stdout == request.target_sha


def test_reconcile_verify_rejects_wrong_parent_without_writes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    committed = "e" * 40
    request = request.model_copy(update={"reconcile_sha": committed})
    results: dict[tuple[str, ...], CommandResult] = {
        ("symbolic-ref", "HEAD"): CommandResult(0, BRANCH_REF, ""),
        ("rev-parse", "--verify", "HEAD"): CommandResult(0, committed, ""),
        ("rev-parse", "--verify", BRANCH_REF): CommandResult(0, committed, ""),
        ("rev-parse", "--verify", "refs/remotes/origin/main"): CommandResult(
            0, request.target_sha, ""
        ),
        ("rev-parse", "--verify", RED_REF): CommandResult(0, request.red_sha, ""),
        ("show", "-s", "--format=%P", committed): CommandResult(
            0, request.source_sha, ""
        ),
    }
    calls: list[tuple[str, ...]] = []

    def read_git(_worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        calls.append(argv)
        return results[argv]

    with pytest.raises(InteractiveCommitError, match="reconciliation parents"):
        reconcile_api.verify_reconcile_final(
            request, InteractiveCommitDependencies(run_git=read_git)
        )

    assert calls
    assert all(action in results for action in calls)


def test_reconcile_cli_default_preview_is_json(tmp_path: Path) -> None:
    request = _request(tmp_path)
    results, _events, _dependencies = _fake_merge(request)
    out: list[str] = []
    err: list[str] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        prefix = ("git", "-C", str(tmp_path), "-c", "core.fsmonitor=false")
        assert command[: len(prefix)] == prefix
        return results[command[len(prefix) :]]

    code = cast(Callable[..., int], main)(
        [*_cli_args(request, "commit"), "--json"],
        run_command=run,
        write_stdout=out.append,
        write_stderr=err.append,
    )

    assert code == 0
    assert json.loads(out[-1]) == {"applied": False, "new_commit": None}
    assert err == []


def test_reconcile_cli_apply_uses_default_candidate_gate(tmp_path: Path) -> None:
    request = _request(tmp_path)
    results, _events, _dependencies = _fake_merge(request)
    gate_probes: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        if command[:2] == (sys.executable, "scripts/check_licenses.py"):
            return CommandResult(
                0, '{"schema_version":1,"outcome":"compliant","violations":[]}', ""
            )
        if command == ("git", "rev-parse", "HEAD"):
            gate_probes.append(command)
            return CommandResult(1, "", "snapshot unavailable")
        prefix = ("git", "-C", str(tmp_path), "-c", "core.fsmonitor=false")
        assert command[: len(prefix)] == prefix
        return results[command[len(prefix) :]]

    errors: list[str] = []
    code = cast(Callable[..., int], main)(
        [*_cli_args(request, "commit"), "--apply"],
        run_command=run,
        write_stderr=errors.append,
    )

    assert code == 1
    assert gate_probes == [("git", "rev-parse", "HEAD")]
    assert errors == ["cheap candidate gate failed"]


def test_reconcile_cli_verify_apply_is_read_only(tmp_path: Path) -> None:
    request = _request(tmp_path)
    committed = "e" * 40
    results: dict[tuple[str, ...], CommandResult] = {
        ("symbolic-ref", "HEAD"): CommandResult(0, BRANCH_REF, ""),
        ("rev-parse", "--verify", "HEAD"): CommandResult(0, committed, ""),
        ("rev-parse", "--verify", BRANCH_REF): CommandResult(0, committed, ""),
        ("rev-parse", "--verify", "refs/remotes/origin/main"): CommandResult(
            0, request.target_sha, ""
        ),
        ("rev-parse", "--verify", RED_REF): CommandResult(0, request.red_sha, ""),
        ("show", "-s", "--format=%P", committed): CommandResult(
            0, f"{request.source_sha} {request.target_sha}", ""
        ),
        ("show", "-s", "--format=%T", committed): CommandResult(
            0, request.judge_tree, ""
        ),
        ("rev-parse", "-q", "--verify", "MERGE_HEAD"): CommandResult(1, "", ""),
        ("status", "--porcelain=v1", "--untracked-files=all"): CommandResult(0, "", ""),
    }
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(command)
        prefix = ("git", "-C", str(tmp_path), "-c", "core.fsmonitor=false")
        assert command[: len(prefix)] == prefix
        return results[command[len(prefix) :]]

    out: list[str] = []
    code = cast(Callable[..., int], main)(
        [
            *_cli_args(request, "verify"),
            "--reconcile-sha",
            committed,
            "--apply",
            "--json",
        ],
        run_command=run,
        write_stdout=out.append,
    )

    assert code == 0
    assert json.loads(out[-1]) == {
        "verified": True,
        "reconcile_commit": committed,
        "judge_tree": request.judge_tree,
    }
    assert commands
    assert all("commit" not in command for command in commands)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        },
    )
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def test_reconcile_real_git_creates_exact_two_parent_commit_and_verifies_read_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "clanker/reconcile-boundary")
    marks = tmp_path / "marks"
    stream = (
        "commit refs/heads/clanker/reconcile-boundary\n"
        "mark :1\n"
        "author Test Author <author@example.invalid> 1 +0000\n"
        "committer Test Committer <committer@example.invalid> 1 +0000\n"
        "data 4\nbase\n"
        "M 100644 inline base.txt\n"
        "data 5\nbase\n\n"
        "commit refs/heads/clanker/reconcile-boundary\n"
        "mark :2\n"
        "author Test Author <author@example.invalid> 2 +0000\n"
        "committer Test Committer <committer@example.invalid> 2 +0000\n"
        "data 3\nRed\n"
        "from :1\n"
        "M 100644 inline tests/test_source.py\n"
        "data 35\ndef test_source():\n    assert True\n\n"
        "commit refs/heads/clanker/reconcile-boundary\n"
        "mark :3\n"
        "author Test Author <author@example.invalid> 3 +0000\n"
        "committer Test Committer <committer@example.invalid> 3 +0000\n"
        "data 6\nsource\n"
        "from :2\n"
        "M 100644 inline source.txt\n"
        "data 7\nsource\n\n"
        "commit refs/heads/target\n"
        "mark :4\n"
        "author Test Author <author@example.invalid> 4 +0000\n"
        "committer Test Committer <committer@example.invalid> 4 +0000\n"
        "data 6\ntarget\n"
        "from :1\n"
        "M 100644 inline target.txt\n"
        "data 7\ntarget\n\n"
    )
    imported = subprocess.run(
        ("git", "-C", str(repo), "fast-import", "--quiet", f"--export-marks={marks}"),
        input=stream,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert imported.returncode == 0, imported.stderr
    identifiers = dict(line.split() for line in marks.read_text().splitlines())
    red = identifiers[":2"]
    source = identifiers[":3"]
    target = identifiers[":4"]
    _git(repo, "update-ref", RED_REF, red)
    _git(repo, "update-ref", "refs/remotes/origin/main", target)
    _git(repo, "reset", "-q", "--hard", "HEAD")
    _git(repo, "merge", "-q", "--no-commit", "--no-ff", "target")
    message = tmp_path / "message.txt"
    message.write_text("Reconcile source with target\n")
    request = reconcile_api.ReconcileCommitRequest(
        worktree=repo,
        branch_ref=BRANCH_REF,
        source_sha=source,
        target_sha=target,
        red_ref=RED_REF,
        red_sha=red,
        judge_tree=_git(repo, "write-tree"),
        message_file=message,
    )
    events: list[str] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        if command[:2] == (sys.executable, "scripts/check_licenses.py"):
            return CommandResult(
                0, '{"schema_version":1,"outcome":"compliant","violations":[]}', ""
            )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=kwargs.get("timeout_seconds", 30),
            cwd=kwargs.get("current_directory"),
            env={
                **kwargs.get("environment", os.environ),
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "author@example.invalid",
                "GIT_COMMITTER_NAME": "Test Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.invalid",
            },
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def gate(_tree: str) -> CommandResult:
        events.append("gate")
        return CommandResult(0, "", "")

    out: list[str] = []
    code = cast(Callable[..., int], main)(
        [*_cli_args(request, "commit"), "--apply", "--json"],
        run_command=run,
        candidate_gate=gate,
        write_stdout=out.append,
    )
    assert code == 0
    committed = json.loads(out[-1])["new_commit"]
    assert _git(repo, "show", "-s", "--format=%P", "HEAD") == f"{source} {target}"
    assert _git(repo, "show", "-s", "--format=%T", "HEAD") == request.judge_tree
    assert _git(repo, "rev-parse", RED_REF) == red
    assert _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert events == ["gate"]
    out.clear()
    code = cast(Callable[..., int], main)(
        [*_cli_args(request, "verify"), "--reconcile-sha", committed, "--json"],
        run_command=run,
        write_stdout=out.append,
    )
    assert code == 0
    assert json.loads(out[-1])["reconcile_commit"] == committed
    assert _git(repo, "rev-parse", "HEAD") == committed


def test_reconcile_cli_rejects_invalid_ref_as_input_error(tmp_path: Path) -> None:
    request = _request(tmp_path)
    args = _cli_args(request, "commit")
    args[args.index("--branch-ref") + 1] = "refs/heads/main"
    err: list[str] = []

    code = cast(Callable[..., int], main)(args, write_stderr=err.append)

    assert code == 2
    assert err
