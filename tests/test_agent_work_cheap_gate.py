# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the exact staged-candidate cheap gate."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from agent_work.cheap_gate import run_candidate_gate
from agent_work.domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from agent_work.repository import CommandResult, run_subprocess


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo), *arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _tiny_repo(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "tracked.txt").write_text("base\n")
    _git(source, "add", "tracked.txt")
    _git(
        source,
        "-c",
        "user.name=Tester",
        "-c",
        "user.email=tester@example.invalid",
        "commit",
        "-m",
        "base",
    )
    head = _git(source, "rev-parse", "HEAD")
    (source / "tracked.txt").write_text("staged\n")
    _git(source, "add", "tracked.txt")
    tree = _git(source, "write-tree")
    (source / "tracked.txt").write_text("unstaged\n")
    (source / "untracked.txt").write_text("untracked\n")
    return source, head, tree


def test_candidate_gate_runs_fixed_read_only_checks_in_exact_tree_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_candidate_gate = importlib.import_module(
        "agent_work.cheap_gate"
    ).run_candidate_gate

    tree = "a" * 40
    snapshot = tmp_path / "candidate"
    snapshot.mkdir()
    (snapshot / ".git").mkdir()
    (snapshot / "check.sh").write_text("#!/usr/bin/env bash\nprintf 'ok\\n'\n")
    snapshots: list[str] = []
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    for name in (
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "AGENT_WORK_GIT_CREDENTIAL_HELPER",
    ):
        monkeypatch.setenv(name, "private-credential")

    @contextmanager
    def candidate_snapshot(candidate_tree: str) -> Iterator[Path]:
        snapshots.append(candidate_tree)
        yield snapshot

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        assert kwargs.get("timeout_seconds") == HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
        if command == ("git", "ls-files", "-z"):
            return CommandResult(0, "check.sh\0", "")
        if command == ("git", "write-tree"):
            return CommandResult(0, tree, "")
        if command[0] == "git":
            return CommandResult(0, "", "")
        environment = kwargs.get("environment")
        assert isinstance(environment, dict)
        assert "private-credential" not in environment.values()
        commands.append((command, kwargs))
        return CommandResult(0, "", "")

    result = run_candidate_gate(
        tree,
        candidate_snapshot=candidate_snapshot,
        current_staged_tree=lambda: tree,
        run_command=run,
    )

    argv = [command for command, _kwargs in commands]
    assert result.returncode == 0
    assert snapshots == [tree]
    assert len(argv) == 7
    assert any("yamllint" in command for command in argv)
    shellcheck = next(command for command in argv if "shellcheck" in command)
    assert any(argument.endswith("check.sh") for argument in shellcheck)
    assert any("agent_work.readability" in command for command in argv)
    assert any("ty" in command and "check" in command for command in argv)
    kingfisher = next(command for command in argv if "kingfisher" in command)
    assert {"--staged", "--no-validate", "--no-update-check"}.issubset(kingfisher)
    assert any("ruff" in command and "check" in command for command in argv)
    assert any("ruff" in command and "format" in command for command in argv)
    assert all("--fix" not in command for command in argv)
    assert all("--no-cache" in command for command in argv if "ruff" in command)
    assert all(
        kwargs.get("current_directory") == snapshot for _command, kwargs in commands
    )


def test_candidate_gate_stops_after_first_failed_check(tmp_path: Path) -> None:
    run_candidate_gate = importlib.import_module(
        "agent_work.cheap_gate"
    ).run_candidate_gate

    tree = "a" * 40
    (tmp_path / ".git").mkdir()
    commands: list[tuple[str, ...]] = []

    @contextmanager
    def candidate_snapshot(_tree: str) -> Iterator[Path]:
        yield tmp_path

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command == ("git", "write-tree"):
            return CommandResult(0, tree, "")
        if command[0] == "git":
            return CommandResult(0, "", "")
        commands.append(command)
        return CommandResult(1, "", "")

    result = run_candidate_gate(
        tree,
        candidate_snapshot=candidate_snapshot,
        current_staged_tree=lambda: tree,
        run_command=run,
    )

    assert result.returncode != 0
    assert len(commands) == 1


