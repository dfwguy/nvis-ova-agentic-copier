# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The controller's worker-read provider checks current durable identity."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work import serial
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.models import LedgerRecord
from tests.test_agent_work_serial_runtime import (
    FakeRepository,
    _config,
    _issue,
    _record,
)

SOURCE_SHA = "a" * 40


class MutableLedger:
    """Expose one current record through the controller's ledger read port."""

    def __init__(self, record: LedgerRecord) -> None:
        self.current: LedgerRecord | None = record
        self.loads: list[int] = []

    def load(self, issue_iid: int) -> LedgerRecord | None:
        self.loads.append(issue_iid)
        return self.current


class HeadTrackingRepository(FakeRepository):
    """Record every exact worktree HEAD check at the fake Git port."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(_config(tmp_path))
        self.head_checks: list[Path] = []

    def head(self, worktree: Path) -> str:
        self.head_checks.append(worktree)
        return self.current_head


def _prepared(
    tmp_path: Path,
) -> tuple[LedgerRecord, MutableLedger, HeadTrackingRepository]:
    record = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(update={"revision": 7})
    return record, MutableLedger(record), HeadTrackingRepository(tmp_path)


def _live_session(
    tmp_path: Path,
    record: LedgerRecord,
    ledger: MutableLedger,
    repository: HeadTrackingRepository,
) -> Any:
    return cast(Any, serial).create_live_worker_read_session(
        record,
        _issue(),
        ledger=ledger,
        repository=repository,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )


def _read(session: Any, action: str) -> dict[str, object]:
    worker_read = importlib.import_module("agent_work.worker_read")
    return cast(
        dict[str, object],
        worker_read.request_worker_read(session.socket_path, session.nonce, action),
    )


def test_live_worker_read_factory_serves_bound_issue_context_and_ledger(
    tmp_path: Path,
) -> None:
    record, ledger, repository = _prepared(tmp_path)
    session = _live_session(tmp_path, record, ledger, repository)
    try:
        context = _read(session, "context")
        issue = _read(session, "issue")
        ledger_view = _read(session, "ledger")
    finally:
        session.close()

    assert context == {
        "state": "executing",
        "phase": "workflow",
        "workflow_lane": "coding",
    }
    assert issue == {"title": "Work", "description": "ordinary"}
    assert ledger_view == {
        "revision": 7,
        "source_branch": "agent/issue-12",
        "source_sha": SOURCE_SHA,
        "state": "executing",
        "phase": "workflow",
    }
    assert ledger.loads == [12, 12, 12]
    assert repository.head_checks == [record.worktree] * 3


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("project_id", 310968),
        ("issue_iid", 13),
        ("worktree", Path("/other/worktree")),
        ("revision", 8),
        ("state", RunnerState.PUBLISHING),
        ("phase", WorkflowPhase.READY_TO_PUBLISH),
        ("workflow_lane", WorkflowLane.CONTENT),
        ("source_branch", "agent/issue-13"),
        ("source_sha", "c" * 40),
    ],
)
def test_live_worker_read_rejects_ledger_identity_drift_before_data(
    tmp_path: Path, field: str, replacement: object
) -> None:
    record, ledger, repository = _prepared(tmp_path)
    session = _live_session(tmp_path, record, ledger, repository)
    ledger.current = record.model_copy(update={field: replacement})
    try:
        with pytest.raises(Exception) as failure:
            _read(session, "issue")
    finally:
        session.close()

    assert ledger.loads == [12]
    assert "ordinary" not in str(failure.value)


def test_live_worker_read_rechecks_record_after_one_successful_read(
    tmp_path: Path,
) -> None:
    record, ledger, repository = _prepared(tmp_path)
    session = _live_session(tmp_path, record, ledger, repository)
    try:
        assert _read(session, "context")["phase"] == "workflow"
        ledger.current = record.model_copy(update={"revision": 8})
        with pytest.raises(Exception, match="worker_read|read failed|identity"):
            _read(session, "ledger")
    finally:
        session.close()

    assert ledger.loads == [12, 12]
    assert repository.head_checks == [record.worktree]


def test_live_worker_read_rejects_git_head_drift_before_data(tmp_path: Path) -> None:
    record, ledger, repository = _prepared(tmp_path)
    session = _live_session(tmp_path, record, ledger, repository)
    repository.current_head = "c" * 40
    try:
        with pytest.raises(Exception) as failure:
            _read(session, "issue")
    finally:
        session.close()

    assert ledger.loads == [12]
    assert repository.head_checks == [record.worktree]
    assert "ordinary" not in str(failure.value)


@pytest.mark.parametrize(
    ("state", "phase"),
    [
        (RunnerState.PUBLISHING, WorkflowPhase.WORKFLOW),
        (RunnerState.EXECUTING, WorkflowPhase.READY_TO_PUBLISH),
    ],
)
def test_live_worker_read_factory_rejects_nonworker_phase(
    tmp_path: Path, state: RunnerState, phase: WorkflowPhase
) -> None:
    record, ledger, repository = _prepared(tmp_path)
    record = record.model_copy(update={"state": state, "phase": phase})
    ledger.current = record

    with pytest.raises((RuntimeError, ValueError), match="worker|phase|identity"):
        _live_session(tmp_path, record, ledger, repository)

    assert ledger.loads == []


def test_live_worker_read_factory_rejects_another_issue(tmp_path: Path) -> None:
    record, ledger, repository = _prepared(tmp_path)

    with pytest.raises((RuntimeError, ValueError), match="worker|issue|identity"):
        cast(Any, serial).create_live_worker_read_session(
            record,
            _issue(iid=13),
            ledger=ledger,
            repository=repository,
            timeout_seconds=10,
            temporary_directory=tmp_path,
        )

    assert ledger.loads == []
