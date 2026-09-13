# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only upstream GitLab finding boundary."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from agent_work.repository import CommandResult
from agent_work.upstream_finding import UpstreamFindingPreview, render_reserved_marker
from agent_work.upstream_gitlab import (
    UpstreamGitLabConfig,
    UpstreamGitLabError,
    UpstreamGitLabReader,
)


def _preview() -> UpstreamFindingPreview:
    """Return the already-validated, content-free manifest summary."""
    return UpstreamFindingPreview(
        schema_version=1,
        command="upstream-finding.preview",
        outcome="valid",
        project_fingerprint="a" * 64,
        revision_fingerprint="b" * 64,
        semantic_finding_fingerprint="c" * 64,
        copied_path_count=2,
        evidence_link_count=1,
    )


def _config() -> UpstreamGitLabConfig:
    """Return a dedicated, explicit upstream connection boundary."""
    return UpstreamGitLabConfig(
        host="upstream.example.com",
        project_id=77,
        credential_file=Path("/dedicated/upstream-token"),
        glab_path=Path("/opt/bin/glab"),
    )


def _project(*, identifier: int = 77, access: int = 20, enabled: bool = True) -> str:
    """Return the smallest valid project preflight response."""
    return (
        f'{{"id":{identifier},"issues_enabled":{str(enabled).lower()},'
        f'"permissions":{{"project_access":{{"access_level":{access}}},'
        '"group_access":null}}'
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("host", "", id="empty-host"),
        pytest.param("host", "https://upstream.example.com", id="host-scheme"),
        pytest.param("host", "upstream.example.com/group", id="host-path"),
        pytest.param("host", "upstream..example.com", id="empty-host-label"),
        pytest.param("host", "-upstream.example.com", id="leading-host-hyphen"),
        pytest.param("host", "upstream-.example.com", id="trailing-host-hyphen"),
        pytest.param("host", "upstréam.example.com", id="unicode-host-label"),
        pytest.param("host", "ｅxample.com", id="confusable-host-label"),
        pytest.param("project_id", 0, id="zero-project"),
        pytest.param("project_id", -1, id="negative-project"),
        pytest.param(
            "credential_file", Path("relative-token"), id="relative-token-path"
        ),
        pytest.param("glab_path", Path("glab"), id="relative-glab-path"),
    ],
)
def test_upstream_config_rejects_unsafe_public_boundary_values(
    field: str, value: str | int | Path
) -> None:
    payload: dict[str, object] = {
        "host": "upstream.example.com",
        "project_id": 77,
        "credential_file": Path("/dedicated/upstream-token"),
        "glab_path": Path("/opt/bin/glab"),
    }
    payload[field] = value
    with pytest.raises(ValueError):
        UpstreamGitLabConfig(**payload)


@given(
    st.lists(
        st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", fullmatch=True),
        min_size=1,
        max_size=4,
    )
)
def test_upstream_config_accepts_every_generated_bare_hostname(
    labels: list[str],
) -> None:
    """A public hostname is accepted only as bare DNS labels."""
    host = ".".join(labels)
    assume(len(host) <= 253)

    assert (
        UpstreamGitLabConfig(
            host=host,
            project_id=77,
            credential_file=Path("/dedicated/upstream-token"),
            glab_path=Path("/opt/bin/glab"),
        ).host
        == host
    )


