# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Redacted GitLab boundary for upstream-finding preview and explicit apply."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .gitlab import GitLabError, reject_unsafe_issue_text
from .ports import CommandResult, CommandRunner
from .upstream_finding import (
    MAX_ISSUE_DESCRIPTION,
    RESERVED_MARKER_PREFIX,
    PreparedIssueDraft,
    UpstreamFindingPreview,
    render_reserved_marker,
    validate_prepared_issue_draft,
)

_MAX_TOKEN_BYTES = 4096
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


def _is_project_path(value: str) -> bool:
    """Return whether value is a bounded, canonical GitLab namespace path."""
    segments = value.split("/")
    return (
        bool(value)
        and len(value) <= 512
        and all(
            segment not in {"", ".", ".."}
            and all(
                character.isascii() and (character.isalnum() or character in "._-")
                for character in segment
            )
            for segment in segments
        )
    )


@dataclass(frozen=True)
class UpstreamGitLabConfig:
    """Explicit, separate connection values for an upstream boundary."""

    host: str
    project_id: int
    credential_file: Path
    glab_path: Path

    def __post_init__(self) -> None:
        canonical_host = self.host.lower()
        if not _is_hostname(canonical_host):
            raise ValueError("upstream host is invalid")
        object.__setattr__(self, "host", canonical_host)
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
    path_with_namespace: str
    issues_enabled: bool
    permissions: _Permissions

    @field_validator("path_with_namespace")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Require one safe canonical GitLab namespace path."""
        if not _is_project_path(value):
            raise ValueError("path is invalid")
        return value


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


class _CreatedIssue(BaseModel):
    """Strictly projected fields required to verify a created issue."""

    model_config = ConfigDict(extra="ignore", strict=True)
    project_id: int
    iid: int = Field(gt=0)
    state: str
    confidential: bool
    title: str
    description: str
    web_url: str


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


def _parse_create(payload: str) -> _CreatedIssue:
    """Decode only the response fields required for strict create verification."""
    try:
        return _CreatedIssue.model_validate(json.loads(payload))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        raise UpstreamGitLabError("upstream response is invalid") from None


def _verify_created(
    created: _CreatedIssue,
    draft: PreparedIssueDraft,
    config: UpstreamGitLabConfig,
    project_path: str,
) -> None:
    """Reject any create response that is not the exact safe issue projection."""
    try:
        parsed = urlsplit(created.web_url)
    except ValueError:
        raise UpstreamGitLabError("upstream response is invalid") from None
    expected_url = f"https://{config.host}/{project_path}/-/issues/{created.iid}"
    if (
        created.project_id != config.project_id
        or created.state != "opened"
        or created.confidential
        or created.title != draft.title
        or created.description != draft.description
        or parsed.scheme != "https"
        or parsed.hostname != config.host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or created.web_url != expected_url
    ):
        raise UpstreamGitLabError("upstream response is invalid")


class UpstreamGitLabReader:
    """Preview or explicitly apply upstream findings with isolated credentials."""

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

    def _post(self, draft: PreparedIssueDraft, token: str) -> str:
        command = (
            str(self._config.glab_path),
            "api",
            "--hostname",
            self._config.host,
            "-X",
            "POST",
            "-f",
            f"title={draft.title}",
            "-f",
            f"description={draft.description}",
            "-f",
            "confidential=false",
            f"projects/{self._config.project_id}/issues",
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

    def _preflight_and_scan(
        self, preview: UpstreamFindingPreview, token: str
    ) -> tuple[UpstreamFindingPreview, _Project]:
        """Perform full project validation and one bounded exact-marker scan."""
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
        return self._scan(preview, token), project

    def _scan(
        self, preview: UpstreamFindingPreview, token: str
    ) -> UpstreamFindingPreview:
        """Perform one bounded exact-marker scan after project preflight."""
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
                if len(issue.description) > MAX_ISSUE_DESCRIPTION:
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
                reserved_count = issue.description.count(RESERVED_MARKER_PREFIX)
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

    def preview(self, preview: UpstreamFindingPreview) -> UpstreamFindingPreview:
        """Return a match or would-create result after bounded GET-only reads."""
        token = self._token()
        result, _ = self._preflight_and_scan(preview, token)
        return result

    def apply(
        self, preview: UpstreamFindingPreview, draft: PreparedIssueDraft
    ) -> UpstreamFindingPreview:
        """Create one verified issue only after two full exact-marker scans."""
        try:
            reject_unsafe_issue_text(draft.title)
            reject_unsafe_issue_text(draft.description)
            reject_unsafe_issue_text(draft.marker)
        except GitLabError as error:
            raise UpstreamGitLabError(
                "upstream draft contains unsafe linebreak text"
            ) from error
        try:
            validate_prepared_issue_draft(draft)
        except ValueError:
            raise UpstreamGitLabError("upstream draft is invalid") from None
        if draft.preview != preview:
            raise UpstreamGitLabError("upstream draft is invalid")
        token = self._token()
        first, project = self._preflight_and_scan(preview, token)
        if first.outcome == "match":
            return first
        second = self._scan(preview, token)
        if second.outcome == "match":
            return second
        created = _parse_create(self._post(draft, token))
        _verify_created(created, draft, self._config, project.path_with_namespace)
        read_back = _parse_create(
            self._get(f"projects/{self._config.project_id}/issues/{created.iid}", token)
        )
        _verify_created(read_back, draft, self._config, project.path_with_namespace)
        if read_back.iid != created.iid:
            raise UpstreamGitLabError("upstream response is invalid")
        return replace(preview, outcome="created", matching_iid=created.iid)
