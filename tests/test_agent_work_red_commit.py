# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for a separate, real Git Red commit transaction."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_work.models import AgentWorkConfig
from agent_work.red_checkpoint import RedCheckpointRequest
from agent_work.repository import CommandResult, GitRepository

BASE = "a" * 40
TREE = "b" * 40
COMMIT = "c" * 40
OTHER = "d" * 40
INDEX = "e" * 40
PATHS = ("tests/test_alpha.py", "tests/test_beta.py")
REF = f"refs/agent-work/red-commits/81/{BASE}"
REQUEST = RedCheckpointRequest(
    argv=("uv", "run", "pytest", *PATHS),
    expected_failure="assert failed",
    test_paths=PATHS,
)


def _module() -> Any:
    return importlib.import_module("agent_work.red_commit")


class FakeCommitDependencies:
    """Git facts and mutations supplied through the proposed transaction seam."""

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.head = BASE
        self.index = INDEX
        self.paths = PATHS
        self.candidate_blobs = {
            path: f"{number}" * 40 for number, path in enumerate(PATHS, 1)
        }
        self.commit_blobs = self.candidate_blobs.copy()
        self.commit_parent = BASE
        self.commit_tree = TREE
        self.ref: str | None = None
        self.cas_succeeds = True
        self.after_isolated: Callable[[], None] | None = None
        self.after_failed_cas: Callable[[], None] | None = None
        self.calls: list[tuple[str, object]] = []

    def current_head(self, worktree: Path) -> str:
        self.calls.append(("head", worktree))
        return self.head

    def current_index_tree(self, worktree: Path) -> str:
        self.calls.append(("index", worktree))
        return self.index

    def changed_paths(self, base: str, tree: str) -> tuple[str, ...]:
        self.calls.append(("paths", (base, tree)))
        return self.paths

    def blob_oid(self, tree: str, path: str) -> str:
        self.calls.append(("blob", (tree, path)))
        return (self.candidate_blobs if tree == TREE else self.commit_blobs)[path]

    def read_ref(self, ref: str) -> str | None:
        self.calls.append(("read_ref", ref))
        return self.ref

    def isolated_commit(self, base: str, tree: str, paths: tuple[str, ...]) -> str:
        self.calls.append(("isolated_commit", (base, tree, paths)))
        if self.after_isolated is not None:
            self.after_isolated()
        return COMMIT

    def commit_identity(self, oid: str) -> tuple[str, str]:
        self.calls.append(("commit_identity", oid))
        if oid == OTHER:
            return OTHER, self.commit_tree
        return self.commit_parent, self.commit_tree

    def cas_ref(self, ref: str, oid: str, old: str | None) -> bool:
        self.calls.append(("cas_ref", (ref, oid, old)))
        if self.cas_succeeds:
            self.ref = oid
        elif self.after_failed_cas is not None:
            self.after_failed_cas()
        return self.cas_succeeds

    def value(self) -> Any:
        return _module().RedCommitDependencies(
            current_head=self.current_head,
            current_index_tree=self.current_index_tree,
            changed_paths=self.changed_paths,
            blob_oid=self.blob_oid,
            read_ref=self.read_ref,
            isolated_commit=self.isolated_commit,
            commit_identity=self.commit_identity,
            cas_ref=self.cas_ref,
        )


def _create(fake: FakeCommitDependencies) -> Any:
    return _module().create_red_commit(
        issue_iid=81,
        worktree=fake.worktree,
        base_sha=BASE,
        candidate_tree=TREE,
        request=REQUEST,
        dependencies=fake.value(),
    )


