"""Behavior tests for bounded segments and CLI dispatch."""

from __future__ import annotations

import json
import logging
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from agent_work.domain import (
    LIFECYCLE_LABELS,
    CommandRequest,
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.gitlab import GitLabError
from agent_work.ledger import JsonLedger
from agent_work.models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabIssueNote,
    GitLabNoteAuthor,
    GitLabProject,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
    TransitionIntentRecord,
)
from agent_work.repository import RepositoryError
from agent_work.runtime import RuntimeDependencies, dispatch
from agent_work.serial import SegmentError, run_segment
from agent_work.worker import WorkerInvocation

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
    )


def _issue(
    *, iid: int = 12, description: str = "ordinary", state: str = "opened"
) -> GitLabIssue:
    return GitLabIssue(
        iid=iid,
        title="Work",
        description=description,
        labels=(),
        state=state,
    )


def _project() -> GitLabProject:
    return GitLabProject(
        id=310967,
        host="gitlab.example.com",
        default_branch="main",
        web_url="https://gitlab.example.com/group/project",
    )


def _mr(
    *,
    state: Literal["opened", "closed", "merged", "locked"] = "opened",
    source_project_id: int = 310967,
    target_project_id: int = 310967,
    source_branch: str = "agent/issue-12",
    target_branch: str = "main",
    sha: str = "a" * 40,
    has_conflicts: bool = False,
) -> MergeRequestEvidence:
    return MergeRequestEvidence(
        iid=88,
        web_url="https://gitlab.example.com/group/project/-/merge_requests/88",
        state=state,
        source_project_id=source_project_id,
        target_project_id=target_project_id,
        source_branch=source_branch,
        target_branch=target_branch,
        sha=sha,
        has_conflicts=has_conflicts,
        description="Closes #12",
    )


class FakeGitLab:
    def __init__(self, issue: GitLabIssue | None = None) -> None:
        self.issue = issue or _issue()
        self.labels: list[str | None] = []
        self.comments: list[str] = []
        self.pipeline: PipelineEvidence | None = None
        self.existing_mr: MergeRequestEvidence | None = None
        self.create_error: RuntimeError | None = None
        self.created_merge_requests = 0
        self.closing_reference_updates = 0
        self.fetched_merge_requests: list[int] = []
        self.fetch_merge_request_error: GitLabError | None = None
        self.pipeline_error: GitLabError | None = None
        self.closing_error: GitLabError | None = None
        self.closing_results: list[bool] = []
        self.label_failures = 0
        self.notes: tuple[GitLabIssueNote, ...] = ()
        self.note_fetches = 0
        self.note_error: GitLabError | None = None

    def verify_project(self) -> GitLabProject:
        return _project()

    def list_eligible(self) -> tuple[GitLabIssue, ...]:
        return (
            (self.issue,)
            if self.issue.state == "opened"
            and not self.issue.confidential
            and not set(self.issue.labels).intersection(LIFECYCLE_LABELS)
            else ()
        )

    def list_projected(self) -> tuple[GitLabIssue, ...]:
        return (
            (self.issue,)
            if set(self.issue.labels).intersection(LIFECYCLE_LABELS)
            else ()
        )

    def fetch_issue(self, iid: int) -> GitLabIssue:
        del iid
        return self.issue

    def fetch_issue_notes(self, iid: int) -> tuple[GitLabIssueNote, ...]:
        del iid
        self.note_fetches += 1
        if self.note_error is not None:
            raise self.note_error
        return self.notes

    def transition_labels(self, iid: int, label: str | None) -> GitLabIssue:
        del iid
        if self.label_failures:
            self.label_failures -= 1
            raise GitLabError("temporary label failure")
        self.labels.append(label)
        return self.issue

    def comment_outcome(
        self, iid: int, *, error_code: str, summary: str, next_action: str
    ) -> None:
        del iid, error_code, next_action
        self.comments.append(summary)

    def fetch_merge_request(self, iid: int) -> MergeRequestEvidence:
        self.fetched_merge_requests.append(iid)
        if self.fetch_merge_request_error is not None:
            raise self.fetch_merge_request_error
        return self.existing_mr or _mr()

    def find_merge_request_by_branch(self, branch: str) -> MergeRequestEvidence | None:
        del branch
        return self.existing_mr

    def create_merge_request(
        self,
        *,
        branch: str,
        target: str,
        issue_iid: int,
        draft: MergeRequestDraft,
    ) -> MergeRequestEvidence:
        del branch, target, issue_iid, draft
        self.created_merge_requests += 1
        if self.create_error is not None:
            raise self.create_error
        return _mr()

    def ensure_closing_issue_reference(
        self, mr: MergeRequestEvidence, issue_iid: int
    ) -> MergeRequestEvidence:
        del issue_iid
        self.closing_reference_updates += 1
        return mr

    def mr_closes_issue(self, mr_iid: int, issue_iid: int) -> bool:
        del mr_iid, issue_iid
        if self.closing_error is not None:
            raise self.closing_error
        return self.closing_results.pop(0) if self.closing_results else True

    def latest_pipeline_for_sha(self, mr_iid: int, sha: str) -> PipelineEvidence | None:
        del mr_iid, sha
        if self.pipeline_error is not None:
            raise self.pipeline_error
        return self.pipeline


