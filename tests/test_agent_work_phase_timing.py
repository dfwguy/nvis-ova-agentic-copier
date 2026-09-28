# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for bounded controller-authored workflow timing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work.cli import _render
from agent_work.domain import RunnerState, WorkflowPhase, WorkKind
from agent_work.models import (
    CommandItemReport,
    CommandReport,
    LedgerRecord,
    PhaseTiming,
)
from agent_work.runtime import _item
from agent_work.timing import (
    append_phase_timing,
    is_bounded_timing_append,
    summarize_phase_timings,
)

STARTED = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


def _timing(
    *,
    phase_or_role: str = "coding-green",
    attempt: int = 1,
    elapsed_seconds: float = 2.5,
    outcome: str = "complete",
    classification: str = "execution",
) -> dict[str, object]:
    """Build one controller timing payload with exact deterministic chronology."""
    return {
        "phase_or_role": phase_or_role,
        "attempt": attempt,
        "started_at": STARTED,
        "finished_at": STARTED + timedelta(seconds=elapsed_seconds),
        "elapsed_seconds": elapsed_seconds,
        "outcome": outcome,
        "classification": classification,
    }


def _record_payload(tmp_path: Path) -> dict[str, object]:
    """Build the smallest valid durable record payload."""
    return {
        "project_id": 310967,
        "issue_iid": 134,
        "state": "executing",
        "phase": "coding-green",
        "work_kind": "ordinary",
        "source_branch": "clanker/record-workflow-phase-timing",
        "worktree": tmp_path / "worktrees" / "134",
    }


def test_phase_timings_accept_exact_bounded_controller_evidence_and_legacy_defaults(
    tmp_path: Path,
) -> None:
    legacy = LedgerRecord.model_validate(_record_payload(tmp_path))
    timed = LedgerRecord.model_validate(
        _record_payload(tmp_path) | {"phase_timings": (_timing(),)}
    )

    assert legacy.phase_timings == ()
    assert timed.phase_timings[0].model_dump() == _timing()


def test_phase_timings_keep_only_a_bounded_newest_64_entry_contract(
    tmp_path: Path,
) -> None:
    bounded = tuple(
        _timing(phase_or_role=f"role-{index}", attempt=index + 1) for index in range(64)
    )
    record = LedgerRecord.model_validate(
        _record_payload(tmp_path) | {"phase_timings": bounded}
    )

    assert len(record.phase_timings) == 64
    with pytest.raises(ValidationError, match="64"):
        LedgerRecord.model_validate(
            _record_payload(tmp_path)
            | {"phase_timings": bounded + (_timing(phase_or_role="overflow"),)}
        )

    appended = append_phase_timing(
        record.phase_timings,
        phase_or_role="newest",
        attempt=65,
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=1),
        outcome="complete",
        classification="execution",
    )
    assert len(appended) == 64
    assert appended[0].phase_or_role == "role-1"
    assert appended[-1].phase_or_role == "newest"


