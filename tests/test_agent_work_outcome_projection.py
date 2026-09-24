# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for durable, replayable outcome projections."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.domain import (
    RepairAttemptEvidence,
    RunnerState,
    SuggestCloseEvidence,
    WorkflowPhase,
    WorkKind,
    label_for_state,
)
from agent_work.gitlab import GitLabError
from agent_work.ledger import JsonLedger, LedgerCorrupt
from agent_work.models import (
    GitLabIssue,
    GitLabProject,
    LedgerRecord,
    TransitionIntentRecord,
)
from agent_work.ports import GitLabPort, RepositoryPort
from agent_work.serial import select_record
from agent_work.state_machine import transition_record

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
FIELDS = {
    "outcome": "The runner stopped at a reviewed outcome.",
    "explanation": "The issue needs an exact next action.",
    "evidence": "Retained local record and review receipt.",
    "retained_work": "The feature branch remains available.",
    "next_action": "Review the retained evidence.",
    "human_decision": "Choose whether to continue.",
}
NOTE_KEY = "issue-12-outcome"
TARGETS = (
    RunnerState.RETRY_WAIT,
    RunnerState.BLOCKED,
    RunnerState.FAILED,
    RunnerState.NEEDS_REPAIR,
    RunnerState.REPAIR_FAILED,
    RunnerState.SUGGEST_CLOSE,
)


def _record(tmp_path: Path, state: RunnerState) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=state,
        phase=(
            WorkflowPhase.POST_PUBLICATION
            if state
            in {
                RunnerState.NEEDS_REPAIR,
                RunnerState.REPAIR_FAILED,
                RunnerState.SUGGEST_CLOSE,
            }
            else WorkflowPhase.CODING_GREEN
        ),
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=tmp_path / "worktrees" / "12",
        retry_at=NOW + timedelta(minutes=10)
        if state is RunnerState.RETRY_WAIT
        else None,
        repair_attempt=(
            RepairAttemptEvidence(fingerprint="a" * 64)
            if state is RunnerState.REPAIR_FAILED
            else None
        ),
        suggest_close_evidence=(
            SuggestCloseEvidence(
                reason="duplicate", issue_iid=12, evidence=("issue 9",)
            )
            if state is RunnerState.SUGGEST_CLOSE
            else None
        ),
    )


class _Ledger(JsonLedger):
    def __init__(self, root: Path, events: list[str]) -> None:
        super().__init__(root, clock=lambda: NOW)
        self.events = events

    def persist(self, record: LedgerRecord) -> LedgerRecord:
        self.events.append("persist")
        return super().persist(record)


class _GitLab:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_label = False
        self.fail_note = False
        self.notes: list[tuple[int, dict[str, str]]] = []

    def verify_project(self) -> GitLabProject:
        return GitLabProject(
            id=310967,
            host="gitlab.example.com",
            default_branch="main",
            web_url="https://gitlab.example.com/example/project",
        )

    def verify_authenticated_user(self) -> str:
        return "factory-bot"

    def transition_labels(self, iid: int, label: str | None) -> GitLabIssue:
        self.events.append(f"label:{iid}:{label}")
        if self.fail_label:
            raise GitLabError("label transport failed")
        return GitLabIssue(iid=iid, title="Issue 12", labels=(label or "",))

    def upsert_outcome_note(self, iid: int, **fields: str) -> None:
        self.events.append("note")
        if self.fail_note:
            raise GitLabError("note transport failed")
        self.notes.append((iid, fields))


def _api() -> Any:
    from agent_work import outcome_projection

    return outcome_projection


def _plan(record: LedgerRecord, **overrides: str) -> LedgerRecord:
    return _api().plan_outcome_projection(
        record,
        outcome_key=NOTE_KEY,
        **(FIELDS | overrides),
    )


