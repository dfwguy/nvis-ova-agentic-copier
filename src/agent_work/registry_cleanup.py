# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only assessment of a GitLab container-registry cleanup policy."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, ValidationError

from .ports import CommandRunner


class RegistryBoundaryError(RuntimeError):
    """The configured GitLab project could not be safely read."""


class _ExpirationPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    enabled: bool | None = None
    cadence: str | None = None
    keep_n: int | None = None
    older_than: str | None = None
    name_regex_delete: str | None = None
    name_regex: str | None = None
    name_regex_keep: str | None = None


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int
    web_url: str
    container_registry_access_level: Literal["enabled", "private", "disabled"] = (
        "disabled"
    )
    container_expiration_policy: _ExpirationPolicy | None = None


@dataclass(frozen=True)
class RegistryCleanupPolicy:
    """Immutable normalized project cleanup-policy observation."""

    project_id: int
    web_url: str
    registry_access: Literal["enabled", "private", "disabled"]
    enabled: bool | None
    cadence: str | None
    keep_n: int | None
    older_than: str | None
    name_regex_delete: str | None
    name_regex: str | None
    name_regex_keep: str | None

    @classmethod
    def from_project(cls, value: Mapping[str, object]) -> RegistryCleanupPolicy:
        project = _Project.model_validate(value)
        policy = project.container_expiration_policy or _ExpirationPolicy()
        return cls(
            project.id,
            project.web_url,
            project.container_registry_access_level,
            policy.enabled,
            policy.cadence,
            policy.keep_n,
            policy.older_than,
            policy.name_regex_delete,
            policy.name_regex,
            policy.name_regex_keep,
        )


@dataclass(frozen=True)
class CleanupAssessment:
    """Pure read-only cleanup-policy assessment result."""

    healthy: bool
    outcome: Literal["not_applicable", "advisory", "healthy"]
    exit_code: int
    human_message: str
    json_payload: dict[str, object]
    effective_delete_regex: str | None = None


def fetch_registry_cleanup_policy(
    *,
    host: str,
    project_id: str,
    glab_path: Path,
    run_command: CommandRunner,
) -> RegistryCleanupPolicy:
    """Read one configured numeric GitLab project through the injected boundary."""
    if not project_id.isdecimal() or int(project_id) <= 0:
        raise RegistryBoundaryError("project id must be numeric")
    if not glab_path.is_absolute() or not host:
        raise RegistryBoundaryError("registry cleanup configuration is invalid")
    result = run_command(
        (str(glab_path), "api", "--hostname", host, f"projects/{project_id}"),
        timeout_seconds=30,
    )
    if result.returncode != 0:
        raise RegistryBoundaryError("registry cleanup project read failed")
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise TypeError
        policy = RegistryCleanupPolicy.from_project(payload)
    except (json.JSONDecodeError, TypeError, ValidationError, ValueError):
        raise RegistryBoundaryError(
            "registry cleanup project response is invalid"
        ) from None
    parsed = urlparse(policy.web_url)
    if policy.project_id != int(project_id):
        raise RegistryBoundaryError("project identity did not match")
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != host.lower()
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
    ):
        raise RegistryBoundaryError("project host or web_url did not match")
    return policy


def evaluate_cleanup_policy(policy: RegistryCleanupPolicy) -> CleanupAssessment:
    """Return a pure, non-mutating policy assessment."""
    delete_regex = (
        policy.name_regex_delete
        if policy.name_regex_delete is not None
        else policy.name_regex
    )
    if policy.registry_access == "disabled":
        return CleanupAssessment(
            False,
            "not_applicable",
            0,
            "Container registry is disabled; cleanup policy is not applicable.",
            _payload("not_applicable", False, policy),
            delete_regex,
        )
    complete = all(
        (
            policy.enabled is True,
            policy.cadence,
            policy.keep_n is not None,
            policy.older_than,
            delete_regex,
        )
    )
    deletes_transient = _regex_status(delete_regex, _TRANSIENT_TAG) == "matches"
    keep_status = _regex_status(policy.name_regex_keep, _TRANSIENT_TAG)
    keep_safe = keep_status in {"absent", "does_not_match"}
    healthy = bool(complete and deletes_transient and keep_safe)
    if healthy:
        return CleanupAssessment(
            True,
            "healthy",
            0,
            "Container registry cleanup policy covers transient validation tags.",
            _payload("healthy", True, policy),
            delete_regex,
        )
    fields = (
        f"container_registry_access_level={policy.registry_access}",
        f"enabled={policy.enabled}",
        f"cadence={policy.cadence}",
        f"keep_n={policy.keep_n}",
        f"older_than={policy.older_than}",
        f"name_regex_delete={policy.name_regex_delete}",
        f"name_regex={policy.name_regex}",
        f"name_regex_keep={policy.name_regex_keep}",
    )
    message = "\n".join(
        (
            *fields,
            "Review cleanup policy for $CI_COMMIT_SHA-$CI_PIPELINE_ID and usable mutable BuildKit cache.",
            "Maintainer/Owner: use "
            + policy.web_url.rstrip("/")
            + "/-/settings/packages_and_registries",
            "Settings → Packages and registries → Container registry → Cleanup policies",
        )
    )
    return CleanupAssessment(
        False,
        "advisory",
        0,
        message,
        _payload("advisory", False, policy, list(fields)),
        delete_regex,
    )


def _regex_status(
    pattern: str | None, value: str
) -> Literal["absent", "matches", "does_not_match", "invalid"]:
    """Classify a nullable regular expression without treating invalid as absent."""
    if pattern is None:
        return "absent"
    try:
        return (
            "matches" if re.fullmatch(pattern, value) is not None else "does_not_match"
        )
    except re.error:
        return "invalid"


_TRANSIENT_TAG = "a" * 40 + "-123"


def _payload(
    outcome: str,
    healthy: bool,
    policy: RegistryCleanupPolicy,
    observed: list[str] | None = None,
) -> dict[str, object]:
    """Render stable successful JSON with the observed project settings."""
    payload: dict[str, object] = {
        "outcome": outcome,
        "healthy": healthy,
        "applied": False,
        "read_only": True,
        "settings_url": policy.web_url.rstrip("/")
        + "/-/settings/packages_and_registries",
        "observed": {
            "container_registry_access_level": policy.registry_access,
            "enabled": policy.enabled,
            "cadence": policy.cadence,
            "keep_n": policy.keep_n,
            "older_than": policy.older_than,
            "name_regex_delete": policy.name_regex_delete,
            "name_regex": policy.name_regex,
            "name_regex_keep": policy.name_regex_keep,
        },
    }
    if observed is not None:
        payload["advisory"] = observed
    return payload
