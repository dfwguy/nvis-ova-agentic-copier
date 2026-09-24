# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for resuming an exact MR from human feedback."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from pydantic import ValidationError

from agent_work.domain import (
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.gitlab import GitLabError, GitLabGateway, GitLabIdentityError
from agent_work.ledger import JsonLedger
from agent_work.models import (
    AgentWorkConfig,
    AuditCapacity,
    AuditReceiptSummary,
    GitLabIssue,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
    PipelineEvidence,
    TransitionIntentRecord,
)
from agent_work.red_checkpoint import RedCheckpointEvidence
from agent_work.repository import (
    RepositoryError,
    RepositoryTransientError,
    discover_config,
)
from agent_work.serial import run_segment as _run_segment
from agent_work.serial import select_record
from agent_work.state_machine import ALLOWED_TRANSITIONS
from agent_work.worker import build_worker_prompt, worker_exit_evidence
from tests.test_agent_work_serial_runtime import (
    NOW,
    FakeGitLab,
    FakeInvoker,
    FakeRepository,
    SingleSessionInvoker,
)


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: str
    stderr: str = ""


def run_segment(*args: Any, **kwargs: Any) -> LedgerRecord | None:
    """Run feedback fixtures with coherent controller-owned Red evidence."""
    if "load_red_checkpoint" not in kwargs:
        ledger = cast(JsonLedger, kwargs["ledger"])

        def load_red_checkpoint(
            project_id: int, issue_iid: int, base_sha: str
        ) -> RedCheckpointEvidence | None:
            record = ledger.load_read_only(issue_iid)
            if record is None or record.source_sha != base_sha:
                return None
            test_path = "tests/test_agent_work_mr_feedback.py"
            return RedCheckpointEvidence(
                project_id=project_id,
                issue_iid=issue_iid,
                source_branch=record.source_branch,
                base_sha=base_sha,
                tree_oid="b" * 40,
                ref=f"refs/agent-work/red-checkpoints/{issue_iid}/{base_sha}",
                test_paths=(test_path,),
                argv=("uv", "run", "pytest", f"{test_path}::test_red"),
                exit_code=1,
                expected_failure="assert expected failure",
                output_sha256=hashlib.sha256(b"assert expected failure\n").hexdigest(),
            )

        kwargs["load_red_checkpoint"] = load_red_checkpoint
    return _run_segment(*args, **kwargs)


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


def _serial_config(tmp_path: Path) -> AgentWorkConfig:
    """Build the focused serial-runner configuration."""
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )


def _issue(*, iid: int = 12, state: str = "opened") -> GitLabIssue:
    """Build the issue required by feedback-only contracts."""
    return GitLabIssue(
        iid=iid, title="Work", description="ordinary", labels=(), state=state
    )


def _mr(
    *,
    state: Literal["opened", "closed", "merged", "locked"] = "opened",
    source_project_id: int = 310967,
    target_project_id: int = 310967,
    source_branch: str = "agent/issue-12",
    target_branch: str = "main",
    sha: str = "a" * 40,
    title: str = "Change",
    is_draft: bool = False,
    description: str = "Closes #12",
) -> MergeRequestEvidence:
    """Build exact merge-request evidence for feedback contracts."""
    return MergeRequestEvidence(
        iid=88,
        web_url="https://gitlab.example.com/group/project/-/merge_requests/88",
        state=state,
        source_project_id=source_project_id,
        target_project_id=target_project_id,
        source_branch=source_branch,
        target_branch=target_branch,
        sha=sha,
        has_conflicts=False,
        title=title,
        is_draft=is_draft,
        description=description,
    )


def _serial_record(
    tmp_path: Path, *, state: RunnerState, phase: WorkflowPhase
) -> LedgerRecord:
    """Build the durable record required by feedback-only contracts."""
    return LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=state,
        phase=phase,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=tmp_path / "state" / "worktrees" / "12",
        source_sha="a" * 40,
    )


def _awaiting_record() -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=12,
        state=RunnerState.AWAITING_HUMAN,
        phase=WorkflowPhase.POST_PUBLICATION,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-12",
        worktree=Path("/state/worktrees/12"),
        merge_request_iid=88,
        merge_request_url="https://gitlab.example.com/group/project/-/merge_requests/88",
        source_sha="a" * 40,
    )


def test_legacy_schema_v1_record_loads_without_feedback_fields() -> None:
    restored = LedgerRecord.model_validate(_awaiting_record().model_dump(mode="json"))

    assert restored.feedback_cursor is None
    assert restored.pending_feedback is None


def test_awaiting_record_persists_strict_feedback_cursor_and_pending_batch() -> None:
    record = LedgerRecord.model_validate(
        _awaiting_record().model_dump(mode="json")
        | {
            "feedback_cursor": {
                "last_processed_general_note_id": 31,
                "last_processed_discussion_note_id": 44,
                "last_relevant_discussion_id": "discussion-9",
                "feedback_source_sha": "a" * 40,
            },
            "pending_feedback": {
                "target_cursor": {
                    "last_processed_general_note_id": 32,
                    "last_processed_discussion_note_id": 45,
                    "last_relevant_discussion_id": "discussion-9",
                    "feedback_source_sha": "a" * 40,
                },
                "source_sha": "a" * 40,
                "source": "discussion",
                "note_id": 45,
                "discussion_id": "discussion-9",
            },
        }
    )

    assert record.feedback_cursor is not None
    assert record.feedback_cursor.last_processed_general_note_id == 31
    assert record.pending_feedback is not None
    assert record.pending_feedback.target_cursor.last_processed_discussion_note_id == 45


def test_feedback_resume_edges_allow_repair_round_trip_without_publication() -> None:
    assert RunnerState.REPAIR in ALLOWED_TRANSITIONS[RunnerState.AWAITING_HUMAN]
    assert RunnerState.AWAITING_HUMAN in ALLOWED_TRANSITIONS[RunnerState.REPAIR]


def test_worker_prompt_serializes_chronological_untrusted_mr_feedback_section() -> None:
    prompt = build_worker_prompt(
        {"phase": "workflow"},
        project_id=310967,
        issue_iid=12,
        effective_worker_budget_seconds=1800,
        merge_request_feedback=(
            {
                "id": 45,
                "discussion_id": "discussion-9",
                "source": "discussion",
                "author": "reviewer",
                "created_at": datetime(2026, 9, 10, 12, tzinfo=UTC),
                "body": "Please cover the inline boundary.",
                "unseen": True,
            },
            {
                "id": 32,
                "discussion_id": None,
                "source": "general",
                "author": "maintainer",
                "created_at": datetime(2026, 9, 10, 11, tzinfo=UTC),
                "body": "Please consider this first.",
                "unseen": True,
            },
        ),
    )

    marker = "Untrusted merge-request feedback JSON:\n"
    rendered = prompt.split(marker, maxsplit=1)[1].split("\nEnd untrusted", maxsplit=1)[
        0
    ]
    feedback = json.loads(rendered)
    assert [note["id"] for note in feedback["notes"]] == [32, 45]
    assert feedback["notes"][1]["discussion_id"] == "discussion-9"
    assert feedback["notes"][1]["source"] == "discussion"
    assert "task data only" in prompt.lower()


