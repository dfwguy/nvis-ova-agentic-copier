# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Guard interactive Red and Green commits and verify their retained history."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, field_validator

from .domain import sanitize_diagnostic
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
    message_file: Path | None = None

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
    commit_worktree: Callable[[Path, tuple[str, ...]], CommandResult] | None = None


@dataclass(frozen=True)
class InteractiveCommitReport:
    """Outcome of one preview or retained Red/Green commit."""

    applied: bool
    new_commit: str | None


@dataclass(frozen=True)
class InteractiveFinalReport:
    """Read-only proof of the retained interactive commit chain."""

    red_commit: str
    green_commit: str
    judge_tree: str


def run_git(
    worktree: Path,
    argv: tuple[str, ...],
    *,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] = os.environ,
    timeout_seconds: int = 30,
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
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = run_command(
        ("git", "-C", str(worktree), "-c", "core.fsmonitor=false", *argv),
        timeout_seconds=timeout_seconds,
        environment=git_environment,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


def run_worktree_commit(
    worktree: Path,
    argv: tuple[str, ...],
    *,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] = os.environ,
) -> CommandResult:
    """Run the installed commit hook with the same isolated Git configuration."""
    return run_git(
        worktree,
        argv,
        run_command=run_command,
        environment=environment,
        timeout_seconds=10_800,
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


def _staged_only(
    dependencies: InteractiveCommitDependencies, worktree: Path, label: str
) -> None:
    try:
        result = dependencies.run_git(
            worktree, ("status", "--porcelain=v1", "--untracked-files=all")
        )
    except RepositoryError as error:
        raise InteractiveCommitError("candidate status failed") from error
    if result.returncode != 0:
        raise InteractiveCommitError("candidate status failed")
    status = result.stdout
    if not status or any(
        line.startswith("??") or len(line) < 2 or line[1] != " "
        for line in status.splitlines()
    ):
        raise InteractiveCommitError(f"{label} has unstaged or untracked changes")


def _staged_tests_only(
    dependencies: InteractiveCommitDependencies, worktree: Path
) -> None:
    """Require at least one staged test path and no staged production path."""
    paths = _git(
        dependencies, worktree, ("diff", "--cached", "--name-only", "-z")
    ).split("\0")
    changed = [path for path in paths if path]
    if not changed or any(
        not path.startswith(("tests/", "template/tests/")) for path in changed
    ):
        raise InteractiveCommitError("Red may stage only test paths")


def _tests_frozen(
    dependencies: InteractiveCommitDependencies, worktree: Path, red_sha: str
) -> None:
    """Reject staged or worktree test changes since the retained Red commit."""
    try:
        result = dependencies.run_git(
            worktree, ("diff", "--quiet", red_sha, "--", "tests", "template/tests")
        )
    except RepositoryError as error:
        raise InteractiveCommitError("test freeze could not be checked") from error
    if result.returncode != 0:
        raise InteractiveCommitError("tests changed after Red")


def _no_pending_merge(
    dependencies: InteractiveCommitDependencies, worktree: Path
) -> None:
    try:
        merge_head = dependencies.run_git(
            worktree, ("rev-parse", "-q", "--verify", "MERGE_HEAD")
        )
    except RepositoryError as error:
        raise InteractiveCommitError("merge state could not be checked") from error
    if merge_head.returncode not in {0, 1} or merge_head.stdout.strip():
        raise InteractiveCommitError("checkout has a pending merge")


def _require_test_only_commit(
    dependencies: InteractiveCommitDependencies,
    worktree: Path,
    commit: str,
) -> str:
    """Return the sole parent after proving a commit changes only tests."""
    parent = _git(
        dependencies,
        worktree,
        ("show", "-s", "--format=%P", commit),
    )
    if re.fullmatch(r"[0-9a-f]{40}", parent) is None:
        raise InteractiveCommitError("Red commit must have one parent")
    changed = _git(
        dependencies,
        worktree,
        (
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--no-renames",
            "-r",
            "-z",
            commit,
        ),
    )
    paths = tuple(path for path in changed.split("\0") if path)
    if not paths or any(
        not path.startswith(("tests/", "template/tests/")) for path in paths
    ):
        raise InteractiveCommitError("Red history contains a non-test change")
    return parent


def _verify_red_chain(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
) -> None:
    """Require a finite single-parent Red chain from the final ref to base."""
    current = request.red_sha
    seen: set[str] = set()
    for _ in range(128):
        if current in seen or current == request.base_sha:
            raise InteractiveCommitError("Red history is cyclic or empty")
        seen.add(current)
        parent = _require_test_only_commit(dependencies, request.worktree, current)
        if parent == request.base_sha:
            return
        current = parent
    raise InteractiveCommitError("Red history exceeds the supported bound")


def verify_interactive_final(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
) -> InteractiveFinalReport:
    """Verify the exact Green tree and complete Red ancestry without mutation."""
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
        (f"{request.judge_tree}^{{tree}}", request.judge_tree, "Judge tree"),
    ):
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", "--verify", revision)),
            expected,
            label,
        )
    _verify_red_chain(request, dependencies)
    _require_equal(
        _git(
            dependencies,
            worktree,
            ("show", "-s", "--format=%P", request.passing_sha),
        ),
        request.red_sha,
        "Green sole parent",
    )
    _require_equal(
        _git(
            dependencies,
            worktree,
            ("show", "-s", "--format=%T", request.passing_sha),
        ),
        request.judge_tree,
        "Green tree",
    )
    _no_pending_merge(dependencies, worktree)
    _clean(dependencies, worktree)
    _tests_frozen(dependencies, worktree, request.red_sha)
    return InteractiveFinalReport(
        red_commit=request.red_sha,
        green_commit=request.passing_sha,
        judge_tree=request.judge_tree,
    )


