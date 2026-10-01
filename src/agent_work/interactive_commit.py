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

import ast
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from os import stat_result
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .domain import sanitize_diagnostic
from .ports import CommandRunner
from .red_preflight import parse_red_preflight_success
from .repository import CommandResult, RepositoryError, run_subprocess
from .test_executor import TestExecutionPolicy
from .validation import cheap_gate_failure_id, license_preflight_compliant


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


class ReconcileCommitRequest(BaseModel):
    """Explicit identities for one pending two-parent reconciliation."""

    worktree: Path
    branch_ref: str
    source_sha: str
    target_sha: str
    red_ref: str
    red_sha: str
    judge_tree: str
    message_file: Path | None = None
    reconcile_sha: str | None = None

    @field_validator(
        "source_sha", "target_sha", "red_sha", "judge_tree", "reconcile_sha"
    )
    @classmethod
    def _object_id(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("object ID must be 40 lowercase hexadecimal characters")
        return value

    @field_validator("branch_ref")
    @classmethod
    def _branch_ref(cls, value: str) -> str:
        return InteractiveCommitRequest._branch_ref(value)

    @field_validator("red_ref")
    @classmethod
    def _red_ref(cls, value: str) -> str:
        if value.startswith("refs/agent-work/red/"):
            return InteractiveCommitRequest._red_ref(value)
        if (
            re.fullmatch(
                r"refs/agent-work/red-checkpoints/[1-9][0-9]*/[a-zA-Z0-9][a-zA-Z0-9/-]*",
                value,
            )
            is None
        ):
            raise ValueError("Red ref must be an agent-work Red checkpoint ref")
        return value


@dataclass(frozen=True)
class InteractiveCommitDependencies:
    """Injected Git command boundary."""

    run_git: Callable[[Path, tuple[str, ...]], CommandResult]
    commit_worktree: Callable[[Path, tuple[str, ...]], CommandResult] | None = None
    run_license_preflight: Callable[[str], CommandResult] | None = None
    check_candidate_gate: Callable[[str], CommandResult] | None = None
    run_red_preflight: Callable[[Path, tuple[str, ...], str], CommandResult] | None = (
        None
    )


@dataclass(frozen=True)
class InteractiveCommitReport:
    """Outcome of one preview or retained Red/Green commit."""

    applied: bool
    new_commit: str | None
    residual_details: tuple[str, ...] = ()


@dataclass(frozen=True)
class InteractiveFinalReport:
    """Read-only proof of the retained interactive commit chain."""

    red_commit: str
    green_commit: str
    judge_tree: str


@dataclass(frozen=True)
class ReconcileFinalReport:
    """Read-only proof of an exact two-parent reconciliation commit."""

    reconcile_commit: str
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
) -> tuple[str, ...]:
    """Require at least one staged test path and no staged production path."""
    paths = _git(
        dependencies, worktree, ("diff", "--cached", "--name-only", "-z")
    ).split("\0")
    changed = [path for path in paths if path]
    if not changed or any(
        not path.startswith(("tests/", "template/tests/")) for path in changed
    ):
        raise InteractiveCommitError("Red may stage only test paths")
    return tuple(sorted(changed))


def _red_preflight(
    dependencies: InteractiveCommitDependencies,
    worktree: Path,
    paths: tuple[str, ...],
    tree: str,
    *,
    committed: bool,
) -> tuple[str, ...]:
    """Validate exact Red tests before commit or ref binding."""
    if dependencies.run_red_preflight is None:
        raise InteractiveCommitError("Red preflight boundary is unavailable")
    result = dependencies.run_red_preflight(worktree, paths, tree)
    details = (
        parse_red_preflight_success(result.stdout)
        if result.returncode == 0 and not result.stderr
        else None
    )
    if details is None:
        raise InteractiveCommitError("Red preflight rejected candidate tests")
    _require_equal(
        _git(dependencies, worktree, ("write-tree",)),
        tree,
        "staged Red tree after preflight",
    )
    if committed:
        _clean(dependencies, worktree)
    else:
        _staged_only(dependencies, worktree, "Red candidate changed during preflight")
    return details


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


