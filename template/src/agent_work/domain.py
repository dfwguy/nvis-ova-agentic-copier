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
import unicodedata
from collections.abc import Mapping
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
    PREPARING = "workflow"
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
    RED_CHECKPOINT_FAILED = "red_checkpoint_failed"
    AGENT_CAPACITY_UNAVAILABLE = "agent_capacity_unavailable"
    GIT_TRANSPORT_RETRYABLE = "git_transport_retryable"
    GIT_AUTHENTICATION = "git_authentication"
    GIT_AUTHORIZATION = "git_authorization"
    GIT_POLICY = "git_policy"
    GIT_PUBLICATION_CONFIGURATION = "git_publication_configuration"
    GITLAB_RETRYABLE = "gitlab_retryable"
    RETRY_EXHAUSTED = "retry_exhausted"
    PUBLICATION_COMMIT_REJECTED = "publication_commit_rejected"
    PUBLICATION_IDENTITY_MISMATCH = "publication_identity_mismatch"
    EXTERNAL_OWNER = "external_owner"
    PIPELINE_PENDING = "pipeline_pending"
    PIPELINE_REJECTED = "pipeline_rejected"
    MERGE_CONFLICT = "merge_conflict"
    STATE_RECONCILIATION_FAILED = "state_reconciliation_failed"


class RedCheckpointFailureStage(StrEnum):
    """Controller-authored diagnostic for a trusted Red boundary failure."""

    RETAINED_PRECHECK = "red_checkpoint: retained checkpoint precheck mismatch"
    CONTEXT_PREPARATION = "red_checkpoint: checkpoint context preparation failed"
    RESULT_REQUEST_PATH = (
        "red_checkpoint: red result request or path verification failed"
    )
    EXECUTION_PERSISTENCE = "red_checkpoint: checkpoint execution or persistence failed"
    RETAINED_RELOAD_MISMATCH = "red_checkpoint: retained evidence reload mismatch"


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


_DIAGNOSTIC_MAX_BYTES: Final = 2048
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:\bbearer\s+|\b(?=[A-Za-z0-9_-]*(?:token|password|secret|api[_-]?key|access[_-]?key)[A-Za-z0-9_-]*\s*[=:])[A-Za-z][A-Za-z0-9_-]*\s*[=:]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"
)
_GLPAT = re.compile(r"\bglpat-[A-Za-z0-9_-]+\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)


def sanitize_diagnostic(value: str, *, limit: int = _DIAGNOSTIC_MAX_BYTES) -> str:
    """Return bounded diagnostic text without control characters or common secrets."""
    normalized = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in value
    )
    redacted = _PRIVATE_KEY.sub("[REDACTED]", normalized)
    redacted = _SECRET_ASSIGNMENT.sub("[REDACTED]", redacted)
    redacted = _GLPAT.sub("[REDACTED]", redacted)
    redacted = _JWT.sub("[REDACTED]", redacted)
    compact = " ".join(redacted.split())
    encoded = compact.encode("utf-8")
    if len(encoded) <= limit:
        return compact
    return encoded[:limit].decode("utf-8", errors="ignore")


def sanitize_outcome_note_field(value: str) -> str:
    """Return the exact bounded text shown in a structured outcome note."""
    return (
        sanitize_diagnostic(re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value), limit=512)
        or "Not recorded."
    )


class PipelineJobEvidence(_Evidence):
    """One validated GitLab job from an exact pipeline inventory."""

    id: int = Field(gt=0)
    name: str = Field(default="unknown", min_length=1, max_length=255)
    stage: str = Field(default="", max_length=255)
    status: str = Field(min_length=1, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=255)
    retried: bool = False
    allow_failure: bool = False
    pipeline_id: int = Field(gt=0)
    pipeline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    @field_validator("name", "stage", "failure_reason", mode="before")
    @classmethod
    def sanitize_metadata(cls, value: str | None) -> str | None:
        """Bound externally supplied job text before retaining it as evidence."""
        return None if value is None else sanitize_diagnostic(value)