def commit_interactive_green(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
    *,
    apply: bool,
) -> InteractiveCommitReport:
    """Commit the exact staged Judge tree on Red with installed hooks."""
    worktree = request.worktree
    _require_equal(
        _git(dependencies, worktree, ("symbolic-ref", "HEAD")),
        request.branch_ref,
        "checked-out branch",
    )
    _no_pending_merge(dependencies, worktree)
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", request.red_ref)),
        request.red_sha,
        "Red ref",
    )
    _verify_red_chain(request, dependencies)
    _require_equal(
        _git(dependencies, worktree, ("write-tree",)),
        request.judge_tree,
        "staged Judge tree",
    )
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", "HEAD")),
        request.red_sha,
        "HEAD",
    )
    _staged_only(dependencies, worktree, "Green candidate")
    _tests_frozen(dependencies, worktree, request.red_sha)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)
    if dependencies.commit_worktree is None:
        raise InteractiveCommitError("worktree commit boundary is unavailable")
    if request.message_file is None:
        raise InteractiveCommitError("Green commit message file is required")
    result = dependencies.commit_worktree(
        worktree, ("commit", "-F", str(request.message_file))
    )
    if result.returncode != 0:
        diagnostic = sanitize_diagnostic(
            f"installed pre-commit rejected Green: {result.stdout} {result.stderr}",
            limit=2048,
        )
        raise InteractiveCommitError(diagnostic)
    green_sha = _git(dependencies, worktree, ("rev-parse", "HEAD"))
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%P", green_sha)),
        request.red_sha,
        "Green sole parent",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%T", green_sha)),
        request.judge_tree,
        "Green tree",
    )
    _clean(dependencies, worktree)
    _tests_frozen(dependencies, worktree, request.red_sha)
    return InteractiveCommitReport(applied=True, new_commit=green_sha)


def commit_interactive_red(
    request: InteractiveCommitRequest,
    dependencies: InteractiveCommitDependencies,
    *,
    apply: bool,
) -> InteractiveCommitReport:
    """Retain the exact staged failing-test tree on its original base."""
    worktree = request.worktree
    _require_equal(
        _git(dependencies, worktree, ("symbolic-ref", "HEAD")),
        request.branch_ref,
        "checked-out branch",
    )
    _no_pending_merge(dependencies, worktree)
    prior_red = request.red_sha
    if prior_red != request.base_sha:
        _verify_red_chain(request, dependencies)
    _require_equal(
        _git(dependencies, worktree, ("write-tree",)),
        request.judge_tree,
        "staged Red tree",
    )
    head = _git(dependencies, worktree, ("rev-parse", "HEAD"))
    if head != prior_red:
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", request.branch_ref)),
            head,
            "Red branch",
        )
        _require_equal(
            _require_test_only_commit(dependencies, worktree, head),
            prior_red,
            "Red sole parent",
        )
        _require_equal(
            _git(dependencies, worktree, ("show", "-s", "--format=%T", head)),
            request.judge_tree,
            "Red tree",
        )
        try:
            red_ref = dependencies.run_git(
                worktree, ("rev-parse", "-q", "--verify", request.red_ref)
            )
        except RepositoryError as error:
            raise InteractiveCommitError("Red ref could not be checked") from error
        ref_sha = red_ref.stdout.strip()
        if red_ref.returncode == 0 and ref_sha == head:
            _clean(dependencies, worktree)
            return InteractiveCommitReport(applied=apply, new_commit=head)
        if red_ref.returncode == 1 and not ref_sha:
            expected_ref = ""
        elif (
            red_ref.returncode == 0
            and prior_red != request.base_sha
            and ref_sha == prior_red
        ):
            expected_ref = prior_red
        else:
            raise InteractiveCommitError("Red ref changed")
        _clean(dependencies, worktree)
        if not apply:
            return InteractiveCommitReport(applied=False, new_commit=None)
        _git(
            dependencies,
            worktree,
            ("update-ref", request.red_ref, head, expected_ref),
        )
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", request.red_ref)),
            head,
            "Red ref",
        )
        _clean(dependencies, worktree)
        return InteractiveCommitReport(applied=True, new_commit=head)
    if prior_red != request.base_sha:
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", request.red_ref)),
            prior_red,
            "prior Red ref",
        )
    else:
        try:
            red_ref = dependencies.run_git(
                worktree, ("rev-parse", "-q", "--verify", request.red_ref)
            )
        except RepositoryError as error:
            raise InteractiveCommitError("Red ref could not be checked") from error
        if red_ref.returncode not in {0, 1} or red_ref.stdout.strip():
            raise InteractiveCommitError("Red ref already exists")
    _staged_only(dependencies, worktree, "Red candidate")
    _staged_tests_only(dependencies, worktree)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)
    if request.message_file is None:
        raise InteractiveCommitError("Red commit message file is required")
    if dependencies.commit_worktree is None:
        raise InteractiveCommitError("worktree commit boundary is unavailable")
    result = dependencies.commit_worktree(
        worktree, ("commit", "--no-verify", "-F", str(request.message_file))
    )
    if result.returncode != 0:
        raise InteractiveCommitError("Red commit could not be created")
    red_sha = _git(dependencies, worktree, ("rev-parse", "HEAD"))
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", request.branch_ref)),
        red_sha,
        "Red branch",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%P", red_sha)),
        prior_red,
        "Red sole parent",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%T", red_sha)),
        request.judge_tree,
        "Red tree",
    )
    _git(
        dependencies,
        worktree,
        (
            "update-ref",
            request.red_ref,
            red_sha,
            "" if prior_red == request.base_sha else prior_red,
        ),
    )
    _clean(dependencies, worktree)
    return InteractiveCommitReport(applied=True, new_commit=red_sha)
