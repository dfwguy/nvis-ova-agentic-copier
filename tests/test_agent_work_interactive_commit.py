# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for one guarded interactive passing commit."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from agent_work.repository import CommandResult

BASE_SHA = "a" * 40
RED_SHA = "b" * 40
PASSING_SHA = "c" * 40
JUDGE_TREE = "d" * 40
NEW_SHA = "e" * 40
WRONG_SHA = "f" * 40
BRANCH_REF = "refs/heads/clanker/issue-306"
RED_REF = "refs/agent-work/red/issue-306"
PASSING_IDENTITY = (
    "Passing Author\0author@example.invalid\0"
    "2026-09-27T08:00:00+00:00\0"
    "Passing Committer\0committer@example.invalid\0"
    "2026-09-27T08:01:00+00:00"
)


def _cli_argv(tmp_path: Path) -> list[str]:
    return [
        "interactive-commit",
        "compact",
        "--worktree",
        str(tmp_path),
        "--branch-ref",
        BRANCH_REF,
        "--base-sha",
        BASE_SHA,
        "--red-ref",
        RED_REF,
        "--red-sha",
        RED_SHA,
        "--passing-sha",
        PASSING_SHA,
        "--judge-tree",
        JUDGE_TREE,
        "--message-file",
        str(tmp_path / "message.txt"),
        "--json",
    ]


def _module() -> Any:
    return importlib.import_module("agent_work.interactive_commit")


def _request(tmp_path: Path, **changes: object) -> Any:
    (tmp_path / "message.txt").write_text("Compact reviewed change\n")
    values: dict[str, object] = {
        "worktree": tmp_path,
        "branch_ref": BRANCH_REF,
        "base_sha": BASE_SHA,
        "red_ref": RED_REF,
        "red_sha": RED_SHA,
        "passing_sha": PASSING_SHA,
        "judge_tree": JUDGE_TREE,
        "message_file": tmp_path / "message.txt",
    }
    values.update(changes)
    return _module().InteractiveCommitRequest(**values)


