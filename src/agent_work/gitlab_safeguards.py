# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure validation of GitLab identity and human-review safeguard payloads."""

from __future__ import annotations

import re
from typing import cast

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class SafeguardPayloadError(ValueError):
    """GitLab safeguard evidence is missing or invalid."""


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")
    return cast("dict[str, object]", value)


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def username_from_payload(value: object) -> str:
    """Return the authenticated username without assuming a prior identity."""
    username = _mapping(value).get("username")
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")
    return username


def default_branch_from_project(
    value: object, *, project_id: int, require_issues: bool = False
) -> str:
    """Require the numeric project, Developer access, and optional issue capability."""
    project = _mapping(value)
    branch = project.get("default_branch")
    permissions = project.get("permissions")
    if not isinstance(permissions, dict):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")
    permissions = cast("dict[str, object]", permissions)
    levels = tuple(
        level
        for key in ("project_access", "group_access")
        if isinstance((entry := permissions.get(key)), dict)
        and (level := _integer(cast("dict[str, object]", entry).get("access_level")))
        is not None
    )
    if (
        _integer(project.get("id")) != project_id
        or not isinstance(branch, str)
        or not branch
        or max(levels, default=0) != 30
        or (
            require_issues
            and (
                project.get("issues_enabled") is not True
                or project.get("issues_access_level", "enabled") != "enabled"
            )
        )
    ):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")
    return branch


def verify_default_branch(value: object, *, branch: str) -> None:
    """Require the effective default branch to block Developer changes."""
    payload = _mapping(value)
    if not (
        payload.get("name") == branch
        and payload.get("default") is True
        and payload.get("protected") is True
        and payload.get("developers_can_push") is False
        and payload.get("developers_can_merge") is False
        and payload.get("can_push") is False
    ):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")


def _global_approval_rule(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    rule = cast("dict[str, object]", value)
    required = _integer(rule.get("approvals_required"))
    return (
        required is not None
        and required >= 1
        and (
            "protected_branches" not in rule
            or rule["protected_branches"] == []
            or rule.get("applies_to_all_protected_branches") is True
        )
    )


def verify_approval_rules(value: object) -> None:
    """Require one globally applicable rule with at least one approval."""
    if not isinstance(value, list) or not any(
        _global_approval_rule(rule) for rule in value
    ):
        raise SafeguardPayloadError("GitLab safeguard evidence is invalid")
