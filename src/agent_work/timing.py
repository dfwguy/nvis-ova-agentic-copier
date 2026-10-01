# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure helpers for bounded controller-authored workflow timing."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, get_args

from .models import (
    LedgerRecord,
    PhaseTiming,
    StageTimingIdentity,
    StageTimingProvenance,
    StageTimingReason,
    StageTimingReport,
    StageTimingStage,
)

MAX_PHASE_TIMINGS = 64


def append_phase_timing(
    timings: tuple[PhaseTiming, ...],
    *,
    phase_or_role: str,
    attempt: int,
    started_at: datetime,
    finished_at: datetime,
    outcome: str,
    classification: Literal["execution", "wait"],
) -> tuple[PhaseTiming, ...]:
    """Append one validated timing and retain only the newest entries."""
    timing = PhaseTiming(
        phase_or_role=phase_or_role,
        attempt=attempt,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_seconds=(finished_at - started_at).total_seconds(),
        outcome=outcome,
        classification=classification,
    )
    return (*timings, timing)[-MAX_PHASE_TIMINGS:]


def is_bounded_timing_append(
    previous: tuple[PhaseTiming, ...], current: tuple[PhaseTiming, ...]
) -> bool:
    """Return whether current is exactly one append with newest-64 pruning."""
    if not current or len(current) != min(len(previous) + 1, MAX_PHASE_TIMINGS):
        return False
    retained = previous[-(MAX_PHASE_TIMINGS - 1) :]
    return current[:-1] == retained


def summarize_phase_timings(timings: tuple[PhaseTiming, ...]) -> str:
    """Return one compact human-safe aggregate token value."""
    execution = []
    wait = []
    legacy_poll = []
    for timing in timings:
        if timing.phase_or_role == "pipeline-wait" and timing.classification == "wait":
            legacy_poll.append(timing.elapsed_seconds)
        elif timing.classification == "execution":
            execution.append(timing.elapsed_seconds)
        else:
            wait.append(timing.elapsed_seconds)
    latest = timings[-1]
    latest_phase = re.sub(r"[^A-Za-z0-9._:-]+", "_", latest.phase_or_role)
    latest_outcome = re.sub(r"[^A-Za-z0-9._:-]+", "_", latest.outcome)
    parts = [
        f"count={len(timings)}",
        "scope=retained",
        "coverage=unknown",
        "internal_roles=unavailable",
        f"observed_execution={sum(execution):.3f}s"
        if execution
        else "observed_execution=unavailable",
        f"observed_wait={sum(wait):.3f}s" if wait else "observed_wait=unavailable",
    ]
    if legacy_poll:
        parts.append(f"legacy_poll={sum(legacy_poll):.3f}s")
    if len(timings) == MAX_PHASE_TIMINGS:
        parts.append("history=possibly-truncated")
    parts.append(
        f"latest={latest_phase}/{latest_outcome}/{latest.elapsed_seconds:.3f}s"
    )
    return ",".join(parts)


def append_record_timing(
    record: LedgerRecord,
    *,
    phase_or_role: str,
    attempt: int,
    started_at: datetime,
    finished_at: datetime,
    outcome: str,
    classification: Literal["execution", "wait"],
    identity: StageTimingIdentity | None,
    boundary_revision: int,
    clock_available: bool = True,
) -> LedgerRecord:
    """Retain one observation without inventing chronology or lost history."""
    try:
        if not clock_available:
            raise ValueError("clock unavailable")
        row = PhaseTiming(
            phase_or_role=phase_or_role,
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=(finished_at - started_at).total_seconds(),
            outcome=outcome,
            classification=classification,
            provenance=StageTimingProvenance(
                identity=identity, boundary_revision=boundary_revision
            )
            if identity is not None
            else None,
        )
    except (TypeError, ValueError, OverflowError):
        reasons = tuple(
            reason
            for reason in get_args(StageTimingReason)
            if reason in (*record.phase_timing_unavailable_reasons, "clock_unavailable")
        )
        return record.model_copy(
            update={
                "phase_timing_count": None,
                "phase_timing_unavailable_reasons": reasons,
            }
        )
    count = record.phase_timing_count
    if (
        count is None
        and not record.phase_timings
        and not record.phase_timing_unavailable_reasons
    ):
        count = 0
    return record.model_copy(
        update={
            "phase_timings": (*record.phase_timings, row)[-MAX_PHASE_TIMINGS:],
            "phase_timing_count": count + 1 if count is not None else None,
        }
    )