class FakeGit:
    """Model only the observable Git identities and mutations in the protocol."""

    def __init__(
        self,
        *,
        stale: str | None = None,
        cas_fails: bool = False,
        identity_text: str = PASSING_IDENTITY,
    ) -> None:
        self.stale = stale
        self.cas_fails = cas_fails
        self.commands: list[tuple[str, ...]] = []
        self.head = PASSING_SHA
        self.branch = PASSING_SHA
        self.red = RED_SHA
        self.clean = True
        self.identity_text = identity_text

    def __call__(self, _worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        self.commands.append(argv)
        command = tuple(argv)
        if "commit-tree" in command:
            assert command == (
                "commit-tree",
                JUDGE_TREE,
                "-p",
                BASE_SHA,
                "-F",
                str(_worktree / "message.txt"),
            )
            return CommandResult(0, NEW_SHA + "\n", "")
        if "update-ref" in command:
            assert command == ("update-ref", BRANCH_REF, NEW_SHA, PASSING_SHA)
            if self.cas_fails:
                return CommandResult(1, "", "cannot lock ref")
            self.branch = NEW_SHA
            self.head = NEW_SHA
            return CommandResult(0, "", "")
        if "symbolic-ref" in command:
            return CommandResult(0, BRANCH_REF + "\n", "")
        if "status" in command or ("diff" in command and "--quiet" in command):
            if (
                self.stale == "dirty"
                or not self.clean
                or (self.stale == "after_dirty" and self.head == NEW_SHA)
            ):
                return CommandResult(0, " M src/agent_work/cli.py\n", "")
            return CommandResult(0, "", "")
        if "merge-base" in command:
            return CommandResult(1 if self.stale == "ancestor" else 0, "", "")
        if "show" in command and "--format=%P" in command:
            if RED_SHA in command or RED_REF in command:
                parent = WRONG_SHA if self.stale == "red_parent" else BASE_SHA
                return CommandResult(0, parent + "\n", "")
            if self.stale == "after_parent":
                return CommandResult(0, WRONG_SHA + "\n", "")
            return CommandResult(0, BASE_SHA + "\n", "")
        if "show" in command and "--format=%T" in command:
            tree = (
                WRONG_SHA
                if self.stale == "tree"
                or (self.stale == "after_tree" and self.head == NEW_SHA)
                else JUDGE_TREE
            )
            return CommandResult(0, tree + "\n", "")
        if "show" in command and any("%an%x00%ae" in part for part in command):
            if command[-1] != PASSING_SHA:
                return CommandResult(1, "", "identity must come from passing commit")
            return CommandResult(0, self.identity_text + "\n", "")
        if "rev-list" in command or "show" in command:
            if RED_SHA in command or RED_REF in command:
                parent = WRONG_SHA if self.stale == "red_parent" else BASE_SHA
                return CommandResult(0, f"{RED_SHA} {parent}\n", "")
            if self.head == NEW_SHA:
                return CommandResult(0, f"{NEW_SHA} {BASE_SHA}\n", "")
            return CommandResult(0, f"{PASSING_SHA} {BASE_SHA}\n", "")
        if "rev-parse" in command:
            revision = command[-1]
            if revision == "MERGE_HEAD":
                return CommandResult(
                    0, RED_SHA if self.stale == "merge_head" else "", ""
                )
            if revision in {"HEAD", "HEAD^{commit}"}:
                head = (
                    WRONG_SHA
                    if self.stale == "head"
                    or (self.stale == "after_head" and self.head == NEW_SHA)
                    else self.head
                )
                return CommandResult(0, head + "\n", "")
            if revision in {BRANCH_REF, f"{BRANCH_REF}^{{commit}}"}:
                branch = (
                    WRONG_SHA
                    if self.stale == "branch"
                    or (self.stale == "after_branch" and self.head == NEW_SHA)
                    else self.branch
                )
                return CommandResult(0, branch + "\n", "")
            if revision in {RED_REF, f"{RED_REF}^{{commit}}"}:
                red = (
                    WRONG_SHA
                    if self.stale == "red_ref"
                    or (self.stale == "after_red_ref" and self.head == NEW_SHA)
                    else self.red
                )
                return CommandResult(0, red + "\n", "")
            if revision.endswith("^{tree}"):
                tree = WRONG_SHA if self.stale == "tree" else JUDGE_TREE
                return CommandResult(0, tree + "\n", "")
            if revision in {BASE_SHA, RED_SHA, PASSING_SHA, NEW_SHA}:
                return CommandResult(0, revision + "\n", "")
        return CommandResult(0, "", "")

    @property
    def mutations(self) -> list[tuple[str, ...]]:
        """Return only Git commands that can change repository state."""
        return [
            command
            for command in self.commands
            if any(
                part in {"commit-tree", "update-ref", "reset", "checkout", "push"}
                for part in command
            )
        ]


def _dependencies(git: FakeGit) -> Any:
    module = _module()

    def commit_tree(
        worktree: Path, argv: tuple[str, ...], _identity: Any
    ) -> CommandResult:
        return git(worktree, argv)

    return module.InteractiveCommitDependencies(run_git=git, commit_tree=commit_tree)


def test_preview_proves_boundary_without_mutating(tmp_path: Path) -> None:
    git = FakeGit()
    module = _module()

    report = module.compact_interactive_commit(
        _request(tmp_path),
        _dependencies(git),
        apply=False,
    )

    assert report.applied is False
    assert report.new_commit is None
    assert git.mutations == []
    assert git.head == PASSING_SHA
    assert git.red == RED_SHA


def test_apply_creates_one_base_parent_commit_and_cas_updates_branch(
    tmp_path: Path,
) -> None:
    git = FakeGit()
    module = _module()

    report = module.compact_interactive_commit(
        _request(tmp_path),
        _dependencies(git),
        apply=True,
    )

    assert report.applied is True
    assert report.new_commit == NEW_SHA
    assert len(git.mutations) == 2
    assert "commit-tree" in git.mutations[0]
    assert JUDGE_TREE in git.mutations[0]
    assert BASE_SHA in git.mutations[0]
    assert str(tmp_path / "message.txt") in git.mutations[0]
    assert git.mutations[1] == ("update-ref", BRANCH_REF, NEW_SHA, PASSING_SHA)
    assert git.head == NEW_SHA
    assert git.red == RED_SHA


@pytest.mark.parametrize(
    "stale",
    (
        "head",
        "branch",
        "red_ref",
        "red_parent",
        "tree",
        "dirty",
        "ancestor",
        "merge_head",
    ),
)
def test_failed_precheck_rejects_without_any_mutation(
    tmp_path: Path, stale: str
) -> None:
    git = FakeGit(stale=stale)
    module = _module()

    with pytest.raises(module.InteractiveCommitError):
        module.compact_interactive_commit(
            _request(tmp_path),
            _dependencies(git),
            apply=True,
        )

    assert git.mutations == []
    assert git.branch == PASSING_SHA
    assert git.red == RED_SHA


def test_failed_compare_and_swap_stops_without_repair(tmp_path: Path) -> None:
    git = FakeGit(cas_fails=True)
    module = _module()

    with pytest.raises(module.InteractiveCommitError):
        module.compact_interactive_commit(
            _request(tmp_path),
            _dependencies(git),
            apply=True,
        )

    assert len(git.mutations) == 2
    assert git.mutations[-1] == ("update-ref", BRANCH_REF, NEW_SHA, PASSING_SHA)
    assert git.branch == PASSING_SHA
    assert git.red == RED_SHA


@pytest.mark.parametrize(
    "stale",
    (
        "after_head",
        "after_branch",
        "after_parent",
        "after_tree",
        "after_red_ref",
        "after_dirty",
    ),
)
def test_failed_postcheck_stops_without_repair(tmp_path: Path, stale: str) -> None:
    git = FakeGit(stale=stale)
    module = _module()

    with pytest.raises(module.InteractiveCommitError):
        module.compact_interactive_commit(
            _request(tmp_path),
            _dependencies(git),
            apply=True,
        )

    assert git.mutations == [
        (
            "commit-tree",
            JUDGE_TREE,
            "-p",
            BASE_SHA,
            "-F",
            str(tmp_path / "message.txt"),
        ),
        ("update-ref", BRANCH_REF, NEW_SHA, PASSING_SHA),
    ]


def test_apply_uses_passing_identity_in_commit_callback(tmp_path: Path) -> None:
    git = FakeGit()
    module = _module()
    captured: list[Any] = []

    def commit_tree(
        worktree: Path, argv: tuple[str, ...], identity: Any
    ) -> CommandResult:
        captured.append(identity)
        return git(worktree, argv)

    report = module.compact_interactive_commit(
        _request(tmp_path),
        module.InteractiveCommitDependencies(run_git=git, commit_tree=commit_tree),
        apply=True,
    )

    assert report.new_commit == NEW_SHA
    assert len(captured) == 1
    identity = captured[0]
    assert identity.author_name == "Passing Author"
    assert identity.author_email == "author@example.invalid"
    assert identity.author_date == "2026-09-27T08:00:00+00:00"
    assert identity.committer_name == "Passing Committer"
    assert identity.committer_email == "committer@example.invalid"
    assert identity.committer_date == "2026-09-27T08:01:00+00:00"
    assert [
        command
        for command in git.commands
        if "show" in command and any("%an%x00%ae" in part for part in command)
    ] == [
        (
            "show",
            "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
            PASSING_SHA,
        )
    ]


@pytest.mark.parametrize("apply", (False, True))
@pytest.mark.parametrize(
    "identity_text",
    (
        "",
        "only one field",
        "A\0B\0C\0D\0E\0",
        "A\0B\0C\0D\0E\0F\nG",
        PASSING_IDENTITY.replace("Passing Author", "A" * 257),
        PASSING_IDENTITY.replace(
            "2026-09-27T08:00:00+00:00", "2026-02-30T08:00:00+00:00"
        ),
        PASSING_IDENTITY.replace(
            "2026-09-27T08:01:00+00:00", "2026-02-30T08:01:00+00:00"
        ),
    ),
)
def test_malformed_passing_identity_rejects_before_mutation(
    tmp_path: Path, identity_text: str, *, apply: bool
) -> None:
    git = FakeGit(identity_text=identity_text)
    module = _module()

    with pytest.raises(module.InteractiveCommitError):
        module.compact_interactive_commit(
            _request(tmp_path),
            _dependencies(git),
            apply=apply,
        )

    assert git.mutations == []
    assert git.branch == PASSING_SHA
    assert git.red == RED_SHA


def test_missing_commit_callback_rejects_at_dependency_boundary() -> None:
    git = FakeGit()
    module = _module()

    with pytest.raises(TypeError):
        module.InteractiveCommitDependencies(run_git=git)

    assert git.mutations == []


@pytest.mark.parametrize("field", ("base_sha", "red_sha", "passing_sha", "judge_tree"))
def test_request_rejects_invalid_object_id(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError):
        _request(tmp_path, **{field: "not-an-object-id"})


@pytest.mark.parametrize("apply", (False, True))
def test_cli_parser_requires_explicit_inputs_and_selects_apply(
    tmp_path: Path, *, apply: bool
) -> None:
    from agent_work.cli import build_parser

    argv = [
        "interactive-commit",
        "compact",
        "--worktree",
        str(tmp_path),
        "--branch-ref",
        BRANCH_REF,
        "--base-sha",
        BASE_SHA,
        "--red-ref",
        RED_REF,
        "--red-sha",
        RED_SHA,
        "--passing-sha",
        PASSING_SHA,
        "--judge-tree",
        JUDGE_TREE,
        "--message-file",
        str(tmp_path / "message.txt"),
        "--json",
    ]
    args = build_parser().parse_args([*argv, *(["--apply"] if apply else [])])

    assert args.resource == "interactive-commit"
    assert args.action == "compact"
    assert args.json is True
    assert args.apply is apply


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_work.cli import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["interactive-commit", "compact", "--help"])

    assert error.value.code == 0
    assert "--apply" in capsys.readouterr().out


