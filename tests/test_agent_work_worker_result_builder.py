# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only worker-result builder."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_work.cli import main
from agent_work.domain import (
    RepairAttemptEvidence,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.ledger import LedgerCorrupt
from agent_work.models import (
    AgentWorkConfig,
    AuditCapacity,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
)
from agent_work.repository import CommandResult
from agent_work.runtime import build_worker_result_dependencies
from agent_work.state_machine import (
    REQUIRED_RECEIPTS,
    WORKFLOW_RECEIPTS,
    apply_worker_result,
    has_complete_workflow_receipt,
    plan_retry,
)
from agent_work.worker import (
    WorkerProtocolError,
    WorkerResultBuildInput,
    build_worker_prompt,
    build_worker_result,
    format_worker_result_diagnostic,
    parse_worker_result_build_input,
    validate_worker_result,
)


def _record(tmp_path: Path) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=81,
        revision=7,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-81",
        worktree=tmp_path / "retained-worktree",
        source_sha="a" * 40,
    )


def _completed_input() -> WorkerResultBuildInput:
    receipts = tuple(
        f"{skill}: approved; uv run pytest tests/test_agent_work_worker_result_builder.py"
        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
    )
    return WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": receipts,
            "change_summary": "Added a read-only worker result builder.",
            "next_action": "Publish the reviewed change.",
            "merge_request_draft": {
                "title": "Add worker result builder",
                "description": "Summary and test evidence for human review.",
            },
            "audit_summary": {
                "capacity": {
                    "required": 12,
                    "available": 16,
                    "shortfall": 0,
                    "decision": "run",
                },
                **_topology_audit(),
                "tests": [
                    "uv run pytest tests/test_agent_work_worker_result_builder.py"
                ],
                "findings": [],
                "corrections": [],
            },
        }
    )


_CODING_RECEIPTS_WITH_POLICY_WRITER = (
    "$architect",
    "$tdd-red",
    "$diataxis-writer",
    "$agent-policy-writer",
    "$tdd-green",
    "$tdd-judge",
    "$nonfunctional-review",
    "$diataxis-judge",
    "$judge-quality",
    "$judge-test",
    "$judge-cicd",
    "$tdd-document",
    "$cli-syntax-judge",
    "$filesystem-structure-judge",
    "$agent-policy-judge",
    "$license-checker",
    "$tdd-finalizer",
)


def _completed_coding_input_with_policy_writer() -> WorkerResultBuildInput:
    """Build the complete protocol-v2 coding receipt input."""
    return _completed_input().model_copy(
        update={
            "receipts": tuple(
                f"{skill}: approved; uv run pytest tests/test_agent_work_worker_result_builder.py"
                for skill in _CODING_RECEIPTS_WITH_POLICY_WRITER
            )
        }
    )


def _topology_audit() -> dict[str, Any]:
    """Build the complete current-launch topology evidence for coding work."""
    role_details = {
        "$architect": ("main", "1", "most_capable"),
        "$tdd-red": ("$architect", "2a", "balanced"),
        "$diataxis-writer": ("$architect", "2b", "balanced"),
        "$agent-policy-writer": ("$architect", "2c", "balanced"),
        "$tdd-green": ("$architect", "3", "balanced"),
        "$tdd-judge": ("$tdd-green", "4", "balanced"),
        "$nonfunctional-review": ("$tdd-judge", "4a", "fast"),
        "$diataxis-judge": ("$tdd-green", "5b", "fast"),
        "$judge-quality": ("$tdd-green", "5c", "most_capable"),
        "$judge-test": ("$tdd-green", "5d", "balanced"),
        "$judge-cicd": ("$tdd-green", "5e", "balanced"),
        "$tdd-document": ("$tdd-green", "5f", "balanced"),
        "$cli-syntax-judge": ("$tdd-green", "5g", "fast"),
        "$filesystem-structure-judge": ("$tdd-green", "5h", "fast"),
        "$agent-policy-judge": ("$tdd-green", "5i", "fast"),
        "$license-checker": ("$tdd-green", "5j", "fast"),
        "$tdd-finalizer": ("$architect", "6", "fast"),
    }
    return {
        "topology": {
            "widest_live_row": [
                "main",
                "$architect",
                "$tdd-green",
                "$diataxis-judge",
                "$judge-quality",
                "$judge-test",
                "$judge-cicd",
                "$tdd-document",
                "$cli-syntax-judge",
                "$filesystem-structure-judge",
                "$agent-policy-judge",
                "$license-checker",
            ],
            "required_live_slots": 12,
            "available_live_slots": 16,
            "roles": [
                {
                    "skill": skill,
                    "owner": owner,
                    "phase": phase,
                    "tier": tier,
                    **({"reuse_from": "$tdd-judge"} if skill == "$judge-test" else {}),
                }
                for skill, (owner, phase, tier) in role_details.items()
            ],
            "owner_headroom": [
                {
                    "owner": "main",
                    "direct_children_required": 1,
                    "direct_children_headroom": 16,
                    "descendants_required": 16,
                    "descendants_headroom": 16,
                },
                {
                    "owner": "$architect",
                    "direct_children_required": 5,
                    "direct_children_headroom": 16,
                    "descendants_required": 15,
                    "descendants_headroom": 16,
                },
                {
                    "owner": "$tdd-green",
                    "direct_children_required": 9,
                    "direct_children_headroom": 16,
                    "descendants_required": 10,
                    "descendants_headroom": 16,
                },
                {
                    "owner": "$tdd-judge",
                    "direct_children_required": 1,
                    "direct_children_headroom": 16,
                    "descendants_required": 1,
                    "descendants_headroom": 16,
                },
            ],
        },
        "retained_objects": {
            "ceiling": 16,
            "current": 0,
            "required_fresh_handles": 16,
            "fresh_headroom": 0,
        },
    }