class ChangingSnapshotJsonLedger(JsonLedger):
    def __init__(self, root: Path, initial_records: tuple[LedgerRecord, ...]) -> None:
        super().__init__(root)
        self.initial_records = initial_records
        self.first_read = True

    def list_records(self) -> tuple[LedgerRecord, ...]:
        if self.first_read:
            self.first_read = False
            return self.initial_records
        return ()


class FakeRepository:
    def __init__(self, config: AgentWorkConfig) -> None:
        self.config = config
        self.prepared = False
        self.pushes = 0
        self.refreshed = 0
        self.paths: tuple[str, ...] = ("src/example.py",)
        self.worktree_present = True
        self.worktree_valid = True
        self.inventory_result: tuple[
            tuple[str, ...], tuple[str, ...], tuple[str, ...]
        ] = (
            (),
            (),
            (),
        )

    def refresh_base(self) -> None:
        """Model a successful clean-checkout refresh."""
        self.refreshed += 1

    def create_issue_worktree(
        self,
        issue_iid: int,
        branch: str,
        default_branch: str,
        *,
        allow_existing_branch: bool = True,
    ) -> Path:
        del branch, default_branch, allow_existing_branch
        self.prepared = True
        return self.config.state_root / "worktrees" / str(issue_iid)

    def worktree_for_existing_branch(
        self, issue_iid: int, branch: str, expected_sha: str | None = None
    ) -> Path:
        del branch, expected_sha
        self.prepared = True
        return self.config.state_root / "worktrees" / str(issue_iid)

    def head(self, worktree: Path) -> str:
        del worktree
        return "a" * 40

    def changed_paths(self, worktree: Path) -> tuple[str, ...]:
        del worktree
        return self.paths

    def worktree_exists(self, worktree: Path) -> bool:
        del worktree
        return self.worktree_present

    def verify_worktree(self, worktree: Path, branch: str, expected_head: str) -> None:
        del worktree, branch, expected_head
        if not self.worktree_valid:
            raise RepositoryError("worktree branch or baseline changed")

    def commit_and_push(
        self,
        worktree: Path,
        branch: str,
        issue_iid: int,
        expected_head: str | None = None,
        allow_replay: bool = False,
    ) -> str:
        del worktree, branch, issue_iid, expected_head, allow_replay
        self.pushes += 1
        return "a" * 40

    def inventory(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        return self.inventory_result


def _dependencies(
    config: AgentWorkConfig,
    ledger: JsonLedger,
    gitlab: FakeGitLab,
    repository: FakeRepository,
) -> RuntimeDependencies:
    return RuntimeDependencies(
        config=config,
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        lock=nullcontext,
        clock=lambda: NOW,
        logger=logging.getLogger(f"test-agent-work-{id(ledger)}"),
        read_plan=lambda path: path.read_bytes(),
    )


class FakeInvoker:
    def __init__(self, output: str = "", returncode: int | None = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.prompts: list[str] = []

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation:
        del checkout, timeout
        self.prompts.append(prompt)
        return WorkerInvocation(self.returncode, self.output, "safe diagnostic")


def _record(
    tmp_path: Path, *, state: RunnerState, phase: WorkflowPhase
) -> LedgerRecord:
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


@pytest.mark.parametrize("labels", [(), ("customer::priority",)])
def test_new_eligible_issue_is_claimed_and_prepared_without_running_worker(
    tmp_path: Path, labels: tuple[str, ...]
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": labels}))
    repository = FakeRepository(config)

    record = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert record is not None and record.state is RunnerState.EXECUTING
    assert repository.prepared
    assert gitlab.labels == ["agent::running"]
    persisted = ledger.load(12)
    assert persisted is not None and persisted.intent is None


def test_execution_parses_identity_bound_result_and_advances_one_phase(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    record = ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    payload = {
        "protocol_version": 1,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": record.revision,
        "expected_state": "executing",
        "expected_phase": "coding-green",
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "phase_status": "complete",
        "completed_phase": "coding-green",
        "receipts": ["$tdd-green: green"],
        "change_summary": "Green complete.",
        "next_action": "Review timing.",
    }
    gitlab = FakeGitLab()

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>"
        ),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.phase is WorkflowPhase.CODING_NONFUNCTIONAL
    assert len(outcome.receipts) == 1


def test_execution_fetches_live_issue_notes_for_the_worker_prompt(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab()
    gitlab.notes = (
        GitLabIssueNote(
            id=1,
            body="Use the reviewer-approved boundary",
            author=GitLabNoteAuthor(username="reviewer"),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            system=False,
        ),
    )
    invoker = FakeInvoker(returncode=1)

    run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert gitlab.note_fetches == 1
    assert "Use the reviewer-approved boundary" in invoker.prompts[0]


def test_issue_note_failure_retries_without_invoking_worker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab()
    gitlab.note_error = GitLabError("safe failure")
    invoker = FakeInvoker()

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE
    assert invoker.prompts == []


def test_worker_failure_is_a_successfully_recorded_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    outcome = run_segment(
        ledger=ledger,
        gitlab=FakeGitLab(),  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(returncode=1),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.retry_at is not None
    assert outcome.error_code is ErrorCode.WORKER_EXIT


def test_worker_timeout_and_supplied_retry_reason_are_recorded(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for invocation, expected in (
        (FakeInvoker(returncode=None), ErrorCode.WORKER_TIMEOUT),
        (
            FakeInvoker(
                '<agent-work-result>{"protocol_version":1,"project_id":310967,'
                '"issue_iid":12,"ledger_revision":1,"expected_state":"executing",'
                '"expected_phase":"coding-green","workflow_lane":"coding",'
                '"result_kind":"retryable","phase_status":"rejected",'
                '"completed_phase":"coding-green","receipts":["$tdd-green"],'
                '"change_summary":"temporary","error_code":"gitlab_retryable",'
                '"next_action":"retry"}</agent-work-result>'
            ),
            ErrorCode.GITLAB_RETRYABLE,
        ),
    ):
        ledger = JsonLedger(config.state_root / str(expected))
        ledger.persist(
            _record(
                tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN
            ).model_copy(update={"worktree": config.state_root / str(expected)})
        )
        outcome = run_segment(
            ledger=ledger,
            gitlab=FakeGitLab(),  # type: ignore[arg-type]
            repository=FakeRepository(config),  # type: ignore[arg-type]
            invoke=invocation,  # type: ignore[arg-type]
            timeout_seconds=10,
            now=NOW,
        )
        assert outcome is not None and outcome.error_code is expected


def test_future_retry_wait_reserves_the_serial_slot(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.RETRY_WAIT, phase=WorkflowPhase.CODING_GREEN
        ).model_copy(update={"retry_at": NOW + timedelta(minutes=10)})
    )
    gitlab = FakeGitLab(_issue(iid=13))
    repository = FakeRepository(config)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is None
    assert not repository.prepared
    assert ledger.load(13) is None


def test_malformed_remediation_is_recorded_as_an_actionable_block(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue(description="<!-- agent-mr-remediation -->"))

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.REMEDIATION_MARKER_INVALID
    assert gitlab.labels[-1] == "agent::blocked"
    assert gitlab.comments and outcome.next_action


def test_transient_remediation_lookup_schedules_retry(tmp_path: Path) -> None:
    marker = """<!-- agent-mr-remediation
project_id: 310967
merge_request_iid: 88
source_branch: agent/issue-12
source_sha: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
finding: review
-->"""
    config = _config(tmp_path)
    gitlab = FakeGitLab(_issue(description=marker))
    gitlab.fetch_merge_request_error = GitLabError("temporary")

    outcome = run_segment(
        ledger=JsonLedger(config.state_root),
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE


def test_publishing_and_observation_are_separate_idempotent_segments(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    publishing = _record(
        tmp_path, state=RunnerState.PUBLISHING, phase=WorkflowPhase.READY_TO_PUBLISH
    ).model_copy(
        update={
            "merge_request_draft": MergeRequestDraft(
                title="Change", description="Details"
            )
        }
    )
    ledger.persist(publishing)
    gitlab = FakeGitLab()
    observed = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    assert observed is not None and observed.state is RunnerState.OBSERVING
    assert observed.merge_request_iid == 88

    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    post = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    assert post is not None and post.phase is WorkflowPhase.POST_PUBLICATION


@pytest.mark.parametrize(
    ("lane", "finalizer"),
    [
        (WorkflowLane.CODING, WorkflowPhase.CODING_FINALIZE),
        (WorkflowLane.CONTENT, WorkflowPhase.CONTENT_FINALIZE),
    ],
)
def test_preintent_publication_without_draft_recovers_to_lane_finalizer(
    tmp_path: Path, lane: WorkflowLane, finalizer: WorkflowPhase
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    original = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "workflow_lane": lane,
                "summary": "Final validation approved.",
            }
        )
    )
    gitlab = FakeGitLab()
    repository = FakeRepository(config)

    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert recovered is not None
    assert recovered.state is RunnerState.EXECUTING
    assert recovered.phase is finalizer
    assert recovered.source_sha == original.source_sha
    assert recovered.receipts == original.receipts
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0


def test_postpublication_handoff_revalidates_mr_after_worker_returns(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    record = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.EXECUTING,
            phase=WorkflowPhase.POST_PUBLICATION,
        ).model_copy(update={"merge_request_iid": 88})
    )
    payload = {
        "protocol_version": 1,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": record.revision,
        "expected_state": "executing",
        "expected_phase": "post-publication",
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "phase_status": "complete",
        "completed_phase": "post-publication",
        "receipts": ["$pipeline-performance-judge", "$findings-triage"],
        "change_summary": "Post-publication review complete.",
        "next_action": "Review the merge request.",
    }
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.existing_mr = _mr(sha="b" * 40)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>"
        ),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert gitlab.labels[-1] == "agent::blocked"


@pytest.mark.parametrize(
    "merge_request",
    [_mr(state="closed"), _mr(target_branch="other"), _mr(sha="b" * 40)],
)
def test_observation_revalidates_exact_merge_request_evidence(
    tmp_path: Path, merge_request: MergeRequestEvidence
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.existing_mr = merge_request

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH


def test_observation_routes_conflicts_to_durable_repair(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.existing_mr = _mr(has_conflicts=True)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.REPAIR
    assert outcome.phase is WorkflowPhase.CODING_GREEN
    assert outcome.work_kind is WorkKind.MERGE_CONFLICT_REPAIR
    assert outcome.error_code is ErrorCode.MERGE_CONFLICT


def test_observation_gitlab_failure_schedules_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.pipeline_error = GitLabError("temporary")

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE


@pytest.mark.parametrize("status", ["created", "scheduled", "pending", "running"])
def test_nonterminal_pipeline_status_waits_without_repair(
    tmp_path: Path, status: str
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    record = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.pipeline = PipelineEvidence(
        id=4, status=status, sha="a" * 40, web_url="https://pipeline/4"
    )

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome == record


def test_failed_pipeline_routes_repair_to_coding_green(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.pipeline = PipelineEvidence(
        id=4, status="failed", sha="a" * 40, web_url="https://pipeline/4"
    )

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.REPAIR
    assert outcome.phase is WorkflowPhase.CODING_GREEN
    assert outcome.error_code is ErrorCode.PIPELINE_REJECTED


def test_publication_transport_failure_schedules_a_recoverable_retry(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.create_error = GitLabError("temporary GitLab failure")

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE
    assert outcome.intent is not None


def test_publication_retry_exhaustion_projects_an_actionable_block(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "retry_count": 3,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.create_error = GitLabError("temporary")

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert gitlab.labels[-1] == "agent::blocked"
    assert gitlab.comments


def test_publication_replays_intent_by_reusing_the_exact_existing_mr(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    pending = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "intent": TransitionIntentRecord(
                    operation_id="12:publish:1",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                    },
                ),
            }
        )
    )
    ledger.persist(pending.model_copy(update={"summary": "Retry publication."}))
    gitlab = FakeGitLab()
    gitlab.existing_mr = _mr()

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert outcome.intent is None
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    "intent",
    [
        TransitionIntentRecord(
            operation_id="12:publish:1",
            kind="prepare_worktree",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish:1",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-13",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish:1",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "b" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="13:publish:1",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="012:publish:0",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:create:0",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish:0:extra",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish:01",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
            },
        ),
        TransitionIntentRecord(
            operation_id="12:publish:0",
            kind="publish_merge_request",
            expected_evidence={"branch": "agent/issue-12"},
        ),
        TransitionIntentRecord(
            operation_id="12:publish:0",
            kind="publish_merge_request",
            expected_evidence={"baseline_sha": "a" * 40},
        ),
        TransitionIntentRecord(
            operation_id="12:publish:0",
            kind="publish_merge_request",
            expected_evidence={
                "branch": "agent/issue-12",
                "baseline_sha": "a" * 40,
                "merge_request_iid": 88,
            },
        ),
    ],
    ids=(
        "kind",
        "branch",
        "baseline",
        "issue-operation",
        "noncanonical-issue-iid",
        "wrong-action",
        "missing-revision",
        "extra-operation-component",
        "noncanonical-revision",
        "missing-baseline-evidence",
        "missing-branch-evidence",
        "additional-evidence",
    ),
)
def test_publication_rejects_inconsistent_replay_intent_before_mutation(
    tmp_path: Path, intent: TransitionIntentRecord
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "intent": intent,
            }
        )
    )
    gitlab = FakeGitLab()
    repository = FakeRepository(config)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert outcome.intent == intent
    assert ledger.load(12) == outcome
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0
    assert gitlab.closing_reference_updates == 0


@pytest.mark.parametrize("operation_revision", [1, 2])
def test_publication_rejects_intent_not_older_than_durable_record(
    tmp_path: Path, operation_revision: int
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    intent = TransitionIntentRecord(
        operation_id=f"12:publish:{operation_revision}",
        kind="publish_merge_request",
        expected_evidence={
            "branch": "agent/issue-12",
            "baseline_sha": "a" * 40,
        },
    )
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "intent": intent,
            }
        )
    )
    gitlab = FakeGitLab()
    repository = FakeRepository(config)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert outcome.intent == intent
    assert ledger.load(12) == outcome
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0
    assert gitlab.closing_reference_updates == 0


