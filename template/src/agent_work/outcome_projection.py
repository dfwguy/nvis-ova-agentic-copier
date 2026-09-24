# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Persist and replay idempotent GitLab outcome projections."""

from __future__ import annotations

from .domain import label_for_state, sanitize_outcome_note_field
from .models import LedgerRecord, OutcomeProjectionRecord, outcome_note_fingerprint
from .ports import GitLabPort, LedgerPort


def plan_outcome_projection(
    record: LedgerRecord,
    *,
    outcome_key: str,
    outcome: str,
    explanation: str,
    evidence: str,
    retained_work: str,
    next_action: str,
    human_decision: str,
) -> LedgerRecord:
    """Prepare one validated projection without altering another intent."""
    supplied = {
        "outcome": outcome,
        "explanation": explanation,
        "evidence": evidence,
        "retained_work": retained_work,
        "next_action": next_action,
        "human_decision": human_decision,
    }
    if any(len(value) > 20_000 for value in supplied.values()):
        raise ValueError("outcome note field exceeds journal limit")
    fields = {
        name: sanitize_outcome_note_field(value) for name, value in supplied.items()
    }
    fingerprint = outcome_note_fingerprint(fields)
    target_label = label_for_state(record.state)
    if target_label is None:
        raise ValueError("state has no outcome label")
    journal = OutcomeProjectionRecord(
        target_state=record.state,
        target_phase=record.phase,
        target_label=target_label,
        outcome_key=outcome_key,
        fingerprint=fingerprint,
        outcome=fields["outcome"],
        explanation=fields["explanation"],
        evidence=fields["evidence"],
        retained_work=fields["retained_work"],
        next_action=fields["next_action"],
        human_decision=fields["human_decision"],
    )
    current = record.outcome_projection
    if current is not None:
        if current == journal or (
            current.model_dump(exclude={"label_complete", "note_complete"})
            == journal.model_dump(exclude={"label_complete", "note_complete"})
        ):
            return record
        if not current.label_complete or not current.note_complete:
            raise ValueError("pending outcome projection cannot be replaced")
    return record.model_copy(update={"outcome_projection": journal})


def replay_outcome_projection(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
) -> LedgerRecord:
    """Replay incomplete label and note steps, persisting each success."""
    record = LedgerRecord.model_validate(record.model_dump(mode="json", warnings=False))
    journal = record.outcome_projection
    if journal is None:
        raise ValueError("no outcome projection to replay")
    if journal.label_complete and journal.note_complete:
        return record
    if gitlab.verify_project().id != record.project_id:
        raise ValueError("outcome projection belongs to another project")
    gitlab.verify_authenticated_user()
    if not journal.label_complete:
        gitlab.transition_labels(record.issue_iid, journal.target_label)
        journal = journal.model_copy(update={"label_complete": True})
        record = ledger.persist(
            record.model_copy(update={"outcome_projection": journal})
        )
    if not journal.note_complete:
        gitlab.upsert_outcome_note(
            record.issue_iid,
            outcome_key=journal.outcome_key,
            outcome=journal.outcome,
            explanation=journal.explanation,
            evidence=journal.evidence,
            retained_work=journal.retained_work,
            next_action=journal.next_action,
            human_decision=journal.human_decision,
        )
        journal = journal.model_copy(update={"note_complete": True})
        record = ledger.persist(
            record.model_copy(update={"outcome_projection": journal})
        )
    return record


def project_outcome(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    outcome_key: str,
    outcome: str,
    explanation: str,
    evidence: str,
    retained_work: str,
    next_action: str,
    human_decision: str,
) -> LedgerRecord:
    """Persist a new outcome intent before touching GitLab and replay it."""
    planned = plan_outcome_projection(
        record,
        outcome_key=outcome_key,
        outcome=outcome,
        explanation=explanation,
        evidence=evidence,
        retained_work=retained_work,
        next_action=next_action,
        human_decision=human_decision,
    )
    if planned is not record:
        if gitlab.verify_project().id != record.project_id:
            raise ValueError("outcome projection belongs to another project")
        planned = ledger.persist(planned)
    return replay_outcome_projection(planned, ledger=ledger, gitlab=gitlab)
