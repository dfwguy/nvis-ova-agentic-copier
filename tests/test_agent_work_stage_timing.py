# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Coarse controller observations require exact provenance and honest coverage."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work import models, timing
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.models import LedgerRecord, PhaseTiming

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PROJECT = 310967
ISSUE = 12
SHA = "a" * 40
TREE = "b" * 40


def identity() -> models.StageTimingIdentity:
    """Return exact controller source and candidate identity."""
    return models.StageTimingIdentity(
        project_id=PROJECT, issue_iid=ISSUE, source_sha=SHA, candidate_tree=TREE
    )


def record(root: Path = Path("/synthetic-fixture")) -> LedgerRecord:
    """Metadata-only record; no helper accesses this path."""
    return LedgerRecord(
        project_id=PROJECT,
        issue_iid=ISSUE,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.CODING_GREEN,
        workflow_lane=WorkflowLane.CODING,
        source_branch="agent/issue-12",
        source_sha=SHA,
        worktree=root,
    )


def history(count: int = 2, *, gap_seconds: int = 10) -> LedgerRecord:
    """Append real production timing records with distinct boundary revisions."""
    current = record()
    for index in range(count):
        start = NOW + timedelta(seconds=index * gap_seconds)
        current = timing.append_record_timing(
            current,
            phase_or_role="coding-green",
            attempt=1,
            started_at=start,
            finished_at=start + timedelta(seconds=2),
            outcome="complete",
            classification="execution",
            identity=identity(),
            boundary_revision=index,
        )
        current = current.model_copy(update={"revision": index + 1})
    return current


def report_control() -> models.StageTimingReport:
    """Prove a complete coarse positive envelope before every denial mutation."""
    result = timing.stage_timing_report(history(), expected_identity=identity())
    assert result.identity == identity() and result.coverage == "partial"
    assert result.history_complete is True and result.execution_seconds == 4
    assert result.wait_seconds is None
    assert (
        result.internal_roles == "unavailable"
        and result.external_human_wait == "unavailable"
    )
    assert len(result.stages) == 1 and result.stages[0].observations == 2
    assert result.stages[0].elapsed_seconds == 4
    assert result.unavailable_reasons == ("internal_roles_unavailable",)
    return result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", True),
        ("project_id", 0),
        ("issue_iid", -1),
        ("issue_iid", "12"),
        ("source_sha", "bad"),
        ("candidate_tree", "f" * 39),
        ("extra", "worker grant"),
    ],
)
def test_stage_identity_boundary_rejects_one_invalid_dimension(
    field: str, value: object
) -> None:
    valid = identity()
    data = valid.model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        models.StageTimingIdentity.model_validate(data)


@pytest.mark.parametrize("revision", [True, -1, "0"])
def test_boundary_revision_is_strict_controller_counter(revision: object) -> None:
    valid = models.StageTimingProvenance(identity=identity(), boundary_revision=0)
    data = valid.model_dump()
    data["boundary_revision"] = revision
    with pytest.raises(ValidationError):
        models.StageTimingProvenance.model_validate(data)


@pytest.mark.parametrize("count", [True, -1, "2"])
def test_retained_observation_counter_rejects_invalid_boundary_numbers(
    count: object,
) -> None:
    valid = history()
    data = valid.model_dump()
    data["phase_timing_count"] = count
    with pytest.raises(ValidationError):
        LedgerRecord.model_validate(data)