def test_builder_requires_complete_current_topology_and_retained_object_evidence(
    tmp_path: Path,
) -> None:
    """A new successful workflow records validated topology, not scalar capacity."""
    payload = _completed_coding_input_with_policy_writer().model_dump(mode="json")
    audit = payload["audit_summary"]
    assert isinstance(audit, dict)
    audit.update(_topology_audit())
    build_input = WorkerResultBuildInput.model_validate(payload)

    result = build_worker_result(_record(tmp_path), build_input, changed_paths=())

    assert result.audit_summary is not None
    summary = result.audit_summary.model_dump(mode="json", exclude_none=True)
    assert summary["topology"] == _topology_audit()["topology"]
    assert summary["retained_objects"] == _topology_audit()["retained_objects"]


def test_builder_rejects_new_success_with_legacy_scalar_only_capacity_audit(
    tmp_path: Path,
) -> None:
    """Recovery-compatible scalar history cannot authorize a fresh workflow result."""
    completed = _completed_coding_input_with_policy_writer()
    assert completed.audit_summary is not None
    legacy = completed.model_copy(
        update={
            "audit_summary": completed.audit_summary.model_copy(
                update={"topology": None, "retained_objects": None}
            )
        }
    )
    with pytest.raises(WorkerProtocolError, match=r"topology|retained"):
        build_worker_result(
            _record(tmp_path),
            legacy,
            changed_paths=(),
        )


@pytest.mark.parametrize(
    ("audit_update", "expected_diagnostic"),
    (
        (
            {
                "retained_objects": {
                    "ceiling": 16,
                    "current": 1,
                    "required_fresh_handles": 16,
                    "fresh_headroom": 0,
                }
            },
            "retained_objects",
        ),
        (
            {
                "topology": {
                    **_topology_audit()["topology"],
                    "owner_headroom": [
                        {
                            **_topology_audit()["topology"]["owner_headroom"][0],
                            "descendants_headroom": 15,
                        },
                        *_topology_audit()["topology"]["owner_headroom"][1:],
                    ],
                }
            },
            "owner_headroom",
        ),
        (
            {
                "topology": {
                    **_topology_audit()["topology"],
                    "roles": [
                        *(_topology_audit()["topology"]["roles"][:9]),
                        {
                            **_topology_audit()["topology"]["roles"][9],
                            "reuse_from": "$tdd-red",
                        },
                        *(_topology_audit()["topology"]["roles"][10:]),
                    ],
                }
            },
            "reuse_from",
        ),
        (
            {
                "topology": {
                    **_topology_audit()["topology"],
                    "roles": [
                        *(_topology_audit()["topology"]["roles"][:10]),
                        {
                            **_topology_audit()["topology"]["roles"][11],
                            "reuse_from": "$tdd-judge",
                        },
                        *(_topology_audit()["topology"]["roles"][12:]),
                    ],
                }
            },
            "reuse_from",
        ),
    ),
    ids=(
        "global-object-shortfall",
        "owner-headroom-shortfall",
        "unsafe-cross-owner-reuse",
        "unsafe-overlapping-reuse",
    ),
)
def test_builder_rejects_incoherent_topology_admission_evidence(
    audit_update: dict[str, object], expected_diagnostic: str
) -> None:
    """Live-slot success cannot mask global, owner, or reuse unsafety."""
    payload = _completed_coding_input_with_policy_writer().model_dump(mode="json")
    audit = payload["audit_summary"]
    assert isinstance(audit, dict)
    audit.update(_topology_audit())
    audit.update(audit_update)

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    assert expected_diagnostic in format_worker_result_diagnostic(captured.value)


