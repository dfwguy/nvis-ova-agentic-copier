# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for direct, human-readable merge-request publication."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_work.gitlab import (
    GitLabError,
    GitLabGateway,
    GitLabMergeRequestPublisher,
    publish_merge_request,
    render_merge_request_description,
)
from agent_work.models import AgentWorkConfig, MergeRequestDraft, MergeRequestEvidence
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult


def _evidence(
    *,
    iid: int = 88,
    source_branch: str = "clanker/structured-mr-descriptions",
    target_branch: str = "main",
    title: str = "Render merge-request descriptions",
    description: str = "",
) -> MergeRequestEvidence:
    return MergeRequestEvidence.model_validate(
        {
            "iid": iid,
            "web_url": f"https://gitlab.example.com/group/project/-/merge_requests/{iid}",
            "state": "opened",
            "source_project_id": 310967,
            "target_project_id": 310967,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "sha": "a" * 40,
            "has_conflicts": False,
            "description": description,
            "title": title,
            "draft": False,
        }
    )


@dataclass
class PublicationPortFake:
    """In-memory publication boundary proving orchestration without GitLab."""

    inventory: tuple[MergeRequestEvidence, ...] = ()
    read_back: MergeRequestEvidence | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def list_merge_requests(
        self, *, project_id: int, source_branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        self.calls.append(("list", (project_id, source_branch)))
        return self.inventory

    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence:
        self.calls.append(
            ("create", (project_id, source_branch, target_branch, title, description))
        )
        return self.read_back or _evidence(description=description)

    def update_merge_request(
        self,
        *,
        project_id: int,
        iid: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence:
        self.calls.append(
            (
                "update",
                (project_id, iid, source_branch, target_branch, title, description),
            )
        )
        return self.read_back or _evidence(iid=iid, description=description)

    def fetch_merge_request(self, *, project_id: int, iid: int) -> MergeRequestEvidence:
        self.calls.append(("fetch", (project_id, iid)))
        return self.read_back or _evidence(iid=iid)


def test_renderer_emits_real_newlines_and_stable_optional_sections() -> None:
    description = render_merge_request_description(
        problem="Rendered bodies were escaped.",
        impact="Reviewers could not scan the request.",
        outcome="Publish readable Markdown.",
        human_action="Please review the boundary.",
        validation=("uv run pytest", "manual dry run"),
        tracking="Closes #123",
    )

    assert description == (
        "## Problem\nRendered bodies were escaped.\n\n"
        "## Impact\nReviewers could not scan the request.\n\n"
        "## Outcome\nPublish readable Markdown.\n\n"
        "## Human action\nPlease review the boundary.\n\n"
        "## Validation\n- uv run pytest\n- manual dry run\n\n"
        "## Tracking\nCloses #123"
    )
    assert "\\n" not in description


def test_renderer_omits_optional_sections_without_changing_required_order() -> None:
    description = render_merge_request_description(
        problem="Problem.", outcome="Outcome.", validation=("pytest",), tracking="!88"
    )

    assert description.split("\n\n") == [
        "## Problem\nProblem.",
        "## Outcome\nOutcome.",
        "## Validation\n- pytest",
        "## Tracking\n!88",
    ]


@pytest.mark.parametrize(
    "description", ["one line\\n\\nanother", "one line\\r\\nanother"]
)
def test_merge_request_descriptions_reject_obvious_escaped_linebreak_payloads(
    description: str,
) -> None:
    with pytest.raises(ValidationError, match="escaped linebreak"):
        MergeRequestDraft(title="Readable", description=description)


def test_single_paragraph_factory_draft_remains_valid() -> None:
    assert (
        MergeRequestDraft(
            title="Factory change", description="One paragraph."
        ).description
        == "One paragraph."
    )


def test_gateway_rejects_malformed_draft_before_any_command(tmp_path: Path) -> None:
    malformed = MergeRequestDraft.model_construct(
        title="Malformed", description="one\\n\\nanother"
    )

    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "{}", "")

    gateway = GitLabGateway(
        AgentWorkConfig(
            host="gitlab.example.com",
            project_id=310967,
            checkout=tmp_path / "checkout",
            state_root=tmp_path / "state",
            git_path=tmp_path / "git",
            glab_path=tmp_path / "glab",
            base_branch="agent/work-base",
        ),
        run,
    )
    with pytest.raises(GitLabError, match="escaped linebreak"):
        gateway.create_merge_request(
            branch="agent/work-base", target="main", issue_iid=12, draft=malformed
        )
    assert calls == []


def test_publisher_dry_run_lists_one_branch_but_never_mutates() -> None:
    port = PublicationPortFake()

    report = publish_merge_request(
        project_id=310967,
        source_branch="clanker/structured-mr-descriptions",
        target_branch="main",
        title="Render merge-request descriptions",
        description="## Problem\nReadable bodies.",
        apply=False,
        port=port,
    )

    assert report.applied is False
    assert report.action == "would-create"
    assert port.calls == [("list", (310967, "clanker/structured-mr-descriptions"))]


@pytest.mark.parametrize("existing", [(), (_evidence(),)])
def test_publisher_applies_one_create_or_update_then_authoritatively_reads_back(
    existing: tuple[MergeRequestEvidence, ...],
) -> None:
    description = "## Problem\nReadable bodies.\n\n## Validation\n- uv run pytest"
    expected = _evidence(description=description)
    port = PublicationPortFake(inventory=existing, read_back=expected)

    report = publish_merge_request(
        project_id=310967,
        source_branch=expected.source_branch,
        target_branch=expected.target_branch,
        title=expected.title,
        description=description,
        apply=True,
        port=port,
    )

    assert report.applied is True
    assert report.merge_request == expected
    assert [call[0] for call in port.calls] == [
        "list",
        "update" if existing else "create",
        "fetch",
    ]
    mutation = port.calls[1][1]
    assert (
        isinstance(mutation, tuple)
        and mutation[-1] == description
        and "\n" in str(mutation[-1])
    )


def test_publisher_fails_closed_when_branch_has_multiple_merge_requests() -> None:
    port = PublicationPortFake(inventory=(_evidence(iid=88), _evidence(iid=89)))

    with pytest.raises(GitLabError, match="more than one"):
        publish_merge_request(
            project_id=310967,
            source_branch="clanker/structured-mr-descriptions",
            target_branch="main",
            title="Render merge-request descriptions",
            description="## Problem\nReadable bodies.",
            apply=True,
            port=port,
        )

    assert [call[0] for call in port.calls] == ["list"]


def test_publisher_rejects_closed_list_candidate_before_update() -> None:
    closed = _evidence().model_copy(update={"state": "closed"})
    port = PublicationPortFake(inventory=(closed,))

    with pytest.raises(GitLabError, match="listed merge request"):
        publish_merge_request(
            project_id=310967,
            source_branch=closed.source_branch,
            target_branch=closed.target_branch,
            title=closed.title,
            description="## Problem\nReadable bodies.",
            apply=True,
            port=port,
        )

    assert [call[0] for call in port.calls] == ["list"]


def test_publisher_rejects_a_read_back_that_does_not_match_exact_request() -> None:
    port = PublicationPortFake(read_back=_evidence(title="Unexpected title"))

    with pytest.raises(GitLabError, match="read-back"):
        publish_merge_request(
            project_id=310967,
            source_branch="clanker/structured-mr-descriptions",
            target_branch="main",
            title="Render merge-request descriptions",
            description="## Problem\nReadable bodies.",
            apply=True,
            port=port,
        )


def test_publisher_rejects_closed_authoritative_read_back() -> None:
    created = _evidence(description="## Problem\nReadable bodies.")
    closed = created.model_copy(update={"state": "closed"})
    port = PublicationPortFake(read_back=closed)

    with pytest.raises(GitLabError, match="read-back"):
        publish_merge_request(
            project_id=310967,
            source_branch=created.source_branch,
            target_branch=created.target_branch,
            title=created.title,
            description=created.description,
            apply=True,
            port=port,
        )


def _publisher_runner(
    responses: list[dict[str, object] | list[dict[str, object]]],
) -> tuple[list[tuple[str, ...]], CommandRunner]:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, json.dumps(responses.pop(0)), "")

    return calls, run