def test_reserved_marker_is_canonical_and_exact_open_match_reports_only_iid() -> None:
    preview = _preview()
    marker = render_reserved_marker(preview)
    assert marker == (
        "<!-- agent-work-upstream-finding:v1 project="
        + "a" * 64
        + " revision="
        + "b" * 64
        + " semantic="
        + "c" * 64
        + " -->"
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        return CommandResult(
            0,
            '[{"iid":31,"state":"opened","description":"untrusted prose '
            + marker
            + '"}]',
            "",
        )

    result = UpstreamGitLabReader(
        _config(),
        read_credential=lambda _path: b"dedicated-token",
        run_command=run,
    ).preview(preview)

    assert result.outcome == "match"
    assert result.matching_iid == 31
    assert len(calls) == 2
    assert calls[0][0] == (
        "/opt/bin/glab",
        "api",
        "--hostname",
        "upstream.example.com",
        "-X",
        "GET",
        "projects/77",
    )
    assert calls[1][0][-1] == "projects/77/issues?state=opened&per_page=100&page=1"
    for _command, kwargs in calls:
        assert kwargs["environment"] == {"GITLAB_TOKEN": "dedicated-token"}


def test_reader_projects_owned_fields_and_normalizes_nullable_issue_descriptions() -> (
    None
):
    """Normal GitLab response extras and nullable descriptions do not abort scans."""

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "projects/77":
            return CommandResult(
                0,
                _project()[:-1] + ',"name":"upstream","namespace":{"id":4}}',
                "",
            )
        return CommandResult(
            0,
            '[{"iid":31,"state":"opened","description":null,'
            '"author":{"username":"someone"}}]',
            "",
        )

    result = UpstreamGitLabReader(
        _config(),
        read_credential=lambda _path: b"dedicated-token",
        run_command=run,
    ).preview(_preview())

    assert result.outcome == "would-create"


@pytest.mark.parametrize(
    ("issues", "expected"),
    [
        pytest.param("[]", "would-create", id="no-exact-marker"),
        pytest.param(
            '[{"iid":1,"state":"opened","description":"<!-- agent-work-upstream-finding:v1 project='
            + "a" * 64
            + " revision=bad semantic="
            + "c" * 64
            + ' -->"}]',
            "error",
            id="malformed-reserved-marker",
        ),
        pytest.param(
            '[{"iid":1,"state":"opened","description":"MARKER"},'
            '{"iid":2,"state":"opened","description":"MARKER"}]',
            "error",
            id="ambiguous-exact-matches",
        ),
        pytest.param(
            '[{"iid":1,"state":"closed","description":"MARKER"}]',
            "would-create",
            id="closed-marker-does-not-match",
        ),
    ],
)
def test_open_issue_marker_classification_fails_closed_without_echoing_prose(
    issues: str, expected: str
) -> None:
    preview = _preview()
    marker = render_reserved_marker(preview)

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            _project()
            if command[-1] == "projects/77"
            else issues.replace("MARKER", marker),
            "",
        )

    reader = UpstreamGitLabReader(
        _config(),
        read_credential=lambda _path: b"token",
        run_command=run,
    )
    if expected == "error":
        with pytest.raises(UpstreamGitLabError) as raised:
            reader.preview(preview)
        assert "agent-work-upstream-finding" not in str(raised.value)
        assert "description" not in str(raised.value)
    else:
        assert reader.preview(preview).outcome == expected


@pytest.mark.parametrize(
    ("project", "failure"),
    [
        pytest.param(_project(identifier=78), "project", id="mismatched-id"),
        pytest.param(_project(access=19), "access", id="low-access"),
        pytest.param(_project(enabled=False), "issues", id="issues-disabled"),
        pytest.param("not-json", "response", id="invalid-json"),
    ],
)
def test_project_preflight_rejects_untrusted_or_insufficient_project_before_listing(
    project: str, failure: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, project, "")

    with pytest.raises(UpstreamGitLabError, match=failure):
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        ).preview(_preview())
    assert [command[-1] for command in calls] == ["projects/77"]


def test_reader_paginates_and_rejects_duplicate_iids_or_page_overflow() -> None:
    marker = render_reserved_marker(_preview())
    calls: list[tuple[str, ...]] = []
    page = (
        "["
        + ",".join(
            f'{{"iid":{iid},"state":"opened","description":""}}'
            for iid in range(1, 101)
        )
        + "]"
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        if command[-1].endswith("page=1"):
            return CommandResult(0, page, "")
        return CommandResult(
            0, '[{"iid":101,"state":"opened","description":"' + marker + '"}]', ""
        )

    assert (
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        )
        .preview(_preview())
        .matching_iid
        == 101
    )
    assert [command[-1] for command in calls] == [
        "projects/77",
        "projects/77/issues?state=opened&per_page=100&page=1",
        "projects/77/issues?state=opened&per_page=100&page=2",
    ]


def test_credentials_and_boundary_failures_are_sanitized() -> None:
    token = "glpat-private-token"
    path = "/dedicated/private-token"
    reader = UpstreamGitLabReader(
        _config(),
        read_credential=lambda _path: token.encode(),
        run_command=lambda *_args, **_kwargs: CommandResult(
            1, "", f"failed {token} {path}"
        ),
    )
    with pytest.raises(UpstreamGitLabError) as raised:
        reader.preview(_preview())
    assert not any(value in str(raised.value) for value in (token, path, "failed"))
    assert re.fullmatch(r"upstream [a-z ]+", str(raised.value))


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"token\nsecond", id="multiline"),
        pytest.param(b"x" * 4097, id="oversize"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
def test_credential_boundary_rejects_bad_tokens_before_any_subprocess(
    credential: bytes,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, _project(), "")

    with pytest.raises(UpstreamGitLabError) as raised:
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: credential,
            run_command=run,
        ).preview(_preview())
    assert calls == []
    assert "token" not in str(raised.value).casefold()


