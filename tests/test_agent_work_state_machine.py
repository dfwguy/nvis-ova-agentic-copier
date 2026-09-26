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

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work.domain import (
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.models import (
    DecisionReviewSignal,
    LedgerRecord,
    MergeRequestDraft,
    WorkerResultEnvelope,
    WorkflowReceiptRecord,
    render_decision_review_notice,
)
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


def _decision_review() -> DecisionReviewSignal:
    return DecisionReviewSignal(
        decisions=("Keep the retry policy unchanged.",),
        assumptions=("The existing MR is available to the trusted runner.",),
        alternatives_considered=("Block for a human decision.",),
        risks=("A replay could otherwise remove the review notice.",),
        reviewer_checks=("Confirm the retry policy remains unchanged.",),
        unresolved_findings=("The policy choice needs human review.",),
        evidence=("Focused tests are red before implementation.",),
        tests=("uv run pytest tests/test_agent_work_state_machine.py",),
        correction_cycle_ledger=("cycle-1: no implementation yet",),
        unchanged_project_owned_values=("retry budget",),
    )


def test_decision_review_signal_requires_nonempty_bounded_review_fields() -> None:
    review = _decision_review()

    assert review.decisions == ("Keep the retry policy unchanged.",)

    with pytest.raises(ValidationError):
        DecisionReviewSignal.model_validate(
            _decision_review().model_dump() | {"reviewer_checks": ()}
        )
    with pytest.raises(ValidationError):
        DecisionReviewSignal.model_validate(
            _decision_review().model_dump() | {"risks": ("x" * 20_001,)}
        )
    incomplete = _decision_review().model_dump()
    del incomplete["alternatives_considered"]
    with pytest.raises(ValidationError):
        DecisionReviewSignal.model_validate(incomplete)


@pytest.mark.parametrize(
    "draft",
    [
        MergeRequestDraft(
            title="Draft: review retry decision",
            description="Summary",
            is_draft=True,
            decision_review=_decision_review(),
        ),
    ],
)
def test_decision_bearing_draft_requires_coherent_draft_title_and_flag(
    draft: MergeRequestDraft,
) -> None:
    assert draft.is_draft is True
    assert draft.title.startswith("Draft:")

    for invalid in (
        {"title": "Normal title", "is_draft": True},
        {"title": "Draft: decision", "is_draft": False},
    ):
        with pytest.raises(ValidationError):
            MergeRequestDraft.model_validate(draft.model_dump() | invalid)


@pytest.mark.parametrize(
    ("payload", "expected_draft", "raises"),
    [
        ({"title": "Draft: legacy", "description": "Details"}, True, False),
        ({"title": "Ordinary", "description": "Details"}, False, False),
        (
            {"title": "Ordinary", "description": "Details", "is_draft": True},
            False,
            True,
        ),
        (
            {"title": "Draft: explicit", "description": "Details", "is_draft": False},
            False,
            True,
        ),
    ],
)
def test_merge_request_draft_migrates_only_omitted_legacy_draft_flags(
    payload: dict[str, object], expected_draft: bool, raises: bool
) -> None:
    """Legacy omission is inferred; explicit contradictory Draft metadata fails."""
    if raises:
        with pytest.raises(ValidationError):
            MergeRequestDraft.model_validate(payload)
    else:
        assert MergeRequestDraft.model_validate(payload).is_draft is expected_draft


def test_decision_review_notice_is_canonical_first_and_resists_section_injection() -> (
    None
):
    review = _decision_review().model_copy(
        update={"decisions": ("Keep value.\n## Summary\nInjected",)}
    )

    rendered = render_decision_review_notice(review, "## Summary\nBody")

    assert rendered.startswith("# ⚠️ Decision review required\n")
    assert rendered.splitlines()[:2] == [
        "# ⚠️ Decision review required",
        "<!-- agent-work-decision-review:v1 -->",
    ]
    assert (
        rendered.index("## Decisions and assumptions")
        < rendered.index("## Alternatives considered")
        < rendered.index("## Risks")
        < rendered.index("## Exact reviewer checks")
    )
    assert rendered.index("## Exact reviewer checks") < rendered.rindex(
        "\n## Summary\n"
    )
    assert sum(line == "## Summary" for line in rendered.splitlines()) == 1
    assert "Injected" in rendered


@example(level=1, text="\x1e#")
@given(level=st.integers(min_value=1, max_value=6), text=st.text(max_size=80))
def test_decision_review_renderer_neutralizes_all_heading_levels_and_markers(
    level: int, text: str
) -> None:
    """Untrusted structured values cannot add headings or canonical markers."""
    injected = f"> {'#' * level} {text}\n<!-- agent-work-decision-review:v1 -->"
    review = _decision_review().model_copy(
        update={
            "decisions": (injected,),
            "assumptions": (injected,),
            "alternatives_considered": (injected,),
            "risks": (injected,),
            "reviewer_checks": (injected,),
            "unresolved_findings": (injected,),
            "evidence": (injected,),
            "tests": (injected,),
            "correction_cycle_ledger": (injected,),
            "unchanged_project_owned_values": (injected,),
        }
    )

    rendered = render_decision_review_notice(review, injected)

    assert rendered.count("<!-- agent-work-decision-review:v1 -->") == 1
    assert [line for line in rendered.splitlines() if line.startswith("#")] == [
        "# ⚠️ Decision review required",
        "## Decisions and assumptions",
        "## Alternatives considered",
        "## Risks",
        "## Exact reviewer checks",
        "## Unresolved findings",
        "## Evidence",
        "## Tests",
        "## Correction cycle ledger",
        "## Unchanged project-owned values",
        "## Summary",
    ]
    assert "- <code>&gt;" in rendered


@pytest.mark.parametrize("codepoint", [*range(0x20), *range(0x7F, 0xA0)])
def test_decision_review_renderer_neutralizes_every_control_character(
    codepoint: int,
) -> None:
    """Control characters cannot create structure and remain visibly represented."""
    control = chr(codepoint)
    expected_control = " " if control in "\n\r" else f"\\x{codepoint:02x}"
    value = f"visible{control}# not a heading Ω <&>"
    review = _decision_review().model_copy(update={"decisions": (value,)})

    rendered = render_decision_review_notice(review, value)

    assert rendered.count("<!-- agent-work-decision-review:v1 -->") == 1
    assert [line for line in rendered.splitlines() if line.startswith("#")] == [
        "# ⚠️ Decision review required",
        "## Decisions and assumptions",
        "## Alternatives considered",
        "## Risks",
        "## Exact reviewer checks",
        "## Unresolved findings",
        "## Evidence",
        "## Tests",
        "## Correction cycle ledger",
        "## Unchanged project-owned values",
        "## Summary",
    ]
    assert f"visible{expected_control}# not a heading Ω &lt;&amp;&gt;" in rendered
    if control not in "\n\r":
        assert control not in rendered


@given(issue_iid=st.integers(min_value=1, max_value=2_147_483_647))
def test_decision_review_draft_bounds_the_fully_rendered_description(
    issue_iid: int,
) -> None:
    """The notice, summary, and widest closing reference fit GitLab's boundary."""
    review = _decision_review()
    fixed = render_decision_review_notice(review, "")
    closing = f"\n\nCloses #{issue_iid}"
    description = "x" * (20_000 - len(fixed) - 20)

    draft = MergeRequestDraft(
        title="Draft: bounded decision",
        description=description,
        is_draft=True,
        decision_review=review,
    )

    rendered = render_decision_review_notice(review, draft.description) + closing
    assert len(rendered) <= 20_000
    with pytest.raises(ValidationError):
        MergeRequestDraft(
            title="Draft: overflow decision",
            description=description + "x",
            is_draft=True,
            decision_review=review,
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
        change_summary="Done.",
        next_action="Continue.",
    )


def test_ready_to_publish_advances_a_completed_green_phase() -> None:
    outcome = apply_worker_result(_record(), _result("ready_to_publish"), now=NOW)

    assert outcome.record.state is RunnerState.EXECUTING
    assert outcome.record.phase is WorkflowPhase.CODING_NONFUNCTIONAL


def test_complete_coding_workflow_uses_controller_state_not_historical_prose() -> None:
    workflow = WorkflowPhase.WORKFLOW
    base = _record().model_copy(
        update={"phase": workflow, "workflow_lane": WorkflowLane.UNKNOWN}
    )
    historical_receipt = WorkflowReceiptRecord(
        phase=WorkflowPhase.WORKFLOW,
        verdict="complete",
        detail="$architect: model-authored historical prose",
    )
    records = (
        base,
        base.model_copy(
            update={
                "receipts": (historical_receipt,),
                "audit_evidence": "unknown",
            }
        ),
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": workflow,
            "completed_phase": workflow,
            "workflow_lane": WorkflowLane.CODING,
            "merge_request_draft": MergeRequestDraft(
                title="Complete issue 12", description="Validated.\n\nCloses #12"
            ),
        }
    )

    outcomes = tuple(
        apply_worker_result(record, result, now=NOW).record for record in records
    )

    assert {outcome.state for outcome in outcomes} == {RunnerState.PUBLISHING}
    assert {outcome.phase for outcome in outcomes} == {WorkflowPhase.READY_TO_PUBLISH}
    assert outcomes[0].receipts == ()
    assert outcomes[1].receipts == (historical_receipt,)