def test_concrete_publication_port_creates_with_numeric_endpoint_and_reads_back() -> (
    None
):
    description = "## Problem\nReadable bodies.\n\n## Validation\n- uv run pytest"
    created = _evidence(description=description).model_dump(mode="json")
    calls, run = _publisher_runner([created, created])
    publisher = GitLabMergeRequestPublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/bin/glab",
        run_command=run,
    )

    published = publisher.create_merge_request(
        project_id=310967,
        source_branch="clanker/structured-mr-descriptions",
        target_branch="main",
        title="Render merge-request descriptions",
        description=description,
    )
    read_back = publisher.fetch_merge_request(project_id=310967, iid=published.iid)

    assert read_back == published
    assert calls[0][calls[0].index("-X") + 2] == "projects/310967/merge_requests"
    assert calls[0][calls[0].index("-X") + 1] == "POST"
    description_fields = [part for part in calls[0] if part.startswith("description=")]
    assert description_fields == [f"description={description}"]
    assert "remove_source_branch=true" in calls[0]
    assert "squash=true" in calls[0]
    assert "\n" in description_fields[0] and "\\n" not in description_fields[0]
    assert calls[1][calls[1].index("-X") + 2] == "projects/310967/merge_requests/88"
    assert calls[1][calls[1].index("-X") + 1] == "GET"


