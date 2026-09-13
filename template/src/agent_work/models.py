# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Validated boundary models for recoverable agent work."""

from __future__ import annotations

import html
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .domain import (
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    split_fingerprint,
)


class BoundaryModel(BaseModel):
    """Strict immutable base for data crossing a system boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentWorkConfig(BoundaryModel):
    """Non-secret configuration discovered from a confirmed checkout."""

    host: str = Field(min_length=1)
    project_id: int = Field(gt=0)
    checkout: Path
    state_root: Path
    git_path: Path
    glab_path: Path
    default_branch: str = Field(default="main", min_length=1)
    base_branch: str = Field(pattern=r"^agent/[A-Za-z0-9][A-Za-z0-9._/-]*$")
    journal_tag: str = Field(default="agent-work", min_length=1)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Require one bare hostname."""
        if "://" in value or "/" in value:
            raise ValueError("host must be a bare hostname")
        return value

    @field_validator("checkout", "state_root", "git_path", "glab_path")
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


class GitLabNoteAuthor(BoundaryModel):
    """Validated GitLab issue-note author metadata."""

    username: str = Field(min_length=1, max_length=255)


class GitLabIssueNote(BoundaryModel):
    """Validated GitLab issue note used as untrusted worker context."""

    id: int = Field(gt=0)
    body: str = ""
    author: GitLabNoteAuthor | None = None
    created_at: datetime
    system: bool = False

    @field_validator("body", mode="before")
    @classmethod
    def normalize_body(cls, value: object) -> object:
        """Accept GitLab's nullable note body."""
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
    title: str = ""
    is_draft: bool = Field(
        default=False, validation_alias=AliasChoices("draft", "is_draft")
    )


class DecisionReviewSignal(BoundaryModel):
    """Complete, bounded evidence for a human decision-review draft."""

    decisions: tuple[str, ...] = Field(min_length=1, max_length=100)
    assumptions: tuple[str, ...] = Field(min_length=1, max_length=100)
    alternatives_considered: tuple[str, ...] = Field(min_length=1, max_length=100)
    risks: tuple[str, ...] = Field(min_length=1, max_length=100)
    reviewer_checks: tuple[str, ...] = Field(min_length=1, max_length=100)
    unresolved_findings: tuple[str, ...] = Field(min_length=1, max_length=100)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=100)
    tests: tuple[str, ...] = Field(min_length=1, max_length=100)
    correction_cycle_ledger: tuple[str, ...] = Field(min_length=1, max_length=100)
    unchanged_project_owned_values: tuple[str, ...] = Field(
        min_length=1, max_length=100
    )

    @field_validator("*")
    @classmethod
    def validate_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject overlong values while preserving text for canonical escaping."""
        if any(not item.strip() or len(item) > 20_000 for item in value):
            raise ValueError("decision-review entries must be nonempty and bounded")
        return value


def _sanitize_notice_text(text: str) -> str:
    """Make control characters safe, visible text before HTML escaping."""
    return "".join(
        " "
        if character in "\n\r"
        else f"\\x{ord(character):02x}"
        if ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F
        else character
        for character in text
    )


def _notice_items(items: tuple[str, ...]) -> str:
    """Render untrusted review text as literal list content, not headings."""
    return "\n".join(
        "- <code>" + html.escape(_sanitize_notice_text(item)) + "</code>"
        for item in items
    )


def render_decision_review_notice(review: DecisionReviewSignal, summary: str) -> str:
    """Render the stable v1 decision-review notice without heading injection."""
    sections = (
        ("## Decisions and assumptions", review.decisions + review.assumptions),
        ("## Alternatives considered", review.alternatives_considered),
        ("## Risks", review.risks),
        ("## Exact reviewer checks", review.reviewer_checks),
        ("## Unresolved findings", review.unresolved_findings),
        ("## Evidence", review.evidence),
        ("## Tests", review.tests),
        ("## Correction cycle ledger", review.correction_cycle_ledger),
        ("## Unchanged project-owned values", review.unchanged_project_owned_values),
    )
    body = "\n\n".join(
        f"{heading}\n{_notice_items(items)}" for heading, items in sections
    )
    safe_summary = _notice_items((summary,)).removeprefix("- ")
    return (
        "# ⚠️ Decision review required\n<!-- agent-work-decision-review:v1 -->\n\n"
        + body
        + f"\n\n## Summary\n{safe_summary}"
    )


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
    is_draft: bool = False
    decision_review: DecisionReviewSignal | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_draft_flag(cls, value: object) -> object:
        """Infer omitted protocol-v1 Draft state from its legacy title."""
        if isinstance(value, dict) and "is_draft" not in value:
            payload = cast(dict[str, object], value)
            return payload | {
                "is_draft": str(payload.get("title")).startswith("Draft:")
            }
        return value

    @model_validator(mode="after")
    def validate_decision_review(self) -> MergeRequestDraft:
        """Require decision review to remain an explicitly labelled Draft."""
        if self.is_draft != self.title.startswith("Draft:"):
            raise ValueError("draft flag must match the Draft: title")
        if self.decision_review is not None and (
            not self.is_draft or not self.title.startswith("Draft:")
        ):
            raise ValueError("decision review requires a Draft: title and draft flag")
        rendered = (
            render_decision_review_notice(self.decision_review, self.description)
            if self.decision_review is not None
            else self.description
        )
        if len(rendered) + 20 > 20_000:
            raise ValueError(
                "merge request description exceeds the publication boundary"
            )
        return self


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


class SplitSliceProposal(BoundaryModel):
    """One ordered, independently mergeable decomposition slice."""

    ordinal: int = Field(ge=1)
    key: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)
    depends_on: tuple[int, ...] = ()

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Require unique positive dependency ordinals."""
        if len(set(value)) != len(value) or any(item < 1 for item in value):
            raise ValueError("dependencies must be unique positive ordinals")
        return value


