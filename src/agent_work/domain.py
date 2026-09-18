# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure domain vocabulary for the recoverable agent-work state machine."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from .models import GitLabIssue

DEFAULT_MAX_ACTIVATION_SECONDS: Final = 10_800
HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS: Final = 120
NONTERMINAL_PIPELINE_STATUSES: Final = frozenset(
    {"created", "waiting_for_resource", "preparing", "scheduled", "pending", "running"}
)


class RunnerState(StrEnum):
    """Durable lifecycle states for one work item."""

    PREPARING = "preparing"
    EXECUTING = "executing"
    RETRY_WAIT = "retry-wait"
    PUBLISHING = "publishing"
    OBSERVING = "observing"
    REPAIR = "repair"
    NEEDS_REPAIR = "needs-repair"
    REPAIR_FAILED = "repair-failed"
    SUGGEST_CLOSE = "suggest-close"
    BLOCKED = "blocked"
    AWAITING_HUMAN = "awaiting-human"
    FAILED = "failed"
    COMPLETE = "complete"


class WorkflowPhase(StrEnum):
    """Resumable repository-workflow phases."""

    WORKFLOW = "workflow"
    DISCOVERY = "discovery"
    CODING_RED_DOCS = "coding-red-docs"
    CODING_GREEN = "coding-green"
    CODING_NONFUNCTIONAL = "coding-nonfunctional"
    CODING_VALIDATION = "coding-validation"
    CODING_FINAL_REVIEW = "coding-final-review"
    CODING_FINALIZE = "coding-finalize"
    CONTENT_WRITE = "content-write"
    CONTENT_REVIEW = "content-review"
    CONTENT_FINALIZE = "content-finalize"
    READY_TO_PUBLISH = "ready-to-publish"
    PIPELINE_WAIT = "pipeline-wait"
    POST_PUBLICATION = "post-publication"


class WorkflowLane(StrEnum):
    """Repository workflow selected during discovery."""

    UNKNOWN = "unknown"
    CODING = "coding"
    CONTENT = "content"


class StatusProjection(StrEnum):
    """Finite public lifecycle values reported by the status command."""

    SELECTED = "selected"
    RUNNING = "running"
    AWAITING_HUMAN_REVIEW = "awaiting-human-review"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    NEEDS_REPAIR = "needs-repair"
    REPAIR_FAILED = "repair-failed"
    SUGGEST_CLOSE = "suggest-close"


class WorkKind(StrEnum):
    """Trusted classifications for issue intake."""

    ORDINARY = "ordinary"
    EXISTING_MR_REMEDIATION = "existing_mr_remediation"
    MERGE_CONFLICT_REPAIR = "merge_conflict_repair"
    PROTECTED_POLICY_MANUAL = "protected_policy_manual"


class ErrorCode(StrEnum):
    """Stable machine-readable runner failures and blockers."""

    LOCK_BUSY = "lock_busy"
    LEDGER_CORRUPT = "ledger_corrupt"
    LEDGER_UNSUPPORTED = "ledger_unsupported"
    LEDGER_WRITE_FAILED = "ledger_write_failed"
    PROJECT_IDENTITY_MISMATCH = "project_identity_mismatch"
    MULTIPLE_RUNNING_PROJECTIONS = "multiple_running_projections"
    MISSING_LEDGER = "missing_ledger"
    ORPHAN_EVIDENCE = "orphan_evidence"
    REMEDIATION_MARKER_INVALID = "remediation_marker_invalid"
    REMEDIATION_MR_MISMATCH = "remediation_mr_mismatch"
    PROTECTED_POLICY_MANUAL = "protected_policy_manual"
    SPLIT_REQUIRED = "split_required"
    WORKTREE_MISSING = "worktree_missing"
    WORKTREE_OWNERSHIP_AMBIGUOUS = "worktree_ownership_ambiguous"
    BRANCH_OWNERSHIP_AMBIGUOUS = "branch_ownership_ambiguous"
    UNSUPPORTED_WORKER_HISTORY = "unsupported_worker_history"
    WORKER_TIMEOUT = "worker_timeout"
    WORKER_EXIT = "worker_exit"
    WORKER_PROTOCOL_INVALID = "worker_protocol_invalid"
    WORKER_IDENTITY_MISMATCH = "worker_identity_mismatch"
    WORKER_RECEIPT_INVALID = "worker_receipt_invalid"
    GIT_TRANSPORT_RETRYABLE = "git_transport_retryable"
    GIT_AUTHENTICATION = "git_authentication"
    GIT_AUTHORIZATION = "git_authorization"
    GIT_POLICY = "git_policy"
    GIT_PUBLICATION_CONFIGURATION = "git_publication_configuration"
    GITLAB_RETRYABLE = "gitlab_retryable"
    RETRY_EXHAUSTED = "retry_exhausted"
    PUBLICATION_IDENTITY_MISMATCH = "publication_identity_mismatch"
    PIPELINE_PENDING = "pipeline_pending"
    PIPELINE_REJECTED = "pipeline_rejected"
    MERGE_CONFLICT = "merge_conflict"
    STATE_RECONCILIATION_FAILED = "state_reconciliation_failed"


