# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior tests for bounded segments and CLI dispatch."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal

import pytest

from agent_work.domain import (
    LIFECYCLE_LABELS,
    CommandRequest,
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    split_fingerprint,
)
from agent_work.gitlab import GitLabError
from agent_work.ledger import JsonLedger
from agent_work.models import (
    AgentWorkConfig,
    DecisionReviewSignal,
    GitLabIssue,
    GitLabIssueNote,
    GitLabNoteAuthor,
    GitLabProject,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
    SplitChildEvidence,
    TransitionIntentRecord,
    WorkflowReceiptRecord,
)
from agent_work.repository import (
    PublicationError,
    RepositoryError,
    RepositoryTransientError,
    classify_publication_failure,
)
from agent_work.runtime import RuntimeDependencies, dispatch
from agent_work.serial import SegmentError, run_segment, select_record
from agent_work.state_machine import WORKFLOW_RECEIPTS
from agent_work.worker import WorkerInvocation, WorkerProtocolError

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
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
    title: str = "Change",
    is_draft: bool = False,
    description: str = "Closes #12",
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
        title=title,
        is_draft=is_draft,
        description=description,
    )


def _decision_draft() -> MergeRequestDraft:
    return MergeRequestDraft(
        title="Draft: retry policy decision",
        description="## Summary\nReview required.",
        is_draft=True,
        decision_review=DecisionReviewSignal(
            decisions=("Keep the retry policy unchanged.",),
            assumptions=("The existing MR can be reconciled.",),
            alternatives_considered=("Block for human direction.",),
            risks=("A retry could lose review context.",),
            reviewer_checks=("Confirm the retry policy.",),
            unresolved_findings=("Human review is required.",),
            evidence=("Red tests specify the behavior.",),
            tests=("uv run pytest tests/test_agent_work_serial_runtime.py",),
            correction_cycle_ledger=("cycle-1",),
            unchanged_project_owned_values=("retry budget",),
        ),
    )


class FakeGitLab:
    def __init__(self, issue: GitLabIssue | None = None) -> None:
        self.issue = issue or _issue()
        self.labels: list[str | None] = []
        self.comments: list[str] = []
        self.pipeline: PipelineEvidence | None = None
        self.existing_mr: MergeRequestEvidence | None = None
        self.open_branch_merge_requests: tuple[MergeRequestEvidence, ...] = ()
        self.open_branch_error: GitLabError | None = None
        self.open_branch_inventory_calls = 0
        self.create_error: RuntimeError | None = None
        self.created_merge_requests = 0
        self.reconciled_drafts: list[MergeRequestDraft] = []
        self.reconcile_error: GitLabError | None = None
        self.reconciled_merge_request: MergeRequestEvidence | None = None
        self.reconciliation_descriptions: list[str] = []
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
        self.split_children: dict[str, GitLabIssue] = {}
        self.split_child_matches: dict[str, tuple[GitLabIssue, ...]] = {}
        self.created_children: list[tuple[str, str]] = []
        self.decomposition_notes: list[str] = []
        self.closed_issues: list[int] = []
        self.child_creation_failures = 0
        self.created_child_override: GitLabIssue | None = None

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

    def open_merge_requests_by_branch(
        self, branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        del branch
        self.open_branch_inventory_calls += 1
        if self.open_branch_error is not None:
            raise self.open_branch_error
        return self.open_branch_merge_requests

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

    def reconcile_merge_request(
        self, mr: MergeRequestEvidence, *, issue_iid: int, draft: MergeRequestDraft
    ) -> MergeRequestEvidence:
        del issue_iid
        self.reconciled_drafts.append(draft)
        if self.reconcile_error is not None:
            raise self.reconcile_error
        canonical_description = (
            "# ⚠️ Decision review required\n"
            "<!-- agent-work-decision-review:v1 -->\n\n"
            f"{draft.description.rstrip()}\n\nCloses #12"
        )
        self.reconciliation_descriptions.append(canonical_description)
        reconciled = self.reconciled_merge_request or mr.model_copy(
            update={
                "title": draft.title,
                "description": canonical_description,
                "is_draft": draft.is_draft or mr.is_draft,
            }
        )
        self.existing_mr = reconciled
        return reconciled

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

    def split_child_inventory(self) -> tuple[GitLabIssue, ...]:
        return (
            *self.split_children.values(),
            *(item for items in self.split_child_matches.values() for item in items),
        )

    def create_split_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue:
        if self.child_creation_failures:
            self.child_creation_failures -= 1
            raise GitLabError("temporary child creation failure")
        self.created_children.append((title, marker))
        child = _issue(
            iid=max((item.iid for item in self.split_children.values()), default=100)
            + 1,
            description=description,
        ).model_copy(update={"title": title})
        if self.created_child_override is not None:
            child = self.created_child_override
        self.split_children[marker] = child
        return child

    def ensure_decomposition_note(self, iid: int, body: str) -> None:
        del iid
        if body not in self.decomposition_notes:
            self.decomposition_notes.append(body)

    def close_issue(self, iid: int) -> GitLabIssue:
        self.closed_issues.append(iid)
        return self.issue.model_copy(update={"state": "closed"})


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
        self.existing_worktree_error: RepositoryError | None = None
        self.prepare_error: RepositoryError | None = None
        self.push_error: RepositoryError | None = None
        self.push_observer: Callable[[str], None] | None = None
        self.binds = 0
        self.bound_sha = "a" * 40
        self.verified_heads: list[str] = []
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
        if self.existing_worktree_error is not None:
            raise self.existing_worktree_error
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
        del worktree, branch
        self.verified_heads.append(expected_head)
        if not self.worktree_valid:
            raise RepositoryError("worktree branch or baseline changed")

    def prepare_commit(
        self,
        worktree: Path,
        branch: str,
        issue_iid: int,
        expected_head: str | None = None,
    ) -> str:
        del worktree, branch, issue_iid, expected_head
        if self.prepare_error is not None:
            raise self.prepare_error
        return "a" * 40

    def push_prepared(self, worktree: Path, branch: str, publication_sha: str) -> str:
        del worktree, branch
        if self.push_error is not None:
            raise self.push_error
        if self.push_observer is not None:
            self.push_observer(publication_sha)
        self.pushes += 1
        return publication_sha

    def bind_retained_commit(
        self, worktree: Path, branch: str, issue_iid: int, baseline_sha: str
    ) -> str:
        del worktree, branch, issue_iid, baseline_sha
        self.binds += 1
        return self.bound_sha

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
        monotonic=lambda: 0.0,
        logger=logging.getLogger(f"test-agent-work-{id(ledger)}"),
        read_plan=lambda path: path.read_bytes(),
    )


