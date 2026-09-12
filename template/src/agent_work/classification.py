# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure, fail-closed classification of untrusted issue data."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import ValidationError

from .domain import WorkClassification, WorkKind
from .models import GitLabIssue, RemediationMarker


class ClassificationError(ValueError):
    """Issue metadata cannot be classified safely."""


_MARKER = re.compile(
    r"<!--\s*agent-mr-remediation\s*\n(?P<body>.*?)\n\s*-->", re.DOTALL
)


def parse_remediation_marker(description: str) -> RemediationMarker | None:
    """Parse exactly one complete remediation marker when its prefix occurs."""
    prefix_count = description.count("agent-mr-remediation")
    matches = tuple(_MARKER.finditer(description))
    if prefix_count == 0:
        return None
    if prefix_count != 1 or len(matches) != 1:
        raise ClassificationError("remediation marker is incomplete or ambiguous")
    values: dict[str, object] = {}
    for line in matches[0]["body"].splitlines():
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ClassificationError("remediation marker contains an invalid field")
        values[key.strip()] = value.strip()
    try:
        return RemediationMarker.model_validate(values)
    except ValidationError as error:
        raise ClassificationError("remediation marker is invalid") from error


def is_protected_policy_path(path: str) -> bool:
    """Return whether a repository path requires a human-directed lane."""
    normalized = PurePosixPath(path).as_posix().removeprefix("./")
    if normalized.startswith("template/"):
        normalized = normalized.removeprefix("template/")
    return normalized in {"AGENTS.md", "CLAUDE.md"} or normalized.startswith(
        (".agents/", ".claude/", ".cursor/")
    )


def classify_work(
    issue: GitLabIssue,
    *,
    project_id: int,
    requested_paths: tuple[str, ...] = (),
) -> WorkClassification:
    """Classify one issue without allowing task text to widen authority."""
    policy_names = ("AGENTS.md", "CLAUDE.md", ".agents/", ".claude/", ".cursor/")
    if any(is_protected_policy_path(path) for path in requested_paths) or any(
        name in issue.description for name in policy_names
    ):
        return WorkClassification(WorkKind.PROTECTED_POLICY_MANUAL)
    marker = parse_remediation_marker(issue.description)
    if marker is not None:
        if marker.project_id != project_id:
            raise ClassificationError("remediation marker belongs to another project")
        kind = (
            WorkKind.MERGE_CONFLICT_REPAIR
            if "merge conflict" in marker.finding.casefold()
            else WorkKind.EXISTING_MR_REMEDIATION
        )
        return WorkClassification(
            kind,
            marker.merge_request_iid,
            marker.source_branch,
            marker.source_sha,
        )
    return WorkClassification(WorkKind.ORDINARY)