def _reconcile_identity(
    request: ReconcileCommitRequest,
    dependencies: InteractiveCommitDependencies,
) -> None:
    """Prove the current checkout still has the exact reviewed pending merge."""
    worktree = request.worktree
    for revision, expected, label in (
        ("HEAD", request.source_sha, "source HEAD"),
        (request.branch_ref, request.source_sha, "source branch"),
        ("MERGE_HEAD", request.target_sha, "merge target"),
        ("refs/remotes/origin/main", request.target_sha, "origin/main"),
        (request.red_ref, request.red_sha, "Red ref"),
        (f"{request.judge_tree}^{{tree}}", request.judge_tree, "Judge tree"),
    ):
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", "--verify", revision)),
            expected,
            label,
        )
    _require_equal(
        _git(dependencies, worktree, ("symbolic-ref", "HEAD")),
        request.branch_ref,
        "checked-out branch",
    )
    if _git(dependencies, worktree, ("ls-files", "-u")):
        raise InteractiveCommitError("merge has unresolved conflicts")
    _require_equal(
        _git(dependencies, worktree, ("write-tree",)),
        request.judge_tree,
        "staged Judge tree",
    )
    _staged_only(dependencies, worktree, "reconciliation candidate")


def _reconcile_final_identity(
    request: ReconcileCommitRequest,
    dependencies: InteractiveCommitDependencies,
    reconcile_sha: str,
) -> None:
    """Prove ordered parents, tree, refs, and clean completed merge."""
    worktree = request.worktree
    _require_equal(
        _git(dependencies, worktree, ("symbolic-ref", "HEAD")),
        request.branch_ref,
        "checked-out branch",
    )
    for revision, expected, label in (
        ("HEAD", reconcile_sha, "HEAD"),
        (request.branch_ref, reconcile_sha, "branch"),
        ("refs/remotes/origin/main", request.target_sha, "origin/main"),
        (request.red_ref, request.red_sha, "Red ref"),
    ):
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", "--verify", revision)),
            expected,
            label,
        )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%P", reconcile_sha)),
        f"{request.source_sha} {request.target_sha}",
        "reconciliation parents",
    )
    _require_equal(
        _git(dependencies, worktree, ("show", "-s", "--format=%T", reconcile_sha)),
        request.judge_tree,
        "reconciliation tree",
    )
    _no_pending_merge(dependencies, worktree)
    _clean(dependencies, worktree)


def _check_commit_preflight(
    judge_tree: str,
    dependencies: InteractiveCommitDependencies,
) -> None:
    """Run the shared license and cheap candidate gates before committing."""
    if dependencies.run_license_preflight is None:
        raise InteractiveCommitError("license preflight is unavailable")
    try:
        license_result = dependencies.run_license_preflight(judge_tree)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise InteractiveCommitError("license preflight failed") from error
    if not license_preflight_compliant(judge_tree, license_result):
        raise InteractiveCommitError("license preflight failed")
    if dependencies.check_candidate_gate is None:
        raise InteractiveCommitError("cheap candidate gate is unavailable")
    try:
        gate = dependencies.check_candidate_gate(judge_tree)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise InteractiveCommitError("cheap candidate gate failed") from error
    if gate.returncode != 0:
        raise InteractiveCommitError(f"{cheap_gate_failure_id(gate)} failed")


