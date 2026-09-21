# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for structured, verified GitLab issue publication."""

from __future__ import annotations

import json
import string
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work import gitlab
from agent_work.cli import build_parser, main
from agent_work.domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from agent_work.repository import CommandResult

MARKER = "<!-- agent-work-finding:v1 fingerprint=0123456789abcdef -->"


def _draft(**changes: object) -> Any:
    values: dict[str, object] = {
        "title": "Factory publication is structured",
        "problem": "The publisher accepted escaped text.\nThe body keeps `inline code`.",
        "impact": "Readers saw one unreadable line.",
        "proposed_outcome": "Publish a canonical Markdown issue.",
        "acceptance_criteria": (
            "Render actual newlines.",
            "Verify the authoritative read-back.",
        ),
        "evidence": "Reproduced with the ordinary CLI.",
        "marker": MARKER,
        "labels": ("Bug", "agent::ready"),
    }
    values.update(changes)
    return gitlab.IssuePublicationDraft(**values)


def _evidence(draft: Any, **changes: object) -> Any:
    values: dict[str, object] = {
        "project_id": 310967,
        "iid": 149,
        "title": draft.title,
        "description": gitlab.render_issue_description(draft),
        "labels": draft.labels,
        "web_url": ("https://gitlab.example.com/group/project/-/issues/149"),
        "state": "opened",
    }
    values.update(changes)
    return gitlab.IssuePublicationEvidence(**values)


class FakeIssuePort:
    """Record exact issue publication operations without external calls."""

    def __init__(self, matches: tuple[Any, ...], read_back: Any) -> None:
        self.matches = matches
        self.read_back = read_back
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_issues(self, *, project_id: int, marker: str) -> tuple[Any, ...]:
        self.calls.append(("list", {"project_id": project_id, "marker": marker}))
        return self.matches

    def create_issue(self, **kwargs: object) -> Any:
        self.calls.append(("create", kwargs))
        return self.read_back

    def update_issue(self, **kwargs: object) -> Any:
        self.calls.append(("update", kwargs))
        return self.read_back

    def fetch_issue(self, *, project_id: int, iid: int) -> Any:
        self.calls.append(("fetch", {"project_id": project_id, "iid": iid}))
        return self.read_back


def test_issue_draft_is_immutable_and_renders_canonical_markdown() -> None:
    draft = _draft()

    expected = f"""## Problem
The publisher accepted escaped text.
The body keeps `inline code`.

## Impact
Readers saw one unreadable line.

## Proposed outcome
Publish a canonical Markdown issue.

## Acceptance criteria
- Render actual newlines.
- Verify the authoritative read-back.

## Evidence
Reproduced with the ordinary CLI.

{MARKER}"""
    assert gitlab.render_issue_description(draft) == expected
    with pytest.raises(FrozenInstanceError):
        draft.title = "changed"


def test_issue_renderer_omits_optional_sections_without_losing_blank_lines() -> None:
    description = gitlab.render_issue_description(
        _draft(impact=None, evidence=None, acceptance_criteria=("Done.",))
    )

    assert "## Impact" not in description
    assert "## Evidence" not in description
    assert "## Acceptance criteria\n- Done.\n\n" + MARKER in description
    assert description.count(MARKER) == 1