@pytest.mark.parametrize(
    "timing",
    [
        _timing(attempt=0),
        _timing() | {"elapsed_seconds": float("inf")},
        _timing() | {"elapsed_seconds": float("nan")},
        _timing(phase_or_role=""),
        _timing(phase_or_role="x" * 129),
        _timing(outcome=""),
        _timing(outcome="x" * 4_097),
        _timing(classification="model-supplied"),
        _timing()
        | {
            "finished_at": STARTED - timedelta(seconds=1),
            "elapsed_seconds": 1.0,
        },
        _timing() | {"elapsed_seconds": 1.0},
        _timing()
        | {
            "started_at": STARTED.astimezone(timezone(timedelta(hours=2))),
            "finished_at": (STARTED + timedelta(seconds=2.5)).astimezone(
                timezone(timedelta(hours=2))
            ),
        },
    ],
)
def test_phase_timings_reject_unbounded_or_incoherent_evidence(
    tmp_path: Path, timing: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as raised:
        LedgerRecord.model_validate(
            _record_payload(tmp_path) | {"phase_timings": (timing,)}
        )

    assert any(
        error["loc"][:2] == ("phase_timings", 0) for error in raised.value.errors()
    )


def test_phase_timings_accept_finite_duration_above_default_activation_limit(
    tmp_path: Path,
) -> None:
    timing = _timing(elapsed_seconds=10_801)

    record = LedgerRecord.model_validate(
        _record_payload(tmp_path) | {"phase_timings": (timing,)}
    )

    assert record.phase_timings[0].elapsed_seconds == 10_801


def test_phase_timing_accepts_exact_text_boundaries(tmp_path: Path) -> None:
    timing = _timing(phase_or_role="p" * 128, outcome="o" * 4_096)

    record = LedgerRecord.model_validate(
        _record_payload(tmp_path) | {"phase_timings": (timing,)}
    )

    assert len(record.phase_timings[0].phase_or_role) == 128
    assert len(record.phase_timings[0].outcome) == 4_096


@given(
    elapsed_values=st.lists(
        st.integers(min_value=0, max_value=86_400), min_size=0, max_size=64
    ),
    newest_elapsed=st.integers(min_value=0, max_value=86_400),
)
def test_append_phase_timing_property_retains_exact_newest_bounded_window(
    elapsed_values: list[int], newest_elapsed: int
) -> None:
    previous = tuple(
        PhaseTiming.model_validate(
            _timing(
                phase_or_role=f"role-{index}",
                attempt=index + 1,
                elapsed_seconds=float(elapsed),
            )
        )
        for index, elapsed in enumerate(elapsed_values)
    )

    current = append_phase_timing(
        previous,
        phase_or_role="newest",
        attempt=len(previous) + 1,
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=newest_elapsed),
        outcome="complete",
        classification="execution",
    )

    assert len(current) == min(len(previous) + 1, 64)
    assert current[-1].phase_or_role == "newest"
    assert current[:-1] == previous[-63:]
    assert is_bounded_timing_append(previous, current)


@settings(max_examples=40)
@given(
    phase=st.text(min_size=1, max_size=32),
    outcome=st.text(min_size=1, max_size=32),
    elapsed=st.integers(min_value=0, max_value=86_400),
    classification=st.sampled_from(("execution", "wait")),
)
def test_timing_summary_property_reports_observed_subtotal_and_missing_classification(
    phase: str,
    outcome: str,
    elapsed: int,
    classification: Literal["execution", "wait"],
) -> None:
    timing = PhaseTiming(
        phase_or_role=phase,
        attempt=1,
        started_at=STARTED,
        finished_at=STARTED + timedelta(seconds=elapsed),
        elapsed_seconds=float(elapsed),
        outcome=outcome,
        classification=classification,
    )

    summary = summarize_phase_timings((timing,))

    assert not any(character.isspace() for character in summary)
    assert "count=1" in summary
    assert "scope=retained" in summary
    assert "coverage=unknown" in summary
    assert "internal_roles=unavailable" in summary
    assert "history=" not in summary
    if phase == "pipeline-wait" and classification == "wait":
        assert f"legacy_poll={elapsed:.3f}s" in summary
        assert "observed_execution=unavailable" in summary
        assert "observed_wait=unavailable" in summary
        return
    other = "wait" if classification == "execution" else "execution"
    assert f"observed_{classification}={elapsed:.3f}s" in summary
    assert f"observed_{other}=unavailable" in summary


def test_timing_summary_at_retention_limit_reports_possible_truncation() -> None:
    timings = tuple(
        PhaseTiming.model_validate(
            _timing(phase_or_role=f"role-{index}", attempt=index + 1)
        )
        for index in range(64)
    )

    summary = summarize_phase_timings(timings)

    assert "count=64" in summary
    assert "observed_execution=160.000s" in summary
    assert "scope=retained" in summary
    assert "history=possibly-truncated" in summary
    assert "coverage=unknown" in summary
    assert "history=truncated" not in summary


def test_timing_summary_separates_legacy_poll_duration_from_wait() -> None:
    summary = summarize_phase_timings(
        (
            PhaseTiming.model_validate(_timing(elapsed_seconds=2.5)),
            PhaseTiming.model_validate(
                _timing(
                    phase_or_role="pipeline-wait",
                    elapsed_seconds=4.0,
                    classification="wait",
                )
            ),
        )
    )

    assert "observed_execution=2.500s" in summary
    assert "observed_wait=unavailable" in summary
    assert "legacy_poll=4.000s" in summary
    assert "latest=pipeline-wait/complete/4.000s" in summary
    assert "coverage=unknown" in summary