def test_gateway_reads_paginated_general_and_nested_discussion_feedback_in_order(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def note(note_id: int, body: str, created_at: str) -> dict[str, object]:
        return {
            "id": note_id,
            "body": body,
            "author": {"username": "reviewer"},
            "created_at": created_at,
            "system": False,
        }

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        endpoint = command[command.index("-X") + 2]
        page = next((part for part in command if part.startswith("page=")), "page=1")
        if endpoint.endswith("/notes"):
            payload = (
                [note(31, "general", "2026-09-10T12:00:00Z")]
                if page == "page=1"
                else []
            )
        elif endpoint.endswith("/discussions"):
            payload = (
                [
                    {
                        "id": "discussion-9",
                        "notes": [
                            note(44, "older inline", "2026-09-10T11:00:00Z"),
                            note(45, "new reply", "2026-09-10T13:00:00Z"),
                        ],
                    }
                ]
                if page == "page=1"
                else []
            )
        else:
            raise AssertionError(endpoint)
        return _Result(0, json.dumps(payload))

    feedback = GitLabGateway(_config(tmp_path), fake).fetch_merge_request_feedback(88)

    assert [(entry.created_at, entry.id) for entry in feedback] == sorted(
        (entry.created_at, entry.id) for entry in feedback
    )
    assert [entry.id for entry in feedback] == [44, 31, 45]
    assert feedback[-1].discussion_id == "discussion-9"
    assert {entry.source for entry in feedback} == {"general", "discussion"}
    assert all(
        "projects/310967/merge_requests/88" in call[call.index("-X") + 2]
        for call in calls
    )
    assert not any("resolve" in " ".join(call) for call in calls)


def test_gateway_feedback_fails_closed_for_conflicting_duplicate(
    tmp_path: Path,
) -> None:
    payload = [
        {
            "id": 31,
            "body": "first",
            "author": {"username": "reviewer"},
            "created_at": "2026-09-10T12:00:00Z",
            "system": False,
        },
        {
            "id": 31,
            "body": "conflicting duplicate",
            "author": {"username": "reviewer"},
            "created_at": "2026-09-10T12:00:00Z",
            "system": False,
        },
    ]
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda command, **_kwargs: _Result(
            0,
            json.dumps(payload)
            if command[command.index("-X") + 2].endswith("/notes")
            else "[]",
        ),
    )

    with pytest.raises(GitLabError, match="feedback"):
        gateway.fetch_merge_request_feedback(88)


def test_gateway_feedback_fails_closed_for_authorless_note(
    tmp_path: Path,
) -> None:
    """An otherwise valid authorless note cannot enter the feedback prompt."""
    payload = [
        {
            "id": 31,
            "body": "Please revise.",
            "author": None,
            "created_at": "2026-09-10T12:00:00Z",
            "system": False,
        }
    ]
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda command, **_kwargs: _Result(
            0,
            json.dumps(payload)
            if command[command.index("-X") + 2].endswith("/notes")
            else "[]",
        ),
    )

    with pytest.raises(GitLabError, match="feedback"):
        gateway.fetch_merge_request_feedback(88)