class SplitPlan(BoundaryModel):
    """Validated topologically ordered worker decomposition plan."""

    slices: tuple[SplitSliceProposal, ...] = Field(min_length=2, max_length=25)

    @model_validator(mode="after")
    def validate_topology(self) -> SplitPlan:
        """Require contiguous ordinals, unique keys, and lower dependencies."""
        if tuple(slice_.ordinal for slice_ in self.slices) != tuple(
            range(1, len(self.slices) + 1)
        ):
            raise ValueError("slice ordinals must be contiguous starting at one")
        if len({slice_.key for slice_ in self.slices}) != len(self.slices):
            raise ValueError("slice keys must be unique")
        if any(
            dependency >= slice_.ordinal
            for slice_ in self.slices
            for dependency in slice_.depends_on
        ):
            raise ValueError("dependencies must refer to lower ordinals")
        return self


class SplitChildEvidence(BoundaryModel):
    """One durable child binding for an ordered split slice."""

    ordinal: int = Field(ge=1)
    key: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    issue_iid: int = Field(gt=0)


class WorkerExitEvidence(BoundaryModel):
    """Bounded, safe diagnostic evidence from one failed worker process."""

    returncode: int
    diagnostic: str = Field(min_length=1)

    @field_validator("returncode")
    @classmethod
    def validate_nonzero_returncode(cls, value: int) -> int:
        """Keep exit evidence exclusive to a failed worker process."""
        if value == 0:
            raise ValueError("worker exit returncode must be nonzero")
        return value

    @field_validator("diagnostic")
    @classmethod
    def validate_diagnostic(cls, value: str) -> str:
        """Reject control-bearing or oversized durable diagnostic text."""
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("worker exit diagnostic exceeds 2048 UTF-8 bytes")
        if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
            raise ValueError(
                "worker exit diagnostic contains unsafe control characters"
            )
        if " ".join(value.split()) != value:
            raise ValueError("worker exit diagnostic must be one canonical line")
        return value


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
    publication_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_request_draft: MergeRequestDraft | None = None
    receipts: tuple[WorkflowReceiptRecord, ...] = ()
    intent: TransitionIntentRecord | None = None
    error_code: ErrorCode | None = None
    worker_exit: WorkerExitEvidence | None = None
    decomposition_plan: SplitPlan | None = None
    decomposition_progress: tuple[SplitChildEvidence, ...] = ()
    summary: str = ""
    next_action: str = ""

    @model_validator(mode="after")
    def validate_decomposition(self) -> LedgerRecord:
        """Keep durable decomposition state and recovered child progress coherent."""
        requires_plan = self.state is RunnerState.DECOMPOSING or (
            self.state is RunnerState.RETRY_WAIT
            and self.prior_state is RunnerState.DECOMPOSING
        )
        if (
            requires_plan or self.decomposition_progress
        ) and self.decomposition_plan is None:
            raise ValueError("decomposition state or progress requires a plan")
        if self.decomposition_plan is not None:
            if len(self.decomposition_progress) > len(self.decomposition_plan.slices):
                raise ValueError("decomposition progress exceeds its plan")
            progress_iids = tuple(
                item.issue_iid for item in self.decomposition_progress
            )
            if len(set(progress_iids)) != len(progress_iids) or any(
                iid == self.issue_iid for iid in progress_iids
            ):
                raise ValueError("decomposition progress has invalid child issue IDs")
            expected = self.decomposition_plan.slices[
                : len(self.decomposition_progress)
            ]
            if any(
                evidence.ordinal != slice_.ordinal
                or evidence.key != slice_.key
                or evidence.fingerprint
                != split_fingerprint(self.project_id, self.issue_iid, slice_.key)
                for evidence, slice_ in zip(
                    self.decomposition_progress, expected, strict=True
                )
            ):
                raise ValueError("decomposition progress is not an exact plan prefix")
        return self


