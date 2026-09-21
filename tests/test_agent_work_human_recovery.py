# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the human-assisted recovery inventory and plans."""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from agent_work.domain import RunnerState
from agent_work.human_recovery import (
    EvidenceDriftError,
    RecoveryClassification,
    RecoveryObservation,
    ResolutionChoice,
    ResolutionRequest,
    build_inventory,
    build_resolution_plan,
    classify_recovery_owner_capacity,
    verify_resolution_plan,
)


def _observation(
    iid: int,
    *,
    issue_state: str = "opened",
    labels: tuple[str, ...] = (),
    record_state: RunnerState | None = None,
    pipeline_status: str | None = None,
    dependencies: tuple[tuple[int, str], ...] = (),
    stale: bool = False,
    ambiguous: bool = False,
) -> RecoveryObservation:
    return RecoveryObservation(
        issue_iid=iid,
        issue_state=issue_state,
        labels=labels,
        record_state=record_state,
        pipeline_status=pipeline_status,
        split_dependencies=dependencies,
        stale=stale,
        ambiguous=ambiguous,
    )


def test_inventory_orders_actionable_candidates_and_never_selects_fresh_work() -> None:
    inventory = build_inventory(
        (
            _observation(81, pipeline_status="failed"),
            _observation(82, record_state=RunnerState.EXECUTING, stale=True),
            _observation(83, record_state=RunnerState.BLOCKED),
            _observation(84, dependencies=((35, "closed"),)),
            _observation(85, record_state=RunnerState.AWAITING_HUMAN),
            _observation(86),
        )
    )

    assert [candidate.issue_iid for candidate in inventory.candidates] == [
        81,
        82,
        83,
        84,
        85,
    ]
    assert [candidate.classification for candidate in inventory.candidates] == [
        RecoveryClassification.NEEDS_HUMAN_DECISION,
        RecoveryClassification.NEEDS_HUMAN_DECISION,
        RecoveryClassification.SAFELY_RETRYABLE,
        RecoveryClassification.SAFELY_RETRYABLE,
        RecoveryClassification.NEEDS_HUMAN_DECISION,
    ]
    assert all(candidate.issue_iid != 86 for candidate in inventory.candidates)


def test_inventory_prioritizes_failed_then_stale_then_blocked_dependencies_and_human() -> (
    None
):
    """The fixed incident ordering never turns a stale active record into retry."""
    inventory = build_inventory(
        (
            _observation(9, record_state=RunnerState.AWAITING_HUMAN),
            _observation(8, dependencies=((3, "opened"),)),
            _observation(7, record_state=RunnerState.BLOCKED),
            _observation(6, record_state=RunnerState.EXECUTING, stale=True),
            _observation(5, pipeline_status="failed"),
        )
    )

    assert [candidate.issue_iid for candidate in inventory.candidates] == [
        5,
        6,
        7,
        8,
        9,
    ]
    assert (
        inventory.candidates[1].classification
        is RecoveryClassification.NEEDS_HUMAN_DECISION
    )


@given(
    st.lists(st.integers(min_value=1, max_value=1_000), unique=True, min_size=1),
    st.sampled_from(
        (None, "source_branch", "worktree", "merge_request_iid", "merge_request_url")
    ),
)
def test_owner_capacity_orders_each_retained_iid_deterministically(
    issue_iids: list[int], identity_collision: str | None
) -> None:
    """The pure owner classifier orders IIDs and rejects every shared identity."""
    assume(identity_collision is None or len(issue_iids) >= 2)
    observations = [
        RecoveryObservation(
            issue_iid=issue_iid,
            record_state=RunnerState.EXECUTING,
            source_branch=(
                "agent/shared"
                if identity_collision == "source_branch" and position == 1
                else "agent/shared"
                if identity_collision == "source_branch" and position == 0
                else f"agent/issue-{issue_iid}"
            ),
            worktree=(
                "/worktrees/shared"
                if identity_collision == "worktree" and position < 2
                else f"/worktrees/{issue_iid}"
            ),
            merge_request_iid=(
                88
                if identity_collision == "merge_request_iid" and position < 2
                else None
            ),
            merge_request_url=(
                "https://mr/shared"
                if identity_collision == "merge_request_url" and position < 2
                else None
            ),
            owner_status="stale",
        )
        for position, issue_iid in enumerate(issue_iids)
    ]

    capacity = classify_recovery_owner_capacity(
        tuple(observations), target_iid=issue_iids[0]
    )

    assert [issue_iid for issue_iid, _status in capacity.owner_states] == sorted(
        issue_iids
    )
    assert capacity.identity_conflict is (identity_collision is not None)


@pytest.mark.parametrize(
    ("observation", "classification"),
    [
        (
            _observation(35, record_state=RunnerState.EXECUTING),
            RecoveryClassification.LEGITIMATELY_WAITING,
        ),
        (
            _observation(78, dependencies=((35, "opened"),)),
            RecoveryClassification.DEPENDENCY_GATED,
        ),
        (
            _observation(79, issue_state="closed", record_state=RunnerState.EXECUTING),
            RecoveryClassification.SUPERSEDED_DUPLICATE,
        ),
        (
            _observation(80, issue_state="opened", labels=("agent::failed",)),
            RecoveryClassification.CLOSURE_CANDIDATE,
        ),
        (
            _observation(81, ambiguous=True),
            RecoveryClassification.NEEDS_HUMAN_DECISION,
        ),
    ],
)
def test_inventory_classifies_recovery_evidence_without_trusting_issue_text(
    observation: RecoveryObservation, classification: RecoveryClassification
) -> None:
    inventory = build_inventory((observation,))

    assert inventory.candidates[0].classification is classification


