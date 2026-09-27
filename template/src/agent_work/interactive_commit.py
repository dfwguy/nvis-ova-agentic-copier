# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Guard one interactive passing commit over its original base."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, field_validator

from .ports import CommandRunner
from .repository import CommandResult, RepositoryError, run_subprocess


class InteractiveCommitError(RuntimeError):
    """The interactive commit proof or mutation failed."""


class InteractiveCommitRequest(BaseModel):
    """Explicit identities for one reviewed interactive candidate."""

    worktree: Path
    branch_ref: str
    base_sha: str
    red_ref: str
    red_sha: str
    passing_sha: str
    judge_tree: str
    message_file: Path

    @field_validator("base_sha", "red_sha", "passing_sha", "judge_tree")
    @classmethod
    def _object_id(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("object ID must be 40 lowercase hexadecimal characters")
        return value

    @field_validator("branch_ref")
    @classmethod
    def _branch_ref(cls, value: str) -> str:
        if re.fullmatch(r"refs/heads/clanker/[a-z0-9][a-z0-9/-]*", value) is None:
            raise ValueError("branch must be a clanker feature ref")
        return value

    @field_validator("red_ref")
    @classmethod
    def _red_ref(cls, value: str) -> str:
        if (
            re.fullmatch(r"refs/agent-work/red/[a-zA-Z0-9][a-zA-Z0-9/-]*", value)
            is None
        ):
            raise ValueError("Red ref must be an agent-work Red checkpoint ref")
        return value


@dataclass(frozen=True)
class InteractiveCommitDependencies:
    """Injected Git command boundary."""

    run_git: Callable[[Path, tuple[str, ...]], CommandResult]
    commit_tree: Callable[[Path, tuple[str, ...], CommitIdentity], CommandResult]


@dataclass(frozen=True)
class CommitIdentity:
    """Author and committer identity copied from the verified passing commit."""

    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    committer_date: str


@dataclass(frozen=True)
class InteractiveCommitReport:
    """Outcome of one preview or guarded compaction."""

    applied: bool
    new_commit: str | None


def run_git(
    worktree: Path,
    argv: tuple[str, ...],
    *,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] = os.environ,
) -> CommandResult:
    """Run Git in the explicit worktree with isolated configuration."""
    git_environment = {
        name: value
        for name, value in environment.items()
        if not name.startswith("GIT_CONFIG_")
        and name
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE",
            "GIT_GRAFT_FILE",
        }
    }
    git_environment["GIT_CONFIG_NOSYSTEM"] = "1"
    git_environment["GIT_CONFIG_GLOBAL"] = os.devnull
    git_environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = run_command(
        ("git", "-C", str(worktree), "-c", "core.fsmonitor=false", *argv),
        timeout_seconds=30,
        environment=git_environment,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def run_commit_git(
    worktree: Path,
    argv: tuple[str, ...],
    identity: CommitIdentity,
    *,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] = os.environ,
) -> CommandResult:
    """Create one commit using the verified passing commit's exact identity."""
    commit_environment = {
        **environment,
        "GIT_AUTHOR_NAME": identity.author_name,
        "GIT_AUTHOR_EMAIL": identity.author_email,
        "GIT_AUTHOR_DATE": identity.author_date,
        "GIT_COMMITTER_NAME": identity.committer_name,
        "GIT_COMMITTER_EMAIL": identity.committer_email,
        "GIT_COMMITTER_DATE": identity.committer_date,
    }
    return run_git(
        worktree,
        argv,
        run_command=run_command,
        environment=commit_environment,
    )


def _git(
    dependencies: InteractiveCommitDependencies,
    worktree: Path,
    argv: tuple[str, ...],
) -> str:
    try:
        result = dependencies.run_git(worktree, argv)
    except RepositoryError as error:
        raise InteractiveCommitError(
            f"Git proof or action failed: {argv[0]}"
        ) from error
    if result.returncode != 0:
        raise InteractiveCommitError(f"Git proof or action failed: {argv[0]}")
    return result.stdout.strip()


