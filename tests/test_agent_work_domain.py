# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the recoverable runner domain."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work import domain
from agent_work.domain import (
    ACTIVE_STATES,
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    label_for_state,
    retries_remaining,
)
from agent_work.state_machine import validate_transition


@given(st.sampled_from(tuple(ACTIVE_STATES)))
def test_every_active_state_projects_to_running(state: RunnerState) -> None:
    assert label_for_state(state) == "agent::running"


def test_terminal_and_handoff_states_have_distinct_projections() -> None:
    assert label_for_state(RunnerState.BLOCKED) == "agent::blocked"
    assert label_for_state(RunnerState.AWAITING_HUMAN) == "agent::awaiting-human"
    assert label_for_state(RunnerState.FAILED) == "agent::failed"
    assert label_for_state(RunnerState.COMPLETE) is None


@pytest.mark.parametrize(
    ("state_value", "expected_label"),
    [
        ("needs-repair", "agent::needs_repair"),
        ("repair-failed", "agent::repair_failed"),
        ("suggest-close", "agent::suggest_close"),
    ],
)
def test_new_nonactive_states_have_distinct_durable_label_projections(
    state_value: str, expected_label: str
) -> None:
    state = RunnerState(state_value)

    assert state not in ACTIVE_STATES
    assert label_for_state(state) == expected_label


def test_lifecycle_label_specs_are_stable_and_cover_every_projection() -> None:
    specs = domain.LIFECYCLE_LABEL_SPECS
    assert specs["agent::needs_repair"] == {
        "color": "#D4A72C",
        "description": "Pipeline repair needs a newly reviewed exact-IID plan.",
    }
    assert specs["agent::repair_failed"] == {
        "color": "#C91C00",
        "description": "A reviewed repair failed; preserve evidence and review a new plan.",
    }
    assert specs["agent::suggest_close"] == {
        "color": "#6F42C1",
        "description": "Structured evidence suggests closure; only a human may close it.",
    }
    assert set(specs) >= {
        label_for_state(state)
        for state in RunnerState
        if label_for_state(state) is not None
    }
    with pytest.raises(TypeError):
        cast(MutableMapping[str, object], specs)["agent::needs_repair"] = {}


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunnerState.PREPARING, RunnerState.EXECUTING),
        (RunnerState.PREPARING, RunnerState.OBSERVING),
        (RunnerState.RETRY_WAIT, RunnerState.PREPARING),
        (RunnerState.EXECUTING, RunnerState.PUBLISHING),
        (RunnerState.EXECUTING, RunnerState.OBSERVING),
        (RunnerState.PUBLISHING, RunnerState.EXECUTING),
        (RunnerState.PUBLISHING, RunnerState.OBSERVING),
        (RunnerState.OBSERVING, RunnerState.REPAIR),
        (RunnerState.REPAIR, RunnerState.PUBLISHING),
        (RunnerState.BLOCKED, RunnerState.PREPARING),
        (RunnerState.AWAITING_HUMAN, RunnerState.COMPLETE),
    ],
)
def test_required_transition_edges_are_explicit(
    source: RunnerState, target: RunnerState
) -> None:
    assert validate_transition(source, target)


@pytest.mark.parametrize(
    ("source_value", "target_value"),
    [("needs-repair", "repair"), ("repair-failed", "needs-repair")],
)
def test_new_repair_transitions_require_reviewed_recovery(
    source_value: str, target_value: str
) -> None:
    assert validate_transition(RunnerState(source_value), RunnerState(target_value))


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (RunnerState.BLOCKED, RunnerState.EXECUTING),
        (RunnerState.FAILED, RunnerState.PREPARING),
        (RunnerState.COMPLETE, RunnerState.EXECUTING),
    ],
)
def test_forbidden_transition_edges_fail_closed(
    source: RunnerState, target: RunnerState
) -> None:
    assert not validate_transition(source, target)