def test_builder_rejects_valid_global_arithmetic_with_too_few_fresh_handles(
    tmp_path: Path,
) -> None:
    """Global arithmetic cannot replace the topology's fresh-handle requirement."""
    payload = _completed_coding_input_with_policy_writer().model_dump(mode="json")
    audit = payload["audit_summary"]
    assert isinstance(audit, dict)
    audit["retained_objects"] = {
        "ceiling": 16,
        "current": 0,
        "required_fresh_handles": 15,
        "fresh_headroom": 1,
    }

    build_input = parse_worker_result_build_input(json.dumps(payload))

    with pytest.raises(WorkerProtocolError, match="fresh handles"):
        build_worker_result(_record(tmp_path), build_input, changed_paths=())


@pytest.mark.parametrize("owner", ("$missing-owner", "$tdd-green"))
def test_builder_rejects_malformed_or_cyclic_topology_ownership(owner: str) -> None:
    """Untrusted ownership chains fail closed before descendant traversal."""
    payload = _completed_coding_input_with_policy_writer().model_dump(mode="json")
    audit = payload["audit_summary"]
    assert isinstance(audit, dict)
    topology = audit["topology"]
    assert isinstance(topology, dict)
    roles = topology["roles"]
    assert isinstance(roles, list)
    roles[0] = {**roles[0], "owner": owner}

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    diagnostic = format_worker_result_diagnostic(captured.value)
    assert (
        diagnostic == "builder_input.audit_summary.topology: invalid type/shape/value"
    )
    assert len(diagnostic.encode()) <= 2048


def test_builder_derives_trusted_identity_and_validates_completed_workflow(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    result = build_worker_result(
        record,
        _completed_input(),
        changed_paths=("src/agent_work/worker.py", "tests/test_agent_work_worker.py"),
    )

    assert result.protocol_version == 2
    assert (result.project_id, result.issue_iid, result.ledger_revision) == (
        310967,
        81,
        7,
    )
    assert result.expected_state is RunnerState.EXECUTING
    assert result.phase_status == "complete"
    assert result.completed_phase is WorkflowPhase.WORKFLOW
    assert result.audit_summary is not None
    assert result.audit_summary.changed_paths == (
        "src/agent_work/worker.py",
        "tests/test_agent_work_worker.py",
    )
    assert result.audit_summary.role_receipts == result.receipts
    assert "$tdd-judge: approved" in "\n".join(result.audit_summary.reviewers)
    assert all(
        reviewer in "\n".join(result.receipts)
        for reviewer in result.audit_summary.reviewers
    )
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 14, tzinfo=UTC)
    ).record
    assert transitioned.state is RunnerState.PUBLISHING
    assert transitioned.phase is WorkflowPhase.READY_TO_PUBLISH


def test_builder_accepts_complete_coding_receipts_with_the_policy_writer(
    tmp_path: Path,
) -> None:
    """Protocol-v2 accepts the complete ordered coding receipt contract."""
    result = build_worker_result(
        _record(tmp_path),
        _completed_coding_input_with_policy_writer(),
        changed_paths=(),
    )

    assert result.receipts == tuple(
        f"{skill}: approved; uv run pytest tests/test_agent_work_worker_result_builder.py"
        for skill in _CODING_RECEIPTS_WITH_POLICY_WRITER
    )


@pytest.mark.parametrize(
    "receipts",
    (
        tuple(
            skill
            for skill in _CODING_RECEIPTS_WITH_POLICY_WRITER
            if skill != "$agent-policy-writer"
        ),
        (
            "$architect",
            "$tdd-red",
            "$diataxis-writer",
            "$agent-policy-judge",
            *_CODING_RECEIPTS_WITH_POLICY_WRITER[4:],
        ),
    ),
    ids=("missing-policy-writer", "wrong-policy-writer"),
)
def test_builder_rejects_coding_receipts_without_the_policy_writer(
    tmp_path: Path, receipts: tuple[str, ...]
) -> None:
    """Protocol-v2 rejects a missing or incorrect phase-two policy receipt."""
    build_input = _completed_coding_input_with_policy_writer().model_copy(
        update={
            "receipts": tuple(
                f"{skill}: approved; uv run pytest tests/test_agent_work_worker_result_builder.py"
                for skill in receipts
            )
        }
    )

    with pytest.raises(WorkerProtocolError, match=r"receipts:.*required"):
        build_worker_result(_record(tmp_path), build_input, changed_paths=())


def test_legacy_coding_red_docs_receipts_exclude_the_policy_writer() -> None:
    """Recorded legacy phase evidence retains its original completion contract."""
    assert REQUIRED_RECEIPTS[WorkflowPhase.CODING_RED_DOCS] == (
        "$tdd-red",
        "$diataxis-writer",
    )


@pytest.mark.parametrize(
    ("lane", "wrong_required"),
    (("coding", 11), ("content", 4)),
)
def test_builder_rejects_capacity_required_that_disagrees_with_lane(
    lane: str, wrong_required: int
) -> None:
    payload = {
        "workflow_lane": lane,
        "result_kind": "ready_to_publish",
        "change_summary": "Completed the workflow.",
        "next_action": "Publish for human review.",
        "audit_summary": {
            "capacity": {
                "required": wrong_required,
                "available": 16,
                "shortfall": 0,
                "decision": "run",
            },
            "tests": ["uv run pytest"],
            "findings": [],
            "corrections": [],
        },
    }

    with pytest.raises(WorkerProtocolError):
        parse_worker_result_build_input(json.dumps(payload))