def _require_equal(observed: str, expected: str, label: str) -> None:
    if observed != expected:
        raise InteractiveCommitError(f"{label} changed")


def _clean(dependencies: InteractiveCommitDependencies, worktree: Path) -> None:
    if _git(
        dependencies, worktree, ("status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise InteractiveCommitError("index or worktree is not clean")


def _passing_identity(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
) -> CommitIdentity:
    """Parse bounded, unambiguous author and committer data from passing SHA."""
    output = _git(
        dependencies,
        request.worktree,
        (
            "show",
            "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
            request.passing_sha,
        ),
    )
    fields = output.split("\0")
    if len(fields) != 6 or any(
        not field
        or len(field.encode("utf-8")) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in field)
        for field in fields
    ):
        raise InteractiveCommitError("passing commit identity is invalid")
    for date in (fields[2], fields[5]):
        if (
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}[+-][0-9]{2}:[0-9]{2}",
                date,
            )
            is None
        ):
            raise InteractiveCommitError("passing commit identity is invalid")
        try:
            datetime.fromisoformat(date)
        except ValueError as error:
            raise InteractiveCommitError(
                "passing commit identity is invalid"
            ) from error
    return CommitIdentity(*fields)


def compact_interactive_commit(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
    *,
    apply: bool,
) -> InteractiveCommitReport:
    """Prove the candidate and optionally replace its branch tip atomically."""
    worktree = request.worktree
    _require_equal(
        _git(dependencies, worktree, ("symbolic-ref", "HEAD")),
        request.branch_ref,
        "checked-out branch",
    )
    for revision, expected, label in (
        ("HEAD", request.passing_sha, "HEAD"),
        (request.branch_ref, request.passing_sha, "branch"),
        (request.red_ref, request.red_sha, "Red ref"),
        (request.base_sha, request.base_sha, "base commit"),
        (request.red_sha, request.red_sha, "Red commit"),
        (request.passing_sha, request.passing_sha, "passing commit"),
        (f"{request.judge_tree}^{{tree}}", request.judge_tree, "Judge tree"),
    ):
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", "--verify", revision)),
            expected,
            label,
        )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%P", request.red_sha)),
        request.base_sha,
        "Red sole parent",
    )
    _git(
        dependencies,
        worktree,
        ("merge-base", "--is-ancestor", request.red_sha, request.passing_sha),
    )
    _require_equal(
        _git(
            dependencies, worktree, ("show", "-s", "--format=%T", request.passing_sha)
        ),
        request.judge_tree,
        "passing tree",
    )
    try:
        merge_head = dependencies.run_git(
            worktree, ("rev-parse", "-q", "--verify", "MERGE_HEAD")
        )
    except RepositoryError as error:
        raise InteractiveCommitError("merge state could not be checked") from error
    if merge_head.returncode not in {0, 1} or merge_head.stdout.strip():
        raise InteractiveCommitError("checkout has a pending merge")
    _clean(dependencies, worktree)
    identity = _passing_identity(request, dependencies)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)

    commit_argv = (
        "commit-tree",
        request.judge_tree,
        "-p",
        request.base_sha,
        "-F",
        str(request.message_file),
    )
    try:
        commit_result = dependencies.commit_tree(worktree, commit_argv, identity)
    except RepositoryError as error:
        raise InteractiveCommitError("Git commit-tree failed") from error
    if commit_result.returncode != 0:
        raise InteractiveCommitError("Git commit-tree failed")
    new_commit = commit_result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", new_commit) is None:
        raise InteractiveCommitError("commit-tree did not return an object ID")
    _git(
        dependencies,
        worktree,
        ("update-ref", request.branch_ref, new_commit, request.passing_sha),
    )
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", "HEAD")), new_commit, "new HEAD"
    )
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", request.branch_ref)),
        new_commit,
        "new branch",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%P", new_commit)),
        request.base_sha,
        "new sole parent",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%T", new_commit)),
        request.judge_tree,
        "new tree",
    )
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", request.red_ref)),
        request.red_sha,
        "Red ref",
    )
    _clean(dependencies, worktree)
    return InteractiveCommitReport(applied=True, new_commit=new_commit)