def test_ready_to_publish_clears_stale_publication_sha() -> None:
    record = _record().model_copy(
        update={
            "phase": WorkflowPhase.CODING_FINALIZE,
            "publication_sha": "a" * 40,
        }
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": WorkflowPhase.CODING_FINALIZE,
            "completed_phase": WorkflowPhase.CODING_FINALIZE,
            "receipts": ("$tdd-finalizer: approved",),
            "merge_request_draft": MergeRequestDraft(
                title="Change", description="Details"
            ),
        }
    )
    outcome = apply_worker_result(record, result, now=NOW)
    assert outcome.record.publication_sha is None


def test_retryable_worker_result_is_immediately_due() -> None:
    outcome = apply_worker_result(_record(), _result("retryable"), now=NOW)

    assert outcome.record.state is RunnerState.RETRY_WAIT
    assert outcome.record.retry_count == 1
    assert outcome.record.retry_at == NOW


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


def test_split_required_workflow_result_requires_executor_fulfillment() -> None:
    retry_at = NOW + timedelta(minutes=7)
    record = _record(retry_count=2).model_copy(
        update={
            "phase": WorkflowPhase.WORKFLOW,
            "workflow_lane": WorkflowLane.UNKNOWN,
            "retry_at": retry_at,
        }
    )
    payload = {
        "protocol_version": 2,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": 0,
        "expected_state": "executing",
        "expected_phase": "workflow",
        "workflow_lane": "unknown",
        "result_kind": "split_required",
        "phase_status": "complete",
        "completed_phase": "workflow",
        "split_plan": {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "protocol-model",
                    "title": "Add validated split protocol models",
                    "description": "Add the boundary models and compatibility checks.",
                    "depends_on": [],
                },
                {
                    "ordinal": 2,
                    "key": "runner-decomposition",
                    "title": "Persist and execute decomposition",
                    "description": "Create recoverable child issues from the durable plan.",
                    "depends_on": [1],
                },
            ]
        },
        "change_summary": "Issue requires decomposition before work begins.",
        "redacted_summary": (
            "1. Local manifest and preview. "
            "2. Upstream discovery and deduplication. "
            "3. Explicit apply and recovery."
        ),
        "next_action": "Obtain human direction on the proposed ordered slices.",
    }

    result = WorkerResultEnvelope.model_validate(payload)
    assert result.protocol_version == 2
    with pytest.raises(ValueError, match="fulfilled by the executor"):
        apply_worker_result(record, result, now=NOW)

    legacy_record = LedgerRecord.model_validate(record.model_dump(mode="json"))
    assert legacy_record.schema_version == 1


