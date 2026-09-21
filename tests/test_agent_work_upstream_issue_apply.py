# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for explicit, bounded upstream issue creation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_work.cli import main
from agent_work.repository import CommandResult
from agent_work.upstream_finding import (
    ManifestError,
    PreparedIssueDraft,
    UpstreamFindingPreview,
    prepare_issue_draft,
    render_issue_title,
)
from agent_work.upstream_gitlab import (
    UpstreamGitLabConfig,
    UpstreamGitLabError,
    UpstreamGitLabReader,
)


def _preview() -> UpstreamFindingPreview:
    """Return a content-free, already-validated finding summary."""
    return prepare_issue_draft(
        Path("finding.yaml"), read_bytes=lambda _path: _manifest()
    ).preview


def _draft(preview: UpstreamFindingPreview) -> PreparedIssueDraft:
    """Build the smallest canonical draft for a reader boundary test."""
    draft = prepare_issue_draft(
        Path("finding.yaml"), read_bytes=lambda _path: _manifest()
    )
    assert draft.preview == preview
    return draft


def _config() -> UpstreamGitLabConfig:
    """Return an isolated upstream client configuration."""
    return UpstreamGitLabConfig(
        "upstream.example.com",
        77,
        Path("/dedicated/upstream-token"),
        Path("/opt/bin/glab"),
    )


def _project() -> str:
    """Return the authoritative project response needed before an apply."""
    return (
        '{"id":77,"path_with_namespace":"group/project","issues_enabled":true,'
        '"permissions":{"project_access":{"access_level":20},"group_access":null}}'
    )


def _manifest() -> bytes:
    """Return one safe manifest whose details must appear only in a bounded draft."""
    return b"schema_version: 1\ngenerated_project:\n  host: gitlab.example.com\n  project_id: 42\n  path_with_namespace: group/widget\ncopier:\n  source: https://github.com/example/template.git\n  revision: 0123456789abcdef0123456789abcdef01234567\nfinding:\n  copied_paths:\n    - src/widget.py\n  reproduction:\n    - Run the generated command\n  expected_behavior: Generated widget accepts input.\n  observed_behavior: Generated widget rejects input.\n  validation_evidence: Reproduced locally.\n  priority: high\n  severity: medium\n  scope: both\n  evidence_links: []\n"


def test_prepare_issue_draft_is_canonical_bounded_and_rejects_divergent_direct_data() -> (
    None
):
    """Only the strict manifest path can produce the canonical issue payload."""
    draft = prepare_issue_draft(
        Path("finding.yaml"), read_bytes=lambda _path: _manifest()
    )
    assert draft.title == render_issue_title(draft.preview)
    assert draft.description.count(draft.marker) == 1
    assert "glpat-" not in draft.description
    constructor: Any = PreparedIssueDraft
    with pytest.raises(TypeError):
        constructor(
            draft.preview, "operator supplied title", draft.description, draft.marker
        )


def test_prepare_issue_draft_rejects_reserved_marker_evidence() -> None:
    """Manifest evidence cannot create a second deduplication marker prefix."""
    draft = prepare_issue_draft(
        Path("finding.yaml"), read_bytes=lambda _path: _manifest()
    )
    constructor: Any = PreparedIssueDraft
    content = _manifest().replace(
        b"expected_behavior: Generated widget accepts input.",
        b"expected_behavior: '<!-- agent-work-upstream-finding: operator evidence'",
    )
    with pytest.raises(ManifestError, match="issue draft is invalid"):
        prepare_issue_draft(Path("finding.yaml"), read_bytes=lambda _path: content)
    with pytest.raises(TypeError):
        constructor(
            draft.preview,
            draft.title,
            f"{draft.description}\noperator-supplied text",
            draft.marker,
        )
    with pytest.raises(ValueError):
        PreparedIssueDraft(
            preview=draft.preview,
            expected_behavior="different evidence",
            observed_behavior=draft.observed_behavior,
            validation_evidence=draft.validation_evidence,
            priority=draft.priority,
            severity=draft.severity,
            scope=draft.scope,
            copied_paths=draft.copied_paths,
            reproduction=draft.reproduction,
            evidence_links=draft.evidence_links,
        )


