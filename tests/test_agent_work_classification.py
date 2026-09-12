# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for safe issue classification."""

from __future__ import annotations

import pytest

from agent_work.classification import (
    ClassificationError,
    classify_work,
    is_protected_policy_path,
    parse_remediation_marker,
)
from agent_work.domain import WorkKind
from agent_work.models import GitLabIssue


def _issue(description: str = "ordinary work") -> GitLabIssue:
    return GitLabIssue(iid=12, title="Work", description=description)


def test_exact_remediation_marker_selects_existing_mr_work() -> None:
    description = """Repair review feedback.

<!-- agent-mr-remediation
project_id: 310967
merge_request_iid: 88
source_branch: review/existing
source_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
finding: judge-quality|src/example.py|clarify recovery
-->
"""

    marker = parse_remediation_marker(description)

    assert marker is not None
    assert marker.merge_request_iid == 88
    assert (
        classify_work(_issue(description), project_id=310967).kind
        is WorkKind.EXISTING_MR_REMEDIATION
    )


def test_foreign_project_remediation_marker_fails_closed() -> None:
    description = """<!-- agent-mr-remediation
project_id: 999
merge_request_iid: 88
source_branch: review/existing
source_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
finding: review
-->"""

    with pytest.raises(ClassificationError, match="project"):
        classify_work(_issue(description), project_id=310967)


def test_protected_policy_lane_takes_precedence_over_remediation() -> None:
    description = """Update AGENTS.md.
<!-- agent-mr-remediation
project_id: 310967
merge_request_iid: 88
source_branch: review/existing
source_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
finding: review
-->"""

    classification = classify_work(_issue(description), project_id=310967)

    assert classification.kind is WorkKind.PROTECTED_POLICY_MANUAL


def test_merge_conflict_marker_selects_conflict_repair() -> None:
    description = """<!-- agent-mr-remediation
project_id: 310967
merge_request_iid: 88
source_branch: review/existing
source_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
finding: merge conflict against main
-->"""

    classification = classify_work(_issue(description), project_id=310967)

    assert classification.kind is WorkKind.MERGE_CONFLICT_REPAIR


@pytest.mark.parametrize(
    "description",
    [
        "<!-- agent-mr-remediation\nproject_id: nope\n-->",
        "<!-- agent-mr-remediation -->\n<!-- agent-mr-remediation -->",
    ],
)
def test_malformed_or_multiple_remediation_markers_fail_closed(
    description: str,
) -> None:
    with pytest.raises(ClassificationError):
        parse_remediation_marker(description)


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "CLAUDE.md",
        ".agents/agents/architect.md",
        ".claude/settings.json",
        ".cursor/rules/tdd.mdc",
        "template/.agents/skills/tdd-red/SKILL.md",
    ],
)
def test_protected_policy_paths_require_the_manual_lane(path: str) -> None:
    assert is_protected_policy_path(path)


def test_ordinary_source_path_is_not_protected_policy() -> None:
    assert not is_protected_policy_path("src/agent_work/domain.py")
