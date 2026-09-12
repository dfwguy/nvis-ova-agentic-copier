# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior tests for GitLab, Git, and worker process boundaries."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pytest_subprocess import FakeProcess

from agent_work.cli import main
from agent_work.domain import LIFECYCLE_LABELS
from agent_work.gitlab import GitLabError, GitLabGateway
from agent_work.models import (
    AgentWorkConfig,
    CommandReport,
    DecisionReviewSignal,
    MergeRequestDraft,
    MergeRequestEvidence,
)
from agent_work.repository import (
    CommandResult,
    GitRepository,
    RepositoryError,
    _origin_identity,
    discover_config,
    run_subprocess,
)
from agent_work.runtime import _build_local_logger
from agent_work.worker import AgentInvoker, WorkerProtocolError, _agent_command


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        base_branch="agent/work-base",
    )


def _issue_payload(iid: int = 12, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Work",
        "description": None,
        "labels": labels or [],
        "state": "opened",
        "confidential": False,
        "created_at": "2026-01-01T00:00:00Z",
        "author": {"username": "ignored"},
    }


def _note_payload(
    note_id: int,
    *,
    body: str = "Useful context",
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "id": note_id,
        "body": body,
        "author": {"username": "reviewer", "id": 42, "name": "Reviewer"},
        "created_at": created_at,
        "system": False,
    }


def _mr_payload(description: str = "") -> dict[str, object]:
    return {
        "iid": 88,
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/88",
        "state": "opened",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "source_branch": "agent/issue-12",
        "target_branch": "main",
        "sha": "a" * 40,
        "has_conflicts": False,
        "description": description,
        "title": "",
        "draft": False,
        "references": {"short": "!88"},
    }


def _mr_evidence(description: str = "") -> MergeRequestEvidence:
    return MergeRequestEvidence.model_validate(
        {
            key: value
            for key, value in _mr_payload(description).items()
            if key != "references"
        }
    )


def _decision_review() -> DecisionReviewSignal:
    return DecisionReviewSignal(
        decisions=("Keep the retry policy unchanged.",),
        assumptions=("The existing MR can be reconciled.",),
        alternatives_considered=("Block for human direction.",),
        risks=("A retry could lose review context.",),
        reviewer_checks=("Confirm the retry policy.",),
        unresolved_findings=("Human review is required.",),
        evidence=("Red tests specify the behavior.",),
        tests=("uv run pytest tests/test_agent_work_boundaries.py",),
        correction_cycle_ledger=("cycle-2",),
        unchanged_project_owned_values=("retry budget",),
    )