@pytest.mark.parametrize(
    "observation",
    (
        _observation(91, issue_state="closed", dependencies=((35, "closed"),)),
        _observation(
            92,
            issue_state="closed",
            record_state=RunnerState.COMPLETE,
            stale=True,
            ambiguous=True,
        ),
    ),
)
def test_closed_terminal_evidence_is_settled_before_drift_or_dependencies(
    observation: RecoveryObservation,
) -> None:
    """Closed no-action and complete records never become recovery work."""
    inventory = build_inventory((observation,))

    assert (
        inventory.candidates[0].classification
        is RecoveryClassification.CLOSED_NO_ACTION
    )


def test_resolution_preview_has_a_stable_evidence_bound_digest() -> None:
    observation = _observation(
        78,
        record_state=RunnerState.BLOCKED,
        dependencies=((35, "closed"),),
    )
    request = ResolutionRequest(
        issue_iid=78,
        choice=ResolutionChoice.RETRY,
        reason="The trusted prerequisite is now closed.",
    )

    first = build_resolution_plan(request, observation)
    second = build_resolution_plan(request, observation)

    assert first.digest == second.digest
    assert first.issue_iid == 78
    assert first.choice is ResolutionChoice.RETRY
    assert first.applied is False
    assert first.evidence


def test_resolution_apply_rejects_changed_evidence_before_any_mutation() -> None:
    request = ResolutionRequest(
        issue_iid=78,
        choice=ResolutionChoice.RETRY,
        reason="The trusted prerequisite is now closed.",
    )
    reviewed_plan = build_resolution_plan(
        request,
        _observation(
            78, record_state=RunnerState.BLOCKED, dependencies=((35, "closed"),)
        ),
    )

    with pytest.raises(EvidenceDriftError):
        verify_resolution_plan(
            reviewed_plan,
            _observation(
                78, record_state=RunnerState.BLOCKED, dependencies=((35, "opened"),)
            ),
        )


@pytest.mark.parametrize(
    "update",
    [
        {"ledger_revision": 2},
        {"source_branch": "agent/issue-79"},
        {"source_sha": "b" * 40},
        {"worktree_exists": True},
        {"worktree_head": "c" * 40},
        {"worktree_dirty": True},
        {"worktree_changed_paths": ("changed.py",)},
        {"merge_request_iid": 9},
        {"merge_request_state": "closed"},
        {"merge_request_sha": "d" * 40},
        {"pipeline_sha": "e" * 40},
        {"controller_status": "active"},
    ],
)
def test_resolution_drift_rejects_every_bound_material_field(
    update: dict[str, object],
) -> None:
    """Any bound retained evidence change invalidates the reviewed plan."""
    observation = _observation(78, record_state=RunnerState.BLOCKED)
    plan = build_resolution_plan(
        ResolutionRequest(78, ResolutionChoice.RETRY, "Human reviewed retry."),
        observation,
    )
    with pytest.raises(EvidenceDriftError):
        verify_resolution_plan(plan, replace(observation, **update))


@pytest.mark.parametrize(
    "choice",
    [ResolutionChoice.CLOSE, ResolutionChoice.SUPERSEDE],
)
def test_close_and_supersede_plans_require_a_reason_and_linked_evidence(
    choice: ResolutionChoice,
) -> None:
    observation = _observation(
        80, labels=("agent::failed",), record_state=RunnerState.BLOCKED
    )

    with pytest.raises(ValueError):
        build_resolution_plan(
            ResolutionRequest(issue_iid=80, choice=choice, reason=""), observation
        )
    with pytest.raises(ValueError):
        build_resolution_plan(
            ResolutionRequest(
                issue_iid=80, choice=choice, reason="Duplicate outcome confirmed."
            ),
            observation,
        )


def test_relabel_plan_accepts_only_canonical_lifecycle_labels() -> None:
    observation = _observation(
        80, labels=("agent::failed",), record_state=RunnerState.BLOCKED
    )
    request = ResolutionRequest(
        issue_iid=80,
        choice=ResolutionChoice.RELABEL,
        reason="The durable record is awaiting a human review.",
        target_label="agent::awaiting-human",
    )

    assert (
        build_resolution_plan(request, observation).target_label
        == "agent::awaiting-human"
    )
    with pytest.raises(ValueError):
        build_resolution_plan(
            ResolutionRequest(
                issue_iid=80,
                choice=ResolutionChoice.RELABEL,
                reason="Do not introduce an uncontrolled label.",
                target_label="triaged-by-bot",
            ),
            observation,
        )


@given(st.lists(st.integers(min_value=1, max_value=10_000), unique=True, max_size=30))
def test_inventory_order_property(iids: list[int]) -> None:
    """Blocked candidates are IID ordered regardless of input order."""
    observations = tuple(
        _observation(iid, record_state=RunnerState.BLOCKED) for iid in iids
    )
    assert [
        item.issue_iid for item in build_inventory(observations).candidates
    ] == sorted(iids)
    assert [
        item.issue_iid
        for item in build_inventory(tuple(reversed(observations))).candidates
    ] == sorted(iids)


@given(st.text(min_size=1, max_size=80).filter(lambda value: bool(value.strip())))
def test_resolution_digest_property(reason: str) -> None:
    """Reason text is part of a deterministic reviewed-plan digest."""
    assume(reason != "base reason")
    observation = _observation(78, record_state=RunnerState.BLOCKED)
    base = ResolutionRequest(78, ResolutionChoice.RETRY, "base reason")
    changed = ResolutionRequest(78, ResolutionChoice.RETRY, reason)
    assert (
        build_resolution_plan(base, observation).digest
        == build_resolution_plan(base, observation).digest
    )
    assert (
        build_resolution_plan(base, observation).digest
        != build_resolution_plan(changed, observation).digest
    )
