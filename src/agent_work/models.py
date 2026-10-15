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
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
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
    label_for_state,
)
from .red_checkpoint import RedCheckpointEvidence, RedCheckpointRequest


class BoundaryModel(BaseModel):
    """Strict immutable base for data crossing a system boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


SCOPE_EVIDENCE_MAX_BYTES = 262_144
ScopeReason = Literal[
    "approved",
    "human_verifier_unavailable",
    "human_unverified",
    "non_human_author",
    "ambiguous_reply",
    "missing_requested_revision",
    "identity_mismatch",
    "source_changed",
    "scope_changed",
    "event_changed",
    "event_missing",
    "incomplete_source",
    "source_unavailable",
    "revoked",
    "stale_binding",
    "invalid_record",
    "invalid_input",
]


class _ScopeModel(BoundaryModel):
    @field_validator("*", mode="after")
    @classmethod
    def _valid_text(cls, value: object) -> object:
        for item in value if isinstance(value, tuple) else (value,):
            if isinstance(item, str):
                try:
                    size = len(item.encode("utf-8"))
                except UnicodeError as error:
                    raise ValueError("invalid scope text") from error
                if size > SCOPE_EVIDENCE_MAX_BYTES:
                    raise ValueError("scope text exceeds evidence limit")
        return value


class ScopeCriterion(_ScopeModel):
    """One observable requirement with its authoritative source and evidence."""

    id: StrictStr = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    behavior: StrictStr = Field(min_length=1, max_length=2048)
    source_ref: StrictStr = Field(min_length=1, max_length=2048)
    constraints: tuple[StrictStr, ...] = ()
    required_evidence: tuple[StrictStr, ...] = ()


class ScopeRevision(_ScopeModel):
    """Exact source description and ordered task projection under review."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    host: StrictStr = Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]*$"
    )
    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    source_issue_iid: StrictInt = Field(gt=0)
    scope_revision: StrictInt = Field(gt=0)
    source_description_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    criteria: tuple[ScopeCriterion, ...] = Field(min_length=1)
    baseline_sha: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def _unique_criteria(self) -> ScopeRevision:
        if len({item.id for item in self.criteria}) != len(self.criteria):
            raise ValueError("duplicate scope criterion")
        if len(self.model_dump_json().encode("utf-8")) > SCOPE_EVIDENCE_MAX_BYTES:
            raise ValueError("scope exceeds evidence limit")
        return self

    def canonical_digest(self) -> str:
        """Hash deterministic UTF-8 JSON while preserving criterion order."""
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


class ScopeApprovalRequest(_ScopeModel):
    """Retained review request that predates the selected approval event."""

    scope: ScopeRevision
    requested_revision: StrictInt = Field(gt=0)
    requested_description_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at: AwareDatetime
    approval_note_id: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _request_matches(self) -> ScopeApprovalRequest:
        if (
            self.requested_revision != self.scope.scope_revision
            or self.requested_description_sha256 != self.scope.source_description_sha256
        ):
            raise ValueError("request does not bind scope revision")
        return self


class ScopeApprovalEvent(_ScopeModel):
    """Exact authenticated event identity, digest and edit metadata."""

    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    note_id: StrictInt = Field(gt=0)
    author_id: StrictInt = Field(gt=0)
    body_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime
    updated_at: AwareDatetime


