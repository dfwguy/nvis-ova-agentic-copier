"""Validated boundary models for recoverable agent work."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domain import ErrorCode, RunnerState, WorkflowLane, WorkflowPhase, WorkKind


class BoundaryModel(BaseModel):
    """Strict immutable base for data crossing a system boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentWorkConfig(BoundaryModel):
    """Non-secret configuration discovered from a confirmed checkout."""

    host: str = Field(min_length=1)
    project_id: int = Field(gt=0)
    checkout: Path
    state_root: Path
    default_branch: str = Field(default="main", min_length=1)
    base_branch: str | None = Field(
        default=None, pattern=r"^agent/[A-Za-z0-9][A-Za-z0-9._/-]*$"
    )
    journal_tag: str = Field(default="agent-work", min_length=1)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Require one bare hostname."""
        if "://" in value or "/" in value:
            raise ValueError("host must be a bare hostname")
        return value

    @field_validator("checkout", "state_root")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        """Require explicit absolute paths."""
        if not value.is_absolute():
            raise ValueError("runner paths must be absolute")
        return value


class GitLabIssue(BoundaryModel):
    """Validated GitLab issue fields used by selection."""

    iid: int = Field(gt=0)
    title: str = Field(min_length=1)
    description: str = ""
    labels: tuple[str, ...] = ()
    state: str = "opened"
    confidential: bool = False
    created_at: str = ""

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Accept GitLab's nullable description."""
        return "" if value is None else value


class GitLabProject(BoundaryModel):
    """Configured-project identity returned by GitLab."""

    id: int = Field(gt=0)
    host: str
    default_branch: str = Field(min_length=1)
    web_url: str = Field(min_length=1)
    http_url_to_repo: str = ""
    ssh_url_to_repo: str = ""


class MergeRequestEvidence(BoundaryModel):
    """Exact MR evidence needed for recovery and publication."""

    iid: int = Field(gt=0)
    web_url: str = Field(min_length=1)
    state: Literal["opened", "closed", "merged", "locked"]
    source_project_id: int = Field(gt=0)
    target_project_id: int = Field(gt=0)
    source_branch: str = Field(min_length=1)
    target_branch: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    has_conflicts: bool = False
    description: str = ""


class PipelineEvidence(BoundaryModel):
    """Pipeline state associated with one exact source SHA."""

    id: int = Field(gt=0)
    status: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    web_url: str = Field(min_length=1)


class RemediationMarker(BoundaryModel):
    """Exact existing-MR remediation metadata from an issue body."""

    project_id: int = Field(gt=0)
    merge_request_iid: int = Field(gt=0)
    source_branch: str = Field(min_length=1)
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    finding: str = Field(min_length=1)


class MergeRequestDraft(BoundaryModel):
    """Human-review metadata proposed by a worker."""

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)


class TransitionIntentRecord(BoundaryModel):
    """Replayable external mutation intent."""

    operation_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    expected_evidence: dict[str, str | int | bool | None] = Field(default_factory=dict)


class WorkflowReceiptRecord(BoundaryModel):
    """One durable phase receipt."""

    phase: WorkflowPhase
    verdict: str = Field(min_length=1)
    detail: str = Field(min_length=1, max_length=20_000)


class LedgerRecord(BoundaryModel):
    """Durable state for one issue."""

    schema_version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    state: RunnerState
    phase: WorkflowPhase
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind
    source_branch: str = Field(min_length=1)
    worktree: Path
    retry_count: int = Field(default=0, ge=0)
    retry_at: datetime | None = None
    prior_state: RunnerState | None = None
    prior_phase: WorkflowPhase | None = None
    merge_request_iid: int | None = Field(default=None, gt=0)
    merge_request_url: str | None = None
    source_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_request_draft: MergeRequestDraft | None = None
    receipts: tuple[WorkflowReceiptRecord, ...] = ()
    intent: TransitionIntentRecord | None = None
    error_code: ErrorCode | None = None
    summary: str = ""
    next_action: str = ""


class WorkerResultEnvelope(BoundaryModel):
    """Versioned, identity-bound worker segment result."""

    protocol_version: Literal[1] = 1
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    ledger_revision: int = Field(ge=0)
    expected_state: RunnerState
    expected_phase: WorkflowPhase
    workflow_lane: WorkflowLane
    result_kind: Literal["ready_to_publish", "retryable", "needs_human"]
    phase_status: Literal["complete", "rejected"]
    completed_phase: WorkflowPhase
    rejection_route: WorkflowPhase | None = None
    receipts: tuple[str, ...] = ()
    change_summary: str = Field(min_length=1, max_length=20_000)
    merge_request_draft: MergeRequestDraft | None = None
    error_code: ErrorCode | None = None
    redacted_summary: str = ""
    next_action: str = Field(min_length=1, max_length=2_000)


class MigrationObservation(BoundaryModel):
    """Read-only legacy evidence used to build a migration plan."""

    project_id: int = Field(gt=0)
    issues: tuple[dict[str, object], ...] = ()
    branches: tuple[str, ...] = ()
    worktrees: tuple[str, ...] = ()
    stashes: tuple[str, ...] = ()


class MigrationAction(BoundaryModel):
    """One reviewed, preconditioned migration action."""

    issue_iid: int = Field(gt=0)
    target_state: RunnerState
    next_action: str = Field(min_length=1)
    evidence: tuple[str, ...] = ()


class MigrationPlan(BoundaryModel):
    """Versioned dry-run migration plan."""

    schema_version: Literal[1] = 1
    project_id: int = Field(gt=0)
    actions: tuple[MigrationAction, ...] = ()


class CommandItemReport(BoundaryModel):
    """Safe operational status for one issue."""

    issue_iid: int = Field(gt=0)
    state: RunnerState
    phase: WorkflowPhase
    work_kind: WorkKind
    attempt: int = Field(ge=1)
    retries_remaining: int = Field(ge=0)
    retry_at: datetime | None = None
    source_branch: str
    worktree: Path
    merge_request_iid: int | None = None
    merge_request_url: str | None = None
    error_code: ErrorCode | None = None
    summary: str = ""
    next_action: str = ""


class CommandReport(BoundaryModel):
    """Common safe JSON envelope for every CLI action."""

    schema_version: Literal[1] = 1
    command: str = Field(min_length=1)
    applied: bool
    outcome: str = Field(min_length=1)
    project: dict[str, str | int] | None = None
    items: tuple[CommandItemReport, ...] = ()
    actions: tuple[MigrationAction, ...] = ()
