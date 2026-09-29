# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior tests for GitLab, Git, and worker process boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest
from pydantic import ValidationError
from pytest_subprocess import FakeProcess

from agent_work import repository as repository_module
from agent_work import worker as worker_module
from agent_work.cli import main
from agent_work.domain import (
    DEFAULT_MAX_ACTIVATION_SECONDS,
    LIFECYCLE_LABEL_SPECS,
    LIFECYCLE_LABELS,
    ErrorCode,
    render_split_marker,
)
from agent_work.gitlab import GitLabError, GitLabGateway
from agent_work.models import (
    AgentWorkConfig,
    CommandReport,
    DecisionReviewSignal,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
)
from agent_work.repository import (
    CommandResult,
    GitRepository,
    PublicationError,
    PublicationFailureCategory,
    RepositoryError,
    _origin_identity,
    build_git_environment,
    classify_publication_failure,
    discover_config,
    run_subprocess,
)
from agent_work.runtime import _build_local_logger
from agent_work.worker import AgentInvoker, WorkerProtocolError, _agent_command


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )


_GIT_TRANSPORT_CREDENTIAL_CONFIG = (
    "-c",
    "credential.helper=",
    "-c",
    'credential.helper=!exec "$AGENT_WORK_GIT_CREDENTIAL_HELPER"',
    "-c",
    "credential.useHttpPath=true",
    "-c",
    "core.askPass=/bin/false",
)


def _git_arguments(command: tuple[str, ...]) -> tuple[str, ...]:
    """Return the Git operation args after validating a transport prefix."""
    arguments = command[3:]
    if arguments[:1] == ("-c",):
        assert arguments[:8] == _GIT_TRANSPORT_CREDENTIAL_CONFIG
        arguments = arguments[8:]
        assert arguments[:1] in {("fetch",), ("pull",), ("push",), ("ls-remote",)}
    if arguments[:1] in {("fetch",), ("pull",), ("push",), ("ls-remote",)}:
        assert command[3:11] == _GIT_TRANSPORT_CREDENTIAL_CONFIG
    return arguments


@pytest.mark.parametrize("marker_is_member", (True, False))
def test_gitlab_checks_marker_sha_in_numeric_project_mr_commit_inventory(
    tmp_path: Path, marker_is_member: bool
) -> None:
    """A marker can name an earlier MR commit, but never an unrelated commit."""
    marker_sha = "c" * 40
    commands: list[tuple[str, ...]] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(
            0,
            json.dumps(
                [
                    {"id": "d" * 40},
                    {"id": marker_sha if marker_is_member else "e" * 40},
                ]
            ),
            "",
        )

    found = GitLabGateway(_config(tmp_path), api).merge_request_contains_commit(
        269, marker_sha
    )

    assert found is marker_is_member
    assert len(commands) == 1
    assert "projects/310967/merge_requests/269/commits" in commands[0]
    assert all("gitlab.example.com" in command for command in commands)


def test_gitlab_finds_marker_commit_on_later_merge_request_page(tmp_path: Path) -> None:
    """A valid historical marker can be beyond the first commit page."""
    marker_sha = "c" * 40
    commands: list[tuple[str, ...]] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        commits = (
            [{"id": "d" * 40}] * 100 if len(commands) == 1 else [{"id": marker_sha}]
        )
        return CommandResult(0, json.dumps(commits), "")

    assert GitLabGateway(_config(tmp_path), api).merge_request_contains_commit(
        269, marker_sha
    )
    assert len(commands) == 2
    assert any("page=2" in argument for argument in commands[1])
    assert all(
        "projects/310967/merge_requests/269/commits" in command for command in commands
    )


def test_changed_paths_parses_raw_porcelain_without_losing_initial_characters(
    tmp_path: Path,
) -> None:
    """The NUL-delimited contract preserves ordinary, spaced, and rename paths."""
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        assert command[3:] == ("status", "--porcelain=v1", "-z")
        return CommandResult(
            0,
            " M README.md\0?? docs/with space.md\0R  new name.py\0old name.py\0",
            "",
        )

    changed = GitRepository(config, run).changed_paths(tmp_path / "worktree")

    assert changed == ("README.md", "docs/with space.md", "new name.py", "old name.py")
    assert len(calls) == 1


def test_worktree_status_observes_dirty_and_paths_from_one_porcelain_result(
    tmp_path: Path,
) -> None:
    """One NUL-safe Git status result supplies both retained recovery facts."""
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        assert command[3:] == ("status", "--porcelain=v1", "-z")
        return CommandResult(
            0,
            " M README.md\0?? docs/with space.md\0R  new name.py\0old name.py\0",
            "",
        )

    observed = GitRepository(config, run).observe_worktree_status(tmp_path / "worktree")

    assert observed.dirty is True
    assert observed.changed_paths == (
        "README.md",
        "docs/with space.md",
        "new name.py",
        "old name.py",
    )
    assert len(calls) == 1


def test_legacy_candidate_reuses_recovery_status_porcelain(tmp_path: Path) -> None:
    """Legacy identity reads reuse the status result already held by recovery."""
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        commands.append(arguments)
        if arguments == ("status", "--porcelain=v1", "-z"):
            return CommandResult(0, " M src/example.py\0", "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40, "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("diff", "--name-only", "-z"):
            return CommandResult(0, "src/example.py\0", "")
        if arguments[:2] == ("diff", "--cached"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    repository = GitRepository(_config(tmp_path), run)
    worktree = tmp_path / "worktree"
    status = repository.observe_worktree_status(worktree)
    candidate = repository.observe_legacy_publication_candidate(worktree, status=status)

    assert candidate.unstaged_paths == ("src/example.py",)
    assert commands.count(("status", "--porcelain=v1", "-z")) == 1


def test_stage_candidate_normalizes_observed_git_order(tmp_path: Path) -> None:
    """Controller staging accepts stable tracked/untracked porcelain groups."""
    base = "a" * 40
    tree = "b" * 40
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        assert command[-5:] == ("add", "--all", "--", "a.py", "z.py")
        return CommandResult(0, "", "")

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return base

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("z.py", "a.py")

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            assert worktree == tmp_path / "worktree"
            assert base_sha == base
            assert paths == ("a.py", "z.py")
            return tree

    result = ObservedRepository(_config(tmp_path), run).stage_candidate(
        tmp_path / "worktree", base
    )
    assert result == tree
    assert [command[-1] for command in calls] == ["z.py", "write-tree"]


def test_stage_candidate_accepts_verified_ancestor_only_when_enabled(
    tmp_path: Path,
) -> None:
    """Authored recovery may stage from its verified original base."""
    base = "a" * 40
    head = "b" * 40
    tree = "c" * 40

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return head

        def _is_ancestor(self, ancestor: str, descendant: str, worktree: Path) -> bool:
            del worktree
            return ancestor == base and descendant == "HEAD"

        def publication_changed_paths(
            self, worktree: Path, target_sha: str
        ) -> tuple[str, ...]:
            del worktree
            assert target_sha == base
            return ("authored.py", "recovery.py")

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree
            assert (base_sha, paths) in {
                (base, ("authored.py", "recovery.py")),
                (head, ("ordinary.py",)),
            }
            return tree

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("ordinary.py",)

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        return CommandResult(0, "", "")

    repository = ObservedRepository(_config(tmp_path), run)
    assert repository.stage_candidate(tmp_path, base, allow_base_ancestor=True) == tree
    with pytest.raises(RepositoryError, match="candidate head changed"):
        repository.stage_candidate(tmp_path, base)


@pytest.mark.parametrize(
    ("state", "returncode"),
    (("terminal-success", 0), ("terminal-failure", 1)),
)
def test_trusted_candidate_judge_response_strips_state_paths(
    tmp_path: Path, state: str, returncode: int
) -> None:
    """The controller returns bounded evidence and keeps snapshot paths private."""
    tree = "b" * 40
    hidden = str(tmp_path / "controller-state" / "junit.xml")
    report = {
        "candidate_tree": tree,
        "state": state,
        "terminal": True,
        "exit_code": returncode,
        "diagnostic": "JUnit py311 failures=1 errors=0" if returncode else None,
        "hook_failures": [],
        "junit_artifacts": (
            []
            if returncode
            else [
                {
                    "environment": environment,
                    "path": hidden,
                    "sha256": "c" * 64,
                    "test_count": 3,
                }
                for environment in ("py311", "py314")
            ]
        ),
        "failed_tests": ["tests.test_module::test_failed"] if returncode else [],
        "failed_junit_digests": ["d" * 64] if returncode else [],
        "junit_timings": (
            []
            if returncode
            else [
                {
                    "environment": "py311",
                    "total_seconds": 1.25,
                    "slowest_cases": [{"name": "test_known", "seconds": 1.25}],
                    "timing_complete": True,
                    "over_100ms_cases": [
                        {"name": "test_known", "seconds": 1.25},
                        {"name": "test_fourth", "seconds": 0.100001},
                    ],
                }
            ]
        ),
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        assert "validation" in command
        return CommandResult(returncode, json.dumps(report), "")

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    observed = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path / "worktree",
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action="validate",
        timeout_seconds=30,
    )

    assert observed["state"] == state
    assert observed["failed_tests"] == report["failed_tests"]
    assert hidden not in json.dumps(observed)
    assert observed["evidence_id"]
    if returncode == 0:
        assert observed["junit_timings"] == report["junit_timings"]


def test_judge_candidate_returns_bounded_deferred_admission(tmp_path: Path) -> None:
    tree = "b" * 40

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        return CommandResult(
            1,
            json.dumps(
                {
                    "candidate_tree": tree,
                    "state": "deferred",
                    "reason": "project_timing_admission_busy",
                }
            ),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            return tree

    report = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path / "worktree",
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action="validate",
        timeout_seconds=30,
    )
    assert report == {"candidate_tree": tree, "state": "deferred"}


@pytest.mark.parametrize(
    "failure_code",
    (
        "baseline_hook_failed",
        "focused_timing_command_failed",
        "capture_failed",
        "PRIVATE_TOKEN=/private/path",
    ),
)
def test_judge_candidate_binds_only_safe_timing_failure_code_to_evidence(
    tmp_path: Path, failure_code: str
) -> None:
    tree = "b" * 40

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        return CommandResult(
            1,
            json.dumps(
                {
                    "candidate_tree": tree,
                    "state": "terminal-artifact-failure",
                    "terminal": True,
                    "exit_code": 0,
                    "diagnostic": None,
                    "hook_failures": [],
                    "junit_artifacts": [],
                    "failed_tests": [],
                    "failed_junit_digests": [],
                    "junit_timings": [],
                    "timing_failure_code": failure_code,
                    "artifact_failure": {
                        "stage": "timing_review",
                        "reason": "invalid_timing",
                        "environment": None,
                        "path": None,
                    },
                }
            ),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    repository = ObservedRepository(_config(tmp_path), run)
    if failure_code.startswith("PRIVATE_TOKEN"):
        with pytest.raises(RepositoryError):
            repository.judge_candidate(
                tmp_path,
                tree,
                base_sha="a" * 40,
                changed_paths=("src/example.py",),
                action="status",
                timeout_seconds=1,
            )
        return

    observed = repository.judge_candidate(
        tmp_path,
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action="status",
        timeout_seconds=1,
    )
    assert observed["timing_failure_code"] == failure_code
    assert observed["evidence_id"]


def test_judge_candidate_timing_failure_codes_change_evidence_identity(
    tmp_path: Path,
) -> None:
    tree = "b" * 40
    failure_code = "baseline_hook_failed"

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        return CommandResult(
            1,
            json.dumps(
                {
                    "candidate_tree": tree,
                    "state": "terminal-artifact-failure",
                    "terminal": True,
                    "exit_code": 0,
                    "hook_failures": [],
                    "junit_artifacts": [],
                    "failed_tests": [],
                    "failed_junit_digests": [],
                    "junit_timings": [],
                    "timing_failure_code": failure_code,
                    "artifact_failure": {
                        "stage": "timing_review",
                        "reason": "invalid_timing",
                        "environment": None,
                        "path": None,
                    },
                }
            ),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    repository = ObservedRepository(_config(tmp_path), run)
    first = repository.judge_candidate(
        tmp_path,
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action="status",
        timeout_seconds=1,
    )
    failure_code = "focused_timing_command_failed"
    second = repository.judge_candidate(
        tmp_path,
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action="status",
        timeout_seconds=1,
    )
    assert first["evidence_id"] != second["evidence_id"]


@pytest.mark.parametrize(
    "case",
    [
        {"name": "/private/test_secret", "seconds": 0.2},
        {"name": "test_known", "seconds": 0.100},
        {"name": "test_known", "seconds": "invalid"},
    ],
)
def test_judge_candidate_rejects_unsafe_over_100ms_case(
    tmp_path: Path, case: dict[str, object]
) -> None:
    """The trusted boundary rejects private names and invalid threshold entries."""
    tree = "b" * 40
    report = {
        "candidate_tree": tree,
        "state": "terminal-success",
        "terminal": True,
        "exit_code": 0,
        "hook_failures": [],
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "junit_timings": [
            {
                "environment": "py311",
                "total_seconds": 1.0,
                "slowest_cases": [{"name": "test_known", "seconds": 1.0}],
                "timing_complete": True,
                "over_100ms_cases": [case],
            }
        ],
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0, tree if command[-1] == "write-tree" else json.dumps(report), ""
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    with pytest.raises(RepositoryError, match="candidate JUnit timing evidence"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path / "worktree",
            tree,
            base_sha="a" * 40,
            changed_paths=("src/example.py",),
            action="validate",
            timeout_seconds=30,
        )


def test_judge_candidate_accepts_verified_ancestor_scope(tmp_path: Path) -> None:
    """Authored validation keeps its original-base paths through Judge."""
    base, head, tree = "a" * 40, "b" * 40, "c" * 40
    report = {
        "candidate_tree": tree,
        "state": "terminal-success",
        "terminal": True,
        "exit_code": 0,
        "hook_failures": [],
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "junit_timings": [],
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0, tree if command[-1] == "write-tree" else json.dumps(report), ""
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return head

        def _is_ancestor(self, ancestor: str, descendant: str, worktree: Path) -> bool:
            del worktree
            return (ancestor, descendant) == (base, "HEAD")

        def publication_changed_paths(
            self, worktree: Path, target_sha: str
        ) -> tuple[str, ...]:
            del worktree
            assert target_sha == base
            return ("authored.py", "recovery.py")

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree
            assert (base_sha, paths) in {
                (base, ("authored.py", "recovery.py")),
                (head, ("ordinary.py",)),
            }
            return tree

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("ordinary.py",)

    observed = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        tree,
        base_sha=base,
        allow_base_ancestor=True,
        changed_paths=("authored.py", "recovery.py"),
        action="status",
        timeout_seconds=1,
    )
    assert observed["candidate_tree"] == tree
    assert observed["evidence_id"]
    ordinary = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        tree,
        base_sha=head,
        changed_paths=("ordinary.py",),
        action="status",
        timeout_seconds=1,
    )
    assert ordinary["candidate_tree"] == tree
    with pytest.raises(RepositoryError, match="candidate changed during validation"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            tree,
            base_sha=base,
            allow_base_ancestor=False,
            changed_paths=("ordinary.py",),
            action="status",
            timeout_seconds=1,
        )


def test_candidate_judge_rejects_tree_changed_while_hook_ran(tmp_path: Path) -> None:
    """Successful hook output cannot attest a tree changed during execution."""
    tree = "b" * 40
    write_trees = iter((tree, "c" * 40))

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, next(write_trees), "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "candidate_tree": tree,
                    "state": "terminal-success",
                    "exit_code": 0,
                    "junit_artifacts": [
                        {
                            "environment": environment,
                            "path": "/private/state.xml",
                            "sha256": "d" * 64,
                            "test_count": 1,
                        }
                        for environment in ("py311", "py314")
                    ],
                    "failed_tests": [],
                    "failed_junit_digests": [],
                    "junit_timings": [],
                }
            ),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

    with pytest.raises(RepositoryError, match="candidate changed during validation"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path / "worktree",
            tree,
            base_sha="a" * 40,
            changed_paths=("src/example.py",),
            action="validate",
            timeout_seconds=30,
        )


def test_agent_work_config_requires_absolute_recorded_git_and_glab_paths(
    tmp_path: Path,
) -> None:
    payload = _config(tmp_path).model_dump() | {
        "git_path": tmp_path / "bin" / "git",
        "glab_path": tmp_path / "bin" / "glab",
    }

    config = AgentWorkConfig.model_validate(payload)

    assert config.git_path == tmp_path / "bin" / "git"
    assert config.glab_path == tmp_path / "bin" / "glab"
    with pytest.raises(ValidationError, match="absolute"):
        AgentWorkConfig.model_validate(payload | {"git_path": Path("git")})


def test_sync_feedback_worktree_uses_the_exact_fast_forward_command_seam(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "12"
    old_sha = "a" * 40
    new_sha = "b" * 40
    calls: list[tuple[str, ...]] = []
    responses = iter(
        (
            CommandResult(0, "", ""),
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, old_sha, ""),
            CommandResult(0, "", ""),
            CommandResult(0, new_sha, ""),
            CommandResult(0, "", ""),
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, new_sha, ""),
        )
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return next(responses)

    GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    ).sync_feedback_worktree(worktree, "agent/issue-12", old_sha, new_sha)

    assert [_git_arguments(command) for command in calls] == [
        ("status", "--porcelain"),
        ("branch", "--show-current"),
        ("rev-parse", "HEAD"),
        ("fetch", "origin", "agent/issue-12"),
        ("rev-parse", "origin/agent/issue-12"),
        ("merge", "--ff-only", "origin/agent/issue-12"),
        ("branch", "--show-current"),
        ("rev-parse", "HEAD"),
    ]


def test_sync_feedback_worktree_replay_at_new_sha_only_verifies_without_merging(
    tmp_path: Path,
) -> None:
    """A crash after the fast-forward is a safe, side-effect-free replay."""
    config = _config(tmp_path)
    worktree = tmp_path / "state" / "worktrees" / "12"
    old_sha = "a" * 40
    new_sha = "b" * 40
    calls: list[tuple[str, ...]] = []
    responses = iter(
        (
            CommandResult(0, "", ""),
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, new_sha, ""),
        )
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return next(responses)

    GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    ).sync_feedback_worktree(worktree, "agent/issue-12", old_sha, new_sha)

    assert [command[3:] for command in calls] == [
        ("status", "--porcelain"),
        ("branch", "--show-current"),
        ("rev-parse", "HEAD"),
    ]


@pytest.mark.parametrize(
    ("responses", "message"),
    (
        ((CommandResult(0, "dirty\n", ""),), "clean"),
        ((CommandResult(0, "", ""), CommandResult(0, "other\n", "")), "branch"),
        (
            (
                CommandResult(0, "", ""),
                CommandResult(0, "agent/issue-12\n", ""),
                CommandResult(0, "c" * 40, ""),
            ),
            "baseline",
        ),
        (
            (
                CommandResult(0, "", ""),
                CommandResult(0, "agent/issue-12\n", ""),
                CommandResult(0, "a" * 40, ""),
                CommandResult(0, "", ""),
                CommandResult(0, "c" * 40, ""),
            ),
            "remote branch",
        ),
    ),
)
def test_sync_feedback_worktree_rejects_dirty_or_inexact_git_evidence(
    tmp_path: Path, responses: tuple[CommandResult, ...], message: str
) -> None:
    """The concrete Git repository rejects unsafe resume evidence before mutation."""
    iterator = iter(responses)
    repository = GitRepository(
        _config(tmp_path),
        lambda *_args, **_kwargs: next(iterator),
        service_environment={"GITLAB_TOKEN": "test-token"},
    )

    with pytest.raises(RepositoryError, match=message):
        repository.sync_feedback_worktree(
            tmp_path / "worktree", "agent/issue-12", "a" * 40, "b" * 40
        )