def commit_reconcile(
    request: ReconcileCommitRequest,
    dependencies: InteractiveCommitDependencies,
    *,
    apply: bool,
) -> InteractiveCommitReport:
    """Preview or commit the exact staged tree from a pending merge."""
    _reconcile_identity(request, dependencies)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)
    _check_commit_preflight(request.judge_tree, dependencies)
    _reconcile_identity(request, dependencies)
    if dependencies.commit_worktree is None:
        raise InteractiveCommitError("worktree commit boundary is unavailable")
    if request.message_file is None:
        raise InteractiveCommitError("reconciliation commit message file is required")
    result = dependencies.commit_worktree(
        request.worktree, ("commit", "-F", str(request.message_file))
    )
    if result.returncode != 0:
        diagnostic = sanitize_diagnostic(
            f"installed pre-commit rejected reconciliation: {result.stdout} {result.stderr}",
            limit=2048,
        )
        raise InteractiveCommitError(diagnostic)
    reconcile_sha = _git(dependencies, request.worktree, ("rev-parse", "HEAD"))
    _reconcile_final_identity(request, dependencies, reconcile_sha)
    return InteractiveCommitReport(applied=True, new_commit=reconcile_sha)


def verify_reconcile_final(
    request: ReconcileCommitRequest,
    dependencies: InteractiveCommitDependencies,
) -> ReconcileFinalReport:
    """Verify a completed reconciliation without changing Git state."""
    if request.reconcile_sha is None:
        raise InteractiveCommitError("reconciliation commit ID is required")
    _reconcile_final_identity(request, dependencies, request.reconcile_sha)
    return ReconcileFinalReport(request.reconcile_sha, request.judge_tree)


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


def _committed_test_paths(
    dependencies: InteractiveCommitDependencies, worktree: Path, commit: str
) -> tuple[str, ...]:
    """Read the already proven test-only paths from one Red commit."""
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
    return tuple(sorted(path for path in changed.split("\0") if path))


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
    _green_candidate_identity(request, dependencies)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)
    _check_commit_preflight(request.judge_tree, dependencies)
    _require_equal(
        _git(dependencies, worktree, ("write-tree",)),
        request.judge_tree,
        "staged Judge tree after cheap gate",
    )
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
            if not apply:
                return InteractiveCommitReport(applied=False, new_commit=None)
            details = _red_preflight(
                dependencies,
                worktree,
                _committed_test_paths(dependencies, worktree, head),
                request.judge_tree,
                committed=True,
            )
            _require_equal(
                _git(dependencies, worktree, ("rev-parse", "HEAD")),
                head,
                "HEAD after preflight",
            )
            _require_equal(
                _git(dependencies, worktree, ("rev-parse", request.red_ref)),
                head,
                "Red ref after preflight",
            )
            return InteractiveCommitReport(
                applied=True, new_commit=head, residual_details=details
            )
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
        details = _red_preflight(
            dependencies,
            worktree,
            _committed_test_paths(dependencies, worktree, head),
            request.judge_tree,
            committed=True,
        )
        _require_equal(
            _git(dependencies, worktree, ("rev-parse", "HEAD")),
            head,
            "HEAD after preflight",
        )
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
        return InteractiveCommitReport(
            applied=True, new_commit=head, residual_details=details
        )
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
    test_paths = _staged_tests_only(dependencies, worktree)
    if not apply:
        return InteractiveCommitReport(applied=False, new_commit=None)
    if request.message_file is None:
        raise InteractiveCommitError("Red commit message file is required")
    if dependencies.commit_worktree is None:
        raise InteractiveCommitError("worktree commit boundary is unavailable")
    details = _red_preflight(
        dependencies, worktree, test_paths, request.judge_tree, committed=False
    )
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
    return InteractiveCommitReport(
        applied=True, new_commit=red_sha, residual_details=details
    )


FocusedReason = Literal[
    "runtime_unavailable",
    "timeout",
    "invalid_result",
    "cleanup_failed",
    "invalid_selection",
]


class FocusedDiagnosticError(InteractiveCommitError):
    """A closed, safe diagnostic process refusal."""

    def __init__(self, reason: FocusedReason) -> None:
        self.reason: FocusedReason = (
            reason
            if reason
            in {
                "runtime_unavailable",
                "timeout",
                "invalid_result",
                "cleanup_failed",
                "invalid_selection",
            }
            else "invalid_result"
        )
        super().__init__(f"focused diagnostic: {self.reason}")