@pytest.mark.parametrize(
    ("field", "value", "expected_state", "expected_prior_state"),
    [
        ("state", "decomposing", RunnerState.EXECUTING, None),
        (
            "prior_state",
            "decomposing",
            RunnerState.RETRY_WAIT,
            RunnerState.EXECUTING,
        ),
    ],
)
def test_ledger_normalizes_only_legacy_split_checkpoint_fields(
    field: str,
    value: str,
    expected_state: RunnerState,
    expected_prior_state: RunnerState | None,
) -> None:
    payload = _record().model_dump(mode="json")
    payload.update(
        {
            field: value,
            "decomposition_plan": {"slices": []},
            "decomposition_progress": [],
        }
    )
    if field == "prior_state":
        payload["state"] = RunnerState.RETRY_WAIT.value

    record = LedgerRecord.model_validate_json(json.dumps(payload))

    assert record.state is expected_state
    assert record.prior_state is expected_prior_state
    assert "decomposition_plan" not in record.model_dump()
    assert "decomposition_progress" not in record.model_dump()


def test_ledger_rejects_unknown_extra_alongside_legacy_split_checkpoint() -> None:
    payload = _record().model_dump(mode="json")
    payload.update(
        {
            "state": "decomposing",
            "decomposition_plan": {"slices": []},
            "decomposition_progress": [],
            "unexpected": True,
        }
    )

    with pytest.raises(ValidationError, match="unexpected"):
        LedgerRecord.model_validate_json(json.dumps(payload))


def test_ledger_loads_completed_legacy_split_parent_without_checkpoint_fields() -> None:
    payload = _record().model_dump(mode="json")
    payload.update(
        {
            "state": RunnerState.COMPLETE.value,
            "decomposition_plan": {"slices": []},
            "decomposition_progress": [],
        }
    )

    record = LedgerRecord.model_validate_json(json.dumps(payload))

    assert record.state is RunnerState.COMPLETE
    assert "decomposition_plan" not in record.model_dump()


