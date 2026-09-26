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

from .domain import JobTraceEvidence, PipelineJobEvidence
from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabIssueNote,
    GitLabProject,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    PipelineEvidence,
)

if TYPE_CHECKING:
    from .repository import LegacyPublicationCandidate, WorktreeStatus
    from .worker import WorkerInvocation, WorkerResultBrokerRequest


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
        current_directory: Path | None = None,
    ) -> CommandResult: ...


class GitLabPort(Protocol):
    """GitLab reads and idempotent mutations."""

    def verify_project(self) -> GitLabProject: ...

    def verify_authenticated_user(self) -> str | None: ...

    def fetch_merge_request_feedback(
        self, iid: int
    ) -> tuple[MergeRequestFeedback, ...]: ...

    def comment_feedback_outcome(
        self,
        iid: int,
        *,
        issue_iid: int,
        source: str,
        note_id: int,
        discussion_id: str | None,
        selection_sha: str,
        summary: str,
    ) -> None: ...

    def list_eligible(self) -> tuple[GitLabIssue, ...]: ...

    def list_projected(self) -> tuple[GitLabIssue, ...]: ...

    def list_closed_projected(self) -> tuple[GitLabIssue, ...]: ...

    def prerequisite_projection_inventory(self) -> tuple[GitLabIssue, ...]: ...

    def ensure_prerequisite_labels(self, labels: tuple[str, ...]) -> bool: ...

    def retire_legacy_prerequisite_labels(self) -> bool: ...

    def transition_prerequisite_labels(
        self, iid: int, labels: tuple[str, ...]
    ) -> GitLabIssue: ...

    def fetch_issue(self, iid: int) -> GitLabIssue: ...

    def fetch_issue_notes(self, iid: int) -> tuple[GitLabIssueNote, ...]: ...

    def ensure_human_handoff(
        self, *, issue_iid: int, merge_request: MergeRequestEvidence
    ) -> bool: ...

    def transition_labels(self, iid: int, label: str | None) -> GitLabIssue: ...

    def comment_outcome(
        self, iid: int, *, error_code: str, summary: str, next_action: str
    ) -> None: ...

    def upsert_outcome_note(
        self,
        iid: int,
        *,
        outcome_key: str,
        outcome: str,
        explanation: str,
        evidence: str,
        retained_work: str,
        next_action: str,
        human_decision: str,
    ) -> None: ...

    def fetch_merge_request(self, iid: int) -> MergeRequestEvidence: ...

    def merge_request_contains_commit(self, mr_iid: int, sha: str) -> bool: ...

    def find_merge_request_by_branch(
        self, branch: str
    ) -> MergeRequestEvidence | None: ...

    def open_merge_requests_by_branch(
        self, branch: str
    ) -> tuple[MergeRequestEvidence, ...]: ...

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

    def closing_issue_iids(self, mr_iid: int) -> frozenset[int]: ...

    def latest_pipeline_for_sha(
        self, mr_iid: int, sha: str
    ) -> PipelineEvidence | None: ...

    def latest_pipeline_for_ref(self, ref: str) -> PipelineEvidence | None: ...

    def list_pipeline_jobs(
        self, pipeline_id: int, source_sha: str
    ) -> tuple[PipelineJobEvidence, ...]: ...

    def fetch_job_trace(self, pipeline_id: int, job_id: int) -> JobTraceEvidence: ...

    def open_merge_requests(self) -> tuple[MergeRequestEvidence, ...]: ...

    def split_child_inventory(self) -> tuple[GitLabIssue, ...]: ...

    def create_split_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue: ...

    def ensure_decomposition_note(self, iid: int, body: str) -> None: ...

    def close_issue(self, iid: int) -> GitLabIssue: ...

    def reopen_issue(self, iid: int) -> GitLabIssue: ...


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

    def refresh_default_branch(self) -> str: ...

    def default_branch_head(self) -> str: ...

    def fresh_issue_branch_available(self, issue_iid: int) -> bool: ...

    def create_issue_worktree(
        self,
        issue_iid: int,
        branch: str,
        default_branch: str,
        *,
        allow_existing_branch: bool = True,
    ) -> Path: ...

    def refresh_retained_worktree(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str: ...

    def worktree_for_existing_branch(
        self, issue_iid: int, branch: str, expected_sha: str
    ) -> Path: ...

    def head(self, worktree: Path) -> str: ...

    def changed_paths(self, worktree: Path) -> tuple[str, ...]: ...

    def observe_worktree_status(self, worktree: Path) -> WorktreeStatus: ...

    def red_candidate_tree(
        self, worktree: Path, base_sha: str, paths: tuple[str, ...]
    ) -> str: ...

    def stage_candidate(
        self, worktree: Path, base_sha: str, *, allow_base_ancestor: bool = False
    ) -> str: ...

    def read_candidate(
        self,
        worktree: Path,
        candidate_tree: str,
        *,
        base_sha: str,
        allow_base_ancestor: bool = False,
        changed_paths: tuple[str, ...],
    ) -> dict[str, object]: ...

    def judge_candidate(
        self,
        worktree: Path,
        candidate_tree: str,
        *,
        base_sha: str,
        allow_base_ancestor: bool = False,
        changed_paths: tuple[str, ...],
        action: str,
        timeout_seconds: int,
    ) -> dict[str, object]: ...

    def observe_legacy_publication_candidate(
        self, worktree: Path, *, status: WorktreeStatus | None = None
    ) -> LegacyPublicationCandidate: ...

    def single_parent(self, worktree: Path, candidate_sha: str) -> str | None: ...

    def publication_changed_paths(
        self, worktree: Path, target_sha: str
    ) -> tuple[str, ...]: ...

    def has_changes(self, worktree: Path) -> bool: ...

    def worktree_exists(self, worktree: Path) -> bool: ...

    def verify_worktree(
        self, worktree: Path, branch: str, expected_head: str
    ) -> None: ...

    def is_ancestor(self, ancestor: str, descendant: str, worktree: Path) -> bool: ...

    def sync_feedback_worktree(
        self, worktree: Path, branch: str, old_sha: str, new_sha: str
    ) -> None: ...

    def observe_current_default(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str: ...

    def reconcile_default_branch(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str | None: ...

    def prepare_commit(
        self,
        worktree: Path,
        branch: str,
        issue_iid: int,
        *,
        expected_head: str,
        validated_paths: tuple[str, ...] | None = None,
    ) -> str: ...

    def push_prepared(
        self, worktree: Path, branch: str, publication_sha: str
    ) -> str: ...

    def bind_retained_commit(
        self, worktree: Path, branch: str, issue_iid: int, baseline_sha: str
    ) -> str: ...

    def inventory(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]: ...


class InvokerPort(Protocol):
    """One bounded worker process."""

    supports_worker_result_broker: bool

    def preflight(self) -> None: ...

    def invoke(
        self,
        checkout: Path,
        prompt: str,
        timeout: int,
        *,
        broker_request: WorkerResultBrokerRequest,
        build_artifact: Callable[[bytes], bytes],
        candidate_action: Callable[[str, str | None], dict[str, object]] | None = None,
    ) -> WorkerInvocation: ...


ProjectLockFactory = Callable[[], AbstractContextManager[None]]