def test_builder_produces_a_structured_human_only_close_suggestion(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "suggest_close",
            "change_summary": "The work is already satisfied by the exact merged change.",
            "next_action": "A human may review the evidence and close the issue.",
            "suggest_close_evidence": {
                "reason": "already-satisfied",
                "issue_iid": 81,
                "evidence": [
                    "https://gitlab.example.test/group/project/-/merge_requests/9"
                ],
            },
        }
    )

    result = build_worker_result(record, build_input, changed_paths=())
    outcome = apply_worker_result(record, result, now=datetime(2026, 9, 16, tzinfo=UTC))

    assert result.phase_status == "complete"
    assert outcome.record.state is RunnerState.SUGGEST_CLOSE
    assert outcome.record.suggest_close_evidence is not None
    assert outcome.record.suggest_close_evidence.issue_iid == record.issue_iid


def test_builder_rejects_a_close_suggestion_bound_to_another_issue(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "suggest_close",
            "change_summary": "The work is already satisfied.",
            "next_action": "A human may review the evidence.",
            "suggest_close_evidence": {
                "reason": "already-satisfied",
                "issue_iid": 82,
                "evidence": [
                    "https://gitlab.example.test/group/project/-/merge_requests/9"
                ],
            },
        }
    )

    with pytest.raises(WorkerProtocolError):
        build_worker_result(record, build_input, changed_paths=())


def test_builder_preserves_a_complete_multiline_coding_receipt_audit(
    tmp_path: Path,
) -> None:
    """A realistic all-role result remains publishable after durable storage."""
    record = _record(tmp_path)
    receipts = tuple(
        f"{skill}: approved\n"
        f"evidence: {skill} checked the final diff\n"
        + (
            "not applicable — no CLI surface changed"
            if skill == "$cli-syntax-judge"
            else "command: uv run pytest tests/test_agent_work_worker_result_builder.py"
        )
        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
    )
    build_input = _completed_input().model_copy(update={"receipts": receipts})

    result = build_worker_result(
        record,
        build_input,
        changed_paths=("src/agent_work/worker.py",),
    )
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 14, tzinfo=UTC)
    ).record

    assert transitioned.state is RunnerState.PUBLISHING
    assert transitioned.phase is WorkflowPhase.READY_TO_PUBLISH
    assert has_complete_workflow_receipt(transitioned)


def test_builder_completes_successful_post_publication_assessment_for_human_handoff(
    tmp_path: Path,
) -> None:
    """A healthy exact-source assessment records its receipt before handoff."""
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    assessment = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": ("$pipeline-performance-judge: exact source is healthy",),
            "change_summary": "The exact-source pipeline is healthy.",
            "next_action": "Await human review of the merge request.",
        }
    )

    result = build_worker_result(record, assessment, changed_paths=())
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 15, tzinfo=UTC)
    ).record

    assert result.phase_status == "complete"
    assert transitioned.state is RunnerState.AWAITING_HUMAN
    assert transitioned.phase is WorkflowPhase.POST_PUBLICATION
    assert transitioned.receipts[-1].verdict == "complete"
    assert "exact source is healthy" in transitioned.receipts[-1].detail


@pytest.mark.parametrize(
    ("result_kind", "error_code", "expected_state", "preserves_error_code"),
    [
        ("needs_human", "pipeline_rejected", RunnerState.BLOCKED, True),
        ("retryable", "pipeline_pending", RunnerState.RETRY_WAIT, False),
    ],
)
def test_builder_keeps_actionable_post_publication_assessments_rejected(
    tmp_path: Path,
    result_kind: str,
    error_code: str,
    expected_state: RunnerState,
    preserves_error_code: bool,
) -> None:
    """Actionable assessments retain their judge evidence instead of handoff."""
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    assessment = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": result_kind,
            "receipts": ("$pipeline-performance-judge: actionable evidence",),
            "change_summary": "The exact-source pipeline needs follow-up.",
            "next_action": "Resolve the recorded pipeline feedback.",
            "error_code": error_code,
        }
    )

    result = build_worker_result(record, assessment, changed_paths=())
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 15, tzinfo=UTC)
    ).record

    assert result.phase_status == "rejected"
    assert result.receipts == assessment.receipts
    assert transitioned.state is expected_state
    if preserves_error_code:
        assert transitioned.error_code is result.error_code