def test_concrete_publication_port_lists_one_branch_through_the_numeric_endpoint() -> (
    None
):
    expected = _evidence()
    calls, run = _publisher_runner([[expected.model_dump(mode="json")]])
    publisher = GitLabMergeRequestPublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/bin/glab",
        run_command=run,
    )

    found = publisher.list_merge_requests(
        project_id=310967, source_branch="clanker/structured-mr-descriptions"
    )

    assert found == (expected,)
    assert calls[0][calls[0].index("-X") + 2] == "projects/310967/merge_requests"
    assert calls[0][calls[0].index("-X") + 1] == "GET"
    assert "state=opened" in calls[0]
    assert "source_branch=clanker/structured-mr-descriptions" in calls[0]


def test_concrete_publication_port_updates_with_numeric_endpoint_and_reads_back() -> (
    None
):
    description = "## Problem\nUpdated readable bodies."
    updated = _evidence(description=description).model_dump(mode="json")
    calls, run = _publisher_runner([updated, updated])
    publisher = GitLabMergeRequestPublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/bin/glab",
        run_command=run,
    )

    published = publisher.update_merge_request(
        project_id=310967,
        iid=88,
        source_branch="clanker/structured-mr-descriptions",
        target_branch="main",
        title="Render merge-request descriptions",
        description=description,
    )
    read_back = publisher.fetch_merge_request(project_id=310967, iid=published.iid)

    assert read_back == published
    assert calls[0][calls[0].index("-X") + 2] == "projects/310967/merge_requests/88"
    assert calls[0][calls[0].index("-X") + 1] == "PUT"
    assert [part for part in calls[0] if part.startswith("description=")] == [
        f"description={description}"
    ]
    assert not any(part.startswith("source_branch=") for part in calls[0])
    assert calls[1][calls[1].index("-X") + 2] == "projects/310967/merge_requests/88"
    assert calls[1][calls[1].index("-X") + 1] == "GET"


def test_concrete_publication_port_rejects_escaped_linebreaks_before_any_command() -> (
    None
):
    calls, run = _publisher_runner([])
    publisher = GitLabMergeRequestPublisher(
        host="gitlab.example.com",
        project_id=310967,
        glab_path="/bin/glab",
        run_command=run,
    )

    with pytest.raises(GitLabError, match="escaped linebreak"):
        publisher.create_merge_request(
            project_id=310967,
            source_branch="clanker/structured-mr-descriptions",
            target_branch="main",
            title="Render merge-request descriptions",
            description="## Problem\\n\\nEscaped body.",
        )

    assert calls == []