def test_cli_missing_required_input_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent_work.cli import build_parser

    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["interactive-commit", "compact"])

    assert error.value.code == 2
    assert "--worktree" in capsys.readouterr().err


@pytest.mark.parametrize("apply", (False, True))
def test_cli_dispatch_uses_injected_git_runner(tmp_path: Path, *, apply: bool) -> None:
    from agent_work.cli import main

    git = FakeGit()
    _request(tmp_path)
    output: list[str] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        assert command[:3] == ("git", "-C", str(tmp_path))
        assert command[3:5] == ("-c", "core.fsmonitor=false")
        return git(tmp_path, command[5:])

    code = main(
        [*_cli_argv(tmp_path), *(["--apply"] if apply else [])],
        run_command=run,
        write_stdout=output.append,
    )

    assert code == 0
    assert json.loads(output[0]) == {
        "applied": apply,
        "new_commit": NEW_SHA if apply else None,
    }
    assert len(git.mutations) == (2 if apply else 0)


def test_cli_dispatch_reports_proof_and_input_errors(tmp_path: Path) -> None:
    from agent_work.cli import main

    _request(tmp_path)
    errors: list[str] = []
    git = FakeGit(stale="head")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        return git(tmp_path, command[5:])

    assert main(_cli_argv(tmp_path), run_command=run, write_stderr=errors.append) == 1
    assert errors and git.mutations == []
    errors.clear()
    argv = _cli_argv(tmp_path)
    argv[argv.index(BASE_SHA)] = "invalid"
    assert main(argv, run_command=run, write_stderr=errors.append) == 2
    assert errors and git.mutations == []