@pytest.mark.parametrize(
    "assessment",
    [
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": ("$tdd-finalizer: unrelated receipt",),
            "change_summary": "Malformed assessment.",
            "next_action": "Do not hand off.",
        },
        {
            "workflow_lane": "content",
            "result_kind": "ready_to_publish",
            "receipts": ("$pipeline-performance-judge: healthy",),
            "change_summary": "Contradictory lane.",
            "next_action": "Do not hand off.",
        },
    ],
    ids=("malformed-receipt", "contradictory-lane"),
)
def test_builder_rejects_incoherent_successful_post_publication_assessments(
    tmp_path: Path, assessment: dict[str, object]
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )

    with pytest.raises(WorkerProtocolError, match=r"receipts:.*coherent"):
        build_worker_result(
            record,
            WorkerResultBuildInput.model_validate(assessment),
            changed_paths=(),
        )


def test_builder_input_forbids_worker_control_of_derived_phase_status() -> None:
    payload = {
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "receipts": ["$pipeline-performance-judge: healthy"],
        "phase_status": "complete",
        "change_summary": "Contradictory worker intent.",
        "next_action": "Do not accept derived fields.",
    }

    with pytest.raises(WorkerProtocolError, match="builder_input: unsupported field"):
        parse_worker_result_build_input(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "untrusted_value"),
    [
        ("protocol_version", "99: super-secret-token"),
        ("unknown_secret_field", "x" * 10_000),
    ],
)
def test_builder_diagnostic_is_field_specific_bounded_and_never_echoes_input(
    field: str, untrusted_value: str
) -> None:
    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps({field: untrusted_value}).encode())

    diagnostic = format_worker_result_diagnostic(captured.value)

    expected = (
        "builder_input.protocol_version: must be omitted"
        if field == "protocol_version"
        else "builder_input: unsupported field"
    )
    assert diagnostic == expected
    assert len(diagnostic.encode("utf-8")) <= 2048
    assert untrusted_value not in diagnostic


def test_builder_diagnostic_keeps_known_nested_path_and_redacts_value() -> None:
    secret = "builder-nested-secret"
    payload = {
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "change_summary": "A valid summary.",
        "next_action": "A valid next action.",
        "audit_summary": {
            "capacity": {
                "required": secret,
                "available": 16,
                "shortfall": 0,
                "decision": "run",
            }
        },
    }

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    diagnostic = format_worker_result_diagnostic(captured.value)

    assert diagnostic == (
        "builder_input.audit_summary.capacity.required: invalid type/shape/value"
    )
    assert secret not in diagnostic


def test_builder_diagnostic_keeps_safe_numeric_position_for_known_list_field() -> None:
    payload = {
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "change_summary": "A valid summary.",
        "next_action": "A valid next action.",
        "audit_summary": {
            "capacity": {
                "required": 12,
                "available": 16,
                "shortfall": 0,
                "decision": "run",
            },
            "tests": [42],
        },
    }

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    assert format_worker_result_diagnostic(captured.value) == (
        "builder_input.audit_summary.tests.0: invalid type/shape/value"
    )


def test_builder_diagnostic_hides_nested_unknown_field() -> None:
    secret_name = "nested_builder_secret"
    secret_value = "nested-builder-value"
    payload = {
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "change_summary": "A valid summary.",
        "next_action": "A valid next action.",
        "audit_summary": {
            "capacity": {
                "required": 12,
                "available": 16,
                "shortfall": 0,
                "decision": "run",
                secret_name: secret_value,
            }
        },
    }

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    diagnostic = format_worker_result_diagnostic(captured.value)

    assert diagnostic == "builder_input.audit_summary.capacity: unsupported field"
    assert secret_name not in diagnostic and secret_value not in diagnostic


def test_builder_protocol_version_outranks_an_earlier_validation_error() -> None:
    payload = {
        "workflow_lane": 42,
        "protocol_version": 2,
        "result_kind": "ready_to_publish",
        "change_summary": "A valid summary.",
        "next_action": "A valid next action.",
    }

    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps(payload))

    assert format_worker_result_diagnostic(captured.value) == (
        "builder_input.protocol_version: must be omitted"
    )