class FakeInvoker:
    def __init__(self, output: str = "", returncode: int | None = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.prompts: list[str] = []

    def preflight(self) -> None:
        pass

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation:
        del checkout, timeout
        self.prompts.append(prompt)
        return WorkerInvocation(self.returncode, self.output, "safe diagnostic")


class BatchingInvoker:
    """Return coherent phase receipts for a complete coding workflow."""

    receipts: ClassVar[dict[str, tuple[str, ...]]] = {
        "discovery": ("$architect: approved",),
        "coding-red-docs": ("$tdd-red: complete", "$diataxis-writer: complete"),
        "coding-green": ("$tdd-green: complete",),
        "coding-nonfunctional": ("$nonfunctional-review: approved",),
        "coding-validation": ("$tdd-judge: approved", "$diataxis-judge: approved"),
        "coding-final-review": (
            "$judge-quality: approved",
            "$judge-test: approved",
            "$judge-cicd: approved",
            "$tdd-document: accurate",
            "$cli-syntax-judge: not applicable",
            "$filesystem-structure-judge: approved",
        ),
        "coding-finalize": ("$tdd-finalizer: approved",),
        "content-write": ("$documentation-writer: complete",),
        "content-review": (
            "$diataxis-judge: approved",
            "$agent-policy-judge: approved",
        ),
        "content-finalize": ("$documentation-finalizer: approved",),
        "post-publication": (
            "$pipeline-performance-judge: approved",
            "$findings-triage: no findings",
        ),
    }

    def __init__(
        self,
        reject_once: tuple[str, str] | None = None,
        *,
        lane: str = "coding",
    ) -> None:
        self.phases: list[str] = []
        self.prompts: list[str] = []
        self.timeouts: list[int] = []
        self.reject_once = reject_once
        self.lane = lane

    def preflight(self) -> None:
        pass

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation:
        del checkout
        self.prompts.append(prompt)
        self.timeouts.append(timeout)
        raw_record = prompt.split("Durable record: ", maxsplit=1)[1].splitlines()[0]
        record = ast.literal_eval(raw_record)
        phase = str(record["phase"])
        self.phases.append(phase)
        payload: dict[str, object] = {
            "protocol_version": 1,
            "project_id": record["project_id"],
            "issue_iid": record["issue_iid"],
            "ledger_revision": record["revision"],
            "expected_state": record["state"],
            "expected_phase": phase,
            "workflow_lane": self.lane,
            "result_kind": "ready_to_publish",
            "phase_status": "complete",
            "completed_phase": phase,
            "receipts": self.receipts[phase],
            "change_summary": f"Completed {phase}.",
            "next_action": "Continue the recoverable workflow.",
        }
        if self.reject_once is not None and phase == self.reject_once[0]:
            payload["phase_status"] = "rejected"
            payload["rejection_route"] = self.reject_once[1]
            self.reject_once = None
        if phase in {"coding-finalize", "content-finalize"}:
            payload["merge_request_draft"] = {
                "title": "Complete issue 12",
                "description": "Validated change.\n\nCloses #12",
            }
        return WorkerInvocation(
            0,
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>",
            "",
        )


class SingleSessionInvoker:
    """Return one complete nested-workflow result."""

    def __init__(self, *, lane: str = "coding", needs_human: bool = False) -> None:
        self.lane = lane
        self.needs_human = needs_human
        self.phases: list[str] = []

    def preflight(self) -> None:
        pass

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation:
        del checkout, timeout
        raw_record = prompt.split("Durable record: ", maxsplit=1)[1].splitlines()[0]
        record = ast.literal_eval(raw_record)
        phase = str(record["phase"])
        self.phases.append(phase)
        if self.lane == "content":
            receipts = (
                "$architect: approved",
                "$documentation-writer: complete",
                "$documentation-finalizer: approved",
            )
        else:
            receipts = (
                "$architect: approved",
                "$tdd-red: red confirmed",
                "$tdd-green: green confirmed and findings repaired",
                "$nonfunctional-review: approved",
                "$tdd-judge: approved",
                "$diataxis-judge: approved",
                "$judge-quality: approved",
                "$judge-test: approved",
                "$judge-cicd: approved",
                "$tdd-document: approved",
                "$cli-syntax-judge: not applicable — no CLI change",
                "$filesystem-structure-judge: approved",
                "$agent-policy-judge: not applicable — no agent policy change",
                "$tdd-finalizer: approved",
            )
        payload = {
            "protocol_version": 1,
            "project_id": record["project_id"],
            "issue_iid": record["issue_iid"],
            "ledger_revision": record["revision"],
            "expected_state": record["state"],
            "expected_phase": phase,
            "workflow_lane": self.lane,
            "result_kind": "ready_to_publish",
            "phase_status": "complete",
            "completed_phase": "workflow",
            "receipts": receipts,
            "change_summary": "Completed the nested workflow.",
            "next_action": "Publish for human review.",
            "merge_request_draft": {
                "title": "Complete issue 12",
                "description": "Validated change.\n\nCloses #12",
            },
        }
        if self.needs_human:
            payload.update(
                {
                    "result_kind": "needs_human",
                    "error_code": "state_reconciliation_failed",
                    "redacted_summary": "One consolidated finding remains unresolved.",
                    "next_action": "Review the unresolved finding.",
                }
            )
        return WorkerInvocation(
            0,
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>",
            "",
        )


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


def _complete_workflow_receipts() -> tuple[WorkflowReceiptRecord, ...]:
    """Return the durable coarse-workflow evidence needed for observation."""
    return (
        WorkflowReceiptRecord(
            phase=WorkflowPhase.WORKFLOW,
            verdict="complete",
            detail="\n".join(
                f"{skill}: approved" for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
            ),
        ),
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
    gitlab.existing_mr = _mr().model_copy(update={"sha": "b" * 40})

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


def test_invalid_worker_receipt_is_immediately_due(tmp_path: Path) -> None:
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
        "receipts": ["$wrong-skill: complete"],
        "change_summary": "Invalid receipt.",
        "next_action": "Retry.",
    }

    outcome = run_segment(
        ledger=ledger,
        gitlab=FakeGitLab(),  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>"
        ),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.WORKER_RECEIPT_INVALID
    assert outcome.retry_at == NOW


def test_malformed_worker_protocol_is_immediately_due(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN)
    )

    outcome = run_segment(
        ledger=ledger,
        gitlab=FakeGitLab(),  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker("<agent-work-result>not-json</agent-work-result>"),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.error_code is ErrorCode.WORKER_PROTOCOL_INVALID
    assert outcome.retry_at == NOW


def test_split_required_workflow_creates_children_in_order_then_closes_parent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    record = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    )
    record = ledger.persist(record)
    payload = {
        "protocol_version": 1,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": record.revision,
        "expected_state": "executing",
        "expected_phase": "workflow",
        "workflow_lane": "unknown",
        "result_kind": "split_required",
        "phase_status": "complete",
        "completed_phase": "workflow",
        "receipts": [],
        "split_plan": {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "first",
                    "title": "First slice",
                    "description": "Independent first work.",
                    "depends_on": [],
                },
                {
                    "ordinal": 2,
                    "key": "second",
                    "title": "Second slice",
                    "description": "Dependent second work.",
                    "depends_on": [1],
                },
            ]
        },
        "change_summary": "Split the parent into two mergeable slices.",
        "next_action": "Create the durable child issues.",
    }
    gitlab = FakeGitLab()

    decomposing = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(
            f"<agent-work-result>{json.dumps(payload)}</agent-work-result>"
        ),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    complete = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert decomposing is not None and decomposing.state is RunnerState.DECOMPOSING
    assert [title for title, _marker in gitlab.created_children] == [
        "First slice",
        "Second slice",
    ]
    first_fingerprint = hashlib.sha256(b"310967:12:first").hexdigest()
    second_fingerprint = hashlib.sha256(b"310967:12:second").hexdigest()
    assert first_fingerprint in gitlab.created_children[0][1]
    assert second_fingerprint in gitlab.created_children[1][1]
    child_descriptions = tuple(
        child.description for child in gitlab.split_children.values()
    )
    assert all("Parent: #12" in description for description in child_descriptions)
    assert "Depends-on: #" not in child_descriptions[0]
    assert "Depends-on: #" in child_descriptions[1]
    assert complete is not None and complete.state is RunnerState.COMPLETE
    assert gitlab.closed_issues == [12]
    assert len(gitlab.decomposition_notes) == 1