@given(
    problem=st.text(alphabet=string.ascii_letters + " .`", min_size=1).filter(
        str.strip
    ),
    criteria=st.lists(
        st.text(alphabet=string.ascii_letters + " .`", min_size=1).filter(str.strip),
        min_size=1,
        max_size=5,
    ).map(tuple),
)
def test_issue_renderer_preserves_section_order_and_exact_marker(
    problem: str, criteria: tuple[str, ...]
) -> None:
    description = gitlab.render_issue_description(
        _draft(problem=problem, acceptance_criteria=criteria)
    )
    headings = (
        "## Problem",
        "## Impact",
        "## Proposed outcome",
        "## Acceptance criteria",
        "## Evidence",
    )
    positions = [description.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert description.count(MARKER) == 1
    assert description.endswith(MARKER)


def test_issue_draft_accepts_one_complete_multiline_comment_marker() -> None:
    marker = """<!-- agent-mr-remediation
project_id: 310967
merge_request_iid: 12
-->"""

    assert gitlab.render_issue_description(_draft(marker=marker)).endswith(marker)


@pytest.mark.parametrize(
    "marker",
    (
        "prefix <!-- identity -->",
        "<!-- identity --> suffix",
        "<!-- identity -->injected<!-- -->",
        "<!-- outer <!-- nested --> -->",
    ),
)
def test_issue_draft_rejects_text_outside_or_nested_in_marker(marker: str) -> None:
    with pytest.raises(gitlab.GitLabError, match="draft is invalid"):
        _draft(marker=marker)


@pytest.mark.parametrize(
    "unsafe",
    (
        "literal\\nline",
        "literal\\rline",
        "literal\\r\\nline",
        "encoded%0aline",
        "encoded%0Dline",
        "encoded%0d%0Aline",
    ),
)
@pytest.mark.parametrize(
    "field",
    ("title", "problem", "impact", "proposed_outcome", "evidence", "marker"),
)
def test_issue_draft_rejects_escaped_or_encoded_linebreaks(
    field: str, unsafe: str
) -> None:
    with pytest.raises(gitlab.GitLabError, match="linebreak"):
        _draft(**{field: unsafe})


@pytest.mark.parametrize(
    "unsafe",
    ("literal\\nline", "literal\\rline", "encoded%0aline", "encoded%0Dline"),
)
def test_issue_draft_rejects_unsafe_labels(unsafe: str) -> None:
    with pytest.raises(gitlab.GitLabError, match="linebreak"):
        _draft(labels=("Bug", unsafe))


@pytest.mark.parametrize(
    "changes",
    (
        {"labels": ("Bug", "")},
        {"labels": ("Bug,Security",)},
        {"labels": ("Bug", "Bug")},
        {"impact": " "},
        {"evidence": ""},
        {"evidence": ("good", " ")},
    ),
)
def test_issue_draft_rejects_invalid_structured_values(
    changes: dict[str, object],
) -> None:
    with pytest.raises(gitlab.GitLabError, match="draft is invalid"):
        _draft(**changes)


def test_issue_draft_rejects_unsafe_acceptance_before_port_access() -> None:
    calls: list[str] = []

    class ExplodingPort:
        def list_issues(self, **_kwargs: object) -> tuple[()]:
            calls.append("list")
            return ()

        def create_issue(self, **_kwargs: object) -> Any:
            raise AssertionError("invalid draft reached create")

        def update_issue(self, **_kwargs: object) -> Any:
            raise AssertionError("invalid draft reached update")

        def fetch_issue(self, **_kwargs: object) -> Any:
            raise AssertionError("invalid draft reached fetch")

    with pytest.raises(gitlab.GitLabError, match="linebreak"):
        gitlab.publish_issue(
            project_id=310967,
            draft=_draft(acceptance_criteria=("bad%0Acriterion",)),
            apply=True,
            port=ExplodingPort(),
        )
    assert calls == []


def test_issue_publish_preview_selects_by_exact_marker_without_mutation() -> None:
    draft = _draft()
    existing = _evidence(draft)
    port = FakeIssuePort((existing,), existing)

    report = gitlab.publish_issue(
        project_id=310967, draft=draft, apply=False, port=port
    )

    assert report.applied is False
    assert report.action == "would-update"
    assert report.project_id == 310967
    assert report.selected_iid == 149
    assert report.issue is None
    assert report.description == gitlab.render_issue_description(draft)
    assert port.calls == [("list", {"project_id": 310967, "marker": MARKER})]


def test_issue_publish_preview_reports_create_without_mutation() -> None:
    draft = _draft()
    port = FakeIssuePort((), _evidence(draft))

    report = gitlab.publish_issue(
        project_id=310967, draft=draft, apply=False, port=port
    )

    assert report.action == "would-create"
    assert report.project_id == 310967
    assert report.selected_iid is None
    assert report.issue is None
    assert port.calls == [("list", {"project_id": 310967, "marker": MARKER})]


def test_issue_publish_create_mutates_once_then_authoritatively_reads_back() -> None:
    draft = _draft()
    created = _evidence(draft)
    port = FakeIssuePort((), created)

    report = gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    assert report.action == "created" and report.issue == created
    assert port.calls == [
        ("list", {"project_id": 310967, "marker": MARKER}),
        (
            "create",
            {
                "project_id": 310967,
                "title": draft.title,
                "description": gitlab.render_issue_description(draft),
                "labels": draft.labels,
            },
        ),
        ("fetch", {"project_id": 310967, "iid": 149}),
    ]


def test_issue_publish_accepts_reordered_exact_label_set_on_read_back() -> None:
    draft = _draft()
    created = _evidence(draft, labels=tuple(reversed(draft.labels)))
    port = FakeIssuePort((), created)

    report = gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    assert report.action == "created"
    assert report.issue == created


def test_issue_publish_update_targets_only_the_sole_exact_marker_match() -> None:
    draft = _draft()
    stale = _evidence(
        draft,
        title="Stale title",
        description=f"stale body\n\n{MARKER}",
        labels=("Bug",),
    )
    updated = _evidence(draft)
    port = FakeIssuePort((stale,), updated)

    report = gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    assert report.action == "updated" and report.issue == updated
    assert port.calls == [
        ("list", {"project_id": 310967, "marker": MARKER}),
        (
            "update",
            {
                "project_id": 310967,
                "iid": 149,
                "title": draft.title,
                "description": gitlab.render_issue_description(draft),
                "labels": draft.labels,
            },
        ),
        ("fetch", {"project_id": 310967, "iid": 149}),
    ]


@pytest.mark.parametrize("apply", (False, True))
def test_issue_publish_rejects_ambiguous_marker_without_mutation(
    *, apply: bool
) -> None:
    draft = _draft()
    first = _evidence(draft)
    second = _evidence(draft, iid=150)
    port = FakeIssuePort((first, second), first)

    with pytest.raises(gitlab.GitLabError, match="more than one"):
        gitlab.publish_issue(project_id=310967, draft=draft, apply=apply, port=port)

    assert port.calls == [("list", {"project_id": 310967, "marker": MARKER})]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"project_id": 7}, "project"),
        ({"iid": 150}, "IID"),
        ({"title": "Wrong"}, "title"),
        ({"description": "Wrong"}, "description"),
        ({"labels": ("Bug",)}, "labels"),
        ({"labels": ("Bug", "agent::ready", "Extra")}, "labels"),
        ({"state": "closed"}, "state"),
        (
            {"web_url": "https://other.example.com/issues/149"},
            "URL",
        ),
    ),
)
def test_issue_publish_fails_closed_on_inexact_authoritative_evidence(
    changes: dict[str, object], message: str
) -> None:
    draft = _draft()
    returned = _evidence(draft)
    port = FakeIssuePort((), replace(returned, **changes))

    with pytest.raises(gitlab.GitLabError, match=message):
        gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    assert [name for name, _values in port.calls] == ["list", "create", "fetch"]


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"project_id": 7}, "project"),
        ({"iid": 150}, "IID"),
        ({"title": "Wrong"}, "title"),
        ({"description": "Wrong"}, "description"),
        ({"labels": ("Bug",)}, "labels"),
        ({"labels": ("Bug", "agent::ready", "Extra")}, "labels"),
        ({"state": "closed"}, "state"),
        ({"web_url": "https://other.example.com/issues/149"}, "URL"),
    ),
)
def test_issue_publish_update_fails_closed_on_inexact_authoritative_evidence(
    changes: dict[str, object], message: str
) -> None:
    draft = _draft()
    stale = _evidence(draft, title="Stale")
    port = FakeIssuePort((stale,), replace(_evidence(draft), **changes))

    with pytest.raises(gitlab.GitLabError, match=message):
        gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    expected_calls = (
        ["list", "update"]
        if "iid" in changes
        else [
            "list",
            "update",
            "fetch",
        ]
    )
    assert [name for name, _values in port.calls] == expected_calls


