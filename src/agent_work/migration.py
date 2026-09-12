# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure evidence-preserving migration planning."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from .domain import RunnerState
from .models import MigrationAction, MigrationObservation, MigrationPlan

_LIFECYCLE = frozenset(
    {
        "agent::running",
        "agent::awaiting-human",
        "agent::blocked",
        "agent::failed",
    }
)
_STASH_ISSUE = re.compile(r"\bissue-(?P<iid>[1-9][0-9]*)\b")


def _evidence_iids(observation: MigrationObservation) -> dict[int, set[str]]:
    evidence: dict[int, set[str]] = {}
    for branch in observation.branches:
        suffix = branch.rsplit("-", maxsplit=1)[-1]
        if branch.startswith("agent/issue-") and suffix.isdigit():
            evidence.setdefault(int(suffix), set()).add("branch")
    for worktree in observation.worktrees:
        name = PurePosixPath(worktree).name
        if name.isdigit():
            evidence.setdefault(int(name), set()).add("worktree")
    for stash in observation.stashes:
        match = _STASH_ISSUE.search(stash)
        if match is not None:
            evidence.setdefault(int(match["iid"]), set()).add("stash")
    return evidence


def build_migration_plan(observation: MigrationObservation) -> MigrationPlan:
    """Build a non-mutating plan from legacy issue, branch, and stash evidence."""
    evidence_by_iid = _evidence_iids(observation)
    actions: list[MigrationAction] = []
    for issue in observation.issues:
        iid = issue.get("iid")
        labels = issue.get("labels", [])
        state = issue.get("state")
        if not isinstance(iid, int) or not isinstance(labels, list):
            continue
        lifecycle = _LIFECYCLE.intersection(str(label) for label in labels)
        if not lifecycle:
            continue
        evidence = tuple(
            name
            for name in ("branch", "worktree", "stash")
            if name in evidence_by_iid.get(iid, set())
        )
        if state == "closed":
            target = RunnerState.COMPLETE
            next_action = "Remove stale lifecycle labels from the closed issue."
        elif "agent::awaiting-human" in lifecycle:
            target = RunnerState.AWAITING_HUMAN
            next_action = "Verify the retained merge request before human review."
        else:
            target = RunnerState.BLOCKED
            retained = " and ".join(evidence) if evidence else "GitLab"
            next_action = f"Review the retained {retained} evidence before recovery."
        actions.append(
            MigrationAction(
                issue_iid=iid,
                target_state=target,
                next_action=next_action,
                evidence=evidence,
            )
        )
    return MigrationPlan(project_id=observation.project_id, actions=tuple(actions))
