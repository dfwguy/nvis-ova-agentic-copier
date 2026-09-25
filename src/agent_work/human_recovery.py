# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure classification and reviewed-plan rules for human recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from .domain import IMMEDIATE_OWNER_STATES, RunnerState
from .state_machine import ALLOWED_TRANSITIONS


class RecoveryClassification(StrEnum):
    """Human-facing classification of retained recovery evidence."""

    LEGITIMATELY_WAITING = "legitimately_waiting"
    DEPENDENCY_GATED = "dependency_gated"
    SAFELY_RETRYABLE = "safely_retryable"
    SUPERSEDED_DUPLICATE = "superseded_duplicate"
    CLOSURE_CANDIDATE = "closure_candidate"
    NEEDS_HUMAN_DECISION = "needs_human_decision"
    CLOSED_NO_ACTION = "closed_no_action"


class ResolutionChoice(StrEnum):
    """One explicitly reviewed lifecycle operation."""

    RETRY = "retry"
    CLOSE = "close"
    REOPEN = "reopen"
    RELABEL = "relabel"
    SUPERSEDE = "supersede"


class EvidenceDriftError(ValueError):
    """The evidence reviewed in a resolution plan is no longer current."""


@dataclass(frozen=True)
class RecoveryObservation:
    """Immutable bounded evidence for one retained item."""

    issue_iid: int
    issue_state: str = "opened"
    labels: tuple[str, ...] = ()
    record_state: RunnerState | None = None
    pipeline_status: str | None = None
    split_dependencies: tuple[tuple[int, str], ...] = ()
    stale: bool = False
    ambiguous: bool = False
    ledger_revision: int | None = None
    source_branch: str | None = None
    source_sha: str | None = None
    worktree: str | None = None
    worktree_exists: bool | None = None
    worktree_head: str | None = None
    worktree_dirty: bool | None = None
    worktree_changed_paths: tuple[str, ...] = ()
    merge_request_iid: int | None = None
    merge_request_state: str | None = None
    merge_request_sha: str | None = None
    pipeline_id: int | None = None
    pipeline_sha: str | None = None
    controller_status: str | None = None
    owner_status: str = "missing"
    blocked_cause: str | None = None
    merge_request_url: str | None = None
    error_code: str | None = None
    precheckpoint_test_only_red: bool = False
    retry_ineligible_reason: str | None = None


@dataclass(frozen=True)
class RecoveryOwnerCapacity:
    """Classify retained ownership without changing any workflow state."""

    owner_states: tuple[tuple[int, str], ...]
    identity_conflict: bool
    live_owner_iid: int | None
    unknown_owner_iid: int | None


def classify_recovery_owner_capacity(
    observations: tuple[RecoveryObservation, ...], *, target_iid: int
) -> RecoveryOwnerCapacity:
    """Return deterministic retained-owner evidence for one exact recovery target."""
    retained = tuple(
        sorted(
            (item for item in observations if item.record_state is not None),
            key=lambda item: item.issue_iid,
        )
    )
    identities = (
        tuple(item.source_branch for item in retained if item.source_branch),
        tuple(item.worktree for item in retained if item.worktree),
        tuple(
            str(item.merge_request_iid) for item in retained if item.merge_request_iid
        ),
        tuple(item.merge_request_url for item in retained if item.merge_request_url),
    )
    identity_conflict = any(len(values) != len(set(values)) for values in identities)
    immediate = tuple(
        item for item in retained if item.record_state in IMMEDIATE_OWNER_STATES
    )
    return RecoveryOwnerCapacity(
        owner_states=tuple((item.issue_iid, item.owner_status) for item in retained),
        identity_conflict=identity_conflict,
        live_owner_iid=next(
            (
                item.issue_iid
                for item in immediate
                if item.issue_iid != target_iid and item.owner_status == "active"
            ),
            None,
        ),
        unknown_owner_iid=next(
            (item.issue_iid for item in immediate if item.owner_status == "unknown"),
            None,
        ),
    )


@dataclass(frozen=True)
class RecoveryCandidate:
    """One classified inventory candidate."""

    issue_iid: int
    classification: RecoveryClassification
    observation: RecoveryObservation


@dataclass(frozen=True)
class RecoveryInventory:
    """Ordered, non-claiming retained recovery inventory."""

    candidates: tuple[RecoveryCandidate, ...]


@dataclass(frozen=True)
class ResolutionRequest:
    """Explicit human input for one recovery resolution plan."""

    issue_iid: int
    choice: ResolutionChoice
    reason: str
    evidence_urls: tuple[str, ...] = ()
    replacement_issue_iid: int | None = None
    target_label: str | None = None


@dataclass(frozen=True)
class ResolutionPlan:
    """Stable, evidence-bound resolution preview."""

    issue_iid: int
    choice: ResolutionChoice
    reason: str
    evidence_urls: tuple[str, ...]
    replacement_issue_iid: int | None
    target_label: str | None
    evidence: tuple[tuple[str, object], ...]
    digest: str
    applied: bool = False


