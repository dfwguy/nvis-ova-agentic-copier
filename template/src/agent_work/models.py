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

import hashlib
import html
import json
import re
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
    RepairAttemptEvidence,
    RunnerState,
    StatusProjection,
    SuggestCloseEvidence,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
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
    factory_username: str = Field(default="factory-bot", min_length=1, max_length=255)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Require one bare hostname."""
        if "://" in value or "/" in value:
            raise ValueError("host must be a bare hostname")
        return value

    @field_validator("factory_username")
    @classmethod
    def validate_factory_username(cls, value: str) -> str:
        """Require a safe GitLab username, never a display name."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError("factory username must be a safe GitLab username")
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


class MergeRequestFeedback(BoundaryModel):
    """One validated, untrusted merge-request comment."""

    id: int = Field(gt=0)
    source: Literal["general", "discussion"]
    discussion_id: str | None = Field(default=None, max_length=255)
    body: str = ""
    author: str | None = Field(default=None, max_length=255)
    created_at: datetime
    system: bool = False

    @model_validator(mode="after")
    def validate_discussion_identity(self) -> MergeRequestFeedback:
        """Require a real discussion identity only for inline feedback."""
        if self.source == "general" and self.discussion_id is not None:
            raise ValueError("general feedback must not have a discussion identity")
        if self.source == "discussion" and (
            not self.discussion_id or not self.discussion_id.strip()
        ):
            raise ValueError("discussion feedback requires a discussion identity")
        return self


class MergeRequestFeedbackCursor(BoundaryModel):
    """Monotonic IDs acknowledged for one exact MR source SHA."""

    last_processed_general_note_id: int | None = Field(default=None, gt=0)
    last_processed_discussion_note_id: int | None = Field(default=None, gt=0)
    last_relevant_discussion_id: str | None = Field(default=None, max_length=255)
    feedback_source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def validate_discussion_cursor(self) -> MergeRequestFeedbackCursor:
        """Do not retain a discussion identity without its note cursor."""
        if (
            self.last_relevant_discussion_id is not None
            and self.last_processed_discussion_note_id is None
        ):
            raise ValueError("discussion identity requires a discussion note cursor")
        if self.last_relevant_discussion_id is not None and not (
            self.last_relevant_discussion_id.strip()
        ):
            raise ValueError("discussion identity must be nonblank")
        return self


class PendingMergeRequestFeedback(BoundaryModel):
    """One durably selected feedback item before its acknowledgement side effect."""

    target_cursor: MergeRequestFeedbackCursor
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    publication_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    source: Literal["general", "discussion"]
    note_id: int = Field(gt=0)
    discussion_id: str | None = Field(default=None, max_length=255)
    outcome: str | None = Field(default=None, max_length=2_000)
    acknowledgement_ready: bool = False

    @model_validator(mode="after")
    def validate_selected_item(self) -> PendingMergeRequestFeedback:
        """Bind a pending acknowledgement to precisely one feedback item."""
        cursor = self.target_cursor
        if self.source == "general":
            if (
                self.discussion_id is not None
                or cursor.last_processed_general_note_id != self.note_id
            ):
                raise ValueError(
                    "general pending feedback must select its general cursor"
                )
        elif (
            not self.discussion_id
            or not self.discussion_id.strip()
            or cursor.last_processed_discussion_note_id != self.note_id
            or cursor.last_relevant_discussion_id != self.discussion_id
        ):
            raise ValueError(
                "discussion pending feedback must select its discussion cursor"
            )
        if self.outcome is not None and not self.outcome.strip():
            raise ValueError("pending outcome must be nonempty")
        if self.acknowledgement_ready and self.outcome is None:
            raise ValueError("acknowledgement readiness requires an outcome")
        if (
            self.outcome is not None
            and self.publication_sha is not None
            and (cursor.feedback_source_sha != self.publication_sha)
        ):
            raise ValueError("published feedback cursor must use publication SHA")
        return self


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