@pytest.mark.parametrize(
    "field", ["project_id", "issue_iid", "source_sha", "candidate_tree"]
)
def test_one_foreign_observation_cannot_inherit_complete_total(field: str) -> None:
    report_control()
    bound = history()
    altered = identity().model_copy(
        update={
            field: PROJECT + 1
            if field == "project_id"
            else ISSUE + 1
            if field == "issue_iid"
            else "c" * 40
        }
    )
    row = bound.phase_timings[-1]
    provenance = models.StageTimingProvenance(identity=altered, boundary_revision=1)
    row = row.model_copy(update={"provenance": provenance})
    changed = bound.model_copy(update={"phase_timings": (bound.phase_timings[0], row)})
    result = timing.stage_timing_report(changed, expected_identity=identity())
    assert result.execution_seconds is None and result.history_complete is False
    assert "identity_mismatch" in result.unavailable_reasons
    assert sum(stage.elapsed_seconds for stage in result.stages) == 2


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("provenance", "missing_provenance"),
        ("counter", "unknown_history"),
        ("count-small", "invalid_history"),
        ("count-large", "invalid_history"),
        ("duplicate-revision", "invalid_history"),
        ("reversed", "invalid_history"),
        ("overlap", "invalid_history"),
        ("clock", "clock_unavailable"),
    ],
)
def test_single_history_loss_prevents_full_total_without_discarding_all_verified_rows(
    fault: str, reason: str
) -> None:
    report_control()
    bound = history()
    if fault == "provenance":
        bound = bound.model_copy(
            update={
                "phase_timings": (
                    bound.phase_timings[0],
                    bound.phase_timings[1].model_copy(update={"provenance": None}),
                )
            }
        )
    elif fault in {"counter", "count-small", "count-large"}:
        bound = bound.model_copy(
            update={
                "phase_timing_count": {
                    "counter": None,
                    "count-small": 1,
                    "count-large": 3,
                }[fault]
            }
        )
    elif fault == "duplicate-revision":
        row = bound.phase_timings[1].model_copy(
            update={
                "provenance": models.StageTimingProvenance(
                    identity=identity(), boundary_revision=0
                )
            }
        )
        bound = bound.model_copy(
            update={"phase_timings": (bound.phase_timings[0], row)}
        )
    elif fault == "reversed":
        bound = bound.model_copy(
            update={"phase_timings": tuple(reversed(bound.phase_timings))}
        )
    elif fault == "overlap":
        row = bound.phase_timings[1].model_copy(
            update={
                "started_at": NOW + timedelta(seconds=1),
                "finished_at": NOW + timedelta(seconds=3),
            }
        )
        bound = bound.model_copy(
            update={"phase_timings": (bound.phase_timings[0], row)}
        )
    else:
        bound = bound.model_copy(
            update={"phase_timing_unavailable_reasons": ("clock_unavailable",)}
        )
    result = timing.stage_timing_report(bound, expected_identity=identity())
    assert result.execution_seconds is None and result.history_complete is False
    assert reason in result.unavailable_reasons
    assert result.wait_seconds is None

    expected_rows = (
        1 if fault in {"provenance", "duplicate-revision", "reversed", "overlap"} else 2
    )
    assert sum(stage.observations for stage in result.stages) == expected_rows
    assert sum(stage.elapsed_seconds for stage in result.stages) == 2 * expected_rows


@pytest.mark.parametrize("count", [63, 64, 65])
def test_actual_retention_count_distinguishes_full_64_from_pruned_history(
    count: int,
) -> None:
    report_control()
    bound = history(count)
    data = bound.model_dump()
    assert data["phase_timing_count"] == count and len(bound.phase_timings) == min(
        count, 64
    )
    result = timing.stage_timing_report(bound, expected_identity=identity())
    assert result.history_complete is (count <= 64)
    if count <= 64:
        assert result.execution_seconds == 2 * count
        assert "truncated_history" not in result.unavailable_reasons
    else:
        assert (
            result.execution_seconds is None
            and "truncated_history" in result.unavailable_reasons
        )
        assert sum(stage.observations for stage in result.stages) == 64


@pytest.mark.parametrize("identity_available", [False, True])
def test_legacy_nonempty_history_never_invents_counter_or_verified_provenance(
    *, identity_available: bool
) -> None:
    report_control()
    legacy = record().model_copy(
        update={
            "phase_timings": (
                PhaseTiming(
                    phase_or_role="coding-green",
                    attempt=1,
                    started_at=NOW,
                    finished_at=NOW + timedelta(seconds=2),
                    elapsed_seconds=2,
                    outcome="complete",
                    classification="execution",
                ),
            )
        }
    )
    appended = timing.append_record_timing(
        legacy,
        phase_or_role="coding-green",
        attempt=1,
        started_at=NOW + timedelta(seconds=10),
        finished_at=NOW + timedelta(seconds=12),
        outcome="complete",
        classification="execution",
        identity=identity() if identity_available else None,
        boundary_revision=3,
    )
    assert appended.model_dump()["phase_timing_count"] is None
    result = timing.stage_timing_report(appended, expected_identity=identity())
    assert (
        result.execution_seconds is None
        and "unknown_history" in result.unavailable_reasons
    )
    assert "missing_provenance" in result.unavailable_reasons


@pytest.mark.parametrize("fault", ["unavailable", "backward"])
def test_missing_or_invalid_clock_does_not_append_fabricated_zero(fault: str) -> None:
    report_control()
    bound = history()
    changed = timing.append_record_timing(
        bound,
        phase_or_role="coding-green",
        attempt=1,
        started_at=NOW + timedelta(seconds=30),
        finished_at=NOW if fault == "backward" else NOW + timedelta(seconds=30),
        outcome="complete",
        classification="execution",
        identity=identity(),
        boundary_revision=4,
        clock_available=fault != "unavailable",
    )
    assert changed.phase_timings == bound.phase_timings
    result = timing.stage_timing_report(changed, expected_identity=identity())
    assert (
        result.execution_seconds is None
        and "clock_unavailable" in result.unavailable_reasons
    )