class ScopeSourceNote(_ScopeModel):
    """Authenticated source note facts without approval interpretation."""

    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    id: StrictInt = Field(gt=0)
    body: StrictStr
    author_id: StrictInt = Field(gt=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    system: StrictBool


class ScopeSourceAuthor(_ScopeModel):
    """Numeric account facts that cannot establish personhood alone."""

    id: StrictInt = Field(gt=0)
    username: StrictStr = Field(min_length=1, max_length=255)
    bot: StrictBool | None = None
    state: StrictStr | None = None
    account_type: StrictStr | None = None


class ScopeSourceObservation(_ScopeModel):
    """Complete authenticated source projection for fresh verification."""

    host: StrictStr
    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    description: StrictStr
    notes: tuple[ScopeSourceNote, ...]
    authors: tuple[ScopeSourceAuthor, ...]
    service_actor_id: StrictInt = Field(gt=0)
    complete: StrictBool = True
    updated_at: AwareDatetime | None = None


class ScopeApprovalBinding(_ScopeModel):
    """Informational evidence retained with its original review request."""

    request: ScopeApprovalRequest
    event: ScopeApprovalEvent | None = None
    state: Literal["pending", "approved", "unknown", "stale", "revoked"] = "unknown"
    reason: ScopeReason
    scope_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digest_matches(self) -> ScopeApprovalBinding:
        if self.scope_sha256 != self.request.scope.canonical_digest():
            raise ValueError("scope binding digest changed")
        if self.event is not None and (
            self.event.project_id != self.request.scope.project_id
            or self.event.issue_iid != self.request.scope.source_issue_iid
        ):
            raise ValueError("scope event identity changed")
        if self.state == "approved" and self.event is None:
            raise ValueError("approved evidence requires an event")
        return self


class InterruptedClaimReceipt(BoundaryModel):
    """Durable identity of a reviewed dirty-claim handoff."""

    issue_iid: int = Field(gt=0)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    dirty_snapshot_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    blocked_record_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


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

    @field_validator("description")
    @classmethod
    def reject_escaped_linebreaks(cls, value: str) -> str:
        """Reject a commonly accidental shell-escaped Markdown body."""
        if "\\n\\n" in value or "\\r\\n" in value:
            raise ValueError("merge request description contains escaped linebreaks")
        return value

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


class RecoveryChildState(BoundaryModel):
    """Durable condition and outcome for one blocked original's child handoff."""

    condition_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempts: int = Field(ge=0)
    cooldown_until: datetime | None = None
    child_iid: int | None = Field(default=None, gt=0)


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
    available: int | None = Field(default=None, ge=0)
    shortfall: int | None = Field(default=None, ge=0)
    decision: Literal["run"]

    @model_validator(mode="after")
    def validate_capacity(self) -> AuditCapacity:
        """Require arithmetic and decision to agree with the evidence."""
        if self.available is None or self.shortfall is None:
            if self.available is not None or self.shortfall is not None:
                raise ValueError(
                    "capacity available and shortfall must both be reported or unreported"
                )
            return self
        if self.shortfall != max(0, self.required - self.available):
            raise ValueError("capacity shortfall must match required and available")
        if self.available < self.required:
            raise ValueError("available capacity must meet required capacity")
        if self.shortfall != 0 or self.decision != "run":
            raise ValueError("capacity decision must match shortfall")
        return self


class AuditTopologyRole(BoundaryModel):
    """One manifest role in a launch-scoped capacity topology."""

    skill: str = Field(pattern=r"^\$[a-z0-9-]+$")
    owner: str = Field(min_length=1)
    phase: str = Field(min_length=1, max_length=8)
    tier: Literal["most_capable", "balanced", "fast"]
    reuse_from: str | None = Field(default=None, pattern=r"^\$[a-z0-9-]+$")


class AuditOwnerHeadroom(BoundaryModel):
    """Fresh child and descendant capacity available to one topology owner."""

    owner: str = Field(min_length=1)
    direct_children_required: int = Field(ge=0)
    direct_children_headroom: int = Field(ge=0)
    descendants_required: int = Field(ge=0)
    descendants_headroom: int = Field(ge=0)


def _is_topology_descendant(
    role: AuditTopologyRole,
    owner: str,
    roles: dict[str, AuditTopologyRole],
) -> bool:
    """Return whether a role is below an owner in the declared role tree."""
    current = role
    while current.owner != "main":
        if current.owner == owner:
            return True
        parent = roles.get(current.owner)
        if parent is None:
            return False
        current = parent
    return owner == "main"


class AuditTopology(BoundaryModel):
    """Exact manifest ownership and live-row evidence for one launch."""

    widest_live_row: tuple[str, ...] = Field(min_length=1, max_length=32)
    required_live_slots: int = Field(ge=1)
    available_live_slots: int = Field(ge=0)
    roles: tuple[AuditTopologyRole, ...] = Field(min_length=1, max_length=32)
    owner_headroom: tuple[AuditOwnerHeadroom, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_topology(self) -> AuditTopology:
        """Reject incomplete, overlapping, or arithmetically unsafe topology data."""
        if len(set(self.widest_live_row)) != len(self.widest_live_row):
            raise ValueError("widest live row must not repeat roles")
        if self.required_live_slots != len(self.widest_live_row):
            raise ValueError("required live slots must match widest live row")
        if self.available_live_slots < self.required_live_slots:
            raise ValueError("available live slots must cover widest live row")
        roles = {role.skill: role for role in self.roles}
        if len(roles) != len(self.roles):
            raise ValueError("topology roles must be unique")
        for role in self.roles:
            seen = {role.skill}
            owner = role.owner
            while owner != "main":
                parent = roles.get(owner)
                if parent is None:
                    raise ValueError("topology owner must name main or a topology role")
                if parent.skill in seen:
                    raise ValueError("topology ownership must not contain a cycle")
                seen.add(parent.skill)
                owner = parent.owner
        phase_order = {
            "1": 1,
            "2a": 2,
            "2b": 2,
            "2c": 2,
            "3": 3,
            "4": 4,
            "4a": 4,
            "5b": 5,
            "5c": 5,
            "5d": 5,
            "5e": 5,
            "5f": 5,
            "5g": 5,
            "5h": 5,
            "5i": 5,
            "5j": 5,
            "6": 6,
        }
        source_counts: dict[str, int] = {}
        for role in self.roles:
            if role.reuse_from is None:
                continue
            source = roles.get(role.reuse_from)
            if source is None:
                raise ValueError("reuse_from must name a topology role")
            if source.owner != role.owner or source.tier != role.tier:
                raise ValueError("reuse_from requires the same owner and tier")
            if phase_order.get(source.phase, 0) >= phase_order.get(role.phase, 0):
                raise ValueError("reuse_from source phase must complete before target")
            source_counts[source.skill] = source_counts.get(source.skill, 0) + 1
        if any(count > 1 for count in source_counts.values()):
            raise ValueError("reuse_from must not overlap concurrent targets")
        owners = {entry.owner: entry for entry in self.owner_headroom}
        if len(owners) != len(self.owner_headroom):
            raise ValueError("owner_headroom owners must be unique")
        children: dict[str, list[AuditTopologyRole]] = {}
        for role in self.roles:
            children.setdefault(role.owner, []).append(role)
        for owner, entry in owners.items():
            direct = sum(role.reuse_from is None for role in children.get(owner, ()))
            descendants = sum(
                role.reuse_from is None and _is_topology_descendant(role, owner, roles)
                for role in self.roles
            )
            if (
                entry.direct_children_required != direct
                or entry.descendants_required != descendants
                or entry.direct_children_headroom < direct
                or entry.descendants_headroom < descendants
            ):
                raise ValueError("owner_headroom must match topology requirements")
        if set(owners) != set(children):
            raise ValueError("owner_headroom must cover every topology owner")
        return self


class AuditRetainedObjects(BoundaryModel):
    """Global fresh-handle evidence retained with a successful workflow audit."""

    ceiling: int = Field(ge=1)
    current: int = Field(ge=0)
    required_fresh_handles: int = Field(ge=1)
    fresh_headroom: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_retained_objects(self) -> AuditRetainedObjects:
        """Require global retained-object arithmetic to cover fresh handles."""
        if self.current + self.required_fresh_handles > self.ceiling:
            raise ValueError("retained_objects ceiling must cover current and fresh")
        if (
            self.fresh_headroom
            != self.ceiling - self.current - self.required_fresh_handles
        ):
            raise ValueError("retained_objects fresh_headroom must match arithmetic")
        return self


class AuditReceiptSummary(BoundaryModel):
    """Compact, bounded evidence for a completed whole workflow."""

    lane: Literal["coding", "content"]
    capacity: AuditCapacity
    topology: AuditTopology | None = None
    retained_objects: AuditRetainedObjects | None = None
    role_receipts: tuple[str, ...] = Field(max_length=32)
    changed_paths: tuple[str, ...] = Field(max_length=128)
    tests: tuple[str, ...] = Field(max_length=64)
    reviewers: tuple[str, ...] = Field(max_length=32)
    findings: tuple[str, ...] = Field(max_length=64)
    corrections: tuple[str, ...] = Field(max_length=64)
    red_checkpoint: RedCheckpointEvidence | None = None

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


class PublicationCorrectionIdentity(BoundaryModel):
    """Immutable controller-authored identity for one dirty publication correction."""

    mode: Literal["publication_commit_rejected", "authored_candidate_recovery"]
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    worktree: Path
    source_branch: str = Field(min_length=1)
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_head: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    index_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    staged_paths: tuple[str, ...] = Field(min_length=1, max_length=128)
    unstaged_paths: tuple[str, ...] = Field(max_length=128)
    workflow_lane: WorkflowLane
    red_checkpoint: RedCheckpointEvidence | None = None

    @field_validator("worktree")
    @classmethod
    def validate_worktree(cls, value: Path) -> Path:
        """Require the captured worktree to be explicit."""
        if not value.is_absolute():
            raise ValueError("correction worktree must be absolute")
        return value

    @field_validator("staged_paths", "unstaged_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require normalized, unique repository-relative path snapshots."""
        if tuple(sorted(value)) != value or len(set(value)) != len(value):
            raise ValueError("correction paths must be sorted and unique")
        if any(
            not item
            or "\x00" in item
            or len(item.encode("utf-8")) > 4_096
            or PurePosixPath(item).is_absolute()
            or ".." in PurePosixPath(item).parts
            or PurePosixPath(item).as_posix() != item
            for item in value
        ):
            raise ValueError("correction paths must be normalized")
        return value

    @model_validator(mode="after")
    def validate_lane_evidence(self) -> PublicationCorrectionIdentity:
        """Bind coding corrections to Red evidence and content corrections to none."""
        if self.workflow_lane is WorkflowLane.CODING and self.red_checkpoint is None:
            raise ValueError("coding correction requires Red checkpoint evidence")
        if (
            self.workflow_lane is not WorkflowLane.CODING
            and self.red_checkpoint is not None
        ):
            raise ValueError(
                "only coding correction may retain Red checkpoint evidence"
            )
        if self.workflow_lane is WorkflowLane.UNKNOWN:
            raise ValueError("correction workflow lane must be known")
        if (
            self.mode == "authored_candidate_recovery"
            and self.baseline_sha == self.head_sha
        ):
            raise ValueError("authored candidate must retain its original base")
        return self


class ConditionEvidence(BoundaryModel):
    """One bounded current or historical operational condition."""

    occurred_at: datetime | None = None
    retry_count: int = Field(default=0, ge=0)
    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=2_048)


StageTimingReason = Literal[
    "missing_identity",
    "identity_mismatch",
    "missing_provenance",
    "unknown_history",
    "truncated_history",
    "invalid_history",
    "clock_unavailable",
    "unsupported_wait",
    "internal_roles_unavailable",
]


class StageTimingIdentity(BoundaryModel):
    """Exact controller source context for one coarse observation."""

    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    source_sha: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: StrictStr = Field(pattern=r"^[0-9a-f]{40}$")


class StageTimingProvenance(BoundaryModel):
    """Controller boundary revision and immutable source identity."""

    identity: StageTimingIdentity
    boundary_revision: StrictInt = Field(ge=0)


class StageTimingStage(BoundaryModel):
    """Aggregate of individually verified coarse observations."""

    phase_or_role: StrictStr = Field(min_length=1, max_length=128)
    classification: Literal["execution", "wait"]
    observations: StrictInt = Field(gt=0)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)


class StageTimingReport(BoundaryModel):
    """Partial controller execution evidence with explicit missing coverage."""

    identity: StageTimingIdentity | None
    coverage: Literal["partial", "unavailable"]
    history_complete: StrictBool
    stages: tuple[StageTimingStage, ...] = Field(default=(), max_length=64)
    execution_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wait_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unavailable_reasons: tuple[StageTimingReason, ...] = Field(default=(), max_length=9)
    internal_roles: Literal["unavailable"] = "unavailable"
    external_human_wait: Literal["unavailable"] = "unavailable"


class PhaseTiming(BoundaryModel):
    """One bounded controller-authored workflow timing observation."""

    phase_or_role: str = Field(min_length=1, max_length=128)
    attempt: int = Field(gt=0)
    started_at: datetime
    finished_at: datetime
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    outcome: str = Field(min_length=1, max_length=4_096)
    classification: Literal["execution", "wait"]
    provenance: StageTimingProvenance | None = None

    @model_serializer(mode="wrap")
    def _serialize_legacy_timing(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        data = cast("dict[str, object]", handler(self))
        if self.provenance is None:
            data.pop("provenance", None)
        return data

    @model_validator(mode="after")
    def validate_chronology(self) -> PhaseTiming:
        """Require exact UTC chronology and controller-computed elapsed time."""
        if self.started_at.utcoffset() != timedelta(
            0
        ) or self.finished_at.utcoffset() != timedelta(0):
            raise ValueError("phase timing timestamps must be timezone-aware UTC")
        elapsed = (self.finished_at - self.started_at).total_seconds()
        if elapsed < 0:
            raise ValueError("phase timing finish must not precede its start")
        if self.elapsed_seconds != elapsed:
            raise ValueError("phase timing elapsed_seconds must match chronology")
        return self


_OUTCOME_NOTE_FIELDS = (
    "outcome",
    "explanation",
    "evidence",
    "retained_work",
    "next_action",
    "human_decision",
)


def outcome_note_fingerprint(fields: Mapping[str, str]) -> str:
    """Hash exactly the six canonical note fields for durable comparison."""
    if fields.keys() != set(_OUTCOME_NOTE_FIELDS):
        raise ValueError("outcome note fingerprint requires six exact fields")
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OutcomeProjectionRecord(BoundaryModel):
    """Durable progress for projecting one structured issue outcome."""

    target_state: RunnerState
    target_phase: WorkflowPhase
    target_label: str
    outcome_key: str = Field(min_length=1, max_length=512)
    outcome: str = Field(min_length=1, max_length=20_000)
    explanation: str = Field(min_length=1, max_length=20_000)
    evidence: str = Field(min_length=1, max_length=20_000)
    retained_work: str = Field(min_length=1, max_length=20_000)
    next_action: str = Field(min_length=1, max_length=20_000)
    human_decision: str = Field(min_length=1, max_length=20_000)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    label_complete: bool = False
    note_complete: bool = False

    @field_validator("outcome_key")
    @classmethod
    def validate_outcome_key_bytes(cls, value: str) -> str:
        """Match the GitLab outcome note identity byte limit."""
        if len(value.encode()) > 512:
            raise ValueError("outcome key exceeds GitLab byte limit")
        return value

    @model_validator(mode="after")
    def validate_projection(self) -> OutcomeProjectionRecord:
        """Bind the label and fingerprint to the durable target and note."""
        if self.target_state not in {
            RunnerState.RETRY_WAIT,
            RunnerState.BLOCKED,
            RunnerState.FAILED,
            RunnerState.NEEDS_REPAIR,
            RunnerState.REPAIR_FAILED,
            RunnerState.SUGGEST_CLOSE,
        }:
            raise ValueError("state has no outcome projection")
        if self.target_label != label_for_state(self.target_state):
            raise ValueError("outcome projection label does not match state")
        fields = {name: getattr(self, name) for name in _OUTCOME_NOTE_FIELDS}
        expected = outcome_note_fingerprint(fields)
        if self.fingerprint != expected:
            raise ValueError("outcome projection fingerprint does not match note")
        if self.note_complete and not self.label_complete:
            raise ValueError("outcome note cannot complete before label")
        return self


class LedgerRecord(BoundaryModel):
    """Durable state for one issue."""

    schema_version: Literal[1, 2] = 1
    revision: int = Field(default=0, ge=0)
    scope_binding: ScopeApprovalBinding | None = None
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    state: RunnerState
    phase: WorkflowPhase
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind = WorkKind.ORDINARY
    source_branch: str = Field(min_length=1)
    issue_title: str | None = Field(default=None, max_length=512)
    main_branch: str | None = Field(default=None, max_length=255)
    main_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    worktree: Path
    retry_count: int = Field(default=0, ge=0)
    retry_at: datetime | None = None
    automatic_repair_count: int = Field(default=0, ge=0)
    automatic_repair_at: datetime | None = None
    blocked_cause: str | None = Field(default=None, max_length=2_048)
    prior_state: RunnerState | None = None
    prior_phase: WorkflowPhase | None = None
    merge_request_iid: int | None = Field(default=None, gt=0)
    merge_request_url: str | None = None
    source_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    remote_baseline_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    remote_checkpoint_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    remote_event_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    worktree_dirty: bool = False
    owner_status: str = "missing"
    split_prerequisites: tuple[tuple[int, str], ...] = ()
    publication_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_request_draft: MergeRequestDraft | None = None
    feedback_cursor: MergeRequestFeedbackCursor | None = None
    pending_feedback: PendingMergeRequestFeedback | None = None
    repair_attempt: RepairAttemptEvidence | None = None
    suggest_close_evidence: SuggestCloseEvidence | None = None
    receipts: tuple[WorkflowReceiptRecord, ...] = ()
    intent: TransitionIntentRecord | None = None
    recovery_child: RecoveryChildState | None = None
    outcome_projection: OutcomeProjectionRecord | None = None
    error_code: ErrorCode | None = None
    worker_exit: WorkerExitEvidence | None = None
    activation_started_at: datetime | None = None
    latest_meaningful_update_at: datetime | None = None
    completed_at: datetime | None = None
    audit_evidence: AuditReceiptSummary | Literal["unknown"] | None = "unknown"
    correction_identity: PublicationCorrectionIdentity | None = None
    interrupted_claim_receipt: InterruptedClaimReceipt | None = None
    condition_history: tuple[ConditionEvidence, ...] = Field(default=(), max_length=64)
    phase_timings: tuple[PhaseTiming, ...] = Field(default=(), max_length=64)
    phase_timing_count: StrictInt | None = Field(default=None, ge=0)
    phase_timing_unavailable_reasons: tuple[StageTimingReason, ...] = Field(
        default=(), max_length=9
    )
    summary: str = ""
    next_action: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_record(cls, value: object) -> object:
        """Discard explicitly retired fields from older durable ledger records."""
        if not isinstance(value, dict):
            return value
        record = cast("dict[str, object]", value)
        state = record.get("state")
        prior_state = record.get("prior_state")
        has_legacy_fields = any(
            field in record
            for field in ("decomposition_plan", "decomposition_progress")
        )
        has_retired_gate_evidence = "publication_gate_evidence" in record
        if (
            state != "decomposing"
            and prior_state != "decomposing"
            and not has_legacy_fields
            and not has_retired_gate_evidence
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
        normalized.pop("publication_gate_evidence", None)
        return normalized

    @model_validator(mode="after")
    def validate_lifecycle(self) -> LedgerRecord:
        """Keep durable lifecycle evidence coherent."""
        if self.scope_binding is not None and (
            self.scope_binding.request.scope.project_id != self.project_id
            or self.scope_binding.request.scope.issue_iid != self.issue_iid
        ):
            raise ValueError("scope binding record identity changed")
        if self.outcome_projection is not None and (
            self.outcome_projection.target_state is not self.state
            or self.outcome_projection.target_phase is not self.phase
        ):
            raise ValueError("outcome projection does not match durable state")
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
        "red_checkpoint_ready",
        "retryable",
        "needs_human",
        "split_required",
        "suggest_close",
    ]
    phase_status: Literal["complete", "rejected"]
    completed_phase: WorkflowPhase
    rejection_route: WorkflowPhase | None = None
    split_plan: SplitPlan | None = None
    change_summary: str = Field(min_length=1, max_length=20_000)
    merge_request_draft: MergeRequestDraft | None = None
    error_code: ErrorCode | None = None
    redacted_summary: str = ""
    next_action: str = Field(min_length=1, max_length=2_000)
    suggest_close_evidence: SuggestCloseEvidence | None = None
    red_checkpoint_request: RedCheckpointRequest | None = None

    @model_validator(mode="after")
    def validate_split_context(self) -> WorkerResultEnvelope:
        """Keep split-required envelopes unambiguous and worker-only."""
        if self.result_kind == "red_checkpoint_ready":
            if (
                self.expected_phase is not WorkflowPhase.WORKFLOW
                or self.completed_phase is not WorkflowPhase.WORKFLOW
                or self.workflow_lane is not WorkflowLane.CODING
                or self.phase_status != "complete"
                or self.red_checkpoint_request is None
                or self.merge_request_draft is not None
                or self.split_plan is not None
                or self.error_code is not None
                or self.suggest_close_evidence is not None
                or self.rejection_route is not None
            ):
                raise ValueError("red_checkpoint_ready envelope has invalid context")
        elif self.red_checkpoint_request is not None:
            raise ValueError(
                "red_checkpoint_request is only valid for red_checkpoint_ready"
            )
        if self.result_kind == "split_required":
            if (
                self.protocol_version != 2
                or self.expected_phase is not WorkflowPhase.WORKFLOW
                or self.completed_phase is not WorkflowPhase.WORKFLOW
                or self.workflow_lane is not WorkflowLane.UNKNOWN
                or self.phase_status != "complete"
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
                or self.merge_request_draft is not None
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
        if is_completed_workflow and self.protocol_version != 2:
            raise ValueError("completed workflow requires protocol version 2")
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
    phase_timings: tuple[PhaseTiming, ...] = Field(default=(), max_length=64)
    stage_timing_report: StageTimingReport | None = None
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


class ValidationAdmissionReport(BoundaryModel):
    """Bounded timing for one interactive validation admission."""

    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_at: datetime
    yield_at: datetime | None = None
    validation_started_at: datetime | None = None
    validation_finished_at: datetime | None = None
    resume_at: datetime | None = None
    resolved_at: datetime | None = None
    issue_iid: int | None = Field(default=None, gt=0)
    resolution: Literal[
        "resumed", "unavailable", "closed_without_resume", "ineligible_without_resume"
    ] = "resumed"
    wait_elapsed_seconds: float = Field(ge=0)
    unavailable: bool


class CommandReport(BoundaryModel):
    """Common safe JSON envelope for every CLI action."""

    schema_version: Literal[1, 2] = 1
    command: str = Field(min_length=1)
    scope_binding: ScopeApprovalBinding | None = None
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
    validation_admission: ValidationAdmissionReport | None = None


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
