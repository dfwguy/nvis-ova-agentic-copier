# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for idempotent structured GitLab issue outcome notes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from agent_work.gitlab import GitLabError, GitLabGateway
from agent_work.models import AgentWorkConfig
from agent_work.ports import GitLabPort


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: str
    stderr: str = ""


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    ).model_copy(update={"factory_username": "factory-bot"})


def _marker(project_id: int, iid: int, outcome_key: str) -> str:
    """Return the stable marker for one configured project issue outcome."""
    identity = json.dumps(
        {"issue": iid, "outcome": outcome_key, "project": project_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "<!-- agent-work-outcome:v2 key="
        + hashlib.sha256(identity.encode()).hexdigest()
        + " -->"
    )


def _note(
    note_id: object, body: str, username: object = "factory-bot"
) -> dict[str, object]:
    """Build one API note payload with a configurable identity boundary."""
    return {"id": note_id, "body": body, "author": {"username": username}}


def _invoke(gateway: GitLabGateway, iid: int = 12, **overrides: object) -> None:
    """Call the approved public port signature with safe default display content."""
    values: dict[str, object] = {
        "outcome_key": "issue-180-outcome",
        "outcome": "Published a durable outcome note.",
        "explanation": "The state transition completed.",
        "evidence": "Focused behavior tests are red before implementation.",
        "retained_work": "No retained work.",
        "next_action": "Implement the gateway method.",
        "human_decision": "No decision is required.",
    }
    values.update(overrides)
    gateway.upsert_outcome_note(iid, **values)  # type: ignore[attr-defined]


def _method(command: tuple[str, ...]) -> str:
    """Read the explicit GitLab API method from an injected command."""
    return command[command.index("-X") + 1]


def _endpoint(command: tuple[str, ...]) -> str:
    """Read the explicit numeric-project endpoint from an injected command."""
    return command[command.index("-X") + 2]


@pytest.mark.parametrize(
    ("project_id", "iid", "outcome_key"),
    (
        pytest.param(True, 12, "key", id="boolean-project"),
        pytest.param(0, 12, "key", id="zero-project"),
        pytest.param(310967, True, "key", id="boolean-issue"),
        pytest.param(310967, 0, "key", id="zero-issue"),
        pytest.param(310967, 12, "", id="empty-key"),
        pytest.param(310967, 12, "k" * 513, id="unbounded-key"),
        pytest.param(310967, 12, "é" * 257, id="unbounded-utf8-key"),
    ),
)
def test_outcome_note_rejects_invalid_identity_or_key_before_commands(
    tmp_path: Path, project_id: object, iid: object, outcome_key: str
) -> None:
    """Untrusted identifiers and unbounded marker keys never reach GitLab."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        return _Result(0, "[]")

    config = _config(tmp_path).model_copy(update={"project_id": project_id})
    with pytest.raises(GitLabError):
        _invoke(
            GitLabGateway(config, fake), iid=cast("int", iid), outcome_key=outcome_key
        )

    assert calls == []


def test_outcome_note_posts_one_sanitized_labeled_body_when_marker_is_absent(
    tmp_path: Path,
) -> None:
    """A missing durable marker creates exactly one bounded structured note."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        return _Result(0, "[]" if _method(command) == "GET" else "{}")

    _invoke(
        GitLabGateway(_config(tmp_path), fake),
        outcome=" bearer glpat-secret\x00" + "é" * 600,
        explanation="",
        evidence="\n",
        retained_work="\x1b[31mkept",
        next_action="",
        human_decision="",
    )

    get_calls = [call for call in calls if _method(call) == "GET"]
    assert get_calls == [
        (
            str(_config(tmp_path).glab_path),
            "api",
            "--hostname",
            "gitlab.example.com",
            "-X",
            "GET",
            "projects/310967/issues/12/notes",
            "-f",
            "per_page=100",
            "-f",
            "page=1",
        )
    ]
    posts = [call for call in calls if _method(call) == "POST"]
    assert len(posts) == 1
    body = next(
        field.removeprefix("body=") for field in posts[0] if field.startswith("body=")
    )
    assert body.startswith(_marker(310967, 12, "issue-180-outcome") + "\n")
    assert "## Outcome\n[REDACTED]" in body
    assert "## What happened\nNot recorded." in body
    assert "## Evidence\nNot recorded." in body
    assert "## Retained work\nkept" in body
    assert "## Next action\nNot recorded." in body
    assert "## Human decision\nNot recorded." in body
    assert "\x00" not in body and "\x1b" not in body and "glpat-secret" not in body
    assert (
        len(body.encode())
        <= len(_marker(310967, 12, "issue-180-outcome").encode()) + 6 * 530
    )


def test_outcome_note_body_keeps_exact_sanitized_format(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        return _Result(0, "[]" if _method(command) == "GET" else "{}")

    _invoke(
        GitLabGateway(_config(tmp_path), fake),
        outcome="glpat-secret",
        explanation="\x00",
        evidence="é" * 300,
        retained_work="\x1b[31mkept",
        next_action="",
        human_decision="",
    )

    posts = [call for call in calls if _method(call) == "POST"]
    assert len(posts) == 1
    body = next(
        field.removeprefix("body=") for field in posts[0] if field.startswith("body=")
    )
    assert body == "\n\n".join(
        (
            _marker(310967, 12, "issue-180-outcome"),
            "## Outcome\n[REDACTED]",
            "## What happened\nNot recorded.",
            "## Evidence\n" + "é" * 256,
            "## Retained work\nkept",
            "## Next action\nNot recorded.",
            "## Human decision\nNot recorded.",
        )
    )


def test_outcome_note_scans_page_two_then_updates_only_its_factory_target(
    tmp_path: Path,
) -> None:
    """A valid target on page two is updated, never duplicated or skipped."""
    calls: list[tuple[str, ...]] = []
    marker = _marker(310967, 12, "issue-180-outcome")

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if _method(command) != "GET":
            return _Result(0, "{}")
        page = "page=2" if "page=2" in command else "page=1"
        notes = [_note(index, "unrelated") for index in range(100)]
        return _Result(
            0, json.dumps([_note(91, marker)] if page == "page=2" else notes)
        )

    _invoke(GitLabGateway(_config(tmp_path), fake))

    assert any("page=1" in call for call in calls)
    assert any("page=2" in call for call in calls)
    assert all("per_page=100" in call for call in calls if _method(call) == "GET")
    mutations = [call for call in calls if _method(call) in {"POST", "PUT"}]
    assert len(mutations) == 1
    assert _method(mutations[0]) == "PUT"
    assert _endpoint(mutations[0]) == "projects/310967/issues/12/notes/91"


@pytest.mark.parametrize(
    ("page_one_notes", "page_two_notes", "reason"),
    (
        pytest.param(
            [_note(91, _marker(310967, 12, "issue-180-outcome"))],
            [_note(91, "<!-- agent-work-outcome:v2 key=not-a-marker -->")],
            "malformed",
            id="malformed-factory-v2",
        ),
        pytest.param(
            [_note(91, _marker(310967, 12, "issue-180-outcome"))],
            [_note(92, _marker(310967, 12, "issue-180-outcome"))],
            "duplicate",
            id="duplicate-factory-target",
        ),
    ),
)
def test_outcome_note_fails_closed_for_page_two_authoritative_conflicts(
    tmp_path: Path,
    page_one_notes: list[dict[str, object]],
    page_two_notes: list[dict[str, object]],
    reason: str,
) -> None:
    """Factory-authored malformed or duplicate page-two records block mutation."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if _method(command) != "GET":
            raise AssertionError(command)
        notes = [_note(index, "unrelated") for index in range(100)]
        notes[: len(page_one_notes)] = page_one_notes
        return _Result(0, json.dumps(page_two_notes if "page=2" in command else notes))

    with pytest.raises(GitLabError, match=reason):
        _invoke(GitLabGateway(_config(tmp_path), fake))

    assert any("page=2" in call for call in calls)
    assert all("per_page=100" in call for call in calls if _method(call) == "GET")
    assert not [call for call in calls if _method(call) in {"POST", "PUT"}]


def test_outcome_note_fails_closed_for_mixed_valid_and_malformed_factory_marker(
    tmp_path: Path,
) -> None:
    """One factory body cannot mix a target marker with malformed v2 text."""
    calls: list[tuple[str, ...]] = []
    body = (
        _marker(310967, 12, "issue-180-outcome")
        + "\n<!-- agent-work-outcome:v2 key=not-a-marker -->"
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if _method(command) != "GET":
            raise AssertionError(command)
        return _Result(0, json.dumps([_note(91, body)]))

    with pytest.raises(GitLabError, match="malformed"):
        _invoke(GitLabGateway(_config(tmp_path), fake))

    assert not [call for call in calls if _method(call) in {"POST", "PUT"}]


@pytest.mark.parametrize(
    "note_id", (pytest.param(True, id="boolean"), pytest.param(0, id="zero"))
)
def test_outcome_note_rejects_invalid_factory_target_note_id_before_mutation(
    tmp_path: Path, note_id: object
) -> None:
    """A malformed authoritative target identity cannot select a PUT endpoint."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if _method(command) != "GET":
            raise AssertionError(command)
        return _Result(
            0,
            json.dumps([_note(note_id, _marker(310967, 12, "issue-180-outcome"))]),
        )

    with pytest.raises(GitLabError):
        _invoke(GitLabGateway(_config(tmp_path), fake))

    assert not [call for call in calls if _method(call) in {"POST", "PUT"}]


@pytest.mark.parametrize(
    "factory_note",
    (
        pytest.param(
            _note(91, _marker(310967, 12, "issue-180-outcome"), "reviewer"),
            id="foreign-target",
        ),
        pytest.param(_note(92, "<!-- agent-work-outcome:v1 -->"), id="legacy-factory"),
        pytest.param(
            _note(93, _marker(310967, 12, "other-key")), id="other-factory-key"
        ),
    ),
)
def test_outcome_note_ignores_non_authoritative_or_unrelated_records(
    tmp_path: Path, factory_note: dict[str, object]
) -> None:
    """Only an exact factory v2 target can suppress creation or be updated."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        return _Result(
            0, json.dumps([factory_note]) if _method(command) == "GET" else "{}"
        )

    _invoke(GitLabGateway(_config(tmp_path), fake))

    mutations = [call for call in calls if _method(call) in {"POST", "PUT"}]
    assert len(mutations) == 1
    assert _method(mutations[0]) == "POST"


def test_outcome_note_fails_closed_after_one_hundred_full_pages(
    tmp_path: Path,
) -> None:
    """An unbounded inventory never permits an ambiguous late mutation."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if _method(command) != "GET":
            raise AssertionError(command)
        return _Result(
            0, json.dumps([_note(index, "unrelated") for index in range(100)])
        )

    with pytest.raises(GitLabError, match="too many"):
        _invoke(GitLabGateway(_config(tmp_path), fake))

    assert sum(part.startswith("page=") for call in calls for part in call) == 100
    assert not [call for call in calls if _method(call) in {"POST", "PUT"}]


def test_outcome_note_replay_after_uncertain_post_updates_created_target_once(
    tmp_path: Path,
) -> None:
    """A stored-but-unacknowledged POST is recovered by one target-only PUT."""
    calls: list[tuple[str, ...]] = []
    stored: list[dict[str, object]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        method = _method(command)
        if method == "GET":
            return _Result(0, json.dumps(stored))
        if method == "POST":
            body = next(
                field.removeprefix("body=")
                for field in command
                if field.startswith("body=")
            )
            stored.append(_note(99, body))
            return _Result(1, "", "connection dropped after write")
        assert method == "PUT"
        return _Result(0, "{}")

    gateway = GitLabGateway(_config(tmp_path), fake)
    with pytest.raises(GitLabError):
        _invoke(gateway)
    _invoke(gateway)

    assert sum(_method(call) == "POST" for call in calls) == 1
    assert sum(_method(call) == "PUT" for call in calls) == 1
    assert len(stored) == 1
    assert _endpoint(calls[-1]) == "projects/310967/issues/12/notes/99"


def test_outcome_note_port_and_gateway_contracts_keep_template_mirrors_in_sync(
    tmp_path: Path, project_root: Path
) -> None:
    """Both portable copies expose the new port before their byte parity is checked."""
    calls: list[tuple[str, ...]] = []
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda command, **_kwargs: calls.append(command) or _Result(0, "[]"),
    )

    assert hasattr(GitLabPort, "upsert_outcome_note")
    _invoke(gateway)
    assert (project_root / "src/agent_work/gitlab.py").read_bytes() == (
        project_root / "template/src/agent_work/gitlab.py"
    ).read_bytes()
    assert (project_root / "src/agent_work/ports.py").read_bytes() == (
        project_root / "template/src/agent_work/ports.py"
    ).read_bytes()
