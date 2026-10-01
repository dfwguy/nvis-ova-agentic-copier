# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Authenticated numeric source adapter produces facts, never human approval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, cast

import pytest

from agent_work import runtime
from agent_work.gitlab import GitLabGateway
from agent_work.models import AgentWorkConfig
from agent_work.repository import CommandResult, CommandTimeoutError
from tests.test_agent_work_approved_scope import (
    AUTHOR,
    BODY,
    DESCRIPTION,
    HOST,
    NOTE,
    NOW,
    PROJECT,
    SERVICE,
    SOURCE,
    SourceFixture,
    approved,
    request,
)


class _ScopeGateway(Protocol):
    """The planned authenticated source method on the actual gateway."""

    def fetch_scope_source(
        self, bound: runtime.ScopeApprovalRequest
    ) -> runtime.ScopeSourceObservation: ...


def _config(root: Path) -> AgentWorkConfig:
    """Make all filesystem paths explicit and fixture owned."""
    return AgentWorkConfig(
        host=HOST,
        project_id=PROJECT,
        checkout=root / "checkout",
        state_root=root / "state",
        git_path=root / "git",
        glab_path=root / "glab",
        base_branch="agent/work-base",
        factory_username="factory-bot",
    )


class ApiFixture:
    """Typed command-boundary fake for actual GitLabGateway parsing."""

    def __init__(self, root: Path) -> None:
        self.config = _config(root)
        self.calls: list[tuple[str, ...]] = []
        self.responses: dict[str, object] = {
            f"projects/{PROJECT}/issues/{SOURCE}": {
                "id": 12345,
                "iid": SOURCE,
                "project_id": PROJECT,
                "description": DESCRIPTION,
                "updated_at": NOW.isoformat(),
            },
            f"projects/{PROJECT}/issues/{SOURCE}/notes": [
                {
                    "id": NOTE,
                    "body": BODY,
                    "system": False,
                    "created_at": NOW.replace(second=1).isoformat(),
                    "updated_at": NOW.replace(second=1).isoformat(),
                    "author": {"id": AUTHOR, "username": "breese"},
                }
            ],
            f"users/{AUTHOR}": {
                "id": AUTHOR,
                "username": "breese",
                "bot": False,
                "state": "active",
            },
            "user": {
                "id": SERVICE,
                "username": "factory-bot",
                "bot": True,
                "state": "active",
            },
        }
        self.failure: str | None = None
        self.failure_endpoint: str | None = None

    def run(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append(command)
        assert command[:6] == (
            str(self.config.glab_path),
            "api",
            "--hostname",
            HOST,
            "-X",
            "GET",
        )
        assert kwargs.get("timeout_seconds") is not None
        endpoint = command[6]
        path = endpoint.split("?", 1)[0]
        assert path in self.responses
        if (
            self.failure == "partial"
            and path.endswith("/notes")
            and ("page=2" in command or "page=2" in endpoint)
        ):
            return CommandResult(1, "", "synthetic-private-token")
        if self.failure_endpoint is None or self.failure_endpoint == path:
            if self.failure == "timeout":
                raise CommandTimeoutError("synthetic-private-token")
            if self.failure == "nonzero":
                return CommandResult(1, "", "synthetic-private-token")
            if self.failure == "malformed":
                return CommandResult(0, "synthetic-private-token", "")
            if self.failure == "oversized":
                return CommandResult(0, '"' + "x" * 1_000_001 + '"', "")
            if self.failure == "unicode":
                raise UnicodeDecodeError(
                    "utf8", b"\xff", 0, 1, "synthetic-private-token"
                )
        return CommandResult(0, json.dumps(self.responses[path]), "")

    def source(
        self, bound: runtime.ScopeApprovalRequest
    ) -> runtime.ScopeSourceObservation:
        return cast(
            _ScopeGateway, GitLabGateway(self.config, self.run)
        ).fetch_scope_source(bound)


def _adapter_control(root: Path) -> ApiFixture:
    """Validate real adapter facts and real approval consumer before mutation."""
    fake = ApiFixture(root)
    source = fake.source(request())
    assert source.project_id == PROJECT and source.issue_iid == SOURCE
    assert source.notes[0].id == NOTE and source.authors[0].id == AUTHOR
    assert source.service_actor_id == SERVICE and source.complete is True
    fixture = SourceFixture()
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=fixture.verify
    )
    assert result.state == "approved"
    assert {command[6].split("?", 1)[0] for command in fake.calls} == set(
        fake.responses
    )
    return fake


def test_actual_adapter_authenticates_numeric_source_and_author_without_promoting_nonbot(
    tmp_path: Path,
) -> None:
    fake = _adapter_control(tmp_path)
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=None
    )
    assert result.state == "unknown" and result.reason == "human_verifier_unavailable"
    assert all(command[5] == "GET" for command in fake.calls)