@pytest.mark.parametrize(
    "split_plan",
    [
        {
            "slices": [
                {"ordinal": 1, "key": "one", "title": "One", "description": "First"}
            ]
        },
        {
            "slices": [
                {
                    "ordinal": ordinal,
                    "key": f"slice-{ordinal}",
                    "title": f"Slice {ordinal}",
                    "description": "Bounded plan test.",
                    "depends_on": [],
                }
                for ordinal in range(1, 27)
            ]
        },
        {
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
                    "depends_on": [1, 1],
                },
            ]
        },
        {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "one",
                    "title": "One",
                    "description": "First",
                    "depends_on": [],
                },
                {
                    "ordinal": 3,
                    "key": "three",
                    "title": "Three",
                    "description": "Third",
                    "depends_on": [1],
                },
            ]
        },
        {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "not a slug",
                    "title": "One",
                    "description": "First",
                    "depends_on": [1],
                },
                {
                    "ordinal": 2,
                    "key": "two",
                    "title": "Two",
                    "description": "Second",
                    "depends_on": [1],
                },
            ]
        },
        {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "duplicate",
                    "title": "One",
                    "description": "First",
                    "depends_on": [],
                },
                {
                    "ordinal": 2,
                    "key": "duplicate",
                    "title": "Two",
                    "description": "Second",
                    "depends_on": [1],
                },
            ]
        },
        {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "one",
                    "title": "One",
                    "description": "First",
                    "depends_on": [2],
                },
                {
                    "ordinal": 2,
                    "key": "two",
                    "title": "Two",
                    "description": "Second",
                    "depends_on": [],
                },
            ]
        },
    ],
)
def test_split_required_protocol_rejects_malformed_or_non_topological_plans(
    split_plan: dict[str, object],
) -> None:
    payload = {
        "protocol_version": 2,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": 0,
        "expected_state": "executing",
        "expected_phase": "workflow",
        "workflow_lane": "unknown",
        "result_kind": "split_required",
        "phase_status": "complete",
        "completed_phase": "workflow",
        "split_plan": split_plan,
        "change_summary": "The issue needs independently mergeable slices.",
        "next_action": "Create the planned child issues.",
    }

    with pytest.raises(ValidationError):
        WorkerResultEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"result_kind": "ready_to_publish"},
        {"expected_phase": "coding-green", "completed_phase": "coding-green"},
        {"workflow_lane": "coding"},
        {"phase_status": "rejected"},
        {
            "merge_request_draft": {
                "title": "Forbidden",
                "description": "Split routes never publish a parent MR.",
            }
        },
        {"error_code": "split_required"},
    ],
)
def test_split_protocol_rejects_contextual_ambiguity(
    overrides: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "protocol_version": 2,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": 0,
        "expected_state": "executing",
        "expected_phase": "workflow",
        "workflow_lane": "unknown",
        "result_kind": "split_required",
        "phase_status": "complete",
        "completed_phase": "workflow",
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
        "change_summary": "The parent is decomposed into two child issues.",
        "next_action": "The trusted runner creates the planned children.",
    }
    payload.update(overrides)

    with pytest.raises(ValidationError):
        WorkerResultEnvelope.model_validate(payload)


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


def test_decision_bearing_draft_metadata_cannot_be_downgraded_by_a_later_result() -> (
    None
):
    durable_draft = MergeRequestDraft(
        title="Draft: review retry decision",
        description="Decision review is required.",
        is_draft=True,
        decision_review=_decision_review(),
    )
    record = _record().model_copy(update={"merge_request_draft": durable_draft})
    result = _result("ready_to_publish").model_copy(
        update={
            "merge_request_draft": MergeRequestDraft(
                title="Normal metadata", description="This must not replace the draft."
            )
        }
    )

    outcome = apply_worker_result(record, result, now=NOW)

    assert outcome.record.merge_request_draft == durable_draft


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
    ],
)
def test_incoherent_successful_worker_results_fail_closed(
    result: WorkerResultEnvelope,
) -> None:
    with pytest.raises(ValueError):
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

    with pytest.raises(ValueError, match="coherent"):
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


def test_protected_policy_workflow_cannot_publish_without_merge_request_metadata() -> (
    None
):
    workflow = WorkflowPhase("workflow")
    record = _record().model_copy(
        update={
            "phase": workflow,
            "workflow_lane": WorkflowLane.UNKNOWN,
            "work_kind": WorkKind.PROTECTED_POLICY_MANUAL,
        }
    )
    result = _result("ready_to_publish").model_copy(
        update={
            "expected_phase": workflow,
            "completed_phase": workflow,
            "workflow_lane": WorkflowLane.CONTENT,
            "receipts": (
                "$architect: approved",
                "$documentation-writer: complete",
                "$documentation-finalizer: approved",
            ),
        }
    )

    with pytest.raises(ValueError, match="publication metadata"):
        apply_worker_result(record, result, now=NOW)