class FocusedDiagnosticRequest(BaseModel):
    """Immutable identities and requested baseline modules from the CLI edge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    worktree: Path
    branch_ref: str
    base_sha: str
    red_ref: str
    red_sha: str
    candidate_tree: str
    baseline_modules: tuple[str, ...] = ()

    @field_validator("base_sha", "red_sha", "candidate_tree")
    @classmethod
    def _object_id(cls, value: str) -> str:
        return InteractiveCommitRequest._object_id(value)

    @field_validator("branch_ref")
    @classmethod
    def _branch_ref(cls, value: str) -> str:
        return InteractiveCommitRequest._branch_ref(value)

    @field_validator("red_ref")
    @classmethod
    def _red_ref(cls, value: str) -> str:
        return InteractiveCommitRequest._red_ref(value)


class FocusedFailureCase(BaseModel):
    """Only a selected module, static declaration and failure phase."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    module: str
    test_name: str | None
    phase: Literal["setup", "call", "teardown", "collection"]


class _FocusedArtifact(BaseModel):
    """Strict bounded process result; raw diagnostics are not accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    failed_cases: tuple[FocusedFailureCase, ...] = Field(max_length=128)
    details_unavailable: bool
    truncated: bool


class FocusedDiagnosticExecution(_FocusedArtifact):
    """Revalidated safe execution facts from an injected runner."""

    failed_cases: tuple[FocusedFailureCase, ...] = Field(default=(), max_length=128)
    details_unavailable: bool = False
    truncated: bool = False
    returncode: int


class FocusedDiagnosticReport(_FocusedArtifact):
    """Advisory outcome with no validation, commit or publication authority."""

    failed_cases: tuple[FocusedFailureCase, ...] = Field(default=(), max_length=128)
    details_unavailable: bool = False
    truncated: bool = False
    applied: bool
    candidate_tree: str
    red_sha: str
    modules: tuple[str, ...]
    outcome: Literal[
        "preview",
        "passed",
        "failed",
        "unavailable",
        "identity_changed",
        "unsupported_policy",
        "invalid_selection",
        "invalid_result",
    ]
    returncode: int | None = None
    stop_reason: (
        FocusedReason | Literal["identity_changed", "unsupported_policy"] | None
    ) = None


class FocusedDiagnosticRunner(Protocol):
    """Run exactly the selected modules with the declared safe case inventory."""

    def __call__(
        self,
        worktree: Path,
        modules: tuple[str, ...],
        known_cases: tuple[tuple[str, str], ...],
        /,
    ) -> FocusedDiagnosticExecution:
        """Return safe execution facts only."""
        ...


@dataclass(frozen=True)
class FocusedDiagnosticDependencies:
    """Host-owned Git, module approval and optional execution composition."""

    git: InteractiveCommitDependencies
    approved_baseline_modules: tuple[str, ...] = ()
    runner: FocusedDiagnosticRunner | None = None
    test_execution_policy: TestExecutionPolicy | None = None


class FocusedProcessPort(Protocol):
    """The owned child process wait and cleanup boundary."""

    pid: int

    def wait(self, *, timeout: float) -> int:
        """Wait within the existing housekeeping bound."""
        ...

    def poll(self) -> int | None:
        """Observe whether this child has exited."""
        ...


def _focused_commit_request(
    request: FocusedDiagnosticRequest,
) -> InteractiveCommitRequest:
    return InteractiveCommitRequest(
        worktree=request.worktree,
        branch_ref=request.branch_ref,
        base_sha=request.base_sha,
        red_ref=request.red_ref,
        red_sha=request.red_sha,
        passing_sha=request.red_sha,
        judge_tree=request.candidate_tree,
    )


def _green_candidate_identity(
    request: InteractiveCommitRequest, dependencies: InteractiveCommitDependencies
) -> None:
    """Shared authentic Red, frozen tests and exact staged candidate proof."""
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
        _git(dependencies, worktree, ("rev-parse", "HEAD")), request.red_sha, "HEAD"
    )
    _require_equal(
        _git(dependencies, worktree, ("rev-parse", request.branch_ref)),
        request.red_sha,
        "branch",
    )
    _staged_only(dependencies, worktree, "Green candidate")
    _tests_frozen(dependencies, worktree, request.red_sha)


def _canonical_test_module(path: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:template/)?tests/(?:[a-zA-Z0-9_-]+/)*test_[a-zA-Z0-9_-]+\.py", path
        )
    )


def _static_test_cases(module: str, source: str) -> tuple[tuple[str, str], ...]:
    """Extract bounded static declarations without parameter values or messages."""
    if len(source.encode("utf-8")) > 1_048_576:
        raise FocusedDiagnosticError("invalid_selection")
    try:
        parsed = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise FocusedDiagnosticError("invalid_selection") from error
    names: list[tuple[str, str]] = []
    for node in parsed.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test_"):
            names.append((module, node.name))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            names.extend(
                (module, f"{node.name}::{child.name}")
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test_")
            )
    return tuple(sorted(set(names)))


def _focused_baseline_selection(
    request: FocusedDiagnosticRequest, dependencies: FocusedDiagnosticDependencies
) -> None:
    """Check requested host approval and regular immutable baseline blobs."""
    for module in request.baseline_modules:
        if (
            not _canonical_test_module(module)
            or module not in dependencies.approved_baseline_modules
        ):
            raise FocusedDiagnosticError("invalid_selection")
        baseline = _git(
            dependencies.git,
            request.worktree,
            ("ls-tree", "-z", request.base_sha, "--", module),
        )
        candidate = _git(
            dependencies.git,
            request.worktree,
            ("ls-tree", "-z", request.candidate_tree, "--", module),
        )
        if (
            not re.fullmatch(
                r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(module) + r"\x00?",
                candidate,
            )
            or baseline != candidate
        ):
            raise FocusedDiagnosticError("invalid_selection")


def _focused_selection(
    request: FocusedDiagnosticRequest, dependencies: FocusedDiagnosticDependencies
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Derive Red modules and verify each approved baseline's immutable blob."""
    _focused_baseline_selection(request, dependencies)
    git = dependencies.git
    current = request.red_sha
    modules: set[str] = set()
    for _ in range(128):
        modules.update(_committed_test_paths(git, request.worktree, current))
        current = _git(git, request.worktree, ("show", "-s", "--format=%P", current))
        if current == request.base_sha:
            break
    modules.update(request.baseline_modules)
    known: list[tuple[str, str]] = []
    for module in sorted(modules):
        if not _canonical_test_module(module):
            raise FocusedDiagnosticError("invalid_selection")
        candidate = _git(
            git,
            request.worktree,
            ("ls-tree", "-z", request.candidate_tree, "--", module),
        )
        if not re.fullmatch(
            r"100(?:644|755) blob [0-9a-f]{40}\t" + re.escape(module) + r"\x00?",
            candidate,
        ):
            raise FocusedDiagnosticError("invalid_selection")
        source = _git(
            git, request.worktree, ("show", f"{request.candidate_tree}:{module}")
        )
        known.extend(_static_test_cases(module, source))
    return tuple(sorted(modules)), tuple(sorted(set(known)))