def test_bare_cli_preview_is_get_only_but_apply_routes_a_prepared_draft_once() -> None:
    """The opt-in flag is the sole route from a local preview to a write boundary."""
    draft = _draft(_preview())
    outputs: list[str] = []
    calls: list[str] = []
    arguments = [
        "upstream-finding",
        "preview",
        "--manifest-file",
        "finding.yaml",
        "--upstream-host",
        "upstream.example.com",
        "--upstream-project-id",
        "77",
        "--upstream-credential-file",
        "/dedicated/token",
        "--json",
    ]

    def prepare(path: Path, *, read_bytes: object) -> PreparedIssueDraft:
        assert path == Path("finding.yaml") and callable(read_bytes)
        calls.append("prepare")
        return draft

    def preview(
        preview: UpstreamFindingPreview, **_kwargs: object
    ) -> UpstreamFindingPreview:
        assert preview == draft.preview
        calls.append("preview")
        return replace(draft.preview, outcome="would-create")

    def apply(draft: PreparedIssueDraft, **_kwargs: object) -> UpstreamFindingPreview:
        assert draft == _draft(_preview())
        calls.append("apply")
        return replace(draft.preview, outcome="created", matching_iid=41)

    assert (
        main(
            arguments,
            prepare_issue_draft=prepare,
            preview_upstream=preview,
            apply_upstream=lambda *_args, **_kwargs: pytest.fail("bare preview posted"),
            write_stdout=outputs.append,
        )
        == 0
    )
    assert calls == ["prepare", "preview"]
    assert json.loads(outputs.pop())["outcome"] == "would-create"
    assert (
        main(
            [*arguments, "--apply"],
            prepare_issue_draft=prepare,
            preview_upstream=lambda *_args, **_kwargs: pytest.fail(
                "apply used GET preview"
            ),
            apply_upstream=apply,
            write_stdout=outputs.append,
        )
        == 0
    )
    assert calls == ["prepare", "preview", "prepare", "apply"]
    assert json.loads(outputs.pop())["matching_iid"] == 41


def test_apply_rechecks_marker_then_posts_once_with_only_canonical_fields() -> None:
    """A race-created issue wins over POST; otherwise the lone POST is canonical."""
    preview = _preview()
    draft = _draft(preview)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []
    scans = iter(("[]", "[]"))

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append((command, kwargs))
        endpoint = command[-1]
        if endpoint == "projects/77":
            return CommandResult(0, _project(), "")
        if endpoint.startswith("projects/77/issues?state=opened"):
            return CommandResult(0, next(scans), "")
        if endpoint == "projects/77/issues/41":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "project_id": 77,
                        "iid": 41,
                        "state": "opened",
                        "confidential": False,
                        "title": draft.title,
                        "description": draft.description,
                        "labels": [],
                        "web_url": "https://upstream.example.com/group/project/-/issues/41",
                    }
                ),
                "",
            )
        return CommandResult(
            0,
            json.dumps(
                {
                    "project_id": 77,
                    "iid": 41,
                    "state": "opened",
                    "confidential": False,
                    "title": draft.title,
                    "description": draft.description,
                    "web_url": "https://upstream.example.com/group/project/-/issues/41",
                }
            ),
            "",
        )

    result = UpstreamGitLabReader(
        _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
    ).apply(preview, draft)
    assert (result.outcome, result.matching_iid) == ("created", 41)
    post = [command for command, _kwargs in commands if "POST" in command]
    assert len(post) == 1 and post[0][-1] == "projects/77/issues"
    assert set(post[0][7:-1]) == {
        "-f",
        f"title={draft.title}",
        f"description={draft.description}",
        "confidential=false",
    }
    for _command, kwargs in commands:
        assert kwargs["environment"] == {"GITLAB_TOKEN": "dedicated-token"}


@pytest.mark.parametrize(
    "unsafe_text",
    (
        pytest.param("literal\\nnewline", id="literal-backslash-n"),
        pytest.param("literal\\r\\nnewline", id="literal-backslash-r-n"),
        pytest.param("percent%0anewline", id="lowercase-percent-lf"),
        pytest.param("percent%0Dnewline", id="uppercase-percent-cr"),
    ),
)
def test_apply_rejects_unsafe_draft_text_before_any_gitlab_command(
    unsafe_text: str,
) -> None:
    """Untrusted finding text cannot reach either the read or write transport."""
    draft = prepare_issue_draft(
        Path("finding.yaml"),
        read_bytes=lambda _path: _manifest().replace(
            b"Generated widget rejects input.", unsafe_text.encode()
        ),
    )
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "", "")

    with pytest.raises(UpstreamGitLabError, match="linebreak"):
        UpstreamGitLabReader(
            _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
        ).apply(draft.preview, draft)

    assert commands == []