def test_candidate_snapshot_creates_private_alternates_and_binds_source_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_snapshot = importlib.import_module(
        "agent_work.cheap_gate"
    ).candidate_snapshot
    source = tmp_path / "source"
    objects = source / ".git" / "objects"
    objects.mkdir(parents=True)
    source_head = "b" * 40
    tree = "a" * 40
    bound_head: list[str] = []
    hostile = str(tmp_path / "hostile")
    for name in (
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_TEMPLATE_DIR",
        "GIT_INDEX_FILE",
        "GIT_WORK_TREE",
        "GIT_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GITLAB_TOKEN",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "AGENT_WORK_GIT_CREDENTIAL_HELPER",
    ):
        monkeypatch.setenv(name, hostile)

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        assert kwargs.get("timeout_seconds") == HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        assert hostile not in environment.values()
        directory = kwargs["current_directory"]
        if directory == source and command == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, source_head, "")
        if directory == source and command == ("git", "rev-parse", "--git-common-dir"):
            return CommandResult(0, ".git", "")
        assert isinstance(directory, Path)
        if command == ("git", "init", "--quiet"):
            (directory / ".git" / "objects").mkdir(parents=True)
        elif command[:3] == ("git", "update-ref", "HEAD"):
            alternate = directory / ".git" / "objects" / "info" / "alternates"
            assert alternate.read_text() == f"{objects.resolve()}\n"
            bound_head.append(command[-1])
        elif command == ("git", "write-tree"):
            return CommandResult(0, tree, "")
        return CommandResult(0, "", "")

    with candidate_snapshot(tree, source, run) as snapshot:
        assert (snapshot / ".git" / "objects" / "info" / "alternates").is_file()

    assert bound_head == [source_head]


def test_candidate_gate_skips_shellcheck_when_snapshot_has_no_shell_files(
    tmp_path: Path,
) -> None:
    run_candidate_gate = importlib.import_module(
        "agent_work.cheap_gate"
    ).run_candidate_gate
    tree = "a" * 40
    (tmp_path / ".git").mkdir()
    commands: list[tuple[str, ...]] = []

    @contextmanager
    def snapshot(_tree: str) -> Iterator[Path]:
        yield tmp_path

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command == ("git", "write-tree"):
            return CommandResult(0, tree, "")
        if command[0] == "git":
            return CommandResult(0, "", "")
        commands.append(command)
        return CommandResult(0, "", "")

    result = run_candidate_gate(
        tree,
        candidate_snapshot=snapshot,
        current_staged_tree=lambda: tree,
        run_command=run,
    )

    assert result.returncode == 0
    assert not any("shellcheck" in command for command in commands)


def test_candidate_gate_rejects_snapshot_without_private_git(tmp_path: Path) -> None:
    run_candidate_gate = importlib.import_module(
        "agent_work.cheap_gate"
    ).run_candidate_gate
    tree = "a" * 40
    tools: list[tuple[str, ...]] = []

    @contextmanager
    def snapshot(_tree: str) -> Iterator[Path]:
        yield tmp_path

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        tools.append(command)
        return CommandResult(0, "", "")

    result = run_candidate_gate(
        tree,
        candidate_snapshot=snapshot,
        current_staged_tree=lambda: tree,
        run_command=run,
    )

    assert result.returncode != 0
    assert tools == []


def test_candidate_snapshot_uses_real_requested_tree_and_source_head(
    tmp_path: Path,
) -> None:
    candidate_snapshot = importlib.import_module(
        "agent_work.cheap_gate"
    ).candidate_snapshot
    source, head, tree = _tiny_repo(tmp_path)

    with candidate_snapshot(tree, source, run_subprocess) as snapshot:
        assert (snapshot / "tracked.txt").read_text() == "staged\n"
        assert not (snapshot / "untracked.txt").exists()
        assert _git(snapshot, "write-tree") == tree
        assert _git(snapshot, "rev-parse", "HEAD") == head
        assert (snapshot / ".git" / "objects" / "info" / "alternates").is_file()

    assert (source / "tracked.txt").read_text() == "unstaged\n"
    assert _git(source, "write-tree") == tree
    with (
        pytest.raises(RuntimeError, match="snapshot|tree"),
        candidate_snapshot("f" * 40, source, run_subprocess),
    ):
        pytest.fail("unknown staged tree was accepted")