def test_split_decomposition_recovers_a_created_child_and_retries_only_the_remaining_child(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab()
    gitlab.child_creation_failures = 1
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
        ).model_copy(
            update={
                "state": RunnerState.DECOMPOSING,
                "workflow_lane": WorkflowLane.UNKNOWN,
                "decomposition_plan": {
                    "slices": [
                        {
                            "ordinal": 1,
                            "key": "first",
                            "title": "First slice",
                            "description": "Independent first work.",
                            "depends_on": [],
                        },
                        {
                            "ordinal": 2,
                            "key": "second",
                            "title": "Second slice",
                            "description": "Dependent second work.",
                            "depends_on": [1],
                        },
                    ]
                },
            }
        )
    )
    first_fingerprint = hashlib.sha256(b"310967:12:first").hexdigest()
    first_marker = (
        "<!-- agent-work-split:v1 "
        f"fingerprint={first_fingerprint} parent=12 key=first dependencies=none -->"
    )
    gitlab.split_children[first_marker] = _issue(iid=101).model_copy(
        update={
            "title": "First slice",
            "description": f"{first_marker}\n\nParent: #12\n\nIndependent first work.",
        }
    )

    retry = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    repository = FakeRepository(config)
    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW + timedelta(minutes=20),
    )

    assert retry is not None and retry.state is RunnerState.RETRY_WAIT
    assert resumed is not None and resumed.state is RunnerState.COMPLETE
    assert [title for title, _marker in gitlab.created_children] == ["Second slice"]


def test_malformed_split_envelope_blocks_before_any_decomposition_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    record = ledger.persist(
        _record(
            tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
        ).model_copy(update={"workflow_lane": WorkflowLane.UNKNOWN})
    )
    malformed = {
        "protocol_version": 1,
        "project_id": 310967,
        "issue_iid": 12,
        "ledger_revision": record.revision,
        "expected_state": "executing",
        "expected_phase": "workflow",
        "workflow_lane": "unknown",
        "result_kind": "split_required",
        "phase_status": "complete",
        "completed_phase": "workflow",
        "receipts": [],
        "split_plan": {
            "slices": [
                {
                    "ordinal": 1,
                    "key": "only",
                    "title": "Only",
                    "description": "Invalid singleton.",
                    "depends_on": [],
                }
            ]
        },
        "change_summary": "Malformed decomposition.",
        "next_action": "Do not mutate GitLab.",
    }
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(
            f"<agent-work-result>{json.dumps(malformed)}</agent-work-result>"
        ),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.RETRY_WAIT
    assert outcome.retry_at == NOW
    assert gitlab.created_children == []
    assert gitlab.decomposition_notes == []
    assert gitlab.closed_issues == []


@pytest.mark.parametrize("match_kind", ["duplicate", "conflicting"])
def test_split_recovery_blocks_ambiguous_marker_evidence_without_mutation(
    tmp_path: Path, match_kind: str
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    fingerprint = hashlib.sha256(b"310967:12:first").hexdigest()
    marker = (
        "<!-- agent-work-split:v1 "
        f"fingerprint={fingerprint} parent=12 key=first dependencies=none -->"
    )
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
        ).model_copy(
            update={
                "state": RunnerState.DECOMPOSING,
                "workflow_lane": WorkflowLane.UNKNOWN,
                "decomposition_plan": {
                    "slices": [
                        {
                            "ordinal": 1,
                            "key": "first",
                            "title": "First",
                            "description": "First slice.",
                            "depends_on": [],
                        },
                        {
                            "ordinal": 2,
                            "key": "second",
                            "title": "Second",
                            "description": "Second slice.",
                            "depends_on": [1],
                        },
                    ]
                },
            }
        )
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    exact = _issue(iid=101, description=f"{marker}\n\nParent: #12")
    gitlab.split_child_matches[marker] = (
        (exact, exact.model_copy(update={"iid": 102}))
        if match_kind == "duplicate"
        else (exact.model_copy(update={"description": f"{marker}\n\nwrong evidence"}),)
    )

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert gitlab.created_children == []
    assert gitlab.decomposition_notes == []
    assert gitlab.closed_issues == []


@pytest.mark.parametrize("override", ["title", "body", "parent"])
def test_split_creation_rejects_inexact_returned_child_before_parent_mutation(
    tmp_path: Path, override: str
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    plan = {
        "slices": [
            {
                "ordinal": 1,
                "key": "one",
                "title": "One",
                "description": "One.",
                "depends_on": [],
            },
            {
                "ordinal": 2,
                "key": "two",
                "title": "Two",
                "description": "Two.",
                "depends_on": [1],
            },
        ]
    }
    from agent_work.domain import render_split_marker

    payload = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_dump(mode="python")
    payload.update(
        {
            "state": RunnerState.DECOMPOSING,
            "workflow_lane": WorkflowLane.UNKNOWN,
            "decomposition_plan": plan,
            "decomposition_progress": (
                SplitChildEvidence(
                    ordinal=1,
                    key="one",
                    fingerprint=split_fingerprint(310967, 12, "one"),
                    issue_iid=101,
                ),
            ),
        }
    )
    ledger.persist(LedgerRecord.model_validate(payload))
    marker = render_split_marker(310967, 12, "two", (101,))
    expected = f"{marker}\n\nParent: #12\nDepends-on: #101\n\nTwo."
    gitlab = FakeGitLab()
    gitlab.created_child_override = _issue(
        iid=12 if override == "parent" else 102,
        description="altered" if override == "body" else expected,
    ).model_copy(update={"title": "altered" if override == "title" else "Two"})
    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=FakeRepository(config),
        invoke=FakeInvoker(),
        timeout_seconds=10,
        now=NOW,
    )  # type: ignore[arg-type]
    assert outcome is not None and outcome.state is RunnerState.BLOCKED
    assert len(gitlab.created_children) == 1
    assert len(outcome.decomposition_progress) == 1
    assert gitlab.decomposition_notes == [] and gitlab.closed_issues == []


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
    assert outcome.retry_at == NOW
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
        assert outcome.retry_at == NOW


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


def test_future_retry_wait_skips_recovery_inventory_and_preserves_retry_budget(
    tmp_path: Path,
) -> None:
    """A not-yet-due recovery does not contact GitLab or consume a retry."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    waiting = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "prior_state": RunnerState.PUBLISHING,
                "prior_phase": WorkflowPhase.READY_TO_PUBLISH,
                "retry_at": NOW + timedelta(minutes=10),
                "retry_count": 1,
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_error = GitLabError("inventory must not run yet")

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is None
    assert gitlab.open_branch_inventory_calls == 0
    assert ledger.load(waiting.issue_iid) == waiting


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
    ("state", "phase", "prior_state", "error_code"),
    [
        (RunnerState.PREPARING, WorkflowPhase.WORKFLOW, None, None),
        (RunnerState.EXECUTING, WorkflowPhase.WORKFLOW, None, None),
        (RunnerState.PUBLISHING, WorkflowPhase.READY_TO_PUBLISH, None, None),
        (
            RunnerState.RETRY_WAIT,
            WorkflowPhase.READY_TO_PUBLISH,
            RunnerState.PUBLISHING,
            None,
        ),
        (
            RunnerState.BLOCKED,
            WorkflowPhase.READY_TO_PUBLISH,
            None,
            ErrorCode.RETRY_EXHAUSTED,
        ),
        (
            RunnerState.BLOCKED,
            WorkflowPhase.READY_TO_PUBLISH,
            None,
            ErrorCode.STATE_RECONCILIATION_FAILED,
        ),
    ],
)
def test_recovery_of_published_ordinary_work_skips_worker_and_new_publication(
    tmp_path: Path,
    state: RunnerState,
    phase: WorkflowPhase,
    prior_state: RunnerState | None,
    error_code: ErrorCode | None,
) -> None:
    """An exact previously-created MR is authoritative across stale states."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=state, phase=phase).model_copy(
            update={
                "prior_state": prior_state,
                "prior_phase": phase if prior_state is not None else None,
                "retry_at": NOW if prior_state is not None else None,
                "retry_count": 3,
                "error_code": error_code,
                "receipts": _complete_workflow_receipts(),
            }
        )
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.open_branch_merge_requests = (_mr(sha="b" * 40),)
    gitlab.existing_mr = gitlab.open_branch_merge_requests[0]
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="b" * 40, web_url="https://pipeline/4"
    )
    repository = FakeRepository(config)
    invoker = FakeInvoker()

    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert recovered is not None and recovered.state is RunnerState.AWAITING_HUMAN
    assert recovered.source_sha == "b" * 40
    assert recovered.merge_request_iid == 88
    assert recovered.retry_count == 0
    assert recovered.intent is None
    assert recovered.prior_state is None and recovered.prior_phase is None
    assert recovered.error_code is None and recovered.retry_at is None
    assert invoker.prompts == []
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0


