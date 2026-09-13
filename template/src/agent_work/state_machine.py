# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure transition planning for recoverable agent work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from .domain import (
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    next_workflow_phase,
)
from .models import LedgerRecord, WorkerResultEnvelope, WorkflowReceiptRecord

ALLOWED_TRANSITIONS: Final = MappingProxyType(
    {
        RunnerState.PREPARING: frozenset(
            {
                RunnerState.EXECUTING,
                RunnerState.OBSERVING,
                RunnerState.RETRY_WAIT,
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.EXECUTING: frozenset(
            {
                RunnerState.DECOMPOSING,
                RunnerState.EXECUTING,
                RunnerState.RETRY_WAIT,
                RunnerState.PUBLISHING,
                RunnerState.OBSERVING,
                RunnerState.BLOCKED,
                RunnerState.AWAITING_HUMAN,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.RETRY_WAIT: frozenset(
            {
                RunnerState.PREPARING,
                RunnerState.DECOMPOSING,
                RunnerState.EXECUTING,
                RunnerState.PUBLISHING,
                RunnerState.OBSERVING,
                RunnerState.REPAIR,
                RunnerState.AWAITING_HUMAN,
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.PUBLISHING: frozenset(
            {
                RunnerState.EXECUTING,
                RunnerState.OBSERVING,
                RunnerState.RETRY_WAIT,
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.OBSERVING: frozenset(
            {
                RunnerState.OBSERVING,
                RunnerState.EXECUTING,
                RunnerState.REPAIR,
                RunnerState.AWAITING_HUMAN,
                RunnerState.RETRY_WAIT,
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.REPAIR: frozenset(
            {
                RunnerState.EXECUTING,
                RunnerState.PUBLISHING,
                RunnerState.RETRY_WAIT,
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.COMPLETE,
                RunnerState.AWAITING_HUMAN,
            }
        ),
        RunnerState.DECOMPOSING: frozenset(
            {
                RunnerState.DECOMPOSING,
                RunnerState.RETRY_WAIT,
                RunnerState.BLOCKED,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.BLOCKED: frozenset(
            {
                RunnerState.PREPARING,
                RunnerState.PUBLISHING,
                RunnerState.OBSERVING,
                RunnerState.COMPLETE,
            }
        ),
        RunnerState.AWAITING_HUMAN: frozenset(
            {
                RunnerState.AWAITING_HUMAN,
                RunnerState.REPAIR,
                RunnerState.RETRY_WAIT,
                RunnerState.COMPLETE,
                RunnerState.BLOCKED,
            }
        ),
        RunnerState.FAILED: frozenset({RunnerState.COMPLETE}),
        RunnerState.COMPLETE: frozenset(),
    }
)

REQUIRED_RECEIPTS: Final = MappingProxyType(
    {
        WorkflowPhase.DISCOVERY: ("$architect",),
        WorkflowPhase.CODING_RED_DOCS: ("$tdd-red", "$diataxis-writer"),
        WorkflowPhase.CODING_GREEN: ("$tdd-green",),
        WorkflowPhase.CODING_NONFUNCTIONAL: ("$nonfunctional-review",),
        WorkflowPhase.CODING_VALIDATION: ("$tdd-judge", "$diataxis-judge"),
        WorkflowPhase.CODING_FINAL_REVIEW: (
            "$judge-quality",
            "$judge-test",
            "$judge-cicd",
            "$tdd-document",
            "$cli-syntax-judge",
            "$filesystem-structure-judge",
        ),
        WorkflowPhase.CODING_FINALIZE: ("$tdd-finalizer",),
        WorkflowPhase.CONTENT_WRITE: ("$documentation-writer",),
        WorkflowPhase.CONTENT_REVIEW: ("$diataxis-judge", "$agent-policy-judge"),
        WorkflowPhase.CONTENT_FINALIZE: ("$documentation-finalizer",),
        WorkflowPhase.POST_PUBLICATION: ("$pipeline-performance-judge",),
    }
)

WORKFLOW_RECEIPTS: Final = MappingProxyType(
    {
        WorkflowLane.CODING: (
            "$architect",
            "$tdd-red",
            "$tdd-green",
            "$nonfunctional-review",
            "$tdd-judge",
            "$diataxis-judge",
            "$judge-quality",
            "$judge-test",
            "$judge-cicd",
            "$tdd-document",
            "$cli-syntax-judge",
            "$filesystem-structure-judge",
            "$agent-policy-judge",
            "$tdd-finalizer",
        ),
        WorkflowLane.CONTENT: (
            "$architect",
            "$documentation-writer",
            "$documentation-finalizer",
        ),
    }
)


def has_complete_workflow_receipt(
    record: LedgerRecord, *, requires_policy_review: bool = False
) -> bool:
    """Return whether durable workflow evidence exactly authorizes publication."""
    required = WORKFLOW_RECEIPTS.get(record.workflow_lane, ())
    workflow_receipts = tuple(
        receipt
        for receipt in record.receipts
        if receipt.phase is WorkflowPhase.WORKFLOW
    )
    if not workflow_receipts or workflow_receipts[-1].verdict != "complete":
        return False
    receipt_skills = tuple(
        receipt.partition(":")[0]
        for receipt in workflow_receipts[-1].detail.splitlines()
    )
    complete = (
        len(receipt_skills) == len(required)
        and set(receipt_skills) == set(required)
        and len(set(receipt_skills)) == len(receipt_skills)
    )
    if (
        not complete
        or not requires_policy_review
        or record.workflow_lane is not WorkflowLane.CODING
    ):
        return complete
    policy_receipt = next(
        receipt
        for receipt in workflow_receipts[-1].detail.splitlines()
        if receipt.partition(":")[0] == "$agent-policy-judge"
    )
    return "not applicable" not in policy_receipt.casefold()


REJECTION_ROUTES: Final = MappingProxyType(
    {
        WorkflowPhase.CODING_RED_DOCS: frozenset(
            {WorkflowPhase.DISCOVERY, WorkflowPhase.CODING_RED_DOCS}
        ),
        WorkflowPhase.CODING_GREEN: frozenset({WorkflowPhase.CODING_RED_DOCS}),
        WorkflowPhase.CODING_NONFUNCTIONAL: frozenset({WorkflowPhase.CODING_GREEN}),
        WorkflowPhase.CODING_VALIDATION: frozenset({WorkflowPhase.CODING_GREEN}),
        WorkflowPhase.CODING_FINAL_REVIEW: frozenset(
            {
                WorkflowPhase.DISCOVERY,
                WorkflowPhase.CODING_RED_DOCS,
                WorkflowPhase.CODING_GREEN,
            }
        ),
        WorkflowPhase.CODING_FINALIZE: frozenset(
            {
                WorkflowPhase.CODING_RED_DOCS,
                WorkflowPhase.CODING_GREEN,
                WorkflowPhase.CODING_VALIDATION,
                WorkflowPhase.CODING_FINAL_REVIEW,
            }
        ),
        WorkflowPhase.CONTENT_REVIEW: frozenset({WorkflowPhase.CONTENT_WRITE}),
        WorkflowPhase.CONTENT_FINALIZE: frozenset(
            {WorkflowPhase.CONTENT_WRITE, WorkflowPhase.CONTENT_REVIEW}
        ),
        WorkflowPhase.POST_PUBLICATION: frozenset({WorkflowPhase.POST_PUBLICATION}),
    }
)


@dataclass(frozen=True)
class PlannedOutcome:
    """One pure durable-record update."""

    record: LedgerRecord


def validate_transition(source: RunnerState, target: RunnerState) -> bool:
    """Return whether one lifecycle edge is declared."""
    return target in ALLOWED_TRANSITIONS[source]


def transition_record(
    record: LedgerRecord, target: RunnerState, **updates: object
) -> LedgerRecord:
    """Return one validated state transition with its accompanying updates."""
    if not validate_transition(record.state, target):
        raise ValueError(f"undeclared transition: {record.state} -> {target}")
    return record.model_copy(update={"state": target, **updates})


def plan_retry(
    record: LedgerRecord,
    *,
    now: datetime,
    retry_delay: timedelta = timedelta(minutes=20),
) -> PlannedOutcome:
    """Schedule a bounded retry or block after the retry budget is exhausted."""
    if record.retry_count >= 3:
        return PlannedOutcome(
            transition_record(
                record,
                RunnerState.BLOCKED,
                retry_at=None,
                error_code=ErrorCode.RETRY_EXHAUSTED,
                summary="Automatic retry budget exhausted.",
                next_action=(
                    "Resolve the blocker, then run agent-work loop retry --issue-iid "
                    f"{record.issue_iid} --apply."
                ),
            )
        )
    return PlannedOutcome(
        transition_record(
            record,
            RunnerState.RETRY_WAIT,
            prior_state=record.state,
            prior_phase=record.phase,
            retry_count=record.retry_count + 1,
            retry_at=now + retry_delay,
            error_code=None,
            summary="Transient failure; retry scheduled.",
            next_action="Resume when the retry deadline is due.",
        )
    )


def apply_worker_result(
    record: LedgerRecord, result: WorkerResultEnvelope, *, now: datetime
) -> PlannedOutcome:
    """Apply one validated worker result to a durable record."""
    if result.result_kind == "retryable":
        return plan_retry(record, now=now, retry_delay=timedelta())
    if result.result_kind == "needs_human":
        return PlannedOutcome(
            transition_record(
                record,
                RunnerState.BLOCKED,
                error_code=result.error_code,
                summary=result.redacted_summary or result.change_summary,
                next_action=result.next_action,
            )
        )
    if result.result_kind == "split_required":
        return PlannedOutcome(
            transition_record(
                record,
                RunnerState.DECOMPOSING,
                decomposition_plan=result.split_plan,
                decomposition_progress=(),
                merge_request_draft=None,
                merge_request_iid=None,
                merge_request_url=None,
                error_code=None,
                summary=result.redacted_summary or result.change_summary,
                next_action=result.next_action,
            )
        )
    required = (
        WORKFLOW_RECEIPTS.get(result.workflow_lane, ())
        if record.phase is WorkflowPhase.WORKFLOW
        else REQUIRED_RECEIPTS.get(record.phase, ())
    )
    receipt_skills = tuple(receipt.partition(":")[0] for receipt in result.receipts)
    has_receipts = (
        len(receipt_skills) == len(required)
        and set(receipt_skills) == set(required)
        and len(set(receipt_skills)) == len(receipt_skills)
    )
    if (
        result.completed_phase is not record.phase
        or not result.receipts
        or not has_receipts
        or (
            record.workflow_lane is not WorkflowLane.UNKNOWN
            and result.workflow_lane is not record.workflow_lane
        )
        or (
            record.phase is WorkflowPhase.DISCOVERY
            and result.workflow_lane is WorkflowLane.UNKNOWN
        )
    ):
        raise ValueError(
            "worker result receipt is not coherent with the recorded phase"
        )
    receipt = WorkflowReceiptRecord(
        phase=result.completed_phase,
        verdict=result.phase_status,
        detail="\n".join(result.receipts),
    )
    if result.phase_status == "rejected":
        route = result.rejection_route
        if route is None or route not in REJECTION_ROUTES.get(
            record.phase, frozenset()
        ):
            raise ValueError("worker rejection route is invalid for the recorded phase")
        return PlannedOutcome(
            transition_record(
                record,
                RunnerState.EXECUTING,
                phase=route,
                receipts=(*record.receipts, receipt),
                summary=result.change_summary,
                next_action=result.next_action,
                error_code=result.error_code,
            )
        )
    next_phase = next_workflow_phase(record.phase, result.workflow_lane)
    merge_request_draft = (
        record.merge_request_draft
        if record.merge_request_draft is not None
        and record.merge_request_draft.decision_review is not None
        else result.merge_request_draft or record.merge_request_draft
    )
    if record.pending_feedback is not None:
        # Existing-MR feedback may change code, never review metadata.
        merge_request_draft = None
    if (
        record.work_kind
        not in {WorkKind.EXISTING_MR_REMEDIATION, WorkKind.MERGE_CONFLICT_REPAIR}
        and record.pending_feedback is None
        and next_phase is WorkflowPhase.READY_TO_PUBLISH
        and merge_request_draft is None
    ):
        raise ValueError("worker result lacks required publication metadata")
    next_state = (
        RunnerState.AWAITING_HUMAN
        if record.phase is WorkflowPhase.POST_PUBLICATION
        else (
            RunnerState.PUBLISHING
            if next_phase is WorkflowPhase.READY_TO_PUBLISH
            else RunnerState.EXECUTING
        )
    )
    return PlannedOutcome(
        transition_record(
            record,
            next_state,
            phase=next_phase,
            workflow_lane=result.workflow_lane,
            receipts=(*record.receipts, receipt),
            merge_request_draft=merge_request_draft,
            publication_sha=(
                None
                if next_phase is WorkflowPhase.READY_TO_PUBLISH
                else record.publication_sha
            ),
            summary=result.change_summary,
            next_action=result.next_action,
            error_code=None,
        )
    )