class JobTraceEvidence(_Evidence):
    """Sanitized trace captured from GitLab for one selected job."""

    provenance: Literal["gitlab-job-trace"] = "gitlab-job-trace"
    status: Literal["collected"] = "collected"
    text: str = Field(min_length=1, max_length=_DIAGNOSTIC_MAX_BYTES)

    @field_validator("text", mode="before")
    @classmethod
    def validate_text(cls, value: str) -> str:
        sanitized = sanitize_diagnostic(value)
        if not sanitized:
            raise ValueError("trace must contain safe diagnostic text")
        return sanitized


class FailedJobDiagnosticEvidence(_Evidence):
    """Durable, exact-pipeline evidence supplied to an automatic repair."""

    project_id: int = Field(gt=0)
    merge_request_iid: int = Field(gt=0)
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pipeline_id: int = Field(gt=0)
    pipeline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    pipeline_status: Literal["failed"]
    job_id: int = Field(gt=0)
    job_name: str = Field(min_length=1, max_length=255)
    job_stage: str = Field(default="", max_length=255)
    job_status: Literal["failed"]
    failure_reason: str | None = Field(default=None, max_length=255)
    retried: bool = False
    trace_provenance: Literal["gitlab-job-trace"]
    trace_status: Literal["collected"]
    trace: str = Field(min_length=1, max_length=_DIAGNOSTIC_MAX_BYTES)

    @field_validator("job_name", "job_stage", "failure_reason", mode="before")
    @classmethod
    def sanitize_metadata(cls, value: str | None) -> str | None:
        """Bound externally supplied job text before durable repair persistence."""
        return None if value is None else sanitize_diagnostic(value)

    @field_validator("trace", mode="before")
    @classmethod
    def validate_trace(cls, value: str) -> str:
        sanitized = sanitize_diagnostic(value)
        if not sanitized:
            raise ValueError("diagnostic trace must contain safe text")
        return sanitized


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
    diagnostic: FailedJobDiagnosticEvidence | None = None
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
    dependency_proofs: tuple[tuple[int, tuple[tuple[int, str], ...]], ...]


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


@dataclass(frozen=True)
class _PrerequisiteGraph:
    """Validated split-marker graph built from one complete issue snapshot."""

    by_iid: Mapping[int, GitLabIssue]
    markers: Mapping[int, tuple[int, str, tuple[int, ...]]]
    children_by_parent: Mapping[int, tuple[int, ...]]

    def proof(self, issue_iid: int) -> tuple[tuple[int, str], ...]:
        """Return stable prerequisite and decomposition evidence for one issue."""
        marker = self.markers.get(issue_iid)
        if marker is None:
            return ()
        proof: dict[int, str] = {}
        pending = list(reversed(marker[2]))
        while pending:
            dependency_iid = pending.pop()
            if dependency_iid in proof:
                continue
            dependency = self.by_iid[dependency_iid]
            proof[dependency_iid] = dependency.state
            if dependency.state == "closed":
                pending.extend(
                    reversed(self.children_by_parent.get(dependency_iid, ()))
                )
        return tuple(sorted(proof.items()))


def _prerequisite_dependency_proofs(
    graph: _PrerequisiteGraph,
) -> tuple[tuple[int, tuple[tuple[int, str], ...]], ...]:
    """Return every stable dependency proof from one validated graph."""
    return tuple(
        (issue_iid, graph.proof(issue_iid)) for issue_iid in sorted(graph.by_iid)
    )