def test_recovery_of_published_draft_reconciles_without_downgrading_it(
    tmp_path: Path,
) -> None:
    """Recovery keeps durable decision-review Draft metadata on the existing MR."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    draft = _decision_draft()
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": draft,
                "receipts": _complete_workflow_receipts(),
            }
        )
    )
    gitlab = FakeGitLab()
    existing = _mr(is_draft=True, title=draft.title, description=draft.description)
    gitlab.open_branch_merge_requests = (existing,)
    gitlab.existing_mr = existing
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    repository = FakeRepository(config)

    recovered = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert recovered is not None and recovered.state is RunnerState.AWAITING_HUMAN
    assert gitlab.reconciled_drafts == [draft]
    assert gitlab.existing_mr is not None and gitlab.existing_mr.is_draft
    assert repository.pushes == 0 and gitlab.created_merge_requests == 0
    assert repository.verified_heads == ["a" * 40, "a" * 40]


@pytest.mark.parametrize("changed_field", ["iid", "web_url", "sha"])
def test_recovery_of_published_draft_blocks_reconciliation_identity_drift(
    tmp_path: Path, changed_field: str
) -> None:
    """A Draft PUT response must retain pinned MR identity and source SHA."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    draft = _decision_draft()
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_draft": draft,
                "receipts": _complete_workflow_receipts(),
            }
        )
    )
    gitlab = FakeGitLab()
    existing = _mr(is_draft=True, title=draft.title, description=draft.description)
    gitlab.open_branch_merge_requests = (existing,)
    gitlab.existing_mr = existing
    changes: dict[str, int | str] = {
        "iid": 89,
        "web_url": "https://mr/89",
        "sha": "b" * 40,
    }
    gitlab.reconciled_merge_request = existing.model_copy(
        update={changed_field: changes[changed_field]}
    )
    repository = FakeRepository(config)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.verified_heads == ["a" * 40]


def test_unpinned_selection_skips_terminal_stale_record_without_open_mr(
    tmp_path: Path,
) -> None:
    """A stale terminal record without publication evidence cannot starve new work."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.BLOCKED,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(update={"error_code": ErrorCode.RETRY_EXHAUSTED})
    )

    class MultiIssueGitLab(FakeGitLab):
        def list_eligible(self) -> tuple[GitLabIssue, ...]:
            return (_issue(iid=13),)

    selected, issue = select_record(
        ledger,
        MultiIssueGitLab(_issue().model_copy(update={"labels": ("agent::blocked",)})),
        now=NOW,
    )

    assert selected is not None and selected.issue_iid == 13
    assert issue is not None and issue.iid == 13


def test_unpinned_selection_scans_every_terminal_record_for_a_later_published_mr(
    tmp_path: Path,
) -> None:
    """An earlier empty terminal inventory must not hide later published work."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    first = ledger.persist(
        _record(
            tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.READY_TO_PUBLISH
        ).model_copy(update={"error_code": ErrorCode.RETRY_EXHAUSTED})
    )
    second = ledger.persist(
        _record(
            tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.READY_TO_PUBLISH
        ).model_copy(
            update={
                "issue_iid": 13,
                "source_branch": "agent/issue-13",
                "worktree": config.state_root / "worktrees" / "13",
                "error_code": ErrorCode.STATE_RECONCILIATION_FAILED,
            }
        )
    )

    class MultiRecordGitLab(FakeGitLab):
        def fetch_issue(self, iid: int) -> GitLabIssue:
            return _issue(iid=iid).model_copy(update={"labels": ("agent::blocked",)})

        def open_merge_requests_by_branch(
            self, branch: str
        ) -> tuple[MergeRequestEvidence, ...]:
            return (
                (_mr(source_branch=branch),) if branch == second.source_branch else ()
            )

    selected, issue = select_record(ledger, MultiRecordGitLab(), now=NOW)

    assert first.issue_iid == 12
    assert selected is not None and selected.issue_iid == second.issue_iid
    assert issue is not None and issue.iid == second.issue_iid


def test_fresh_preparing_record_with_existing_mr_blocks_before_worker_or_worktree(
    tmp_path: Path,
) -> None:
    """A synthesized record cannot adopt publication evidence from a crash."""
    config = _config(tmp_path)
    fresh = _record(
        tmp_path, state=RunnerState.PREPARING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(update={"revision": 0})
    ledger = ChangingSnapshotJsonLedger(config.state_root, (fresh,))
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(),)
    repository = FakeRepository(config)
    invoker = FakeInvoker()

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.prepared is False
    assert invoker.prompts == [] and repository.pushes == 0
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    ("state", "error_code", "expected_state", "expected_error"),
    [
        (
            RunnerState.PUBLISHING,
            None,
            RunnerState.RETRY_WAIT,
            ErrorCode.GITLAB_RETRYABLE,
        ),
        (
            RunnerState.BLOCKED,
            ErrorCode.RETRY_EXHAUSTED,
            RunnerState.BLOCKED,
            ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
        ),
    ],
)
def test_recovery_inventory_error_is_retryable_or_remains_fail_closed(
    tmp_path: Path,
    state: RunnerState,
    error_code: ErrorCode | None,
    expected_state: RunnerState,
    expected_error: ErrorCode,
) -> None:
    """Inventory transport errors cannot replay publication or escape a block."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=state, phase=WorkflowPhase.READY_TO_PUBLISH).model_copy(
            update={"error_code": error_code}
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_error = GitLabError("temporary inventory failure")
    repository = FakeRepository(config)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is expected_state
    assert outcome.error_code is expected_error
    assert repository.pushes == 0 and gitlab.created_merge_requests == 0


def test_recovery_inventory_error_from_retry_wait_resumes_prior_active_state(
    tmp_path: Path,
) -> None:
    """Recovery retries from the prior state instead of a RETRY_WAIT self-edge."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "prior_state": RunnerState.PUBLISHING,
                "prior_phase": WorkflowPhase.READY_TO_PUBLISH,
                "retry_at": NOW,
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_error = GitLabError("temporary inventory failure")

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
    assert outcome.prior_state is RunnerState.PUBLISHING


def test_preparation_recovery_inventory_error_retries_from_preparing(
    tmp_path: Path,
) -> None:
    """A preparation recovery inventory error retains its recoverable prior state."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.WORKFLOW,
        ).model_copy(
            update={
                "prior_state": RunnerState.PREPARING,
                "prior_phase": WorkflowPhase.WORKFLOW,
                "retry_at": NOW,
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_error = GitLabError("temporary inventory failure")

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
    assert outcome.prior_state is RunnerState.PREPARING


def test_due_preparation_retry_resumes_preparing_before_worktree_mutation(
    tmp_path: Path,
) -> None:
    """A due preparation retry returns to preparation through an allowed edge."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.WORKFLOW,
        ).model_copy(
            update={
                "prior_state": RunnerState.PREPARING,
                "prior_phase": WorkflowPhase.WORKFLOW,
                "retry_at": NOW,
            }
        )
    )
    gitlab = FakeGitLab()
    repository = FakeRepository(config)

    resumed = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert resumed is not None and resumed.state is RunnerState.PREPARING
    assert resumed.retry_at is None
    assert repository.prepared is False