class ApiStub:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.issue = _issue_payload(labels=["customer::priority"])
        self.mr_title = ""
        self.mr_description = ""
        self.mr_draft = False

    def __call__(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967":
            payload: object = {
                "id": 310967,
                "host": "ignored",
                "default_branch": "main",
                "web_url": "https://gitlab.example.com/group/project",
                "namespace": {"name": "ignored"},
            }
        elif endpoint.endswith("/issues"):
            payload = [self.issue, _issue_payload(13, ["agent::running"])]
        elif endpoint.endswith("/issues/12"):
            if method == "PUT":
                additions = [
                    part.removeprefix("add_labels=")
                    for part in command
                    if part.startswith("add_labels=")
                ]
                self.issue["labels"] = additions
            payload = self.issue
        elif endpoint.endswith("/merge_requests"):
            if method == "POST":
                payload = _mr_payload()
                payload["title"] = next(
                    part.removeprefix("title=")
                    for part in command
                    if part.startswith("title=")
                )
                payload["description"] = next(
                    part.removeprefix("description=")
                    for part in command
                    if part.startswith("description=")
                )
                payload["draft"] = payload["title"].startswith("Draft:")
                self.mr_title = payload["title"]
                self.mr_description = payload["description"]
                self.mr_draft = payload["draft"]
            else:
                payload = [_mr_payload()]
        elif endpoint.endswith("/merge_requests/88"):
            payload = _mr_payload("Closes #12")
            if method == "PUT":
                self.mr_title = next(
                    (
                        part.removeprefix("title=")
                        for part in command
                        if part.startswith("title=")
                    ),
                    self.mr_title,
                )
                self.mr_description = next(
                    (
                        part.removeprefix("description=")
                        for part in command
                        if part.startswith("description=")
                    ),
                    self.mr_description,
                )
                payload["title"] = self.mr_title
                payload["description"] = self.mr_description
                payload["draft"] = self.mr_draft
        elif endpoint.endswith("/closes_issues"):
            payload = [{"iid": 12, "project_id": 310967}]
        elif endpoint.endswith("/pipelines"):
            payload = [
                {
                    "id": 3,
                    "status": "success",
                    "sha": "a" * 40,
                    "web_url": "https://pipeline/3",
                    "ref": "ignored",
                },
                {
                    "id": 2,
                    "status": "failed",
                    "sha": "b" * 40,
                    "web_url": "https://pipeline/2",
                },
            ]
        elif endpoint.endswith("/notes"):
            payload = {"id": 1}
        else:
            raise AssertionError(endpoint)
        return CommandResult(0, json.dumps(payload), "")


@pytest.mark.parametrize("target", [*LIFECYCLE_LABELS, None])
def test_gitlab_lifecycle_transition_never_requests_target_removal(
    tmp_path: Path, target: str | None
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    gateway.transition_labels(12, target)

    update = next(command for command in api.commands if "PUT" in command)
    remove_field = next(part for part in update if part.startswith("remove_labels="))
    removed = set(remove_field.removeprefix("remove_labels=").split(","))
    assert removed == set(LIFECYCLE_LABELS).difference({target})
    if target is not None:
        assert f"add_labels={target}" in update


def test_gitlab_gateway_enforces_eligible_selection_and_exact_evidence(
    tmp_path: Path,
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.verify_project().id == 310967
    assert [issue.iid for issue in gateway.list_eligible()] == [12]
    issue_list_calls = [
        command
        for command in api.commands
        if command[command.index("-X") + 2].endswith("/issues")
    ]
    assert all(
        not any(part.startswith("labels=") for part in command)
        for command in issue_list_calls
    )
    assert {issue.iid for issue in gateway.list_projected()} == {13}
    assert gateway.fetch_issue(12).description == ""
    assert gateway.transition_labels(12, "agent::running").labels == ("agent::running",)
    assert gateway.fetch_merge_request(88).iid == 88
    assert gateway.find_merge_request_by_branch("agent/issue-12") is not None
    draft = MergeRequestDraft(title="Change", description="Details")
    assert (
        gateway.create_merge_request(
            branch="agent/issue-12", target="main", issue_iid=12, draft=draft
        ).iid
        == 88
    )
    mr = gateway.ensure_closing_issue_reference(gateway.fetch_merge_request(88), 12)
    assert mr.description == "Closes #12"
    assert gateway.mr_closes_issue(88, 12)
    pipeline = gateway.latest_pipeline_for_sha(88, "a" * 40)
    assert pipeline is not None and pipeline.status == "success"
    assert gateway.latest_pipeline_for_sha(88, "c" * 40) is None
    gateway.comment_outcome(
        12, error_code="blocked", summary="Safe", next_action="Review"
    )
    outcome_call = next(
        command
        for command in api.commands
        if command[command.index("-X") + 2].endswith("/notes")
    )
    assert any(
        part.startswith("body=<!-- agent-work-outcome:v1 -->\nAgent outcome")
        for part in outcome_call
    )
    assert all(
        "projects/310967" in command[command.index("-X") + 2]
        for command in api.commands
    )


def test_gitlab_gateway_creates_and_reconciles_a_decision_review_draft(
    tmp_path: Path,
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)
    draft = MergeRequestDraft(
        title="Draft: retry policy decision",
        description="## Summary\nReview required.",
        is_draft=True,
        decision_review=_decision_review(),
    )

    created = gateway.create_merge_request(
        branch="agent/issue-12", target="main", issue_iid=12, draft=draft
    )
    stale = created.model_copy(
        update={"description": "Previously rendered.\ncloses # 12"}
    )
    reconciled = gateway.reconcile_merge_request(stale, issue_iid=12, draft=draft)

    post = next(command for command in api.commands if "POST" in command)
    assert not any(part.startswith("draft=") for part in post)
    assert any(part.startswith("title=Draft: retry policy decision") for part in post)
    assert reconciled.is_draft is True
    update = next(
        command
        for command in api.commands
        if "PUT" in command and command[command.index("-X") + 2].endswith("/88")
    )
    assert any(
        part.startswith("description=# ⚠️ Decision review required\n") for part in update
    )
    assert not any(part.startswith("draft=") for part in update)


def test_gitlab_gateway_reconciliation_is_idempotent_at_the_api_boundary(
    tmp_path: Path,
) -> None:
    """Two real gateway updates converge on one exact rendered description."""
    commands: list[tuple[str, ...]] = []
    responses = iter([_mr_payload(), _mr_payload()])
    draft = MergeRequestDraft(
        title="Draft: decision",
        description="Inline text: do not Closes #12 here.",
        is_draft=True,
        decision_review=_decision_review(),
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        response = next(responses)
        response["title"] = draft.title
        response["description"] = next(
            part.removeprefix("description=")
            for part in command
            if part.startswith("description=")
        )
        response["draft"] = True
        return CommandResult(0, json.dumps(response), "")

    gateway = GitLabGateway(_config(tmp_path), api)
    existing = _mr_evidence("Old.\nCLOSES # 12\nInline text: do not Closes #12 here.")
    first = gateway.reconcile_merge_request(existing, issue_iid=12, draft=draft)
    second = gateway.reconcile_merge_request(
        first.model_copy(update={"description": first.description + "\ncloses # 12"}),
        issue_iid=12,
        draft=draft,
    )

    assert first.description == second.description
    assert first.description.count("<!-- agent-work-decision-review:v1 -->") == 1
    assert sum(line == "Closes #12" for line in first.description.splitlines()) == 1
    assert first.description.endswith("\n\nCloses #12")
    assert "Inline text: do not Closes #12 here." in first.description
    assert len(commands) == 2
    assert not any(
        part.startswith("draft=") for command in commands for part in command
    )


def test_gitlab_gateway_preserves_an_existing_draft_title_and_evidence(
    tmp_path: Path,
) -> None:
    """Ordinary metadata must not downgrade an existing GitLab Draft."""
    commands: list[tuple[str, ...]] = []
    existing = _mr_evidence("Old details").model_copy(
        update={"title": "Draft: earlier decision", "is_draft": True}
    )
    draft = MergeRequestDraft(title="Ordinary update", description="Details")

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        response = _mr_payload(
            next(
                part.removeprefix("description=")
                for part in command
                if part.startswith("description=")
            )
        )
        response.update({"title": existing.title, "draft": True})
        return CommandResult(0, json.dumps(response), "")

    reconciled = GitLabGateway(_config(tmp_path), api).reconcile_merge_request(
        existing, issue_iid=12, draft=draft
    )

    assert reconciled.title == existing.title
    assert reconciled.is_draft is True
    assert not any(part.startswith("title=") for part in commands[0])
    assert not any(part.startswith("draft=") for part in commands[0])


@pytest.mark.parametrize(
    ("operation", "mismatched_field"),
    [
        ("create", "title"),
        ("create", "description"),
        ("create", "draft"),
        ("reconcile", "title"),
        ("reconcile", "description"),
        ("reconcile", "draft"),
    ],
)
def test_gitlab_gateway_rejects_response_that_does_not_apply_exact_metadata(
    tmp_path: Path, operation: str, mismatched_field: str
) -> None:
    """GitLab success is insufficient unless its returned metadata is exact."""
    draft = MergeRequestDraft(
        title="Draft: decision", description="Details", is_draft=True
    )

    def api(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        response = _mr_payload("Details\n\nCloses #12")
        response.update({"title": draft.title, "draft": draft.is_draft})
        response[mismatched_field] = {
            "title": "wrong title",
            "description": "wrong description",
            "draft": False,
        }[mismatched_field]
        assert any(part.startswith("description=") for part in command)
        return CommandResult(0, json.dumps(response), "")

    gateway = GitLabGateway(_config(tmp_path), api)
    if operation == "create":
        with pytest.raises(GitLabError, match="did not apply"):
            gateway.create_merge_request(
                branch="agent/issue-12", target="main", issue_iid=12, draft=draft
            )
    else:
        with pytest.raises(GitLabError, match="did not apply"):
            gateway.reconcile_merge_request(
                _mr_evidence("old"),
                issue_iid=12,
                draft=draft,
            )


def test_gitlab_eligibility_honors_valid_split_dependency_markers(
    tmp_path: Path,
) -> None:
    child_fingerprint = hashlib.sha256(b"310967:12:child").hexdigest()
    independent_fingerprint = hashlib.sha256(b"310967:12:independent").hexdigest()
    child = _issue_payload(20)
    child["description"] = (
        f"<!-- agent-work-split:v1 fingerprint={child_fingerprint} parent=12 key=child "
        "dependencies=19 -->\n\nParent: #12\nDepends-on: #19"
    )
    prerequisite = _issue_payload(19)
    prerequisite.update({"state": "opened", "labels": ["agent::awaiting-human"]})
    independent = _issue_payload(21)
    independent["description"] = (
        f"<!-- agent-work-split:v1 fingerprint={independent_fingerprint} parent=12 key=independent "
        "dependencies=none -->\n\nParent: #12"
    )
    issues = [child, prerequisite, independent]

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[command.index("-X") + 2].endswith("/issues")
        state = next(
            part.removeprefix("state=") for part in command if part.startswith("state=")
        )
        payload = (
            issues
            if state == "all"
            else [issue for issue in issues if issue["state"] == state]
        )
        return CommandResult(0, json.dumps(payload), "")

    gateway = GitLabGateway(_config(tmp_path), fake)

    assert [issue.iid for issue in gateway.list_eligible()] == [21]
    prerequisite["state"] = "closed"
    assert [issue.iid for issue in gateway.list_eligible()] == [20, 21]


def test_gitlab_split_mutations_use_exact_marker_bound_evidence(tmp_path: Path) -> None:
    """The real gateway creates, notes, and closes only exact split evidence."""
    commands: list[tuple[str, ...]] = []
    marker = (
        "<!-- agent-work-split:v1 fingerprint="
        + "a" * 64
        + " parent=12 key=child dependencies=none -->"
    )
    child = _issue_payload(20)
    child.update({"title": "Child", "description": marker})
    parent = _issue_payload(12)
    parent.update({"state": "closed"})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint.endswith("/issues") and method == "POST":
            return CommandResult(0, json.dumps(child), "")
        if endpoint.endswith("/issues/12/notes") and method == "GET":
            return CommandResult(0, "[]", "")
        if endpoint.endswith("/issues/12/notes"):
            return CommandResult(0, json.dumps({"id": 1}), "")
        if endpoint.endswith("/issues/12") and method == "PUT":
            return CommandResult(0, json.dumps(parent), "")
        raise AssertionError(endpoint)

    gateway = GitLabGateway(_config(tmp_path), fake)
    assert (
        gateway.create_split_child(title="Child", description=marker, marker=marker).iid
        == 20
    )
    gateway.ensure_decomposition_note(12, "note")
    assert gateway.close_issue(12).state == "closed"
    assert any(
        "title=Child" in command and f"description={marker}" in command
        for command in commands
    )
    assert any("body=note" in command for command in commands)
    assert any("state_event=close" in command for command in commands)


@pytest.mark.parametrize(
    ("child_update", "parent_update", "message"),
    [
        ({"description": "missing"}, {}, "split child evidence"),
        ({}, {"state": "opened"}, "did not close"),
    ],
)
def test_gitlab_split_mutations_reject_inexact_evidence(
    tmp_path: Path,
    child_update: dict[str, object],
    parent_update: dict[str, object],
    message: str,
) -> None:
    marker = (
        "<!-- agent-work-split:v1 fingerprint="
        + "a" * 64
        + " parent=12 key=child dependencies=none -->"
    )
    child = _issue_payload(20)
    child.update({"title": "Child", "description": marker, **child_update})
    parent = _issue_payload(12)
    parent.update({"state": "closed", **parent_update})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            json.dumps(
                child
                if command[command.index("-X") + 2].endswith("/issues")
                else parent
            ),
            "",
        )

    gateway = GitLabGateway(_config(tmp_path), fake)
    with pytest.raises(GitLabError, match=message):
        if child_update:
            gateway.create_split_child(title="Child", description=marker, marker=marker)
        else:
            gateway.close_issue(12)


def test_gitlab_split_note_is_idempotent_for_exact_existing_body(
    tmp_path: Path,
) -> None:
    """An exact durable decomposition note is never posted a second time."""
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        endpoint = command[command.index("-X") + 2]
        if endpoint.endswith("/issues/12/notes"):
            return CommandResult(
                0,
                json.dumps([_note_payload(1, body="existing decomposition")]),
                "",
            )
        raise AssertionError(endpoint)

    GitLabGateway(_config(tmp_path), fake).ensure_decomposition_note(
        12, "existing decomposition"
    )

    assert not any(command[command.index("-X") + 1] == "POST" for command in commands)


@pytest.mark.parametrize(
    "description",
    [
        "<!-- agent-work-split:v1 fingerprint=bad parent=12 key=child dependencies=none -->",
        "<!-- agent-work-split:v1 fingerprint="
        + hashlib.sha256(b"310967:12:child").hexdigest()
        + " parent=12 key=child dependencies=20 -->",
        "<!-- agent-work-split:v1 fingerprint="
        + hashlib.sha256(b"310967:12:child").hexdigest()
        + " parent=12 key=child dependencies=999 -->",
    ],
)
def test_gitlab_eligibility_fails_closed_for_malformed_or_invalid_split_dependencies(
    tmp_path: Path, description: str
) -> None:
    issue = _issue_payload(20)
    issue["description"] = description

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[command.index("-X") + 2].endswith("/issues")
        return CommandResult(0, json.dumps([issue]), "")

    with pytest.raises(GitLabError):
        GitLabGateway(_config(tmp_path), fake).list_eligible()


@pytest.mark.parametrize(
    ("labels", "state", "confidential", "eligible"),
    [
        ([], "opened", False, True),
        (["customer::priority"], "opened", False, True),
        (["agent::running"], "opened", False, False),
        (["agent::awaiting-human"], "opened", False, False),
        (["agent::blocked"], "opened", False, False),
        (["agent::failed"], "opened", False, False),
        ([], "opened", True, False),
        ([], "closed", False, False),
    ],
)
def test_gitlab_eligibility_excludes_every_ineligible_issue_kind(
    tmp_path: Path,
    labels: list[str],
    state: str,
    *,
    confidential: bool,
    eligible: bool,
) -> None:
    payload = _issue_payload(labels=labels)
    payload.update({"state": state, "confidential": confidential})

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert "state=opened" in command
        assert "order_by=created_at" in command
        assert "sort=asc" in command
        assert not any(part.startswith("labels=") for part in command)
        return CommandResult(0, json.dumps([payload]), "")

    issues = GitLabGateway(_config(tmp_path), fake).list_eligible()

    assert bool(issues) is eligible


def test_gitlab_eligible_inventory_reads_every_oldest_first_page(
    tmp_path: Path,
) -> None:
    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        page_field = next(part for part in command if part.startswith("page="))
        page = int(page_field.removeprefix("page="))
        payload = (
            [_issue_payload(iid) for iid in range(1, 101)]
            if page == 1
            else [_issue_payload(101)]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(_config(tmp_path), fake).list_eligible()

    assert [issue.iid for issue in issues] == list(range(1, 102))


def test_gitlab_projected_inventory_reads_every_issue_page(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        page_field = next(
            (part for part in command if part.startswith("page=")), "page=1"
        )
        page = int(page_field.removeprefix("page="))
        payload = (
            [_issue_payload(iid, ["agent::running"]) for iid in range(1, 101)]
            if page == 1
            else [_issue_payload(101, ["agent::running"])]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(config, fake).list_projected()

    assert len(issues) == 101
    assert issues[-1].iid == 101


def test_gitlab_issue_notes_are_paginated_and_deterministically_ordered(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        page = int(
            next(part for part in command if part.startswith("page=")).removeprefix(
                "page="
            )
        )
        payload = (
            [
                _note_payload(
                    note_id,
                    created_at=f"2026-01-01T00:{note_id % 60:02d}:00Z",
                )
                for note_id in range(100, 0, -1)
            ]
            if page == 1
            else [_note_payload(101, created_at="2026-01-01T00:59:00Z")]
        )
        return CommandResult(0, json.dumps(payload), "")

    notes = GitLabGateway(_config(tmp_path), fake).fetch_issue_notes(12)

    assert len(notes) == 101
    assert [(note.created_at, note.id) for note in notes] == sorted(
        (note.created_at, note.id) for note in notes
    )
    assert all("projects/310967/issues/12/notes" in command for command in calls)
    assert all("order_by=created_at" in command for command in calls)
    assert all("sort=asc" in command for command in calls)
    assert all("per_page=100" in command for command in calls)


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1},
        [_note_payload(1), _note_payload(1, body="conflicting")],
        [_note_payload(1, created_at="not-a-date")],
    ],
)
def test_gitlab_issue_notes_fail_closed_for_invalid_data(
    tmp_path: Path, payload: object
) -> None:
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda *_args, **_kwargs: CommandResult(0, json.dumps(payload), ""),
    )

    with pytest.raises(GitLabError, match="invalid issue note data"):
        gateway.fetch_issue_notes(12)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(1, "secret", "secret"), "request failed"),
        (CommandResult(0, "not-json", ""), "invalid JSON"),
    ],
)
def test_gitlab_gateway_redacts_failed_or_invalid_responses(
    tmp_path: Path, result: CommandResult, message: str
) -> None:
    gateway = GitLabGateway(_config(tmp_path), lambda *_args, **_kwargs: result)
    with pytest.raises(GitLabError, match=message):
        gateway.verify_project()


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://gitlab.example.com/group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
        (
            "git@gitlab.example.com:group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
    ],
)
def test_origin_identity_accepts_https_and_scp(
    origin: str, expected: tuple[str, str]
) -> None:
    assert _origin_identity(origin) == expected


