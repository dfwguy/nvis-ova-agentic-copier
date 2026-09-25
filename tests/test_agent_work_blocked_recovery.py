# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Red behavior contracts for automatic blocked-record recovery."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.models import LedgerRecord, MergeRequestEvidence, TransitionIntentRecord
from agent_work.serial import recover_blocked_records

NOW = datetime(2026, 9, 25, tzinfo=UTC)


def _blocked(
    tmp_path: Path,
    issue_iid: int,
    **updates: object,
) -> LedgerRecord:
    record = LedgerRecord(
        project_id=310967,
        issue_iid=issue_iid,
        state=RunnerState.BLOCKED,
        phase=WorkflowPhase.PREPARING,
        source_branch=f"agent/issue-{issue_iid}",
        worktree=tmp_path / f"worktree-{issue_iid}",
        source_sha="a" * 40,
    )
    return record.model_copy(update=updates)


def test_recovery_selects_at_most_one_proof_complete_blocked_record(
    tmp_path: Path,
) -> None:
    records = (
        _blocked(tmp_path, 241, blocked_cause="transient runtime failure"),
        _blocked(tmp_path, 242, blocked_cause="transient runtime failure"),
    )

    selected = recover_blocked_records(records, now=NOW)

    assert [record.issue_iid for record in selected] == [241]


@pytest.mark.parametrize(
    "updates",
    (
        {"worktree_dirty": True},
        {"merge_request_iid": 901, "merge_request_state": "opened"},
        {"owner_status": "manual"},
        {"split_prerequisites": ((235, "opened"),)},
    ),
)
def test_recovery_leaves_unsafe_blocked_records_unchanged(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    record = _blocked(tmp_path, 243, **updates)

    selected = recover_blocked_records((record,), now=NOW)

    assert selected == ()
    assert record.state is RunnerState.BLOCKED


def test_recovery_rechecks_lock_and_evidence_before_transition(
    tmp_path: Path,
) -> None:
    record = _blocked(
        tmp_path,
        244,
        ledger_revision=7,
        source_sha="b" * 40,
        intent=TransitionIntentRecord(
            operation_id="244:publish:0",
            kind="publish_merge_request",
            expected_evidence={"source_sha": "b" * 40},
        ),
    )

    selected = recover_blocked_records((record,), now=NOW)

    assert selected == ()
    assert record.intent is not None
    assert record.intent.kind == "publish_merge_request"
    assert record.merge_request_iid is None


def test_recovery_cannot_approve_or_merge_a_publication(
    tmp_path: Path,
) -> None:
    record = _blocked(
        tmp_path,
        245,
        merge_request_iid=902,
        merge_request_url="https://gitlab.example.test/merge_requests/902",
        intent=TransitionIntentRecord(
            operation_id="245:publish:0",
            kind="publish_merge_request",
            expected_evidence={"source_sha": "a" * 40},
        ),
    )
    observed_mr = MergeRequestEvidence(
        iid=902,
        web_url=record.merge_request_url or "",
        state="opened",
        source_project_id=310967,
        target_project_id=310967,
        source_branch=record.source_branch,
        target_branch="main",
        sha=record.source_sha or "",
    )

    selected = recover_blocked_records((record,), now=NOW)

    assert selected == ()
    assert observed_mr.state == "opened"
    assert observed_mr.is_draft is False