def stage_timing_identity(record: LedgerRecord) -> StageTimingIdentity | None:
    """Resolve only the latest controller-issued context matching the record."""
    if not record.source_sha or not record.phase_timings:
        return None
    provenance = record.phase_timings[-1].provenance
    if provenance is None:
        return None
    identity = provenance.identity
    return (
        identity
        if (identity.project_id, identity.issue_iid, identity.source_sha)
        == (record.project_id, record.issue_iid, record.source_sha)
        else None
    )


def stage_timing_report(
    record: LedgerRecord, *, expected_identity: StageTimingIdentity | None
) -> StageTimingReport:
    """Aggregate verified coarse rows while withholding incomplete totals."""
    reasons: set[StageTimingReason] = set(record.phase_timing_unavailable_reasons)
    reasons.add("internal_roles_unavailable")
    context_matches = expected_identity is not None and record.source_sha is not None
    if not context_matches:
        reasons.add("missing_identity")
    elif expected_identity is not None and (
        expected_identity.project_id,
        expected_identity.issue_iid,
        expected_identity.source_sha,
    ) != (record.project_id, record.issue_iid, record.source_sha):
        reasons.add("identity_mismatch")
        context_matches = False
    count = record.phase_timing_count
    retained = len(record.phase_timings)
    if count is None:
        reasons.add("unknown_history")
    elif count < retained or count > retained and retained < MAX_PHASE_TIMINGS:
        reasons.add("invalid_history")
    elif count > MAX_PHASE_TIMINGS:
        reasons.add("truncated_history")
    groups: dict[str, tuple[int, float]] = {}
    previous_revision = -1
    previous_finish: datetime | None = None
    for original in record.phase_timings:
        try:
            row = PhaseTiming.model_validate(original.model_dump())
        except (TypeError, ValueError):
            reasons.add("invalid_history")
            continue
        if type(original.attempt) is not int or original.attempt <= 0:
            reasons.add("invalid_history")
            continue
        chronology_valid = previous_finish is None or row.started_at >= previous_finish
        if not chronology_valid:
            reasons.add("invalid_history")
        previous_finish = (
            row.finished_at
            if previous_finish is None
            else max(previous_finish, row.finished_at)
        )
        provenance = row.provenance
        if provenance is None:
            reasons.add("missing_provenance")
            continue
        revision_valid = provenance.boundary_revision > previous_revision
        if not revision_valid:
            reasons.add("invalid_history")
        previous_revision = max(previous_revision, provenance.boundary_revision)
        if not context_matches or provenance.identity != expected_identity:
            if context_matches:
                reasons.add("identity_mismatch")
            continue
        if row.classification != "execution":
            reasons.add("unsupported_wait")
            continue
        if not chronology_valid or not revision_valid:
            continue
        observations, elapsed = groups.get(row.phase_or_role, (0, 0.0))
        groups[row.phase_or_role] = (observations + 1, elapsed + row.elapsed_seconds)
    stages = tuple(
        StageTimingStage(
            phase_or_role=label,
            classification="execution",
            observations=count,
            elapsed_seconds=elapsed,
        )
        for label, (count, elapsed) in groups.items()
    )
    complete = bool(stages) and reasons == {"internal_roles_unavailable"}
    return StageTimingReport(
        identity=expected_identity,
        coverage="partial" if stages else "unavailable",
        history_complete=complete,
        stages=stages,
        execution_seconds=sum(stage.elapsed_seconds for stage in stages)
        if complete
        else None,
        wait_seconds=None,
        unavailable_reasons=tuple(
            reason for reason in get_args(StageTimingReason) if reason in reasons
        ),
    )


def render_stage_timing_report(report: StageTimingReport) -> str:
    """Render a bounded safe controller summary without unknown-as-zero totals."""
    execution = (
        "unavailable"
        if report.execution_seconds is None
        else f"{report.execution_seconds:.3f}s"
    )
    stages = ",".join(
        f"{re.sub(r'[^A-Za-z0-9._:-]+', '_', stage.phase_or_role)}:{stage.observations}/{stage.elapsed_seconds:.3f}s"
        for stage in report.stages
    )
    return (
        f"controller_stage_timing coverage={report.coverage} history_complete={report.history_complete} "
        f"observed_execution={execution} observed_wait=unavailable "
        f"internal_roles=unavailable external_human_wait=unavailable "
        f"stages={stages or 'unavailable'} reasons={','.join(report.unavailable_reasons)}"
    )
