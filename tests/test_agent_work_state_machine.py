# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for retry and worker-result transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from agent_work.domain import (
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.models import LedgerRecord, MergeRequestDraft, WorkerResultEnvelope
from agent_work.state_machine import apply_worker_result, plan_retry

NOW = datetime(2026, 9, 9, tzinfo=UTC)


def _record(*, retry_count: int = 0) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.CODING_GREEN,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=Path("/state/worktrees/12"),
        retry_count=retry_count,
    )


def _result(
    kind: Literal["ready_to_publish", "retryable", "needs_human"],
) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        project_id=310967,
        issue_iid=12,
        ledger_revision=0,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.CODING_GREEN,
        workflow_lane=WorkflowLane.CODING,
        result_kind=kind,
        phase_status="complete",
        completed_phase=WorkflowPhase.CODING_GREEN,
        receipts=("$tdd-green: green suite",),
        change_summary="Done.",
        next_action="Continue.",
    )


def test_ready_to_publish_advances_a_completed_green_phase() -> None:
    outcome = apply_worker_result(_record(), _result("ready_to_publish"), now=NOW)

    assert outcome.record.state is RunnerState.EXECUTING
    assert outcome.record.phase is WorkflowPhase.CODING_NONFUNCTIONAL


def test_complete_coding_workflow_advances_directly_to_publication() -> None:
    workflow = WorkflowPhase("workflow")
    record = _record().model_copy(
        update={"phase": workflow, "workflow_lane": WorkflowLane.UNKNOWN}
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": workflow,
            "completed_phase": workflow,
            "workflow_lane": WorkflowLane.CODING,
            "receipts": (
                "$architect: approved",
                "$tdd-red: red confirmed",
                "$tdd-green: green confirmed and findings repaired",
                "$nonfunctional-review: approved",
                "$tdd-judge: approved",
                "$diataxis-judge: approved",
                "$judge-quality: approved",
                "$judge-test: approved",
                "$judge-cicd: approved",
                "$tdd-document: approved",
                "$cli-syntax-judge: not applicable — no CLI change",
                "$filesystem-structure-judge: approved",
                "$agent-policy-judge: not applicable — no agent policy change",
                "$tdd-finalizer: approved",
            ),
            "merge_request_draft": MergeRequestDraft(
                title="Complete issue 12", description="Validated.\n\nCloses #12"
            ),
        }
    )

    outcome = apply_worker_result(record, result, now=NOW)

    assert outcome.record.state is RunnerState.PUBLISHING
    assert outcome.record.phase is WorkflowPhase.READY_TO_PUBLISH


@pytest.mark.parametrize(
    "receipts",
    [
        ("$architect: approved",),
        (
            "$architect: approved",
            "$architect: duplicate",
            "$tdd-red: red confirmed",
        ),
        (
            "$architect: approved",
            "$documentation-writer: complete",
            "$documentation-finalizer: approved",
        ),
    ],
)
def test_incomplete_duplicate_or_wrong_lane_workflow_receipts_fail_closed(
    receipts: tuple[str, ...],
) -> None:
    workflow = WorkflowPhase("workflow")
    record = _record().model_copy(
        update={"phase": workflow, "workflow_lane": WorkflowLane.UNKNOWN}
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": workflow,
            "completed_phase": workflow,
            "workflow_lane": WorkflowLane.CODING,
            "receipts": receipts,
            "merge_request_draft": MergeRequestDraft(
                title="Complete issue 12", description="Validated.\n\nCloses #12"
            ),
        }
    )

    with pytest.raises(ValueError, match="receipt"):
        apply_worker_result(record, result, now=NOW)


def test_retryable_result_schedules_twenty_minutes_after_now() -> None:
    outcome = apply_worker_result(_record(), _result("retryable"), now=NOW)

    assert outcome.record.state is RunnerState.RETRY_WAIT
    assert outcome.record.retry_count == 1
    assert outcome.record.retry_at == NOW + timedelta(minutes=20)


def test_fourth_retryable_result_blocks_instead_of_failing() -> None:
    outcome = plan_retry(_record(retry_count=3), now=NOW)

    assert outcome.record.state is RunnerState.BLOCKED
    assert outcome.record.error_code is ErrorCode.RETRY_EXHAUSTED
    assert "loop retry" in outcome.record.next_action
    assert "label" not in outcome.record.next_action


def test_needs_human_result_becomes_a_documented_block() -> None:
    result = _result("needs_human").model_copy(
        update={
            "error_code": ErrorCode.PROTECTED_POLICY_MANUAL,
            "redacted_summary": "Policy edit needs a directed lane.",
            "next_action": "Run the policy workflow manually.",
        }
    )

    outcome = apply_worker_result(_record(), result, now=NOW)

    assert outcome.record.state is RunnerState.BLOCKED
    assert outcome.record.next_action == "Run the policy workflow manually."


def test_successful_postpublication_repair_reenters_downstream_validation() -> None:
    record = _record().model_copy(
        update={
            "state": RunnerState.REPAIR,
            "phase": WorkflowPhase.CODING_GREEN,
            "merge_request_draft": MergeRequestDraft(
                title="Original", description="Original description"
            ),
        }
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_state": RunnerState.REPAIR,
            "expected_phase": WorkflowPhase.CODING_GREEN,
            "completed_phase": WorkflowPhase.CODING_GREEN,
        }
    )

    outcome = apply_worker_result(record, result, now=NOW)

    assert outcome.record.state is RunnerState.EXECUTING
    assert outcome.record.phase is WorkflowPhase.CODING_NONFUNCTIONAL
    assert outcome.record.merge_request_draft == record.merge_request_draft