@pytest.mark.parametrize("missing", ["expected", "record-source"])
def test_missing_source_identity_is_unavailable_instead_of_zero(missing: str) -> None:
    report_control()
    bound = history()
    if missing == "record-source":
        bound = bound.model_copy(update={"source_sha": None})
    result = timing.stage_timing_report(
        bound, expected_identity=None if missing == "expected" else identity()
    )
    assert result.execution_seconds is None and result.wait_seconds is None
    assert "missing_identity" in result.unavailable_reasons


@given(
    gap=st.integers(min_value=3, max_value=1_000_000),
    count=st.integers(min_value=1, max_value=20),
)
def test_pure_coarse_total_excludes_all_intercall_gaps(gap: int, count: int) -> None:
    bound = history(count, gap_seconds=gap)
    result = timing.stage_timing_report(bound, expected_identity=identity())
    assert result.execution_seconds == 2 * count and result.wait_seconds is None
    assert result.history_complete is True
    assert sum(stage.observations for stage in result.stages) == count


def test_wait_claim_is_not_an_external_human_duration() -> None:
    report_control()
    bound = history()
    row = bound.phase_timings[-1].model_copy(
        update={"classification": "wait", "phase_or_role": "capacity-wait"}
    )
    result = timing.stage_timing_report(
        bound.model_copy(update={"phase_timings": (bound.phase_timings[0], row)}),
        expected_identity=identity(),
    )
    assert result.execution_seconds is None and result.wait_seconds is None
    assert "unsupported_wait" in result.unavailable_reasons
    assert all(stage.classification == "execution" for stage in result.stages)


def test_corrected_source_and_reason_rendering_cannot_reuse_old_totals() -> None:
    report_control()
    changed = history().model_copy(update={"source_sha": "d" * 40})
    result = timing.stage_timing_report(
        changed,
        expected_identity=identity().model_copy(update={"source_sha": "d" * 40}),
    )
    assert result.coverage == "unavailable" and result.execution_seconds is None
    assert "identity_mismatch" in result.unavailable_reasons
    rendered = timing.render_stage_timing_report(result)
    assert "identity_mismatch" in rendered and "internal_roles" in rendered
    assert "<script>" not in rendered


@settings(max_examples=20)
@given(
    count=st.integers(min_value=1, max_value=70),
    duration=st.integers(min_value=1, max_value=30),
)
def test_generated_append_preserves_record_retention_and_clock_refusal(
    count: int, duration: int
) -> None:
    bound = history(count, gap_seconds=40)
    original = bound.model_dump()
    start = NOW + timedelta(seconds=count * 40)
    result = timing.append_record_timing(
        bound,
        phase_or_role="pipeline-poll",
        attempt=2,
        started_at=start,
        finished_at=start + timedelta(seconds=duration),
        outcome="pending",
        classification="execution",
        identity=identity(),
        boundary_revision=count,
    )
    assert bound.model_dump() == original
    assert result.phase_timings[:-1] == bound.phase_timings[-63:]
    assert len(result.phase_timings) == min(count + 1, 64)
    assert result.phase_timing_count == count + 1
    assert result.phase_timings[-1].elapsed_seconds == duration
    assert result.state == bound.state and result.summary == bound.summary
    legacy = bound.model_copy(update={"phase_timing_count": None})
    unknown = timing.append_record_timing(
        legacy,
        phase_or_role="pipeline-poll",
        attempt=2,
        started_at=start,
        finished_at=start + timedelta(seconds=duration),
        outcome="pending",
        classification="execution",
        identity=identity(),
        boundary_revision=count,
    )
    assert unknown.phase_timing_count is None
    refused = timing.append_record_timing(
        bound,
        phase_or_role="pipeline-poll",
        attempt=2,
        started_at=start,
        finished_at=start + timedelta(seconds=duration),
        outcome="pending",
        classification="execution",
        identity=identity(),
        boundary_revision=count,
        clock_available=False,
    )
    assert (
        refused.phase_timings == bound.phase_timings
        and refused.phase_timing_count is None
    )
    assert "clock_unavailable" in refused.phase_timing_unavailable_reasons