class AuditCapacity(BoundaryModel):
    """Bounded, coherent live-session capacity decision."""

    required: int = Field(ge=1)
    available: int = Field(ge=0)
    shortfall: int = Field(ge=0)
    decision: Literal["run"]

    @model_validator(mode="after")
    def validate_capacity(self) -> AuditCapacity:
        """Require arithmetic and decision to agree with the evidence."""
        if self.shortfall != max(0, self.required - self.available):
            raise ValueError("capacity shortfall must match required and available")
        if self.available < self.required:
            raise ValueError("available capacity must meet required capacity")
        if self.shortfall != 0 or self.decision != "run":
            raise ValueError("capacity decision must match shortfall")
        return self


class AuditReceiptSummary(BoundaryModel):
    """Compact, bounded evidence for a completed whole workflow."""

    lane: Literal["coding", "content"]
    capacity: AuditCapacity
    role_receipts: tuple[str, ...] = Field(max_length=32)
    changed_paths: tuple[str, ...] = Field(max_length=128)
    tests: tuple[str, ...] = Field(max_length=64)
    reviewers: tuple[str, ...] = Field(max_length=32)
    findings: tuple[str, ...] = Field(max_length=64)
    corrections: tuple[str, ...] = Field(max_length=64)

    @field_validator(
        "role_receipts",
        "changed_paths",
        "tests",
        "reviewers",
        "findings",
        "corrections",
    )
    @classmethod
    def validate_compact_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank or individually oversized durable audit entries."""
        if any(not item.strip() or len(item.encode("utf-8")) > 4_096 for item in value):
            raise ValueError("audit evidence entry exceeds 4096 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_aggregate_size(self) -> AuditReceiptSummary:
        """Keep the entire durable audit compact, not only each entry."""
        if len(self.model_dump_json(exclude_none=True).encode("utf-8")) > 32_768:
            raise ValueError("audit evidence exceeds 32768 UTF-8 bytes")
        if self.lane == "coding" and (not self.tests or not self.reviewers):
            raise ValueError("coding audit requires test and reviewer evidence")
        return self


class ConditionEvidence(BoundaryModel):
    """One bounded current or historical operational condition."""

    occurred_at: datetime | None = None
    retry_count: int = Field(default=0, ge=0)
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=2_048)


class LedgerRecord(BoundaryModel):
    """Durable state for one issue."""

    schema_version: Literal[1, 2] = 1
    revision: int = Field(default=0, ge=0)
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    state: RunnerState
    phase: WorkflowPhase
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind
    source_branch: str = Field(min_length=1)
    issue_title: str | None = Field(default=None, max_length=512)
    main_branch: str | None = Field(default=None, max_length=255)
    main_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    worktree: Path
    retry_count: int = Field(default=0, ge=0)
    retry_at: datetime | None = None
    automatic_repair_count: int = Field(default=0, ge=0)
    automatic_repair_at: datetime | None = None
    prior_state: RunnerState | None = None
    prior_phase: WorkflowPhase | None = None
    merge_request_iid: int | None = Field(default=None, gt=0)
    merge_request_url: str | None = None
    source_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    publication_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_request_draft: MergeRequestDraft | None = None
    feedback_cursor: MergeRequestFeedbackCursor | None = None
    pending_feedback: PendingMergeRequestFeedback | None = None
    repair_attempt: RepairAttemptEvidence | None = None
    suggest_close_evidence: SuggestCloseEvidence | None = None
    receipts: tuple[WorkflowReceiptRecord, ...] = ()
    intent: TransitionIntentRecord | None = None
    error_code: ErrorCode | None = None
    worker_exit: WorkerExitEvidence | None = None
    activation_started_at: datetime | None = None
    latest_meaningful_update_at: datetime | None = None
    completed_at: datetime | None = None
    audit_evidence: AuditReceiptSummary | Literal["unknown"] | None = "unknown"
    condition_history: tuple[ConditionEvidence, ...] = Field(default=(), max_length=64)
    summary: str = ""
    next_action: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_split_record(cls, value: object) -> object:
        """Discard only the retired split checkpoint from an older ledger record."""
        if not isinstance(value, dict):
            return value
        record = cast("dict[str, object]", value)
        state = record.get("state")
        prior_state = record.get("prior_state")
        has_legacy_fields = any(
            field in record
            for field in ("decomposition_plan", "decomposition_progress")
        )
        if (
            state != "decomposing"
            and prior_state != "decomposing"
            and not has_legacy_fields
        ):
            return value
        normalized = dict(record)
        if state == "decomposing":
            normalized["state"] = RunnerState.EXECUTING.value
        if prior_state == "decomposing":
            normalized["prior_state"] = RunnerState.EXECUTING.value
        if has_legacy_fields:
            normalized.pop("decomposition_plan", None)
            normalized.pop("decomposition_progress", None)
        return normalized

    @model_validator(mode="after")
    def validate_lifecycle(self) -> LedgerRecord:
        """Keep durable lifecycle evidence coherent."""
        if (
            self.state is RunnerState.SUGGEST_CLOSE
            and self.suggest_close_evidence is None
        ):
            raise ValueError("suggest-close state requires structured evidence")
        if self.suggest_close_evidence is not None and (
            self.state is not RunnerState.SUGGEST_CLOSE
            or self.suggest_close_evidence.issue_iid != self.issue_iid
        ):
            raise ValueError(
                "suggest-close evidence must bind its matching state and issue"
            )
        if self.state is RunnerState.REPAIR_FAILED and self.repair_attempt is None:
            raise ValueError("repair-failed state requires repair-attempt evidence")
        return self


class WorkerResultEnvelope(BoundaryModel):
    """Versioned, identity-bound worker segment result."""

    protocol_version: Literal[2] = 2
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    ledger_revision: int = Field(ge=0)
    expected_state: RunnerState
    expected_phase: WorkflowPhase
    workflow_lane: WorkflowLane
    result_kind: Literal[
        "ready_to_publish",
        "retryable",
        "needs_human",
        "split_required",
        "suggest_close",
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
    audit_summary: AuditReceiptSummary | None = None
    suggest_close_evidence: SuggestCloseEvidence | None = None

    @model_validator(mode="after")
    def validate_split_context(self) -> WorkerResultEnvelope:
        """Keep split-required envelopes unambiguous and worker-only."""
        if self.result_kind == "split_required":
            if (
                self.protocol_version != 2
                or self.audit_summary is not None
                or self.expected_phase is not WorkflowPhase.WORKFLOW
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
        if self.result_kind == "suggest_close":
            if (
                self.expected_phase is not WorkflowPhase.POST_PUBLICATION
                or self.completed_phase is not WorkflowPhase.POST_PUBLICATION
                or self.phase_status != "complete"
                or self.suggest_close_evidence is None
                or self.suggest_close_evidence.issue_iid != self.issue_iid
                or self.receipts
                or self.merge_request_draft is not None
                or self.audit_summary is not None
                or self.error_code is not None
            ):
                raise ValueError("suggest_close envelope has invalid context")
        elif self.suggest_close_evidence is not None:
            raise ValueError("suggest_close_evidence is only valid for suggest_close")
        is_completed_workflow = (
            self.completed_phase is WorkflowPhase.WORKFLOW
            and self.result_kind == "ready_to_publish"
            and self.phase_status == "complete"
        )
        if is_completed_workflow and (
            self.protocol_version != 2
            or self.audit_summary is None
            or self.audit_summary.lane != self.workflow_lane.value
        ):
            raise ValueError("completed workflow requires v2 matching audit_summary")
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

    project_id: int | None = Field(default=None, gt=0)
    issue_iid: int = Field(gt=0)
    projected_status: StatusProjection | None = None
    state: RunnerState
    phase: WorkflowPhase
    work_kind: WorkKind
    attempt: int = Field(ge=1)
    retries_remaining: int = Field(ge=0)
    retry_at: datetime | None = None
    automatic_repair_count: int = Field(default=0, ge=0)
    automatic_repair_at: datetime | None = None
    source_branch: str
    source_sha: str | None = None
    main_branch: str | None = None
    main_sha: str | None = None
    publication_sha: str | None = None
    worktree: Path
    merge_request_iid: int | None = None
    merge_request_url: str | None = None
    error_code: ErrorCode | None = None
    worker_exit: WorkerExitEvidence | None = None
    current_condition: ConditionEvidence | None = None
    condition_history: tuple[ConditionEvidence, ...] = ()
    audit_evidence: AuditReceiptSummary | Literal["unknown"] | None = None
    activation_started_at: datetime | None = None
    latest_meaningful_update_at: datetime | None = None
    completed_at: datetime | None = None
    issue_title: str | None = None
    corroboration: dict[str, str] = Field(default_factory=dict)
    summary: str = ""
    next_action: str = ""


class PrerequisiteLabelProjectionReport(BoundaryModel):
    """Current and intended dynamic prerequisite labels for one issue."""

    issue_iid: int = Field(gt=0)
    current_labels: tuple[str, ...] = ()
    intended_labels: tuple[str, ...] = ()


class CommandReport(BoundaryModel):
    """Common safe JSON envelope for every CLI action."""

    schema_version: Literal[1, 2] = 1
    command: str = Field(min_length=1)
    applied: bool
    outcome: str = Field(min_length=1)
    project: dict[str, str | int] | None = None
    items: tuple[CommandItemReport, ...] = ()
    actions: tuple[MigrationAction, ...] = ()
    segments_run: int | None = Field(default=None, ge=0)
    stop_reason: str | None = None
    total_items: int | None = Field(default=None, ge=0)
    items_truncated: bool | None = None
    summary: str | None = Field(default=None, max_length=2_048)
    next_action: str | None = Field(default=None, max_length=2_048)
    operational_plan: OperationalPlan | None = None
    recovery_candidates: tuple[RecoveryCandidateReport, ...] = ()
    recovery_summary: RecoverySummary | None = None
    prerequisite_projections: tuple[PrerequisiteLabelProjectionReport, ...] = ()


class RecoverySummary(BoundaryModel):
    """Bounded aggregate of one read-only recovery inventory."""

    total: int = Field(ge=0)
    open: int = Field(ge=0)
    closed: int = Field(ge=0)
    active: int = Field(ge=0)
    blocked: int = Field(ge=0)
    dependency_gated: int = Field(ge=0)
    human_action: int = Field(ge=0)
    no_action: int = Field(ge=0)


class RecoveryCandidateReport(BoundaryModel):
    """Bounded human recovery classification evidence."""

    issue_iid: int = Field(gt=0)
    classification: str
    issue_state: str
    labels: tuple[str, ...] = ()
    record_state: RunnerState | None = None
    priority: int = Field(ge=0)
    corroboration: dict[str, str] = Field(default_factory=dict)
    recommendation: str
    active: bool = False
    human_action_required: bool = False
    blocked_cause: str | None = Field(default=None, max_length=2_048)
    dependency_explanation: str | None = Field(default=None, max_length=2_048)
    next_action: str = Field(default="", max_length=2_048)


class OperationalPlan(BoundaryModel):
    """Immutable, self-authenticating evidence for one operational action."""

    command: str = Field(min_length=1)
    action: str = Field(min_length=1)
    project: dict[str, str | int]
    authenticated_username: str = Field(min_length=1)
    target_iid: int | None = Field(default=None, gt=0)
    evidence: dict[str, str | int | bool | None] = Field(default_factory=dict)
    fingerprint: str = Field(default="", pattern=r"^[0-9a-f]{0,64}$")

    @model_validator(mode="after")
    def validate_fingerprint(self) -> OperationalPlan:
        """Bind the plan to canonical public evidence without retaining secrets."""
        canonical = self.model_dump(exclude={"fingerprint"}, mode="json")
        expected = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("operational plan fingerprint does not match evidence")
        if not self.fingerprint:
            object.__setattr__(self, "fingerprint", expected)
        return self