def test_red_commit_binds_exact_parent_tree_and_blobs_without_changing_worker_index(
    tmp_path: Path,
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")

    proof = _create(fake)

    assert (proof.commit_sha, proof.base_sha, proof.tree_oid, proof.ref) == (
        COMMIT,
        BASE,
        TREE,
        REF,
    )
    assert fake.ref == COMMIT
    assert fake.head == BASE and fake.index == INDEX
    assert ("isolated_commit", (BASE, TREE, PATHS)) in fake.calls
    after_cas = fake.calls[fake.calls.index(("cas_ref", (REF, COMMIT, None))) + 1 :]
    assert ("read_ref", REF) in after_cas
    assert ("commit_identity", COMMIT) in after_cas
    assert all(("blob", (TREE, path)) in fake.calls for path in PATHS)
    assert all(("blob", (COMMIT, path)) in fake.calls for path in PATHS)


@pytest.mark.parametrize(
    ("mutation", "value"),
    (
        ("head", OTHER),
        ("paths", (PATHS[0], "src/unaccepted.py")),
        ("paths", (PATHS[0],)),
    ),
)
def test_red_commit_rejects_changed_base_or_unaccepted_candidate_paths_before_commit(
    tmp_path: Path, mutation: str, value: object
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    setattr(fake, mutation, value)

    with pytest.raises(_module().RedCommitError):
        _create(fake)

    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


@pytest.mark.parametrize("drift", ("commit_parent", "commit_tree", "commit_blobs"))
def test_red_commit_rejects_parent_tree_or_blob_drift_before_binding_ref(
    tmp_path: Path, drift: str
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    value = {PATHS[0]: OTHER, PATHS[1]: "2" * 40} if drift == "commit_blobs" else OTHER
    setattr(fake, drift, value)

    with pytest.raises(_module().RedCommitError):
        _create(fake)

    assert fake.ref is None
    assert not any(name == "cas_ref" for name, _ in fake.calls)


def test_red_commit_failed_cas_recovers_only_from_exact_readback(
    tmp_path: Path,
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    fake.cas_succeeds = False

    with pytest.raises(_module().RedCommitError):
        _create(fake)

    assert fake.ref is None
    assert ("read_ref", REF) in fake.calls


@pytest.mark.parametrize("changed", ("head", "index"))
def test_red_commit_rejects_worker_drift_during_isolated_commit_before_ref(
    tmp_path: Path, changed: str
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    fake.after_isolated = lambda: setattr(fake, changed, OTHER)

    with pytest.raises(_module().RedCommitError):
        _create(fake)

    assert fake.ref is None
    assert not any(name == "cas_ref" for name, _ in fake.calls)


def test_red_commit_cas_race_recovers_only_the_same_verified_commit(
    tmp_path: Path,
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    fake.cas_succeeds = False
    fake.after_failed_cas = lambda: setattr(fake, "ref", COMMIT)

    proof = _create(fake)

    assert proof.commit_sha == COMMIT
    assert fake.ref == COMMIT


def test_red_commit_restart_reuses_exact_ref_without_creating_another_commit(
    tmp_path: Path,
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    fake.ref = COMMIT

    proof = _create(fake)

    assert proof.commit_sha == COMMIT
    assert fake.ref == COMMIT
    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


def test_red_commit_conflicting_ref_fails_closed_without_rewriting_it(
    tmp_path: Path,
) -> None:
    fake = FakeCommitDependencies(tmp_path / "worker")
    fake.ref = OTHER

    with pytest.raises(_module().RedCommitError):
        _create(fake)

    assert fake.ref == OTHER
    assert not any(name in {"isolated_commit", "cas_ref"} for name, _ in fake.calls)


def test_repository_real_red_commit_uses_detached_checkout_and_git_commit_no_verify(
    tmp_path: Path,
) -> None:
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(command)
        if command[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{COMMIT}\n", "")
        return CommandResult(0, "", "")

    oid = GitRepository(config, run).create_isolated_red_commit(
        worktree=worker,
        isolated_worktree=isolated,
        base_sha=BASE,
        candidate_tree=TREE,
        test_paths=PATHS,
    )

    assert oid == COMMIT
    assert ("worktree", "add", "--detach", str(isolated), BASE) in [
        command[3:] for command in commands
    ]
    assert ("read-tree", TREE) in [command[3:] for command in commands]
    assert any(
        command[1:3] == ("-C", str(isolated))
        and command[3:5] == ("commit", "--no-verify")
        for command in commands
    )
    assert not any(
        command[1:3] == ("-C", str(worker))
        and command[3] in {"add", "read-tree", "commit", "reset"}
        for command in commands
    )
    assert not any("commit-tree" in command for command in commands)


def test_repository_red_commit_dependencies_bind_and_recover_exact_ref(
    tmp_path: Path,
) -> None:
    worker = tmp_path / "worker"
    isolated = tmp_path / "isolated"
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=worker,
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )
    ref_oid: str | None = None
    commands: list[tuple[str, ...]] = []
    blob_oids = {path: f"{number}" * 40 for number, path in enumerate(PATHS, 1)}

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        nonlocal ref_oid
        commands.append(command)
        args = command[3:]
        if args == ("rev-parse", "HEAD"):
            return CommandResult(
                0, f"{COMMIT if command[2] == str(isolated) else BASE}\n", ""
            )
        if args == ("write-tree",):
            return CommandResult(0, f"{INDEX}\n", "")
        if args == ("diff", "--name-only", "-z", "--no-renames", BASE, TREE):
            return CommandResult(0, "\0".join(PATHS) + "\0", "")
        if args[:1] == ("rev-parse",) and len(args) == 2 and ":" in args[1]:
            return CommandResult(0, f"{blob_oids[args[1].split(':', 1)[1]]}\n", "")
        if args == ("rev-parse", "-q", "--verify", REF):
            return (
                CommandResult(0, f"{ref_oid}\n", "")
                if ref_oid
                else CommandResult(1, "", "")
            )
        if args == ("rev-list", "--parents", "-n", "1", COMMIT):
            return CommandResult(0, f"{COMMIT} {BASE}\n", "")
        if args == ("rev-parse", f"{COMMIT}^{{tree}}"):
            return CommandResult(0, f"{TREE}\n", "")
        if args == ("update-ref", REF, COMMIT, ""):
            ref_oid = COMMIT
            return CommandResult(0, "", "")
        if args in {
            ("worktree", "add", "--detach", str(isolated), BASE),
            ("read-tree", TREE),
            ("checkout-index", "-a", "-f"),
            ("commit", "--no-verify", "-m", "Accepted failing tests"),
            ("worktree", "remove", "--force", str(isolated)),
        }:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected Git command: {args}")

    repository = GitRepository(config, run)
    kwargs = {
        "issue_iid": 81,
        "worktree": worker,
        "base_sha": BASE,
        "candidate_tree": TREE,
        "request": REQUEST,
        "dependencies": repository.red_commit_dependencies(worker, isolated),
    }
    first = _module().create_red_commit(**kwargs)
    restarted = _module().create_red_commit(**kwargs)
    assert first == restarted
    assert ref_oid == COMMIT
    assert sum(command[3] == "commit" for command in commands) == 1
    assert all(
        command[2] != str(worker)
        or command[3] not in {"add", "read-tree", "commit", "reset"}
        for command in commands
    )