def test_controller_invariant_diagnostic_keeps_exact_corrective_reason(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    result = build_worker_result(record, _completed_input(), changed_paths=())
    assert result.audit_summary is not None
    tampered_result = result.model_copy(
        update={
            "audit_summary": result.audit_summary.model_copy(
                update={"changed_paths": ("src/changed.py",)}
            )
        }
    )

    with pytest.raises(WorkerProtocolError) as captured:
        validate_worker_result(record, tampered_result, changed_paths=())

    assert format_worker_result_diagnostic(captured.value) == (
        "controller_invariant.audit_summary.changed_paths: "
        "does not match observed changed paths"
    )


def test_controller_rejects_current_capacity_that_disagrees_with_lane(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    result = build_worker_result(record, _completed_input(), changed_paths=())
    assert result.audit_summary is not None
    prior_capacity = AuditCapacity(
        required=13, available=16, shortfall=0, decision="run"
    )
    tampered_result = result.model_copy(
        update={
            "audit_summary": result.audit_summary.model_copy(
                update={"capacity": prior_capacity}
            )
        }
    )

    with pytest.raises(WorkerProtocolError) as captured:
        validate_worker_result(record, tampered_result, changed_paths=())

    assert format_worker_result_diagnostic(captured.value) == (
        "controller_invariant.audit_summary: "
        "scalar capacity must match topology live-slot evidence"
    )


def test_controller_rejects_scalar_only_direct_workflow_result(tmp_path: Path) -> None:
    """A direct envelope cannot use recovery-compatible scalar evidence to publish."""
    record = _record(tmp_path)
    result = build_worker_result(record, _completed_input(), changed_paths=())
    assert result.audit_summary is not None
    scalar_only = result.model_copy(
        update={
            "audit_summary": result.audit_summary.model_copy(
                update={"topology": None, "retained_objects": None}
            )
        }
    )

    with pytest.raises(WorkerProtocolError, match="topology"):
        validate_worker_result(record, scalar_only, changed_paths=())


@pytest.mark.parametrize(
    "receipts",
    [
        ("$architect: approved",),
        lambda complete: (*complete[:-1], complete[0]),
        lambda complete: tuple(receipt.removeprefix("$") for receipt in complete),
        lambda complete: ("$architect", *complete[1:]),
    ],
    ids=("incomplete", "duplicate", "malformed", "missing-detail"),
)
def test_builder_rejects_incomplete_duplicate_or_malformed_receipts(
    tmp_path: Path,
    receipts: tuple[str, ...] | Callable[[tuple[str, ...]], tuple[str, ...]],
) -> None:
    complete = _completed_input().receipts
    resolved = receipts if isinstance(receipts, tuple) else receipts(complete)
    invalid = _completed_input().model_copy(update={"receipts": resolved})

    with pytest.raises(WorkerProtocolError, match=r"receipts:.*required"):
        build_worker_result(_record(tmp_path), invalid, changed_paths=())


@pytest.mark.parametrize(
    "record_updates",
    [
        {
            "state": RunnerState.REPAIR,
            "repair_attempt": RepairAttemptEvidence(
                fingerprint="a" * 64, started_at="2026-09-10T00:00:00+00:00"
            ),
        },
        {
            "work_kind": WorkKind.EXISTING_MR_REMEDIATION,
            "merge_request_iid": 88,
            "merge_request_url": "https://gitlab.example.test/mr/88",
            "merge_request_draft": MergeRequestDraft(
                title="Existing MR", description="Existing review evidence."
            ),
            "publication_sha": "b" * 40,
        },
        {"merge_request_iid": 88},
        {"merge_request_url": "https://gitlab.example.test/mr/88"},
        {
            "merge_request_draft": MergeRequestDraft(
                title="Existing MR", description="Existing review evidence."
            )
        },
        {"publication_sha": "b" * 40},
        {
            "feedback_cursor": MergeRequestFeedbackCursor(
                last_processed_general_note_id=1, feedback_source_sha="a" * 40
            )
        },
        {
            "pending_feedback": PendingMergeRequestFeedback(
                target_cursor=MergeRequestFeedbackCursor(
                    last_processed_general_note_id=1, feedback_source_sha="a" * 40
                ),
                source_sha="a" * 40,
                publication_sha="b" * 40,
                source="general",
                note_id=1,
            )
        },
    ],
    ids=(
        "repair",
        "existing-mr",
        "mr-iid",
        "mr-url",
        "mr-draft",
        "publication-sha",
        "feedback-cursor",
        "pending-feedback",
    ),
)
def test_builder_rejects_split_required_outside_fresh_ordinary_intake(
    tmp_path: Path, record_updates: dict[str, object]
) -> None:
    record = _record(tmp_path).model_copy(update=record_updates)
    split = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "unknown",
            "result_kind": "split_required",
            "change_summary": "Split the oversized intake.",
            "next_action": "Create ordinary children.",
            "split_plan": {
                "slices": [
                    {
                        "ordinal": 1,
                        "key": "first",
                        "title": "First",
                        "description": "First slice.",
                        "depends_on": [],
                    },
                    {
                        "ordinal": 2,
                        "key": "second",
                        "title": "Second",
                        "description": "Second slice.",
                        "depends_on": [1],
                    },
                ]
            },
        }
    )

    with pytest.raises(
        WorkerProtocolError, match="split_required: is limited to fresh ordinary intake"
    ):
        build_worker_result(record, split, changed_paths=())