def test_delayed_publication_closing_evidence_schedules_retry(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.closing_results = [False]

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.prior_state is RunnerState.PUBLISHING
    assert outcome.prior_phase is WorkflowPhase.READY_TO_PUBLISH
    assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE
    assert outcome.intent is not None
    assert outcome.merge_request_iid == 88
    assert outcome.merge_request_url == (
        "https://gitlab.example.com/group/project/-/merge_requests/88"
    )
    assert "agent::blocked" not in gitlab.labels
    assert gitlab.comments == []


def test_delayed_publication_closing_evidence_recovers_without_duplicate_mr(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.closing_results = [False, True]
    repository = FakeRepository(config)

    waiting = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    assert waiting is not None and waiting.retry_at is not None
    gitlab.existing_mr = _mr()

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=waiting.retry_at,
    )
    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=waiting.retry_at,
    )

    assert resumed is not None and resumed.state is RunnerState.PUBLISHING
    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert outcome.phase is WorkflowPhase.PIPELINE_WAIT
    assert gitlab.created_merge_requests == 1
    assert gitlab.fetched_merge_requests == [88]


def test_missing_publication_closing_evidence_exhaustion_blocks(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "retry_count": 3,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.closing_results = [False]

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.RETRY_EXHAUSTED
    assert gitlab.labels[-1] == "agent::blocked"
    assert gitlab.comments


def test_remediation_publication_reuses_only_its_recorded_merge_request(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "work_kind": WorkKind.EXISTING_MR_REMEDIATION,
                "merge_request_iid": 88,
                "merge_request_draft": MergeRequestDraft(
                    title="Repair", description="Details"
                ),
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.existing_mr = _mr()

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert gitlab.fetched_merge_requests == [88]
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    "merge_request",
    [
        _mr(state="closed"),
        _mr(source_project_id=42),
        _mr(target_project_id=42),
        _mr(source_branch="other"),
        _mr(target_branch="other"),
        _mr(sha="b" * 40),
    ],
)
def test_publication_mismatch_is_an_actionable_block(
    tmp_path: Path, merge_request: MergeRequestEvidence
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.existing_mr = merge_request

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert gitlab.comments


def test_actual_protected_policy_changes_block_before_publication(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    repository = FakeRepository(config)
    repository.paths = ("AGENTS.md",)
    gitlab = FakeGitLab()

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.PROTECTED_POLICY_MANUAL
    assert repository.pushes == 0


def test_closed_issue_is_reconciled_and_lifecycle_label_removed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab(_issue(state="closed"))
    result = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    assert result is not None and result.state is RunnerState.COMPLETE
    assert gitlab.labels[-1] is None


def test_closed_terminal_record_is_reconciled_before_new_selection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab(
        _issue(state="closed").model_copy(update={"labels": ("agent::blocked",)})
    )

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.COMPLETE
    assert gitlab.labels == [None]


def test_orphan_running_projection_becomes_a_documented_block(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.MISSING_LEDGER
    assert gitlab.labels == ["agent::blocked"]


def test_missing_active_worktree_becomes_a_documented_block(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    repository = FakeRepository(config)
    repository.worktree_present = False

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.WORKTREE_MISSING
    assert gitlab.comments


def test_resumed_worker_requires_the_recorded_branch_and_baseline(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    repository = FakeRepository(config)
    repository.worktree_valid = False

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.WORKTREE_OWNERSHIP_AMBIGUOUS


def test_multiple_lifecycle_projections_become_blocked(tmp_path: Path) -> None:
    config = _config(tmp_path)
    gitlab = FakeGitLab(
        _issue().model_copy(update={"labels": ("agent::blocked", "agent::running")})
    )

    outcome = run_segment(
        ledger=JsonLedger(config.state_root),
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert outcome.error_code is ErrorCode.MISSING_LEDGER


def test_terminal_ledger_issue_is_not_selected_as_fresh_work(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    completed = ledger.persist(
        _record(tmp_path, state=RunnerState.COMPLETE, phase=WorkflowPhase.DISCOVERY)
    )
    gitlab = FakeGitLab(_issue())

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is None
    assert ledger.load(12) == completed
    preview = dispatch(
        CommandRequest(command="loop.run"),
        apply=False,
        dependencies=_dependencies(config, ledger, gitlab, FakeRepository(config)),
    )
    assert preview.outcome == "idle"


def test_run_preview_reports_eligible_unprojected_work(tmp_path: Path) -> None:
    config = _config(tmp_path)

    report = dispatch(
        CommandRequest(command="loop.run"),
        apply=False,
        dependencies=_dependencies(
            config,
            JsonLedger(config.state_root),
            FakeGitLab(_issue()),
            FakeRepository(config),
        ),
    )

    assert report.outcome == "eligible"


def test_run_preview_uses_initial_ledger_snapshot_for_eligibility(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = ChangingSnapshotJsonLedger(
        config.state_root,
        (
            _record(
                tmp_path,
                state=RunnerState.COMPLETE,
                phase=WorkflowPhase.POST_PUBLICATION,
            ),
        ),
    )

    report = dispatch(
        CommandRequest(command="loop.run"),
        apply=False,
        dependencies=_dependencies(
            config,
            ledger,
            FakeGitLab(_issue()),
            FakeRepository(config),
        ),
    )

    assert report.outcome == "idle"


def test_complete_record_with_stale_label_only_removes_the_projection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    complete = ledger.persist(
        _record(
            tmp_path, state=RunnerState.COMPLETE, phase=WorkflowPhase.POST_PUBLICATION
        )
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::failed",)}))

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome == complete
    assert gitlab.labels == [None]


def test_multiple_active_records_fail_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.OBSERVING, phase=WorkflowPhase.PIPELINE_WAIT
        ).model_copy(update={"issue_iid": 13, "source_branch": "agent/issue-13"})
    )
    with pytest.raises(SegmentError, match="multiple resumable"):
        run_segment(
            ledger=ledger,
            gitlab=FakeGitLab(),  # type: ignore[arg-type]
            repository=FakeRepository(config),  # type: ignore[arg-type]
            invoke=FakeInvoker(),  # type: ignore[arg-type]
            timeout_seconds=10,
            now=NOW,
        )


def test_runtime_status_and_run_preview_never_refresh_checkout(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.CODING_GREEN)
    )
    fake_gitlab = FakeGitLab(_issue().model_copy(update={"labels": ()}))
    repository = FakeRepository(config)
    dependencies = _dependencies(config, ledger, fake_gitlab, repository)

    status = dispatch(
        CommandRequest(command="loop.status"), apply=False, dependencies=dependencies
    )
    preview = dispatch(
        CommandRequest(command="loop.run"), apply=False, dependencies=dependencies
    )
    assert status.outcome == "status" and status.items[0].state is RunnerState.BLOCKED
    assert preview.outcome == "idle"


def test_observation_segment_does_not_refresh_the_unrelated_base_checkout(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
        ).model_copy(update={"merge_request_iid": 88})
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    repository = FakeRepository(config)

    dispatch(
        CommandRequest(command="loop.run"),
        apply=True,
        dependencies=_dependencies(config, ledger, gitlab, repository),
    )

    assert repository.refreshed == 0


def test_runtime_migration_preview_and_blocked_retry_are_explicit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    failed_issue = _issue().model_copy(update={"labels": ("agent::failed",)})
    fake_gitlab = FakeGitLab(failed_issue)
    fake_repository = FakeRepository(config)
    fake_repository.inventory_result = (
        ("agent/issue-12",),
        (str(config.checkout),),
        ("agent-work recovery issue-12",),
    )
    dependencies = _dependencies(config, ledger, fake_gitlab, fake_repository)

    migration = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(migration.model_dump_json())
    applied_migration = dispatch(
        CommandRequest(command="migration.apply", plan_file=plan_file),
        apply=True,
        dependencies=dependencies,
    )
    retry_preview = dispatch(
        CommandRequest(command="loop.retry", issue_iid=12),
        apply=False,
        dependencies=dependencies,
    )
    retried = dispatch(
        CommandRequest(command="loop.retry", issue_iid=12),
        apply=True,
        dependencies=dependencies,
    )

    assert migration.actions[0].target_state is RunnerState.BLOCKED
    assert applied_migration.outcome == "migration_applied"
    assert retry_preview.outcome == "retry_preview"
    assert retried.items[0].state is RunnerState.PREPARING
    assert fake_gitlab.labels == ["agent::blocked", "agent::running"]


@pytest.mark.parametrize("mutation", ["wrong-project", "tampered-action"])
def test_migration_apply_rejects_tampered_reviewed_evidence_before_mutation(
    tmp_path: Path, mutation: str
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::failed",)}))
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    ).model_dump(mode="json")
    if mutation == "wrong-project":
        preview["project"]["project_id"] = 999  # type: ignore[index]
    else:
        preview["actions"][0]["next_action"] = "Unreviewed action"  # type: ignore[index]
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(json.dumps(preview))

    with pytest.raises(RuntimeError):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )

    assert ledger.list_records() == ()
    assert gitlab.labels == []


def test_migration_apply_rejects_stale_inventory_before_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::failed",)}))
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    repository.inventory_result = ((), (), ())

    with pytest.raises(RuntimeError, match="evidence changed"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )

    assert ledger.list_records() == ()
    assert gitlab.labels == []


def test_migration_apply_never_overwrites_a_newer_ledger_record(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::failed",)}))
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    current = ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )

    with pytest.raises(RuntimeError, match="ledger"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )

    assert ledger.load(12) == current
    assert gitlab.labels == []


def test_migration_preflights_every_ledger_before_any_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    issue_12 = _issue().model_copy(update={"labels": ("agent::failed",)})
    issue_13 = _issue(iid=13).model_copy(update={"labels": ("agent::failed",)})

    class MultiGitLab(FakeGitLab):
        def list_projected(self) -> tuple[GitLabIssue, ...]:
            return (issue_12, issue_13)

        def fetch_issue(self, iid: int) -> GitLabIssue:
            return issue_12 if iid == 12 else issue_13

    gitlab = MultiGitLab(issue_12)
    repository = FakeRepository(config)
    repository.inventory_result = (
        ("agent/issue-12", "agent/issue-13"),
        (),
        (),
    )
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    current = ledger.persist(
        _record(
            tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN
        ).model_copy(update={"issue_iid": 13, "source_branch": "agent/issue-13"})
    )

    with pytest.raises(RuntimeError, match="ledger"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )

    assert ledger.load(12) is None
    assert ledger.load(13) == current
    assert gitlab.labels == []


def test_migration_replays_its_intent_after_partial_gitlab_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    issue = _issue().model_copy(update={"labels": ("agent::failed",)})
    gitlab = FakeGitLab(issue)
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    gitlab.label_failures = 1

    with pytest.raises(GitLabError, match="label"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )
    interrupted = ledger.load(12)
    assert interrupted is not None and interrupted.intent is not None

    report = dispatch(
        CommandRequest(command="migration.apply", plan_file=plan_file),
        apply=True,
        dependencies=dependencies,
    )

    assert report.outcome == "migration_applied"
    completed = ledger.load(12)
    assert completed is not None and completed.intent is None
    assert gitlab.labels[-1] == "agent::blocked"


def test_migration_replays_only_exact_pending_intent_after_projection_removed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    seed_ledger = JsonLedger(config.state_root)
    issue = _issue().model_copy(update={"labels": ("agent::failed",)})
    seed_gitlab = FakeGitLab(issue)
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    seed_dependencies = _dependencies(config, seed_ledger, seed_gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=seed_dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    seed_gitlab.label_failures = 1

    with pytest.raises(GitLabError, match="label"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=seed_dependencies,
        )

    pending = seed_ledger.load(12)
    assert pending is not None and pending.intent is not None
    variants = {
        "exact": pending.intent,
        "missing": None,
        "operation": pending.intent.model_copy(update={"operation_id": "migration:13"}),
        "kind": pending.intent.model_copy(update={"kind": "other"}),
        "evidence": pending.intent.model_copy(
            update={"expected_evidence": {"label": "agent::failed"}}
        ),
    }

    for name, intent in variants.items():
        case_root = tmp_path / name
        case_config = _config(case_root)
        case_ledger = JsonLedger(case_config.state_root)
        case_ledger.persist(
            pending.model_copy(
                update={
                    "revision": 0,
                    "intent": intent,
                    "worktree": case_config.state_root / "worktrees" / "12",
                }
            )
        )
        case_issue = issue.model_copy(update={"labels": ()})
        case_gitlab = FakeGitLab(case_issue)
        case_repository = FakeRepository(case_config)
        case_repository.inventory_result = (("agent/issue-12",), (), ())
        dependencies = _dependencies(
            case_config, case_ledger, case_gitlab, case_repository
        )

        if name == "exact":
            report = dispatch(
                CommandRequest(command="migration.apply", plan_file=plan_file),
                apply=True,
                dependencies=dependencies,
            )
            assert report.outcome == "migration_applied"
            assert case_gitlab.labels == ["agent::blocked"]
            completed = case_ledger.load(12)
            assert completed is not None and completed.intent is None
        else:
            with pytest.raises(RuntimeError):
                dispatch(
                    CommandRequest(command="migration.apply", plan_file=plan_file),
                    apply=True,
                    dependencies=dependencies,
                )
            assert case_gitlab.labels == []

    conflicting_root = tmp_path / "conflicting"
    conflicting_config = _config(conflicting_root)
    conflicting_ledger = JsonLedger(conflicting_config.state_root)
    conflicting_ledger.persist(
        pending.model_copy(
            update={
                "revision": 0,
                "worktree": conflicting_config.state_root / "worktrees" / "12",
            }
        )
    )
    conflicting_gitlab = FakeGitLab(
        issue.model_copy(update={"labels": ("agent::awaiting-human",)})
    )
    conflicting_repository = FakeRepository(conflicting_config)
    conflicting_repository.inventory_result = (("agent/issue-12",), (), ())

    with pytest.raises(RuntimeError):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=_dependencies(
                conflicting_config,
                conflicting_ledger,
                conflicting_gitlab,
                conflicting_repository,
            ),
        )
    assert conflicting_gitlab.labels == []


@pytest.mark.parametrize(
    ("reviewed_state", "changed_state"),
    [("opened", "closed"), ("closed", "opened")],
)
def test_migration_rejects_pending_replay_when_issue_state_changed(
    tmp_path: Path, reviewed_state: str, changed_state: str
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    issue = _issue(state=reviewed_state).model_copy(
        update={"labels": ("agent::failed",)}
    )
    gitlab = FakeGitLab(issue)
    repository = FakeRepository(config)
    repository.inventory_result = (("agent/issue-12",), (), ())
    dependencies = _dependencies(config, ledger, gitlab, repository)
    preview = dispatch(
        CommandRequest(command="migration.preview"),
        apply=False,
        dependencies=dependencies,
    )
    plan_file = tmp_path / "migration-plan.json"
    plan_file.write_text(preview.model_dump_json())
    gitlab.label_failures = 1

    with pytest.raises(GitLabError, match="label"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )
    pending = ledger.load(12)
    assert pending is not None and pending.intent is not None
    gitlab.issue = issue.model_copy(update={"labels": (), "state": changed_state})

    with pytest.raises(RuntimeError, match="migration evidence changed"):
        dispatch(
            CommandRequest(command="migration.apply", plan_file=plan_file),
            apply=True,
            dependencies=dependencies,
        )

    assert gitlab.labels == []
    assert ledger.load(12) == pending