def test_apply_reads_created_issue_back_and_requires_exact_authoritative_evidence() -> (
    None
):
    """A successful POST is not publication evidence until the issue GET matches."""
    preview = _preview()
    draft = _draft(preview)
    commands: list[tuple[str, ...]] = []

    def issue_payload(*, state: str = "opened") -> str:
        return json.dumps(
            {
                "project_id": 77,
                "iid": 41,
                "state": state,
                "confidential": False,
                "title": draft.title,
                "description": draft.description,
                "labels": [],
                "web_url": "https://upstream.example.com/group/project/-/issues/41",
            }
        )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[-1]
        if endpoint == "projects/77":
            return CommandResult(0, _project(), "")
        if endpoint.startswith("projects/77/issues?state=opened"):
            return CommandResult(0, "[]", "")
        if endpoint == "projects/77/issues":
            return CommandResult(0, issue_payload(), "")
        if endpoint == "projects/77/issues/41":
            return CommandResult(0, issue_payload(), "")
        raise AssertionError(endpoint)

    result = UpstreamGitLabReader(
        _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
    ).apply(preview, draft)

    assert (result.outcome, result.matching_iid) == ("created", 41)
    assert commands[-1][-1] == "projects/77/issues/41"


def test_apply_fails_closed_when_created_issue_readback_does_not_match() -> None:
    """A closed or altered authoritative read-back never confirms the POST."""
    preview = _preview()
    draft = _draft(preview)
    commands: list[tuple[str, ...]] = []

    def payload(*, state: str) -> str:
        return json.dumps(
            {
                "project_id": 77,
                "iid": 41,
                "state": state,
                "confidential": False,
                "title": draft.title,
                "description": draft.description,
                "labels": [],
                "web_url": "https://upstream.example.com/group/project/-/issues/41",
            }
        )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[-1]
        if endpoint == "projects/77":
            return CommandResult(0, _project(), "")
        if endpoint.startswith("projects/77/issues?state=opened"):
            return CommandResult(0, "[]", "")
        if endpoint == "projects/77/issues":
            return CommandResult(0, payload(state="opened"), "")
        if endpoint == "projects/77/issues/41":
            return CommandResult(0, payload(state="closed"), "")
        raise AssertionError(endpoint)

    with pytest.raises(UpstreamGitLabError, match="upstream response is invalid"):
        UpstreamGitLabReader(
            _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
        ).apply(preview, draft)

    assert sum("POST" in command for command in commands) == 1


def test_apply_second_scan_match_recovers_without_a_post() -> None:
    """A marker appearing in the race window is returned instead of posted again."""
    preview = _preview()
    draft = _draft(preview)
    commands: list[tuple[str, ...]] = []
    scans = iter(
        (
            "[]",
            json.dumps([{"iid": 41, "state": "opened", "description": draft.marker}]),
        )
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        return CommandResult(0, next(scans), "")

    result = UpstreamGitLabReader(
        _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
    ).apply(preview, draft)
    assert (result.outcome, result.matching_iid) == ("match", 41)
    assert all("POST" not in command for command in commands)


def test_apply_rejects_a_created_response_with_marker_but_nonidentical_description() -> (
    None
):
    """A returned marker is insufficient: the complete canonical body must match."""
    preview = _preview()
    draft = _draft(preview)
    post_count = 0

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal post_count
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        if command[5] == "GET":
            return CommandResult(0, "[]", "")
        post_count += 1
        return CommandResult(
            0,
            json.dumps(
                {
                    "project_id": 77,
                    "iid": 41,
                    "state": "opened",
                    "confidential": False,
                    "title": draft.title,
                    "description": f"operator text\\n{draft.description}",
                    "web_url": "https://upstream.example.com/group/project/-/issues/41",
                }
            ),
            "",
        )

    with pytest.raises(UpstreamGitLabError, match="upstream response is invalid"):
        UpstreamGitLabReader(
            _config(), read_credential=lambda _path: b"dedicated-token", run_command=run
        ).apply(preview, draft)
    assert post_count == 1


def test_rerun_recovers_an_uncertain_create_via_the_first_marker_scan() -> None:
    """An invocation never retries an uncertain POST, while a later run can recover."""
    preview = _preview()
    draft = _draft(preview)
    first_commands: list[tuple[str, ...]] = []

    def uncertain(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        first_commands.append(command)
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        if command[5] == "GET":
            return CommandResult(0, "[]", "")
        return CommandResult(1, "", "network outcome unknown")

    with pytest.raises(UpstreamGitLabError):
        UpstreamGitLabReader(
            _config(),
            read_credential=lambda _path: b"dedicated-token",
            run_command=uncertain,
        ).apply(preview, draft)
    assert sum("POST" in command for command in first_commands) == 1

    recovered_commands: list[tuple[str, ...]] = []

    def recovered(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        recovered_commands.append(command)
        if command[-1] == "projects/77":
            return CommandResult(0, _project(), "")
        return CommandResult(
            0,
            json.dumps([{"iid": 41, "state": "opened", "description": draft.marker}]),
            "",
        )

    result = UpstreamGitLabReader(
        _config(),
        read_credential=lambda _path: b"dedicated-token",
        run_command=recovered,
    ).apply(preview, draft)
    assert (result.outcome, result.matching_iid) == ("match", 41)
    assert all("POST" not in command for command in recovered_commands)
