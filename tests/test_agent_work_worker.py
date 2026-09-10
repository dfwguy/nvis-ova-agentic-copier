"""Behavior contracts for the versioned worker result protocol."""

from __future__ import annotations

import json

import pytest

from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.worker import WorkerProtocolError, parse_worker_result


def _envelope(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "protocol_version": 1,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": 3,
        "expected_state": RunnerState.EXECUTING.value,
        "expected_phase": WorkflowPhase.CODING_GREEN.value,
        "workflow_lane": WorkflowLane.CODING.value,
        "result_kind": "ready_to_publish",
        "phase_status": "complete",
        "completed_phase": WorkflowPhase.CODING_GREEN.value,
        "receipts": ["green suite"],
        "change_summary": "Implementation is green.",
        "next_action": "Run nonfunctional review.",
    }
    payload.update(updates)
    return payload


def test_parse_worker_result_accepts_one_exact_versioned_envelope() -> None:
    output = (
        "progress\n<agent-work-result>"
        + json.dumps(_envelope())
        + "</agent-work-result>\n"
    )

    result = parse_worker_result(output)

    assert result.issue_iid == 12
    assert result.protocol_version == 1
    assert result.result_kind == "ready_to_publish"


@pytest.mark.parametrize(
    "output",
    [
        "no envelope",
        "<agent-work-result>{}</agent-work-result>",
        "<agent-work-result>{}</agent-work-result><agent-work-result>{}</agent-work-result>",
    ],
)
def test_worker_protocol_fails_closed_for_missing_invalid_or_multiple_results(
    output: str,
) -> None:
    with pytest.raises(WorkerProtocolError):
        parse_worker_result(output)


def test_worker_protocol_rejects_identity_mismatch() -> None:
    output = (
        "<agent-work-result>"
        + json.dumps(_envelope(issue_iid=13))
        + "</agent-work-result>"
    )

    with pytest.raises(WorkerProtocolError, match="identity"):
        parse_worker_result(
            output,
            expected_project_id=310967,
            expected_issue_iid=12,
            expected_revision=3,
        )