@pytest.mark.parametrize(
    ("state", "updates", "expected_state", "expected_error", "expected_prior_state"),
    [
        (
            RunnerState.PUBLISHING,
            {},
            RunnerState.RETRY_WAIT,
            ErrorCode.GIT_TRANSPORT_RETRYABLE,
            RunnerState.PUBLISHING,
        ),
        (
            RunnerState.RETRY_WAIT,
            {
                "prior_state": RunnerState.PUBLISHING,
                "prior_phase": WorkflowPhase.READY_TO_PUBLISH,
                "retry_at": NOW,
            },
            RunnerState.RETRY_WAIT,
            ErrorCode.GIT_TRANSPORT_RETRYABLE,
            RunnerState.PUBLISHING,
        ),
        (
            RunnerState.BLOCKED,
            {"error_code": ErrorCode.RETRY_EXHAUSTED},
            RunnerState.BLOCKED,
            ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
            None,
        ),
    ],
)
def test_recovery_transient_fetch_normalizes_active_waiting_and_blocked_states(
    tmp_path: Path,
    state: RunnerState,
    updates: dict[str, object],
    expected_state: RunnerState,
    expected_error: ErrorCode,
    expected_prior_state: RunnerState | None,
) -> None:
    """A transient published-work fetch observes the recovery state policy."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=state, phase=WorkflowPhase.READY_TO_PUBLISH).model_copy(
            update=updates
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(),)
    repository = FakeRepository(config)
    repository.existing_worktree_error = RepositoryTransientError("temporary fetch")

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is expected_state
    assert outcome.error_code is expected_error
    assert outcome.prior_state is expected_prior_state


@pytest.mark.parametrize(
    "candidates",
    [
        (_mr(), _mr()),
        (_mr(source_project_id=999),),
        (_mr(target_project_id=999),),
        (_mr(source_branch="agent/other"),),
        (_mr(target_branch="release"),),
        (_mr(state="closed"),),
    ],
)
def test_recovery_blocks_ambiguous_or_inexact_open_mr_inventory_before_side_effects(
    tmp_path: Path, candidates: tuple[MergeRequestEvidence, ...]
) -> None:
    """Inventory ambiguity or identity drift must never replay publication."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(update={"receipts": _complete_workflow_receipts()})
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = candidates
    repository = FakeRepository(config)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.pushes == 0 and gitlab.created_merge_requests == 0


@pytest.mark.parametrize("failure", ["closing-relation", "worktree"])
def test_recovery_blocks_when_existing_mr_cannot_be_bound_to_durable_work(
    tmp_path: Path, failure: str
) -> None:
    """A discovered MR is usable only after closing and local identity verification."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "publication_sha": "a" * 40,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "receipts": _complete_workflow_receipts(),
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(),)
    gitlab.existing_mr = gitlab.open_branch_merge_requests[0]
    if failure == "closing-relation":
        gitlab.closing_results = [False]
    repository = FakeRepository(config)
    if failure == "worktree":
        repository.worktree_valid = False

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert blocked is not None and blocked.state is RunnerState.BLOCKED
    assert blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    assert repository.pushes == 0 and gitlab.created_merge_requests == 0


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
    ledger.persist(
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
    assert recovered.intent is None
    assert recovered.publication_sha is None
    assert repository.pushes == 0
    assert not repository.prepared
    assert not gitlab.created_merge_requests


def test_recovery_blocks_mr_sha_mismatch_with_durable_publication_sha(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.PUBLISHING, phase=WorkflowPhase.READY_TO_PUBLISH
        ).model_copy(
            update={
                "publication_sha": "b" * 40,
                "receipts": _complete_workflow_receipts(),
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(sha="a" * 40),)
    repository = FakeRepository(config)
    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=10,
        now=NOW,
    )  # type: ignore[arg-type]
    assert (
        outcome is not None
        and outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    )
    assert (
        repository.pushes == 0
        and gitlab.created_merge_requests == 0
        and not gitlab.reconciled_drafts
    )


@pytest.mark.parametrize("durable_field", ["merge_request_iid", "merge_request_url"])
def test_recovery_blocks_durable_mr_identifier_mismatch_before_side_effects(
    tmp_path: Path, durable_field: str
) -> None:
    """Persisted MR identifiers are constraints, not evidence that may be replaced."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    mismatch = 99 if durable_field == "merge_request_iid" else "https://mr/99"
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(update={durable_field: mismatch})
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(),)
    repository = FakeRepository(config)

    blocked = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert (
        blocked is not None
        and blocked.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    )
    assert repository.prepared is False and gitlab.reconciled_drafts == []