def test_credential_reader_failure_is_sanitized_before_any_subprocess() -> None:
    calls: list[tuple[str, ...]] = []

    def unreadable(_path: Path) -> bytes:
        raise PermissionError("/dedicated/secret-token is unavailable")

    with pytest.raises(UpstreamGitLabError) as raised:
        UpstreamGitLabReader(
            _config(),
            read_credential=unreadable,
            run_command=lambda command, **_kwargs: (
                calls.append(command) or CommandResult(0, "", "")
            ),
        ).preview(_preview())
    assert calls == []
    assert "secret-token" not in str(raised.value)


@pytest.mark.parametrize(
    "project",
    [
        pytest.param(
            '{"id":77,"issues_enabled":true,"permissions":'
            '{"project_access":null,"group_access":{"access_level":20}}}',
            id="group-reporter",
        ),
        pytest.param(
            '{"id":77,"issues_enabled":true,"permissions":'
            '{"project_access":{"access_level":20},"group_access":null}}',
            id="project-reporter",
        ),
    ],
)
def test_project_preflight_accepts_reporter_access_from_either_scope(
    project: str,
) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(0, project if command[-1] == "projects/77" else "[]", "")

    assert (
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        )
        .preview(_preview())
        .outcome
        == "would-create"
    )


@pytest.mark.parametrize(
    "project",
    [
        pytest.param('{"id":77,"issues_enabled":true}', id="missing-permissions"),
        pytest.param(
            '{"id":77,"issues_enabled":true,"permissions":null}', id="null-permissions"
        ),
        pytest.param(
            '{"id":77,"issues_enabled":true,"permissions":'
            '{"project_access":{"access_level":"20"},"group_access":null}}',
            id="noninteger-access",
        ),
    ],
)
def test_project_preflight_rejects_missing_or_malformed_access_schema(
    project: str,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, project, "")

    with pytest.raises(UpstreamGitLabError):
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        ).preview(_preview())
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(CommandResult(1, "", "untrusted stderr"), id="nonzero"),
        pytest.param(CommandResult(0, "not-json", ""), id="invalid-json"),
        pytest.param(CommandResult(0, "{}", ""), id="invalid-project-schema"),
    ],
)
def test_project_api_failures_are_redacted_and_do_not_list_issues(
    response: CommandResult,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return response

    with pytest.raises(UpstreamGitLabError) as raised:
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        ).preview(_preview())
    assert len(calls) == 1
    assert "stderr" not in str(raised.value)


@pytest.mark.parametrize(
    "issues",
    [
        pytest.param("{}", id="non-list-schema"),
        pytest.param('[{"iid":1,"state":"opened"}]', id="missing-description"),
        pytest.param(
            '[{"iid":1,"state":"opened","description":"' + "x" * 10_001 + '"}]',
            id="oversize-description",
        ),
    ],
)
def test_open_issue_schema_failures_are_redacted(issues: str) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0, _project() if command[-1] == "projects/77" else issues, ""
        )

    with pytest.raises(UpstreamGitLabError) as raised:
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=run,
        ).preview(_preview())
    assert "x" * 100 not in str(raised.value)


def test_duplicate_iid_conflict_and_full_page_limit_fail_closed() -> None:
    page = (
        "["
        + ",".join(
            f'{{"iid":{iid},"state":"opened","description":""}}'
            for iid in range(1, 101)
        )
        + "]"
    )
    calls: list[tuple[str, ...]] = []

    def overflow(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(
            0, _project() if command[-1] == "projects/77" else page, ""
        )

    with pytest.raises(UpstreamGitLabError):
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=overflow,
        ).preview(_preview())
    assert len(calls) == 101

    def conflict(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        if command[-1].endswith("page=1"):
            return CommandResult(0, page, "")
        return CommandResult(
            0, '[{"iid":1,"state":"opened","description":"changed"}]', ""
        )

    with pytest.raises(UpstreamGitLabError):
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"token",
            run_command=conflict,
        ).preview(_preview())


def test_only_exact_markers_count_and_all_commands_are_get_with_isolated_environment() -> (
    None
):
    marker = render_reserved_marker(_preview())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        return CommandResult(
            0,
            '[{"iid":4,"state":"opened","description":"ordinary untrusted prose"},'
            '{"iid":5,"state":"opened","description":"' + marker + marker + '"}]',
            "",
        )

    with pytest.raises(UpstreamGitLabError) as raised:
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"dedicated-token",
            run_command=run,
        ).preview(_preview())
    assert "ordinary untrusted prose" not in str(raised.value)
    for command, kwargs in calls:
        assert command[1:6] == (
            "api",
            "--hostname",
            "upstream.example.com",
            "-X",
            "GET",
        )
        assert kwargs["environment"] == {"GITLAB_TOKEN": "dedicated-token"}
