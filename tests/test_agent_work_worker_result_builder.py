# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Contracts for controller-built live worker results."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.models import LedgerRecord, MergeRequestDraft
from agent_work.red_checkpoint import RedCheckpointEvidence, RedCheckpointRequest
from agent_work.state_machine import apply_worker_result
from agent_work.worker import (
    WorkerProtocolError,
    WorkerResultBrokerRequest,
    WorkerResultBuildInput,
    build_worker_prompt,
    build_worker_result,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _record(tmp_path: Path, lane: WorkflowLane) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=81,
        revision=7,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        workflow_lane=lane,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-81",
        worktree=tmp_path / "checkout",
        source_sha="a" * 40,
    )


def _draft() -> MergeRequestDraft:
    return MergeRequestDraft(
        title="Complete issue 81",
        description="Validated and ready for human review.",
    )


def _checkpoint(record: LedgerRecord) -> RedCheckpointEvidence:
    test_path = "tests/test_agent_work_worker_result_builder.py"
    assert record.source_sha is not None
    return RedCheckpointEvidence(
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        source_branch=record.source_branch,
        base_sha=record.source_sha,
        tree_oid="b" * 40,
        ref=(f"refs/agent-work/red-checkpoints/{record.issue_iid}/{record.source_sha}"),
        test_paths=(test_path,),
        argv=("uv", "run", "pytest", test_path),
        exit_code=1,
        expected_failure="expected Red failure",
        output_sha256=hashlib.sha256(b"expected Red failure").hexdigest(),
    )


def test_receiptless_content_result_builds_and_applies(tmp_path: Path) -> None:
    """The program moves a receiptless content workflow to publication."""
    record = _record(tmp_path, WorkflowLane.CONTENT)
    result = build_worker_result(
        record,
        WorkerResultBuildInput.model_validate(
            {
                "workflow_lane": "content",
                "result_kind": "ready_to_publish",
                "change_summary": "Update the operator documentation.",
                "next_action": "Publish the reviewed documentation.",
                "merge_request_draft": _draft(),
            }
        ),
        changed_paths=("docs/reference/serial-gitlab-agent-loop.md",),
        red_checkpoint=None,
    )

    outcome = apply_worker_result(record, result, now=NOW).record

    assert result.model_dump(exclude_none=True).get("receipts") is None
    assert result.model_dump(exclude_none=True).get("audit_summary") is None
    assert outcome.state is RunnerState.PUBLISHING
    assert outcome.phase is WorkflowPhase.READY_TO_PUBLISH
    assert outcome.receipts == ()


def test_receiptless_coding_result_requires_and_applies_exact_red_checkpoint(
    tmp_path: Path,
) -> None:
    """Coding publication is bound to controller-retained Red evidence and paths."""
    record = _record(tmp_path, WorkflowLane.CODING)
    checkpoint = _checkpoint(record)
    result = build_worker_result(
        record,
        WorkerResultBuildInput(
            workflow_lane=WorkflowLane.CODING,
            result_kind="ready_to_publish",
            change_summary="Implement the broker boundary.",
            next_action="Publish for human review.",
            merge_request_draft=_draft(),
        ),
        changed_paths=(
            "src/agent_work/worker.py",
            "tests/test_agent_work_worker_result_builder.py",
        ),
        red_checkpoint=checkpoint,
    )

    outcome = apply_worker_result(record, result, now=NOW).record

    assert outcome.state is RunnerState.PUBLISHING
    assert outcome.phase is WorkflowPhase.READY_TO_PUBLISH
    assert outcome.receipts == ()


@pytest.mark.parametrize(
    ("checkpoint", "changed_paths"),
    (
        (None, ("tests/test_agent_work_worker_result_builder.py",)),
        ("exact", ("src/agent_work/worker.py",)),
        ("wrong-issue", ("tests/test_agent_work_worker_result_builder.py",)),
        ("wrong-branch", ("tests/test_agent_work_worker_result_builder.py",)),
        ("wrong-sha", ("tests/test_agent_work_worker_result_builder.py",)),
    ),
)
def test_coding_result_rejects_missing_or_mismatched_controller_checkpoint(
    tmp_path: Path,
    checkpoint: str | None,
    changed_paths: tuple[str, ...],
) -> None:
    record = _record(tmp_path, WorkflowLane.CODING)
    evidence = _checkpoint(record)
    if checkpoint == "wrong-issue":
        evidence = evidence.model_copy(update={"issue_iid": 99})
    elif checkpoint == "wrong-branch":
        evidence = evidence.model_copy(update={"source_branch": "agent/issue-99"})
    elif checkpoint == "wrong-sha":
        evidence = evidence.model_copy(update={"base_sha": "f" * 40})

    with pytest.raises(WorkerProtocolError, match="red_checkpoint"):
        build_worker_result(
            record,
            WorkerResultBuildInput(
                workflow_lane=WorkflowLane.CODING,
                result_kind="ready_to_publish",
                change_summary="Implementation complete.",
                next_action="Publish for human review.",
                merge_request_draft=_draft(),
            ),
            changed_paths=changed_paths,
            red_checkpoint=None if checkpoint is None else evidence,
        )