def test_origin_identity_rejects_an_unusable_remote() -> None:
    with pytest.raises(RepositoryError):
        _origin_identity("local-path")


@pytest.mark.parametrize(
    "base_branch_fields",
    (
        pytest.param({}, id="omitted"),
        pytest.param({"base_branch": None}, id="null"),
    ),
)
def test_agent_work_config_requires_base_branch(
    tmp_path: Path, base_branch_fields: dict[str, object]
) -> None:
    payload = {
        "host": "gitlab.example.com",
        "project_id": 310967,
        "checkout": tmp_path / "checkout",
        "state_root": tmp_path / "state",
        **base_branch_fields,
    }

    with pytest.raises(ValidationError, match="base_branch"):
        AgentWorkConfig.model_validate(payload)


@pytest.mark.parametrize(
    "base_branch_fields",
    (
        pytest.param({}, id="missing"),
        pytest.param({"AGENT_WORK_BASE_BRANCH": ""}, id="empty"),
    ),
)
def test_discover_config_requires_base_branch_before_running_commands(
    tmp_path: Path, base_branch_fields: dict[str, str]
) -> None:
    calls: list[tuple[str, ...]] = []

    def unexpected_command(
        command: tuple[str, ...], **_kwargs: object
    ) -> CommandResult:
        calls.append(command)
        raise AssertionError("configuration discovery called a command")

    environment = {
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
        **base_branch_fields,
    }

    with pytest.raises(RepositoryError, match=r"^AGENT_WORK_BASE_BRANCH is required$"):
        discover_config(
            current_directory=tmp_path,
            environment=environment,
            run_command=unexpected_command,
        )

    assert calls == []