ACTIVE_STATES = frozenset(
    {
        RunnerState.PREPARING,
        RunnerState.EXECUTING,
        RunnerState.RETRY_WAIT,
        RunnerState.PUBLISHING,
        RunnerState.OBSERVING,
        RunnerState.REPAIR,
    }
)

# A queued retry is resumable only when no immediate owner is progressing.
IMMEDIATE_OWNER_STATES = ACTIVE_STATES - {RunnerState.RETRY_WAIT}

LIFECYCLE_LABELS: Final = (
    "agent::running",
    "agent::awaiting-human",
    "agent::blocked",
    "agent::failed",
    "agent::needs_repair",
    "agent::repair_failed",
    "agent::suggest_close",
)

MANUAL_TRACKER_LABEL: Final = "agent-work::manual"

_MANUAL_TRACKER_LABEL_SPEC: Final = MappingProxyType(
    {
        "color": "#6B7280",
        "description": "Human-maintained tracker; excluded from fresh agent-work selection.",
    }
)

LIFECYCLE_LABEL_SPECS: Final = MappingProxyType(
    {
        "agent::running": MappingProxyType(
            {"color": "#1F78D1", "description": "Agent work is active."}
        ),
        "agent::awaiting-human": MappingProxyType(
            {
                "color": "#1F78D1",
                "description": "Human review is required.",
            }
        ),
        "agent::blocked": MappingProxyType(
            {"color": "#C91C00", "description": "Agent work is blocked."}
        ),
        "agent::failed": MappingProxyType(
            {"color": "#C91C00", "description": "Agent work failed."}
        ),
        "agent::needs_repair": MappingProxyType(
            {
                "color": "#D4A72C",
                "description": "Retained pipeline repair requires exact recovery evidence.",
            }
        ),
        "agent::repair_failed": MappingProxyType(
            {
                "color": "#C91C00",
                "description": "A repair failed; preserve exact evidence for safe follow-up.",
            }
        ),
        "agent::suggest_close": MappingProxyType(
            {
                "color": "#6F42C1",
                "description": "Structured evidence suggests closure; only a human may close it.",
            }
        ),
    }
)

CANARY_LABEL_SPECS: Final = MappingProxyType(
    {
        name: LIFECYCLE_LABEL_SPECS[name]
        for name in (
            "agent::needs_repair",
            "agent::repair_failed",
            "agent::suggest_close",
        )
    }
    | {MANUAL_TRACKER_LABEL: _MANUAL_TRACKER_LABEL_SPEC}
)