def _validate_focused_execution(
    execution: FocusedDiagnosticExecution,
    modules: tuple[str, ...],
    known: tuple[tuple[str, str], ...],
) -> FocusedDiagnosticExecution:
    """Revalidate injected DTOs and bind every fact to the selected declarations."""
    checked = FocusedDiagnosticExecution.model_validate(execution.model_dump())
    if checked.returncode == 0 and (
        checked.failed_cases or checked.details_unavailable or checked.truncated
    ):
        raise FocusedDiagnosticError("invalid_result")
    for case in checked.failed_cases:
        if (
            case.module not in modules
            or (case.test_name is None and case.phase != "collection")
            or (
                case.test_name is not None
                and (case.module, case.test_name) not in known
            )
        ):
            raise FocusedDiagnosticError("invalid_result")
    return checked


def run_focused_diagnostic(
    request: FocusedDiagnosticRequest,
    dependencies: FocusedDiagnosticDependencies,
    *,
    apply: bool = False,
) -> FocusedDiagnosticReport:
    """Preview or run one advisory diagnostic behind unchanged commit guards."""
    modules: tuple[str, ...] = ()
    commit_request = _focused_commit_request(request)

    def refused(
        outcome: Literal[
            "unavailable",
            "identity_changed",
            "unsupported_policy",
            "invalid_selection",
            "invalid_result",
        ],
        reason: FocusedReason | Literal["identity_changed", "unsupported_policy"],
    ) -> FocusedDiagnosticReport:
        return FocusedDiagnosticReport(
            applied=False,
            candidate_tree=request.candidate_tree,
            red_sha=request.red_sha,
            modules=modules,
            outcome=outcome,
            stop_reason=reason,
        )

    try:
        _focused_baseline_selection(request, dependencies)
    except (InteractiveCommitError, OSError, UnicodeError, subprocess.SubprocessError):
        return refused("invalid_selection", "invalid_selection")
    try:
        _green_candidate_identity(commit_request, dependencies.git)
    except (InteractiveCommitError, OSError, UnicodeError, subprocess.SubprocessError):
        return refused("identity_changed", "identity_changed")
    try:
        modules, known = _focused_selection(request, dependencies)
    except (InteractiveCommitError, OSError, UnicodeError, subprocess.SubprocessError):
        return refused("invalid_selection", "invalid_selection")
    if not apply:
        return FocusedDiagnosticReport(
            applied=False,
            candidate_tree=request.candidate_tree,
            red_sha=request.red_sha,
            modules=modules,
            outcome="preview",
        )
    if dependencies.test_execution_policy is not None:
        return refused("unsupported_policy", "unsupported_policy")
    if dependencies.runner is None:
        return refused("unavailable", "runtime_unavailable")
    result: FocusedDiagnosticExecution | None = None
    reason: FocusedReason | None = None
    try:
        result = _validate_focused_execution(
            dependencies.runner(request.worktree, modules, known), modules, known
        )
    except FocusedDiagnosticError as error:
        reason = error.reason
    except (ValidationError, TypeError, AttributeError, ValueError):
        reason = "invalid_result"
    except (OSError, subprocess.SubprocessError, RuntimeError):
        reason = "runtime_unavailable"
    try:
        _green_candidate_identity(commit_request, dependencies.git)
        after_modules, after_known = _focused_selection(request, dependencies)
        _require_equal(
            repr((after_modules, after_known)),
            repr((modules, known)),
            "selected modules",
        )
    except (InteractiveCommitError, OSError, UnicodeError, subprocess.SubprocessError):
        return refused("identity_changed", "identity_changed")
    if reason is not None:
        return refused(
            "invalid_result" if reason == "invalid_result" else "unavailable", reason
        )
    if result is None:
        return refused("invalid_result", "invalid_result")
    return FocusedDiagnosticReport(
        applied=True,
        candidate_tree=request.candidate_tree,
        red_sha=request.red_sha,
        modules=modules,
        outcome="passed" if result.returncode == 0 else "failed",
        **result.model_dump(),
    )