@pytest.mark.parametrize(
    "issue_iid",
    [
        40,
        78,
    ],
)
def test_worker_validation_failures_keep_the_retained_worktree_retryable(
    tmp_path: Path, issue_iid: int
) -> None:
    record = _record(tmp_path).model_copy(update={"issue_iid": issue_iid})

    with pytest.raises(WorkerProtocolError):
        build_worker_result(
            record,
            _completed_input().model_copy(update={"receipts": ("$architect",)}),
            changed_paths=(),
        )

    retry = plan_retry(record, now=datetime(2026, 9, 14, tzinfo=UTC)).record

    assert retry.state is RunnerState.RETRY_WAIT
    assert retry.prior_state is RunnerState.EXECUTING
    assert retry.prior_phase is WorkflowPhase.WORKFLOW
    assert retry.retry_count == record.retry_count + 1
    assert retry.worktree == record.worktree


def test_worker_prompt_requires_the_builder_and_v2_contract(tmp_path: Path) -> None:
    prompt = build_worker_prompt(
        _record(tmp_path).model_dump(mode="json"),
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=600,
    )

    assert "agent-work worker-result build" in prompt
    assert "protocol_version=2" in prompt
    assert "WORKFLOW_RECEIPTS" in prompt


def test_worker_prompt_requires_the_serial_judge_before_phase_five_fanout(
    tmp_path: Path,
) -> None:
    """The trusted worker prompt preserves the Judge-before-review protocol."""
    prompt = build_worker_prompt(
        _record(tmp_path).model_dump(mode="json"),
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=600,
    )

    judge = prompt.index("delegates the serial Phase-4 $tdd-judge")
    nonfunctional = prompt.index(
        "delegates $nonfunctional-review as its read-only child"
    )
    fanout = prompt.index("Phase-5 reviewer")

    assert judge < nonfunctional < fanout


def test_worker_prompt_requires_all_applicable_phase_two_receipts(
    tmp_path: Path,
) -> None:
    """Mixed coding work preserves the Policy Writer's separate ownership."""
    prompt = build_worker_prompt(
        _record(tmp_path).model_dump(mode="json"),
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=600,
    )

    assert "conditional $agent-policy-writer" in prompt
    assert "Policy Writer exclusively owns policy paths" in prompt
    assert "Green owns production, tests, and docs" in prompt


def test_coding_receipts_put_the_judge_before_every_phase_five_reviewer() -> None:
    """Whole-workflow receipt order matches the serial review dependency."""
    receipts = WORKFLOW_RECEIPTS[WorkflowLane.CODING]

    assert receipts.index("$nonfunctional-review") == receipts.index("$tdd-judge") + 1
    assert receipts.index("$tdd-judge") < receipts.index("$diataxis-judge")


def test_worker_result_build_cli_writes_only_one_envelope_to_stdout(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--apply",
        ],
        load_record=lambda issue_iid: _record(tmp_path) if issue_iid == 81 else None,
        changed_paths=lambda worktree: ("src/agent_work/worker.py",),
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 0
    assert stderr
    assert len(stdout) == 1
    assert stdout[0].startswith("<agent-work-result>{")
    assert stdout[0].endswith("}</agent-work-result>")
    assert "\n" not in stdout[0]


def test_worker_result_build_json_writes_one_bare_compact_object(
    tmp_path: Path,
) -> None:
    """The explicit JSON mode is directly consumable by JSON tooling."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stdout: list[str] = []

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--json",
        ],
        load_record=lambda issue_iid: _record(tmp_path) if issue_iid == 81 else None,
        changed_paths=lambda worktree: ("src/agent_work/worker.py",),
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert len(stdout) == 1
    assert json.loads(stdout[0])["protocol_version"] == 2


def test_worker_result_build_help_and_usage_follow_argparse_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["worker-result", "build", "--help"])

    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "agent-work worker-result build" in help_text
    assert "--input-file" in help_text
    assert "retain bounded diagnostics on failure" in help_text

    with pytest.raises(SystemExit) as usage_exit:
        main(["worker-result", "build"])

    assert usage_exit.value.code == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("case", "input_payload"),
    [
        ("invalid", {"raw-worker-prose-secret": "raw-worker-prose-secret"}),
        ("missing", None),
    ],
)
def test_worker_result_build_failure_is_bounded_and_emits_no_envelope(
    tmp_path: Path,
    case: str,
    input_payload: dict[str, str] | None,
) -> None:
    """Invalid input and missing ledger evidence never produce a partial result."""
    input_path = tmp_path / "result.json"
    payload = _completed_input().model_dump(mode="json") if case == "invalid" else {}
    if input_payload is not None:
        payload.update(input_payload)
    input_path.write_text(json.dumps(payload))
    stdout: list[str] = []
    stderr: list[str] = []

    def load_record(issue_iid: int) -> LedgerRecord | None:
        return _record(tmp_path) if case == "invalid" and issue_iid == 81 else None

    def changed_paths(worktree: Path) -> tuple[str, ...]:
        return ()

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
        ],
        load_record=load_record,  # type: ignore[arg-type]
        changed_paths=changed_paths,
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert len(stderr) == 1
    assert "raw-worker-prose-secret" not in stderr[0]
    assert len(stderr[0].encode("utf-8")) <= 2048


def test_worker_result_build_runtime_failure_is_sanitized_even_in_debug_mode(
    tmp_path: Path,
) -> None:
    """Default-safe diagnostics never expose a runtime boundary's detail."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stderr: list[str] = []

    def fail_load(issue_iid: int) -> LedgerRecord | None:
        _ = issue_iid
        raise RuntimeError("untrusted runtime detail")

    def observe_paths(worktree: Path) -> tuple[str, ...]:
        _ = worktree
        return ()

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--debug",
        ],
        load_record=fail_load,
        changed_paths=observe_paths,
        read_bytes=Path.read_bytes,
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stderr == ["worker_result: could not be read or validated"]