def _classification(observation: RecoveryObservation) -> RecoveryClassification:
    if observation.issue_state == "closed" and observation.record_state in {
        None,
        RunnerState.COMPLETE,
    }:
        return RecoveryClassification.CLOSED_NO_ACTION
    if (
        observation.ambiguous
        or observation.owner_status == "unknown"
        or observation.pipeline_status == "failed"
    ):
        return RecoveryClassification.NEEDS_HUMAN_DECISION
    if observation.stale:
        return RecoveryClassification.NEEDS_HUMAN_DECISION
    if observation.issue_state == "closed":
        return RecoveryClassification.SUPERSEDED_DUPLICATE
    if "agent::failed" in observation.labels:
        return RecoveryClassification.CLOSURE_CANDIDATE
    if observation.record_state is RunnerState.AWAITING_HUMAN:
        return RecoveryClassification.NEEDS_HUMAN_DECISION
    if (
        observation.error_code == "red_checkpoint_failed"
        and not observation.precheckpoint_test_only_red
    ):
        return RecoveryClassification.NEEDS_HUMAN_DECISION
    dependency_states = {state for _, state in observation.split_dependencies}
    if "opened" in dependency_states:
        return RecoveryClassification.DEPENDENCY_GATED
    if observation.retry_ineligible_reason is not None:
        return RecoveryClassification.NEEDS_HUMAN_DECISION
    if observation.record_state is RunnerState.BLOCKED or (
        observation.split_dependencies and dependency_states == {"closed"}
    ):
        return RecoveryClassification.SAFELY_RETRYABLE
    return RecoveryClassification.LEGITIMATELY_WAITING


def build_inventory(observations: tuple[RecoveryObservation, ...]) -> RecoveryInventory:
    """Classify retained observations, excluding fresh ordinary work."""
    candidates = tuple(
        RecoveryCandidate(item.issue_iid, _classification(item), item)
        for item in observations
        if item.record_state is not None
        or item.labels
        or item.pipeline_status
        or item.ambiguous
        or item.split_dependencies
    )

    return RecoveryInventory(tuple(sorted(candidates, key=lambda item: item.issue_iid)))


def _evidence(observation: RecoveryObservation) -> tuple[tuple[str, object], ...]:
    return (
        ("issue_iid", observation.issue_iid),
        ("issue_state", observation.issue_state),
        ("labels", observation.labels),
        (
            "record_state",
            None
            if observation.record_state is None
            else observation.record_state.value,
        ),
        ("pipeline_status", observation.pipeline_status),
        ("split_dependencies", observation.split_dependencies),
        ("stale", observation.stale),
        ("ambiguous", observation.ambiguous),
        ("ledger_revision", observation.ledger_revision),
        ("source_branch", observation.source_branch),
        ("source_sha", observation.source_sha),
        ("worktree", observation.worktree),
        ("worktree_exists", observation.worktree_exists),
        ("worktree_head", observation.worktree_head),
        ("worktree_dirty", observation.worktree_dirty),
        ("worktree_changed_paths", observation.worktree_changed_paths),
        ("merge_request_iid", observation.merge_request_iid),
        ("merge_request_state", observation.merge_request_state),
        ("merge_request_sha", observation.merge_request_sha),
        ("pipeline_id", observation.pipeline_id),
        ("pipeline_sha", observation.pipeline_sha),
        ("controller_status", observation.controller_status),
        ("owner_status", observation.owner_status),
        ("merge_request_url", observation.merge_request_url),
    )


def build_resolution_plan(
    request: ResolutionRequest, observation: RecoveryObservation
) -> ResolutionPlan:
    """Create one deterministic, evidence-bound, non-applied resolution plan."""
    if request.issue_iid != observation.issue_iid or not request.reason.strip():
        raise ValueError("resolution requires the observed issue and a reason")
    if request.choice in {ResolutionChoice.CLOSE, ResolutionChoice.SUPERSEDE} and not (
        request.evidence_urls or request.replacement_issue_iid
    ):
        raise ValueError("close and supersede require linked evidence")
    if (
        request.choice is ResolutionChoice.SUPERSEDE
        and request.replacement_issue_iid is None
    ):
        raise ValueError("supersede requires a replacement issue")
    if request.choice is ResolutionChoice.RELABEL and request.target_label not in {
        "agent::blocked",
        "agent::awaiting-human",
        "agent::failed",
    }:
        raise ValueError("relabel requires one supported lifecycle label")
    if request.choice is ResolutionChoice.RELABEL:
        target = {
            "agent::blocked": RunnerState.BLOCKED,
            "agent::awaiting-human": RunnerState.AWAITING_HUMAN,
            "agent::failed": RunnerState.FAILED,
        }[request.target_label or ""]
        if observation.record_state is None or (
            target is not observation.record_state
            and target not in ALLOWED_TRANSITIONS[observation.record_state]
        ):
            raise ValueError("relabel target is not a declared durable transition")
    if request.choice is ResolutionChoice.RETRY and (
        observation.record_state is not RunnerState.BLOCKED
        or any(state != "closed" for _, state in observation.split_dependencies)
    ):
        raise ValueError("retry requires a blocked retained record")
    if request.choice is ResolutionChoice.REOPEN and (
        observation.issue_state != "closed" or observation.record_state is not None
    ):
        raise ValueError("reopen requires a closed issue with no retained record")
    evidence = _evidence(observation)
    payload = {
        "issue_iid": request.issue_iid,
        "choice": request.choice.value,
        "reason": request.reason,
        "evidence_urls": request.evidence_urls,
        "replacement_issue_iid": request.replacement_issue_iid,
        "target_label": request.target_label,
        "evidence": evidence,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    return ResolutionPlan(
        request.issue_iid,
        request.choice,
        request.reason,
        request.evidence_urls,
        request.replacement_issue_iid,
        request.target_label,
        evidence,
        digest,
    )


def verify_resolution_plan(
    plan: ResolutionPlan, observation: RecoveryObservation
) -> None:
    """Fail closed unless fresh observation exactly matches reviewed evidence."""
    request = ResolutionRequest(
        plan.issue_iid,
        plan.choice,
        plan.reason,
        plan.evidence_urls,
        plan.replacement_issue_iid,
        plan.target_label,
    )
    try:
        current = build_resolution_plan(request, observation)
    except ValueError as error:
        raise EvidenceDriftError(
            "resolution evidence changed; create a new preview"
        ) from error
    if current.digest != plan.digest:
        raise EvidenceDriftError("resolution evidence changed; create a new preview")