@settings(max_examples=20)
@given(
    project=st.integers(min_value=1, max_value=1_000_000),
    issue=st.integers(min_value=1, max_value=1_000_000),
    digit=st.sampled_from(tuple("0123456789abcdef")),
)
def test_generated_identity_matches_latest_proof_and_rejects_single_record_mutation(
    project: int, issue: int, digit: str
) -> None:
    expected = identity().model_copy(
        update={"project_id": project, "issue_iid": issue, "source_sha": digit * 40}
    )
    bound = history().model_copy(
        update={"project_id": project, "issue_iid": issue, "source_sha": digit * 40}
    )
    row = bound.phase_timings[-1].model_copy(
        update={
            "provenance": models.StageTimingProvenance(
                identity=expected, boundary_revision=1
            )
        }
    )
    bound = bound.model_copy(update={"phase_timings": (bound.phase_timings[0], row)})
    original = bound.model_dump()
    assert timing.stage_timing_identity(bound) == expected
    for field, value in (
        ("project_id", project + 1),
        ("issue_iid", issue + 1),
        ("source_sha", ("a" if digit != "a" else "b") * 40),
    ):
        assert (
            timing.stage_timing_identity(bound.model_copy(update={field: value}))
            is None
        )
    unbound = row.model_copy(update={"provenance": None})
    assert (
        timing.stage_timing_identity(
            bound.model_copy(
                update={"phase_timings": (bound.phase_timings[0], unbound)}
            )
        )
        is None
    )
    assert (
        timing.stage_timing_identity(bound.model_copy(update={"phase_timings": ()}))
        is None
    )
    assert bound.model_dump() == original


@settings(max_examples=20)
@given(
    durations=st.lists(st.integers(min_value=1, max_value=10), min_size=2, max_size=12),
    gap=st.integers(min_value=11, max_value=1_000_000),
)
def test_generated_report_has_independent_sums_and_excludes_one_unbound_row(
    durations: list[int], gap: int
) -> None:
    bound = record()
    for index, duration in enumerate(durations):
        start = NOW + timedelta(seconds=index * gap)
        bound = timing.append_record_timing(
            bound,
            phase_or_role="coding-green",
            attempt=1,
            started_at=start,
            finished_at=start + timedelta(seconds=duration),
            outcome="complete",
            classification="execution",
            identity=identity(),
            boundary_revision=index,
        )
        bound = bound.model_copy(update={"revision": index + 1})
    original = bound.model_dump()
    result = timing.stage_timing_report(bound, expected_identity=identity())
    assert result.history_complete is True and result.execution_seconds == sum(
        durations
    )
    assert sum(stage.observations for stage in result.stages) == len(durations)
    assert sum(stage.elapsed_seconds for stage in result.stages) == sum(durations)
    assert result.wait_seconds is None
    bad = bound.phase_timings[-1].model_copy(update={"provenance": None})
    changed = bound.model_copy(
        update={"phase_timings": (*bound.phase_timings[:-1], bad)}
    )
    refused = timing.stage_timing_report(changed, expected_identity=identity())
    assert refused.execution_seconds is None and refused.history_complete is False
    assert "missing_provenance" in refused.unavailable_reasons
    assert sum(stage.observations for stage in refused.stages) == len(durations) - 1
    assert sum(stage.elapsed_seconds for stage in refused.stages) == sum(durations[:-1])
    assert bound.model_dump() == original


@settings(max_examples=20)
@given(
    label=st.text(alphabet="abcXYZ012._:- /\n\r\t界<>", min_size=1, max_size=50),
    count=st.integers(min_value=1, max_value=20),
)
def test_generated_render_is_deterministic_sanitized_and_preserves_unknowns(
    label: str, count: int
) -> None:
    bound = history(count)
    report = timing.stage_timing_report(bound, expected_identity=identity())
    stage = report.stages[0].model_copy(update={"phase_or_role": label})
    report = report.model_copy(update={"stages": (stage,)})
    original = report.model_dump()
    rendered = timing.render_stage_timing_report(report)
    assert rendered == timing.render_stage_timing_report(report)
    assert "\n" not in rendered and "\r" not in rendered and "\t" not in rendered
    assert f"observed_execution={2 * count:.3f}s" in rendered
    assert (
        "observed_wait=unavailable" in rendered
        and "internal_roles=unavailable" in rendered
    )
    unknown = report.model_copy(
        update={"execution_seconds": None, "history_complete": False}
    )
    unavailable = timing.render_stage_timing_report(unknown)
    assert "observed_execution=unavailable" in unavailable
    assert f"observed_execution={2 * count:.3f}s" not in unavailable
    assert report.model_dump() == original
