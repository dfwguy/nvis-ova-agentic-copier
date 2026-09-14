# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure, bounded status projection helpers."""

from __future__ import annotations

from .domain import RunnerState, StatusProjection, WorkflowPhase


def project_status(
    *, state: RunnerState, phase: WorkflowPhase, source_sha: str | None
) -> StatusProjection:
    """Map durable state to one concise human lifecycle without mutation."""
    del phase
    if state is RunnerState.PREPARING and source_sha is None:
        return StatusProjection.SELECTED
    if state is RunnerState.AWAITING_HUMAN:
        return StatusProjection.AWAITING_HUMAN_REVIEW
    if state in {RunnerState.BLOCKED, RunnerState.FAILED}:
        return StatusProjection.BLOCKED
    if state is RunnerState.COMPLETE:
        return StatusProjection.COMPLETE
    return StatusProjection.RUNNING
