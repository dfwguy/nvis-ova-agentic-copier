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
from typing import Final

DEFAULT_MAX_ACTIVATION_SECONDS: Final = 10_800


class RunnerState(StrEnum):
    """Durable lifecycle states for one work item."""

    PREPARING = "preparing"
    EXECUTING = "executing"
    RETRY_WAIT = "retry-wait"
    PUBLISHING = "publishing"
    OBSERVING = "observing"
    REPAIR = "repair"
    DECOMPOSING = "decomposing"
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
        RunnerState.DECOMPOSING,
    }
)

LIFECYCLE_LABELS: Final = (
    "agent::running",
    "agent::awaiting-human",
    "agent::blocked",
    "agent::failed",
)

AGENT_OUTCOME_NOTE_MARKER: Final = "<!-- agent-work-outcome:v1 -->"
_SPLIT_MARKER: Final = re.compile(
    r"<!-- agent-work-split:v1 fingerprint=([0-9a-f]{64}) parent=([1-9][0-9]*) key=([a-z0-9]+(?:-[a-z0-9]+)*) dependencies=(none|[1-9][0-9]*(?:,[1-9][0-9]*)*) -->"
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