def test_builder_runtime_uses_read_only_ledger_loading(
    tmp_path: Path,
) -> None:
    """The builder's default dependency composition never preserves corruption."""
    config = AgentWorkConfig(
        host="git.example.test",
        project_id=310967,
        checkout=tmp_path,
        state_root=tmp_path / "state",
        git_path=Path("/usr/bin/git"),
        glab_path=Path("/usr/bin/glab"),
        base_branch="agent/issue-81",
    )
    record_path = config.state_root / "records" / "81.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{")
    dependencies = build_worker_result_dependencies(config=config)

    with pytest.raises(LedgerCorrupt):
        dependencies.load_record(81)

    assert not (config.state_root / "recovery").exists()


def test_cli_default_builder_dependencies_do_not_preserve_corrupt_ledger(
    tmp_path: Path,
) -> None:
    """Normal CLI wiring retains corrupt-ledger reads as a no-write failure."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    state_root = tmp_path / "state" / "agent-work" / "projects" / "310967-e80a65b3aa73"
    record_path = state_root / "records" / "81.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{")
    environment = {
        "AGENT_WORK_BASE_BRANCH": "agent/issue-81",
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_GIT": "/usr/bin/git",
        "AGENT_WORK_GLAB": "/usr/bin/glab",
        "GITLAB_HOST": "git.example.test",
        "GITLAB_PROJECT_ID": "310967",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-3:] == ("rev-parse", "--show-toplevel"):
            return CommandResult(0, f"{tmp_path}\n", "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, "https://git.example.test/group/project.git\n", "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "path_with_namespace": "group/project",
                    "http_url_to_repo": "https://git.example.test/group/project.git",
                }
            ),
            "",
        )

    stdout: list[str] = []
    stderr: list[str] = []
    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
        ],
        environment=environment,
        run_command=run_command,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["worker_result: could not be read or validated"]
    assert not (state_root / "recovery").exists()
    assert not (state_root / "agent-work.log").exists()


def test_builder_derives_no_content_reviewers(tmp_path: Path) -> None:
    """Content audits retain no coding-reviewer receipts."""
    record = _record(tmp_path).model_copy(
        update={"workflow_lane": WorkflowLane.CONTENT}
    )
    receipts = tuple(
        f"{skill}: approved" for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "content",
            "result_kind": "ready_to_publish",
            "receipts": receipts,
            "change_summary": "Completed content workflow.",
            "next_action": "Publish after validation.",
            "merge_request_draft": {"title": "Content", "description": "Ready."},
            "audit_summary": {
                "capacity": {
                    "required": 3,
                    "available": 16,
                    "shortfall": 0,
                    "decision": "run",
                },
                "topology": {
                    "widest_live_row": [
                        "main",
                        "$architect",
                        "$documentation-writer",
                    ],
                    "required_live_slots": 3,
                    "available_live_slots": 16,
                    "roles": [
                        {
                            "skill": "$architect",
                            "owner": "main",
                            "phase": "1",
                            "tier": "most_capable",
                        },
                        {
                            "skill": "$documentation-writer",
                            "owner": "$architect",
                            "phase": "2",
                            "tier": "balanced",
                        },
                        {
                            "skill": "$documentation-finalizer",
                            "owner": "$architect",
                            "phase": "3",
                            "tier": "fast",
                        },
                    ],
                    "owner_headroom": [
                        {
                            "owner": "main",
                            "direct_children_required": 1,
                            "direct_children_headroom": 3,
                            "descendants_required": 3,
                            "descendants_headroom": 3,
                        },
                        {
                            "owner": "$architect",
                            "direct_children_required": 2,
                            "direct_children_headroom": 2,
                            "descendants_required": 2,
                            "descendants_headroom": 2,
                        },
                    ],
                },
                "retained_objects": {
                    "ceiling": 3,
                    "current": 0,
                    "required_fresh_handles": 3,
                    "fresh_headroom": 0,
                },
            },
        }
    )

    result = build_worker_result(record, build_input, changed_paths=())

    assert result.audit_summary is not None
    assert result.audit_summary.reviewers == ()