def test_sync_feedback_worktree_rejects_unsafe_remote_without_merging(
    tmp_path: Path,
) -> None:
    """Unsafe remote evidence cannot reach the merge mutation seam."""
    calls: list[tuple[str, ...]] = []
    responses = iter(
        (
            CommandResult(0, "", ""),
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, "a" * 40, ""),
            CommandResult(0, "", ""),
            CommandResult(0, "c" * 40, ""),
        )
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return next(responses)

    with pytest.raises(RepositoryError, match="remote branch"):
        GitRepository(
            _config(tmp_path), fake, service_environment={"GITLAB_TOKEN": "test-token"}
        ).sync_feedback_worktree(
            tmp_path / "worktree", "agent/issue-12", "a" * 40, "b" * 40
        )

    assert not any("merge" in command for command in calls)


@pytest.mark.parametrize(
    ("source", "discussion_id", "message"),
    [
        pytest.param("general", "thread-1", "general feedback", id="general-id"),
        pytest.param("discussion", None, "discussion feedback", id="missing-id"),
        pytest.param("discussion", "", "discussion feedback", id="empty-id"),
        pytest.param("discussion", " ", "discussion feedback", id="blank-id"),
    ],
)
def test_feedback_model_rejects_incoherent_discussion_identity(
    source: Literal["general", "discussion"],
    discussion_id: str | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        MergeRequestFeedback(
            id=1,
            source=source,
            discussion_id=discussion_id,
            author="human",
            created_at=datetime.fromisoformat("2026-09-13T00:00:00+00:00"),
        )


def test_gitlab_feedback_rejects_whitespace_raw_discussion_identity(
    tmp_path: Path,
) -> None:
    discussion = [{"id": " \t", "notes": []}]
    endpoints: list[str] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("GET") + 1]
        endpoints.append(endpoint)
        return CommandResult(
            0, json.dumps([] if endpoint.endswith("/notes") else discussion), ""
        )

    with pytest.raises(GitLabError, match="invalid merge request feedback"):
        GitLabGateway(_config(tmp_path), fake).fetch_merge_request_feedback(12)
    assert endpoints == [
        "projects/310967/merge_requests/12/notes",
        "projects/310967/merge_requests/12/discussions",
    ]


def test_feedback_cursor_rejects_whitespace_discussion_identity() -> None:
    with pytest.raises(ValidationError, match="discussion identity must be nonblank"):
        MergeRequestFeedbackCursor(
            last_processed_discussion_note_id=1,
            last_relevant_discussion_id=" \t",
            feedback_source_sha="a" * 40,
        )


def test_pending_feedback_rejects_whitespace_discussion_identity() -> None:
    cursor = MergeRequestFeedbackCursor(
        last_processed_discussion_note_id=1,
        last_relevant_discussion_id="discussion-1",
        feedback_source_sha="a" * 40,
    )

    with pytest.raises(
        ValidationError,
        match="discussion pending feedback must select its discussion cursor",
    ):
        PendingMergeRequestFeedback(
            target_cursor=cursor,
            source_sha="a" * 40,
            source="discussion",
            note_id=1,
            discussion_id=" \t",
        )


@pytest.mark.parametrize("path_name", ("AGENT_WORK_GIT", "AGENT_WORK_GLAB"))
def test_discovery_rejects_missing_or_nonabsolute_recorded_executables(
    tmp_path: Path, path_name: str
) -> None:
    environment = {
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
        "AGENT_WORK_BASE_BRANCH": "agent/work-base",
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_GIT": str(tmp_path / "bin" / "git"),
        "AGENT_WORK_GLAB": str(tmp_path / "bin" / "glab"),
    }
    environment[path_name] = "git" if path_name == "AGENT_WORK_GIT" else ""

    with pytest.raises(RepositoryError, match="absolute executable"):
        discover_config(
            current_directory=tmp_path,
            environment=environment,
            run_command=lambda *_args, **_kwargs: CommandResult(0, "", ""),
        )


def test_discovery_and_git_repository_use_recorded_executables_and_environment(
    tmp_path: Path,
) -> None:
    git = tmp_path / "bin" / "git"
    glab = tmp_path / "bin" / "glab"
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    git_environments: list[dict[str, str]] = []

    def fake(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        environment = kwargs.get("environment")
        if isinstance(environment, dict):
            captured_environment: dict[str, str] = {}
            for key, value in environment.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    break
                captured_environment[key] = value
            else:
                git_environments.append(captured_environment)
        if command[-1:] == ("--show-toplevel",):
            return CommandResult(0, f"{tmp_path / 'checkout'}\n", "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if command[0] == str(glab):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "path_with_namespace": "group/project",
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                    }
                ),
                "",
            )
        if command[-2:] == ("rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40, "")
        return CommandResult(0, "", "")

    config = discover_config(
        current_directory=tmp_path,
        environment={
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
            "AGENT_WORK_BASE_BRANCH": "agent/work-base",
            "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
            "AGENT_WORK_GIT": str(git),
            "AGENT_WORK_GLAB": str(glab),
        },
        run_command=fake,
    )
    repository = GitRepository(
        config,
        fake,
        service_environment={"GITLAB_TOKEN": "not-in-argv"},
    )
    assert repository.head(tmp_path / "worktree") == "a" * 40

    assert config.git_path == git
    assert config.glab_path == glab
    assert calls[0][0][0] == str(git)
    assert any(command[0] == str(glab) for command, _ in calls)
    git_call = calls[-1]
    assert git_call[0][0] == str(git)
    assert "not-in-argv" not in " ".join(git_call[0])
    assert "GIT_CONFIG_COUNT" not in git_environments[-1]
    assert not any(
        key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
        for key in git_environments[-1]
    )
    assert git_environments[-1]["AGENT_WORK_GIT_CREDENTIAL_HELPER"] == str(
        config.checkout / "src" / "agent-work-git-credential.sh"
    )
    assert {kwargs.get("timeout_seconds") for _, kwargs in calls} == {120}


def test_gitlab_gateway_uses_the_finite_housekeeping_command_deadline(
    tmp_path: Path,
) -> None:
    """GitLab control-plane calls cannot outlive the housekeeping allowance."""
    calls: list[dict[str, object]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        del command
        calls.append(kwargs)
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "path_with_namespace": "group/project",
                    "default_branch": "main",
                    "web_url": "https://gitlab.example.com/group/project",
                    "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                }
            ),
            "",
        )

    GitLabGateway(_config(tmp_path), run_command).verify_project()

    assert [call["timeout_seconds"] for call in calls] == [120]


@pytest.mark.parametrize(
    ("checkout_origin", "api_origin"),
    (
        (
            "http://gitlab.example.com/group/project.git",
            "https://gitlab.example.com/group/project.git",
        ),
        (
            "https://user@gitlab.example.com/group/project.git",
            "https://gitlab.example.com/group/project.git",
        ),
        (
            "https://gitlab.example.com/group/project.git",
            "http://gitlab.example.com/group/project.git",
        ),
        (
            "https://gitlab.example.com/group/project.git",
            "https://user:password@gitlab.example.com/group/project.git",
        ),
        (
            "https://gitlab.example.com/group/project.git",
            "https://gitlab.example.com/group/other.git",
        ),
    ),
)
def test_discovery_rejects_non_authoritative_https_origin(
    tmp_path: Path, checkout_origin: str, api_origin: str
) -> None:
    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1:] == ("--show-toplevel",):
            return CommandResult(0, f"{tmp_path / 'checkout'}\n", "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, checkout_origin, "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "main",
                    "path_with_namespace": "group/project",
                    "http_url_to_repo": api_origin,
                }
            ),
            "",
        )

    with pytest.raises(RepositoryError, match="HTTPS|identity"):
        discover_config(
            current_directory=tmp_path,
            environment={
                "XDG_STATE_HOME": str(tmp_path / "xdg"),
                "GITLAB_HOST": "gitlab.example.com",
                "GITLAB_PROJECT_ID": "310967",
                "AGENT_WORK_BASE_BRANCH": "agent/work-base",
                "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
                "AGENT_WORK_GIT": str(tmp_path / "bin" / "git"),
                "AGENT_WORK_GLAB": str(tmp_path / "bin" / "glab"),
            },
            run_command=fake,
        )


