# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for compact audit evidence and status projection."""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work.domain import RunnerState, StatusProjection, WorkflowPhase, WorkKind
from agent_work.ledger import JsonLedger, XdgProjectLock, observe_project_lock
from agent_work.models import (
    AuditCapacity,
    AuditReceiptSummary,
    CommandItemReport,
    CommandReport,
    LedgerRecord,
)
from agent_work.runtime import _item
from agent_work.status import project_status


def test_audit_receipt_accepts_bounded_typed_workflow_evidence() -> None:
    summary = AuditReceiptSummary(
        lane="coding",
        capacity=AuditCapacity(required=13, available=16, shortfall=0, decision="run"),
        role_receipts=("architect: accepted", "green: passed"),
        changed_paths=("src/agent_work/runtime.py",),
        tests=("uv run pytest tests/test_agent_work_audit_status.py",),
        reviewers=("tdd-judge: approved",),
        findings=(),
        corrections=(),
    )

    assert summary.lane == "coding"
    assert summary.changed_paths == ("src/agent_work/runtime.py",)


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": "untrusted prompt text"},
        {"transcript": "agent transcript"},
        {"role_receipts": ("x" * 20_001,)},
    ],
)
def test_audit_receipt_rejects_prohibited_or_unbounded_evidence(
    payload: dict[str, object],
) -> None:
    base = {
        "lane": "coding",
        "capacity": {
            "required": 13,
            "available": 16,
            "shortfall": 0,
            "decision": "run",
        },
        "role_receipts": ("architect: accepted",),
        "changed_paths": (),
        "tests": (),
        "reviewers": (),
        "findings": (),
        "corrections": (),
    }

    with pytest.raises(ValidationError):
        AuditReceiptSummary.model_validate(base | payload)


@pytest.mark.parametrize(
    ("state", "phase", "source_sha", "expected"),
    [
        (
            RunnerState.PREPARING,
            WorkflowPhase.DISCOVERY,
            None,
            StatusProjection.SELECTED,
        ),
        (
            RunnerState.PREPARING,
            WorkflowPhase.CODING_GREEN,
            "a" * 40,
            StatusProjection.RUNNING,
        ),
        (
            RunnerState.EXECUTING,
            WorkflowPhase.CODING_GREEN,
            "a" * 40,
            StatusProjection.RUNNING,
        ),
        (
            RunnerState.RETRY_WAIT,
            WorkflowPhase.CODING_GREEN,
            "a" * 40,
            StatusProjection.RUNNING,
        ),
        (
            RunnerState.AWAITING_HUMAN,
            WorkflowPhase.POST_PUBLICATION,
            "a" * 40,
            StatusProjection.AWAITING_HUMAN_REVIEW,
        ),
        (
            RunnerState.BLOCKED,
            WorkflowPhase.CODING_GREEN,
            "a" * 40,
            StatusProjection.BLOCKED,
        ),
        (
            RunnerState.FAILED,
            WorkflowPhase.CODING_GREEN,
            "a" * 40,
            StatusProjection.BLOCKED,
        ),
        (
            RunnerState.COMPLETE,
            WorkflowPhase.POST_PUBLICATION,
            "a" * 40,
            StatusProjection.COMPLETE,
        ),
    ],
)
def test_status_projection_preserves_raw_state_while_reporting_human_lifecycle(
    state: RunnerState,
    phase: WorkflowPhase,
    source_sha: str | None,
    expected: StatusProjection,
) -> None:
    assert project_status(state=state, phase=phase, source_sha=source_sha) is expected


@given(
    state=st.sampled_from(tuple(RunnerState)),
    phase=st.sampled_from(tuple(WorkflowPhase)),
    source_sha=st.one_of(st.none(), st.just("a" * 40)),
)
def test_status_projection_partitions_every_durable_state(
    state: RunnerState, phase: WorkflowPhase, source_sha: str | None
) -> None:
    expected = (
        StatusProjection.SELECTED
        if state is RunnerState.PREPARING and source_sha is None
        else StatusProjection.AWAITING_HUMAN_REVIEW
        if state is RunnerState.AWAITING_HUMAN
        else StatusProjection.BLOCKED
        if state in {RunnerState.BLOCKED, RunnerState.FAILED}
        else StatusProjection.COMPLETE
        if state is RunnerState.COMPLETE
        else StatusProjection.RUNNING
    )
    assert project_status(state=state, phase=phase, source_sha=source_sha) is expected


