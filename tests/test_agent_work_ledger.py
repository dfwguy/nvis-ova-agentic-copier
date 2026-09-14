# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for XDG-local durable records."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_work.domain import RunnerState, WorkflowPhase, WorkKind
from agent_work.ledger import JsonLedger, LedgerCorrupt, project_state_path
from agent_work.models import LedgerRecord


def _record(root: Path) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=RunnerState.PREPARING,
        phase=WorkflowPhase.DISCOVERY,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=root / "worktrees" / "12",
    )


def test_project_state_path_uses_xdg_and_project_identity(tmp_path: Path) -> None:
    path = project_state_path(
        host="gitlab.example.com",
        project_id=310967,
        environment={"XDG_STATE_HOME": str(tmp_path)},
    )

    assert path.parent == tmp_path / "agent-work" / "projects"
    assert path.name.startswith("310967-")


def test_ledger_round_trips_a_frozen_record_with_restrictive_permissions(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)
    ledger = JsonLedger(tmp_path, clock=lambda: now)
    record = _record(tmp_path)

    ledger.persist(record)

    assert ledger.load(12) == record.model_copy(
        update={
            "schema_version": 2,
            "revision": 1,
            "activation_started_at": now,
            "latest_meaningful_update_at": now,
        }
    )
    assert stat.S_IMODE((tmp_path / "records" / "12.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "records").stat().st_mode) == 0o700


def test_corrupt_record_is_preserved_as_recovery_evidence(tmp_path: Path) -> None:
    record_path = tmp_path / "records" / "12.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("not-json")
    ledger = JsonLedger(tmp_path, clock=lambda: datetime.now(UTC))

    with pytest.raises(LedgerCorrupt):
        ledger.load(12)

    recovery = tuple((tmp_path / "recovery").glob("12-*.json"))
    assert len(recovery) == 1
    payload = json.loads(recovery[0].read_text())
    assert payload["issue_iid"] == 12
    assert payload["error_code"] == "ledger_corrupt"