def test_cli_debug_exposes_proof_error(tmp_path: Path) -> None:
    from agent_work.cli import main

    _request(tmp_path)
    git = FakeGit(stale="head")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        return git(tmp_path, command[5:])

    with pytest.raises(_module().InteractiveCommitError):
        main([*_cli_argv(tmp_path), "--debug"], run_command=run)


def test_real_git_adapter_discards_ambient_config_controls(tmp_path: Path) -> None:
    module = _module()
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        calls.append((command, kwargs["environment"]))
        return CommandResult(0, "", "")

    poisoned = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "evil",
        "GIT_CONFIG_PARAMETERS": "evil",
        "GIT_CONFIG_GLOBAL": "/tmp/evil",
        "GIT_CONFIG_SYSTEM": "/tmp/evil",
        "GIT_DIR": "/tmp/other",
        "GIT_INDEX_FILE": "/tmp/other/index",
        "GIT_AUTHOR_NAME": "Approved Author",
    }
    module.run_git(
        tmp_path, ("status", "--porcelain=v1"), run_command=run, environment=poisoned
    )

    command, environment = calls[0]
    assert command[-2:] == ("status", "--porcelain=v1")
    assert "core.fsmonitor=false" in command
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_AUTHOR_NAME"] == "Approved Author"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert not any(name.startswith("GIT_CONFIG_KEY_") for name in environment)
    assert "GIT_CONFIG_COUNT" not in environment
    assert "GIT_CONFIG_PARAMETERS" not in environment
    assert "GIT_DIR" not in environment
    assert "GIT_INDEX_FILE" not in environment


def test_commit_adapter_overrides_ambient_identity(tmp_path: Path) -> None:
    module = _module()
    calls: list[dict[str, str]] = []

    def run(_command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        calls.append(kwargs["environment"])
        return CommandResult(0, NEW_SHA + "\n", "")

    identity = module.CommitIdentity(
        author_name="Passing Author",
        author_email="author@example.invalid",
        author_date="2026-09-27T08:00:00+00:00",
        committer_name="Passing Committer",
        committer_email="committer@example.invalid",
        committer_date="2026-09-27T08:01:00+00:00",
    )
    result = module.run_commit_git(
        tmp_path,
        ("commit-tree", JUDGE_TREE, "-p", BASE_SHA),
        identity,
        run_command=run,
        environment={
            "GIT_AUTHOR_NAME": "Wrong Author",
            "GIT_COMMITTER_NAME": "Wrong Committer",
            "GIT_CONFIG_GLOBAL": "/tmp/untrusted",
        },
    )

    assert result.stdout.strip() == NEW_SHA
    assert calls == [
        {
            "GIT_AUTHOR_NAME": identity.author_name,
            "GIT_AUTHOR_EMAIL": identity.author_email,
            "GIT_AUTHOR_DATE": identity.author_date,
            "GIT_COMMITTER_NAME": identity.committer_name,
            "GIT_COMMITTER_EMAIL": identity.committer_email,
            "GIT_COMMITTER_DATE": identity.committer_date,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
    ]