def test_failed_push_records_only_safe_diagnostic_and_replays_retained_commit(
    tmp_path: Path,
) -> None:
    config = AgentWorkConfig.model_validate(
        _config(tmp_path).model_dump()
        | {
            "git_path": tmp_path / "bin" / "git",
            "glab_path": tmp_path / "bin" / "glab",
        }
    )
    head = "a" * 40
    status = "M changed.py\n"
    calls: list[tuple[str, ...]] = []
    diagnostics: list[dict[str, object]] = []
    raw_failure = "https://user:token@example.test secret-token authentication failed"

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal head, status
        calls.append(command)
        arguments = _git_arguments(command)
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{head}\n", "")
        if arguments == ("rev-list", "--parents", "-n", "1", "b" * 40):
            return CommandResult(0, f"{'b' * 40} {'a' * 40}\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        if arguments[:2] == ("commit", "-m"):
            head = "b" * 40
            status = ""
            return CommandResult(0, "", "")
        if arguments[:1] == ("push",):
            if (
                len([call for call in calls if _git_arguments(call)[:1] == ("push",)])
                == 1
            ):
                return CommandResult(1, "", raw_failure)
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/agent/issue-12"):
            return CommandResult(0, f"{head}\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config,
        fake,
        service_environment={"GITLAB_TOKEN": "secret-token"},
        diagnostic_sink=diagnostics.append,
    )
    worktree = tmp_path / "worktree"
    prepared = repository.prepare_commit(
        worktree, "agent/issue-12", 12, expected_head="a" * 40
    )
    with pytest.raises(PublicationError) as failure:
        repository.push_prepared(worktree, "agent/issue-12", prepared)
    replayed = repository.push_prepared(worktree, "agent/issue-12", prepared)

    assert replayed == "b" * 40
    assert len([call for call in calls if _git_arguments(call)[:1] == ("commit",)]) == 1
    assert [
        _git_arguments(call) for call in calls if _git_arguments(call)[:1] == ("push",)
    ] == [
        ("push", "origin", f"{'b' * 40}:refs/heads/agent/issue-12"),
        ("push", "origin", f"{'b' * 40}:refs/heads/agent/issue-12"),
    ]
    assert failure.value.code is ErrorCode.GIT_AUTHENTICATION
    assert diagnostics == [
        {
            "category": "authentication",
            "operation": "push",
            "returncode": 1,
        }
    ]
    assert raw_failure not in str(diagnostics)
    assert "secret-token" not in str(failure.value)


def test_git_credential_environment_disables_prompts_and_uses_only_repository_helper(
    tmp_path: Path,
) -> None:
    credential_helper = tmp_path / "src" / "agent-work-git-credential.sh"
    environment = build_git_environment(
        {
            "GITLAB_TOKEN": "secret-value",
            "PATH": "/untrusted/bin",
            "GIT_CONFIG_PARAMETERS": "credential.helper=hostile",
            "GIT_ASKPASS": "/hostile/askpass",
            "SSH_ASKPASS": "/hostile/ssh-askpass",
        },
        credential_helper,
    )

    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "never"
    assert "GIT_CONFIG_COUNT" not in environment
    assert not any(
        key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")) for key in environment
    )
    assert environment["AGENT_WORK_GIT_CREDENTIAL_HELPER"] == str(credential_helper)
    assert environment["GIT_ASKPASS"] == "/bin/false"
    assert environment["SSH_ASKPASS"] == "/bin/false"
    assert environment["GITLAB_TOKEN"] == "secret-value"
    assert "GIT_CONFIG_PARAMETERS" not in environment


@pytest.mark.parametrize(
    "arguments",
    (
        ("fetch", "origin", "main"),
        ("pull", "origin", "main"),
        ("push", "origin", f"{'a' * 40}:refs/heads/agent/issue-12"),
        ("ls-remote", "origin"),
    ),
)
def test_remote_git_invocations_use_only_the_repository_credential_helper(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    """Network Git commands carry the helper configuration at the command seam."""
    config = _config(tmp_path)
    token = "token-must-not-appear-in-argv"
    credential_helper = config.checkout / "src" / "agent-work-git-credential.sh"
    calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
    expected_command = (
        str(config.git_path),
        "-C",
        str(config.checkout),
        "-c",
        "credential.helper=",
        "-c",
        'credential.helper=!exec "$AGENT_WORK_GIT_CREDENTIAL_HELPER"',
        "-c",
        "credential.useHttpPath=true",
        "-c",
        "core.askPass=/bin/false",
        *arguments,
    )

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        environment = cast(Mapping[str, str], kwargs["environment"])
        assert isinstance(environment, Mapping)
        calls.append((command, environment))
        if (
            command != expected_command
            or token in command
            or str(credential_helper) in command
            or environment.get("GITLAB_TOKEN") != token
            or environment.get("AGENT_WORK_GIT_CREDENTIAL_HELPER")
            != str(credential_helper)
            or environment.get("GIT_TERMINAL_PROMPT") != "0"
            or environment.get("GCM_INTERACTIVE") != "never"
            or environment.get("GIT_ASKPASS") != "/bin/false"
            or environment.get("SSH_ASKPASS") != "/bin/false"
            or "GIT_CONFIG_COUNT" in environment
            or "GIT_CONFIG_PARAMETERS" in environment
            or any(
                key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
                for key in environment
            )
        ):
            return CommandResult(1, "", "credential helper was not configured")
        return CommandResult(0, "", "")

    GitRepository(
        config,
        run,
        service_environment={
            "GITLAB_TOKEN": token,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!hostile-helper",
            "GIT_CONFIG_KEY_1": "core.askPass",
            "GIT_CONFIG_VALUE_1": "/hostile/askpass",
            "GIT_CONFIG_PARAMETERS": "credential.helper=hostile",
            "GIT_ASKPASS": "/hostile/askpass",
            "SSH_ASKPASS": "/hostile/ssh-askpass",
        },
    )._git(*arguments)

    assert calls == [(expected_command, calls[0][1])]
    assert token not in " ".join(calls[0][0])
    assert str(credential_helper) not in " ".join(calls[0][0])


@pytest.mark.parametrize("token", (None, ""))
def test_git_credential_environment_rejects_an_unavailable_token_before_git_runs(
    tmp_path: Path, token: str | None
) -> None:
    service_environment = {"PATH": "/untrusted/bin"}
    if token is not None:
        service_environment["GITLAB_TOKEN"] = token

    with pytest.raises(RepositoryError, match="token"):
        build_git_environment(
            service_environment,
            tmp_path / "src" / "agent-work-git-credential.sh",
        )


@pytest.mark.parametrize("operation", ("fetch", "pull", "push", "ls-remote"))
@pytest.mark.parametrize("service_environment", ({}, None))
def test_repository_blocks_remote_git_without_a_token_before_command_runner(
    tmp_path: Path, operation: str, service_environment: Mapping[str, str] | None
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    repository = GitRepository(
        _config(tmp_path), run, service_environment=service_environment
    )
    with pytest.raises(RepositoryError, match="token"):
        repository._git(operation, "origin")
    assert calls == []


@pytest.mark.parametrize(
    ("stderr", "category", "code", "retryable"),
    [
        (
            "fatal: could not read Username for 'https://gitlab.example.com'",
            "authentication",
            ErrorCode.GIT_AUTHENTICATION,
            False,
        ),
        (
            "remote: You are not allowed to push code to this project",
            "authorization",
            ErrorCode.GIT_AUTHORIZATION,
            False,
        ),
        (
            "remote: GitLab: You are not allowed to force push code to a protected branch",
            "policy",
            ErrorCode.GIT_POLICY,
            False,
        ),
        (
            "fatal: unable to access: Connection timed out",
            "transport",
            ErrorCode.GIT_TRANSPORT_RETRYABLE,
            True,
        ),
        (
            "fatal: unexpected failure",
            "configuration",
            ErrorCode.GIT_PUBLICATION_CONFIGURATION,
            False,
        ),
    ],
)
def test_publication_failure_classification_is_safe_and_deterministic(
    stderr: str,
    category: PublicationFailureCategory,
    code: ErrorCode,
    *,
    retryable: bool,
) -> None:
    failure = classify_publication_failure(stderr)

    assert failure.category == category
    assert failure.code is code
    assert failure.retryable is retryable
    assert stderr not in failure.summary
    assert stderr not in failure.next_action
    assert "Username" not in str(PublicationError(failure))


@pytest.mark.parametrize(
    "stderr",
    (
        "temporary failure in name resolution",
        "connection reset by peer",
        "connection refused",
        "TLS handshake timeout",
        "remote end hung up unexpectedly",
        "The requested URL returned error: 500",
        "The requested URL returned error: 502",
        "The requested URL returned error: 503",
        "The requested URL returned error: 504",
    ),
)
def test_publication_classifier_accepts_only_known_transport_signatures(
    stderr: str,
) -> None:
    assert classify_publication_failure(stderr).retryable


@pytest.mark.parametrize(
    "stderr",
    ("TLS certificate verification failed", "bad config line 5", "temporary failure"),
)
def test_publication_classifier_rejects_generic_or_permanent_signatures(
    stderr: str,
) -> None:
    failure = classify_publication_failure(stderr)
    assert failure.category is PublicationFailureCategory.CONFIGURATION
    assert not failure.retryable


def _issue_payload(
    iid: int = 12,
    labels: list[str] | None = None,
    *,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Work",
        "description": None,
        "labels": labels or [],
        "state": "opened",
        "confidential": False,
        "created_at": created_at,
        "author": {"username": "ignored"},
    }


@pytest.mark.parametrize(
    ("boundary", "change"),
    [
        ("readback", {"title": "Wrong title"}),
        ("readback", {"description": "Wrong description"}),
        ("readback", {"state": "closed"}),
        ("readback", {"labels": ["agent::running"]}),
        ("readback", {"iid": 102}),
        ("readback", {"confidential": True}),
        ("readback", {"confidential": "__omit__"}),
        ("post", {"title": "Wrong title"}),
        ("post", {"description": "Wrong description"}),
        ("post", {"state": "closed"}),
        ("post", {"labels": ["agent::running"]}),
        ("post", {"confidential": "__omit__"}),
    ],
)
def test_gitlab_recovery_child_rejects_mismatched_post_or_readback(
    tmp_path: Path, boundary: str, change: dict[str, object]
) -> None:
    marker = "<!-- agent-work-recovery-child:v1 original=12 fingerprint=abc -->"
    title = "Recover blocked issue #12 in a clean checkout"
    description = f"{marker}\n\nParent: #12"
    posted = _issue_payload(iid=101) | {"title": title, "description": description}
    read_back = posted.copy()
    selected = posted if boundary == "post" else read_back
    selected.update(change)
    if change.get("confidential") == "__omit__":
        selected.pop("confidential")
    requests: list[tuple[str, str]] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        method = command[command.index("-X") + 1]
        endpoint = command[command.index("-X") + 2]
        requests.append((method, endpoint))
        if (method, endpoint) == ("POST", "projects/310967/issues"):
            return CommandResult(0, json.dumps(posted), "")
        if (method, endpoint) == ("GET", "projects/310967/issues/101"):
            return CommandResult(0, json.dumps(read_back), "")
        raise AssertionError((method, endpoint))

    with pytest.raises(GitLabError, match="not exact"):
        GitLabGateway(_config(tmp_path), api).create_recovery_child(
            title=title, description=description, marker=marker
        )

    assert requests == (
        [("POST", "projects/310967/issues")]
        if boundary == "post"
        else [
            ("POST", "projects/310967/issues"),
            ("GET", "projects/310967/issues/101"),
        ]
    )


def _note_payload(
    note_id: int,
    *,
    body: str = "Useful context",
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "reviewer", "id": 42, "name": "Reviewer"},
        "created_at": created_at,
        "system": False,
    }


def _mr_payload(description: str = "") -> dict[str, object]:
    return {
        "iid": 88,
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/88",
        "state": "opened",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "source_branch": "agent/issue-12",
        "target_branch": "main",
        "sha": "a" * 40,
        "has_conflicts": False,
        "description": description,
        "title": "",
        "draft": False,
        "references": {"short": "!88"},
    }


def _mr_evidence(description: str = "") -> MergeRequestEvidence:
    return MergeRequestEvidence.model_validate(
        {
            key: value
            for key, value in _mr_payload(description).items()
            if key != "references"
        }
    )


def _decision_review() -> DecisionReviewSignal:
    return DecisionReviewSignal(
        decisions=("Keep the retry policy unchanged.",),
        assumptions=("The existing MR can be reconciled.",),
        alternatives_considered=("Block for human direction.",),
        risks=("A retry could lose review context.",),
        reviewer_checks=("Confirm the retry policy.",),
        unresolved_findings=("Human review is required.",),
        evidence=("Red tests specify the behavior.",),
        tests=("uv run pytest tests/test_agent_work_boundaries.py",),
        correction_cycle_ledger=("cycle-2",),
        unchanged_project_owned_values=("retry budget",),
    )


class ApiStub:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.issue = _issue_payload(labels=["customer::priority"])
        self.mr_title = ""
        self.mr_description = ""
        self.mr_draft = False

    def __call__(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967":
            payload: object = {
                "id": 310967,
                "host": "ignored",
                "default_branch": "main",
                "web_url": "https://gitlab.example.com/group/project",
                "namespace": {"name": "ignored"},
            }
        elif endpoint.endswith("/issues"):
            payload = [self.issue, _issue_payload(13, ["agent::running"])]
        elif endpoint.endswith("/issues/12"):
            if method == "PUT":
                additions = [
                    part.removeprefix("add_labels=")
                    for part in command
                    if part.startswith("add_labels=")
                ]
                self.issue["labels"] = additions
            payload = self.issue
        elif endpoint.endswith("/merge_requests"):
            if method == "POST":
                payload = _mr_payload()
                payload["title"] = next(
                    part.removeprefix("title=")
                    for part in command
                    if part.startswith("title=")
                )
                payload["description"] = next(
                    part.removeprefix("description=")
                    for part in command
                    if part.startswith("description=")
                )
                payload["draft"] = payload["title"].startswith("Draft:")
                self.mr_title = payload["title"]
                self.mr_description = payload["description"]
                self.mr_draft = payload["draft"]
            else:
                payload = [_mr_payload()]
        elif endpoint.endswith("/merge_requests/88"):
            payload = _mr_payload("Closes #12")
            if method == "PUT":
                self.mr_title = next(
                    (
                        part.removeprefix("title=")
                        for part in command
                        if part.startswith("title=")
                    ),
                    self.mr_title,
                )
                self.mr_description = next(
                    (
                        part.removeprefix("description=")
                        for part in command
                        if part.startswith("description=")
                    ),
                    self.mr_description,
                )
                payload["title"] = self.mr_title
                payload["description"] = self.mr_description
                payload["draft"] = self.mr_draft
        elif endpoint.endswith("/closes_issues"):
            payload = [{"iid": 12, "project_id": 310967}]
        elif endpoint.endswith("/pipelines"):
            payload = [
                {
                    "id": 3,
                    "status": "success",
                    "sha": "a" * 40,
                    "web_url": "https://pipeline/3",
                    "ref": "ignored",
                },
                {
                    "id": 2,
                    "status": "failed",
                    "sha": "b" * 40,
                    "web_url": "https://pipeline/2",
                },
            ]
        elif endpoint.endswith("/notes"):
            payload = {"id": 1}
        else:
            raise AssertionError(endpoint)
        return CommandResult(0, json.dumps(payload), "")


@pytest.mark.parametrize("target", [*LIFECYCLE_LABELS, None])
def test_gitlab_lifecycle_transition_never_requests_target_removal(
    tmp_path: Path, target: str | None
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    gateway.transition_labels(12, target)

    update = next(command for command in api.commands if "PUT" in command)
    remove_field = next(part for part in update if part.startswith("remove_labels="))
    removed = set(remove_field.removeprefix("remove_labels=").split(","))
    assert removed == set(LIFECYCLE_LABELS).difference({target})
    if target is not None:
        assert f"add_labels={target}" in update


@pytest.mark.parametrize(
    "lifecycle_label",
    ["agent::needs_repair", "agent::repair_failed", "agent::suggest_close"],
)
def test_new_nonactive_lifecycle_label_is_ineligible_and_transition_removes_all_competitors(
    tmp_path: Path, lifecycle_label: str
) -> None:
    api = ApiStub()
    api.issue = _issue_payload(labels=["customer::priority", lifecycle_label])
    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.list_eligible() == ()

    gateway.transition_labels(12, lifecycle_label)

    update = next(command for command in api.commands if "PUT" in command)
    removed = next(part for part in update if part.startswith("remove_labels="))
    assert set(removed.removeprefix("remove_labels=").split(",")) == {
        "agent::running",
        "agent::awaiting-human",
        "agent::blocked",
        "agent::failed",
        "agent::needs_repair",
        "agent::repair_failed",
        "agent::suggest_close",
    }.difference({lifecycle_label})


def test_gitlab_lifecycle_label_provisioning_is_idempotent_and_numeric_project_scoped(
    tmp_path: Path,
) -> None:
    class LabelApi:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []
            self.labels: list[dict[str, str]] = []

        def __call__(
            self, command: tuple[str, ...], **_kwargs: object
        ) -> CommandResult:
            self.commands.append(command)
            endpoint = command[command.index("-X") + 2]
            method = command[command.index("-X") + 1]
            if endpoint != "projects/310967/labels":
                raise AssertionError(endpoint)
            if method == "POST":
                fields = {
                    field.partition("=")[0]: field.partition("=")[2]
                    for field in command
                    if "=" in field
                }
                self.labels.append(
                    {
                        "name": fields["name"],
                        "color": fields["color"],
                        "description": fields["description"],
                    }
                )
                return CommandResult(0, json.dumps(self.labels[-1]), "")
            return CommandResult(0, json.dumps(self.labels), "")

    api = LabelApi()
    gateway = GitLabGateway(_config(tmp_path), api)

    gateway.ensure_lifecycle_labels()
    gateway.ensure_lifecycle_labels()

    creates = [
        command
        for command in api.commands
        if "-X" in command and command[command.index("-X") + 1] == "POST"
    ]
    assert len(creates) == 4
    assert {label["name"] for label in api.labels} == {
        "agent-work::manual",
        "agent::needs_repair",
        "agent::repair_failed",
        "agent::suggest_close",
    }
    lifecycle_definitions = {
        label["name"]: label
        for label in api.labels
        if label["name"].startswith("agent::")
    }
    assert {label["color"] for label in lifecycle_definitions.values()} == {
        "#D4A72C",
        "#C91C00",
        "#6F42C1",
    }
    manual_definition = next(
        label for label in api.labels if label["name"] == "agent-work::manual"
    )
    assert manual_definition["color"].startswith("#")
    assert manual_definition["description"].strip()
    assert "agent-work::manual" not in LIFECYCLE_LABELS
    assert all(label["description"].strip() for label in api.labels)
    assert all(
        command[command.index("-X") + 2] == "projects/310967/labels"
        for command in api.commands
    )


@pytest.mark.parametrize(
    "manual_definition",
    (
        None,
        {
            "name": "agent-work::manual",
            "color": "#000000",
            "description": "wrong",
        },
    ),
)
def test_gitlab_lifecycle_label_drift_reports_missing_or_mismatched_manual_tracker_marker(
    tmp_path: Path, manual_definition: dict[str, str] | None
) -> None:
    """The guarded canary includes the fixed manual queue-control label."""

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        if endpoint != "projects/310967/labels":
            raise AssertionError(endpoint)
        return CommandResult(
            0,
            json.dumps(
                [
                    *(
                        {
                            "name": name,
                            "color": spec["color"],
                            "description": spec["description"],
                        }
                        for name, spec in LIFECYCLE_LABEL_SPECS.items()
                    ),
                    *(() if manual_definition is None else (manual_definition,)),
                ]
            ),
            "",
        )

    drift = GitLabGateway(_config(tmp_path), api).lifecycle_label_drift()

    assert drift == ("agent-work::manual",)


def test_gitlab_prerequisite_labels_are_normalized_and_transitioned_deterministically(
    tmp_path: Path,
) -> None:
    """Dynamic labels use a fixed definition and replace only prior prerequisite labels."""

    class PrerequisiteLabelApi:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []
            self.issue = _issue_payload(
                labels=[
                    "customer::priority",
                    "agent-work::manual",
                    "agent::running",
                    "agent::prereq_10",
                    "agent::prereq_37",
                ]
            )
            self.labels = [
                {
                    "name": "agent::prereq_37",
                    "color": "#000000",
                    "description": "wrong",
                }
            ]

        def __call__(
            self, command: tuple[str, ...], **_kwargs: object
        ) -> CommandResult:
            self.commands.append(command)
            endpoint = command[command.index("-X") + 2]
            method = command[command.index("-X") + 1]
            if endpoint == "projects/310967/labels":
                if method == "GET":
                    return CommandResult(0, json.dumps(self.labels), "")
                fields = {
                    item.partition("=")[0]: item.partition("=")[2]
                    for item in command
                    if "=" in item
                }
                label = {
                    "name": fields["name"],
                    "color": fields["color"],
                    "description": fields["description"],
                }
                self.labels = [
                    existing
                    for existing in self.labels
                    if existing["name"] != label["name"]
                ] + [label]
                return CommandResult(0, json.dumps(label), "")
            if endpoint == "projects/310967/labels/agent-prereq_37":
                fields = {
                    item.partition("=")[0]: item.partition("=")[2]
                    for item in command
                    if "=" in item
                }
                self.labels[0] |= fields
                return CommandResult(0, json.dumps(self.labels[0]), "")
            if endpoint == "projects/310967/issues/12" and method == "GET":
                return CommandResult(0, json.dumps(self.issue), "")
            if endpoint == "projects/310967/issues/12" and method == "PUT":
                fields = {
                    item.partition("=")[0]: item.partition("=")[2]
                    for item in command
                    if "=" in item
                }
                removed = set(fields["remove_labels"].split(","))
                additions = fields["add_labels"].split(",")
                current_labels = self.issue["labels"]
                assert isinstance(current_labels, list)
                self.issue["labels"] = [
                    label for label in current_labels if label not in removed
                ] + additions
                return CommandResult(0, json.dumps(self.issue), "")
            raise AssertionError((endpoint, method))

    api = PrerequisiteLabelApi()
    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.ensure_prerequisite_labels(("agent-prereq_37", "agent-prereq_2"))
    assert not gateway.ensure_prerequisite_labels(("agent-prereq_37", "agent-prereq_2"))
    updated = gateway.transition_prerequisite_labels(
        12, ("agent-prereq_2", "agent-prereq_37")
    )

    assert updated.labels == (
        "customer::priority",
        "agent-work::manual",
        "agent::running",
        "agent-prereq_2",
        "agent-prereq_37",
    )
    definitions = {label["name"]: label for label in api.labels}
    assert set(definitions) == {
        "agent::prereq_37",
        "agent-prereq_2",
        "agent-prereq_37",
    }
    canonical_definitions = {
        name: label for name, label in definitions.items() if name.startswith("agent-")
    }
    assert {label["color"] for label in canonical_definitions.values()} == {"#6F42C1"}
    assert {label["description"] for label in canonical_definitions.values()} == {
        "Open split prerequisites for agent-work issue dependency display."
    }
    issue_update = next(
        command
        for command in api.commands
        if command[command.index("-X") + 2] == "projects/310967/issues/12"
        and command[command.index("-X") + 1] == "PUT"
    )
    assert "remove_labels=agent::prereq_10,agent::prereq_37" in issue_update
    assert "add_labels=agent-prereq_2,agent-prereq_37" in issue_update


def test_gitlab_prerequisite_transition_preserves_retained_dynamic_labels_when_gitlab_removals_win(
    tmp_path: Path,
) -> None:
    """Only stale dynamic labels are removed when GitLab resolves overlaps as removals."""
    commands: list[tuple[str, ...]] = []
    issue = _issue_payload(
        labels=[
            "customer::priority",
            "agent-work::manual",
            "agent-prereq_10",
            "agent-prereq_37",
        ]
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967/issues/12" and method == "GET":
            return CommandResult(0, json.dumps(issue), "")
        if endpoint == "projects/310967/issues/12" and method == "PUT":
            fields = {
                item.partition("=")[0]: item.partition("=")[2]
                for item in command
                if "=" in item
            }
            removed = set(fields["remove_labels"].split(","))
            additions = set(fields["add_labels"].split(","))
            current_labels = issue["labels"]
            assert isinstance(current_labels, list)
            # GitLab resolves a label in both fields as removed, not re-added.
            issue["labels"] = [
                label for label in [*current_labels, *additions] if label not in removed
            ]
            return CommandResult(0, json.dumps(issue), "")
        raise AssertionError((endpoint, method))

    updated = GitLabGateway(_config(tmp_path), api).transition_prerequisite_labels(
        12, ("agent-prereq_2", "agent-prereq_37")
    )

    assert updated.labels == (
        "customer::priority",
        "agent-work::manual",
        "agent-prereq_37",
        "agent-prereq_2",
    )
    update = next(command for command in commands if "PUT" in command)
    fields = {
        item.partition("=")[0]: item.partition("=")[2] for item in update if "=" in item
    }
    assert fields == {
        "remove_labels": "agent-prereq_10",
        "add_labels": "agent-prereq_2",
    }


def test_gitlab_prerequisite_transition_skips_update_when_current_dynamic_labels_match(
    tmp_path: Path,
) -> None:
    """An already exact dynamic projection performs no mutation request."""
    commands: list[tuple[str, ...]] = []
    issue = _issue_payload(
        labels=["customer::priority", "agent-work::manual", "agent-prereq_2"]
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967/issues/12" and method == "GET":
            return CommandResult(0, json.dumps(issue), "")
        raise AssertionError((endpoint, method))

    updated = GitLabGateway(_config(tmp_path), api).transition_prerequisite_labels(
        12, ("agent-prereq_2",)
    )

    assert updated.labels == (
        "customer::priority",
        "agent-work::manual",
        "agent-prereq_2",
    )
    assert len(commands) == 1


def test_gitlab_prerequisite_projection_inventory_requests_all_issue_states(
    tmp_path: Path,
) -> None:
    """Reconciliation receives a complete issue snapshot, including closed issues."""
    commands: list[tuple[str, ...]] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, json.dumps([_issue_payload()]), "")

    inventory = GitLabGateway(
        _config(tmp_path), api
    ).prerequisite_projection_inventory()

    assert tuple(issue.iid for issue in inventory) == (12,)
    assert len(commands) == 1
    assert "state=all" in commands[0]
    assert not any(field.startswith("labels=") for field in commands[0])


def test_gitlab_retires_only_exact_legacy_prerequisite_definitions_after_migration(
    tmp_path: Path,
) -> None:
    """Legacy definition cleanup is encoded, ordered, and leaves lookalikes alone."""
    commands: list[tuple[str, ...]] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967/labels" and method == "GET":
            return CommandResult(
                0,
                json.dumps(
                    [
                        {"name": "agent::prereq_10"},
                        {"name": "agent::prereq_2"},
                        {"name": "agent::prereq_01"},
                        {"name": "customer::priority"},
                    ]
                ),
                "",
            )
        if method == "DELETE":
            return CommandResult(0, "", "")
        raise AssertionError((endpoint, method))

    changed = GitLabGateway(_config(tmp_path), api).retire_legacy_prerequisite_labels()

    assert changed
    deletes = [
        command[command.index("-X") + 2]
        for command in commands
        if command[command.index("-X") + 1] == "DELETE"
    ]
    assert deletes == [
        "projects/310967/labels/agent%3A%3Aprereq_2",
        "projects/310967/labels/agent%3A%3Aprereq_10",
    ]


def test_gitlab_prerequisite_transition_rejects_an_unverified_update(
    tmp_path: Path,
) -> None:
    """A successful mutation response cannot substitute for a verified projection."""
    issue = _issue_payload(labels=["agent::prereq_37"])

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967/issues/12" and method == "GET":
            return CommandResult(0, json.dumps(issue), "")
        if endpoint == "projects/310967/issues/12" and method == "PUT":
            return CommandResult(
                0,
                json.dumps(_issue_payload(labels=["agent::prereq_2"])),
                "",
            )
        raise AssertionError(endpoint)

    gateway = GitLabGateway(_config(tmp_path), api)

    with pytest.raises(GitLabError, match="did not apply"):
        gateway.transition_prerequisite_labels(12, ("agent::prereq_2",))


@pytest.mark.parametrize(
    ("prerequisite_state", "dependent_labels", "eligible"),
    [
        ("opened", (), ()),
        ("closed", (), (38,)),
        ("closed", ("agent::prereq_37",), (38,)),
    ],
)
def test_gitlab_eligibility_uses_trusted_marker_and_live_state_not_prerequisite_labels(
    tmp_path: Path,
    prerequisite_state: str,
    dependent_labels: tuple[str, ...],
    eligible: tuple[int, ...],
) -> None:
    """Absent or stale display labels never change the dependency eligibility decision."""
    dependent = _issue_payload(38, list(dependent_labels)) | {
        "description": render_split_marker(310967, 1, "dependent", (37,)),
        "state": "opened",
    }
    prerequisite = _issue_payload(
        37, ["agent::running"] if prerequisite_state == "opened" else []
    ) | {"state": prerequisite_state}

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        if endpoint != "projects/310967/issues":
            raise AssertionError(endpoint)
        return CommandResult(0, json.dumps([dependent, prerequisite]), "")

    result = GitLabGateway(_config(tmp_path), api).list_eligible()

    assert tuple(issue.iid for issue in result) == eligible


def test_gitlab_manual_tracker_marker_excludes_only_exact_open_issue_from_selection_and_projection(
    tmp_path: Path,
) -> None:
    """A manual queue marker is neither eligible work nor a lifecycle projection."""
    manual = _issue_payload(12, ["agent-work::manual"]) | {"state": "opened"}
    lookalike = _issue_payload(13, ["agent-work::manual-review"]) | {"state": "opened"}

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        if endpoint != "projects/310967/issues":
            raise AssertionError(endpoint)
        state = next(
            field.removeprefix("state=")
            for field in command
            if field.startswith("state=")
        )
        if state == "closed":
            return CommandResult(0, json.dumps([]), "")
        return CommandResult(0, json.dumps([manual, lookalike]), "")

    gateway = GitLabGateway(_config(tmp_path), api)

    assert tuple(issue.iid for issue in gateway.list_eligible()) == (13,)
    assert gateway.list_projected() == ()
    assert gateway.list_closed_projected() == ()


@pytest.mark.parametrize(
    "closing_issues",
    (
        [{}],
        [{"project_id": 310967}],
        [{"iid": 12}],
        [{"iid": "12", "project_id": 310967}],
        [{"iid": 12, "project_id": "310967"}],
        [
            {"iid": 12, "project_id": 310967},
            {"iid": "unknown", "project_id": 310967},
        ],
    ),
)
@pytest.mark.parametrize("projection_method", ("set", "single"))
def test_gitlab_rejects_malformed_closing_issue_projection(
    tmp_path: Path, closing_issues: list[dict[str, object]], projection_method: str
) -> None:
    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[command.index("-X") + 2] == (
            "projects/310967/merge_requests/88/closes_issues"
        )
        return CommandResult(0, json.dumps(closing_issues), "")

    gateway = GitLabGateway(_config(tmp_path), api)

    with pytest.raises(GitLabError):
        if projection_method == "set":
            gateway.closing_issue_iids(88)
        else:
            gateway.mr_closes_issue(88, 12)


def test_gitlab_reads_validated_closing_issue_set_once(tmp_path: Path) -> None:
    calls = 0

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal calls
        calls += 1
        assert command[command.index("-X") + 2] == (
            "projects/310967/merge_requests/88/closes_issues"
        )
        return CommandResult(
            0,
            json.dumps(
                [
                    {"iid": 12, "project_id": 310967},
                    {"iid": 13, "project_id": 310967},
                    {"iid": 9, "project_id": 999},
                ]
            ),
            "",
        )

    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.closing_issue_iids(88) == frozenset({12, 13})
    assert calls == 1


def test_gitlab_gateway_enforces_eligible_selection_and_exact_evidence(
    tmp_path: Path,
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.verify_project().id == 310967
    assert [issue.iid for issue in gateway.list_eligible()] == [12]
    issue_list_calls = [
        command
        for command in api.commands
        if command[command.index("-X") + 2].endswith("/issues")
    ]
    assert all(
        not any(part.startswith("labels=") for part in command)
        for command in issue_list_calls
    )
    assert {issue.iid for issue in gateway.list_projected()} == {13}
    assert gateway.fetch_issue(12).description == ""
    assert gateway.transition_labels(12, "agent::running").labels == ("agent::running",)
    assert gateway.fetch_merge_request(88).iid == 88
    assert gateway.find_merge_request_by_branch("agent/issue-12") is not None
    assert gateway.open_merge_requests_by_branch("agent/issue-12") == (_mr_evidence(),)
    inventory_call = api.commands[-1]
    assert "state=opened" in inventory_call
    assert "source_branch=agent/issue-12" in inventory_call
    assert "per_page=100" in inventory_call
    draft = MergeRequestDraft(title="Change", description="Details")
    assert (
        gateway.create_merge_request(
            branch="agent/issue-12", target="main", issue_iid=12, draft=draft
        ).iid
        == 88
    )
    mr = gateway.ensure_closing_issue_reference(gateway.fetch_merge_request(88), 12)
    assert mr.description == "Closes #12"
    assert gateway.mr_closes_issue(88, 12)
    pipeline = gateway.latest_pipeline_for_sha(88, "a" * 40)
    assert pipeline is not None and pipeline.status == "success"
    assert gateway.latest_pipeline_for_sha(88, "c" * 40) is None
    gateway.comment_outcome(
        12, error_code="blocked", summary="Safe", next_action="Review"
    )
    outcome_call = next(
        command
        for command in api.commands
        if command[command.index("-X") + 2].endswith("/notes")
    )
    assert any(
        part.startswith("body=<!-- agent-work-outcome:v1 -->\nAgent outcome")
        for part in outcome_call
    )
    assert all(
        "projects/310967" in command[command.index("-X") + 2]
        for command in api.commands
    )


def test_gitlab_default_branch_pipeline_uses_numeric_project_and_exact_ref(
    tmp_path: Path,
) -> None:
    """The default-branch gate reads newest pipeline evidence from its configured ref."""
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    pipeline = gateway.latest_pipeline_for_ref("main")

    assert pipeline is not None and pipeline.id == 3 and pipeline.status == "success"
    pipeline_call = api.commands[-1]
    assert pipeline_call[pipeline_call.index("-X") + 2] == "projects/310967/pipelines"
    assert "ref=main" in pipeline_call
    assert "per_page=100" in pipeline_call


def test_gitlab_failed_job_evidence_uses_exact_numeric_paths_and_redacts_boundary_errors(
    tmp_path: Path,
) -> None:
    """Job inventory and one trace stay bound to the configured project and pipeline."""
    commands: list[tuple[str, ...]] = []
    jobs = [
        {
            "id": 71,
            "name": "unit",
            "stage": "test",
            "status": "failed",
            "failure_reason": "script_failure",
            "retried": False,
            "allow_failure": False,
            "pipeline": {"id": 4, "sha": "a" * 40, "status": "failed"},
        }
    ]

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        if endpoint == "projects/310967/pipelines/4/jobs":
            return CommandResult(0, json.dumps(jobs), "")
        if endpoint == "projects/310967/jobs/71/trace":
            return CommandResult(0, "Bearer top-secret\x00trace", "")
        raise AssertionError(endpoint)

    gateway = GitLabGateway(_config(tmp_path), api)
    inventory = gateway.list_pipeline_jobs(4, "a" * 40)
    trace = gateway.fetch_job_trace(4, 71)

    assert inventory[0].id == 71
    assert trace.status == "collected"
    assert "top-secret" not in trace.text
    assert "\x00" not in trace.text
    assert [command[command.index("-X") + 2] for command in commands] == [
        "projects/310967/pipelines/4/jobs",
        "projects/310967/jobs/71/trace",
    ]
    assert "per_page=100" in commands[0]


def test_gitlab_pipeline_jobs_aggregate_two_bounded_pages(
    tmp_path: Path,
) -> None:
    """A full first page continues once and preserves exact job evidence."""
    commands: list[tuple[str, ...]] = []

    def job(job_id: int) -> dict[str, object]:
        return {
            "id": job_id,
            "name": "unit",
            "stage": "test",
            "status": "failed",
            "retried": False,
            "allow_failure": False,
            "pipeline": {"id": 4, "sha": "a" * 40},
        }

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        page = int(
            next(
                part.removeprefix("page=")
                for part in command
                if part.startswith("page=")
            )
        )
        payload = [job(job_id) for job_id in range(1, 101)] if page == 1 else [job(101)]
        return CommandResult(0, json.dumps(payload), "")

    jobs = GitLabGateway(_config(tmp_path), api).list_pipeline_jobs(4, "a" * 40)

    assert [job.id for job in jobs] == list(range(1, 102))
    assert [
        next(part for part in command if part.startswith("page="))
        for command in commands
    ] == [
        "page=1",
        "page=2",
    ]
    assert all(
        command[command.index("-X") + 2] == "projects/310967/pipelines/4/jobs"
        and "per_page=100" in command
        and "include_retried=true" in command
        for command in commands
    )


def test_gitlab_pipeline_jobs_preserve_optional_failure_and_missing_retried(
    tmp_path: Path,
) -> None:
    payload = [
        {
            "id": 71,
            "name": "unit",
            "stage": "test",
            "status": "success",
            "allow_failure": False,
            "pipeline": {"id": 4, "sha": "a" * 40},
        },
        {
            "id": 72,
            "name": "optional",
            "stage": "test",
            "status": "failed",
            "allow_failure": True,
            "retried": True,
            "pipeline": {"id": 4, "sha": "a" * 40},
        },
    ]

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        del command
        return CommandResult(0, json.dumps(payload), "")

    jobs = GitLabGateway(_config(tmp_path), api).list_pipeline_jobs(4, "a" * 40)

    assert [(job.id, job.retried, job.allow_failure) for job in jobs] == [
        (71, False, False),
        (72, True, True),
    ]


def test_gitlab_pipeline_jobs_reject_duplicate_ids_across_pages(tmp_path: Path) -> None:
    """A repeated job identity cannot become ambiguous repair evidence."""
    calls = 0

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal calls
        calls += 1
        page = int(
            next(
                part.removeprefix("page=")
                for part in command
                if part.startswith("page=")
            )
        )
        payload = [
            {
                "id": job_id,
                "name": "unit",
                "stage": "test",
                "status": "failed",
                "retried": False,
                "allow_failure": False,
                "pipeline": {"id": 4, "sha": "a" * 40},
            }
            for job_id in (range(1, 101) if page == 1 else (1,))
        ]
        return CommandResult(0, json.dumps(payload), "")

    with pytest.raises(GitLabError, match="duplicate"):
        GitLabGateway(_config(tmp_path), api).list_pipeline_jobs(4, "a" * 40)

    assert calls == 2


def test_gitlab_pipeline_jobs_fail_closed_at_bounded_page_limit(tmp_path: Path) -> None:
    """One hundred full pages reject the inventory without reading page 101."""
    pages: list[int] = []

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        page = int(
            next(
                part.removeprefix("page=")
                for part in command
                if part.startswith("page=")
            )
        )
        pages.append(page)
        return CommandResult(
            0,
            json.dumps(
                [
                    {
                        "id": page * 100 + job_id,
                        "name": "unit",
                        "stage": "test",
                        "status": "failed",
                        "retried": False,
                        "allow_failure": False,
                        "pipeline": {"id": 4, "sha": "a" * 40},
                    }
                    for job_id in range(100)
                ]
            ),
            "",
        )

    with pytest.raises(GitLabError, match="exceeded the bound"):
        GitLabGateway(_config(tmp_path), api).list_pipeline_jobs(4, "a" * 40)

    assert pages == list(range(1, 101))


def test_gitlab_open_merge_requests_reads_bounded_pages_of_validated_inventory(
    tmp_path: Path,
) -> None:
    """Pre-selection inventory reads every opened MR from bounded numeric-project pages."""

    class PagedMergeRequestApi(ApiStub):
        def __call__(
            self, command: tuple[str, ...], **_kwargs: object
        ) -> CommandResult:
            endpoint = command[command.index("-X") + 2]
            method = command[command.index("-X") + 1]
            if endpoint == "projects/310967/merge_requests" and method == "GET":
                self.commands.append(command)
                page = int(
                    next(
                        part.removeprefix("page=")
                        for part in command
                        if part.startswith("page=")
                    )
                )
                if page == 1:
                    payload = [
                        _mr_payload()
                        | {
                            "iid": iid,
                            "web_url": f"https://pipeline.example/mr/{iid}",
                            "source_branch": f"agent/issue-{iid}",
                        }
                        for iid in range(1, 101)
                    ]
                elif page == 2:
                    payload = [
                        _mr_payload()
                        | {
                            "iid": 101,
                            "web_url": "https://pipeline.example/mr/101",
                            "source_branch": "agent/issue-101",
                        }
                    ]
                else:
                    raise AssertionError(f"unexpected page {page}")
                return CommandResult(0, json.dumps(payload), "")
            return super().__call__(command, **_kwargs)

    api = PagedMergeRequestApi()
    merge_requests = GitLabGateway(_config(tmp_path), api).open_merge_requests()

    assert [merge_request.iid for merge_request in merge_requests] == list(
        range(1, 102)
    )
    calls = [
        command
        for command in api.commands
        if command[command.index("-X") + 2] == "projects/310967/merge_requests"
    ]
    assert len(calls) == 2
    assert all("state=opened" in call and "per_page=100" in call for call in calls)
    assert {"page=1", "page=2"} == {
        next(part for part in call if part.startswith("page=")) for call in calls
    }


def test_gitlab_gateway_creates_and_reconciles_a_decision_review_draft(
    tmp_path: Path,
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)
    draft = MergeRequestDraft(
        title="Draft: retry policy decision",
        description="## Summary\nReview required.",
        is_draft=True,
        decision_review=_decision_review(),
    )

    created = gateway.create_merge_request(
        branch="agent/issue-12", target="main", issue_iid=12, draft=draft
    )
    stale = created.model_copy(
        update={"description": "Previously rendered.\ncloses # 12"}
    )
    reconciled = gateway.reconcile_merge_request(stale, issue_iid=12, draft=draft)

    post = next(command for command in api.commands if "POST" in command)
    assert not any(part.startswith("draft=") for part in post)
    assert any(part.startswith("title=Draft: retry policy decision") for part in post)
    assert reconciled.is_draft is True
    update = next(
        command
        for command in api.commands
        if "PUT" in command and command[command.index("-X") + 2].endswith("/88")
    )
    assert any(
        part.startswith("description=# ⚠️ Decision review required\n") for part in update
    )
    assert not any(part.startswith("draft=") for part in update)


def test_gitlab_gateway_reconciliation_is_idempotent_at_the_api_boundary(
    tmp_path: Path,
) -> None:
    """Two real gateway updates converge on one exact rendered description."""
    commands: list[tuple[str, ...]] = []
    responses = iter([_mr_payload(), _mr_payload()])
    draft = MergeRequestDraft(
        title="Draft: decision",
        description="Inline text: do not Closes #12 here.",
        is_draft=True,
        decision_review=_decision_review(),
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        response = next(responses)
        response["title"] = draft.title
        response["description"] = next(
            part.removeprefix("description=")
            for part in command
            if part.startswith("description=")
        )
        response["draft"] = True
        return CommandResult(0, json.dumps(response), "")

    gateway = GitLabGateway(_config(tmp_path), api)
    existing = _mr_evidence("Old.\nCLOSES # 12\nInline text: do not Closes #12 here.")
    first = gateway.reconcile_merge_request(existing, issue_iid=12, draft=draft)
    second = gateway.reconcile_merge_request(
        first.model_copy(update={"description": first.description + "\ncloses # 12"}),
        issue_iid=12,
        draft=draft,
    )

    assert first.description == second.description
    assert first.description.count("<!-- agent-work-decision-review:v1 -->") == 1
    assert sum(line == "Closes #12" for line in first.description.splitlines()) == 1
    assert first.description.endswith("\n\nCloses #12")
    assert "Inline text: do not Closes #12 here." in first.description
    assert len(commands) == 2
    assert not any(
        part.startswith("draft=") for command in commands for part in command
    )


def test_gitlab_gateway_preserves_an_existing_draft_title_and_evidence(
    tmp_path: Path,
) -> None:
    """Ordinary metadata must not downgrade an existing GitLab Draft."""
    commands: list[tuple[str, ...]] = []
    existing = _mr_evidence("Old details").model_copy(
        update={"title": "Draft: earlier decision", "is_draft": True}
    )
    draft = MergeRequestDraft(title="Ordinary update", description="Details")

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        response = _mr_payload(
            next(
                part.removeprefix("description=")
                for part in command
                if part.startswith("description=")
            )
        )
        response.update({"title": existing.title, "draft": True})
        return CommandResult(0, json.dumps(response), "")

    reconciled = GitLabGateway(_config(tmp_path), api).reconcile_merge_request(
        existing, issue_iid=12, draft=draft
    )

    assert reconciled.title == existing.title
    assert reconciled.is_draft is True
    assert not any(part.startswith("title=") for part in commands[0])
    assert not any(part.startswith("draft=") for part in commands[0])


@pytest.mark.parametrize(
    ("operation", "mismatched_field"),
    [
        ("create", "title"),
        ("create", "description"),
        ("create", "draft"),
        ("reconcile", "title"),
        ("reconcile", "description"),
        ("reconcile", "draft"),
    ],
)
def test_gitlab_gateway_rejects_response_that_does_not_apply_exact_metadata(
    tmp_path: Path, operation: str, mismatched_field: str
) -> None:
    """GitLab success is insufficient unless its returned metadata is exact."""
    draft = MergeRequestDraft(
        title="Draft: decision", description="Details", is_draft=True
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        response = _mr_payload("Details\n\nCloses #12")
        response.update({"title": draft.title, "draft": draft.is_draft})
        response[mismatched_field] = {
            "title": "wrong title",
            "description": "wrong description",
            "draft": False,
        }[mismatched_field]
        assert any(part.startswith("description=") for part in command)
        return CommandResult(0, json.dumps(response), "")

    gateway = GitLabGateway(_config(tmp_path), api)
    if operation == "create":
        with pytest.raises(GitLabError, match="did not apply"):
            gateway.create_merge_request(
                branch="agent/issue-12", target="main", issue_iid=12, draft=draft
            )
    else:
        with pytest.raises(GitLabError, match="did not apply"):
            gateway.reconcile_merge_request(
                _mr_evidence("old"),
                issue_iid=12,
                draft=draft,
            )


def test_gitlab_eligibility_honors_valid_split_dependency_markers(
    tmp_path: Path,
) -> None:
    child_fingerprint = hashlib.sha256(b"310967:12:child").hexdigest()
    independent_fingerprint = hashlib.sha256(b"310967:12:independent").hexdigest()
    child = _issue_payload(20)
    child["description"] = (
        f"<!-- agent-work-split:v1 fingerprint={child_fingerprint} parent=12 key=child "
        "dependencies=19 -->\n\nParent: #12\nDepends-on: #19"
    )
    prerequisite = _issue_payload(19)
    prerequisite.update({"state": "opened", "labels": ["agent::awaiting-human"]})
    independent = _issue_payload(21)
    independent["description"] = (
        f"<!-- agent-work-split:v1 fingerprint={independent_fingerprint} parent=12 key=independent "
        "dependencies=none -->\n\nParent: #12"
    )
    issues = [child, prerequisite, independent]

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[command.index("-X") + 2].endswith("/issues")
        state = next(
            part.removeprefix("state=") for part in command if part.startswith("state=")
        )
        payload = (
            issues
            if state == "all"
            else [issue for issue in issues if issue["state"] == state]
        )
        return CommandResult(0, json.dumps(payload), "")

    gateway = GitLabGateway(_config(tmp_path), fake)

    assert [issue.iid for issue in gateway.list_eligible()] == [21]
    prerequisite["state"] = "closed"
    assert [issue.iid for issue in gateway.list_eligible()] == [20, 21]


def test_gitlab_eligibility_transfers_closed_dependency_to_its_open_children(
    tmp_path: Path,
) -> None:
    """Admission follows marker-derived descendants independently of display labels."""
    issues = [
        _issue_payload(264, ["agent-prereq_999"])
        | {"description": render_split_marker(310967, 260, "dependent", (262, 263))},
        _issue_payload(262) | {"state": "closed"},
        _issue_payload(263) | {"state": "closed"},
        _issue_payload(271)
        | {"description": render_split_marker(310967, 263, "first", ())},
        _issue_payload(272)
        | {"description": render_split_marker(310967, 263, "second", (271,))},
        _issue_payload(273)
        | {"description": render_split_marker(310967, 263, "third", (272,))},
    ]

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        assert endpoint == "projects/310967/issues"
        return CommandResult(0, json.dumps(issues), "")

    gateway = GitLabGateway(_config(tmp_path), api)

    assert 264 not in {issue.iid for issue in gateway.list_eligible()}
    for issue in issues:
        if issue["iid"] in {271, 272, 273}:
            issue["state"] = "closed"
    issues[0]["labels"] = ["agent-prereq_271"]
    assert 264 in {issue.iid for issue in gateway.list_eligible()}


def test_gitlab_eligibility_prioritizes_exact_labels_and_absolute_creation_time(
    tmp_path: Path,
) -> None:
    """Fresh work is ranked independently of GitLab's inventory ordering."""
    issues = [
        _issue_payload(90, ["agent-priority::low"], created_at="2025-01-01T00:00:00Z"),
        _issue_payload(10, [], created_at="2025-12-30T00:00:00Z"),
        _issue_payload(
            30,
            [],
            created_at="2026-01-01T01:00:00+01:00",
        ),
        _issue_payload(
            29,
            ["agent-priority::urgent"],
            created_at="2025-12-31T19:00:00-05:00",
        ),
        _issue_payload(60, ["agent-priority::high"], created_at="2027-01-01T00:00:00Z"),
    ]
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, json.dumps(issues), "")

    eligible = GitLabGateway(_config(tmp_path), fake).list_eligible()

    assert [issue.iid for issue in eligible] == [60, 10, 29, 30, 90]
    assert not any("/notes" in command[command.index("-X") + 2] for command in commands)


def test_gitlab_eligibility_keeps_high_priority_ineligible_candidates_excluded(
    tmp_path: Path,
) -> None:
    """Priority never bypasses confidentiality, lifecycle, or split prerequisites."""
    fingerprint = hashlib.sha256(b"310967:12:child").hexdigest()
    dependent = _issue_payload(23, ["agent-priority::high"])
    dependent["description"] = (
        f"<!-- agent-work-split:v1 fingerprint={fingerprint} parent=12 key=child "
        "dependencies=24 -->"
    )
    prerequisite = _issue_payload(24, ["agent::awaiting-human"])
    prerequisite["state"] = "opened"
    issues = [
        _issue_payload(21, ["agent-priority::high"]),
        _issue_payload(22, ["agent-priority::high", "agent::running"]),
        dependent,
        prerequisite,
    ]
    issues[0]["confidential"] = True

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        state = next(
            part.removeprefix("state=") for part in command if part.startswith("state=")
        )
        payload = (
            issues
            if state == "all"
            else [issue for issue in issues if issue["state"] == "opened"]
        )
        return CommandResult(0, json.dumps(payload), "")

    assert GitLabGateway(_config(tmp_path), fake).list_eligible() == ()


def test_gitlab_eligibility_rejects_invalid_priority_timestamp(tmp_path: Path) -> None:
    """Malformed timestamp evidence cannot silently select fresh work."""
    invalid = _issue_payload(12, created_at="not-a-timestamp")

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        del command
        return CommandResult(0, json.dumps([invalid]), "")

    with pytest.raises(GitLabError, match="invalid issue creation time"):
        GitLabGateway(_config(tmp_path), fake).list_eligible()


def test_gitlab_split_mutations_use_exact_marker_bound_evidence(tmp_path: Path) -> None:
    """The real gateway creates, notes, and closes only exact split evidence."""
    commands: list[tuple[str, ...]] = []
    marker = (
        "<!-- agent-work-split:v1 fingerprint="
        + "a" * 64
        + " parent=12 key=child dependencies=none -->"
    )
    child = _issue_payload(20)
    child.update({"title": "Child", "description": marker})
    parent = _issue_payload(12)
    parent.update({"state": "closed"})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint.endswith("/issues") and method == "POST":
            return CommandResult(0, json.dumps(child), "")
        if endpoint.endswith("/issues/20") and method == "GET":
            return CommandResult(0, json.dumps(child), "")
        if endpoint.endswith("/issues/12/notes") and method == "GET":
            return CommandResult(0, "[]", "")
        if endpoint.endswith("/issues/12/notes"):
            return CommandResult(0, json.dumps({"id": 1}), "")
        if endpoint.endswith("/issues/12") and method == "PUT":
            return CommandResult(0, json.dumps(parent), "")
        raise AssertionError(endpoint)

    gateway = GitLabGateway(_config(tmp_path), fake)
    assert (
        gateway.create_split_child(title="Child", description=marker, marker=marker).iid
        == 20
    )
    gateway.ensure_decomposition_note(12, "note")
    assert gateway.close_issue(12).state == "closed"
    assert any(
        "title=Child" in command and f"description={marker}" in command
        for command in commands
    )
    assert not any(
        field.startswith("labels=") for command in commands for field in command
    )
    assert any("body=note" in command for command in commands)
    assert any("state_event=close" in command for command in commands)


@pytest.mark.parametrize(
    "unsafe_description",
    (
        pytest.param("marker\\nunsafe", id="literal-backslash-n"),
        pytest.param("marker\\r\\nunsafe", id="literal-backslash-r-n"),
        pytest.param("marker%0aunsafe", id="lowercase-percent-lf"),
        pytest.param("marker%0Dunsafe", id="uppercase-percent-cr"),
    ),
)
def test_gitlab_split_child_rejects_unsafe_text_before_post(
    tmp_path: Path, unsafe_description: str
) -> None:
    """Split children share the publication text boundary before any transport call."""
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "{}", "")

    with pytest.raises(GitLabError, match="linebreak"):
        GitLabGateway(_config(tmp_path), fake).create_split_child(
            title="Child", description=unsafe_description, marker="<!-- marker -->"
        )

    assert commands == []


def test_gitlab_split_child_reads_back_exact_created_issue_evidence(
    tmp_path: Path,
) -> None:
    """A child POST is confirmed only by a same-project GET of its exact issue."""
    marker = "<!-- agent-work-split:v1 fingerprint=" + "a" * 64 + " -->"
    description = f"Child body\n\n{marker}"
    child = _issue_payload(20)
    child.update(
        {
            "title": "Child",
            "description": description,
            "labels": ["agent::factory"],
            "state": "opened",
        }
    )
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        if endpoint == "projects/310967/issues":
            return CommandResult(0, json.dumps(child), "")
        if endpoint == "projects/310967/issues/20":
            return CommandResult(0, json.dumps(child), "")
        raise AssertionError(endpoint)

    issue = GitLabGateway(_config(tmp_path), fake).create_split_child(
        title="Child", description=description, marker=marker
    )

    assert (issue.iid, issue.title, issue.description, issue.labels, issue.state) == (
        20,
        "Child",
        description,
        ("agent::factory",),
        "opened",
    )
    assert marker.encode() in description.encode()
    assert commands[-1][commands[-1].index("-X") + 2] == "projects/310967/issues/20"


def test_gitlab_split_child_rejects_corrupt_post_and_readback_description(
    tmp_path: Path,
) -> None:
    marker = "<!-- agent-work-split:v1 fingerprint=" + "a" * 64 + " -->"
    requested = f"Requested body\n\n{marker}"
    corrupt = _issue_payload(20)
    corrupt.update(
        {
            "title": "Child",
            "description": f"Corrupt\n\n{marker}",
            "labels": ["agent::factory"],
            "state": "opened",
        }
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        endpoint = command[command.index("-X") + 2]
        if endpoint in {"projects/310967/issues", "projects/310967/issues/20"}:
            return CommandResult(0, json.dumps(corrupt), "")
        raise AssertionError(endpoint)

    with pytest.raises(GitLabError, match="split child evidence"):
        GitLabGateway(_config(tmp_path), fake).create_split_child(
            title="Child", description=requested, marker=marker
        )


@pytest.mark.parametrize(
    ("child_update", "parent_update", "message"),
    [
        ({"description": "missing"}, {}, "split child evidence"),
        ({}, {"state": "opened"}, "did not close"),
    ],
)
def test_gitlab_split_mutations_reject_inexact_evidence(
    tmp_path: Path,
    child_update: dict[str, object],
    parent_update: dict[str, object],
    message: str,
) -> None:
    marker = (
        "<!-- agent-work-split:v1 fingerprint="
        + "a" * 64
        + " parent=12 key=child dependencies=none -->"
    )
    child = _issue_payload(20)
    child.update({"title": "Child", "description": marker, **child_update})
    parent = _issue_payload(12)
    parent.update({"state": "closed", **parent_update})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            json.dumps(
                child
                if command[command.index("-X") + 2].endswith("/issues")
                else parent
            ),
            "",
        )

    gateway = GitLabGateway(_config(tmp_path), fake)
    with pytest.raises(GitLabError, match=message):
        if child_update:
            gateway.create_split_child(title="Child", description=marker, marker=marker)
        else:
            gateway.close_issue(12)


def test_gitlab_reopen_is_one_atomic_state_and_lifecycle_projection_mutation(
    tmp_path: Path,
) -> None:
    """Reopen clears every lifecycle label in the same verified issue PUT."""
    commands: list[tuple[str, ...]] = []
    reopened = _issue_payload(12)
    reopened.update({"state": "opened", "labels": []})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, json.dumps(reopened), "")

    issue = GitLabGateway(_config(tmp_path), fake).reopen_issue(12)

    assert issue.state == "opened"
    puts = [command for command in commands if "PUT" in command]
    assert len(puts) == 1
    assert "state_event=reopen" in puts[0]
    assert f"remove_labels={','.join(LIFECYCLE_LABELS)}" in puts[0]


def test_gitlab_split_note_is_idempotent_for_exact_existing_body(
    tmp_path: Path,
) -> None:
    """An exact durable decomposition note is never posted a second time."""
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        if endpoint.endswith("/issues/12/notes"):
            return CommandResult(
                0,
                json.dumps([_note_payload(1, body="existing decomposition")]),
                "",
            )
        raise AssertionError(endpoint)

    GitLabGateway(_config(tmp_path), fake).ensure_decomposition_note(
        12, "existing decomposition"
    )

    assert not any(command[command.index("-X") + 1] == "POST" for command in commands)


@pytest.mark.parametrize(
    "description",
    [
        "<!-- agent-work-split:v1 fingerprint=bad parent=12 key=child dependencies=none -->",
        "<!-- agent-work-split:v1 fingerprint="
        + hashlib.sha256(b"310967:12:child").hexdigest()
        + " parent=12 key=child dependencies=20 -->",
        "<!-- agent-work-split:v1 fingerprint="
        + hashlib.sha256(b"310967:12:child").hexdigest()
        + " parent=12 key=child dependencies=999 -->",
    ],
)
def test_gitlab_eligibility_fails_closed_for_malformed_or_invalid_split_dependencies(
    tmp_path: Path, description: str
) -> None:
    issue = _issue_payload(20)
    issue["description"] = description

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[command.index("-X") + 2].endswith("/issues")
        return CommandResult(0, json.dumps([issue]), "")

    with pytest.raises(GitLabError):
        GitLabGateway(_config(tmp_path), fake).list_eligible()


@pytest.mark.parametrize(
    ("labels", "state", "confidential", "eligible"),
    [
        ([], "opened", False, True),
        (["customer::priority"], "opened", False, True),
        (["agent::running"], "opened", False, False),
        (["agent::awaiting-human"], "opened", False, False),
        (["agent::blocked"], "opened", False, False),
        (["agent::failed"], "opened", False, False),
        ([], "opened", True, False),
        ([], "closed", False, False),
    ],
)
def test_gitlab_eligibility_excludes_every_ineligible_issue_kind(
    tmp_path: Path,
    labels: list[str],
    state: str,
    *,
    confidential: bool,
    eligible: bool,
) -> None:
    payload = _issue_payload(labels=labels)
    payload.update({"state": state, "confidential": confidential})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert "state=all" in command
        assert "order_by=created_at" in command
        assert "sort=asc" in command
        assert not any(part.startswith("labels=") for part in command)
        return CommandResult(0, json.dumps([payload]), "")

    issues = GitLabGateway(_config(tmp_path), fake).list_eligible()

    assert bool(issues) is eligible


def test_gitlab_eligible_inventory_reads_every_oldest_first_page(
    tmp_path: Path,
) -> None:
    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert "state=all" in command
        page_field = next(part for part in command if part.startswith("page="))
        page = int(page_field.removeprefix("page="))
        payload = (
            [_issue_payload(iid) for iid in range(1, 101)]
            if page == 1
            else [_issue_payload(101)]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(_config(tmp_path), fake).list_eligible()

    assert [issue.iid for issue in issues] == list(range(1, 102))


def test_gitlab_projected_inventory_reads_every_issue_page(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        page_field = next(
            (part for part in command if part.startswith("page=")), "page=1"
        )
        page = int(page_field.removeprefix("page="))
        payload = (
            [_issue_payload(iid, ["agent::running"]) for iid in range(1, 101)]
            if page == 1
            else [_issue_payload(101, ["agent::running"])]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(config, fake).list_projected()

    assert len(issues) == 101
    assert issues[-1].iid == 101


def test_gitlab_closed_projected_inventory_is_closed_only_sorted_and_paginated(
    tmp_path: Path,
) -> None:
    """Closed housekeeping never expands the existing all-state projection scan."""
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        page = int(
            next(part for part in command if part.startswith("page=")).removeprefix(
                "page="
            )
        )
        payload = (
            [_issue_payload(iid, ["agent::running"]) for iid in range(100, 0, -1)]
            if page == 1
            else [
                _issue_payload(102, ["unrelated"]),
                _issue_payload(101, ["agent::blocked"]),
            ]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(config, fake).list_closed_projected()

    assert [issue.iid for issue in issues] == [*range(1, 101), 101]
    assert len(calls) == 2
    assert all("state=closed" in command for command in calls)
    assert all("per_page=100" in command for command in calls)


def test_gitlab_open_mr_inventory_reads_every_page_before_reconciliation(
    tmp_path: Path,
) -> None:
    """An ambiguous page-two MR cannot be hidden behind a full first page."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        page = int(
            next(part for part in command if part.startswith("page=")).removeprefix(
                "page="
            )
        )
        payload = [_mr_payload() for _ in range(100)] if page == 1 else [_mr_payload()]
        return CommandResult(0, json.dumps(payload), "")

    inventory = GitLabGateway(_config(tmp_path), fake).open_merge_requests_by_branch(
        "agent/issue-12"
    )

    assert len(inventory) == 101
    assert [
        next(part for part in command if part.startswith("page=")) for command in calls
    ] == ["page=1", "page=2"]
    assert all("per_page=100" in command for command in calls)


def test_gitlab_open_mr_inventory_rejects_excessive_pages(tmp_path: Path) -> None:
    """A non-terminating MR inventory fails closed at its bounded page limit."""
    calls = 0
    full_page = json.dumps([{}] * 100)

    def fake(*_args: object, **_kwargs: object) -> CommandResult:
        nonlocal calls
        calls += 1
        return CommandResult(0, full_page, "")

    gateway = GitLabGateway(
        _config(tmp_path),
        fake,
    )

    with pytest.raises(GitLabError, match="too many merge request pages"):
        gateway.open_merge_requests_by_branch("agent/issue-12")
    assert calls == 100


def test_gitlab_issue_notes_are_paginated_and_deterministically_ordered(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        page = int(
            next(part for part in command if part.startswith("page=")).removeprefix(
                "page="
            )
        )
        payload = (
            [
                _note_payload(
                    note_id,
                    created_at=f"2026-01-01T00:{note_id % 60:02d}:00Z",
                )
                for note_id in range(100, 0, -1)
            ]
            if page == 1
            else [_note_payload(101, created_at="2026-01-01T00:59:00Z")]
        )
        return CommandResult(0, json.dumps(payload), "")

    notes = GitLabGateway(_config(tmp_path), fake).fetch_issue_notes(12)

    assert len(notes) == 101
    assert [(note.created_at, note.id) for note in notes] == sorted(
        (note.created_at, note.id) for note in notes
    )
    assert all("projects/310967/issues/12/notes" in command for command in calls)
    assert all("order_by=created_at" in command for command in calls)
    assert all("sort=asc" in command for command in calls)
    assert all("per_page=100" in command for command in calls)


def test_gitlab_human_handoff_is_paginated_factory_owned_and_sha_idempotent(
    tmp_path: Path,
) -> None:
    """The human handoff is one factory-owned note for each exact MR SHA."""
    commands: list[tuple[str, ...]] = []
    notes: dict[int, list[dict[str, object]]] = {
        1: [
            _note_payload(note_id, body="<!-- unrelated -->")
            for note_id in range(1, 101)
        ],
        2: [],
    }

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        if "POST" in command:
            return CommandResult(0, json.dumps({"id": 999}), "")
        assert endpoint.endswith("/issues/12/notes")
        page = int(
            next(field for field in command if field.startswith("page=")).removeprefix(
                "page="
            )
        )
        return CommandResult(0, json.dumps(notes[page]), "")

    gateway = GitLabGateway(_config(tmp_path), api)
    mr = _mr_evidence()

    gateway.ensure_human_handoff(issue_iid=12, merge_request=mr)

    first_post = next(command for command in commands if "POST" in command)
    first_body = next(
        field.removeprefix("body=") for field in first_post if field.startswith("body=")
    )
    assert "<!-- agent-work-human-handoff:v1 key=" in first_body
    assert mr.web_url in first_body
    assert "approve and merge" in first_body
    assert "actionable general or inline feedback" in first_body
    assert "Only humans approve or merge" in first_body
    assert [
        field
        for command in commands
        if "POST" not in command
        for field in command
        if field.startswith("page=")
    ] == ["page=1", "page=2"]

    notes[1] = [_note_payload(101, body=first_body)]
    commands.clear()
    gateway.ensure_human_handoff(issue_iid=12, merge_request=mr)
    assert sum("POST" in command for command in commands) == 1

    notes[1] = [
        _note_payload(102, body=first_body) | {"author": {"username": "factory-bot"}}
    ]
    commands.clear()
    gateway.ensure_human_handoff(issue_iid=12, merge_request=mr)
    assert not any("POST" in command for command in commands)

    commands.clear()
    gateway.ensure_human_handoff(
        issue_iid=12, merge_request=mr.model_copy(update={"sha": "b" * 40})
    )
    assert sum("POST" in command for command in commands) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1},
        [_note_payload(1), _note_payload(1, body="conflicting")],
        [_note_payload(1, created_at="not-a-date")],
    ],
)
def test_gitlab_issue_notes_fail_closed_for_invalid_data(
    tmp_path: Path, payload: object
) -> None:
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda *_args, **_kwargs: CommandResult(0, json.dumps(payload), ""),
    )

    with pytest.raises(GitLabError, match="invalid issue note data"):
        gateway.fetch_issue_notes(12)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(1, "secret", "secret"), "request failed"),
        (CommandResult(0, "not-json", ""), "invalid JSON"),
    ],
)
def test_gitlab_gateway_redacts_failed_or_invalid_responses(
    tmp_path: Path, result: CommandResult, message: str
) -> None:
    gateway = GitLabGateway(_config(tmp_path), lambda *_args, **_kwargs: result)
    with pytest.raises(GitLabError, match=message):
        gateway.verify_project()


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://gitlab.example.com/group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
        (
            "git@gitlab.example.com:group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
    ],
)
def test_origin_identity_accepts_https_and_scp(
    origin: str, expected: tuple[str, str]
) -> None:
    assert _origin_identity(origin) == expected


def test_origin_identity_rejects_an_unusable_remote() -> None:
    with pytest.raises(RepositoryError):
        _origin_identity("local-path")


@pytest.mark.parametrize(
    "base_branch_fields",
    (
        pytest.param({}, id="omitted"),
        pytest.param({"base_branch": None}, id="null"),
    ),
)
def test_agent_work_config_requires_base_branch(
    tmp_path: Path, base_branch_fields: dict[str, object]
) -> None:
    payload = {
        "host": "gitlab.example.com",
        "project_id": 310967,
        "checkout": tmp_path / "checkout",
        "state_root": tmp_path / "state",
        **base_branch_fields,
    }

    with pytest.raises(ValidationError, match="base_branch"):
        AgentWorkConfig.model_validate(payload)


@pytest.mark.parametrize(
    "base_branch_fields",
    (
        pytest.param({}, id="missing"),
        pytest.param({"AGENT_WORK_BASE_BRANCH": ""}, id="empty"),
    ),
)
def test_discover_config_requires_base_branch_before_running_commands(
    tmp_path: Path, base_branch_fields: dict[str, str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def unexpected_command(
        command: tuple[str, ...], **_kwargs: object
    ) -> CommandResult:
        calls.append(command)
        raise AssertionError("configuration discovery called a command")

    environment = {
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
        **base_branch_fields,
    }

    with pytest.raises(RepositoryError, match=r"^AGENT_WORK_BASE_BRANCH is required$"):
        discover_config(
            current_directory=tmp_path,
            environment=environment,
            run_command=unexpected_command,
        )

    assert calls == []


def test_discover_config_is_read_only_and_confirms_numeric_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if "rev-parse" in command:
            return CommandResult(0, str(checkout), "")
        if "get-url" in command:
            return CommandResult(0, "https://gitlab.example.com/group/project.git", "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "main",
                    "path_with_namespace": "group/project",
                    "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                }
            ),
            "",
        )

    config = discover_config(
        current_directory=tmp_path,
        environment={
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
            "AGENT_WORK_BASE_BRANCH": "agent/work-base",
            "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
            "AGENT_WORK_GIT": str(tmp_path / "bin" / "git"),
            "AGENT_WORK_GLAB": str(tmp_path / "bin" / "glab"),
        },
        run_command=fake,
    )
    assert config.project_id == 310967
    assert config.default_branch == "main"
    assert config.base_branch == "agent/work-base"
    assert config.state_root.parent.name == "projects"
    assert not any("pull" in command for command in calls)


def test_run_subprocess_captures_output_and_wraps_timeouts(fp: FakeProcess) -> None:
    fp.register(["printf", "ok"], stdout="ok")
    assert run_subprocess(("printf", "ok")).stdout == "ok"

    def timeout(_process: object) -> None:
        raise subprocess.TimeoutExpired(("slow-command",), 1)

    fp.register(["slow-command"], callback=timeout)
    with pytest.raises(RepositoryError, match="timed out") as failure:
        run_subprocess(("slow-command",), timeout_seconds=0.001)
    assert "slow-command" not in str(failure.value)


def test_repository_prepares_publishes_and_inventories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()
    responses: dict[tuple[str, ...], CommandResult] = {}
    calls: list[tuple[str, ...]] = []
    head = "a" * 40

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal head
        calls.append(command)
        args = command[3:]
        if args in responses:
            return responses[args]
        if args == ("branch", "--show-current"):
            branch = (
                config.base_branch
                if Path(command[2]) == config.checkout
                else "agent/issue-12"
            )
            return CommandResult(0, f"{branch}\n", "")
        if args == ("status", "--porcelain"):
            return CommandResult(0, "M file\n", "")
        if args[:2] == ("commit", "-m"):
            head = "b" * 40
            responses[("status", "--porcelain")] = CommandResult(0, "", "")
            return CommandResult(0, "", "")
        if args == ("rev-parse", "HEAD") or args == (
            "rev-parse",
            "origin/agent/issue-12",
        ):
            return CommandResult(0, head, "")
        if args == ("rev-list", "--parents", "-n", "1", "b" * 40):
            return CommandResult(0, f"{'b' * 40} {'a' * 40}\n", "")
        if args[:2] == ("for-each-ref", "--format=%(refname:short)"):
            return CommandResult(0, "agent/issue-12\n", "")
        if args[:2] == ("worktree", "list"):
            return CommandResult(0, f"worktree {config.checkout}\n", "")
        if args[:2] == ("stash", "list"):
            return CommandResult(0, "agent-work recovery issue-12\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )
    with pytest.raises(RepositoryError, match="clean"):
        repository.refresh_base()
    responses[("status", "--porcelain")] = CommandResult(0, "", "")
    repository.refresh_base()
    worktree = repository.create_issue_worktree(12, "agent/issue-12", "main")
    assert worktree == config.state_root / "worktrees" / "12"
    responses[("status", "--porcelain")] = CommandResult(0, "M file\n", "")
    assert (
        repository.push_prepared(
            worktree,
            "agent/issue-12",
            repository.prepare_commit(
                worktree, "agent/issue-12", 12, expected_head="a" * 40
            ),
        )
        == "b" * 40
    )
    branches, worktrees, stashes = repository.inventory()
    assert branches == ("agent/issue-12",)
    assert worktrees == (str(config.checkout),)
    assert stashes == ("agent-work recovery issue-12",)
    assert any("pull" in command for command in calls)


@pytest.mark.parametrize(
    ("recorded_head", "initial_head", "default_head", "expects_merge"),
    [
        ("a" * 40, "a" * 40, "b" * 40, True),
        ("b" * 40, "b" * 40, "b" * 40, False),
        ("a" * 40, "b" * 40, "b" * 40, False),
        ("a" * 40, "b" * 40, "c" * 40, True),
    ],
)
def test_refresh_retained_worktree_fast_forwards_or_replays_exact_default(
    tmp_path: Path,
    recorded_head: str,
    initial_head: str,
    default_head: str,
    *,
    expects_merge: bool,
) -> None:
    """Untouched retained work converges by fast-forward without reset or overwrite."""
    config = _config(tmp_path)
    worktree = config.state_root / "worktrees" / "12"
    worktree.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []
    head = initial_head

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal head
        calls.append(command)
        arguments = _git_arguments(command)
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "--git-path", "MERGE_HEAD"):
            return CommandResult(0, str(tmp_path / "missing-MERGE_HEAD"), "")
        if arguments == ("fetch", "origin", "main"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{default_head}\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{head}\n", "")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            ancestor, descendant = arguments[2:]
            related = ancestor == descendant or (ancestor, descendant) in {
                (recorded_head, initial_head),
                (initial_head, default_head),
                (recorded_head, default_head),
            }
            return CommandResult(0 if related else 1, "", "")
        if arguments == ("merge", "--ff-only", "origin/main"):
            head = default_head
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    observed = repository.refresh_retained_worktree(
        worktree, "agent/issue-12", recorded_head
    )

    assert observed == default_head
    merge_calls = [
        command for command in calls if command[3:5] == ("merge", "--ff-only")
    ]
    assert bool(merge_calls) is expects_merge
    assert not any(command[3] in {"reset", "stash"} for command in calls)


@pytest.mark.parametrize("failure", ["unrelated-recorded-head", "inexact-merge-result"])
def test_refresh_retained_worktree_rejects_inexact_replay_or_merge_result(
    tmp_path: Path, failure: str
) -> None:
    """Replay ancestry and the post-merge head must both be exact."""
    config = _config(tmp_path)
    worktree = config.state_root / "worktrees" / "12"
    worktree.mkdir(parents=True)
    recorded_head = "a" * 40
    default_head = "b" * 40
    head = default_head if failure == "unrelated-recorded-head" else recorded_head
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = _git_arguments(command)
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "--git-path", "MERGE_HEAD"):
            return CommandResult(0, str(tmp_path / "missing-MERGE_HEAD"), "")
        if arguments == ("fetch", "origin", "main"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{default_head}\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{head}\n", "")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return CommandResult(
                1 if failure == "unrelated-recorded-head" else 0, "", ""
            )
        if arguments == ("merge", "--ff-only", "origin/main"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError):
        repository.refresh_retained_worktree(worktree, "agent/issue-12", recorded_head)

    assert not any(command[3] in {"reset", "stash"} for command in calls)


def test_refresh_retained_worktree_rejects_an_unrelated_recorded_replay_chain(
    tmp_path: Path,
) -> None:
    """Current-to-default ancestry cannot substitute for the recorded baseline."""
    config = _config(tmp_path)
    worktree = config.state_root / "worktrees" / "12"
    worktree.mkdir(parents=True)
    recorded_head = "a" * 40
    current_head = "b" * 40
    default_head = "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = _git_arguments(command)
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "--git-path", "MERGE_HEAD"):
            return CommandResult(0, str(tmp_path / "missing-MERGE_HEAD"), "")
        if arguments == ("fetch", "origin", "main"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{default_head}\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{current_head}\n", "")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return CommandResult(
                0 if arguments[2:] == (current_head, default_head) else 1, "", ""
            )
        raise AssertionError(f"unexpected Git command: {arguments}")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError, match="recorded baseline"):
        repository.refresh_retained_worktree(worktree, "agent/issue-12", recorded_head)

    assert not any(command[3] in {"merge", "reset", "stash"} for command in calls)


@pytest.mark.parametrize("path_state", ["wrong", "missing"])
def test_refresh_retained_worktree_requires_the_exact_existing_directory(
    tmp_path: Path, path_state: str
) -> None:
    """A wrong or absent retained worktree path stops before Git is invoked."""
    config = _config(tmp_path)
    exact_path = config.state_root / "worktrees" / "12"
    worktree = exact_path if path_state == "missing" else tmp_path / "other" / "12"
    if path_state == "wrong":
        worktree.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError, match="path is not exact"):
        repository.refresh_retained_worktree(worktree, "agent/issue-12", "a" * 40)

    assert calls == []


@pytest.mark.parametrize(
    ("branch", "status", "recorded_head", "initial_head", "default_head", "merge_head"),
    [
        ("other", "", "a" * 40, "a" * 40, "b" * 40, False),
        ("agent/issue-12", " M owned.py", "a" * 40, "a" * 40, "b" * 40, False),
        ("agent/issue-12", "", "c" * 40, "c" * 40, "b" * 40, False),
        ("agent/issue-12", "", "a" * 40, "a" * 40, "c" * 40, False),
        ("agent/issue-12", "", "a" * 40, "a" * 40, "b" * 40, True),
    ],
)
def test_refresh_retained_worktree_rejects_non_exact_or_authored_state(
    tmp_path: Path,
    branch: str,
    status: str,
    recorded_head: str,
    initial_head: str,
    default_head: str,
    *,
    merge_head: bool,
) -> None:
    """Wrong, dirty, ahead, divergent, and merging worktrees fail without mutation."""
    config = _config(tmp_path)
    worktree = config.state_root / "worktrees" / "12"
    worktree.mkdir(parents=True)
    merge_head_path = tmp_path / "MERGE_HEAD"
    if merge_head:
        merge_head_path.write_text("d" * 40, encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = _git_arguments(command)
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, f"{branch}\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        if arguments == ("rev-parse", "--git-path", "MERGE_HEAD"):
            return CommandResult(0, str(merge_head_path), "")
        if arguments == ("fetch", "origin", "main"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{default_head}\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{initial_head}\n", "")
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            ancestor, descendant = arguments[2:]
            related = (
                (ancestor, descendant) == (default_head, recorded_head)
                and recorded_head == initial_head
                and recorded_head != "a" * 40
            )
            return CommandResult(0 if related else 1, "", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError):
        repository.refresh_retained_worktree(worktree, "agent/issue-12", recorded_head)

    assert not any(command[3] in {"merge", "reset", "stash"} for command in calls)


def test_push_prepared_rejects_a_changed_head_without_push(tmp_path: Path) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, "b" * 40, "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError, match="no longer matches"):
        GitRepository(config, fake).push_prepared(
            tmp_path / "worktree", "agent/issue-12", "a" * 40
        )
    assert not any(call[3:4] == ("push",) for call in calls)


def test_bind_retained_commit_returns_only_the_exact_legacy_commit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, "b" * 40, "")
        if arguments == ("rev-list", "--parents", "-n", "1", "b" * 40):
            return CommandResult(0, f"{'b' * 40} {'a' * 40}\n", "")
        if arguments == (
            "rev-list",
            "--first-parent",
            "--count",
            f"{'a' * 40}..{'b' * 40}",
        ):
            return CommandResult(0, "1\n", "")
        if arguments == ("log", "-1", "--format=%s", "b" * 40):
            return CommandResult(0, "Resolve issue #12\n", "")
        return CommandResult(0, "", "")

    assert (
        GitRepository(config, fake).bind_retained_commit(
            tmp_path / "worktree", "agent/issue-12", 12, "a" * 40
        )
        == "b" * 40
    )
    assert not any(call[3:4] == ("push",) for call in calls)


@pytest.mark.parametrize("merge_head", (None, "c" * 40))
def test_observe_legacy_publication_candidate_reads_exact_dirty_identity(
    tmp_path: Path, merge_head: str | None
) -> None:
    """The legacy hook inspector reads NUL-safe Git evidence without mutating Git."""
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{'a' * 40}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(0 if merge_head else 1, f"{merge_head or ''}\n", "")
        if arguments == ("diff", "--cached", "--raw", "-z"):
            return CommandResult(0, "raw-index-evidence\0", "")
        if arguments == ("diff", "--cached", "--name-only", "-z"):
            return CommandResult(0, "src/a file.py\0src/z.py\0", "")
        if arguments == ("diff", "--name-only", "-z"):
            return CommandResult(0, "src/a file.py\0", "")
        if arguments == ("status", "--porcelain=v1", "-z"):
            return CommandResult(0, "MM src/a file.py\0", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    candidate = GitRepository(config, fake).observe_legacy_publication_candidate(
        tmp_path / "worktree"
    )

    assert candidate.branch == "agent/issue-12"
    assert candidate.head == "a" * 40
    assert (
        candidate.index_fingerprint
        == hashlib.sha256(b"raw-index-evidence\0").hexdigest()
    )
    assert candidate.staged_paths == ("src/a file.py", "src/z.py")
    assert candidate.unstaged_paths == ("src/a file.py",)
    assert candidate.merge_head == merge_head
    assert not candidate.has_untracked
    assert not candidate.has_unmerged
    assert not candidate.has_renames
    assert not any(
        command[3] in {"add", "commit", "merge", "push", "reset", "stash", "write-tree"}
        for command in calls
    )


@pytest.mark.parametrize("conflict_status", ("AA", "DD"))
def test_observe_legacy_publication_candidate_rejects_both_sides_conflict(
    tmp_path: Path, conflict_status: str
) -> None:
    """Both-added and both-deleted Git conflicts are unmerged work."""

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("status", "--porcelain=v1", "-z"):
            return CommandResult(0, f"{conflict_status} src/conflict.py\0", "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{'a' * 40}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments in {
            ("diff", "--cached", "--raw", "-z"),
            ("diff", "--cached", "--name-only", "-z"),
            ("diff", "--name-only", "-z"),
        }:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected Git command: {arguments}")

    candidate = GitRepository(
        _config(tmp_path), fake
    ).observe_legacy_publication_candidate(tmp_path / "worktree")

    assert candidate.has_unmerged


def test_retained_worktree_fingerprint_separates_two_file_content_boundaries(
    tmp_path: Path,
) -> None:
    """Moving delimiter-like bytes between files changes snapshot identity."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    first_path = worktree / "a"
    second_path = worktree / "b"
    header = b"b\0file\0" + b"0644\0"
    first_path.write_bytes(b"x\0" + header + b"y")
    second_path.write_bytes(b"z")
    first_path.chmod(0o644)
    second_path.chmod(0o644)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[3:] == ("diff", "--name-only", "-z")
        return CommandResult(0, "a\0b\0", "")

    repository = GitRepository(_config(tmp_path), fake)
    first = repository.retained_worktree_fingerprint(worktree)
    first_path.write_bytes(b"x")
    second_path.write_bytes(b"y\0" + header + b"z")

    assert repository.retained_worktree_fingerprint(worktree) != first


@pytest.mark.parametrize(
    ("status", "branch", "parents", "count", "subject"),
    (
        (
            "M changed.py\n",
            "agent/issue-12",
            "b" * 40 + " " + "a" * 40,
            "1",
            "Resolve issue #12",
        ),
        ("", "other", "b" * 40 + " " + "a" * 40, "1", "Resolve issue #12"),
        ("", "agent/issue-12", "b" * 40 + " " + "c" * 40, "1", "Resolve issue #12"),
        ("", "agent/issue-12", "b" * 40 + " " + "a" * 40, "2", "Resolve issue #12"),
        ("", "agent/issue-12", "b" * 40 + " " + "a" * 40, "1", "Other subject"),
    ),
)
def test_bind_retained_commit_rejects_nonexact_legacy_state(
    tmp_path: Path, status: str, branch: str, parents: str, count: str, subject: str
) -> None:
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, branch, "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, "b" * 40, "")
        if arguments == ("rev-list", "--parents", "-n", "1", "b" * 40):
            return CommandResult(0, f"{parents}\n", "")
        if arguments == (
            "rev-list",
            "--first-parent",
            "--count",
            f"{'a' * 40}..{'b' * 40}",
        ):
            return CommandResult(0, f"{count}\n", "")
        if arguments == ("log", "-1", "--format=%s", "b" * 40):
            return CommandResult(0, subject, "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError):
        GitRepository(config, fake).bind_retained_commit(
            tmp_path / "worktree", "agent/issue-12", 12, "a" * 40
        )
    assert not any(call[3:4] == ("push",) for call in calls)


def test_existing_mr_worktree_requires_the_recorded_remote_sha(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--list", "review/existing"):
            return CommandResult(0, "review/existing\n", "")
        if arguments == ("rev-parse", "origin/review/existing"):
            return CommandResult(0, "b" * 40, "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError, match="recorded merge request SHA"):
        repository.worktree_for_existing_branch(12, "review/existing", "a" * 40)


@pytest.mark.parametrize(
    ("branch", "head", "status"),
    [
        ("other", "a" * 40, "M src/example.py\n"),
        ("agent/issue-12", "b" * 40, "M src/example.py\n"),
        ("agent/issue-12", "a" * 40, ""),
    ],
)
def test_publication_rejects_wrong_branch_worker_commits_and_empty_changes(
    tmp_path: Path, branch: str, head: str, status: str
) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, branch, "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, head, "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )

    with pytest.raises(RepositoryError):
        repository.prepare_commit(
            tmp_path / "worktree", "agent/issue-12", 12, expected_head="a" * 40
        )


def test_agent_profiles_are_argv_only_and_invocation_redacts_secrets(
    tmp_path: Path,
) -> None:
    for name in ("codex", "claude", "pi"):
        command, stdin = _agent_command(name, f"/{name}", tmp_path, "prompt")
        assert command[0] == f"/{name}"
        assert (stdin == "prompt") is (name == "codex")
    with pytest.raises(WorkerProtocolError):
        _agent_command("other", "/other", tmp_path, "prompt")

    invoker = AgentInvoker(
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="token-value", stderr="token-value"
        ),
        environment={"AGENT_WORK_AGENT": "codex", "SECRET_TOKEN": "token-value"},
    )
    result = invoker.invoke(tmp_path, "prompt", 10)
    assert result.stdout == result.stderr == "[REDACTED]"


def test_agent_invoker_retains_only_the_shared_gitlab_token_for_issue_worktree(
    tmp_path: Path,
) -> None:
    """The AI and its children inherit only the checkout-scoped shared token."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    retained = "shared-gitlab-value"
    filtered = (
        "pass-path-value",
        "unrelated-token-value",
        "private-key-value",
        "password-value",
        "secret-value",
        "credential-value",
    )
    stale_direnv = {
        "DIRENV_DIFF": "stale-diff",
        "DIRENV_DIR": "stale-dir",
        "DIRENV_FILE": "stale-file",
        "DIRENV_IN_ENVRC": "1",
        "DIRENV_WATCHES": "stale-watches",
    }

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout=("usage: codex exec --add-dir DIR --enable FEATURE"),
                stderr="",
            )
        if command[-1] == "list":
            return SimpleNamespace(
                returncode=0, stdout="multi_agent stable true\n", stderr=""
            )
        diagnostic = " ".join((retained, *filtered))
        return SimpleNamespace(returncode=1, stdout=diagnostic, stderr=diagnostic)

    environment = {
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": "/codex",
        "GITLAB_TOKEN": retained,
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "91",
        "GITLAB_TOKEN_PASS_PATH": filtered[0],
        "UNRELATED_API_TOKEN": filtered[1],
        "SSH_PRIVATE_KEY": filtered[2],
        "DATABASE_PASSWORD": filtered[3],
        "CLIENT_SECRET": filtered[4],
        "CREDENTIAL_FILE": filtered[5],
        "ORDINARY_SETTING": "retained-setting",
        "GIT_CONFIG_COUNT": "4",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "!exec credential-helper",
        "GIT_CONFIG_KEY_2": "credential.useHttpPath",
        "GIT_CONFIG_VALUE_2": "true",
        "GIT_CONFIG_KEY_3": "core.askPass",
        "GIT_CONFIG_VALUE_3": "/bin/false",
        "GIT_CONFIG_PARAMETERS": "credential.helper=hostile",
        "GIT_CONFIG_GLOBAL": "/tmp/host-gitconfig",
        "GIT_CONFIG_NOSYSTEM": "1",
        "DIRENV_CONFIG": "retained-direnv-setting",
        **stale_direnv,
    }
    issue_worktree = tmp_path / "issue-worktree"
    issue_worktree.mkdir()
    assert not (issue_worktree / ".envrc").exists()
    invoker = AgentInvoker(run_command, environment=environment)

    invoker.preflight()
    result = invoker.invoke(issue_worktree, "prompt", 10)

    expected_environment = {
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": "/codex",
        "GITLAB_TOKEN": retained,
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "91",
        "ORDINARY_SETTING": "retained-setting",
        "DIRENV_CONFIG": "retained-direnv-setting",
    }
    environments = [cast(dict[str, str], kwargs["environment"]) for _, kwargs in calls]
    assert environments[:2] == [expected_environment, expected_environment]
    assert {
        key: value for key, value in environments[2].items() if key != "CODEX_HOME"
    } == expected_environment
    assert Path(str(environments[2]["CODEX_HOME"])).name == "codex"
    assert [kwargs["timeout_seconds"] for _, kwargs in calls] == [120, 120, 10]
    assert retained not in result.stdout + result.stderr
    assert all(value not in result.stdout + result.stderr for value in filtered)
    assert result.stdout == result.stderr
    assert result.stdout.split() == ["[REDACTED]"] * (len(filtered) + 1)


def test_private_codex_home_cleanup_failure_preserves_primary_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed invocation keeps its primary diagnostic when cleanup also fails."""
    source = tmp_path / "source"
    source.mkdir()
    for name in ("auth.json", "config.toml"):
        credential = source / name
        credential.write_text("credential")
        credential.chmod(0o600)

    def run_command(*_args: object, **_kwargs: object) -> CommandResult:
        raise RuntimeError("primary failure")

    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup failure")

    monkeypatch.setattr(worker_module.shutil, "rmtree", fail_cleanup)
    invoker = AgentInvoker(
        run_command,
        environment={"CODEX_HOME": str(source)},
        temporary_directory=tmp_path,
    )
    with pytest.raises(
        RuntimeError, match="primary failure; private Codex home cleanup failed"
    ):
        invoker._run_worker_command(
            "codex", ("codex", "exec"), stdin=None, timeout_seconds=1, environment={}
        )


def test_private_codex_home_cleanup_failure_is_reported_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful command cannot silently retain its private credential home."""
    source = tmp_path / "source"
    source.mkdir()
    for name in ("auth.json", "config.toml"):
        credential = source / name
        credential.write_text("credential")
        credential.chmod(0o600)

    def fail_cleanup(_path: Path) -> None:
        raise OSError("cleanup failure")

    monkeypatch.setattr(worker_module.shutil, "rmtree", fail_cleanup)
    invoker = AgentInvoker(
        lambda *_args, **_kwargs: CommandResult(0, "", ""),
        environment={"CODEX_HOME": str(source)},
        temporary_directory=tmp_path,
    )
    with pytest.raises(RuntimeError, match="private Codex home cleanup failed"):
        invoker._run_worker_command(
            "codex", ("codex", "exec"), stdin=None, timeout_seconds=1, environment={}
        )


def test_codex_command_scopes_policy_writes_to_the_prepared_checkout(
    tmp_path: Path,
) -> None:
    command, stdin = _agent_command("codex", "/codex", tmp_path, "prompt")

    assert command == (
        "/codex",
        "exec",
        "-C",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--enable",
        "multi_agent",
        "--add-dir",
        str(tmp_path / ".agents"),
        "--add-dir",
        str(tmp_path / "template" / ".agents"),
        "--color",
        "never",
        "-",
    )
    assert stdin == "prompt"


def test_recovery_advisory_codex_process_has_no_original_write_or_broker_access(
    tmp_path: Path,
) -> None:
    """The bounded advisory receives a read-only command and no GitLab authority."""
    original = tmp_path / "original-worktree"
    original.mkdir()
    advisory_checkout = tmp_path / "advisory-checkout"
    advisory_checkout.mkdir()
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    for name in ("auth.json", "config.toml"):
        credential = source_home / name
        credential.write_text("local Codex configuration")
        credential.chmod(0o600)
    prompt = "RECOVERY_ADVISORY_V1\nReturn deferred advice for issue 12."
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        environment = cast(Mapping[str, str], kwargs["environment"])
        private_home = Path(environment["CODEX_HOME"])
        assert (private_home / "auth.json").is_symlink()
        assert not (private_home / "config.toml").exists()
        calls.append((command, kwargs))
        return CommandResult(0, '{"action":"defer"}', "")

    invoker = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
            "GITLAB_TOKEN": "gitlab-test-token",
        },
        temporary_directory=tmp_path,
    )

    result = invoker.invoke(advisory_checkout, prompt, 120)

    assert result.returncode == 0 and result.stdout == '{"action":"defer"}'
    assert len(calls) == 1
    command, options = calls[0]
    environment = cast(Mapping[str, str], options["environment"])
    assert command[:4] == ("/codex", "exec", "-C", str(advisory_checkout))
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "features.shell_tool=false" in command
    assert "features.unified_exec=false" in command
    assert "--add-dir" not in command
    assert not any("mcp_servers" in argument for argument in command)
    assert str(original) not in " ".join(command)
    assert str(original) not in " ".join(environment.values())
    assert "GITLAB_TOKEN" not in environment
    assert not any("BROKER" in name or "CANDIDATE" in name for name in environment)
    assert options["stdin"] == prompt and options["timeout_seconds"] == 120
    with pytest.raises(ValueError, match="bounded Codex invocation"):
        invoker.invoke(advisory_checkout, prompt, 121)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("agent", "required_flags"),
    [
        (
            "claude",
            (
                "--tools",
                "",
                "--permission-mode",
                "plan",
                "--no-session-persistence",
                "--print",
            ),
        ),
        (
            "pi",
            (
                "--no-tools",
                "--no-extensions",
                "--no-skills",
                "--no-session",
                "--print",
                "--mode",
                "text",
            ),
        ),
    ],
)
def test_recovery_advisory_portable_agents_have_no_tools_or_broker_access(
    tmp_path: Path, agent: str, required_flags: tuple[str, ...]
) -> None:
    original = tmp_path / "original-worktree"
    original.mkdir()
    advisory_checkout = tmp_path / "advisory-checkout"
    advisory_checkout.mkdir()
    prompt = "RECOVERY_ADVISORY_V1\nReturn deferred advice for issue 12."
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        return CommandResult(0, '{"action":"defer"}', "")

    invoker = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": agent,
            "AGENT_WORK_AGENT_PATH": f"/{agent}",
            "GITLAB_TOKEN": "gitlab-test-token",
        },
    )

    result = invoker.invoke(advisory_checkout, prompt, 120)

    assert result.returncode == 0 and result.stdout == '{"action":"defer"}'
    assert len(calls) == 1
    command, options = calls[0]
    environment = cast(Mapping[str, str], options["environment"])
    assert command[0] == f"/{agent}"
    assert all(part in command for part in required_flags)
    if agent == "claude":
        assert command[command.index("--tools") + 1] == ""
        assert command[command.index("--permission-mode") + 1] == "plan"
    else:
        assert command[command.index("--mode") + 1] == "text"
    assert str(original) not in " ".join(command)
    assert str(original) not in " ".join(environment.values())
    assert "GITLAB_TOKEN" not in environment
    assert not any("BROKER" in name or "CANDIDATE" in name for name in environment)
    assert options["timeout_seconds"] == 120
    assert options["stdin"] is None
    with pytest.raises(ValueError, match="bounded"):
        invoker.invoke(advisory_checkout, prompt, 121)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "usage: codex exec"),
        (0, "usage: codex exec\n  --sandbox workspace-write"),
        (0, "usage: codex exec"),
    ],
)
def test_codex_preflight_rejects_an_unsupported_exec(
    tmp_path: Path, returncode: int, stdout: str
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="failure")

    invoker = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "SECRET_TOKEN": "token-value",
        },
    )

    with pytest.raises(WorkerProtocolError):
        invoker.preflight()

    assert calls == [
        (
            ("/codex", "exec", "--help"),
            {
                "stdin": None,
                "timeout_seconds": 120,
                "environment": {
                    "AGENT_WORK_AGENT": "codex",
                    "AGENT_WORK_AGENT_PATH": "/codex",
                },
            },
        ),
        (
            ("/codex", "exec", "--help"),
            {
                "stdin": None,
                "timeout_seconds": 120,
                "environment": {
                    "AGENT_WORK_AGENT": "codex",
                    "AGENT_WORK_AGENT_PATH": "/codex",
                },
            },
        ),
    ]


def test_codex_preflight_accepts_exec_with_scoped_directory_support() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "usage: codex exec --add-dir DIR --enable FEATURE"
                if command[-1] == "--help"
                else "multi_agent stable true\n"
            ),
            stderr="",
        )

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    invoker.preflight()

    assert calls[0][0] == ("/codex", "exec", "--help")
    assert calls[0][1]["timeout_seconds"] == 120
    assert calls[1][0] == ("/codex", "features", "list")


def test_codex_preflight_retries_one_anomalous_help_probe_before_features() -> None:
    """A transient missing exec capability is retried once before multi-agent is read."""
    calls: list[tuple[str, ...]] = []
    help_results = iter(
        (
            SimpleNamespace(
                returncode=1, stdout="usage: codex exec", stderr="transient"
            ),
            SimpleNamespace(
                returncode=0,
                stdout="usage: codex exec --add-dir DIR --enable FEATURE",
                stderr="",
            ),
        )
    )

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command == ("/codex", "exec", "--help"):
            return next(help_results)
        return SimpleNamespace(
            returncode=0, stdout="multi_agent stable true\n", stderr=""
        )

    AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    ).preflight()

    assert calls == [
        ("/codex", "exec", "--help"),
        ("/codex", "exec", "--help"),
        ("/codex", "features", "list"),
    ]


def test_codex_preflight_fails_closed_after_one_persistent_missing_exec_probe() -> None:
    """Two invalid exec probes retain the established capability diagnostic."""
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout="usage: codex exec --enable FEATURE", stderr=""
        )

    with pytest.raises(WorkerProtocolError, match="--add-dir"):
        AgentInvoker(
            run_command,
            environment={
                "AGENT_WORK_AGENT": "codex",
                "AGENT_WORK_AGENT_PATH": "/codex",
            },
        ).preflight()

    assert calls == [("/codex", "exec", "--help")] * 2


def test_codex_preflight_requires_service_child_delegation_capability() -> None:
    """The unattended launch proves multi-agent support before invocation."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="usage: codex exec --add-dir DIR --enable FEATURE",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="multi_agent stable true\n",
            stderr="",
        )

    invoker = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "GITLAB_TOKEN": "shared-token",
            "SECRET_TOKEN": "filtered-token",
            "GIT_CONFIG_COUNT": "1",
        },
    )

    invoker.preflight()

    assert [call[0] for call in calls] == [
        ("/codex", "exec", "--help"),
        ("/codex", "features", "list"),
    ]
    assert [call[1] for call in calls] == [
        {
            "stdin": None,
            "timeout_seconds": 120,
            "environment": {
                "AGENT_WORK_AGENT": "codex",
                "AGENT_WORK_AGENT_PATH": "/codex",
                "GITLAB_TOKEN": "shared-token",
            },
        }
    ] * 2


def test_codex_preflight_defers_when_child_delegation_is_unavailable() -> None:
    """An unavailable multi-agent feature fails before any worker invocation."""
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="usage: codex exec --add-dir DIR --enable FEATURE",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="multi_agent stable false\n",
            stderr="",
        )

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    with pytest.raises(WorkerProtocolError, match="multi-agent"):
        invoker.preflight()

    assert calls == [
        ("/codex", "exec", "--help"),
        ("/codex", "features", "list"),
    ]


def test_codex_preflight_rejects_missing_enable_support() -> None:
    """The feature gate cannot authorize a launch without the enable flag."""
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(
            returncode=0, stdout="usage: codex exec --add-dir DIR", stderr=""
        )

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    with pytest.raises(WorkerProtocolError, match="--enable"):
        invoker.preflight()

    assert calls == [("/codex", "exec", "--help")] * 2


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "multi_agent stable true\n"),
        (0, ""),
        (0, "multi_agent stable maybe\n"),
        (0, "other_feature stable true\n"),
    ],
)
def test_codex_preflight_rejects_unproven_child_delegation(
    returncode: int, stdout: str
) -> None:
    """Failed or malformed feature output cannot authorize a worker launch."""
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="usage: codex exec --add-dir DIR --enable FEATURE",
                stderr="",
            )
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    with pytest.raises(WorkerProtocolError, match="multi-agent"):
        invoker.preflight()

    assert calls == [("/codex", "exec", "--help"), ("/codex", "features", "list")]


def test_codex_preflight_retries_after_child_delegation_returns() -> None:
    """A failed capability check does not cache an unavailable feature."""
    calls: list[tuple[str, ...]] = []
    feature_outputs = iter(("multi_agent stable false\n", "multi_agent stable true\n"))

    def run_command(command: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[-1] == "--help":
            return SimpleNamespace(
                returncode=0,
                stdout="usage: codex exec --add-dir DIR --enable FEATURE",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout=next(feature_outputs), stderr="")

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    with pytest.raises(WorkerProtocolError, match="multi-agent"):
        invoker.preflight()
    invoker.preflight()

    assert (
        calls
        == [
            ("/codex", "exec", "--help"),
            ("/codex", "features", "list"),
        ]
        * 2
    )


def test_default_activation_budget_remains_three_hours() -> None:
    """The worker retains its independent 10,800-second activation budget."""
    assert DEFAULT_MAX_ACTIVATION_SECONDS == 10_800


def test_local_operational_log_keeps_info_and_debug_at_requested_verbosity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ordinary = _build_local_logger(config, debug=False)
    ordinary.info("ordinary lifecycle")
    ordinary.debug("hidden diagnostic")
    for handler in ordinary.handlers:
        handler.flush()
    log_path = config.state_root / "agent-work.log"
    assert log_path.read_text() == "ordinary lifecycle\n"

    debug = _build_local_logger(config, debug=True)
    debug.debug("visible diagnostic")
    for handler in debug.handlers:
        handler.flush()
    assert "visible diagnostic" in log_path.read_text()


def test_local_operational_log_records_command_outcomes_and_failures(
    tmp_path: Path,
) -> None:
    logger = _build_local_logger(_config(tmp_path), debug=False)

    assert (
        main(
            ["loop", "status"],
            dispatch=lambda *_args, **_kwargs: CommandReport(
                command="loop.status", applied=False, outcome="status"
            ),
        )
        == 0
    )

    def fail(*_args: object, **_kwargs: object) -> CommandReport:
        raise RuntimeError("ledger unavailable")

    assert main(["loop", "status"], dispatch=fail) == 1
    for handler in logger.handlers:
        handler.flush()
    contents = (_config(tmp_path).state_root / "agent-work.log").read_text()
    assert "outcome=status command=loop.status" in contents
    assert "failure=ledger unavailable" in contents


def test_loop_run_text_output_exposes_top_level_pipeline_handoff(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A blocked pre-selection gate remains actionable without requiring JSON parsing."""

    def blocked_report(*_args: object, **_kwargs: object) -> CommandReport:
        return CommandReport(
            command="loop.run",
            applied=False,
            outcome="pipeline_blocked",
            summary="Pipeline https://pipeline/4 failed for the current source SHA.",
            next_action="Inspect the pipeline and reconcile its exact retained owner.",
        )

    assert main(["loop", "run"], dispatch=blocked_report) == 0

    output = capsys.readouterr().out
    assert "https://pipeline/4" in output
    assert "Inspect the pipeline" in output


@pytest.mark.parametrize(
    ("branch", "candidate", "merge_head", "merge_head_is_current", "raises"),
    (
        ("agent/issue-12", "b" * 40, None, True, False),
        ("agent/issue-12", "a" * 40, None, True, False),
        ("agent/issue-12", "a" * 40, "b" * 40, True, False),
        ("agent/issue-12", "a" * 40, "c" * 40, False, True),
        ("other", "a" * 40, None, True, True),
    ),
)
def test_repository_reconciles_current_default_or_recovers_only_exact_pending_merge(
    tmp_path: Path,
    branch: str,
    candidate: str,
    merge_head: str | None,
    merge_head_is_current: bool,
    raises: bool,
) -> None:
    """Only the recorded candidate may start or recover a current-default merge."""
    config = _config(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, f"{branch}\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{candidate}\n", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{'b' * 40}\n", "")
        if arguments == ("merge-base", "--is-ancestor", "b" * 40, candidate):
            return CommandResult(0 if candidate == "b" * 40 else 1, "", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            merge_started = any(call[3:4] == ("merge",) for call in calls)
            pending = merge_head or (
                "b" * 40 if candidate == "a" * 40 and merge_started else None
            )
            return CommandResult(0 if pending else 1, f"{pending or ''}\n", "")
        if arguments == ("merge-base", "--is-ancestor", merge_head, "b" * 40):
            return CommandResult(0 if merge_head_is_current else 1, "", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )
    if raises:
        with pytest.raises(RepositoryError):
            repository.reconcile_default_branch(
                tmp_path / "worktree", "agent/issue-12", candidate
            )
        return

    result = repository.reconcile_default_branch(
        tmp_path / "worktree", "agent/issue-12", candidate
    )

    assert result == (None if candidate == "b" * 40 else "b" * 40)
    assert any(_git_arguments(call)[:2] == ("fetch", "origin") for call in calls)
    assert any(
        call[3:] == ("rev-parse", "-q", "--verify", "MERGE_HEAD") for call in calls
    )
    if candidate == "a" * 40 and merge_head is None:
        assert any(
            call[3:] == ("merge", "--no-ff", "--no-commit", "b" * 40) for call in calls
        )


def test_repository_reconciliation_rejects_dirty_worktree_without_pending_merge(
    tmp_path: Path,
) -> None:
    """Unrelated changes cannot be folded into a default-branch reconciliation."""
    config = _config(tmp_path)
    candidate, default = "a" * 40, "b" * 40

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{candidate}\n", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{default}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "M unrelated.py\n", "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError, match="uncommitted changes"):
        GitRepository(
            config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
        ).reconcile_default_branch(tmp_path / "worktree", "agent/issue-12", candidate)


def test_repository_publication_changed_paths_compares_the_prospective_tree_to_target(
    tmp_path: Path,
) -> None:
    """Policy evidence is based on the tree that would be published, not status."""
    config = _config(tmp_path)
    target = "b" * 40
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("rev-parse", "-q", "--verify", f"{target}^{{commit}}"):
            return CommandResult(0, f"{target}\n", "")
        if arguments == ("merge-base", "--is-ancestor", target, "HEAD"):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("ls-files", "--unmerged"):
            return CommandResult(0, "", "")
        if arguments == ("diff", "--name-only", "--no-renames", target):
            return CommandResult(0, "src/owned.py\ntemplate/AGENTS.md\n", "")
        if arguments == ("ls-files", "--others", "--exclude-standard"):
            return CommandResult(0, "new.txt\n", "")
        return CommandResult(0, "", "")

    paths = GitRepository(config, fake).publication_changed_paths(
        tmp_path / "worktree", target
    )

    assert paths == ("new.txt", "src/owned.py", "template/AGENTS.md")
    assert any(
        call[3:] == ("diff", "--name-only", "--no-renames", target) for call in calls
    )


@pytest.mark.parametrize(
    ("target_verifies", "target_is_ancestor", "merge_head", "unmerged", "message"),
    (
        (False, True, None, False, "target"),
        (True, False, None, False, "ancestor"),
        (True, True, "c" * 40, False, "pending merge"),
        (True, True, "b" * 40, True, "unresolved"),
    ),
)
def test_repository_publication_changed_paths_rejects_ambiguous_tree_evidence(
    tmp_path: Path,
    target_verifies: bool,
    target_is_ancestor: bool,
    merge_head: str | None,
    unmerged: bool,
    message: str,
) -> None:
    """A target mismatch, non-ancestor, or unresolved merge cannot publish policy evidence."""
    config = _config(tmp_path)
    target = "b" * 40

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("rev-parse", "-q", "--verify", f"{target}^{{commit}}"):
            return CommandResult(0 if target_verifies else 1, f"{target}\n", "")
        if arguments == ("merge-base", "--is-ancestor", target, "HEAD"):
            return CommandResult(0 if target_is_ancestor else 1, "", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(0 if merge_head else 1, f"{merge_head or ''}\n", "")
        if arguments == ("ls-files", "--unmerged"):
            return CommandResult(0, "conflict\n" if unmerged else "", "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError, match=message):
        GitRepository(config, fake).publication_changed_paths(
            tmp_path / "worktree", target
        )


@pytest.mark.parametrize(
    ("unmerged", "porcelain", "parents", "raises"),
    (
        (False, "M changed.py\n", ("a" * 40, "b" * 40), False),
        (False, "", ("a" * 40, "b" * 40), False),
        (True, "M changed.py\n", ("a" * 40, "b" * 40), True),
        (False, "M changed.py\n", ("b" * 40, "a" * 40), True),
    ),
)
def test_prepare_commit_requires_a_resolved_pending_merge_and_exact_ordered_parents(
    tmp_path: Path,
    unmerged: bool,
    porcelain: str,
    parents: tuple[str, str],
    raises: bool,
) -> None:
    """Trusted publication commits a resolved pending merge with candidate then default."""
    config = _config(tmp_path)
    candidate, default, prepared = "a" * 40, "b" * 40, "c" * 40
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(
                0, f"{candidate if len(calls) < 6 else prepared}\n", ""
            )
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, porcelain, "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(0, f"{default}\n", "")
        if arguments == ("ls-files", "--unmerged"):
            return CommandResult(
                0, "100644 deadbeef 1\tchanged.py\n" if unmerged else "", ""
            )
        if arguments == ("rev-list", "--parents", "-n", "1", prepared):
            return CommandResult(0, f"{prepared} {' '.join(parents)}\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )
    if raises:
        with pytest.raises(RepositoryError):
            repository.prepare_commit(
                tmp_path / "worktree", "agent/issue-12", 12, expected_head=candidate
            )
    else:
        assert (
            repository.prepare_commit(
                tmp_path / "worktree", "agent/issue-12", 12, expected_head=candidate
            )
            == prepared
        )
        assert any(
            call[3:] == ("rev-list", "--parents", "-n", "1", prepared) for call in calls
        )


def test_prepare_commit_stages_only_explicit_validated_paths_including_deletions(
    tmp_path: Path,
) -> None:
    """Publication never broad-stages unrelated dirty files."""
    config = _config(tmp_path)
    candidate, prepared = "a" * 40, "b" * 40
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(
                0, f"{candidate if len(calls) < 5 else prepared}\n", ""
            )
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(
                0, "M docs/space name.md\n D removed.py\n?? unrelated.py\n", ""
            )
        if arguments[:2] == ("commit", "-m"):
            return CommandResult(0, "", "")
        if arguments == ("rev-list", "--parents", "-n", "1", prepared):
            return CommandResult(0, f"{prepared} {candidate}\n", "")
        return CommandResult(0, "", "")

    prepared_sha = GitRepository(config, fake).prepare_commit(
        tmp_path / "worktree",
        "agent/issue-12",
        12,
        expected_head=candidate,
        validated_paths=("docs/space name.md", "removed.py"),
    )

    assert prepared_sha == prepared
    assert any(
        call[3:] == ("add", "--all", "--", "docs/space name.md", "removed.py")
        for call in calls
    )
    assert not any(call[3:] == ("add", "--all") for call in calls)


def test_prepare_commit_does_not_bypass_the_installed_git_hook(tmp_path: Path) -> None:
    """An ordinary publication commit has no no-verify hook bypass."""
    candidate, prepared = "a" * 40, "b" * 40
    head = candidate
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal head
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{head}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "M changed.py\n", "")
        if arguments == ("commit", "-m", "Resolve issue #12"):
            head = prepared
            return CommandResult(0, "", "")
        if arguments == ("rev-list", "--parents", "-n", "1", prepared):
            return CommandResult(0, f"{prepared} {candidate}\n", "")
        return CommandResult(0, "", "")

    prepared_sha = GitRepository(_config(tmp_path), fake).prepare_commit(
        tmp_path / "worktree", "agent/issue-12", 12, expected_head=candidate
    )

    assert prepared_sha == prepared
    commit_calls = [call[3:] for call in calls if call[3] == "commit"]
    assert commit_calls == [("commit", "-m", "Resolve issue #12")]
    assert all("--no-verify" not in call for call in commit_calls)


@pytest.mark.parametrize("merge_head", [None, "c" * 40])
def test_prepare_commit_classifies_an_exact_retained_hook_rejection_for_correction(
    tmp_path: Path, merge_head: str | None
) -> None:
    """A rejected commit is correctable only while every local identity stays exact."""
    candidate = "a" * 40
    candidate_tree = "d" * 40
    raw_diagnostic = (
        "readability-check.................................Failed\n"
        "- hook id: readability-check\n"
        "- exit code: 1\n"
        "docs/example.md: grade 10.6\n"
        "token=super-secret\x00"
    )
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{candidate}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(0 if merge_head else 1, f"{merge_head or ''}\n", "")
        if arguments == ("ls-files", "--unmerged"):
            return CommandResult(0, "", "")
        if arguments in {
            ("status", "--porcelain"),
            ("status", "--porcelain=v1", "-z"),
        }:
            return CommandResult(0, "M  changed.py\n", "")
        if arguments == ("write-tree",):
            return CommandResult(0, f"{candidate_tree}\n", "")
        if arguments[:2] == ("commit", "-m"):
            return CommandResult(1, "", raw_diagnostic)
        return CommandResult(0, "", "")

    rejected_type = repository_module.PublicationCommitRejected
    with pytest.raises(rejected_type) as failure:
        GitRepository(_config(tmp_path), fake).prepare_commit(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head=candidate,
        )

    evidence = failure.value.failure
    assert evidence.diagnostic.startswith("Installed pre-commit rejected the commit")
    assert "readability-check" in evidence.diagnostic
    assert "docs/example.md: grade 10.6" in evidence.diagnostic
    assert "super-secret" not in evidence.diagnostic
    assert "\n" not in evidence.diagnostic and "\x00" not in evidence.diagnostic
    assert len(evidence.diagnostic.encode()) <= 2_048
    assert [call[3:] for call in calls if call[3] == "commit"] == [
        ("commit", "-m", "Resolve issue #12")
    ]


@pytest.mark.parametrize("drift", ["branch", "head", "merge", "tree"])
def test_prepare_commit_keeps_post_rejection_identity_drift_fail_closed(
    tmp_path: Path, drift: str
) -> None:
    """A failed commit with changed local evidence remains an identity mismatch."""
    candidate = "a" * 40
    tree = "d" * 40
    observations = {"branch": 0, "head": 0, "merge": 0, "tree": 0}

    def observed(key: str, exact: str, changed: str) -> str:
        observations[key] += 1
        return changed if drift == key and observations[key] > 1 else exact

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(
                0, observed("branch", "agent/issue-12\n", "other\n"), ""
            )
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(
                0, observed("head", f"{candidate}\n", f"{'b' * 40}\n"), ""
            )
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            value = observed("merge", "", f"{'c' * 40}\n")
            return CommandResult(0 if value else 1, value, "")
        if arguments == ("ls-files", "--unmerged"):
            return CommandResult(0, "", "")
        if arguments in {
            ("status", "--porcelain"),
            ("status", "--porcelain=v1", "-z"),
        }:
            return CommandResult(0, "M  changed.py\n", "")
        if arguments == ("write-tree",):
            return CommandResult(0, observed("tree", f"{tree}\n", f"{'e' * 40}\n"), "")
        if arguments[:2] == ("commit", "-m"):
            return CommandResult(
                1,
                "readability-check........Failed\n- hook id: readability-check\n",
                "",
            )
        return CommandResult(0, "", "")

    rejected_type = repository_module.PublicationCommitRejected
    with pytest.raises(RepositoryError) as failure:
        GitRepository(_config(tmp_path), fake).prepare_commit(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head=candidate,
        )

    assert not isinstance(failure.value, rejected_type)


def test_prepare_commit_allows_unstaged_hook_repairs_when_staged_tree_stays_exact(
    tmp_path: Path,
) -> None:
    """A hook may repair working files without changing the staged candidate tree."""
    candidate = "a" * 40
    statuses = iter(("M  changed.py\n", "MM changed.py\n"))

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{candidate}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, next(statuses), "")
        if arguments == ("write-tree",):
            return CommandResult(0, f"{'d' * 40}\n", "")
        if arguments[:2] == ("commit", "-m"):
            return CommandResult(
                1,
                "ruff........................................Failed\n"
                "- hook id: ruff\n"
                "src/example.py:1:1: E501 too long\n",
                "",
            )
        return CommandResult(0, "", "")

    with pytest.raises(repository_module.PublicationCommitRejected):
        GitRepository(_config(tmp_path), fake).prepare_commit(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head=candidate,
        )


@pytest.mark.parametrize(
    "diagnostic",
    (
        "error: gpg failed to sign the data",
        "Author identity unknown",
        "fatal: bad config line 1",
        "fatal: Unable to create '.git/index.lock': File exists.",
        "fatal: cannot open .git/COMMIT_EDITMSG: Permission denied",
        "fatal: loose object is corrupt",
        "unknown commit failure",
    ),
)
def test_prepare_commit_keeps_unrecognized_commit_failures_fail_closed(
    tmp_path: Path, diagnostic: str
) -> None:
    """Non-hook commit failures never enter the correction route."""
    candidate = "a" * 40

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{candidate}\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "M  changed.py\n", "")
        if arguments == ("write-tree",):
            return CommandResult(0, f"{'d' * 40}\n", "")
        if arguments[:2] == ("commit", "-m"):
            return CommandResult(1, "", diagnostic)
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError) as failure:
        GitRepository(_config(tmp_path), fake).prepare_commit(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head=candidate,
        )

    assert not isinstance(failure.value, repository_module.PublicationCommitRejected)


@pytest.mark.parametrize(
    ("parents", "second_parent_is_current", "raises"),
    (
        (("a" * 40, "b" * 40), True, False),
        (("c" * 40, "b" * 40), True, True),
        (("a" * 40, "c" * 40), False, True),
    ),
)
def test_bind_retained_commit_rebinds_only_an_exact_current_default_merge(
    tmp_path: Path,
    parents: tuple[str, str],
    second_parent_is_current: bool,
    raises: bool,
) -> None:
    """A crash replay may bind only the exact clean runner merge already in progress."""
    config = _config(tmp_path)
    baseline, prepared, current_default = "a" * 40, "d" * 40, "b" * 40
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        arguments = command[3:]
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, "", "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-12\n", "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{prepared}\n", "")
        if arguments == ("rev-list", "--parents", "-n", "1", prepared):
            return CommandResult(0, f"{prepared} {' '.join(parents)}\n", "")
        if arguments == (
            "rev-list",
            "--first-parent",
            "--count",
            f"{baseline}..{prepared}",
        ):
            return CommandResult(0, "1\n", "")
        if arguments == ("log", "-1", "--format=%s", prepared):
            return CommandResult(0, "Resolve issue #12\n", "")
        if arguments == ("rev-parse", "origin/main"):
            return CommandResult(0, f"{current_default}\n", "")
        if arguments == ("merge-base", "--is-ancestor", parents[1], current_default):
            return CommandResult(0 if second_parent_is_current else 1, "", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        config, fake, service_environment={"GITLAB_TOKEN": "test-token"}
    )
    if raises:
        with pytest.raises(RepositoryError):
            repository.bind_retained_commit(
                tmp_path / "worktree", "agent/issue-12", 12, baseline
            )
    else:
        assert (
            repository.bind_retained_commit(
                tmp_path / "worktree", "agent/issue-12", 12, baseline
            )
            == prepared
        )
        assert any(_git_arguments(call)[:2] == ("fetch", "origin") for call in calls)


@pytest.mark.parametrize(
    ("outcome", "reason", "omitted_findings"),
    (
        ("correction_required", "corroborated_regression", 0),
        ("report_only", "touched_slow", 2),
        ("report_only", "inherited_variance", 0),
    ),
)
def test_serial_judge_passes_exact_baseline_and_rejects_missing_timing_proof(
    tmp_path: Path,
    outcome: str,
    reason: str,
    omitted_findings: int,
) -> None:
    baseline_sha = "a" * 40
    candidate_tree = "b" * 40
    observed_commands: list[tuple[str, ...]] = []
    report: dict[str, object] = {
        "candidate_tree": candidate_tree,
        "state": "terminal-success",
        "exit_code": 0,
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "junit_timings": [],
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        observed_commands.append(command)
        return CommandResult(
            0,
            candidate_tree if command[-1] == "write-tree" else json.dumps(report),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return baseline_sha

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("tests/test_speed.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return candidate_tree

    with pytest.raises(RepositoryError, match="timing proof"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            candidate_tree,
            base_sha=baseline_sha,
            changed_paths=("tests/test_speed.py",),
            action="validate",
            timeout_seconds=30,
            timing_review_required=True,
        )

    validation_command = next(
        command for command in observed_commands if "validation" in command
    )
    assert "--serial-timing" in validation_command
    assert validation_command[validation_command.index("--baseline-sha") + 1] == (
        baseline_sha
    )

    class_case = "tests/test_speed.py::TestWidget::test_existing"
    hashed_class_case = f"{class_case}[param_{'a' * 64}]"
    report["timing_review"] = {
        "baseline_sha": baseline_sha,
        "baseline_tree": "c" * 40,
        "candidate_tree": candidate_tree,
        "outcome": outcome,
        "omitted_findings": omitted_findings,
        "transcript_sha256": "d" * 64,
        "runs": {
            environment: {
                source: {
                    "sha256": "e" * 64,
                    "test_count": 2,
                    "timing_complete": True,
                }
                for source in ("baseline", "candidate")
            }
            for environment in ("py311", "py314")
        },
        "findings": [
            {
                "environment": "py311",
                "name": "tests/test_speed.py::test_existing",
                "reason": reason,
                "phase_deltas": (
                    {"setup": 0.0, "call": 0.8, "teardown": 0.0}
                    if reason in {"corroborated_regression", "inherited_variance"}
                    else None
                ),
            },
            {
                "environment": "py314",
                "name": hashed_class_case,
                "reason": "inherited_slow",
                "phase_deltas": None,
            },
        ],
    }
    report["junit_artifacts"] = [
        {
            "environment": environment,
            "path": str(tmp_path / f"private-{environment}.xml"),
            "sha256": "e" * 64,
            "test_count": 2,
        }
        for environment in ("py311", "py314")
    ]
    report["junit_timings"] = [
        {
            "environment": "py314",
            "total_seconds": 1.1,
            "slowest_cases": [
                {"name": class_case, "seconds": 0.7},
                {"name": hashed_class_case, "seconds": 0.4},
            ],
            "timing_complete": True,
            "over_100ms_cases": [
                {"name": class_case, "seconds": 0.7},
                {"name": hashed_class_case, "seconds": 0.4},
            ],
        }
    ]
    observed = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        candidate_tree,
        base_sha=baseline_sha,
        changed_paths=("tests/test_speed.py",),
        action="validate",
        timeout_seconds=30,
        timing_review_required=True,
    )

    assert observed["timing_review"] == report["timing_review"]
    assert (
        cast(list[dict[str, object]], observed["junit_timings"])[0]["over_100ms_cases"]
        == cast(list[dict[str, object]], report["junit_timings"])[0]["over_100ms_cases"]
    )

    report["junit_timings"] = [
        {
            "environment": "py314",
            "total_seconds": 0.4,
            "slowest_cases": [
                {"name": "tests.test_speed::test_existing", "seconds": 0.4}
            ],
            "timing_complete": True,
            "over_100ms_cases": [
                {"name": "tests.test_speed::test_existing", "seconds": 0.4}
            ],
        }
    ]
    interactive = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        candidate_tree,
        base_sha=baseline_sha,
        changed_paths=("tests/test_speed.py",),
        action="validate",
        timeout_seconds=30,
    )
    assert (
        cast(list[dict[str, object]], interactive["junit_timings"])[0][
            "over_100ms_cases"
        ]
        == cast(list[dict[str, object]], report["junit_timings"])[0]["over_100ms_cases"]
    )

    timing = cast(dict[str, object], report["timing_review"])
    runs = cast(dict[str, object], timing["runs"])
    del runs["py314"]
    with pytest.raises(RepositoryError, match="timing proof"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            candidate_tree,
            base_sha=baseline_sha,
            changed_paths=("tests/test_speed.py",),
            action="validate",
            timeout_seconds=30,
            timing_review_required=True,
        )