@pytest.mark.parametrize("failure", ["closing", "draft"])
@pytest.mark.parametrize("state", [RunnerState.PUBLISHING, RunnerState.BLOCKED])
def test_recovery_gitlab_binding_errors_retry_active_and_preserve_blocks(
    tmp_path: Path, failure: str, state: RunnerState
) -> None:
    """GitLab transport errors are retryable only while stale recovery is active."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=state, phase=WorkflowPhase.READY_TO_PUBLISH).model_copy(
            update={
                "error_code": ErrorCode.RETRY_EXHAUSTED
                if state is RunnerState.BLOCKED
                else None,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
                if failure == "draft"
                else None,
            }
        )
    )
    gitlab = FakeGitLab()
    gitlab.open_branch_merge_requests = (_mr(),)
    gitlab.closing_error = (
        GitLabError("temporary closing failure") if failure == "closing" else None
    )
    gitlab.reconcile_error = (
        GitLabError("temporary draft failure") if failure == "draft" else None
    )
    repository = FakeRepository(config)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None
    if state is RunnerState.BLOCKED:
        assert outcome.state is RunnerState.BLOCKED
        assert outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    else:
        assert outcome.state is RunnerState.RETRY_WAIT
        assert outcome.error_code is ErrorCode.GITLAB_RETRYABLE


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
                "publication_sha": "a" * 40,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
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


@pytest.mark.parametrize(
    "error_code",
    (
        ErrorCode.GIT_AUTHENTICATION,
        ErrorCode.GIT_AUTHORIZATION,
        ErrorCode.GIT_POLICY,
        ErrorCode.GIT_PUBLICATION_CONFIGURATION,
    ),
)
def test_permanent_git_publication_failure_blocks_without_consuming_retry_budget(
    tmp_path: Path, error_code: ErrorCode
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    initial = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "retry_count": 2,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
            }
        )
    )
    repository = FakeRepository(config)

    failure = classify_publication_failure(
        {
            ErrorCode.GIT_AUTHENTICATION: "fatal: Authentication failed",
            ErrorCode.GIT_AUTHORIZATION: "remote: You are not allowed to push",
            ErrorCode.GIT_POLICY: "remote: pre-receive hook declined",
            ErrorCode.GIT_PUBLICATION_CONFIGURATION: "fatal: unexpected failure",
        }[error_code]
    )
    assert failure.code is error_code
    repository.push_error = PublicationError(failure)
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
    assert outcome.error_code is error_code
    assert outcome.retry_count == initial.retry_count
    assert outcome.retry_at is None
    assert outcome.intent is not None and outcome.intent.kind == "publish_merge_request"
    assert gitlab.labels[-1] == "agent::blocked"
    assert str(error_code) in gitlab.comments[-1]


def test_fresh_publication_persists_sha_before_push(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.PUBLISHING, phase=WorkflowPhase.READY_TO_PUBLISH
        ).model_copy(
            update={
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                )
            }
        )
    )
    repository = FakeRepository(config)

    def observe(sha: str) -> None:
        record = ledger.load(12)
        assert record is not None and record.publication_sha == sha
        assert record.intent is not None
        assert record.intent.expected_evidence["publication_sha"] == sha

    repository.push_observer = observe
    outcome = run_segment(
        ledger=ledger,
        gitlab=FakeGitLab(),
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=10,
        now=NOW,
    )  # type: ignore[arg-type]
    assert outcome is not None and outcome.state is RunnerState.OBSERVING


def test_replay_without_durable_publication_sha_blocks_without_git_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.PUBLISHING, phase=WorkflowPhase.READY_TO_PUBLISH
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
    repository = FakeRepository(config)
    outcome = run_segment(
        ledger=ledger,
        gitlab=FakeGitLab(),
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=10,
        now=NOW,
    )  # type: ignore[arg-type]
    assert (
        outcome is not None
        and outcome.error_code is ErrorCode.PUBLICATION_IDENTITY_MISMATCH
    )
    assert not repository.prepared and repository.pushes == 0


def test_explicit_legacy_retry_binds_then_replays_exact_commit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.READY_TO_PUBLISH
        ).model_copy(
            update={
                "source_sha": "a" * 40,
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
    gitlab = FakeGitLab()
    gitlab.existing_mr = _mr().model_copy(update={"sha": "b" * 40})
    repository = FakeRepository(config)
    repository.bound_sha = "b" * 40
    dispatch(
        CommandRequest(command="loop.retry", issue_iid=12),
        apply=True,
        dependencies=_dependencies(config, ledger, gitlab, repository),
    )
    bound = ledger.load(12)
    assert bound is not None and bound.state is RunnerState.PUBLISHING
    assert bound.publication_sha == "b" * 40 and repository.binds == 1
    assert (
        bound.intent is not None
        and bound.intent.expected_evidence["publication_sha"] == "b" * 40
    )
    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=10,
        now=NOW,
    )  # type: ignore[arg-type]
    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert repository.pushes == 1 and not repository.prepared
    assert gitlab.created_merge_requests == 0


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
                "publication_sha": "a" * 40,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "intent": TransitionIntentRecord(
                    operation_id="12:publish:1",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "publication_sha": "a" * 40,
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


def test_publication_reconciles_existing_mr_to_preserve_decision_review_draft(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    draft = _decision_draft()
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(update={"merge_request_draft": draft})
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
    assert gitlab.created_merge_requests == 0
    assert gitlab.reconciled_drafts == [draft]


def test_replayed_publication_reconciles_the_recorded_mr_with_decision_review(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    draft = _decision_draft()
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "merge_request_iid": 88,
                "publication_sha": "a" * 40,
                "merge_request_draft": draft,
                "intent": TransitionIntentRecord(
                    operation_id="12:publish:0",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "publication_sha": "a" * 40,
                    },
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
    assert gitlab.reconciled_drafts == [draft]


def test_remediation_reconciles_only_its_recorded_mr_with_decision_review(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    draft = _decision_draft()
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "work_kind": WorkKind.EXISTING_MR_REMEDIATION,
                "merge_request_iid": 88,
                "merge_request_draft": draft,
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
    assert gitlab.reconciled_drafts == [draft]


def test_existing_draft_is_not_downgraded_by_ordinary_publication_metadata(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ordinary = MergeRequestDraft(title="Normal change", description="Details")
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(update={"merge_request_draft": ordinary})
    )
    gitlab = FakeGitLab()
    gitlab.existing_mr = _mr(title="Draft: earlier review", is_draft=True)

    outcome = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=FakeRepository(config),  # type: ignore[arg-type]
        invoke=FakeInvoker(),  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert gitlab.existing_mr is not None and gitlab.existing_mr.is_draft is True


def test_repeated_reconciliation_has_one_canonical_notice_and_closing_reference() -> (
    None
):
    gitlab = FakeGitLab()
    draft = _decision_draft()
    existing = _mr(description="old description\n\nCloses #12")

    first = gitlab.reconcile_merge_request(existing, issue_iid=12, draft=draft)
    second = gitlab.reconcile_merge_request(first, issue_iid=12, draft=draft)

    assert second.description == first.description
    assert second.description.count("Closes #12") == 1
    assert second.description.count("# ⚠️ Decision review required") == 1


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


def test_policy_canary_completes_workflow_and_runner_creates_merge_request(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    repository = FakeRepository(config)
    repository.paths = ("AGENTS.md",)
    gitlab = FakeGitLab(_issue(description="Update AGENTS.md safely."))
    invoker = SingleSessionInvoker(lane="content")

    prepared = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    completed = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )
    published = run_segment(
        ledger=ledger,
        gitlab=gitlab,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        invoke=invoker,  # type: ignore[arg-type]
        timeout_seconds=10,
        now=NOW,
    )

    assert prepared is not None and prepared.state is RunnerState.EXECUTING
    assert completed is not None and completed.state is RunnerState.PUBLISHING
    assert published is not None and published.state is RunnerState.OBSERVING
    assert invoker.phases == ["workflow"]
    assert repository.pushes == 1
    assert gitlab.created_merge_requests == 1


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        WorkflowReceiptRecord(
            phase=WorkflowPhase.WORKFLOW,
            verdict="complete",
            detail="$architect: approved",
        ),
        WorkflowReceiptRecord(
            phase=WorkflowPhase.WORKFLOW,
            verdict="complete",
            detail=(
                "$architect: approved\n"
                "$documentation-writer: complete\n"
                "$wrong-skill: approved"
            ),
        ),
        WorkflowReceiptRecord(
            phase=WorkflowPhase.WORKFLOW,
            verdict="complete",
            detail=(
                "$architect: approved\n"
                "$documentation-writer: complete\n"
                "$documentation-writer: complete"
            ),
        ),
        WorkflowReceiptRecord(
            phase=WorkflowPhase.WORKFLOW,
            verdict="rejected",
            detail=(
                "$architect: approved\n"
                "$documentation-writer: complete\n"
                "$documentation-finalizer: approved"
            ),
        ),
    ],
)
def test_protected_policy_publication_with_invalid_receipts_fails_closed(
    tmp_path: Path, receipt: WorkflowReceiptRecord | None
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
                "work_kind": WorkKind.PROTECTED_POLICY_MANUAL,
                "workflow_lane": WorkflowLane.CONTENT,
                "merge_request_draft": MergeRequestDraft(
                    title="Policy update", description="Validated policy update."
                ),
                "receipts": () if receipt is None else (receipt,),
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
    assert outcome.error_code is ErrorCode.WORKER_RECEIPT_INVALID
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    ("workflow_lane", "receipt", "draft"),
    [
        (
            WorkflowLane.CODING,
            WorkflowReceiptRecord(
                phase=WorkflowPhase.WORKFLOW,
                verdict="complete",
                detail="\n".join(
                    f"{skill}: {'not applicable — no policy change' if skill == '$agent-policy-judge' else 'approved'}"
                    for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
                ),
            ),
            MergeRequestDraft(title="Policy update", description="Validated."),
        ),
        (
            WorkflowLane.CONTENT,
            WorkflowReceiptRecord(
                phase=WorkflowPhase.WORKFLOW,
                verdict="complete",
                detail="\n".join(
                    f"{skill}: approved"
                    for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
                ),
            ),
            MergeRequestDraft(
                title="Draft: policy update",
                is_draft=True,
                description=(
                    "unresolved findings; exact human decisions; tests; cycle ledger; "
                    "unchanged project-owned values"
                ),
            ),
        ),
    ],
)
def test_protected_policy_publication_rejects_incoherent_receipt_or_draft(
    tmp_path: Path,
    workflow_lane: WorkflowLane,
    receipt: WorkflowReceiptRecord,
    draft: MergeRequestDraft,
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
                "work_kind": WorkKind.PROTECTED_POLICY_MANUAL,
                "workflow_lane": workflow_lane,
                "merge_request_draft": draft,
                "receipts": (receipt,),
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
    assert outcome.error_code is ErrorCode.WORKER_RECEIPT_INVALID
    assert repository.pushes == 0
    assert gitlab.created_merge_requests == 0


@pytest.mark.parametrize(
    ("workflow_lane", "receipt", "draft"),
    [
        (
            WorkflowLane.CODING,
            WorkflowReceiptRecord(
                phase=WorkflowPhase.WORKFLOW,
                verdict="complete",
                detail="\n".join(
                    f"{skill}: approved"
                    for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
                ),
            ),
            MergeRequestDraft(title="Policy update", description="Validated."),
        ),
        (
            WorkflowLane.CONTENT,
            WorkflowReceiptRecord(
                phase=WorkflowPhase.WORKFLOW,
                verdict="complete",
                detail="\n".join(
                    f"{skill}: approved"
                    for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
                ),
            ),
            MergeRequestDraft(
                title="Draft: policy update",
                is_draft=True,
                description=(
                    "unresolved findings and evidence; exact human decisions; tests; "
                    "cycle ledger; unchanged project-owned values"
                ),
            ),
        ),
        (
            WorkflowLane.CONTENT,
            WorkflowReceiptRecord(
                phase=WorkflowPhase.WORKFLOW,
                verdict="complete",
                detail="\n".join(
                    f"{skill}: approved"
                    for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
                ),
            ),
            _decision_draft(),
        ),
    ],
)
def test_protected_policy_publication_with_complete_evidence_publishes(
    tmp_path: Path,
    workflow_lane: WorkflowLane,
    receipt: WorkflowReceiptRecord,
    draft: MergeRequestDraft,
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
                "work_kind": WorkKind.PROTECTED_POLICY_MANUAL,
                "workflow_lane": workflow_lane,
                "merge_request_draft": draft,
                "receipts": (receipt,),
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

    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert repository.pushes == 1
    assert gitlab.created_merge_requests == 1


@pytest.mark.parametrize(
    ("receipts", "expected_state", "expected_pushes"),
    [
        (
            (
                WorkflowReceiptRecord(
                    phase=WorkflowPhase.WORKFLOW,
                    verdict="rejected",
                    detail="$architect: rejected",
                ),
                WorkflowReceiptRecord(
                    phase=WorkflowPhase.WORKFLOW,
                    verdict="complete",
                    detail="\n".join(
                        f"{skill}: approved"
                        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
                    ),
                ),
            ),
            RunnerState.OBSERVING,
            1,
        ),
        (
            (
                WorkflowReceiptRecord(
                    phase=WorkflowPhase.WORKFLOW,
                    verdict="complete",
                    detail="\n".join(
                        f"{skill}: approved"
                        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
                    ),
                ),
                WorkflowReceiptRecord(
                    phase=WorkflowPhase.WORKFLOW,
                    verdict="rejected",
                    detail="$architect: rejected",
                ),
            ),
            RunnerState.BLOCKED,
            0,
        ),
    ],
)
def test_protected_policy_publication_uses_latest_workflow_receipt(
    tmp_path: Path,
    receipts: tuple[WorkflowReceiptRecord, WorkflowReceiptRecord],
    expected_state: RunnerState,
    expected_pushes: int,
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
                "work_kind": WorkKind.PROTECTED_POLICY_MANUAL,
                "workflow_lane": WorkflowLane.CONTENT,
                "merge_request_draft": MergeRequestDraft(
                    title="Policy update", description="Validated."
                ),
                "receipts": receipts,
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

    assert outcome is not None and outcome.state is expected_state
    assert repository.pushes == expected_pushes
    assert gitlab.created_merge_requests == expected_pushes


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


def test_apply_batches_the_complete_coding_workflow_until_human_handoff(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    repository = FakeRepository(config)
    invoker = SingleSessionInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, repository), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    assert report.outcome == "activation_complete"
    assert report.stop_reason == "awaiting_human"
    assert report.segments_run == 4
    assert len(report.items) == 4
    assert report.items[-1].state is RunnerState.AWAITING_HUMAN
    assert invoker.phases == ["workflow"]
    assert gitlab.created_merge_requests == 1


def test_applied_run_preflight_failure_precedes_lock_and_lifecycle_mutation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    repository = FakeRepository(config)

    class UnsupportedInvoker:
        def __init__(self) -> None:
            self.preflight_calls = 0
            self.invoke_calls = 0

        def preflight(self) -> None:
            self.preflight_calls += 1
            raise WorkerProtocolError("codex exec does not support --add-dir")

        def invoke(self, checkout: Path, prompt: str, timeout: int) -> WorkerInvocation:
            del checkout, prompt, timeout
            self.invoke_calls += 1
            raise AssertionError("invoke must not run after failed preflight")

    def lock() -> object:
        raise AssertionError("lock must not run after failed preflight")

    invoker = UnsupportedInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, repository),
        invoke=invoker,  # type: ignore[arg-type]
        lock=lock,  # type: ignore[arg-type]
    )

    with pytest.raises(WorkerProtocolError, match="--add-dir"):
        dispatch(
            CommandRequest(command="loop.run"), apply=True, dependencies=dependencies
        )

    assert invoker.preflight_calls == 1
    assert invoker.invoke_calls == 0
    assert gitlab.labels == []
    assert ledger.list_records() == ()
    assert not repository.prepared


def test_apply_batches_the_complete_content_workflow_until_human_handoff(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    invoker = SingleSessionInvoker(lane="content")
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "awaiting_human"
    assert report.segments_run == 4
    assert invoker.phases == ["workflow"]


def test_apply_defers_failed_workflow_pipeline_repair_to_the_next_activation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    gitlab.pipeline = PipelineEvidence(
        id=4, status="failed", sha="a" * 40, web_url="https://pipeline/4"
    )
    invoker = SingleSessionInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    persisted = ledger.load(12)
    assert report.stop_reason == "repair"
    assert report.segments_run == 4
    assert invoker.phases == ["workflow"]
    assert persisted is not None and persisted.state is RunnerState.REPAIR
    assert persisted.phase is WorkflowPhase.WORKFLOW
    assert persisted.error_code is ErrorCode.PIPELINE_REJECTED


def test_apply_runs_one_workflow_owner_when_resuming_pipeline_repair(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(tmp_path, state=RunnerState.REPAIR, phase=WorkflowPhase.WORKFLOW)
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    gitlab.pipeline = PipelineEvidence(
        id=4, status="failed", sha="a" * 40, web_url="https://pipeline/4"
    )
    invoker = SingleSessionInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    persisted = ledger.load(12)
    assert report.stop_reason == "repair"
    assert report.segments_run == 3
    assert invoker.phases == ["workflow"]
    assert persisted is not None and persisted.state is RunnerState.REPAIR
    assert persisted.phase is WorkflowPhase.WORKFLOW


def test_failed_pipeline_keeps_legacy_phase_ledger_on_coding_green(
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


def test_apply_segment_limit_yields_and_next_activation_recovers(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha="a" * 40, web_url="https://pipeline/4"
    )
    repository = FakeRepository(config)
    invoker = SingleSessionInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, repository), invoke=invoker
    )

    first = dispatch(
        CommandRequest(command="loop.run", max_segments=3, max_activation_seconds=3600),
        apply=True,
        dependencies=dependencies,
    )
    resumed = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    persisted = ledger.load(12)
    assert first.stop_reason == "segment_limit"
    assert first.segments_run == 3
    assert first.items[-1].phase is WorkflowPhase.PIPELINE_WAIT
    assert resumed.stop_reason == "awaiting_human"
    assert resumed.items[0].phase is WorkflowPhase.PIPELINE_WAIT
    assert invoker.phases == ["workflow"]
    assert persisted is not None and persisted.state is RunnerState.AWAITING_HUMAN


def test_apply_never_reconciles_another_issue_after_pinning_active_work(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    active = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.CODING_GREEN
    )
    closed_blocked = _record(
        tmp_path, state=RunnerState.BLOCKED, phase=WorkflowPhase.CODING_GREEN
    ).model_copy(
        update={
            "issue_iid": 13,
            "source_branch": "agent/issue-13",
            "worktree": tmp_path / "state" / "worktrees" / "13",
        }
    )
    ledger.persist(active)
    closed_blocked = ledger.persist(closed_blocked)

    class MultipleIssueGitLab(FakeGitLab):
        def __init__(self) -> None:
            super().__init__()
            self.issues = {
                12: _issue().model_copy(update={"labels": ("agent::running",)}),
                13: _issue(iid=13, state="closed").model_copy(
                    update={"labels": ("agent::blocked",)}
                ),
            }
            self.transitions: list[tuple[int, str | None]] = []
            self.fetches: list[int] = []

        def fetch_issue(self, iid: int) -> GitLabIssue:
            self.fetches.append(iid)
            if iid != 12:
                raise AssertionError("pinned activation fetched an unrelated issue")
            return self.issues[iid]

        def transition_labels(self, iid: int, label: str | None) -> GitLabIssue:
            self.transitions.append((iid, label))
            return self.issues[iid]

    gitlab = MultipleIssueGitLab()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)),
        invoke=BatchingInvoker(),
    )

    report = dispatch(
        CommandRequest(command="loop.run", max_segments=1),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "segment_limit"
    assert [item.issue_iid for item in report.items] == [12]
    assert ledger.load(13) == closed_blocked
    assert gitlab.fetches == [12]
    assert all(iid == 12 for iid, _label in gitlab.transitions)


def test_apply_stops_after_one_pending_pipeline_observation(tmp_path: Path) -> None:
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
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=_dependencies(config, ledger, gitlab, FakeRepository(config)),
    )

    assert report.stop_reason == "pipeline_wait"
    assert report.segments_run == 2
    assert [item.state for item in report.items] == [
        RunnerState.OBSERVING,
        RunnerState.OBSERVING,
    ]
    assert gitlab.created_merge_requests == 1


def test_apply_respects_activation_time_budget_between_segments(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    readings = iter((0.0, 0.0, 61.0))
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)),
        monotonic=lambda: next(readings),
    )

    report = dispatch(
        CommandRequest(command="loop.run", max_segments=16, max_activation_seconds=60),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "time_limit"
    assert report.segments_run == 1
    assert report.items[0].state is RunnerState.EXECUTING


def test_apply_gives_each_workflow_invocation_the_remaining_activation_budget(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.EXECUTING,
            phase=WorkflowPhase.CODING_GREEN,
        )
    )
    invoker = BatchingInvoker()
    readings = iter((0.0, 0.0, 0.2, 12.2, 12.2, 10_801.0))
    dependencies = replace(
        _dependencies(config, ledger, FakeGitLab(), FakeRepository(config)),
        invoke=invoker,
        monotonic=lambda: next(readings),
    )

    report = dispatch(
        CommandRequest(
            command="loop.run",
            max_segments=16,
            max_activation_seconds=10_800,
        ),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "time_limit"
    assert invoker.timeouts == [10_800, 10_788]
    assert "effective bounded worker budget for this session is 10800 seconds" in (
        invoker.prompts[0].casefold()
    )
    assert "effective bounded worker budget for this session is 10788 seconds" in (
        invoker.prompts[1].casefold()
    )


def test_apply_returns_unresolved_workflow_feedback_without_relaunching(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    gitlab = FakeGitLab(_issue())
    invoker = SingleSessionInvoker(needs_human=True)
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "blocked"
    assert report.items[-1].summary == "One consolidated finding remains unresolved."
    assert invoker.phases == ["workflow"]


def test_apply_resumes_a_due_retry_and_continues_in_the_same_activation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.CODING_FINALIZE,
        ).model_copy(
            update={
                "retry_at": NOW,
                "prior_state": RunnerState.EXECUTING,
                "prior_phase": WorkflowPhase.CODING_FINALIZE,
            }
        )
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::running",)}))
    invoker = BatchingInvoker()
    dependencies = replace(
        _dependencies(config, ledger, gitlab, FakeRepository(config)), invoke=invoker
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=dependencies,
    )

    assert report.stop_reason == "pipeline_wait"
    assert report.items[0].state is RunnerState.EXECUTING
    assert invoker.phases == ["coding-finalize"]


def test_apply_relaunches_immediately_due_worker_exits_until_retry_exhaustion(
    tmp_path: Path,
) -> None:
    """Worker exits retry in one bounded activation on the retained worktree."""
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.EXECUTING,
            phase=WorkflowPhase.CODING_GREEN,
        )
    )
    repository = FakeRepository(config)
    invoker = FakeInvoker(returncode=1)
    clock_reads = 0

    def clock() -> datetime:
        nonlocal clock_reads
        value = NOW + timedelta(seconds=clock_reads)
        clock_reads += 1
        return value

    report = dispatch(
        CommandRequest(command="loop.run", max_segments=8, max_activation_seconds=3600),
        apply=True,
        dependencies=replace(
            _dependencies(config, ledger, FakeGitLab(), repository),
            invoke=invoker,
            clock=clock,
        ),
    )

    persisted = ledger.load(12)
    assert report.stop_reason == "blocked"
    assert len(invoker.prompts) == 4
    assert report.items[0].retry_at == NOW
    assert all(item.issue_iid == 12 for item in report.items)
    assert persisted is not None and persisted.error_code is ErrorCode.RETRY_EXHAUSTED
    assert repository.prepared is False
    assert repository.verified_heads == ["a" * 40] * 4


def test_apply_yields_without_mutation_for_a_future_retry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    waiting = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.RETRY_WAIT,
            phase=WorkflowPhase.CODING_GREEN,
        ).model_copy(
            update={
                "retry_at": NOW + timedelta(minutes=20),
                "prior_state": RunnerState.EXECUTING,
                "prior_phase": WorkflowPhase.CODING_GREEN,
            }
        )
    )

    report = dispatch(
        CommandRequest(
            command="loop.run", max_segments=16, max_activation_seconds=3600
        ),
        apply=True,
        dependencies=_dependencies(
            config, ledger, FakeGitLab(), FakeRepository(config)
        ),
    )

    assert report.outcome == "activation_complete"
    assert report.stop_reason == "retry_wait"
    assert report.segments_run == 0
    assert ledger.load(12) == waiting


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


def test_retry_replays_retained_publication_intent_without_restarting_workflow(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ledger = JsonLedger(config.state_root)
    retained = ledger.persist(
        _record(
            tmp_path,
            state=RunnerState.BLOCKED,
            phase=WorkflowPhase.READY_TO_PUBLISH,
        ).model_copy(
            update={
                "error_code": ErrorCode.RETRY_EXHAUSTED,
                "retry_count": 3,
                "source_sha": "a" * 40,
                "publication_sha": "a" * 40,
                "merge_request_draft": MergeRequestDraft(
                    title="Change", description="Details"
                ),
                "intent": TransitionIntentRecord(
                    operation_id="12:publish:1",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": "agent/issue-12",
                        "baseline_sha": "a" * 40,
                        "publication_sha": "a" * 40,
                    },
                ),
            }
        )
    )
    gitlab = FakeGitLab(_issue().model_copy(update={"labels": ("agent::blocked",)}))
    repository = FakeRepository(config)

    replay = dispatch(
        CommandRequest(command="loop.retry", issue_iid=12),
        apply=True,
        dependencies=_dependencies(config, ledger, gitlab, repository),
    )

    resumed = replay.items[0]
    assert replay.outcome == "retry_scheduled"
    assert resumed.state is RunnerState.PUBLISHING
    assert resumed.phase is WorkflowPhase.READY_TO_PUBLISH
    assert resumed.source_sha == retained.source_sha
    assert resumed.source_branch == retained.source_branch
    persisted = ledger.load(12)
    assert persisted is not None
    assert persisted.merge_request_draft == retained.merge_request_draft
    assert (
        persisted.intent is not None
        and persisted.intent.kind == "publish_merge_request"
    )
    assert repository.prepared is False
    assert repository.pushes == 0
    assert gitlab.labels == ["agent::running"]


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