class _Evidence(BaseModel):
    """Bounded non-secret evidence retained at a durable boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SuggestCloseEvidence(_Evidence):
    """Evidence that permits a human-only close suggestion."""

    reason: str
    issue_iid: int = Field(gt=0)
    evidence: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if value not in {
            "duplicate",
            "superseded",
            "already-satisfied",
            "no-longer-applicable",
        }:
            raise ValueError("unsupported close-suggestion reason")
        return value

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip()
            or len(item) > 2048
            or item.strip().lower() in {"close it", "claim"}
            for item in value
        ):
            raise ValueError("close evidence must be bounded and meaningful")
        return value


class RepairAttemptEvidence(_Evidence):
    """Bounded evidence retained for one repair attempt."""

    fingerprint: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        validation_alias=AliasChoices("fingerprint", "reviewed_plan_fingerprint"),
    )
    snapshot: dict[str, object] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("snapshot", "reviewed_snapshot"),
    )
    origin: Literal["reviewed-plan", "automatic-exact-pipeline"] = "reviewed-plan"
    evidence_delta: dict[str, object] = Field(default_factory=dict)
    failure_reason: str = Field(default="", max_length=2048)
    started_at: str = Field(default="", max_length=128)
    elapsed_seconds: float = Field(default=0.0, ge=0, le=DEFAULT_MAX_ACTIVATION_SECONDS)

    @model_validator(mode="after")
    def validate_size(self) -> RepairAttemptEvidence:
        if len(str(self.snapshot)) + len(str(self.evidence_delta)) > 8192:
            raise ValueError("repair evidence exceeds bound")
        return self

    @property
    def reviewed_plan_fingerprint(self) -> str:
        """Read legacy repair-plan field names from older callers."""
        return self.fingerprint

    @property
    def reviewed_snapshot(self) -> dict[str, object]:
        """Read legacy repair-plan field names from older callers."""
        return self.snapshot


AGENT_OUTCOME_NOTE_MARKER: Final = "<!-- agent-work-outcome:v1 -->"
_SPLIT_MARKER: Final = re.compile(
    r"<!-- agent-work-split:v1 fingerprint=([0-9a-f]{64}) parent=([1-9][0-9]*) key=([a-z0-9]+(?:-[a-z0-9]+)*) dependencies=(none|[1-9][0-9]*(?:,[1-9][0-9]*)*) -->"
)
PREREQUISITE_LABEL_PREFIX: Final = "agent-prereq_"
PREREQUISITE_LABEL_PATTERN: Final = re.compile(r"^agent-prereq_([1-9][0-9]*)$")
LEGACY_PREREQUISITE_LABEL_PATTERN: Final = re.compile(r"^agent::prereq_([1-9][0-9]*)$")
PREREQUISITE_LABEL_COLOR: Final = "#6F42C1"
PREREQUISITE_LABEL_DESCRIPTION: Final = (
    "Open split prerequisites for agent-work issue dependency display."
)


@dataclass(frozen=True)
class PrerequisiteLabelProjection:
    """One derived display-label projection for an issue."""

    issue_iid: int
    current_labels: tuple[str, ...]
    intended_labels: tuple[str, ...]


@dataclass(frozen=True)
class PrerequisiteLabelReconciliation:
    """Validated dynamic-label repair intent for one complete issue snapshot."""

    projections: tuple[PrerequisiteLabelProjection, ...]
    label_names: tuple[str, ...]


def prerequisite_label_iid(label: str) -> int | None:
    """Return the IID encoded by an exact canonical or legacy managed label."""
    match = PREREQUISITE_LABEL_PATTERN.fullmatch(
        label
    ) or LEGACY_PREREQUISITE_LABEL_PATTERN.fullmatch(label)
    return None if match is None else int(match.group(1))


def legacy_prerequisite_label_iid(label: str) -> int | None:
    """Return the IID encoded by an exact managed legacy label."""
    match = LEGACY_PREREQUISITE_LABEL_PATTERN.fullmatch(label)
    return None if match is None else int(match.group(1))


def prerequisite_label_name(iid: int) -> str:
    """Return the canonical display label for one positive prerequisite IID."""
    if iid < 1:
        raise ValueError("prerequisite IID must be positive")
    return f"{PREREQUISITE_LABEL_PREFIX}{iid}"


def prerequisite_projection_inventory(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> tuple[PrerequisiteLabelProjection, ...]:
    """Derive safe prerequisite-label projections from a complete issue snapshot."""
    return tuple(
        projection
        for projection in _prerequisite_label_intentions(issues, project_id=project_id)
        if frozenset(projection.current_labels) != frozenset(projection.intended_labels)
    )


def prerequisite_label_reconciliation(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> PrerequisiteLabelReconciliation:
    """Derive all applied label repairs from one validated issue snapshot."""
    intentions = _prerequisite_label_intentions(issues, project_id=project_id)
    return PrerequisiteLabelReconciliation(
        projections=tuple(
            projection
            for projection in intentions
            if frozenset(projection.current_labels)
            != frozenset(projection.intended_labels)
        ),
        label_names=tuple(
            sorted(
                {
                    label
                    for projection in intentions
                    for label in projection.intended_labels
                    if prerequisite_label_iid(label) is not None
                },
                key=lambda label: prerequisite_label_iid(label) or 0,
            )
        ),
    )


def _prerequisite_label_intentions(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> tuple[PrerequisiteLabelProjection, ...]:
    """Derive complete intended labels while validating the trusted snapshot."""
    by_iid = {issue.iid: issue for issue in issues}
    if len(by_iid) != len(issues):
        raise ValueError("issue inventory contains duplicate IIDs")
    projections: list[PrerequisiteLabelProjection] = []
    for issue in sorted(issues, key=lambda item: item.iid):
        if issue.state not in {"opened", "closed"}:
            raise ValueError("issue state is invalid")
        marker = parse_split_marker(issue.description, project_id)
        if marker is None:
            dependency_iids: tuple[int, ...] = ()
        else:
            parent_iid, _key, dependency_iids = marker
            if parent_iid == issue.iid or issue.iid in dependency_iids:
                raise ValueError("invalid split marker dependency")
            if any(dependency_iid not in by_iid for dependency_iid in dependency_iids):
                raise ValueError("split dependency is missing")
        intended_dynamic = (
            tuple(
                prerequisite_label_name(dependency_iid)
                for dependency_iid in sorted(dependency_iids)
                if by_iid[dependency_iid].state == "opened"
            )
            if issue.state == "opened"
            else ()
        )
        intended = (
            *(label for label in issue.labels if prerequisite_label_iid(label) is None),
            *intended_dynamic,
        )
        projections.append(
            PrerequisiteLabelProjection(issue.iid, issue.labels, intended)
        )
    return tuple(projections)


def split_fingerprint(project_id: int, parent_iid: int, key: str) -> str:
    """Return the deterministic identity fingerprint for one split child."""
    return hashlib.sha256(
        f"{project_id}:{parent_iid}:{key}".encode("ascii")
    ).hexdigest()


def render_split_marker(
    project_id: int, parent_iid: int, key: str, dependency_iids: tuple[int, ...]
) -> str:
    """Render the canonical durable split marker."""
    dependencies = ",".join(str(iid) for iid in dependency_iids) or "none"
    return f"<!-- agent-work-split:v1 fingerprint={split_fingerprint(project_id, parent_iid, key)} parent={parent_iid} key={key} dependencies={dependencies} -->"


def parse_split_marker(
    description: str, project_id: int
) -> tuple[int, str, tuple[int, ...]] | None:
    """Parse and validate one split marker, failing closed for malformed sentinels."""
    match = _SPLIT_MARKER.search(description)
    if match is None:
        if "<!-- agent-work-split:v1" in description:
            raise ValueError("malformed split marker")
        return None
    fingerprint, parent, key, dependencies = match.groups()
    dependency_iids = (
        ()
        if dependencies == "none"
        else tuple(int(value) for value in dependencies.split(","))
    )
    if fingerprint != split_fingerprint(project_id, int(parent), key) or len(
        set(dependency_iids)
    ) != len(dependency_iids):
        raise ValueError("invalid split marker")
    return int(parent), key, dependency_iids


@dataclass(frozen=True)
class WorkClassification:
    """Trusted classification derived from untrusted issue text."""

    kind: WorkKind
    merge_request_iid: int | None = None
    source_branch: str | None = None
    source_sha: str | None = None


@dataclass(frozen=True)
class CommandRequest:
    """Parsed non-secret CLI request."""

    command: str
    issue_iid: int | None = None
    choice: str | None = None
    reason: str | None = None
    evidence_urls: tuple[str, ...] = ()
    replacement_issue_iid: int | None = None
    target_label: str | None = None
    plan_file: Path | None = None
    max_segments: int = 16
    max_activation_seconds: int = DEFAULT_MAX_ACTIVATION_SECONDS
    debug: bool = False


def label_for_state(state: RunnerState) -> str | None:
    """Return the one GitLab lifecycle projection for a durable state."""
    if state in ACTIVE_STATES:
        return "agent::running"
    return {
        RunnerState.BLOCKED: "agent::blocked",
        RunnerState.AWAITING_HUMAN: "agent::awaiting-human",
        RunnerState.FAILED: "agent::failed",
        RunnerState.NEEDS_REPAIR: "agent::needs_repair",
        RunnerState.REPAIR_FAILED: "agent::repair_failed",
        RunnerState.SUGGEST_CLOSE: "agent::suggest_close",
        RunnerState.COMPLETE: None,
    }[state]


def retries_remaining(retry_count: int) -> int:
    """Return the remaining automatic retries after completed retries."""
    return max(0, 3 - retry_count)


def next_workflow_phase(phase: WorkflowPhase, lane: WorkflowLane) -> WorkflowPhase:
    """Advance one successfully completed workflow phase."""
    coding = {
        WorkflowPhase.WORKFLOW: WorkflowPhase.READY_TO_PUBLISH,
        WorkflowPhase.DISCOVERY: WorkflowPhase.CODING_RED_DOCS,
        WorkflowPhase.CODING_RED_DOCS: WorkflowPhase.CODING_GREEN,
        WorkflowPhase.CODING_GREEN: WorkflowPhase.CODING_NONFUNCTIONAL,
        WorkflowPhase.CODING_NONFUNCTIONAL: WorkflowPhase.CODING_VALIDATION,
        WorkflowPhase.CODING_VALIDATION: WorkflowPhase.CODING_FINAL_REVIEW,
        WorkflowPhase.CODING_FINAL_REVIEW: WorkflowPhase.CODING_FINALIZE,
        WorkflowPhase.CODING_FINALIZE: WorkflowPhase.READY_TO_PUBLISH,
    }
    content = {
        WorkflowPhase.WORKFLOW: WorkflowPhase.READY_TO_PUBLISH,
        WorkflowPhase.DISCOVERY: WorkflowPhase.CONTENT_WRITE,
        WorkflowPhase.CONTENT_WRITE: WorkflowPhase.CONTENT_REVIEW,
        WorkflowPhase.CONTENT_REVIEW: WorkflowPhase.CONTENT_FINALIZE,
        WorkflowPhase.CONTENT_FINALIZE: WorkflowPhase.READY_TO_PUBLISH,
    }
    return (content if lane is WorkflowLane.CONTENT else coding).get(phase, phase)