@pytest.mark.parametrize(
    "result",
    [
        _result("ready_to_publish").model_copy(update={"phase_status": "rejected"}),
        _result("ready_to_publish").model_copy(
            update={"completed_phase": WorkflowPhase.CODING_VALIDATION}
        ),
        _result("ready_to_publish").model_copy(
            update={"workflow_lane": WorkflowLane.CONTENT}
        ),
        _result("ready_to_publish").model_copy(update={"receipts": ()}),
    ],
)
def test_incoherent_successful_worker_results_fail_closed(
    result: WorkerResultEnvelope,
) -> None:
    with pytest.raises(ValueError):
        apply_worker_result(_record(), result, now=NOW)


@pytest.mark.parametrize(
    "receipts",
    [
        ("looks green",),
        ("$tdd-green: green", "unrelated receipt"),
        ("$tdd-green: green", "$tdd-green: duplicate"),
    ],
)
def test_only_one_exact_receipt_per_required_skill_can_advance_a_phase(
    receipts: tuple[str, ...],
) -> None:
    result = _result("ready_to_publish").model_copy(update={"receipts": receipts})

    with pytest.raises(ValueError, match="receipt"):
        apply_worker_result(_record(), result, now=NOW)


def test_retry_planning_rejects_an_undeclared_transition() -> None:
    record = _record().model_copy(update={"state": RunnerState.FAILED})

    with pytest.raises(ValueError, match="transition"):
        plan_retry(record, now=NOW)


@pytest.mark.parametrize(
    ("phase", "route", "receipts"),
    [
        (
            WorkflowPhase.CODING_NONFUNCTIONAL,
            WorkflowPhase.CODING_GREEN,
            ("$nonfunctional-review",),
        ),
        (
            WorkflowPhase.CODING_VALIDATION,
            WorkflowPhase.CODING_GREEN,
            ("$tdd-judge", "$diataxis-judge"),
        ),
        (
            WorkflowPhase.CODING_FINAL_REVIEW,
            WorkflowPhase.CODING_RED_DOCS,
            (
                "$judge-quality",
                "$judge-test",
                "$judge-cicd",
                "$tdd-document",
                "$cli-syntax-judge: not applicable — no CLI change",
                "$filesystem-structure-judge",
            ),
        ),
        (
            WorkflowPhase.CODING_FINAL_REVIEW,
            WorkflowPhase.CODING_GREEN,
            (
                "$judge-quality",
                "$judge-test",
                "$judge-cicd",
                "$tdd-document",
                "$cli-syntax-judge",
                "$filesystem-structure-judge",
            ),
        ),
        (
            WorkflowPhase.CONTENT_REVIEW,
            WorkflowPhase.CONTENT_WRITE,
            ("$diataxis-judge", "$agent-policy-judge"),
        ),
        (
            WorkflowPhase.POST_PUBLICATION,
            WorkflowPhase.CODING_GREEN,
            ("$pipeline-performance-judge", "$findings-triage"),
        ),
    ],
)
def test_rejected_phase_routes_to_its_recorded_owner_without_retrying(
    phase: WorkflowPhase, route: WorkflowPhase, receipts: tuple[str, ...]
) -> None:
    record = _record().model_copy(update={"phase": phase})
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": phase,
            "completed_phase": phase,
            "phase_status": "rejected",
            "rejection_route": route,
            "receipts": receipts,
        }
    )

    outcome = apply_worker_result(record, result, now=NOW)

    assert outcome.record.state is RunnerState.EXECUTING
    assert outcome.record.phase is route
    assert outcome.record.retry_count == 0


def test_discovery_must_establish_a_known_workflow_lane() -> None:
    record = _record().model_copy(
        update={
            "phase": WorkflowPhase.DISCOVERY,
            "workflow_lane": WorkflowLane.UNKNOWN,
        }
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": WorkflowPhase.DISCOVERY,
            "completed_phase": WorkflowPhase.DISCOVERY,
            "workflow_lane": WorkflowLane.UNKNOWN,
            "receipts": ("$architect: classified",),
        }
    )

    with pytest.raises(ValueError, match="receipt"):
        apply_worker_result(record, result, now=NOW)


@pytest.mark.parametrize(
    ("lane", "phase", "receipt"),
    [
        (
            WorkflowLane.CODING,
            WorkflowPhase.CODING_FINALIZE,
            "$tdd-finalizer: approved",
        ),
        (
            WorkflowLane.CONTENT,
            WorkflowPhase.CONTENT_FINALIZE,
            "$documentation-finalizer: approved",
        ),
    ],
)
def test_ordinary_finalizer_cannot_publish_without_merge_request_metadata(
    lane: WorkflowLane, phase: WorkflowPhase, receipt: str
) -> None:
    record = _record().model_copy(update={"workflow_lane": lane, "phase": phase})
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": phase,
            "completed_phase": phase,
            "workflow_lane": lane,
            "receipts": (receipt,),
        }
    )

    with pytest.raises(ValueError, match="publication metadata"):
        apply_worker_result(record, result, now=NOW)