@pytest.mark.parametrize("mutation", ("tracked", "untracked", "missing_git"))
def test_candidate_gate_rejects_private_snapshot_mutation(
    tmp_path: Path, mutation: str
) -> None:
    module = importlib.import_module("agent_work.cheap_gate")
    source, _head, tree = _tiny_repo(tmp_path)
    tools: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if command[0] == "git":
            return cast(Callable[..., CommandResult], run_subprocess)(command, **kwargs)
        tools.append(command)
        snapshot = kwargs["current_directory"]
        assert isinstance(snapshot, Path)
        if mutation == "tracked":
            (snapshot / "tracked.txt").write_text("changed\n")
        elif mutation == "untracked":
            (snapshot / "extra.txt").write_text("extra\n")
        else:
            shutil.rmtree(snapshot / ".git")
        return CommandResult(0, "", "")

    result = module.run_candidate_gate(
        tree,
        candidate_snapshot=lambda selected: module.candidate_snapshot(
            selected, source, run_subprocess
        ),
        current_staged_tree=lambda: _git(source, "write-tree"),
        run_command=run,
    )

    assert result.returncode != 0
    assert len(tools) == 1


@dataclass
class CheapGateObservation:
    """Shared public command/snapshot observations for original Ruff failures."""

    snapshot: Path
    tree: str
    output: str = (
        "src/widget.py:1:1: I001 [*] PRIVATE_TOKEN=discarded\n"
        "tests/test_widget.py:7:2: I001 [*] private source context\n"
        "Found 2 errors.\n[*] 2 fixable with the `--fix` option.\n"
    )
    returncode: int = 1
    stderr: str = ""
    tracked: tuple[str, ...] = ("src/widget.py", "tests/test_widget.py")
    failed_tool: str = "ruff"
    mutation: str = ""
    commands: list[tuple[str, ...]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    current_tree: str = ""

    def __post_init__(self) -> None:
        self.current_tree = self.tree
        (self.snapshot / ".git").mkdir(parents=True)
        for relative in self.tracked:
            path = self.snapshot / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("import os\n")

    @contextmanager
    def open_snapshot(self, tree: str) -> Iterator[Path]:
        """Observe exact candidate selection and snapshot release."""
        assert tree == self.tree
        self.events.append("opened")
        try:
            yield self.snapshot
        finally:
            self.events.append("closed")

    def run(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        """Admit ordinary checks and return the original failing child output."""
        self.commands.append(command)
        if command == ("git", "ls-files", "-z"):
            return CommandResult(0, "\0".join(self.tracked) + "\0", "")
        if command == ("git", "write-tree"):
            return CommandResult(0, self.tree, "")
        if command[0] == "git":
            changed = self.mutation == "snapshot" and "failed" in self.events
            return CommandResult(0, " M src/widget.py" if changed else "", "")
        if self.failed_tool in command:
            self.events.append("failed")
            if self.mutation == "staged":
                self.current_tree = "b" * 40
            return CommandResult(self.returncode, self.output, self.stderr)
        return CommandResult(0, "", "")

    def gate(self, tree: str) -> CommandResult:
        """Run the production gate using the same boundaries for all cases."""
        return run_candidate_gate(
            tree,
            candidate_snapshot=self.open_snapshot,
            current_staged_tree=lambda: self.current_tree,
            run_command=self.run,
            environment={},
        )


@pytest.mark.parametrize("absolute", (False, True))
def test_original_ruff_locations_survive_snapshot_release_without_messages(
    tmp_path: Path, *, absolute: bool
) -> None:
    observation = CheapGateObservation(tmp_path, "a" * 40)
    if absolute:
        observation.output = observation.output.replace(
            "src/widget.py:", f"{tmp_path}/src/widget.py:"
        ).replace("tests/test_widget.py:", f"{tmp_path}/tests/test_widget.py:")

    result = observation.gate(observation.tree)

    assert result.returncode == 1
    assert observation.events == ["opened", "failed", "closed"]
    assert result.stderr == "ruff-check failed"
    assert result.stdout, "original Ruff I001 locations were discarded"
    assert json.loads(result.stdout) == {
        "candidate_tree": observation.tree,
        "check_id": "ruff-check",
        "diagnostic_lines": [
            "src/widget.py:1:1: I001",
            "tests/test_widget.py:7:2: I001",
        ],
    }
    ruff = [command for command in observation.commands if "ruff" in command]
    assert ruff == [
        (
            "uv",
            "run",
            "--no-sync",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=concise",
            ".",
        )
    ]
    assert observation.commands.count(("git", "ls-files", "-z")) == 1
    assert "PRIVATE_TOKEN" not in result.stdout
    assert str(tmp_path) not in result.stdout


@pytest.mark.parametrize(
    "output",
    (
        "",
        "Found 0 errors.\n",
        "private free text\n",
        "src/widget.py:1:1: F401 private\n",
        "src/widget.py:1:1: I001 private\nunknown.py:2:1: I001 private\n",
        "src/widget.py:1:1: I001 private\nprivate free text\n",
        "src/widget.py:0:1: I001 private\n",
        "src/widget.py:01:1: I001 private\n",
        "src/widget.py:True:1: I001 private\n",
        "src/widget.py:1:False: I001 private\n",
        "src/widget.py:1000000000:1: I001 private\n",
        "src/widget.py:1:-1: I001 private\n",
        "unknown.py:1:1: I001 private\n",
        "/foreign/src/widget.py:1:1: I001 private\n",
        "../src/widget.py:1:1: I001 private\n",
        "src/../src/widget.py:1:1: I001 private\n",
        "./src/widget.py:1:1: I001 private\n",
        "src//widget.py:1:1: I001 private\n",
        "src\\widget.py:1:1: I001 private\n",
        "\x1b[31msrc/widget.py:1:1: I001 private\n",
        "src/widget.py:1:1: I001 private\x00text\n",
        "src/widget.py:1:1: I001 " + "é" * 2048,
        "\n".join(f"src/widget.py:{line}:1: I001 private" for line in range(1, 6)),
    ),
)
def test_original_ruff_untrusted_or_unbounded_output_has_no_projection(
    tmp_path: Path, output: str
) -> None:
    observation = CheapGateObservation(tmp_path, "a" * 40, output=output)

    result = observation.gate(observation.tree)

    assert result == CommandResult(1, "", "ruff-check failed")
    assert observation.events[-1] == "closed"
    assert not any("format" in command for command in observation.commands)


@pytest.mark.parametrize(
    ("returncode", "stderr", "failed_tool", "failure"),
    (
        (2, "", "ruff", "ruff-check"),
        (1, "secret", "ruff", "ruff-check"),
        (1, "", "ty", "ty"),
        (1, "", "yamllint", "yamllint"),
    ),
)
def test_original_ruff_shape_from_setup_or_other_tool_has_no_projection(
    tmp_path: Path, returncode: int, stderr: str, failed_tool: str, failure: str
) -> None:
    observation = CheapGateObservation(
        tmp_path,
        "a" * 40,
        returncode=returncode,
        stderr=stderr,
        failed_tool=failed_tool,
    )
    assert observation.gate(observation.tree) == CommandResult(
        returncode, "", f"{failure} failed"
    )


@pytest.mark.parametrize("topology", ("leaf", "ancestor", "untracked", "directory"))
def test_original_ruff_location_requires_canonical_tracked_regular_membership(
    tmp_path: Path, topology: str
) -> None:
    observation = CheapGateObservation(tmp_path / "snapshot", "a" * 40)
    widget = observation.snapshot / "src/widget.py"
    if topology == "leaf":
        widget.unlink()
        widget.symlink_to(observation.snapshot / "tests/test_widget.py")
    elif topology == "ancestor":
        outside = tmp_path / "outside"
        widget.parent.rename(outside)
        widget.parent.symlink_to(outside, target_is_directory=True)
    elif topology == "untracked":
        observation.tracked = ("tests/test_widget.py",)
    else:
        widget.unlink()
        widget.mkdir()
    assert observation.gate(observation.tree) == CommandResult(
        1, "", "ruff-check failed"
    )


@pytest.mark.parametrize("mutation", ("snapshot", "staged"))
def test_original_ruff_diagnostics_cannot_override_candidate_mutation(
    tmp_path: Path, mutation: str
) -> None:
    observation = CheapGateObservation(tmp_path, "a" * 40, mutation=mutation)
    result = observation.gate(observation.tree)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "changed during cheap gate" in result.stderr