def test_issue_publish_update_rejects_coupled_wrong_iid_and_url() -> None:
    draft = _draft()
    stale = _evidence(draft, title="Stale")
    wrong_target = _evidence(
        draft,
        iid=150,
        web_url="https://gitlab.example.com/group/project/-/issues/150",
    )
    port = FakeIssuePort((stale,), wrong_target)

    with pytest.raises(gitlab.GitLabError, match="IID"):
        gitlab.publish_issue(project_id=310967, draft=draft, apply=True, port=port)

    assert [name for name, _values in port.calls] == ["list", "update"]


def _cli_arguments() -> list[str]:
    return [
        "issue",
        "publish",
        "--title",
        "Factory publication is structured",
        "--problem",
        "Problem text.",
        "--outcome",
        "Outcome text.",
        "--acceptance",
        "First criterion.",
        "--acceptance",
        "Second criterion.",
        "--marker",
        MARKER,
        "--impact",
        "Impact text.",
        "--evidence",
        "Evidence text.",
        "--evidence",
        "More evidence.",
        "--label",
        "Bug",
        "--label",
        "agent::ready",
    ]


def test_issue_publish_cli_is_dry_run_by_default_and_emits_json() -> None:
    calls: list[tuple[Any, bool]] = []
    output: list[str] = []

    def publish(*, draft: Any, apply: bool) -> Any:
        calls.append((draft, apply))
        return SimpleNamespace(
            applied=False,
            action="would-create",
            project_id=310967,
            selected_iid=None,
            issue=None,
            description=gitlab.render_issue_description(draft),
        )

    assert (
        main(
            [*_cli_arguments(), "--json"],
            publish_issue=publish,
            write_stdout=output.append,
        )
        == 0
    )
    assert len(calls) == 1 and calls[0][1] is False
    draft = calls[0][0]
    assert draft.title == "Factory publication is structured"
    assert draft.problem == "Problem text."
    assert draft.proposed_outcome == "Outcome text."
    assert draft.acceptance_criteria == ("First criterion.", "Second criterion.")
    assert draft.labels == ("Bug", "agent::ready")
    assert draft.impact == "Impact text."
    assert draft.evidence == ("Evidence text.", "More evidence.")
    assert draft.marker == MARKER
    assert json.loads(output[0]) == {
        "action": "would-create",
        "applied": False,
        "project_id": 310967,
        "selected_iid": None,
        "title": draft.title,
        "labels": ["Bug", "agent::ready"],
        "marker": MARKER,
        "description": gitlab.render_issue_description(draft),
    }