def test_timing_summary_counts_new_pipeline_poll_as_observed_execution() -> None:
    summary = summarize_phase_timings(
        (
            PhaseTiming.model_validate(
                _timing(
                    phase_or_role="pipeline-poll",
                    elapsed_seconds=5.0,
                    classification="execution",
                )
            ),
        )
    )

    assert "observed_execution=5.000s" in summary
    assert "observed_wait=unavailable" in summary
    assert "latest=pipeline-poll/complete/5.000s" in summary
    assert "legacy_poll=" not in summary


def test_bounded_timing_append_rejects_non_appends_and_wrong_full_window_shift() -> (
    None
):
    timings = tuple(
        PhaseTiming.model_validate(
            _timing(phase_or_role=f"role-{index}", attempt=index + 1)
        )
        for index in range(64)
    )
    replacement = PhaseTiming.model_validate(
        _timing(phase_or_role="replacement", attempt=65)
    )

    assert not is_bounded_timing_append((), ())
    assert not is_bounded_timing_append(timings[:3], timings[:3])
    assert not is_bounded_timing_append(timings[:3], (*timings[:2], replacement))
    assert not is_bounded_timing_append(timings, (*timings[:-1], replacement))


def test_status_json_exposes_full_bounded_phase_timings(tmp_path: Path) -> None:
    record = LedgerRecord.model_validate(
        _record_payload(tmp_path)
        | {
            "phase_timings": (
                _timing(),
                _timing(
                    phase_or_role="capacity-wait",
                    elapsed_seconds=4.0,
                    outcome="capacity-available",
                    classification="wait",
                ),
            )
        }
    )

    payload = json.loads(
        _render(
            CommandReport(
                command="loop.status",
                applied=False,
                outcome="status",
                items=(_item(record),),
            ),
            as_json=True,
        )
    )

    assert payload["items"][0]["phase_timings"] == [
        {
            **_timing(),
            "started_at": STARTED.isoformat().replace("+00:00", "Z"),
            "finished_at": (STARTED + timedelta(seconds=2.5))
            .isoformat()
            .replace("+00:00", "Z"),
        },
        {
            **_timing(
                phase_or_role="capacity-wait",
                elapsed_seconds=4.0,
                outcome="capacity-available",
                classification="wait",
            ),
            "started_at": STARTED.isoformat().replace("+00:00", "Z"),
            "finished_at": (STARTED + timedelta(seconds=4.0))
            .isoformat()
            .replace("+00:00", "Z"),
        },
    ]


def test_human_status_adds_one_sanitized_latest_and_aggregate_timing_token() -> None:
    item = CommandItemReport.model_validate(
        {
            "issue_iid": 134,
            "state": RunnerState.EXECUTING,
            "phase": WorkflowPhase.CODING_GREEN,
            "work_kind": WorkKind.ORDINARY,
            "attempt": 1,
            "retries_remaining": 3,
            "source_branch": "clanker/record-workflow-phase-timing",
            "worktree": "/state/worktrees/134",
            "phase_timings": (
                _timing(),
                _timing(
                    phase_or_role="pipeline wait\nprompt",
                    elapsed_seconds=4.0,
                    outcome="success transcript\tdata",
                    classification="wait",
                ),
            ),
        }
    )
    rendered = _render(
        CommandReport(
            command="loop.status",
            applied=False,
            outcome="status",
            items=(item,),
        ),
        as_json=False,
    )

    assert rendered.count("timing=") == 1
    assert "count=2" in rendered
    assert "observed_execution=2.500s" in rendered
    assert "observed_wait=4.000s" in rendered
    assert "scope=retained" in rendered
    assert "coverage=unknown" in rendered
    assert "internal_roles=unavailable" in rendered
    assert "latest=pipeline_wait_prompt/success_transcript_data/4.000s" in rendered
    assert "\n" not in rendered and "\t" not in rendered
    assert "pipeline wait\nprompt" not in rendered
    assert "success transcript\tdata" not in rendered