_FOCUSED_BOOTSTRAP = """
import json, os, stat, sys
try:
    import pytest
except ImportError:
    raise SystemExit(77)
from pathlib import Path
root, result_path, device, inode, modules_json, known_json = sys.argv[1:]
modules = tuple(json.loads(modules_json))
known = set(tuple(pair) for pair in json.loads(known_json))
sys.path.insert(0, str(Path(root) / "src"))
class Reports:
    def __init__(self) -> None:
        "Retain only bounded selected-source failure facts."
        self.cases: set[tuple[str, str | None, str]] = set()
        self.unavailable = False
        self.truncated = False
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        "Project a failed test without reading its diagnostics."
        if report.failed:
            module, separator, name = report.nodeid.partition("::")
            name = name.split("[", 1)[0]
            self.retain(module, name if separator else None, report.when)
    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        "Project selected-module collection failures only."
        if report.failed:
            module = report.nodeid.split("::", 1)[0]
            self.retain(module, None, "collection")
    def retain(self, module: str, name: str | None, phase: str) -> None:
        "Omit undeclared names and bound the safe inventory."
        if module not in modules or (name is not None and (module, name) not in known):
            self.unavailable = True
            return
        if len(self.cases) >= 128:
            self.truncated = True
            return
        self.cases.add((module, name, phase))
reports = Reports()
code = pytest.main(["-q", "--capture=no", "-p", "no:cacheprovider", *modules], plugins=[reports])
payload = json.dumps({"failed_cases": [{"module": module, "test_name": name, "phase": phase} for module, name, phase in sorted(reports.cases, key=lambda item: (item[0], item[1] or "", item[2]))], "details_unavailable": reports.unavailable, "truncated": reports.truncated}).encode()
if len(payload) > 65536:
    raise SystemExit(78)
fd = os.open(result_path, os.O_WRONLY | os.O_NOFOLLOW)
try:
    actual = os.fstat(fd)
    if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (int(device), int(inode)):
        raise SystemExit(78)
    os.ftruncate(fd, 0)
    with os.fdopen(fd, "wb", closefd=False) as output:
        output.write(payload)
finally:
    os.close(fd)
raise SystemExit(code)
"""