@pytest.mark.parametrize(
    ("target", "field", "value", "reason"),
    [
        ("issue", "project_id", PROJECT + 1, "identity_mismatch"),
        ("issue", "iid", SOURCE + 1, "identity_mismatch"),
        ("issue", "description", DESCRIPTION + "changed", "source_changed"),
        ("author", "id", AUTHOR + 1, "identity_mismatch"),
        ("author", "bot", True, "non_human_author"),
        ("author", "state", "blocked", "human_unverified"),
        ("author", "account_type", "service_account", "non_human_author"),
        ("self", "id", AUTHOR, "non_human_author"),
    ],
)
def test_adapter_single_identity_or_account_mutation_cannot_grant_approval(
    tmp_path: Path, target: str, field: str, value: object, reason: str
) -> None:
    fake = _adapter_control(tmp_path)
    endpoint = {
        "issue": f"projects/{PROJECT}/issues/{SOURCE}",
        "author": f"users/{AUTHOR}",
        "self": "user",
    }[target]
    data = fake.responses[endpoint]
    assert isinstance(data, dict)
    fake.responses[endpoint] = {**data, field: value}
    fixture = SourceFixture()
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=fixture.verify
    )
    assert result.state != "approved" and result.reason == reason


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "conflicting", "missing", "edited", "system", "malformed-author"],
)
def test_selected_source_reply_conflict_deletion_or_edit_is_not_proof(
    tmp_path: Path, fault: str
) -> None:
    fake = _adapter_control(tmp_path)
    endpoint = f"projects/{PROJECT}/issues/{SOURCE}/notes"
    notes = fake.responses[endpoint]
    assert isinstance(notes, list)
    note = notes[0]
    assert isinstance(note, dict)
    if fault == "duplicate":
        fake.responses[endpoint] = [note, dict(note)]
    elif fault == "conflicting":
        fake.responses[endpoint] = [note, {**note, "body": "other"}]
    elif fault == "missing":
        fake.responses[endpoint] = []
    elif fault == "edited":
        fake.responses[endpoint] = [
            {**note, "updated_at": NOW.replace(second=2).isoformat()}
        ]
    elif fault == "system":
        fake.responses[endpoint] = [{**note, "system": True}]
    else:
        fake.responses[endpoint] = [{**note, "author": {"id": True}}]
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=SourceFixture().verify
    )
    assert result.state != "approved"
    assert result.reason in {
        "incomplete_source",
        "event_missing",
        "event_changed",
        "human_unverified",
        "source_unavailable",
        "identity_mismatch",
        "non_human_author",
    }


@pytest.mark.parametrize(
    "fault", ["timeout", "nonzero", "malformed", "oversized", "unicode"]
)
@pytest.mark.parametrize("endpoint", ["issue", "notes", "author", "self"])
def test_each_authenticated_fetch_failure_is_unknown_without_private_output(
    tmp_path: Path, fault: str, endpoint: str
) -> None:
    fake = _adapter_control(tmp_path)
    fake.failure = fault
    fake.failure_endpoint = {
        "issue": f"projects/{PROJECT}/issues/{SOURCE}",
        "notes": f"projects/{PROJECT}/issues/{SOURCE}/notes",
        "author": f"users/{AUTHOR}",
        "self": "user",
    }[endpoint]
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=SourceFixture().verify
    )
    assert result.state == "unknown" and result.reason in {
        "source_unavailable",
        "incomplete_source",
    }
    assert "synthetic-private-token" not in result.model_dump_json()


def test_partial_notes_pagination_is_unknown_not_selected_note_approval(
    tmp_path: Path,
) -> None:
    fake = _adapter_control(tmp_path)
    endpoint = f"projects/{PROJECT}/issues/{SOURCE}/notes"
    notes = fake.responses[endpoint]
    assert isinstance(notes, list)
    note = notes[0]
    assert isinstance(note, dict)
    fake.responses[endpoint] = [{**note, "id": NOTE + index} for index in range(100)]
    fake.calls.clear()
    fake.failure = "partial"
    fake.failure_endpoint = endpoint
    result = runtime.capture_scope(
        request(), read_source=fake.source, verify_event=SourceFixture().verify
    )
    assert result.state == "unknown" and result.reason == "source_unavailable"
    note_calls = [
        command for command in fake.calls if command[6].split("?", 1)[0] == endpoint
    ]
    assert len(note_calls) == 2
    assert "page=2" in note_calls[-1] or "page=2" in note_calls[-1][6]


def test_generic_issue_note_and_serialized_host_claim_are_not_verifier_ports(
    tmp_path: Path,
) -> None:
    fake = _adapter_control(tmp_path)
    assert approved().state == "approved"
    payload = request().model_dump(mode="json")
    payload["host_decision"] = {"author_kind": "human", "decision": "approve"}
    with pytest.raises(ValueError):
        runtime.ScopeApprovalRequest.model_validate(payload)
    assert fake.source(request()).notes[0].body == BODY