def test_issue_publish_cli_apply_invokes_publication_once() -> None:
    draft = _draft(
        problem="Problem text.",
        impact="Impact text.",
        proposed_outcome="Outcome text.",
        acceptance_criteria=("First criterion.", "Second criterion."),
        evidence="Evidence text.",
    )
    issue = _evidence(draft)
    calls = 0
    output: list[str] = []

    def publish(*, draft: Any, apply: bool) -> Any:
        nonlocal calls
        calls += 1
        assert apply is True
        return SimpleNamespace(
            applied=True,
            action="created",
            project_id=310967,
            selected_iid=None,
            issue=issue,
            description=gitlab.render_issue_description(draft),
        )

    assert (
        main(
            [*_cli_arguments(), "--apply", "--json"],
            publish_issue=publish,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == 1
    assert json.loads(output[0]) == {
        "action": "created",
        "applied": True,
        "iid": 149,
        "web_url": "https://gitlab.example.com/group/project/-/issues/149",
    }


@pytest.mark.parametrize("apply", (False, True))
def test_issue_publish_cli_human_output_contains_complete_result(
    *, apply: bool
) -> None:
    draft = _draft(
        problem="Problem text.",
        impact="Impact text.",
        proposed_outcome="Outcome text.",
        acceptance_criteria=("First criterion.", "Second criterion."),
        evidence=("Evidence text.", "More evidence."),
    )
    issue = _evidence(draft)
    output: list[str] = []

    def publish(**_kwargs: object) -> Any:
        return SimpleNamespace(
            applied=apply,
            action="created" if apply else "would-update",
            project_id=310967,
            selected_iid=None if apply else 149,
            issue=issue if apply else None,
            description=gitlab.render_issue_description(draft),
        )

    arguments = [*_cli_arguments(), *(["--apply"] if apply else [])]
    assert main(arguments, publish_issue=publish, write_stdout=output.append) == 0
    if apply:
        assert output == [
            (
                "created: applied=true iid=149 "
                "url=https://gitlab.example.com/group/project/-/issues/149"
            )
        ]
    else:
        rendered = output[0]
        assert "would-update: applied=false project=310967 selected_iid=149" in rendered
        assert 'title="Factory publication is structured"' in rendered
        assert 'labels=["Bug", "agent::ready"]' in rendered
        assert f"marker={MARKER}" in rendered
        assert gitlab.render_issue_description(draft) in rendered


def test_issue_publish_cli_maps_publication_failure_to_one() -> None:
    errors: list[str] = []

    def fail(**_kwargs: object) -> Any:
        raise gitlab.GitLabError("issue publication failed")

    assert main(_cli_arguments(), publish_issue=fail, write_stderr=errors.append) == 1
    assert errors == ["issue publication failed"]


def test_issue_publish_cli_help_and_missing_required_input_use_argparse_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        build_parser().parse_args(["issue", "publish", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--acceptance" in help_text
    assert "repeat" in help_text.lower()
    assert "dry" in help_text.lower()

    with pytest.raises(SystemExit) as invalid_exit:
        build_parser().parse_args(["issue", "publish"])
    assert invalid_exit.value.code == 2
    error = capsys.readouterr().err
    assert "--title" in error and "--problem" in error and "--marker" in error


def _published_issue_payload(
    *, iid: int = 149, description: object, **changes: object
) -> str:
    payload: dict[str, object] = {
        "project_id": 310967,
        "iid": iid,
        "title": "Factory publication is structured",
        "description": description,
        "labels": ["Bug", "agent::ready"],
        "web_url": f"https://gitlab.example.com/group/project/-/issues/{iid}",
        "state": "opened",
    }
    payload.update(changes)
    return json.dumps(payload)


def test_issue_transport_lists_full_open_inventory_and_filters_exact_marker() -> None:
    commands: list[tuple[str, ...]] = []
    draft = _draft()

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        if "page=1" in command:
            payload = [
                json.loads(
                    _published_issue_payload(
                        iid=iid,
                        description=None if iid == 1 else "no marker",
                    )
                )
                for iid in range(1, 101)
            ]
        else:
            payload = [
                json.loads(
                    _published_issue_payload(
                        description=gitlab.render_issue_description(draft)
                    )
                )
            ]
        return CommandResult(0, json.dumps(payload), "")

    publisher = gitlab.GitLabIssuePublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/usr/bin/glab",
        run_command=run,
    )

    assert publisher.list_issues(project_id=310967, marker=MARKER)[0].iid == 149
    assert len(commands) == 2 and "page=2" in commands[1]
    command = commands[0]
    assert command[command.index("-X") + 1] == "GET"
    assert command[command.index("-X") + 2] == "projects/310967/issues"
    assert (
        "state=opened" in command and "per_page=100" in command and "page=1" in command
    )
    assert not any(value.startswith(("search=", "in=")) for value in command)


@pytest.mark.parametrize(
    ("description", "state", "message"),
    (
        (MARKER, "closed", "opened"),
        (f"{MARKER}\n{MARKER}", "opened", "more than once"),
    ),
)
def test_issue_transport_fails_closed_on_stale_or_ambiguous_inventory(
    description: str, state: str, message: str
) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        payload = json.loads(_published_issue_payload(description=description))
        payload["state"] = state
        return CommandResult(0, json.dumps([payload]), "")

    publisher = gitlab.GitLabIssuePublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/usr/bin/glab",
        run_command=run,
    )

    with pytest.raises(gitlab.GitLabError, match=message):
        publisher.list_issues(project_id=310967, marker=MARKER)


@pytest.mark.parametrize(
    ("operation", "method", "endpoint"),
    (
        ("create", "POST", "projects/310967/issues"),
        ("update", "PUT", "projects/310967/issues/149"),
        ("fetch", "GET", "projects/310967/issues/149"),
    ),
)
def test_issue_transport_uses_exact_methods_fields_and_parses_evidence(
    operation: str, method: str, endpoint: str
) -> None:
    draft = _draft()
    description = gitlab.render_issue_description(draft)
    commands: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append((command, kwargs))
        return CommandResult(0, _published_issue_payload(description=description), "")

    publisher = gitlab.GitLabIssuePublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/usr/bin/glab",
        run_command=run,
    )
    if operation == "create":
        evidence = publisher.create_issue(
            project_id=310967,
            title=draft.title,
            description=description,
            labels=draft.labels,
        )
    elif operation == "update":
        evidence = publisher.update_issue(
            project_id=310967,
            iid=149,
            title=draft.title,
            description=description,
            labels=draft.labels,
        )
    else:
        evidence = publisher.fetch_issue(project_id=310967, iid=149)
    assert evidence.iid == 149 and evidence.project_id == 310967
    command, kwargs = commands[0]
    assert command[0] == "/usr/bin/glab"
    assert command[command.index("--hostname") + 1] == "gitlab.example.com"
    assert kwargs["timeout_seconds"] == HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
    assert command[command.index("-X") + 1] == method
    assert command[command.index("-X") + 2] == endpoint
    if operation != "fetch":
        assert f"title={draft.title}" in command
        assert f"description={description}" in command
        assert "labels=Bug,agent::ready" in command


@pytest.mark.parametrize(
    "changes",
    (
        {"project_id": "310967"},
        {"iid": "149"},
        {"labels": ["Bug", 7]},
        {"project_id": 7},
        {"web_url": "https://other.example.com/group/project/-/issues/149"},
    ),
)
def test_issue_transport_rejects_malformed_or_wrong_identity_evidence(
    changes: dict[str, object],
) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        payload = json.loads(_published_issue_payload(description="body"))
        payload.update(changes)
        return CommandResult(0, json.dumps(payload), "")

    publisher = gitlab.GitLabIssuePublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/usr/bin/glab",
        run_command=run,
    )

    with pytest.raises(gitlab.GitLabError, match="issue publication"):
        publisher.fetch_issue(project_id=310967, iid=149)


def test_issue_publish_cli_composes_default_numeric_project_transport() -> None:
    output: list[str] = []
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "[]", "")

    assert (
        main(
            [*_cli_arguments(), "--json"],
            environment={
                "GITLAB_HOST": "gitlab.example.com",
                "GITLAB_PROJECT_ID": "310967",
            },
            find_executable=lambda _name: "/usr/bin/glab",
            run_command=run,
            write_stdout=output.append,
        )
        == 0
    )
    payload = json.loads(output[0])
    assert payload["project_id"] == 310967
    assert payload["title"] == "Factory publication is structured"
    assert payload["labels"] == ["Bug", "agent::ready"]
    assert len(commands) == 1