def prerequisite_dependency_proof(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> tuple[tuple[int, tuple[tuple[int, str], ...]], ...]:
    """Derive every issue's trusted dependency proof from one complete snapshot."""
    graph = _prerequisite_graph(issues, project_id=project_id)
    return _prerequisite_dependency_proofs(graph)


def prerequisite_projection_inventory(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> tuple[PrerequisiteLabelProjection, ...]:
    """Derive safe prerequisite-label projections from a complete issue snapshot."""
    return prerequisite_label_reconciliation(issues, project_id=project_id).projections


def prerequisite_label_reconciliation(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> PrerequisiteLabelReconciliation:
    """Derive all applied label repairs from one validated issue snapshot."""
    graph = _prerequisite_graph(issues, project_id=project_id)
    dependency_proofs = _prerequisite_dependency_proofs(graph)
    intentions = _prerequisite_label_intentions(issues, proofs=dict(dependency_proofs))
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
        dependency_proofs=dependency_proofs,
    )


def _prerequisite_label_intentions(
    issues: tuple[GitLabIssue, ...],
    *,
    proofs: Mapping[int, tuple[tuple[int, str], ...]],
) -> tuple[PrerequisiteLabelProjection, ...]:
    """Derive complete intended labels from validated dependency proofs."""
    projections: list[PrerequisiteLabelProjection] = []
    for issue in sorted(issues, key=lambda item: item.iid):
        intended_dynamic = (
            tuple(
                prerequisite_label_name(dependency_iid)
                for dependency_iid, state in proofs[issue.iid]
                if state == "opened"
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


def _prerequisite_graph(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> _PrerequisiteGraph:
    """Parse and validate one complete issue snapshot exactly once."""
    by_iid = {issue.iid: issue for issue in issues}
    if len(by_iid) != len(issues):
        raise ValueError("issue inventory contains duplicate IIDs")
    markers: dict[int, tuple[int, str, tuple[int, ...]]] = {}
    child_identities: set[tuple[int, str]] = set()
    children_by_parent: dict[int, list[int]] = {}
    edges: dict[int, set[int]] = {issue.iid: set() for issue in issues}
    for issue in issues:
        if issue.state not in {"opened", "closed"}:
            raise ValueError("issue state is invalid")
        if issue.description.count("<!-- agent-work-split:v1") > 1:
            raise ValueError("split marker is ambiguous")
        marker = parse_split_marker(issue.description, project_id)
        if marker is None:
            continue
        parent_iid, key, dependency_iids = marker
        if parent_iid == issue.iid or issue.iid in dependency_iids:
            raise ValueError("invalid split marker dependency")
        if any(dependency_iid not in by_iid for dependency_iid in dependency_iids):
            raise ValueError("split dependency is missing")
        identity = (parent_iid, key)
        if identity in child_identities:
            raise ValueError("split child identity is duplicate")
        child_identities.add(identity)
        markers[issue.iid] = marker
        children_by_parent.setdefault(parent_iid, []).append(issue.iid)
        edges[issue.iid].update(dependency_iids)
        if parent_iid in by_iid:
            edges[parent_iid].add(issue.iid)

    visit_state = {issue_iid: 0 for issue_iid in edges}
    for root_iid in sorted(edges):
        if visit_state[root_iid] != 0:
            continue
        visit_state[root_iid] = 1
        stack = [(root_iid, iter(sorted(edges[root_iid])))]
        while stack:
            _issue_iid, referenced_iids = stack[-1]
            try:
                referenced_iid = next(referenced_iids)
            except StopIteration:
                visit_state[_issue_iid] = 2
                stack.pop()
                continue
            if visit_state[referenced_iid] == 1:
                raise ValueError("split dependency cycle is invalid")
            if visit_state[referenced_iid] == 0:
                visit_state[referenced_iid] = 1
                stack.append((referenced_iid, iter(sorted(edges[referenced_iid]))))
    return _PrerequisiteGraph(
        by_iid=MappingProxyType(by_iid),
        markers=MappingProxyType(markers),
        children_by_parent=MappingProxyType(
            {
                parent_iid: tuple(sorted(child_iids))
                for parent_iid, child_iids in children_by_parent.items()
            }
        ),
    )


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
    _automatic_blocked_retry: bool = False


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