def test_discover_config_is_read_only_and_confirms_numeric_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if "rev-parse" in command:
            return CommandResult(0, str(checkout), "")
        if "get-url" in command:
            return CommandResult(0, "https://gitlab.example.com/group/project.git", "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "main",
                    "path_with_namespace": "group/project",
                }
            ),
            "",
        )

    config = discover_config(
        current_directory=tmp_path,
        environment={
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
            "AGENT_WORK_BASE_BRANCH": "agent/work-base",
        },
        run_command=fake,
    )
    assert config.project_id == 310967
    assert config.default_branch == "main"
    assert config.base_branch == "agent/work-base"
    assert config.state_root.parent.name == "projects"
    assert not any("pull" in command for command in calls)


def test_run_subprocess_captures_output_and_wraps_timeouts(fp: FakeProcess) -> None:
    fp.register(["printf", "ok"], stdout="ok")
    assert run_subprocess(("printf", "ok")).stdout == "ok"

    def timeout(_process: object) -> None:
        raise subprocess.TimeoutExpired(("slow-command",), 1)

    fp.register(["slow-command"], callback=timeout)
    with pytest.raises(RepositoryError, match="required command"):
        run_subprocess(("slow-command",), timeout_seconds=0.001)


def test_repository_prepares_publishes_and_inventories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()
    responses: dict[tuple[str, ...], CommandResult] = {}
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        args = command[3:]
        if args in responses:
            return responses[args]
        if args == ("branch", "--show-current"):
            branch = (
                config.base_branch
                if Path(command[2]) == config.checkout
                else "agent/issue-12"
            )
            return CommandResult(0, f"{branch}\n", "")
        if args == ("status", "--porcelain"):
            return CommandResult(0, "M file\n", "")
        if args == ("rev-parse", "HEAD") or args == (
            "rev-parse",
            "origin/agent/issue-12",
        ):
            return CommandResult(0, "a" * 40, "")
        if args[:2] == ("for-each-ref", "--format=%(refname:short)"):
            return CommandResult(0, "agent/issue-12\n", "")
        if args[:2] == ("worktree", "list"):
            return CommandResult(0, f"worktree {config.checkout}\n", "")
        if args[:2] == ("stash", "list"):
            return CommandResult(0, "agent-work recovery issue-12\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)
    with pytest.raises(RepositoryError, match="clean"):
        repository.refresh_base()
    responses[("status", "--porcelain")] = CommandResult(0, "", "")
    repository.refresh_base()
    worktree = repository.create_issue_worktree(12, "agent/issue-12", "main")
    assert worktree == config.state_root / "worktrees" / "12"
    responses[("status", "--porcelain")] = CommandResult(0, "M file\n", "")
    assert (
        repository.commit_and_push(
            worktree, "agent/issue-12", 12, expected_head="a" * 40
        )
        == "a" * 40
    )
    branches, worktrees, stashes = repository.inventory()
    assert branches == ("agent/issue-12",)
    assert worktrees == (str(config.checkout),)
    assert stashes == ("agent-work recovery issue-12",)
    assert any("pull" in command for command in calls)


def test_existing_mr_worktree_requires_the_recorded_remote_sha(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--list", "review/existing"):
            return CommandResult(0, "review/existing\n", "")
        if arguments == ("rev-parse", "origin/review/existing"):
            return CommandResult(0, "b" * 40, "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)

    with pytest.raises(RepositoryError, match="recorded merge request SHA"):
        repository.worktree_for_existing_branch(12, "review/existing", "a" * 40)


@pytest.mark.parametrize(
    ("branch", "head", "status"),
    [
        ("other", "a" * 40, "M src/example.py\n"),
        ("agent/issue-12", "b" * 40, "M src/example.py\n"),
        ("agent/issue-12", "a" * 40, ""),
    ],
)
def test_publication_rejects_wrong_branch_worker_commits_and_empty_changes(
    tmp_path: Path, branch: str, head: str, status: str
) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, branch, "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, head, "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)

    with pytest.raises(RepositoryError):
        repository.commit_and_push(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head="a" * 40,
        )


def test_agent_profiles_are_argv_only_and_invocation_redacts_secrets(
    tmp_path: Path,
) -> None:
    for name in ("codex", "claude", "pi"):
        command, stdin = _agent_command(name, f"/{name}", tmp_path, "prompt")
        assert command[0] == f"/{name}"
        assert (stdin == "prompt") is (name == "codex")
    with pytest.raises(WorkerProtocolError):
        _agent_command("other", "/other", tmp_path, "prompt")

    invoker = AgentInvoker(
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="token-value", stderr="token-value"
        ),
        environment={"AGENT_WORK_AGENT": "codex", "SECRET_TOKEN": "token-value"},
    )
    result = invoker.invoke(tmp_path, "prompt", 10)
    assert result.stdout == result.stderr == "[REDACTED]"


def test_codex_command_scopes_policy_writes_to_the_prepared_checkout(
    tmp_path: Path,
) -> None:
    command, stdin = _agent_command("codex", "/codex", tmp_path, "prompt")

    assert command == (
        "/codex",
        "exec",
        "-C",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--add-dir",
        str(tmp_path / ".agents"),
        "--add-dir",
        str(tmp_path / "template" / ".agents"),
        "--ephemeral",
        "--color",
        "never",
        "-",
    )
    assert stdin == "prompt"


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "usage: codex exec"),
        (0, "usage: codex exec\n  --sandbox workspace-write"),
    ],
)
def test_codex_preflight_rejects_an_unsupported_exec(
    tmp_path: Path, returncode: int, stdout: str
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="failure")

    invoker = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "SECRET_TOKEN": "token-value",
        },
    )

    with pytest.raises(WorkerProtocolError):
        invoker.preflight()

    assert calls == [
        (
            ("/codex", "exec", "--help"),
            {
                "stdin": None,
                "timeout_seconds": None,
                "environment": {
                    "AGENT_WORK_AGENT": "codex",
                    "AGENT_WORK_AGENT_PATH": "/codex",
                },
            },
        )
    ]


