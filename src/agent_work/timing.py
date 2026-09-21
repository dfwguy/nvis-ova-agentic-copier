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
from typing import Literal

from .models import PhaseTiming

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
    execution = sum(
        timing.elapsed_seconds
        for timing in timings
        if timing.classification == "execution"
    )
    wait = sum(
        timing.elapsed_seconds for timing in timings if timing.classification == "wait"
    )
    latest = timings[-1]
    latest_phase = re.sub(r"[^A-Za-z0-9._:-]+", "_", latest.phase_or_role)
    latest_outcome = re.sub(r"[^A-Za-z0-9._:-]+", "_", latest.outcome)
    return (
        f"count={len(timings)},execution={execution:.3f}s,wait={wait:.3f}s,"
        f"latest={latest_phase}/{latest_outcome}/{latest.elapsed_seconds:.3f}s"
    )