@pytest.mark.parametrize(
    ("source_value", "target_value"),
    [
        ("needs-repair", "executing"),
        ("repair-failed", "repair"),
        ("suggest-close", "executing"),
    ],
)
def test_nonactive_lifecycle_states_cannot_activate_without_review(
    source_value: str, target_value: str
) -> None:
    assert not validate_transition(RunnerState(source_value), RunnerState(target_value))


def test_retry_budget_allows_initial_attempt_plus_three_retries() -> None:
    assert [retries_remaining(value) for value in range(5)] == [3, 2, 1, 0, 0]


def test_domain_exposes_stable_lifecycle_vocabulary() -> None:
    assert RunnerState.RETRY_WAIT.value == "retry-wait"
    assert WorkflowPhase.POST_PUBLICATION.value == "post-publication"
    assert WorkflowLane.CONTENT.value == "content"
    assert ErrorCode.RETRY_EXHAUSTED.value == "retry_exhausted"


@pytest.mark.parametrize(
    "reason",
    ["duplicate", "superseded", "already-satisfied", "no-longer-applicable"],
)
def test_suggest_close_requires_one_allowed_reason_and_bound_structured_evidence(
    reason: str,
) -> None:
    evidence_type = domain.SuggestCloseEvidence

    evidence = evidence_type(
        reason=reason,
        issue_iid=12,
        evidence=("https://gitlab.example.test/group/project/-/issues/13",),
    )

    assert evidence.reason == reason
    assert evidence.issue_iid == 12


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "duplicate", "issue_iid": 12, "evidence": ()},
        {"reason": "duplicate", "issue_iid": None, "evidence": ("#13",)},
        {"reason": "unverified-claim", "issue_iid": 12, "evidence": ("#13",)},
        {"reason": "duplicate", "issue_iid": 12, "evidence": ("close it",)},
    ],
)
def test_suggest_close_rejects_empty_unbound_or_bare_claim_evidence(
    payload: dict[str, object],
) -> None:
    evidence_type = domain.SuggestCloseEvidence

    with pytest.raises(ValidationError):
        evidence_type(**cast(Any, payload))


def test_repair_attempt_evidence_is_bounded_nonsecret_and_binds_plan_and_delta() -> (
    None
):
    evidence_type = domain.RepairAttemptEvidence
    attempt = evidence_type(
        reviewed_plan_fingerprint="a" * 64,
        reviewed_snapshot={"issue_iid": 12, "pipeline_id": 4},
        evidence_delta={"pipeline_status": "failed"},
        failure_reason="repair worker returned a non-zero result",
        started_at="2026-09-10T00:00:00+00:00",
        elapsed_seconds=12.5,
    )

    assert attempt.reviewed_plan_fingerprint == "a" * 64
    assert attempt.evidence_delta == {"pipeline_status": "failed"}
    assert attempt.elapsed_seconds == 12.5


@pytest.mark.parametrize(
    "payload",
    [
        {"reviewed_plan_fingerprint": "not-a-fingerprint"},
        {"reviewed_plan_fingerprint": "a" * 64, "failure_reason": "x" * 2_049},
        {"reviewed_plan_fingerprint": "a" * 64, "elapsed_seconds": -1},
        {"reviewed_plan_fingerprint": "a" * 64, "elapsed_seconds": 10_801},
        {
            "reviewed_plan_fingerprint": "a" * 64,
            "reviewed_snapshot": {"snapshot": "x" * 8_192},
            "evidence_delta": {"delta": "x"},
        },
        {
            "reviewed_plan_fingerprint": "a" * 64,
            "reviewed_snapshot": {"snapshot": "x"},
            "evidence_delta": {"delta": "x" * 8_192},
        },
        {
            "reviewed_plan_fingerprint": "a" * 64,
            "reviewed_snapshot": {"snapshot": "x" * 4_100},
            "evidence_delta": {"delta": "x" * 4_100},
        },
    ],
)
def test_repair_attempt_evidence_rejects_unbounded_or_invalid_payload(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        domain.RepairAttemptEvidence(**cast(Any, payload))