def test_codex_preflight_accepts_exec_with_scoped_directory_support() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout="usage: codex exec --add-dir DIR", stderr=""
        )

    invoker = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
    )

    invoker.preflight()

    assert calls[0][0] == ("/codex", "exec", "--help")


def test_local_operational_log_keeps_info_and_debug_at_requested_verbosity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ordinary = _build_local_logger(config, debug=False)
    ordinary.info("ordinary lifecycle")
    ordinary.debug("hidden diagnostic")
    for handler in ordinary.handlers:
        handler.flush()
    log_path = config.state_root / "agent-work.log"
    assert log_path.read_text() == "ordinary lifecycle\n"

    debug = _build_local_logger(config, debug=True)
    debug.debug("visible diagnostic")
    for handler in debug.handlers:
        handler.flush()
    assert "visible diagnostic" in log_path.read_text()


def test_local_operational_log_records_command_outcomes_and_failures(
    tmp_path: Path,
) -> None:
    logger = _build_local_logger(_config(tmp_path), debug=False)

    assert (
        main(
            ["loop", "status"],
            dispatch=lambda *_args, **_kwargs: CommandReport(
                command="loop.status", applied=False, outcome="status"
            ),
        )
        == 0
    )

    def fail(*_args: object, **_kwargs: object) -> CommandReport:
        raise RuntimeError("ledger unavailable")

    assert main(["loop", "status"], dispatch=fail) == 1
    for handler in logger.handlers:
        handler.flush()
    contents = (_config(tmp_path).state_root / "agent-work.log").read_text()
    assert "outcome=status command=loop.status" in contents
    assert "failure=ledger unavailable" in contents
