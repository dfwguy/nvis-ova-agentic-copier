# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for a controller-owned, native-hook Green transaction."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_work.domain import DEFAULT_MAX_ACTIVATION_SECONDS
from agent_work.red_commit import RedCommitProof

BASE = "a" * 40
RED = "b" * 40
RED_TREE = "c" * 40
GREEN_TREE = "d" * 40
GREEN = "e" * 40
OTHER = "f" * 40
BRANCH = "agent/issue-81"
PATHS = ("tests/test_alpha.py", "tests/test_beta.py")
RED_REF = f"refs/agent-work/red-commits/81/{BASE}"
GREEN_REF = f"refs/agent-work/green-commits/81/{BASE}"
MESSAGE = "Implement issue #81"
RED_PROOF = RedCommitProof(RED, BASE, RED_TREE, RED_REF, PATHS)
TIMINGS: tuple[dict[str, object], ...] = (
    {"environment": "py311", "timing_complete": True, "over_100ms_cases": []},
    {"environment": "py314", "timing_complete": True, "over_100ms_cases": []},
)


def _module() -> Any:
    return importlib.import_module("agent_work.green_commit")


class FakeGreenDependencies:
    """Observe a worker checkout and the two retained transaction refs."""

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.branch = BRANCH
        self.head = BASE
        self.index = GREEN_TREE
        self.worktree_tree = GREEN_TREE
        self.red_ref: str | None = RED
        self.green_ref: str | None = None
        self.red_parent = BASE
        self.red_tree = RED_TREE
        self.green_parent = RED
        self.green_tree = GREEN_TREE
        self.green_message = MESSAGE
        self.red_blobs = {path: str(i) * 40 for i, path in enumerate(PATHS, 2)}
        self.green_blobs = self.red_blobs.copy()
        self.isolated_error: RuntimeError | None = None
        self.after_isolated: Callable[[], None] | None = None
        self.cas_success = True
        self.fail_branch_cas = False
        self.after_failed_cas: Callable[[], None] | None = None
        self.timing_status = "complete"
        self.junit_timings: tuple[dict[str, object], ...] = TIMINGS
        self.calls: list[tuple[str, object]] = []

    def worker_state(self, worktree: Path) -> tuple[str, str, str, str]:
        self.calls.append(("worker_state", worktree))
        return self.branch, self.head, self.index, self.worktree_tree

    def read_ref(self, ref: str) -> str | None:
        self.calls.append(("read_ref", ref))
        return self.red_ref if ref == RED_REF else self.green_ref

    def commit_identity(self, oid: str) -> tuple[str, str, str]:
        self.calls.append(("commit_identity", oid))
        if oid == RED:
            return self.red_parent, self.red_tree, "Accepted failing tests"
        if oid == OTHER:
            return BASE, OTHER, "Conflicting Green commit"
        return self.green_parent, self.green_tree, self.green_message

    def blob_oid(self, tree: str, path: str) -> str:
        self.calls.append(("blob_oid", (tree, path)))
        if tree == OTHER:
            return OTHER
        return (self.red_blobs if tree in (RED_TREE, RED) else self.green_blobs)[path]

    def isolated_commit(self, red: str, tree: str, message: str) -> Any:
        self.calls.append(("isolated_commit", (red, tree, message)))
        if self.after_isolated is not None:
            self.after_isolated()
        if self.isolated_error is not None:
            raise self.isolated_error
        return _module().IsolatedGreenCommit(
            GREEN, self.timing_status, self.junit_timings
        )

    def cas_ref(self, ref: str, oid: str, old: str | None) -> bool:
        self.calls.append(("cas_ref", (ref, oid, old)))
        if self.fail_branch_cas and ref == f"refs/heads/{BRANCH}":
            return False
        if self.cas_success:
            if ref == GREEN_REF:
                self.green_ref = oid
            else:
                self.head = oid
        elif self.after_failed_cas is not None:
            self.after_failed_cas()
        return self.cas_success

    def value(self) -> Any:
        return _module().GreenCommitDependencies(
            worker_state=self.worker_state,
            read_ref=self.read_ref,
            commit_identity=self.commit_identity,
            blob_oid=self.blob_oid,
            isolated_commit=self.isolated_commit,
            cas_ref=self.cas_ref,
        )