def test_unprepared_preparing_item_is_selected_even_when_a_branch_exists() -> None:
    assert (
        project_status(
            state=RunnerState.PREPARING,
            phase=WorkflowPhase.CODING_GREEN,
            source_sha=None,
        )
        is StatusProjection.SELECTED
    )


def test_legacy_ledger_record_loads_with_unknown_audit_then_persists_v2_timestamps(
    tmp_path: Path,
) -> None:
    legacy = LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=RunnerState.PREPARING,
        phase=WorkflowPhase.DISCOVERY,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=tmp_path / "worktrees" / "12",
    )
    target = tmp_path / "records" / "12.json"
    target.parent.mkdir()
    target.write_text(json.dumps(legacy.model_dump(mode="json")))
    now = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)

    ledger = JsonLedger(tmp_path, clock=lambda: now)
    loaded = ledger.load(12)
    assert loaded is not None
    assert loaded.audit_evidence == "unknown"

    persisted = ledger.persist(loaded)
    stored = json.loads(target.read_text())
    assert persisted.schema_version == 2
    assert persisted.activation_started_at == now
    assert persisted.latest_meaningful_update_at == now
    assert persisted.completed_at is None
    assert stored["schema_version"] == 2


def test_complete_persist_sets_completion_time_without_replacing_activation_time(
    tmp_path: Path,
) -> None:
    activation = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)
    completed = datetime(2026, 9, 14, 12, 35, tzinfo=UTC)
    moments = iter((activation, completed))
    ledger = JsonLedger(tmp_path, clock=lambda: next(moments))
    prepared = ledger.persist(
        LedgerRecord(
            project_id=310967,
            issue_iid=12,
            state=RunnerState.PREPARING,
            phase=WorkflowPhase.DISCOVERY,
            work_kind=WorkKind.ORDINARY,
            source_branch="agent/issue-12",
            worktree=tmp_path / "worktrees" / "12",
        )
    )

    terminal = ledger.persist(
        prepared.model_copy(
            update={
                "state": RunnerState.COMPLETE,
                "phase": WorkflowPhase.POST_PUBLICATION,
            }
        )
    )

    assert terminal.activation_started_at == activation
    assert terminal.latest_meaningful_update_at == completed
    assert terminal.completed_at == completed


def test_status_models_keep_current_condition_separate_from_failure_history_and_identities() -> (
    None
):
    item = CommandItemReport.model_validate(
        {
            "project_id": 310967,
            "issue_iid": 12,
            "projected_status": "blocked",
            "state": "failed",
            "phase": "coding-green",
            "work_kind": "ordinary",
            "attempt": 2,
            "retries_remaining": 2,
            "source_branch": "agent/issue-12",
            "source_sha": "a" * 40,
            "main_branch": "main",
            "main_sha": "b" * 40,
            "publication_sha": "c" * 40,
            "merge_request_iid": 88,
            "merge_request_url": "https://gitlab.example.test/mr/88",
            "worktree": "/state/worktrees/12",
            "current_condition": {"code": "worker_exit", "detail": "exited"},
            "condition_history": [{"code": "worker_exit", "detail": "exited"}],
            "next_action": "Start a fresh whole workflow and repeat validation.",
        }
    )

    cleared = item.model_copy(update={"current_condition": None})

    assert cleared.current_condition is None
    assert len(cleared.condition_history) == 1
    assert cleared.main_sha == "b" * 40


def test_compact_v2_status_report_caps_general_listing_but_exact_lookup_is_uncapped() -> (
    None
):
    report = CommandReport.model_validate(
        {
            "schema_version": 2,
            "command": "loop.status",
            "applied": False,
            "outcome": "status",
            "total_items": 101,
            "items_truncated": True,
            "items": [],
        }
    )

    assert report.schema_version == 2
    assert report.total_items == 101
    assert report.items_truncated


