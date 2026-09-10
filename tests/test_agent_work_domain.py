"""Behavior contracts for the recoverable runner domain."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

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
    ("source", "target"),
    [
        (RunnerState.PREPARING, RunnerState.EXECUTING),
        (RunnerState.EXECUTING, RunnerState.PUBLISHING),
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
    ("source", "target"),
    [
        (RunnerState.PREPARING, RunnerState.OBSERVING),
        (RunnerState.EXECUTING, RunnerState.OBSERVING),
        (RunnerState.PUBLISHING, RunnerState.EXECUTING),
        (RunnerState.BLOCKED, RunnerState.EXECUTING),
        (RunnerState.FAILED, RunnerState.PREPARING),
        (RunnerState.COMPLETE, RunnerState.EXECUTING),
    ],
)
def test_forbidden_transition_edges_fail_closed(
    source: RunnerState, target: RunnerState
) -> None:
    assert not validate_transition(source, target)


def test_retry_budget_allows_initial_attempt_plus_three_retries() -> None:
    assert [retries_remaining(value) for value in range(5)] == [3, 2, 1, 0, 0]


def test_domain_exposes_stable_lifecycle_vocabulary() -> None:
    assert RunnerState.RETRY_WAIT.value == "retry-wait"
    assert WorkflowPhase.POST_PUBLICATION.value == "post-publication"
    assert WorkflowLane.CONTENT.value == "content"
    assert ErrorCode.RETRY_EXHAUSTED.value == "retry_exhausted"