def test_gateway_verifies_factory_identity_and_deduplicates_feedback_outcome_marker(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []
    identity = json.dumps(
        {
            "project": 310967,
            "issue": 12,
            "mr": 88,
            "source": "general",
            "note": 45,
            "discussion": None,
            "sha": "a" * 40,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    marker = (
        "<!-- agent-work-feedback-outcome:v2 key="
        + hashlib.sha256(identity.encode()).hexdigest()
        + " -->"
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        endpoint = command[command.index("-X") + 2]
        if endpoint == "user":
            return _Result(0, json.dumps({"username": "factory-bot"}))
        if endpoint.endswith("/notes") and command[command.index("-X") + 1] == "GET":
            page = command[
                command.index("page=1")
                if "page=1" in command
                else command.index("page=2")
            ]
            return _Result(
                0,
                json.dumps(
                    [
                        {
                            "id": 90,
                            "body": marker,
                            "author": {"username": "factory-bot"},
                            "created_at": "2026-09-10T12:00:00Z",
                            "system": False,
                        }
                    ]
                    if page == "page=2"
                    else [{"id": index, "body": "unrelated"} for index in range(100)]
                ),
            )
        raise AssertionError(command)

    gateway = GitLabGateway(_config(tmp_path), fake)
    gateway.verify_authenticated_user()
    gateway.comment_feedback_outcome(
        88,
        issue_iid=12,
        source="general",
        note_id=45,
        discussion_id=None,
        selection_sha="a" * 40,
        summary="No code changes were needed.",
    )

    assert not any(call[call.index("-X") + 1] == "POST" for call in calls)
    assert any("page=2" in call for call in calls)


def test_gateway_feedback_outcome_posts_when_its_marker_is_absent(
    tmp_path: Path,
) -> None:
    """An absent idempotency marker produces one concise acknowledgement."""
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if command[command.index("-X") + 1] == "GET":
            return _Result(0, "[]")
        return _Result(0, "{}")

    GitLabGateway(_config(tmp_path), fake).comment_feedback_outcome(
        88,
        issue_iid=12,
        source="general",
        note_id=45,
        discussion_id=None,
        selection_sha="a" * 40,
        summary="No code changes were needed.",
    )

    posts = [call for call in calls if call[call.index("-X") + 1] == "POST"]
    assert len(posts) == 1
    assert "agent-work-feedback-outcome:v2" in " ".join(posts[0])


def test_gateway_feedback_outcome_ignores_human_injected_marker(
    tmp_path: Path,
) -> None:
    """Only a service-authored durable marker can suppress an acknowledgement."""
    calls: list[tuple[str, ...]] = []
    identity = json.dumps(
        {
            "project": 310967,
            "issue": 12,
            "mr": 88,
            "source": "general",
            "note": 45,
            "discussion": None,
            "sha": "a" * 40,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    marker = (
        "<!-- agent-work-feedback-outcome:v2 key="
        + hashlib.sha256(identity.encode()).hexdigest()
        + " -->"
    )

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        calls.append(command)
        if command[command.index("-X") + 1] == "GET":
            return _Result(
                0,
                json.dumps(
                    [
                        {
                            "id": 90,
                            "body": marker,
                            "author": {"username": "reviewer"},
                        }
                    ]
                ),
            )
        return _Result(0, "{}")

    GitLabGateway(_config(tmp_path), fake).comment_feedback_outcome(
        88,
        issue_iid=12,
        source="general",
        note_id=45,
        discussion_id=None,
        selection_sha="a" * 40,
        summary="No code changes were needed.",
    )

    assert len([call for call in calls if call[call.index("-X") + 1] == "POST"]) == 1


def test_factory_mr_create_description_contains_one_feedback_notice_and_one_closing_reference(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> _Result:
        commands.append(command)
        description = next(
            part for part in command if part.startswith("description=")
        ).removeprefix("description=")
        return _Result(
            0,
            json.dumps(
                {
                    "iid": 88,
                    "web_url": "https://gitlab.example/mr/88",
                    "state": "opened",
                    "source_project_id": 310967,
                    "target_project_id": 310967,
                    "source_branch": "agent/issue-12",
                    "target_branch": "main",
                    "sha": "a" * 40,
                    "has_conflicts": False,
                    "description": description,
                    "title": "Change",
                    "draft": False,
                }
            ),
        )

    GitLabGateway(_config(tmp_path), fake).create_merge_request(
        branch="agent/issue-12",
        target="main",
        issue_iid=12,
        draft=MergeRequestDraft(title="Change", description="Details"),
    )

    description = next(part for part in commands[0] if part.startswith("description="))
    assert description.count("agent-work-feedback-notice:v1") == 1
    assert description.count("Closes #12") == 1


@pytest.mark.parametrize("username", ("other-bot", "", None))
def test_gateway_rejects_authenticated_identity_that_does_not_match_config(
    tmp_path: Path, username: str | None
) -> None:
    gateway = GitLabGateway(
        _config(tmp_path),
        lambda *_args, **_kwargs: _Result(0, json.dumps({"username": username})),
    )

    with pytest.raises(GitLabError, match="identity"):
        gateway.verify_authenticated_user()


@pytest.mark.parametrize("username", ("", " has spaces", "bad/slash"))
def test_config_requires_a_nonempty_safe_factory_username(
    tmp_path: Path, username: str
) -> None:
    payload = _config(tmp_path).model_dump()
    payload["factory_username"] = username

    with pytest.raises(ValidationError):
        AgentWorkConfig.model_validate(payload)


def test_discovery_requires_factory_username_before_any_worker_can_run(
    tmp_path: Path,
) -> None:
    with pytest.raises(RepositoryError, match="FACTORY_USERNAME"):
        discover_config(
            current_directory=tmp_path,
            environment={
                "AGENT_WORK_BASE_BRANCH": "agent/work-base",
                "AGENT_WORK_GIT": str(tmp_path / "bin" / "git"),
                "AGENT_WORK_GLAB": str(tmp_path / "bin" / "glab"),
            },
            run_command=lambda *_args, **_kwargs: _Result(1, ""),
        )


class _FeedbackGitLab(FakeGitLab):
    """Serial-runner fake exposing only the proposed MR-feedback boundary."""

    def __init__(self) -> None:
        super().__init__(
            _issue().model_copy(update={"labels": ("agent::awaiting-human",)})
        )
        self.handoff_keys.add((12, 88, "a" * 40))
        self.feedback: tuple[MergeRequestFeedback, ...] = ()
        self.feedback_by_mr: dict[int, tuple[MergeRequestFeedback, ...]] = {}
        self.feedback_outcomes: list[tuple[int, int, int, str]] = []
        self.feedback_error: GitLabError | None = None
        self.identity_checks = 0
        self.identity_error_on_check: int | None = None

    def fetch_merge_request_feedback(
        self, iid: int
    ) -> tuple[MergeRequestFeedback, ...]:
        if self.feedback_error is not None:
            raise self.feedback_error
        return self.feedback_by_mr.get(iid, self.feedback)

    def verify_authenticated_user(self) -> str | None:
        self.identity_checks += 1
        if self.identity_error_on_check == self.identity_checks:
            raise GitLabIdentityError("GitLab authenticated identity changed")
        return self.authenticated_username

    def comment_feedback_outcome(
        self,
        iid: int,
        *,
        issue_iid: int,
        source: str,
        note_id: int,
        discussion_id: str | None,
        selection_sha: str,
        summary: str,
    ) -> None:
        del source, discussion_id
        self.feedback_outcomes.append((iid, issue_iid, note_id, selection_sha))


def _feedback(
    note_id: int,
    *,
    source: Literal["general", "discussion"] = "general",
    discussion_id: str | None = None,
    body: str = "Please revise.",
    author: str = "reviewer",
    system: bool = False,
    created_at: datetime | str = datetime(2026, 9, 10, 12, tzinfo=UTC),
) -> MergeRequestFeedback:
    return MergeRequestFeedback.model_validate(
        {
            "id": note_id,
            "source": source,
            "discussion_id": discussion_id,
            "body": body,
            "author": author,
            "system": system,
            "created_at": created_at,
        }
    )


def _awaiting_feedback_record(tmp_path: Path) -> LedgerRecord:
    return _serial_record(
        tmp_path, state=RunnerState.AWAITING_HUMAN, phase=WorkflowPhase.POST_PUBLICATION
    ).model_copy(
        update={
            "merge_request_iid": 88,
            "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/88",
            "feedback_cursor": None,
            "pending_feedback": None,
        }
    )


def test_unseen_general_feedback_wakes_only_its_awaiting_issue_and_invokes_workflow(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    invoke = FakeInvoker()

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.state is RunnerState.REPAIR
    assert resumed.phase is WorkflowPhase.WORKFLOW
    assert invoke.prompts == []
    assert gitlab.created_merge_requests == 0


def test_transient_feedback_poll_failure_retries_without_side_effects_then_resumes(
    tmp_path: Path,
) -> None:
    """Feedback polling retries durably before polling normally again."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback_error = GitLabError("temporary feedback failure")
    repository = FakeRepository(config)

    assert (
        run_segment(
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=FakeInvoker(),
            timeout_seconds=30,
            now=NOW,
        )
        is None
    )
    retry = ledger.load(12)

    assert retry is not None and retry.state is RunnerState.RETRY_WAIT
    assert retry.error_code is ErrorCode.GITLAB_RETRYABLE
    assert retry.prior_state is RunnerState.AWAITING_HUMAN
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0
    assert repository.prepared is False and repository.pushes == 0

    gitlab.feedback_error = None
    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW + timedelta(minutes=20),
    )
    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW + timedelta(minutes=20),
    )

    assert recovered is not None and recovered.state is RunnerState.AWAITING_HUMAN
    assert resumed is None


def test_unverified_feedback_wake_retries_locally_without_immediate_reentry(
    tmp_path: Path,
) -> None:
    """Feedback identity transport failures retry without touching remote state."""

    class FailingIdentityGitLab(_FeedbackGitLab):
        def verify_authenticated_user(self) -> None:
            self.identity_checks += 1
            raise GitLabError("identity endpoint remains unavailable")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _awaiting_feedback_record(tmp_path)
    ledger.persist(record)
    gitlab = FailingIdentityGitLab()
    repository = FakeRepository(config)
    invoke = FakeInvoker()
    labels_before = list(gitlab.labels)
    comments_before = list(gitlab.comments)
    worktree_before = record.worktree

    initial = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert initial is None
    first_retry = ledger.load(record.issue_iid)
    assert first_retry is not None and first_retry.state is RunnerState.RETRY_WAIT
    assert first_retry.error_code is ErrorCode.GITLAB_RETRYABLE
    assert first_retry.retry_count == 1
    assert first_retry.prior_state is RunnerState.AWAITING_HUMAN
    assert first_retry.prior_phase is WorkflowPhase.POST_PUBLICATION
    assert first_retry.retry_at == NOW + timedelta(minutes=20)
    first_retry_at = first_retry.retry_at
    assert first_retry_at is not None
    assert first_retry.worktree == worktree_before
    assert gitlab.identity_checks == 1
    assert gitlab.labels == labels_before and gitlab.comments == comments_before
    assert gitlab.feedback_outcomes == [] and gitlab.created_merge_requests == 0
    assert invoke.prompts == []
    assert repository.prepared is False and repository.pushes == 0
    assert repository.synced_heads == [] and repository.verified_heads == []

    selected, issue = select_record(ledger, gitlab, now=NOW, repository=repository)
    assert selected is None and issue is None
    assert gitlab.identity_checks == 1

    second = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=first_retry_at,
    )  # type: ignore[arg-type]
    assert second is not None and second.state is RunnerState.RETRY_WAIT
    assert second.retry_count == 2
    assert second.prior_state is RunnerState.AWAITING_HUMAN
    assert second.retry_at == NOW + timedelta(minutes=40)
    second_retry_at = second.retry_at
    assert second_retry_at is not None

    third = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=second_retry_at,
    )  # type: ignore[arg-type]
    assert third is not None and third.state is RunnerState.RETRY_WAIT
    assert third.retry_count == 3
    assert third.prior_state is RunnerState.AWAITING_HUMAN
    assert third.retry_at == NOW + timedelta(minutes=60)
    third_retry_at = third.retry_at
    assert third_retry_at is not None

    exhausted = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=third_retry_at,
    )  # type: ignore[arg-type]
    assert exhausted is not None and exhausted.state is RunnerState.BLOCKED
    assert exhausted.error_code is ErrorCode.RETRY_EXHAUSTED
    assert exhausted.retry_at is None and exhausted.worktree == worktree_before
    assert exhausted.prior_state is RunnerState.AWAITING_HUMAN
    assert gitlab.identity_checks == 4
    assert gitlab.labels == labels_before and gitlab.comments == comments_before
    assert gitlab.feedback_outcomes == [] and gitlab.created_merge_requests == 0
    assert invoke.prompts == []
    assert repository.prepared is False and repository.pushes == 0
    assert repository.synced_heads == [] and repository.verified_heads == []


def test_inline_reply_in_existing_discussion_wakes_and_multiple_feedback_is_chronological(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (
        _feedback(
            45,
            source="discussion",
            discussion_id="older-thread",
            body="new reply",
            created_at="2026-09-10T13:00:00Z",
        ),
        _feedback(31, body="general first", created_at="2026-09-10T11:00:00Z"),
        _feedback(
            44,
            source="discussion",
            discussion_id="older-thread",
            body="inline",
            created_at="2026-09-10T12:00:00Z",
        ),
    )
    repository = FakeRepository(config)
    invoke = FakeInvoker()

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.pending_feedback is not None
    assert invoke.prompts == []

    executed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert executed is not None
    assert executed.pending_feedback is not None


def test_feedback_repair_worker_exit_retries_immediately_with_diagnostic_and_pending_feedback(
    tmp_path: Path,
) -> None:
    """A failed feedback repair retains its selected acknowledgement for retry."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)

    woken = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(returncode=23),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert woken is not None and woken.state is RunnerState.REPAIR
    assert retry is not None and retry.state is RunnerState.RETRY_WAIT
    assert retry.retry_at == NOW
    assert retry.pending_feedback is not None
    assert retry.pending_feedback.note_id == 31
    assert retry.worker_exit is not None
    assert retry.worker_exit.returncode == 23
    assert retry.worker_exit.diagnostic == "safe diagnostic"
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0 and repository.pushes == 0


def test_worker_exit_evidence_sanitizes_and_bounds_untrusted_stderr() -> None:
    """Durable worker diagnostics exclude prompt and terminal-control content."""
    evidence = worker_exit_evidence(
        23,
        "prompt content\x1b[2J\r\ncredential-value " + "é" * 2_000,
        prompt="prompt content",
        environment={"SERVICE_CREDENTIAL": "credential-value"},
    )

    assert evidence.returncode == 23
    assert "prompt content" not in evidence.diagnostic
    assert "credential-value" not in evidence.diagnostic
    assert "\x1b" not in evidence.diagnostic
    assert "\n" not in evidence.diagnostic
    assert len(evidence.diagnostic.encode()) <= 2_048


@pytest.mark.parametrize(
    "note",
    (
        _feedback(31, system=True),
        _feedback(31, author="factory-bot"),
        _feedback(31, body="<!-- agent-work-outcome:v1 -->\nold outcome"),
        _feedback(31, body="<!-- agent-work-feedback-outcome:v1 -->\nnew outcome"),
        _feedback(31, body="<!-- agent-work-feedback-outcome:v2 -->\nnew outcome"),
    ),
)
def test_system_factory_and_outcome_notes_do_not_wake(
    tmp_path: Path, note: MergeRequestFeedback
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (note,)
    repository = FakeRepository(config)
    invoke = FakeInvoker()

    assert (
        run_segment(
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=invoke,
            timeout_seconds=30,
            now=NOW,
        )
        is None
    )  # type: ignore[arg-type]
    assert invoke.prompts == []


def test_noop_feedback_posts_once_advances_cursor_and_never_publishes(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ()

    woken = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert woken is not None and woken.pending_feedback is not None
    final = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert final is not None and final.state is RunnerState.AWAITING_HUMAN
    assert final.feedback_cursor is not None
    assert final.feedback_cursor.last_processed_general_note_id == 31
    assert final.pending_feedback is None
    assert repository.prepared is False and repository.pushes == 0
    assert gitlab.created_merge_requests == 0 and gitlab.reconciled_drafts == []
    assert len(gitlab.feedback_outcomes) == 1


def test_noop_feedback_dirty_worktree_blocks_before_acknowledgement(
    tmp_path: Path,
) -> None:
    """Keep a selected no-op comment pending until its worktree is clean."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ()
    repository.dirty = True

    woken = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert woken is not None and woken.pending_feedback is not None
    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.pending_feedback is not None
    assert blocked.feedback_cursor is None
    assert gitlab.feedback_outcomes == []


def test_noop_feedback_does_not_persist_model_authored_receipts(
    tmp_path: Path,
) -> None:
    """A feedback result advances without turning model prose into evidence."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ()

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    final = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert final is not None
    assert final.receipts == () and final.audit_evidence == "unknown"


def test_identity_drift_after_pipeline_success_blocks_before_feedback_acknowledgement(
    tmp_path: Path,
) -> None:
    """A changed service identity may not acknowledge completed feedback."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/example.py",)
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None and publishing.source_sha is not None
    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    gitlab.pipeline = PipelineEvidence(
        id=1,
        status="success",
        sha=publishing.source_sha,
        web_url="https://gitlab.example/pipelines/1",
    )
    gitlab.identity_error_on_check = gitlab.identity_checks + 2
    labels_before = list(gitlab.labels)
    comments_before = list(gitlab.comments)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert gitlab.feedback_outcomes == []
    assert "agent::blocked" not in gitlab.labels[len(labels_before) :]
    assert gitlab.comments == comments_before


def test_noop_feedback_ack_failure_does_not_advance_cursor_and_replay_deduplicates(
    tmp_path: Path,
) -> None:
    """A failed acknowledgement retains pending work; idempotent retry posts once."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ()

    def failing_ack(
        _iid: int,
        *,
        issue_iid: int,
        source: str,
        note_id: int,
        discussion_id: str | None,
        selection_sha: str,
        summary: str,
    ) -> None:
        del issue_iid, source, note_id, discussion_id, selection_sha, summary
        raise GitLabError("temporary")

    object.__setattr__(gitlab, "comment_feedback_outcome", failing_ack)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    retained = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert retained is not None and retained.pending_feedback is not None
    assert retained.feedback_cursor is None
    assert retained.state is RunnerState.RETRY_WAIT
    assert retained.retry_at is not None
    object.__setattr__(
        gitlab,
        "comment_feedback_outcome",
        _FeedbackGitLab.comment_feedback_outcome.__get__(gitlab),
    )
    waiting = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert waiting is None
    assert ledger.load(12) == retained
    assert gitlab.feedback_outcomes == []

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=retained.retry_at,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.pending_feedback is None
    assert resumed.feedback_cursor is not None
    assert len(gitlab.feedback_outcomes) == 1


def test_changed_feedback_reuses_recorded_mr_preserves_metadata_and_acknowledges_after_exact_new_sha_pipeline(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    gitlab.existing_mr = _mr(
        title="Draft: retained", is_draft=True, description="Retained\n\nCloses #12"
    )
    repository = FakeRepository(config)
    repository.paths = ("src/example.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/example.py",)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert publishing is not None and publishing.state is RunnerState.PUBLISHING
    assert publishing.pending_feedback is not None
    assert publishing.pending_feedback.outcome is not None
    assert gitlab.created_merge_requests == 0
    assert gitlab.fetched_merge_requests == [88]
    assert (
        gitlab.existing_mr is not None and gitlab.existing_mr.title == "Draft: retained"
    )
    assert gitlab.existing_mr.description == "Retained\n\nCloses #12"
    published = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert published is not None and published.state is RunnerState.OBSERVING
    assert repository.prepared_paths == ("src/example.py",)
    assert gitlab.reconciled_drafts == []
    assert gitlab.closing_reference_updates == 0
    assert gitlab.feedback_outcomes == []
    assert published.source_sha is not None
    gitlab.pipeline = PipelineEvidence(
        id=1,
        status="success",
        sha=published.source_sha,
        web_url="https://gitlab.example/pipelines/1",
    )
    assessed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert assessed is not None and assessed.state is RunnerState.EXECUTING
    assert assessed.phase is WorkflowPhase.POST_PUBLICATION
    final = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert final is not None and final.state is RunnerState.AWAITING_HUMAN
    assert final.pending_feedback is None
    assert final.feedback_cursor is not None
    assert final.feedback_cursor.feedback_source_sha == published.source_sha
    assert len(gitlab.feedback_outcomes) == 1


def test_feedback_publication_blocks_when_worktree_no_longer_matches_controller_intent_paths(
    tmp_path: Path,
) -> None:
    """Publication may stage only the controller-validated feedback result."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/review_fix.py",)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None and publishing.audit_evidence is not None
    repository.paths = ("secrets/customer-export.csv",)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.pushes == 0
    assert "customer-export.csv" not in blocked.summary
    assert "customer-export.csv" not in blocked.next_action


def _controller_feedback_publication(
    tmp_path: Path,
) -> tuple[JsonLedger, _FeedbackGitLab, FakeRepository, LedgerRecord]:
    """Create one publication bound to controller-observed feedback paths."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    publication = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    assert publication is not None and publication.intent is not None
    return ledger, gitlab, repository, publication


@pytest.mark.parametrize("defect", ("missing", "noncanonical", "extra", "fingerprint"))
def test_feedback_publication_rejects_invalid_controller_intent_paths(
    tmp_path: Path, defect: str
) -> None:
    """Only the exact canonical path set from the trusted transition may publish."""
    ledger, gitlab, repository, publication = _controller_feedback_publication(tmp_path)
    assert publication.intent is not None
    evidence = dict(publication.intent.expected_evidence)
    if defect == "missing":
        evidence.pop("validated_paths")
    elif defect == "noncanonical":
        evidence["validated_paths"] = '["src/z.py","src/a.py"]'
    elif defect == "extra":
        paths = ("src/extra.py", "src/review_fix.py")
        evidence.update(
            {
                "validated_path_count": len(paths),
                "validated_paths_fingerprint": hashlib.sha256(
                    "".join(f"{path}\0" for path in paths).encode()
                ).hexdigest(),
                "validated_paths": json.dumps(paths, separators=(",", ":")),
            }
        )
    else:
        evidence["validated_paths_fingerprint"] = "f" * 64
    ledger.persist(
        publication.model_copy(
            update={
                "intent": publication.intent.model_copy(
                    update={"expected_evidence": evidence}
                )
            }
        )
    )

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.pushes == 0


@pytest.mark.parametrize(
    "historical_paths", (("src/review_fix.py",), ("secrets/forged.txt",))
)
def test_historical_audit_paths_cannot_change_feedback_publication(
    tmp_path: Path, historical_paths: tuple[str, ...]
) -> None:
    """Historical model prose cannot alter controller-owned intent paths."""
    ledger, gitlab, repository, publication = _controller_feedback_publication(tmp_path)
    historical = AuditReceiptSummary(
        lane="coding",
        capacity=AuditCapacity(required=12, available=16, shortfall=0, decision="run"),
        role_receipts=("$tdd-finalizer: handcrafted",),
        changed_paths=historical_paths,
        tests=("handcrafted test prose",),
        reviewers=("$tdd-judge: handcrafted",),
        findings=(),
        corrections=(),
    )
    ledger.persist(publication.model_copy(update={"audit_evidence": historical}))

    published = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )

    assert published is not None and published.state is RunnerState.OBSERVING
    assert repository.prepared_paths == ("src/review_fix.py",)


def test_feedback_publication_replay_before_commit_prepares_exact_durable_intent(
    tmp_path: Path,
) -> None:
    """An interrupted feedback publication resumes its one validated commit."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = repository.paths

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None and publishing.audit_evidence is not None
    fingerprint = hashlib.sha256(b"src/review_fix.py\0").hexdigest()
    ledger.persist(
        publishing.model_copy(
            update={
                "intent": TransitionIntentRecord(
                    operation_id=f"12:publish:{publishing.revision}",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "validated_path_count": 1,
                        "validated_paths_fingerprint": fingerprint,
                        "validated_paths": '["src/review_fix.py"]',
                    },
                )
            }
        )
    )

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.state is RunnerState.OBSERVING
    assert resumed.publication_sha == "a" * 40
    assert repository.prepared_paths == ("src/review_fix.py",)
    assert repository.pushes == 1


@pytest.mark.parametrize(
    ("retained_paths", "expected_state"),
    [
        (("src/review_fix.py",), RunnerState.OBSERVING),
        (("src/unrelated.py",), RunnerState.BLOCKED),
    ],
)
def test_feedback_publication_replay_after_commit_binds_only_an_exact_path_set(
    tmp_path: Path, retained_paths: tuple[str, ...], expected_state: RunnerState
) -> None:
    """A retained feedback commit must match its durable validated path set."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/review_fix.py",)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None
    retained_sha = "b" * 40
    fingerprint = hashlib.sha256(b"src/review_fix.py\0").hexdigest()
    repository.current_head = retained_sha
    repository.bound_sha = retained_sha
    repository.paths = retained_paths
    repository.dirty = False
    gitlab.existing_mr = _mr()
    repository.push_observer = lambda _sha: setattr(
        gitlab, "existing_mr", _mr(sha=retained_sha)
    )
    ledger.persist(
        publishing.model_copy(
            update={
                "intent": TransitionIntentRecord(
                    operation_id=f"12:publish:{publishing.revision}",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "validated_path_count": 1,
                        "validated_paths_fingerprint": fingerprint,
                        "validated_paths": '["src/review_fix.py"]',
                    },
                )
            }
        )
    )

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.state is expected_state
    if expected_state is RunnerState.BLOCKED:
        assert resumed.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
        assert repository.binds == 0
        assert repository.pushes == 0
    else:
        assert resumed.publication_sha == retained_sha
        assert repository.binds == 1
        assert repository.prepares == 0
        assert repository.pushes == 1
        assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize("dirty", (False, True), ids=("clean", "dirty"))
def test_feedback_publication_replay_after_push_requires_a_clean_exact_worktree(
    tmp_path: Path, dirty: bool
) -> None:
    """An already-pushed feedback SHA never repushes and never accepts dirt."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/review_fix.py",)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None
    published_sha = "b" * 40
    fingerprint = hashlib.sha256(b"src/review_fix.py\0").hexdigest()
    repository.current_head = published_sha
    repository.dirty = dirty
    gitlab.existing_mr = _mr(sha=published_sha)
    ledger.persist(
        publishing.model_copy(
            update={
                "publication_sha": published_sha,
                "intent": TransitionIntentRecord(
                    operation_id=f"12:publish:{publishing.revision}",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "publication_sha": published_sha,
                        "validated_path_count": 1,
                        "validated_paths_fingerprint": fingerprint,
                        "validated_paths": '["src/review_fix.py"]',
                    },
                ),
            }
        )
    )

    replayed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    if dirty:
        assert replayed is not None and replayed.state is RunnerState.BLOCKED
        assert replayed.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    else:
        assert replayed is not None and replayed.state is RunnerState.OBSERVING
    assert repository.pushes == 0
    assert repository.prepares == 0


def test_dirty_feedback_post_publication_reroutes_before_acknowledgement(
    tmp_path: Path,
) -> None:
    """Retained feedback dirt must be republished before it can be acknowledged."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    repository = FakeRepository(config)
    repository.paths = ("src/review_fix.py",)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = repository.paths

    selected = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert selected is not None and selected.state is RunnerState.REPAIR
    publishing = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert publishing is not None and publishing.state is RunnerState.PUBLISHING
    assert publishing.audit_evidence == "unknown"
    assert publishing.intent is not None
    assert publishing.intent.expected_evidence["validated_paths"] == json.dumps(
        repository.paths, separators=(",", ":")
    )
    observed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert observed is not None and observed.state is RunnerState.OBSERVING
    post_publication = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert post_publication is not None
    assert post_publication.state is RunnerState.EXECUTING
    assert post_publication.phase is WorkflowPhase.POST_PUBLICATION
    repository.dirty = True

    rerouted = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=SingleSessionInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert rerouted is not None
    assert rerouted.state is RunnerState.PUBLISHING
    assert rerouted.phase is WorkflowPhase.READY_TO_PUBLISH
    assert rerouted.pending_feedback is not None
    assert rerouted.pending_feedback.acknowledgement_ready is False
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    "repair_error",
    (ErrorCode.MERGE_CONFLICT, ErrorCode.PIPELINE_REJECTED),
)
def test_feedback_repair_is_not_acknowledged_before_repair_completion(
    tmp_path: Path, repair_error: ErrorCode
) -> None:
    """Conflict and rejected-pipeline repairs retain selected feedback for the worker."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    selected = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "state": RunnerState.REPAIR,
            "phase": WorkflowPhase.WORKFLOW,
            "error_code": repair_error,
            "pending_feedback": PendingMergeRequestFeedback(
                target_cursor=MergeRequestFeedbackCursor(
                    last_processed_general_note_id=31,
                    feedback_source_sha="a" * 40,
                ),
                source_sha="a" * 40,
                source="general",
                note_id=31,
                outcome="Initial remediation was published.",
            ),
        }
    )
    ledger.persist(selected)
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    invoke = FakeInvoker()

    retried = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert retried is not None and retried.state is RunnerState.RETRY_WAIT
    assert retried.prior_state is RunnerState.REPAIR
    assert retried.pending_feedback == selected.pending_feedback
    assert retried.feedback_cursor is None
    assert len(invoke.prompts) == 1
    assert gitlab.feedback_outcomes == []


def test_global_identity_check_gitlab_error_retries_without_remote_mutations(
    tmp_path: Path,
) -> None:
    """A transient identity transport failure is retryable, unlike identity drift."""

    class TransientIdentityGitLab(_FeedbackGitLab):
        def verify_authenticated_user(self) -> None:
            raise GitLabError("temporary identity endpoint failure")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _serial_record(
        tmp_path, state=RunnerState.REPAIR, phase=WorkflowPhase.WORKFLOW
    )
    ledger.persist(record)
    gitlab = TransientIdentityGitLab()
    repository = FakeRepository(config)

    retried = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert retried is not None and retried.state is RunnerState.RETRY_WAIT
    assert retried.error_code is ErrorCode.GITLAB_RETRYABLE
    assert retried.prior_state is RunnerState.REPAIR
    assert gitlab.labels == []
    assert gitlab.comments == []
    assert gitlab.feedback_outcomes == []
    assert repository.pushes == 0


@pytest.mark.parametrize("acknowledgement_ready", (False, True))
def test_due_retry_normalizes_before_consecutive_global_identity_transport_failures(
    tmp_path: Path, acknowledgement_ready: bool
) -> None:
    """Due retries retain their original durable context across identity failures."""

    class FlakyIdentityGitLab(_FeedbackGitLab):
        def __init__(self) -> None:
            super().__init__()
            self.failures_remaining = 2

        def verify_authenticated_user(self) -> None:
            self.identity_checks += 1
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise GitLabError("temporary identity endpoint failure")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    cursor = MergeRequestFeedbackCursor(
        last_processed_general_note_id=31,
        feedback_source_sha="a" * 40,
    )
    pending = PendingMergeRequestFeedback(
        target_cursor=cursor,
        source_sha="a" * 40,
        source="general",
        note_id=31,
        outcome="Feedback outcome." if acknowledgement_ready else None,
        acknowledgement_ready=acknowledgement_ready,
    )
    record = (
        _awaiting_feedback_record(tmp_path)
        if acknowledgement_ready
        else _serial_record(
            tmp_path, state=RunnerState.RETRY_WAIT, phase=WorkflowPhase.WORKFLOW
        )
    ).model_copy(
        update={
            "state": RunnerState.RETRY_WAIT,
            "phase": WorkflowPhase.WORKFLOW,
            "prior_state": (
                RunnerState.REPAIR if acknowledgement_ready else RunnerState.PREPARING
            ),
            "prior_phase": WorkflowPhase.WORKFLOW,
            "retry_count": 1,
            "retry_at": NOW,
            "feedback_cursor": cursor,
            "pending_feedback": pending,
        }
    )
    ledger.persist(record)
    gitlab = FlakyIdentityGitLab()
    repository = FakeRepository(config)
    invoke = FakeInvoker()

    first_retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert first_retry is not None and first_retry.state is RunnerState.RETRY_WAIT
    assert first_retry.error_code is ErrorCode.GITLAB_RETRYABLE
    assert first_retry.retry_count == 2
    assert first_retry.prior_state is record.prior_state
    assert first_retry.prior_phase is record.prior_phase
    assert first_retry.pending_feedback == pending
    assert first_retry.feedback_cursor == cursor
    assert first_retry.worktree == record.worktree
    first_retry_at = first_retry.retry_at
    assert first_retry_at is not None
    assert first_retry_at == NOW + timedelta(minutes=20)
    assert invoke.prompts == []
    assert repository.prepared is False and repository.pushes == 0
    assert repository.verified_heads == []
    assert gitlab.labels == [] and gitlab.comments == []
    assert gitlab.feedback_outcomes == [] and gitlab.created_merge_requests == 0

    second_retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=first_retry_at,
    )  # type: ignore[arg-type]

    assert second_retry is not None and second_retry.state is RunnerState.RETRY_WAIT
    assert second_retry.error_code is ErrorCode.GITLAB_RETRYABLE
    assert second_retry.retry_count == 3
    assert second_retry.prior_state is record.prior_state
    assert second_retry.prior_phase is record.prior_phase
    assert second_retry.pending_feedback == pending
    assert second_retry.feedback_cursor == cursor
    assert second_retry.worktree == record.worktree
    second_retry_at = second_retry.retry_at
    assert second_retry_at is not None
    assert second_retry_at == first_retry_at + timedelta(minutes=20)
    assert invoke.prompts == []
    assert repository.prepared is False and repository.pushes == 0
    assert repository.verified_heads == []
    assert gitlab.labels == [] and gitlab.comments == []
    assert gitlab.feedback_outcomes == [] and gitlab.created_merge_requests == 0

    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=second_retry_at,
    )  # type: ignore[arg-type]

    if acknowledgement_ready:
        assert recovered is not None and recovered.state is RunnerState.AWAITING_HUMAN
        assert recovered.pending_feedback is None
        assert recovered.feedback_cursor == cursor
        assert invoke.prompts == []
        assert gitlab.feedback_outcomes == [(88, 12, 31, "a" * 40)]
    else:
        assert recovered is not None and recovered.state is RunnerState.PREPARING
        assert recovered.phase is WorkflowPhase.WORKFLOW
        assert recovered.retry_at is None
        assert recovered.prior_state is record.prior_state
        assert recovered.prior_phase is record.prior_phase
        assert repository.prepared is False
        assert invoke.prompts == []
    assert gitlab.identity_checks == (5 if acknowledgement_ready else 3)


def test_exhausted_global_identity_transport_retry_blocks_locally_without_mutations(
    tmp_path: Path,
) -> None:
    """An unverifiable identity never authorizes exhaustion side effects."""

    class FailingIdentityGitLab(_FeedbackGitLab):
        def verify_authenticated_user(self) -> None:
            self.identity_checks += 1
            raise GitLabError("identity endpoint remains unavailable")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    cursor = MergeRequestFeedbackCursor(
        last_processed_general_note_id=31,
        feedback_source_sha="a" * 40,
    )
    pending = PendingMergeRequestFeedback(
        target_cursor=cursor,
        source_sha="a" * 40,
        source="general",
        note_id=31,
        outcome="Feedback outcome.",
        acknowledgement_ready=True,
    )
    record = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "state": RunnerState.RETRY_WAIT,
            "phase": WorkflowPhase.WORKFLOW,
            "prior_state": RunnerState.REPAIR,
            "prior_phase": WorkflowPhase.WORKFLOW,
            "retry_count": 3,
            "retry_at": NOW,
            "feedback_cursor": cursor,
            "pending_feedback": pending,
        }
    )
    ledger.persist(record)
    gitlab = FailingIdentityGitLab()
    repository = FakeRepository(config)
    invoke = FakeInvoker()
    labels_before = list(gitlab.labels)
    comments_before = list(gitlab.comments)
    outcomes_before = list(gitlab.feedback_outcomes)
    created_before = gitlab.created_merge_requests

    exhausted = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert exhausted is not None and exhausted.state is RunnerState.BLOCKED
    assert exhausted.error_code is ErrorCode.RETRY_EXHAUSTED
    assert exhausted.retry_at is None
    assert exhausted.feedback_cursor == cursor
    assert exhausted.pending_feedback == pending
    assert gitlab.labels == labels_before
    assert gitlab.comments == comments_before
    assert gitlab.feedback_outcomes == outcomes_before
    assert gitlab.created_merge_requests == created_before
    assert invoke.prompts == []
    assert repository.prepared is False
    assert repository.pushes == 0
    assert repository.synced_heads == []
    assert repository.verified_heads == []
    assert exhausted.worktree == record.worktree


def test_exhausted_operation_local_identity_transport_retry_blocks_locally(
    tmp_path: Path,
) -> None:
    """A later identity check cannot authorize retry-exhaustion side effects."""

    class FailingSecondIdentityGitLab(_FeedbackGitLab):
        def verify_authenticated_user(self) -> None:
            self.identity_checks += 1
            if self.identity_checks >= 3:
                raise GitLabError("identity endpoint remains unavailable")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    cursor = MergeRequestFeedbackCursor(
        last_processed_general_note_id=31,
        feedback_source_sha="a" * 40,
    )
    pending = PendingMergeRequestFeedback(
        target_cursor=cursor,
        source_sha="a" * 40,
        source="general",
        note_id=31,
        outcome="Feedback outcome.",
        acknowledgement_ready=True,
    )
    record = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "state": RunnerState.RETRY_WAIT,
            "phase": WorkflowPhase.WORKFLOW,
            "prior_state": RunnerState.REPAIR,
            "prior_phase": WorkflowPhase.WORKFLOW,
            "retry_count": 3,
            "retry_at": NOW,
            "feedback_cursor": cursor,
            "pending_feedback": pending,
        }
    )
    ledger.persist(record)
    gitlab = FailingSecondIdentityGitLab()
    repository = FakeRepository(config)
    invoke = FakeInvoker()
    labels_before = list(gitlab.labels)
    comments_before = list(gitlab.comments)
    outcomes_before = list(gitlab.feedback_outcomes)
    created_before = gitlab.created_merge_requests

    exhausted = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert exhausted is not None and exhausted.state is RunnerState.BLOCKED
    assert exhausted.error_code is ErrorCode.RETRY_EXHAUSTED
    assert exhausted.retry_at is None
    assert exhausted.prior_state is RunnerState.REPAIR
    assert exhausted.prior_phase is WorkflowPhase.WORKFLOW
    assert exhausted.feedback_cursor == cursor
    assert exhausted.pending_feedback == pending
    assert gitlab.identity_checks == 3
    assert gitlab.labels == labels_before
    assert gitlab.comments == comments_before
    assert gitlab.feedback_outcomes == outcomes_before
    assert gitlab.created_merge_requests == created_before
    assert invoke.prompts == []
    assert repository.prepared is False
    assert repository.pushes == 0
    assert repository.synced_heads == []
    assert repository.verified_heads == ["a" * 40]
    assert exhausted.worktree == record.worktree


def test_post_publish_mr_identity_drift_blocks_before_feedback_acknowledgement(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    repository = FakeRepository(config)
    changed_feedback = SingleSessionInvoker()
    changed_feedback.changed_paths = ("src/example.py",)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=changed_feedback,
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    published = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]
    assert published is not None and published.source_sha is not None
    gitlab.pipeline = PipelineEvidence(
        id=1,
        status="success",
        sha=published.source_sha,
        web_url="https://gitlab.example/pipelines/1",
    )
    gitlab.existing_mr = _mr(sha=published.source_sha, source_branch="agent/foreign")

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert gitlab.feedback_outcomes == []
    assert blocked.feedback_cursor is None


@pytest.mark.parametrize(
    "mr",
    (
        _mr(state="closed"),
        _mr(source_project_id=999),
        _mr(source_branch="agent/foreign"),
        _mr(target_branch="release"),
    ),
)
def test_feedback_resume_blocks_exact_identity_conflicts_without_adopting_foreign_branch(
    tmp_path: Path, mr: object
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (_feedback(31),)
    gitlab.existing_mr = cast(MergeRequestEvidence, mr)
    repository = FakeRepository(config)

    selected = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    blocked = selected
    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.prepared is False
    assert gitlab.created_merge_requests == 0
    assert gitlab.labels == ["agent::blocked"]


def test_existing_active_record_precedes_awaiting_feedback(tmp_path: Path) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    waiting = _awaiting_feedback_record(tmp_path)
    active = _serial_record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(
        update={
            "issue_iid": 13,
            "source_branch": "agent/issue-13",
            "worktree": tmp_path / "state/worktrees/13",
        }
    )
    ledger.persist(waiting)
    ledger.persist(active)
    gitlab = _FeedbackGitLab()

    selected, _issue_value = select_record(
        ledger, gitlab, now=NOW, repository=FakeRepository(_serial_config(tmp_path))
    )

    assert selected is not None and selected.issue_iid == 13


def test_two_awaiting_records_are_isolated_to_the_mr_with_unseen_feedback(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    first = _awaiting_feedback_record(tmp_path)
    second = first.model_copy(
        update={
            "issue_iid": 13,
            "source_branch": "agent/issue-13",
            "worktree": tmp_path / "state/worktrees/13",
            "merge_request_iid": 89,
        }
    )
    ledger.persist(first)
    ledger.persist(second)
    gitlab = _FeedbackGitLab()
    gitlab.feedback_by_mr = {89: (_feedback(31),)}

    selected, _issue_value = select_record(
        ledger, gitlab, now=NOW, repository=FakeRepository(config)
    )

    assert selected is not None and selected.issue_iid == 13
    assert ledger.load(12) == first.model_copy(
        update={
            "schema_version": 2,
            "revision": 1,
            "activation_started_at": NOW,
            "latest_meaningful_update_at": NOW,
        }
    )


def test_processed_feedback_is_not_replayed_and_newer_feedback_after_new_sha_wakes(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "feedback_cursor": {
                "last_processed_general_note_id": 31,
                "feedback_source_sha": "a" * 40,
            }
        }
    )
    ledger.persist(record)
    gitlab = _FeedbackGitLab()
    repository = FakeRepository(config)
    gitlab.feedback = (_feedback(31),)

    assert (
        run_segment(
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=FakeInvoker(),
            timeout_seconds=30,
            now=NOW,
        )
        is None
    )  # type: ignore[arg-type]
    gitlab.existing_mr = _mr(sha="b" * 40)
    gitlab.feedback = (_feedback(31), _feedback(32, body="new request"))

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )  # type: ignore[arg-type]

    assert resumed is not None and resumed.pending_feedback is not None
    assert resumed.pending_feedback.source_sha == "b" * 40
    assert repository.head(record.worktree) == "a" * 40
    ready = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    assert ready is not None and ready.source_sha == "b" * 40
    assert repository.head(record.worktree) == "b" * 40


def test_transient_feedback_worktree_sync_retries_without_acknowledgement_or_cursor_advance(
    tmp_path: Path,
) -> None:
    """A transport failure during feedback sync remains retryable and unacknowledged."""

    class TransientSyncRepository(FakeRepository):
        def sync_feedback_worktree(
            self, worktree: Path, branch: str, old_sha: str, new_sha: str
        ) -> None:
            del worktree, branch, old_sha, new_sha
            raise RepositoryTransientError("temporary feedback sync failure")

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "feedback_cursor": {
                "last_processed_general_note_id": 31,
                "feedback_source_sha": "a" * 40,
            }
        }
    )
    ledger.persist(record)
    gitlab = _FeedbackGitLab()
    gitlab.existing_mr = _mr(sha="b" * 40)
    gitlab.feedback = (_feedback(31), _feedback(32, body="new request"))
    repository = TransientSyncRepository(config)

    selected = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    assert selected is not None and selected.pending_feedback is not None

    retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )

    assert retry is not None and retry.state is RunnerState.RETRY_WAIT
    assert retry.error_code is ErrorCode.GIT_TRANSPORT_RETRYABLE
    assert retry.prior_state is RunnerState.REPAIR
    assert retry.feedback_cursor == selected.feedback_cursor
    assert retry.pending_feedback == selected.pending_feedback
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0
    assert repository.pushes == 0


def test_consecutive_transient_feedback_sync_retries_preserve_repair_recovery(
    tmp_path: Path,
) -> None:
    """Feedback sync retries retain repair state until a later sync succeeds."""

    class FlakySyncRepository(FakeRepository):
        def __init__(self, config: AgentWorkConfig) -> None:
            super().__init__(config)
            self.sync_failures_remaining = 2
            self.sync_attempts = 0
            self.successful_syncs = 0

        def sync_feedback_worktree(
            self, worktree: Path, branch: str, old_sha: str, new_sha: str
        ) -> None:
            self.sync_attempts += 1
            if self.sync_failures_remaining:
                self.sync_failures_remaining -= 1
                raise RepositoryTransientError("temporary feedback sync failure")
            super().sync_feedback_worktree(worktree, branch, old_sha, new_sha)
            self.successful_syncs += 1

    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _awaiting_feedback_record(tmp_path).model_copy(
        update={
            "feedback_cursor": {
                "last_processed_general_note_id": 31,
                "feedback_source_sha": "a" * 40,
            }
        }
    )
    ledger.persist(record)
    gitlab = _FeedbackGitLab()
    gitlab.existing_mr = _mr(sha="b" * 40)
    gitlab.feedback = (_feedback(31), _feedback(32, body="new request"))
    repository = FlakySyncRepository(config)

    selected = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    assert selected is not None and selected.pending_feedback is not None

    first_retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )
    assert first_retry is not None and first_retry.state is RunnerState.RETRY_WAIT
    assert first_retry.error_code is ErrorCode.GIT_TRANSPORT_RETRYABLE
    assert first_retry.retry_count == 1
    assert first_retry.retry_at is not None
    assert repository.sync_attempts == 1

    waiting = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=first_retry.retry_at - timedelta(seconds=1),
    )
    assert waiting is None
    assert ledger.load(12) == first_retry
    assert repository.sync_attempts == 1

    second_retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=first_retry.retry_at,
    )
    assert second_retry is not None and second_retry.state is RunnerState.RETRY_WAIT
    assert second_retry.error_code is ErrorCode.GIT_TRANSPORT_RETRYABLE
    assert second_retry.retry_count == 2
    assert second_retry.retry_at is not None
    assert second_retry.prior_state is RunnerState.REPAIR
    assert second_retry.pending_feedback == selected.pending_feedback
    assert second_retry.feedback_cursor == selected.feedback_cursor
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0
    assert repository.pushes == 0
    assert repository.sync_attempts == 2

    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=second_retry.retry_at,
    )
    assert recovered is not None and recovered.state is RunnerState.REPAIR
    assert recovered.phase is WorkflowPhase.WORKFLOW
    assert recovered.source_sha == "b" * 40
    assert recovered.pending_feedback == selected.pending_feedback
    assert recovered.feedback_cursor == selected.feedback_cursor
    assert gitlab.feedback_outcomes == []
    assert gitlab.created_merge_requests == 0
    assert repository.pushes == 0
    assert repository.sync_attempts == 3
    assert repository.successful_syncs == 1


def test_identity_drift_blocks_preparation_before_remote_mutations(
    tmp_path: Path,
) -> None:
    """Configured identity is verified before every reachable remote write segment."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    record = _serial_record(
        tmp_path,
        state=RunnerState.PREPARING,
        phase=WorkflowPhase.WORKFLOW,
    )
    ledger.persist(record)
    gitlab = _FeedbackGitLab()
    gitlab.identity_error_on_check = 1
    repository = FakeRepository(config)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=30,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert gitlab.labels == []
    assert gitlab.comments == []
    assert gitlab.created_merge_requests == 0
    assert gitlab.created_children == []
    assert gitlab.decomposition_notes == []
    assert gitlab.closed_issues == []
    assert repository.prepared is False
    assert repository.pushes == 0


def test_feedback_resume_blocks_absent_closing_relation_and_worktree_mismatch(
    tmp_path: Path,
) -> None:
    config = _serial_config(tmp_path)
    for closes_issue, worktree_valid in ((False, True), (True, False)):
        ledger = JsonLedger(
            tmp_path / f"state-{closes_issue}-{worktree_valid}", clock=lambda: NOW
        )
        ledger.persist(_awaiting_feedback_record(tmp_path))
        gitlab = _FeedbackGitLab()
        gitlab.feedback = (_feedback(31),)
        gitlab.closing_results = [closes_issue]
        repository = FakeRepository(config)
        repository.worktree_valid = worktree_valid

        selected = run_segment(
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=FakeInvoker(),
            timeout_seconds=30,
            now=NOW,
        )  # type: ignore[arg-type]

        if not closes_issue:
            assert selected is not None and selected.state is RunnerState.BLOCKED
            continue
        blocked = run_segment(
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=FakeInvoker(),
            timeout_seconds=30,
            now=NOW,
        )
        assert blocked is not None and blocked.state is RunnerState.BLOCKED
        assert repository.prepared is False


def test_feedback_wake_persists_only_the_earliest_unseen_item_without_skipping_later_sources(
    tmp_path: Path,
) -> None:
    """One wake must durably select the first chronological human item only."""
    config = _serial_config(tmp_path)
    ledger = JsonLedger(config.state_root, clock=lambda: NOW)
    ledger.persist(_awaiting_feedback_record(tmp_path))
    gitlab = _FeedbackGitLab()
    gitlab.feedback = (
        _feedback(500, body="later general", created_at="2026-09-10T13:00:00Z"),
        _feedback(
            1,
            source="discussion",
            discussion_id="thread-1",
            body="first inline reply",
            created_at="2026-09-10T11:00:00Z",
        ),
    )

    selected, _issue_value = select_record(
        ledger, gitlab, now=NOW, repository=FakeRepository(config)
    )

    assert selected is not None and selected.pending_feedback is not None
    cursor = selected.pending_feedback.target_cursor
    assert cursor.last_processed_discussion_note_id == 1
    assert cursor.last_processed_general_note_id is None


def test_worker_prompt_marks_selected_feedback_and_excludes_later_unseen_work() -> None:
    """The worker sees bounded historical context, not a batch of future work."""
    prompt = build_worker_prompt(
        {
            "phase": "workflow",
            "pending_feedback": {
                "source": "discussion",
                "note_id": 1,
                "discussion_id": "thread-1",
            },
        },
        project_id=310967,
        issue_iid=12,
        effective_worker_budget_seconds=1800,
        merge_request_feedback=(
            {
                "id": 1,
                "source": "discussion",
                "discussion_id": "thread-1",
                "author": "reviewer",
                "created_at": datetime(2026, 9, 10, 11, tzinfo=UTC),
                "body": "first inline reply",
            },
            {
                "id": 500,
                "source": "general",
                "discussion_id": None,
                "author": "reviewer",
                "created_at": datetime(2026, 9, 10, 13, tzinfo=UTC),
                "body": "later general request",
            },
        ),
    )

    marker = "Untrusted merge-request feedback JSON:\n"
    rendered = prompt.split(marker, maxsplit=1)[1].split("\nEnd untrusted", maxsplit=1)[
        0
    ]
    feedback = json.loads(rendered)

    assert feedback["selected_item"] == {
        "source": "discussion",
        "note_id": 1,
        "discussion_id": "thread-1",
    }
    assert [note["id"] for note in feedback["notes"]] == [1]
    assert feedback["context_truncated"] is False
    assert feedback["omitted_future_note_count"] == 1