@pytest.mark.parametrize(
    "capacity",
    [
        {"required": 0, "available": 1, "shortfall": 0, "decision": "run"},
        {"required": 2, "available": 1, "shortfall": 1, "decision": "needs_human"},
        {"required": 2, "available": 1, "shortfall": 1, "decision": "run"},
        {"required": 2, "available": 2, "shortfall": 1, "decision": "run"},
        {"required": 3, "available": 2, "shortfall": 0, "decision": "run"},
    ],
)
def test_audit_capacity_only_accepts_a_successful_usable_run(
    capacity: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        AuditCapacity.model_validate(capacity)


def test_audit_capacity_schema_advertises_only_the_usable_run_decision() -> None:
    assert AuditCapacity.model_json_schema()["properties"]["decision"]["const"] == "run"


def test_project_lock_releases_and_closes_when_owner_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = XdgProjectLock(tmp_path / "project.lock")
    lock.__enter__()
    handle = lock._handle
    assert handle is not None
    descriptor = handle.fileno()
    calls: list[tuple[int, int]] = []

    def failing_unlink(_path: Path, **_kwargs: object) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    monkeypatch.setattr(
        "fcntl.flock", lambda fd, operation: calls.append((fd, operation))
    )

    with pytest.raises(OSError, match="unlink failed"):
        lock.__exit__(None, None, None)

    assert calls == [(descriptor, fcntl.LOCK_UN)]
    assert handle.closed
    assert lock._handle is None


def test_project_lock_removes_owner_marker_when_owner_permissions_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = XdgProjectLock(tmp_path / "project.lock")

    def failing_chmod(_path: Path, _mode: int) -> None:
        raise OSError("chmod failed")

    monkeypatch.setattr(Path, "chmod", failing_chmod)

    with pytest.raises(OSError, match="chmod failed"):
        lock.__enter__()

    assert not lock._owner_marker.exists()
    assert lock._handle is None
    monkeypatch.undo()
    replacement = XdgProjectLock(tmp_path / "project.lock")
    replacement.__enter__()
    replacement.__exit__(None, None, None)


def test_project_lock_preserves_stale_owner_marker_when_owner_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = XdgProjectLock(tmp_path / "project.lock")
    lock._owner_marker.write_text("stale-owner", encoding="ascii")
    original_write_text = Path.write_text

    def failing_owner_write(
        path: Path,
        data: str,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path == lock._owner_marker:
            raise OSError("owner write failed")
        return original_write_text(
            path, data, encoding=encoding, errors=errors, newline=newline
        )

    monkeypatch.setattr(Path, "write_text", failing_owner_write)

    with pytest.raises(OSError, match="owner write failed"):
        lock.__enter__()

    assert lock._owner_marker.read_text(encoding="ascii") == "stale-owner"
    assert lock._handle is None


def test_audit_rejects_blank_and_aggregate_oversized_entries() -> None:
    base = {
        "lane": "content",
        "capacity": {"required": 1, "available": 1, "shortfall": 0, "decision": "run"},
        "role_receipts": ("architect: approved",),
        "changed_paths": (),
        "tests": (),
        "reviewers": (),
        "findings": (),
        "corrections": (),
    }
    with pytest.raises(ValidationError):
        AuditReceiptSummary.model_validate(base | {"tests": (" ",)})
    with pytest.raises(ValidationError):
        AuditReceiptSummary.model_validate(
            base | {"findings": tuple("x" * 4090 for _ in range(9))}
        )


def test_status_projection_rejects_unknown_public_value() -> None:
    with pytest.raises(ValidationError):
        CommandItemReport.model_validate(
            {
                "issue_iid": 1,
                "projected_status": "unbounded",
                "state": "blocked",
                "phase": "workflow",
                "work_kind": "ordinary",
                "attempt": 1,
                "retries_remaining": 3,
                "source_branch": "agent/issue-1",
                "worktree": "/state/worktrees/1",
            }
        )


def test_status_lock_observation_is_passive_and_never_claims_empty_legacy_lock(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "project.lock"
    lock.write_text("")
    assert observe_project_lock(lock, process_alive=lambda _pid: None) == "missing"
    marker = tmp_path / "project.lock.owner"
    marker.write_text("12")
    assert observe_project_lock(lock, process_alive=lambda _pid: True) == "active"
    assert observe_project_lock(lock, process_alive=lambda _pid: False) == "stale"
    marker.write_text("not-a-pid")
    assert observe_project_lock(lock, process_alive=lambda _pid: None) == "unknown"


def test_status_exposes_uncoded_terminal_condition() -> None:
    record = LedgerRecord(
        project_id=1,
        issue_iid=2,
        state=RunnerState.FAILED,
        phase=WorkflowPhase.WORKFLOW,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-2",
        worktree=Path("/state/worktrees/2"),
        summary="A bounded failure summary.",
    )
    item = _item(record)
    assert item.current_condition is not None
    assert item.current_condition.code == "failed_condition"
    assert item.projected_status is StatusProjection.BLOCKED