def test_ready_to_publish_rejects_missing_controller_checked_draft(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path, WorkflowLane.CONTENT)

    with pytest.raises(WorkerProtocolError, match="transition"):
        build_worker_result(
            record,
            WorkerResultBuildInput(
                workflow_lane=WorkflowLane.CONTENT,
                result_kind="ready_to_publish",
                change_summary="Documentation complete.",
                next_action="Publish for human review.",
            ),
            changed_paths=("docs/reference/serial-gitlab-agent-loop.md",),
            red_checkpoint=None,
        )


def test_red_checkpoint_ready_builds_without_model_authorization(
    tmp_path: Path,
) -> None:
    """The producer can request the existing trusted checkpoint transaction."""
    record = _record(tmp_path, WorkflowLane.UNKNOWN)
    request = RedCheckpointRequest(
        argv=(
            "uv",
            "run",
            "pytest",
            "tests/test_agent_work_worker_result_builder.py",
        ),
        expected_failure="expected Red failure",
        test_paths=("tests/test_agent_work_worker_result_builder.py",),
    )
    result = build_worker_result(
        record,
        WorkerResultBuildInput(
            workflow_lane=WorkflowLane.CODING,
            result_kind="red_checkpoint_ready",
            change_summary="Red is ready for controller capture.",
            next_action="Create the trusted checkpoint.",
            red_checkpoint_request=request,
        ),
        changed_paths=request.test_paths,
        red_checkpoint=None,
    )

    assert result.result_kind == "red_checkpoint_ready"
    assert result.red_checkpoint_request == request
    with pytest.raises(ValueError, match="fulfilled by the controller"):
        apply_worker_result(record, result, now=NOW)


@pytest.mark.parametrize("result_kind", ("ready_to_publish", "suggest_close"))
def test_post_publication_results_apply_without_model_receipts(
    tmp_path: Path, result_kind: str
) -> None:
    record = _record(tmp_path, WorkflowLane.CODING).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    values: dict[str, object] = {
        "workflow_lane": WorkflowLane.CODING,
        "result_kind": result_kind,
        "change_summary": "The exact pipeline assessment is complete.",
        "next_action": "Await human review.",
    }
    if result_kind == "suggest_close":
        values["suggest_close_evidence"] = {
            "reason": "already-satisfied",
            "issue_iid": record.issue_iid,
            "evidence": ("Exact merged source SHA contains the requested change.",),
        }
    result = build_worker_result(
        record,
        WorkerResultBuildInput.model_validate(values),
        changed_paths=(),
        red_checkpoint=None,
    )

    outcome = apply_worker_result(record, result, now=NOW).record

    expected = (
        RunnerState.SUGGEST_CLOSE
        if result_kind == "suggest_close"
        else RunnerState.AWAITING_HUMAN
    )
    assert outcome.state is expected


def test_builder_input_rejects_model_authored_receipts_and_audit_summary() -> None:
    """Program authorization cannot accept model-authored workflow evidence."""
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        WorkerResultBuildInput.model_validate(
            {
                "workflow_lane": "coding",
                "result_kind": "needs_human",
                "change_summary": "A human decision is required.",
                "next_action": "Await the reviewer decision.",
                "receipts": ["$architect: approved"],
                "audit_summary": {"capacity": {"required": 12}},
            }
        )


def test_worker_prompt_routes_the_live_producer_through_the_private_broker() -> None:
    """The producer invokes the trusted builder while its broker is still live."""
    request = WorkerResultBrokerRequest(
        nonce="0123456789abcdef" * 2,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )
    prompt = build_worker_prompt(
        {"phase": "workflow"},
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=600,
        worker_result_broker=request,
        worker_result_builder=Path("/controller/bin/agent-work-from-checkout"),
    )
    assert "--broker-socket" in prompt and "--broker-nonce" in prompt
    assert "/controller/bin/agent-work-from-checkout" in prompt
    assert "private scratch" in prompt and "one corrected request" in prompt
    for forbidden in ("audit_summary",):
        assert forbidden not in prompt
