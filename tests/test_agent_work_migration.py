"""Behavior contracts for evidence-preserving migration planning."""

from __future__ import annotations

from agent_work.domain import RunnerState
from agent_work.migration import build_migration_plan
from agent_work.models import MigrationObservation


def test_migration_preview_leaves_unlabelled_issues_untouched() -> None:
    plan = build_migration_plan(
        MigrationObservation(
            project_id=310967,
            issues=({"iid": 1, "state": "opened", "labels": []},),
        )
    )

    assert plan.actions == ()


def test_ambiguous_failed_branch_or_stash_is_planned_as_blocked() -> None:
    plan = build_migration_plan(
        MigrationObservation(
            project_id=310967,
            issues=({"iid": 12, "state": "opened", "labels": ["agent::failed"]},),
            branches=("agent/issue-12",),
            stashes=("agent-work recovery issue-12",),
        )
    )

    assert len(plan.actions) == 1
    assert plan.actions[0].target_state is RunnerState.BLOCKED
    assert "stash" in plan.actions[0].next_action.lower()


def test_stash_evidence_is_associated_with_the_exact_issue_iid() -> None:
    plan = build_migration_plan(
        MigrationObservation(
            project_id=310967,
            issues=(
                {"iid": 1, "state": "opened", "labels": ["agent::failed"]},
                {"iid": 12, "state": "opened", "labels": ["agent::failed"]},
            ),
            stashes=("agent-work recovery issue-12",),
        )
    )

    assert [action.issue_iid for action in plan.actions] == [1, 12]
    assert plan.actions[0].evidence == ()
    assert plan.actions[1].evidence == ("stash",)


def test_all_legacy_lifecycle_projections_and_worktrees_are_inventoried() -> None:
    plan = build_migration_plan(
        MigrationObservation(
            project_id=310967,
            issues=(
                {"iid": 2, "state": "opened", "labels": ["agent::running"]},
                {
                    "iid": 3,
                    "state": "opened",
                    "labels": ["agent::awaiting-human"],
                },
                {"iid": 4, "state": "closed", "labels": ["agent::failed"]},
            ),
            worktrees=("/state/worktrees/2",),
        )
    )

    assert [(action.issue_iid, action.target_state) for action in plan.actions] == [
        (2, RunnerState.BLOCKED),
        (3, RunnerState.AWAITING_HUMAN),
        (4, RunnerState.COMPLETE),
    ]
    assert plan.actions[0].evidence == ("worktree",)
