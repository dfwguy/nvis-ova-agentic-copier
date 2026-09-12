# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Dependency-inversion protocols for recoverable agent work."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabIssueNote,
    GitLabProject,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
)

if TYPE_CHECKING:
    from .worker import WorkerInvocation


class CommandResult(Protocol):
    """Structural subprocess result."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Injected subprocess boundary."""

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


class GitLabPort(Protocol):
    """GitLab reads and idempotent mutations."""

    def verify_project(self) -> GitLabProject: ...

    def list_eligible(self) -> tuple[GitLabIssue, ...]: ...

    def list_projected(self) -> tuple[GitLabIssue, ...]: ...

    def fetch_issue(self, iid: int) -> GitLabIssue: ...

    def fetch_issue_notes(self, iid: int) -> tuple[GitLabIssueNote, ...]: ...

    def transition_labels(self, iid: int, label: str | None) -> GitLabIssue: ...

    def comment_outcome(
        self, iid: int, *, error_code: str, summary: str, next_action: str
    ) -> None: ...

    def fetch_merge_request(self, iid: int) -> MergeRequestEvidence: ...

    def find_merge_request_by_branch(
        self, branch: str
    ) -> MergeRequestEvidence | None: ...

    def create_merge_request(
        self,
        *,
        branch: str,
        target: str,
        issue_iid: int,
        draft: MergeRequestDraft,
    ) -> MergeRequestEvidence: ...

    def ensure_closing_issue_reference(
        self, mr: MergeRequestEvidence, issue_iid: int
    ) -> MergeRequestEvidence: ...

    def reconcile_merge_request(
        self, mr: MergeRequestEvidence, *, issue_iid: int, draft: MergeRequestDraft
    ) -> MergeRequestEvidence: ...

    def mr_closes_issue(self, mr_iid: int, issue_iid: int) -> bool: ...

    def latest_pipeline_for_sha(
        self, mr_iid: int, sha: str
    ) -> PipelineEvidence | None: ...

    def split_child_inventory(self) -> tuple[GitLabIssue, ...]: ...

    def create_split_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue: ...

    def ensure_decomposition_note(self, iid: int, body: str) -> None: ...

    def close_issue(self, iid: int) -> GitLabIssue: ...


class LedgerPort(Protocol):
    """Durable record storage."""

    state_root: Path

    def load(self, issue_iid: int) -> LedgerRecord | None: ...

    def list_records(self) -> tuple[LedgerRecord, ...]: ...

    def persist(self, record: LedgerRecord) -> LedgerRecord: ...


class RepositoryPort(Protocol):
    """Durable worktree and Git boundary."""

    config: AgentWorkConfig

    def refresh_base(self) -> None: ...

    def create_issue_worktree(
        self,
        issue_iid: int,
        branch: str,
        default_branch: str,
        *,
        allow_existing_branch: bool = True,
    ) -> Path: ...

    def worktree_for_existing_branch(
        self, issue_iid: int, branch: str, expected_sha: str
    ) -> Path: ...

    def head(self, worktree: Path) -> str: ...

    def changed_paths(self, worktree: Path) -> tuple[str, ...]: ...

    def worktree_exists(self, worktree: Path) -> bool: ...

    def verify_worktree(
        self, worktree: Path, branch: str, expected_head: str
    ) -> None: ...

    def commit_and_push(
        self,
        worktree: Path,
        branch: str,
        issue_iid: int,
        *,
        expected_head: str,
        allow_replay: bool = False,
    ) -> str: ...

    def inventory(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]: ...


class InvokerPort(Protocol):
    """One bounded worker process."""

    def preflight(self) -> None: ...

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation: ...


ProjectLockFactory = Callable[[], AbstractContextManager[None]]