def _create(fake: FakeGreenDependencies, *, branch: str = BRANCH) -> Any:
    return _module().create_green_commit(
        issue_iid=81,
        worktree=fake.worktree,
        branch=branch,
        base_sha=BASE,
        candidate_tree=GREEN_TREE,
        red_proof=RED_PROOF,
        message=MESSAGE,
        dependencies=fake.value(),
    )


def _install_hook(worker: Path) -> None:
    hooks = worker / ".git" / "hooks"
    hooks.mkdir(parents=True)
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)


def test_green_commit_preserves_red_tests_and_advances_worker_only_after_verified_commit(
    tmp_path: Path,
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")

    proof = _create(fake)

    assert (proof.commit_sha, proof.red_sha, proof.tree_oid, proof.ref) == (
        GREEN,
        RED,
        GREEN_TREE,
        GREEN_REF,
    )
    assert (fake.head, fake.green_ref, fake.index, fake.worktree_tree) == (
        GREEN,
        GREEN,
        GREEN_TREE,
        GREEN_TREE,
    )
    assert proof.timing_status == "complete" and proof.junit_timings == TIMINGS
    assert fake.calls.index(("commit_identity", GREEN)) < fake.calls.index(
        ("cas_ref", (GREEN_REF, GREEN, None))
    )
    assert fake.calls.index(("cas_ref", (GREEN_REF, GREEN, None))) < fake.calls.index(
        ("cas_ref", (f"refs/heads/{BRANCH}", GREEN, BASE))
    )
    assert all(("blob_oid", (GREEN, path)) in fake.calls for path in PATHS)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("branch", "agent/issue-82"),
        ("head", OTHER),
        ("worktree_tree", OTHER),
        ("red_ref", OTHER),
        ("red_parent", OTHER),
        ("red_tree", OTHER),
    ),
)
def test_green_commit_rejects_drift_before_isolated_commit(
    tmp_path: Path, field: str, value: str
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    setattr(fake, field, value)

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


@pytest.mark.parametrize("branch", ("agent/issue-82", "agent/custom"))
def test_green_commit_rejects_supplied_branch_mismatched_to_issue_before_commit(
    tmp_path: Path, branch: str
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.branch = branch

    with pytest.raises(_module().GreenCommitError):
        _create(fake, branch=branch)

    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


@pytest.mark.parametrize(
    "drift", ("green_parent", "green_tree", "green_message", "green_blobs")
)
def test_green_commit_rejects_changed_commit_or_red_test_blob_before_branch_update(
    tmp_path: Path, drift: str
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    setattr(
        fake,
        drift,
        {PATHS[0]: OTHER, PATHS[1]: fake.red_blobs[PATHS[1]]}
        if drift == "green_blobs"
        else OTHER,
    )

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert fake.head == BASE
    assert not any(name == "cas_ref" for name, _ in fake.calls)


def test_green_commit_hook_failure_leaves_worker_base_and_no_green_ref(
    tmp_path: Path,
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.isolated_error = RuntimeError("pre-commit failed")

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert fake.head == BASE and fake.green_ref is None
    assert not any(name == "cas_ref" for name, _ in fake.calls)


@pytest.mark.parametrize("drift", ("head", "index", "worktree_tree"))
def test_green_commit_rejects_worker_drift_during_hook_before_branch_update(
    tmp_path: Path, drift: str
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.after_isolated = lambda: setattr(fake, drift, OTHER)

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert fake.green_ref is None
    assert not any(name == "cas_ref" for name, _ in fake.calls)


def test_green_commit_failed_branch_cas_retains_verified_ref_for_recovery(
    tmp_path: Path,
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.fail_branch_cas = True

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert fake.head == BASE and fake.green_ref == GREEN
    assert sum(name == "isolated_commit" for name, _ in fake.calls) == 1


def test_green_commit_restart_reuses_exact_ref_without_second_hook_run(
    tmp_path: Path,
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.green_ref = GREEN

    proof = _create(fake)

    assert (proof.commit_sha, proof.red_sha, proof.tree_oid, proof.ref) == (
        GREEN,
        RED,
        GREEN_TREE,
        GREEN_REF,
    )
    assert proof.timing_status == "unknown" and proof.junit_timings == ()
    assert fake.head == GREEN
    assert not any(name == "isolated_commit" for name, _ in fake.calls)


def test_green_commit_replay_after_success_is_idempotent(tmp_path: Path) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    first = _create(fake)

    second = _create(fake)

    assert (
        second.commit_sha,
        second.red_sha,
        second.tree_oid,
        second.ref,
    ) == (first.commit_sha, first.red_sha, first.tree_oid, first.ref)
    assert second.timing_status == "unknown" and second.junit_timings == ()
    assert sum(name == "isolated_commit" for name, _ in fake.calls) == 1


def test_green_commit_conflicting_ref_fails_closed(tmp_path: Path) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.green_ref = OTHER

    with pytest.raises(_module().GreenCommitError):
        _create(fake)

    assert fake.head == BASE and fake.green_ref == OTHER
    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


@pytest.mark.parametrize("timing_status", ("unknown", "complete"))
def test_green_commit_incomplete_timing_is_unknown_without_blocking_valid_commit(
    tmp_path: Path, timing_status: str
) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.timing_status = timing_status
    fake.junit_timings = () if timing_status == "unknown" else TIMINGS

    proof = _create(fake)

    assert proof.timing_status == timing_status
    assert proof.junit_timings == (() if timing_status == "unknown" else TIMINGS)
    assert fake.head == GREEN


@pytest.mark.parametrize(
    ("porcelain", "clean_against_index"),
    (
        ("R  dest\0old\0", True),
        ("C  dest\0old\0", True),
        ("R  dest\0x\0", True),
        ("C  dest\0x\0", True),
        (" M path\0", False),
        ("?? path\0", False),
    ),
)
def test_green_commit_worker_state_parses_nul_status_records(
    tmp_path: Path, porcelain: str, clean_against_index: bool
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository

    worker = tmp_path / "worker"
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        args = command[3:]
        if args == ("branch", "--show-current"):
            return CommandResult(0, f"{BRANCH}\n", "")
        if args == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{BASE}\n", "")
        if args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        if args == ("status", "--porcelain=v1", "-z"):
            return CommandResult(0, porcelain, "")
        raise AssertionError(args)

    state = (
        GitRepository(config, run)
        .green_commit_dependencies(worker, tmp_path / "isolated")
        .worker_state(worker)
    )

    assert state == (
        BRANCH,
        BASE,
        GREEN_TREE,
        GREEN_TREE if clean_against_index else "",
    )


def test_repository_green_commit_uses_one_native_hook_and_preserves_worker(
    tmp_path: Path,
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository

    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    _install_hook(worker)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    commands: list[tuple[str, ...]] = []
    commit_timeouts: list[float | None] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        commands.append(command)
        if command[3:4] == ("commit",):
            commit_timeouts.append(kwargs.get("timeout_seconds"))
        if command[3:] == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if command[3:] == ("config", "--get", "core.hooksPath"):
            return CommandResult(1, "", "")
        if command[-1:] == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        if command[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{GREEN}\n", "")
        return CommandResult(0, "", "")

    result = GitRepository(config, run).create_isolated_green_commit(
        worktree=worker,
        isolated_worktree=isolated,
        red_sha=RED,
        candidate_tree=GREEN_TREE,
        message=MESSAGE,
    )

    assert result.commit_sha == GREEN
    assert commit_timeouts == [DEFAULT_MAX_ACTIVATION_SECONDS]
    assert ("worktree", "add", "--detach", str(isolated), RED) in [
        command[3:] for command in commands
    ]
    assert ("read-tree", "--reset", "-u", GREEN_TREE) in [
        command[3:] for command in commands
    ]
    assert sum(command[3:4] == ("commit",) for command in commands) == 1
    assert any(
        command[1:3] == ("-C", str(isolated))
        and command[3:6] == ("commit", "-m", MESSAGE)
        for command in commands
    )
    assert not any(command[3:5] == ("hook", "run") for command in commands)
    assert not any("--no-verify" in command for command in commands)
    assert not any(
        command[1:3] == ("-C", str(worker))
        and command[3] in {"add", "read-tree", "commit", "reset"}
        for command in commands
    )


def test_repository_green_commit_rejects_present_malformed_junit_after_successful_hook(
    tmp_path: Path,
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository, RepositoryError

    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    _install_hook(worker)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    committed: list[bool] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        args = command[3:]
        if args == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if args == ("config", "--get", "core.hooksPath"):
            return CommandResult(1, "", "")
        if args[:3] == ("worktree", "add", "--detach"):
            isolated.mkdir()
        elif args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        elif args[:1] == ("commit",):
            committed.append(True)
            (isolated / "test-results-py311.xml").write_text(
                "<testsuite>", encoding="utf-8"
            )
        elif args == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{GREEN}\n", "")
        elif args[:3] == ("worktree", "remove", "--force"):
            (isolated / "test-results-py311.xml").unlink()
            isolated.rmdir()
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError):
        GitRepository(config, run).create_isolated_green_commit(
            worktree=worker,
            isolated_worktree=isolated,
            red_sha=RED,
            candidate_tree=GREEN_TREE,
            message=MESSAGE,
        )

    assert committed == [True]
    assert not isolated.exists()


def test_repository_green_commit_removes_red_only_file_before_hook(
    tmp_path: Path,
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository

    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    _install_hook(worker)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    removed = isolated / "red-only.py"
    hook_observations: list[bool] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        args = command[3:]
        if args == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if args == ("config", "--get", "core.hooksPath"):
            return CommandResult(1, "", "")
        if args[:3] == ("worktree", "add", "--detach"):
            isolated.mkdir()
            removed.write_text("old Red content", encoding="utf-8")
        elif args == ("read-tree", "--reset", "-u", GREEN_TREE):
            removed.unlink()
        elif args[:3] == ("worktree", "remove", "--force"):
            removed.unlink(missing_ok=True)
            isolated.rmdir()
        elif args[:1] == ("commit",):
            hook_observations.append(removed.exists())
            if removed.exists():
                return CommandResult(1, "", "hook saw deleted Red file")
        elif args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        elif args == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{GREEN}\n", "")
        return CommandResult(0, "", "")

    result = GitRepository(config, run).create_isolated_green_commit(
        worktree=worker,
        isolated_worktree=isolated,
        red_sha=RED,
        candidate_tree=GREEN_TREE,
        message=MESSAGE,
    )

    assert result.commit_sha == GREEN
    assert hook_observations == [False]
    assert not isolated.exists()


@pytest.mark.parametrize("failure", ("nonzero", "timeout"))
def test_repository_green_commit_hook_failure_cleans_isolated_worktree(
    tmp_path: Path, failure: str
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import (
        CommandResult,
        CommandTimeoutError,
        GitRepository,
        RepositoryError,
    )

    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    _install_hook(worker)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(command[3:])
        args = command[3:]
        if args == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if args == ("config", "--get", "core.hooksPath"):
            return CommandResult(1, "", "")
        if args[:3] == ("worktree", "add", "--detach"):
            isolated.mkdir()
        elif args[:3] == ("worktree", "remove", "--force"):
            isolated.rmdir()
        elif args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        elif args[:1] == ("commit",):
            if failure == "timeout":
                raise CommandTimeoutError("simulated hook timeout")
            return CommandResult(1, "", "simulated hook failure")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError):
        GitRepository(config, run).create_isolated_green_commit(
            worktree=worker,
            isolated_worktree=isolated,
            red_sha=RED,
            candidate_tree=GREEN_TREE,
            message=MESSAGE,
        )

    assert sum(args[:1] == ("commit",) for args in commands) == 1
    assert ("worktree", "remove", "--force", str(isolated)) in commands
    assert not isolated.exists()


@pytest.mark.parametrize("hook_state", ("missing", "disabled"))
def test_repository_green_commit_requires_an_enabled_installed_hook(
    tmp_path: Path, hook_state: str
) -> None:
    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository, RepositoryError

    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    hooks = worker / ".git" / "hooks"
    hooks.mkdir(parents=True)
    if hook_state == "disabled":
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(command[3:])
        args = command[3:]
        if args == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if args[:3] == ("worktree", "add", "--detach"):
            isolated.mkdir()
        elif args[:3] == ("worktree", "remove", "--force"):
            isolated.rmdir()
        elif args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        elif args[:3] == ("config", "--get", "core.hooksPath"):
            if hook_state == "disabled":
                return CommandResult(0, "/dev/null\n", "")
            return CommandResult(1, "", "")
        elif args == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{GREEN}\n", "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError):
        GitRepository(config, run).create_isolated_green_commit(
            worktree=worker,
            isolated_worktree=isolated,
            red_sha=RED,
            candidate_tree=GREEN_TREE,
            message=MESSAGE,
        )

    assert not any(args[:1] == ("commit",) for args in commands)
    assert not isolated.exists()


def test_green_commit_over_100ms_case_is_advisory(tmp_path: Path) -> None:
    fake = FakeGreenDependencies(tmp_path / "worker")
    fake.junit_timings = (
        {
            "environment": "py311",
            "timing_complete": True,
            "over_100ms_cases": [
                {"name": "tests/test_alpha.py::test_slow", "seconds": 0.101}
            ],
        },
        TIMINGS[1],
    )

    proof = _create(fake)

    assert proof.junit_timings == fake.junit_timings
    assert fake.head == GREEN


@pytest.mark.parametrize(
    "size", [1_048_577, 4_194_304, 4_194_305, 6_291_456, 33_554_432, 33_554_433]
)
def test_repository_green_commit_reads_complete_xml_through_inclusive_cap(
    tmp_path: Path,
    size: int,
) -> None:
    from hashlib import sha256

    from agent_work.models import AgentWorkConfig
    from agent_work.repository import CommandResult, GitRepository, RepositoryError

    artifact = b"<testsuite tests='1'><testcase classname='tests.test_speed' name='test_case' time='0.12'/></testsuite>"
    artifact += b" " * (size - len(artifact))
    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    _install_hook(worker)
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    committed: list[bool] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        args = command[3:]
        if args == ("rev-parse", "--git-common-dir"):
            return CommandResult(0, f"{worker / '.git'}\n", "")
        if args == ("config", "--get", "core.hooksPath"):
            return CommandResult(1, "", "")
        if args[:3] == ("worktree", "add", "--detach"):
            isolated.mkdir()
        elif args == ("write-tree",):
            return CommandResult(0, f"{GREEN_TREE}\n", "")
        elif args[:1] == ("commit",):
            committed.append(True)
            for environment in ("py311", "py314"):
                (isolated / f"test-results-{environment}.xml").write_bytes(artifact)
        elif args == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{GREEN}\n", "")
        elif args[:3] == ("worktree", "remove", "--force"):
            for environment in ("py311", "py314"):
                (isolated / f"test-results-{environment}.xml").unlink()
            isolated.rmdir()
        return CommandResult(0, "", "")

    repository = GitRepository(config, run)
    if size > 33_554_432:
        with pytest.raises(RepositoryError, match="oversized"):
            repository.create_isolated_green_commit(
                worktree=worker,
                isolated_worktree=isolated,
                red_sha=RED,
                candidate_tree=GREEN_TREE,
                message=MESSAGE,
            )
    else:
        result = repository.create_isolated_green_commit(
            worktree=worker,
            isolated_worktree=isolated,
            red_sha=RED,
            candidate_tree=GREEN_TREE,
            message=MESSAGE,
        )
        assert result.timing_status == "complete"
        assert result.junit_timings == tuple(
            {
                "environment": environment,
                "sha256": sha256(artifact).hexdigest(),
                "test_count": 1,
                "timing_complete": True,
                "over_100ms_cases": [
                    {"name": "tests/test_speed.py::test_case", "seconds": 0.12}
                ],
            }
            for environment in ("py311", "py314")
        )
    assert committed == [True]
    assert not isolated.exists()