def run_focused_pytest(
    worktree: Path,
    modules: tuple[str, ...],
    *,
    known_cases: tuple[tuple[str, str], ...],
    interpreter: Path,
    environment: Mapping[str, str],
    timeout_seconds: float = 10_800,
    temporary_directory: Callable[[], AbstractContextManager[Path]],
    create_result: Callable[[Path], stat_result],
    read_result: Callable[[Path, stat_result], bytes],
    open_process: Callable[..., FocusedProcessPort],
    stop_process: Callable[[FocusedProcessPort], None],
) -> FocusedDiagnosticExecution:
    """Run installed pytest with discarded streams and a verified ephemeral result."""
    if not modules or any(not _canonical_test_module(module) for module in modules):
        raise FocusedDiagnosticError("invalid_selection")
    safe_environment = {
        name: value
        for name, value in environment.items()
        if not name.startswith(("PYTHON", "PYTEST"))
    }
    process: FocusedProcessPort | None = None
    try:
        with temporary_directory() as directory:
            result_path = directory / "result.json"
            expected = create_result(result_path)
            if (
                not stat.S_ISREG(expected.st_mode)
                or stat.S_IMODE(expected.st_mode) != 0o600
            ):
                raise FocusedDiagnosticError("invalid_result")
            command = (
                str(interpreter),
                "-I",
                "-B",
                "-c",
                _FOCUSED_BOOTSTRAP,
                str(worktree),
                str(result_path),
                str(expected.st_dev),
                str(expected.st_ino),
                json.dumps(modules),
                json.dumps(known_cases),
            )
            try:
                try:
                    process = open_process(
                        command,
                        cwd=worktree,
                        env=safe_environment,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                        shell=False,
                    )
                except (OSError, ValueError) as error:
                    raise FocusedDiagnosticError("runtime_unavailable") from error
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired as error:
                    raise FocusedDiagnosticError("timeout") from error
            finally:
                if process is not None:
                    try:
                        stop_process(process)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                        raise FocusedDiagnosticError("cleanup_failed") from error
            if returncode == 77:
                raise FocusedDiagnosticError("runtime_unavailable")
            try:
                raw = read_result(result_path, expected)
                if len(raw) > 65_536:
                    raise FocusedDiagnosticError("invalid_result")
                artifact = _FocusedArtifact.model_validate_json(raw)
                return _validate_focused_execution(
                    FocusedDiagnosticExecution(
                        returncode=returncode, **artifact.model_dump()
                    ),
                    modules,
                    known_cases,
                )
            except (OSError, ValueError, UnicodeError) as error:
                raise FocusedDiagnosticError("invalid_result") from error
    except FocusedDiagnosticError:
        raise
    except (OSError, RuntimeError) as error:
        raise FocusedDiagnosticError("cleanup_failed") from error
