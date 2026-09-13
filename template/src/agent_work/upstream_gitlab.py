# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only, redacted GitLab boundary for upstream-finding previews."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .ports import CommandResult, CommandRunner
from .upstream_finding import UpstreamFindingPreview, render_reserved_marker

_RESERVED = "<!-- agent-work-upstream-finding:"
_MAX_TOKEN_BYTES = 4096
_MAX_DESCRIPTION = 10_000
_PAGE_SIZE = 100
_MAX_PAGES = 100


class UpstreamGitLabError(RuntimeError):
    """A sanitized upstream-read failure."""


def _is_hostname(value: str) -> bool:
    """Return whether value is one bounded DNS-style hostname."""
    labels = value.split(".")
    return (
        bool(value)
        and len(value) <= 253
        and all(
            0 < len(label) <= 63
            and label[0].isascii()
            and label[0].isalnum()
            and label[-1].isascii()
            and label[-1].isalnum()
            and all(
                character.isascii() and (character.isalnum() or character == "-")
                for character in label
            )
            for label in labels
        )
    )


@dataclass(frozen=True)
class UpstreamGitLabConfig:
    """Explicit, separate connection values for a read-only upstream."""

    host: str
    project_id: int
    credential_file: Path
    glab_path: Path

    def __post_init__(self) -> None:
        if not _is_hostname(self.host):
            raise ValueError("upstream host is invalid")
        if isinstance(self.project_id, bool) or self.project_id <= 0:
            raise ValueError("upstream project ID is invalid")
        if not self.credential_file.is_absolute():
            raise ValueError("upstream credential file is invalid")
        if not self.glab_path.is_absolute():
            raise ValueError("upstream glab path is invalid")


class _Access(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    access_level: int


class _Permissions(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    project_access: _Access | None
    group_access: _Access | None


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int
    issues_enabled: bool
    permissions: _Permissions


class _Issue(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    iid: int = Field(gt=0)
    state: str
    description: str

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Accept GitLab's nullable issue description."""
        return "" if value is None else value


def _parse(payload: str, model: type[_Project | _Issue] | None) -> object:
    """Decode a bounded API payload without retaining rejected content."""
    try:
        data = json.loads(payload)
        if model is None:
            if not isinstance(data, list):
                raise ValueError
            return tuple(_Issue.model_validate(item) for item in data)
        return model.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        raise UpstreamGitLabError("upstream response is invalid") from None


class UpstreamGitLabReader:
    """Find exact reserved markers without sending inherited identity or secrets."""

    def __init__(
        self,
        config: UpstreamGitLabConfig,
        *,
        read_credential: Callable[[Path], bytes],
        run_command: CommandRunner,
    ) -> None:
        self._config = config
        self._read_credential = read_credential
        self._run_command = run_command

    def _token(self) -> str:
        try:
            value = self._read_credential(self._config.credential_file)
            if (
                not isinstance(value, bytes)
                or not value
                or len(value) > _MAX_TOKEN_BYTES
            ):
                raise ValueError
            token = value.decode("utf-8")
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeDecodeError):
            raise UpstreamGitLabError("upstream credential is invalid") from None
        if (
            "\n" in token
            or "\r" in token
            or not token.strip()
            or token != token.strip()
        ):
            raise UpstreamGitLabError("upstream credential is invalid")
        return token

    def _get(self, endpoint: str, token: str) -> str:
        command = (
            str(self._config.glab_path),
            "api",
            "--hostname",
            self._config.host,
            "-X",
            "GET",
            endpoint,
        )
        try:
            result: CommandResult = self._run_command(
                command, environment={"GITLAB_TOKEN": token}
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            raise UpstreamGitLabError("upstream request unavailable") from None
        if result.returncode != 0:
            raise UpstreamGitLabError("upstream request unavailable")
        return result.stdout

    def preview(self, preview: UpstreamFindingPreview) -> UpstreamFindingPreview:
        """Return a match or would-create result after bounded GET-only reads."""
        token = self._token()
        project = _parse(
            self._get(f"projects/{self._config.project_id}", token), _Project
        )
        assert isinstance(project, _Project)
        levels = tuple(
            access.access_level
            for access in (
                project.permissions.project_access,
                project.permissions.group_access,
            )
            if access is not None
        )
        if project.id != self._config.project_id:
            raise UpstreamGitLabError("upstream project is invalid")
        if not project.issues_enabled:
            raise UpstreamGitLabError("upstream issues are unavailable")
        if max(levels, default=0) < 20:
            raise UpstreamGitLabError("upstream project access is invalid")
        marker = render_reserved_marker(preview)
        seen: dict[int, _Issue] = {}
        matches: list[int] = []
        for page in range(1, _MAX_PAGES + 1):
            issues = _parse(
                self._get(
                    f"projects/{self._config.project_id}/issues?state=opened&per_page={_PAGE_SIZE}&page={page}",
                    token,
                ),
                None,
            )
            assert isinstance(issues, tuple)
            for issue in issues:
                assert isinstance(issue, _Issue)
                if len(issue.description) > _MAX_DESCRIPTION:
                    raise UpstreamGitLabError("upstream response is invalid")
                previous = seen.get(issue.iid)
                if previous is not None:
                    if previous != issue:
                        raise UpstreamGitLabError(
                            "upstream issue evidence is ambiguous"
                        )
                    continue
                seen[issue.iid] = issue
                if issue.state != "opened":
                    continue
                reserved_count = issue.description.count(_RESERVED)
                exact_count = issue.description.count(marker)
                if reserved_count != exact_count or exact_count > 1:
                    raise UpstreamGitLabError("upstream issue evidence is ambiguous")
                if exact_count:
                    matches.append(issue.iid)
            if len(issues) < _PAGE_SIZE:
                break
        else:
            raise UpstreamGitLabError("upstream issue listing is incomplete")
        if len(matches) > 1:
            raise UpstreamGitLabError("upstream issue evidence is ambiguous")
        return replace(
            preview,
            outcome="match" if matches else "would-create",
            matching_iid=matches[0] if matches else None,
        )