class WorkerResultEnvelope(BoundaryModel):
    """Versioned, identity-bound worker segment result."""

    protocol_version: Literal[1] = 1
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    ledger_revision: int = Field(ge=0)
    expected_state: RunnerState
    expected_phase: WorkflowPhase
    workflow_lane: WorkflowLane
    result_kind: Literal[
        "ready_to_publish", "retryable", "needs_human", "split_required"
    ]
    phase_status: Literal["complete", "rejected"]
    completed_phase: WorkflowPhase
    rejection_route: WorkflowPhase | None = None
    receipts: tuple[str, ...] = ()
    split_plan: SplitPlan | None = None
    change_summary: str = Field(min_length=1, max_length=20_000)
    merge_request_draft: MergeRequestDraft | None = None
    error_code: ErrorCode | None = None
    redacted_summary: str = ""
    next_action: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_split_context(self) -> WorkerResultEnvelope:
        """Keep split-required envelopes unambiguous and worker-only."""
        if self.result_kind == "split_required":
            if (
                self.expected_phase is not WorkflowPhase.WORKFLOW
                or self.completed_phase is not WorkflowPhase.WORKFLOW
                or self.workflow_lane is not WorkflowLane.UNKNOWN
                or self.phase_status != "complete"
                or self.receipts
                or self.merge_request_draft is not None
                or self.error_code is not None
                or self.split_plan is None
            ):
                raise ValueError("split_required envelope has invalid context")
        elif self.split_plan is not None:
            raise ValueError("split_plan is only valid for split_required")
        return self


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
    source_sha: str | None = None
    worktree: Path
    merge_request_iid: int | None = None
    merge_request_url: str | None = None
    error_code: ErrorCode | None = None
    worker_exit: WorkerExitEvidence | None = None
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
    segments_run: int | None = Field(default=None, ge=0)
    stop_reason: str | None = None