@pytest.mark.parametrize("state", TARGETS)
def test_plan_records_exact_target_label_and_canonical_note_fingerprint(
    tmp_path: Path, state: RunnerState
) -> None:
    record = _record(tmp_path, state)
    planned = _plan(record)
    journal = planned.outcome_projection

    assert planned.state is state and planned.phase is record.phase
    assert journal is not None
    assert journal.target_state is state
    assert journal.target_phase is record.phase
    assert journal.target_label == label_for_state(state)
    assert journal.outcome_key == NOTE_KEY
    assert {name: getattr(journal, name) for name in FIELDS} == FIELDS
    assert (
        journal.fingerprint
        == hashlib.sha256(
            json.dumps(FIELDS, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert not journal.label_complete and not journal.note_complete


@pytest.mark.parametrize("field", tuple(FIELDS))
def test_plan_rejects_unbounded_note_field(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError):
        _plan(_record(tmp_path, RunnerState.BLOCKED), **{field: "x" * 20_001})


@pytest.mark.parametrize("state", (RunnerState.AWAITING_HUMAN, RunnerState.COMPLETE))
def test_plan_rejects_states_outside_outcome_projection(
    tmp_path: Path, state: RunnerState
) -> None:
    with pytest.raises(ValueError):
        _plan(_record(tmp_path, state))


def test_pending_projection_cannot_be_replaced_with_different_note_content(
    tmp_path: Path,
) -> None:
    pending = _plan(_record(tmp_path, RunnerState.BLOCKED))

    with pytest.raises(ValueError):
        _plan(pending, evidence="Different retained evidence.")


def test_projection_persists_before_label_and_replays_after_restart(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    initial = ledger.persist(_record(tmp_path, RunnerState.BLOCKED))
    events.clear()

    result = _api().project_outcome(
        initial, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
    )

    assert events == [
        "persist",
        "label:12:agent::blocked",
        "persist",
        "note",
        "persist",
    ]
    assert ledger.load(12) == result
    assert result.outcome_projection.label_complete
    assert result.outcome_projection.note_complete
    assert gitlab.notes == [(12, {"outcome_key": NOTE_KEY} | FIELDS)]


def test_label_failure_retains_pending_journal_and_retries_both_steps(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    gitlab.fail_label = True
    initial = ledger.persist(_record(tmp_path, RunnerState.FAILED))
    events.clear()

    with pytest.raises(GitLabError, match="label transport"):
        _api().project_outcome(
            initial, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
        )

    pending = ledger.load(12)
    assert pending is not None and pending.outcome_projection is not None
    assert events == ["persist", "label:12:agent::failed"]
    assert not pending.outcome_projection.label_complete
    assert not pending.outcome_projection.note_complete
    gitlab.fail_label = False
    events.clear()

    completed = _api().replay_outcome_projection(pending, ledger=ledger, gitlab=gitlab)

    assert completed.outcome_projection.label_complete
    assert completed.outcome_projection.note_complete
    assert events == ["label:12:agent::failed", "persist", "note", "persist"]


def test_note_failure_replay_preserves_successful_label_step(tmp_path: Path) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    gitlab.fail_note = True
    initial = ledger.persist(_record(tmp_path, RunnerState.NEEDS_REPAIR))
    events.clear()

    with pytest.raises(GitLabError, match="note transport"):
        _api().project_outcome(
            initial, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
        )

    pending = ledger.load(12)
    assert pending is not None and pending.outcome_projection is not None
    assert events == ["persist", "label:12:agent::needs_repair", "persist", "note"]
    assert pending.outcome_projection.label_complete
    assert not pending.outcome_projection.note_complete
    gitlab.fail_note = False
    events.clear()

    completed = _api().replay_outcome_projection(pending, ledger=ledger, gitlab=gitlab)

    assert completed.outcome_projection.note_complete
    assert events == ["note", "persist"]
    assert gitlab.notes == [(12, {"outcome_key": NOTE_KEY} | FIELDS)]


def test_completed_projection_is_noop_and_changed_payload_resets_steps(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    initial = ledger.persist(_record(tmp_path, RunnerState.BLOCKED))
    completed = _api().project_outcome(
        initial, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
    )
    events.clear()

    unchanged = _api().project_outcome(
        completed, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
    )

    assert unchanged == completed
    assert events == []
    changed = _api().project_outcome(
        unchanged,
        ledger=ledger,
        gitlab=gitlab,
        outcome_key=NOTE_KEY,
        **(FIELDS | {"evidence": "New exact evidence."}),
    )
    assert (
        changed.outcome_projection.fingerprint
        != completed.outcome_projection.fingerprint
    )
    assert events == [
        "persist",
        "label:12:agent::blocked",
        "persist",
        "note",
        "persist",
    ]


def test_pending_identical_projection_and_unrelated_intent_are_preserved(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    gitlab.fail_note = True
    intent = TransitionIntentRecord(operation_id="other-op", kind="other-kind")
    initial = ledger.persist(
        _record(tmp_path, RunnerState.BLOCKED).model_copy(update={"intent": intent})
    )
    with pytest.raises(GitLabError):
        _api().project_outcome(
            initial, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
        )
    pending = ledger.load(12)
    assert pending is not None
    gitlab.fail_note = False
    events.clear()

    completed = _api().project_outcome(
        pending, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
    )

    assert completed.intent == intent
    assert events == ["note", "persist"]


@pytest.mark.parametrize("field", ("target_state", "target_phase", "fingerprint"))
def test_replay_rejects_unvalidated_in_memory_journal_before_remote_mutation(
    tmp_path: Path, field: str
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    record = _plan(_record(tmp_path, RunnerState.BLOCKED))
    journal = record.outcome_projection
    assert journal is not None
    corrupt = record.model_copy(
        update={"outcome_projection": journal.model_copy(update={field: "invalid"})}
    )

    with pytest.raises(ValueError):
        _api().replay_outcome_projection(corrupt, ledger=ledger, gitlab=gitlab)

    assert events == []


def test_projection_rejects_wrong_project_before_remote_mutation(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    record = ledger.persist(
        _record(tmp_path, RunnerState.BLOCKED).model_copy(update={"project_id": 999})
    )
    original_bytes = (tmp_path / "records" / "12.json").read_bytes()
    events.clear()

    with pytest.raises(ValueError):
        _api().project_outcome(
            record, ledger=ledger, gitlab=gitlab, outcome_key=NOTE_KEY, **FIELDS
        )

    assert events == []
    assert (tmp_path / "records" / "12.json").read_bytes() == original_bytes
    assert ledger.load(12) == record


def test_plan_rejects_outcome_key_beyond_gateway_byte_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _api().plan_outcome_projection(
            _record(tmp_path, RunnerState.BLOCKED),
            outcome_key="é" * 300,
            **FIELDS,
        )


def test_projection_journals_exact_sanitized_note_payload(tmp_path: Path) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    supplied = FIELDS | {
        "outcome": "glpat-secret123",
        "explanation": "\x1b[31mReady\x1b[0m",
        "evidence": "x" * 600,
        "retained_work": "",
        "next_action": "   ",
        "human_decision": "Choose\nnow",
    }
    expected = {
        "outcome": "[REDACTED]",
        "explanation": "Ready",
        "evidence": "x" * 512,
        "retained_work": "Not recorded.",
        "next_action": "Not recorded.",
        "human_decision": "Choose now",
    }

    result = _api().project_outcome(
        _record(tmp_path, RunnerState.BLOCKED),
        ledger=ledger,
        gitlab=gitlab,
        outcome_key=NOTE_KEY,
        **supplied,
    )

    journal = result.outcome_projection
    assert journal is not None
    assert {name: getattr(journal, name) for name in FIELDS} == expected
    assert (
        journal.fingerprint
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert gitlab.notes == [(12, {"outcome_key": NOTE_KEY} | expected)]
    reloaded = ledger.load(12)
    assert reloaded is not None
    assert "glpat-secret123" not in reloaded.model_dump_json()


@pytest.mark.parametrize(
    "field",
    ("target_state", "target_phase", "target_label", "fingerprint"),
)
def test_tampered_journal_fails_closed_on_ledger_decode(
    tmp_path: Path, field: str
) -> None:
    ledger = _Ledger(tmp_path, [])
    record = ledger.persist(_plan(_record(tmp_path, RunnerState.BLOCKED)))
    path = tmp_path / "records" / "12.json"
    payload = json.loads(path.read_text())
    payload["outcome_projection"][field] = "invalid"
    path.write_text(json.dumps(payload))

    with pytest.raises(LedgerCorrupt):
        ledger.load(record.issue_iid)


def test_selector_drains_future_retry_projection_before_scheduling_work(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    pending = ledger.persist(_plan(_record(tmp_path, RunnerState.RETRY_WAIT)))
    events.clear()

    selected, issue = select_record(
        ledger,
        cast("GitLabPort", gitlab),
        now=NOW,
        repository=cast("RepositoryPort", object()),
    )

    assert (selected, issue) == (None, None)
    reloaded = ledger.load(12)
    assert reloaded is not None and reloaded.outcome_projection is not None
    assert reloaded.outcome_projection.note_complete
    assert events == ["label:12:agent::running", "persist", "note", "persist"]
    assert reloaded.state is pending.state


def test_completed_retry_projection_survives_due_retry_transition_and_reload(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    ledger = _Ledger(tmp_path, events)
    gitlab = _GitLab(events)
    retry = _record(tmp_path, RunnerState.RETRY_WAIT).model_copy(
        update={
            "prior_state": RunnerState.EXECUTING,
            "prior_phase": WorkflowPhase.CODING_GREEN,
        }
    )
    completed = _api().project_outcome(
        ledger.persist(retry),
        ledger=ledger,
        gitlab=gitlab,
        outcome_key=NOTE_KEY,
        **FIELDS,
    )

    resumed = ledger.persist(
        transition_record(
            completed,
            RunnerState.EXECUTING,
            phase=WorkflowPhase.CODING_GREEN,
            retry_at=None,
        )
    )

    assert resumed.outcome_projection is None
    assert ledger.load(12) == resumed


def test_pending_projection_cannot_be_dropped_by_state_transition(
    tmp_path: Path,
) -> None:
    pending = _plan(_record(tmp_path, RunnerState.RETRY_WAIT))

    with pytest.raises(ValueError, match="pending outcome projection"):
        transition_record(pending, RunnerState.EXECUTING)
