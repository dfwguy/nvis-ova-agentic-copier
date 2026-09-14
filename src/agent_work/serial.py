# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""One bounded, recoverable state-machine segment."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from .classification import ClassificationError, classify_work, is_protected_policy_path
from .domain import (
    ACTIVE_STATES,
    LIFECYCLE_LABELS,
    ErrorCode,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    render_split_marker,
    split_fingerprint,
)
from .gitlab import GitLabError, GitLabIdentityError
from .models import (
    ConditionEvidence,
    GitLabIssue,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
    SplitChildEvidence,
    TransitionIntentRecord,
)
from .ports import GitLabPort, InvokerPort, LedgerPort, RepositoryPort
from .repository import PublicationError, RepositoryError, RepositoryTransientError
from .state_machine import (
    apply_worker_result,
    has_complete_workflow_receipt,
    plan_retry,
    transition_record,
)
from .worker import (
    WorkerProtocolError,
    build_worker_prompt,
    format_worker_result_diagnostic,
    parse_worker_result,
    validate_worker_result,
    worker_exit_evidence,
)


class SegmentError(RuntimeError):
    """A segment could not safely reconcile or record its outcome."""


def _has_workflow_receipt(record: LedgerRecord) -> bool:
    return any(receipt.phase is WorkflowPhase.WORKFLOW for receipt in record.receipts)


def _split_description(
    marker: str, parent_iid: int, description: str, dependencies: tuple[int, ...]
) -> str:
    """Render one child body with human-readable provenance."""
    links = "\n".join(f"Depends-on: #{item}" for item in dependencies)
    dependency_section = f"{links}\n" if links else ""
    return f"{marker}\n\nParent: #{parent_iid}\n{dependency_section}\n{description}".rstrip()


def _split_child_is_exact(
    child: GitLabIssue,
    *,
    parent_iid: int,
    title: str,
    description: str,
    assigned_iids: set[int],
) -> bool:
    """Return whether one recovered or created child exactly matches its plan evidence."""
    return (
        child.iid != parent_iid
        and child.iid not in assigned_iids
        and child.title == title
        and child.description == description
    )


def _decompose(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    close_parent: bool = True,
) -> LedgerRecord:
    """Persistently create or recover every split child, then close its parent."""
    plan = record.decomposition_plan
    if plan is None:
        raise SegmentError("decomposition state lacks a plan")
    children = list(record.decomposition_progress)
    try:
        inventory = gitlab.split_child_inventory()
        for slice_ in plan.slices[len(children) :]:
            dependency_iids = tuple(
                children[ordinal - 1].issue_iid for ordinal in slice_.depends_on
            )
            marker = render_split_marker(
                record.project_id, record.issue_iid, slice_.key, dependency_iids
            )
            matches = tuple(issue for issue in inventory if marker in issue.description)
            if len(matches) > 1:
                raise SegmentError("split child marker evidence is ambiguous")
            description = _split_description(
                marker, record.issue_iid, slice_.description, dependency_iids
            )
            if matches:
                child = matches[0]
                if not _split_child_is_exact(
                    child,
                    parent_iid=record.issue_iid,
                    title=slice_.title,
                    description=description,
                    assigned_iids={item.issue_iid for item in children},
                ):
                    raise SegmentError(
                        "split child marker evidence conflicts with the plan"
                    )
            else:
                child = gitlab.create_split_child(
                    title=slice_.title, description=description, marker=marker
                )
                if not _split_child_is_exact(
                    child,
                    parent_iid=record.issue_iid,
                    title=slice_.title,
                    description=description,
                    assigned_iids={item.issue_iid for item in children},
                ):
                    raise SegmentError("created split child evidence is not exact")
            children.append(
                SplitChildEvidence(
                    ordinal=slice_.ordinal,
                    key=slice_.key,
                    fingerprint=split_fingerprint(
                        record.project_id, record.issue_iid, slice_.key
                    ),
                    issue_iid=child.iid,
                )
            )
            record = ledger.persist(
                record.model_copy(update={"decomposition_progress": tuple(children)})
            )
        if not close_parent:
            return record
        note = "\n".join(
            ["<!-- agent-work-decomposition:v1 -->", "Decomposed into:"]
            + [f"- #{child.issue_iid}" for child in children]
        )
        gitlab.ensure_decomposition_note(record.issue_iid, note)
        gitlab.close_issue(record.issue_iid)
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
            allow_remote_exhaustion_side_effects=False,
        )
    except SegmentError as error:
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                summary=str(error),
                next_action="Resolve conflicting split evidence before retrying.",
            )
        )
        gitlab.transition_labels(record.issue_iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            record.issue_iid,
            error_code=str(ErrorCode.STATE_RECONCILIATION_FAILED),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    return ledger.persist(
        transition_record(
            record,
            RunnerState.COMPLETE,
            summary="Parent decomposed into durable child issues.",
            next_action="No action required.",
        )
    )


def _draft_handoff_is_valid(draft: MergeRequestDraft) -> bool:
    """Validate the mandatory content of a controversial Draft handoff."""
    if not draft.title.startswith("Draft:"):
        return True
    if draft.decision_review is not None:
        return True
    description = draft.description.casefold()
    return all(
        phrase in description
        for phrase in (
            "unresolved findings",
            "evidence",
            "exact human decisions",
            "tests",
            "cycle ledger",
            "unchanged project-owned values",
        )
    )


def _new_record(
    issue: GitLabIssue, *, project_id: int, state_root: Path, main_branch: str
) -> LedgerRecord:
    try:
        classification = classify_work(issue, project_id=project_id)
    except ClassificationError as error:
        return LedgerRecord(
            project_id=project_id,
            issue_iid=issue.iid,
            state=RunnerState.BLOCKED,
            phase=WorkflowPhase.DISCOVERY,
            work_kind=WorkKind.ORDINARY,
            source_branch=f"agent/issue-{issue.iid}",
            worktree=state_root / "worktrees" / str(issue.iid),
            error_code=ErrorCode.REMEDIATION_MARKER_INVALID,
            summary=str(error),
            next_action="Correct or remove the remediation marker, then retry explicitly.",
        )
    branch = classification.source_branch or f"agent/issue-{issue.iid}"
    return LedgerRecord(
        project_id=project_id,
        issue_iid=issue.iid,
        state=RunnerState.PREPARING,
        phase=WorkflowPhase.WORKFLOW,
        work_kind=classification.kind,
        source_branch=branch,
        issue_title=issue.title,
        main_branch=main_branch,
        worktree=state_root / "worktrees" / str(issue.iid),
        merge_request_iid=classification.merge_request_iid,
        source_sha=classification.source_sha,
        summary="Issue claimed for durable preparation.",
        next_action="Prepare its durable worktree.",
    )


def _due(record: LedgerRecord, now: datetime) -> bool:
    return record.state is not RunnerState.RETRY_WAIT or (
        record.retry_at is not None and record.retry_at <= now
    )


def _can_reconcile_published_ordinary_work(record: LedgerRecord) -> bool:
    """Return whether a stale ordinary record may already have an opened MR."""
    if (
        record.work_kind is not WorkKind.ORDINARY
        or record.revision == 0
        or record.phase is WorkflowPhase.POST_PUBLICATION
    ):
        return False
    if record.state in {
        RunnerState.PREPARING,
        RunnerState.EXECUTING,
        RunnerState.PUBLISHING,
    }:
        return True
    if record.state is RunnerState.RETRY_WAIT:
        return record.prior_state in {
            RunnerState.PREPARING,
            RunnerState.EXECUTING,
            RunnerState.PUBLISHING,
        }
    return record.state is RunnerState.BLOCKED and record.error_code in {
        ErrorCode.RETRY_EXHAUSTED,
        ErrorCode.STATE_RECONCILIATION_FAILED,
    }


def _is_fresh_ordinary_preparation(record: LedgerRecord) -> bool:
    """Return whether an un-revised ordinary record is about to prepare work."""
    return (
        record.work_kind is WorkKind.ORDINARY
        and record.revision == 0
        and record.state is RunnerState.PREPARING
    )


def _feedback_is_qualifying(note: object, factory_username: str) -> bool:
    """Keep only human, non-outcome MR feedback as untrusted task data."""
    body = str(getattr(note, "body", ""))
    author = getattr(note, "author", None)
    return bool(
        body.strip()
        and not bool(getattr(note, "system", False))
        and author
        and author != factory_username
        and not body.lstrip().startswith("<!-- agent-work-outcome:v1")
        and not body.lstrip().startswith("<!-- agent-work-feedback-outcome:")
    )


def _acknowledge_feedback(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    """Acknowledge one already-decided item without replaying its worker."""
    pending = record.pending_feedback
    if pending is None or pending.outcome is None or not pending.acknowledgement_ready:
        raise SegmentError("feedback acknowledgement has no durable outcome")
    if record.merge_request_iid is None or record.source_sha is None:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Feedback acknowledgement lacks exact MR evidence.",
        )
    try:
        _verify_authenticated_user(gitlab)
        repository.verify_worktree(
            record.worktree, record.source_branch, record.source_sha
        )
        if repository.has_changes(record.worktree):
            raise RepositoryError("durable worktree must be clean")
        mr = gitlab.fetch_merge_request(record.merge_request_iid)
        if not _mr_evidence_is_exact(
            record,
            mr,
            gitlab=gitlab,
            repository=repository,
            source_sha=record.source_sha,
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback acknowledgement MR evidence is not exact.",
            )
        _verify_authenticated_user(gitlab)
        gitlab.comment_feedback_outcome(
            record.merge_request_iid,
            issue_iid=record.issue_iid,
            source=pending.source,
            note_id=pending.note_id,
            discussion_id=pending.discussion_id,
            selection_sha=pending.source_sha,
            summary=pending.outcome,
        )
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    except RepositoryError as error:
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.AWAITING_HUMAN,
            phase=WorkflowPhase.POST_PUBLICATION,
            feedback_cursor=pending.target_cursor,
            pending_feedback=None,
            merge_request_draft=None,
            summary="Human feedback outcome acknowledged.",
            next_action="Await human review or further feedback.",
        )
    )


def _wake_for_feedback(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord | None:
    """Atomically select unseen qualifying feedback for one exact open MR."""
    if record.merge_request_iid is None or record.source_sha is None:
        return None
    try:
        _verify_authenticated_user(gitlab)
        mr = gitlab.fetch_merge_request(record.merge_request_iid)
        cursor = record.feedback_cursor
        if cursor is not None and cursor.feedback_source_sha != record.source_sha:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback cursor source SHA does not match recorded worktree.",
            )
        if not _mr_publication_identity_is_exact(
            record, mr, repository=repository, source_sha=mr.sha
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback merge request evidence is not exact.",
            )
        if not gitlab.mr_closes_issue(mr.iid, record.issue_iid):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback merge request does not close its issue.",
            )
        feedback = tuple(
            item
            if isinstance(item, MergeRequestFeedback)
            else MergeRequestFeedback.model_validate(item)
            for item in gitlab.fetch_merge_request_feedback(mr.iid)
        )
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
            allow_remote_exhaustion_side_effects=False,
        )
    except RepositoryError:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Feedback worktree evidence could not be verified.",
        )
    username = repository.config.factory_username
    qualifying = tuple(
        note for note in feedback if _feedback_is_qualifying(note, username)
    )
    cursor = record.feedback_cursor
    unseen = tuple(
        note
        for note in qualifying
        if (
            (
                note.source == "general"
                and (
                    cursor is None
                    or note.id > (cursor.last_processed_general_note_id or 0)
                )
            )
            or (
                note.source == "discussion"
                and (
                    cursor is None
                    or note.id > (cursor.last_processed_discussion_note_id or 0)
                )
            )
        )
    )
    if not unseen:
        return None
    selected = min(unseen, key=lambda note: (note.created_at, note.id))
    prior_general = None if cursor is None else cursor.last_processed_general_note_id
    prior_discussion = (
        None if cursor is None else cursor.last_processed_discussion_note_id
    )
    target = MergeRequestFeedbackCursor(
        last_processed_general_note_id=(
            selected.id if selected.source == "general" else prior_general
        ),
        last_processed_discussion_note_id=(
            selected.id if selected.source == "discussion" else prior_discussion
        ),
        last_relevant_discussion_id=(
            selected.discussion_id
            if selected.source == "discussion"
            else (None if cursor is None else cursor.last_relevant_discussion_id)
        ),
        feedback_source_sha=mr.sha,
    )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.REPAIR,
            phase=WorkflowPhase.WORKFLOW,
            pending_feedback=PendingMergeRequestFeedback(
                target_cursor=target,
                source_sha=mr.sha,
                source=selected.source,
                note_id=selected.id,
                discussion_id=selected.discussion_id,
            ),
            # The recorded worktree stays at its durable head until the next
            # segment can resync it after this selection is persisted.
            source_sha=record.source_sha,
            merge_request_draft=None,
            summary="Unseen human merge-request feedback selected.",
            next_action="Run one existing-MR feedback remediation workflow.",
        )
    )


def _synchronize_pending_feedback(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    """Resume a durably selected feedback item before running its worker."""
    pending = record.pending_feedback
    if pending is None or pending.outcome is not None or record.source_sha is None:
        return record
    if record.source_sha == pending.source_sha:
        return record
    try:
        _verify_authenticated_user(gitlab)
        mr = gitlab.fetch_merge_request(record.merge_request_iid or 0)
        if not (
            _mr_publication_identity_is_exact(
                record, mr, repository=repository, source_sha=pending.source_sha
            )
            and gitlab.mr_closes_issue(mr.iid, record.issue_iid)
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback merge request evidence is not exact.",
            )
        repository.sync_feedback_worktree(
            record.worktree, record.source_branch, record.source_sha, pending.source_sha
        )
        repository.verify_worktree(
            record.worktree, record.source_branch, pending.source_sha
        )
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    except RepositoryTransientError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
        )
    except RepositoryError as error:
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    return ledger.persist(record.model_copy(update={"source_sha": pending.source_sha}))


def select_record(
    ledger: LedgerPort,
    gitlab: GitLabPort,
    *,
    now: datetime,
    issue_iid: int | None = None,
    repository: RepositoryPort,
) -> tuple[LedgerRecord | None, GitLabIssue | None]:
    """Reconcile work before ranking eligible fresh issues by priority and age."""
    project = gitlab.verify_project()
    records = ledger.list_records()
    if any(record.project_id != project.id for record in records):
        raise SegmentError("ledger record belongs to another project")
    active = tuple(record for record in records if record.state in ACTIVE_STATES)
    if len(active) > 1:
        raise SegmentError("multiple resumable records require reconciliation")
    if issue_iid is not None:
        pinned = next(
            (record for record in records if record.issue_iid == issue_iid), None
        )
        if pinned is None:
            raise SegmentError("pinned issue is not the active durable record")
        if _can_reconcile_published_ordinary_work(pinned):
            return pinned, gitlab.fetch_issue(pinned.issue_iid)
        if pinned not in active:
            raise SegmentError("pinned issue is not the active durable record")
        if not _due(pinned, now):
            return None, None
        return pinned, gitlab.fetch_issue(pinned.issue_iid)
    issues = {
        record.issue_iid: gitlab.fetch_issue(record.issue_iid) for record in records
    }
    for record in records:
        issue = issues[record.issue_iid]
        if issue.state != "opened" and record.state is not RunnerState.COMPLETE:
            return record, issue
    if active:
        record = active[0]
        if _can_reconcile_published_ordinary_work(record):
            return record, issues[record.issue_iid]
        if not _due(record, now):
            return None, None
        return record, issues[record.issue_iid]
    for waiting in sorted(
        (record for record in records if record.state is RunnerState.AWAITING_HUMAN),
        key=lambda record: record.issue_iid,
    ):
        woken = _wake_for_feedback(
            waiting, ledger=ledger, gitlab=gitlab, repository=repository, now=now
        )
        if woken is not None:
            if woken.state is RunnerState.RETRY_WAIT:
                return None, None
            return woken, issues[woken.issue_iid]
    for reconciliable in (
        record for record in records if _can_reconcile_published_ordinary_work(record)
    ):
        try:
            if gitlab.open_merge_requests_by_branch(reconciliable.source_branch):
                return reconciliable, issues[reconciliable.issue_iid]
        except (GitLabError, TypeError):
            return reconciliable, issues[reconciliable.issue_iid]
    lifecycle = set(LIFECYCLE_LABELS)
    for record in records:
        issue = issues[record.issue_iid]
        expected = label_for_state(record.state)
        if set(issue.labels).intersection(lifecycle) != (
            {expected} if expected else set()
        ):
            return record, issue
    record_iids = {record.issue_iid for record in records}
    orphan = next(
        (issue for issue in gitlab.list_projected() if issue.iid not in record_iids),
        None,
    )
    if orphan is not None:
        return (
            LedgerRecord(
                project_id=project.id,
                issue_iid=orphan.iid,
                state=RunnerState.BLOCKED,
                phase=WorkflowPhase.DISCOVERY,
                work_kind=WorkKind.ORDINARY,
                source_branch=f"agent/issue-{orphan.iid}",
                worktree=ledger.state_root / "worktrees" / str(orphan.iid),
                error_code=ErrorCode.MISSING_LEDGER,
                summary="Lifecycle label has no durable ledger record.",
                next_action="Review local and GitLab evidence, then retry explicitly.",
            ),
            orphan,
        )
    eligible = tuple(
        issue for issue in gitlab.list_eligible() if issue.iid not in record_iids
    )
    if not eligible:
        return None, None
    issue = eligible[0]
    return (
        _new_record(
            issue,
            project_id=project.id,
            state_root=ledger.state_root,
            main_branch=repository.config.default_branch,
        ),
        issue,
    )


class IdentityVerificationTransportError(GitLabError):
    """Mark an ordinary transport failure that leaves identity unverified."""


def _verify_authenticated_user(gitlab: GitLabPort) -> None:
    """Verify identity while preserving its distinct retry safety boundary."""
    try:
        gitlab.verify_authenticated_user()
    except GitLabIdentityError:
        raise
    except GitLabError as error:
        raise IdentityVerificationTransportError(str(error)) from error


def _completed_closed_record(record: LedgerRecord) -> LedgerRecord:
    """Return the canonical terminal record for one remotely closed issue."""
    updates: dict[str, object] = {
        "retry_count": 0,
        "retry_at": None,
        "prior_state": None,
        "prior_phase": None,
        "intent": None,
        "error_code": None,
        "worker_exit": None,
        "pending_feedback": None,
        "summary": "Issue is closed; durable record reconciled.",
        "next_action": "No action required.",
    }
    if record.state is RunnerState.COMPLETE:
        return record.model_copy(update=updates)
    return transition_record(record, RunnerState.COMPLETE, **updates)


def reconcile_closed_issues(
    ledger: LedgerPort, gitlab: GitLabPort, *, project_id: int
) -> None:
    """Persist closed records before clearing their lifecycle-label projections."""
    inventory = gitlab.list_closed_projected()
    for issue in sorted(inventory, key=lambda item: item.iid):
        record = ledger.load(issue.iid)
        if record is not None:
            if record.project_id != project_id:
                raise ValueError("ledger record project does not match")
            completed = _completed_closed_record(record)
            if completed != record:
                ledger.persist(completed)
        _verify_authenticated_user(gitlab)
        gitlab.transition_labels(issue.iid, None)


def _record_retry(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    error_code: ErrorCode,
    detail: str | None = None,
    retry_delay: timedelta = timedelta(minutes=20),
    allow_remote_exhaustion_side_effects: bool = True,
) -> LedgerRecord:
    """Persist one bounded retry and optionally publish its exhaustion block."""
    outcome = plan_retry(record, now=now, retry_delay=retry_delay).record
    if outcome.state is RunnerState.RETRY_WAIT:
        history = (
            *record.condition_history,
            ConditionEvidence(
                occurred_at=now,
                retry_count=outcome.retry_count,
                code=error_code.value,
                detail=detail or outcome.summary,
            ),
        )[-64:]
        outcome = outcome.model_copy(
            update={"error_code": error_code, "condition_history": history}
        )
    outcome = ledger.persist(outcome)
    if outcome.state is RunnerState.BLOCKED and allow_remote_exhaustion_side_effects:
        gitlab.transition_labels(record.issue_iid, label_for_state(outcome.state))
        gitlab.comment_outcome(
            record.issue_iid,
            error_code=str(outcome.error_code),
            summary=outcome.summary,
            next_action=outcome.next_action,
        )
    return outcome


def _record_identity_retry(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
) -> LedgerRecord:
    """Persist an unverified-identity retry without remote exhaustion effects."""
    return _record_retry(
        record,
        ledger=ledger,
        gitlab=gitlab,
        now=now,
        error_code=ErrorCode.GITLAB_RETRYABLE,
        allow_remote_exhaustion_side_effects=False,
    )


def _resume_due_retry(record: LedgerRecord, *, ledger: LedgerPort) -> LedgerRecord:
    """Restore a due retry's durable prior state before another retryable operation."""
    if record.state is not RunnerState.RETRY_WAIT:
        return record
    return ledger.persist(
        transition_record(
            record,
            record.prior_state or RunnerState.EXECUTING,
            phase=record.prior_phase or record.phase,
            retry_at=None,
            error_code=None,
            worker_exit=None,
        )
    )


def _mr_publication_identity_is_exact(
    record: LedgerRecord,
    mr: MergeRequestEvidence,
    *,
    repository: RepositoryPort,
    source_sha: str,
) -> bool:
    """Verify required merge-request publication identity and state."""
    return (
        mr.state == "opened"
        and mr.source_project_id == record.project_id
        and mr.target_project_id == record.project_id
        and mr.source_branch == record.source_branch
        and mr.target_branch == repository.config.default_branch
        and mr.sha == source_sha
    )


def _publication_intent_is_exact(record: LedgerRecord, *, baseline_sha: str) -> bool:
    """Return whether durable intent authorizes this publication replay."""
    intent = record.intent
    if intent is None:
        return False
    issue_iid, issue_separator, operation = intent.operation_id.partition(":")
    action, action_separator, revision = operation.partition(":")
    record_revision = str(record.revision)
    revision_is_canonical = revision == "0" or (
        revision.isascii() and revision.isdecimal() and not revision.startswith("0")
    )
    revision_is_older = len(revision) < len(record_revision) or (
        len(revision) == len(record_revision) and revision < record_revision
    )
    return (
        intent.kind == "publish_merge_request"
        and issue_iid == str(record.issue_iid)
        and issue_separator == ":"
        and action == "publish"
        and action_separator == ":"
        and revision_is_canonical
        and revision_is_older
        and intent.expected_evidence
        == (
            {
                "branch": record.source_branch,
                "baseline_sha": baseline_sha,
                "publication_sha": record.publication_sha,
            }
            if record.publication_sha is not None
            else {"branch": record.source_branch, "baseline_sha": baseline_sha}
        )
    )


def _mr_evidence_is_exact(
    record: LedgerRecord,
    mr: MergeRequestEvidence,
    *,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    source_sha: str,
) -> bool:
    """Verify the complete merge-request identity and closing relationship."""
    return _mr_publication_identity_is_exact(
        record, mr, repository=repository, source_sha=source_sha
    ) and gitlab.mr_closes_issue(mr.iid, record.issue_iid)


def _block_publication_identity(
    record: LedgerRecord, *, ledger: LedgerPort, gitlab: GitLabPort, summary: str
) -> LedgerRecord:
    """Persist and project one actionable publication-identity block."""
    updates = {
        "error_code": ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
        "summary": summary,
        "next_action": "Review the exact MR, branch, target, SHA, and closing evidence.",
    }
    blocked = ledger.persist(
        record.model_copy(update=updates)
        if record.state is RunnerState.BLOCKED
        else transition_record(record, RunnerState.BLOCKED, **updates)
    )
    gitlab.transition_labels(record.issue_iid, label_for_state(blocked.state))
    gitlab.comment_outcome(
        record.issue_iid,
        error_code=str(blocked.error_code),
        summary=blocked.summary,
        next_action=blocked.next_action,
    )
    return blocked


def _block_identity_drift(
    record: LedgerRecord, *, ledger: LedgerPort, summary: str
) -> LedgerRecord:
    """Fail closed locally when the authenticated service identity changed."""
    updates = {
        "error_code": ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
        "summary": summary,
        "next_action": "Restore the configured service identity before resuming.",
    }
    return ledger.persist(
        record.model_copy(update=updates)
        if record.state is RunnerState.BLOCKED
        else transition_record(record, RunnerState.BLOCKED, **updates)
    )


def _retry_or_block_recovery_transport_error(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    summary: str,
    error_code: ErrorCode,
) -> LedgerRecord:
    """Retry active recovery transport failures while retaining existing blocks."""
    if record.state is RunnerState.BLOCKED:
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=summary
        )
    record = _resume_due_retry(record, ledger=ledger)
    return _record_retry(
        record,
        ledger=ledger,
        gitlab=gitlab,
        now=now,
        error_code=error_code,
    )


def _block_fresh_preparation_with_published_mr(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
) -> LedgerRecord | None:
    """Fail closed when a newly synthesized record already has an open MR."""
    try:
        candidates = gitlab.open_merge_requests_by_branch(record.source_branch)
    except GitLabError:
        return _retry_or_block_recovery_transport_error(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            summary="Published merge request inventory could not be verified.",
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    if not candidates:
        return None
    return _block_publication_identity(
        record,
        ledger=ledger,
        gitlab=gitlab,
        summary="A newly synthesized record cannot adopt an existing merge request.",
    )


def _reconcile_published_ordinary_work(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord | None:
    """Recover an already-published ordinary record without replaying publication."""
    try:
        candidates = gitlab.open_merge_requests_by_branch(record.source_branch)
    except GitLabError:
        return _retry_or_block_recovery_transport_error(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            summary="Published merge request inventory could not be verified.",
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    if not candidates:
        return None
    if len(candidates) != 1:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Opened merge request inventory for the recorded branch is ambiguous.",
        )
    mr = candidates[0]
    if record.publication_sha is not None and record.publication_sha != mr.sha:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Discovered merge request SHA does not match durable publication evidence.",
        )
    if (
        record.merge_request_iid is not None and record.merge_request_iid != mr.iid
    ) or (
        record.merge_request_url is not None and record.merge_request_url != mr.web_url
    ):
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Discovered merge request does not match durable evidence.",
        )
    candidate_iid = mr.iid
    candidate_url = mr.web_url
    candidate_sha = mr.sha
    try:
        if not _mr_evidence_is_exact(
            record, mr, gitlab=gitlab, repository=repository, source_sha=mr.sha
        ):
            raise RepositoryError("discovered merge request evidence is not exact")
        worktree = repository.worktree_for_existing_branch(
            record.issue_iid, record.source_branch, mr.sha
        )
        repository.verify_worktree(worktree, record.source_branch, mr.sha)
        if record.merge_request_draft is not None:
            mr = gitlab.reconcile_merge_request(
                mr, issue_iid=record.issue_iid, draft=record.merge_request_draft
            )
        if (
            mr.iid != candidate_iid
            or mr.web_url != candidate_url
            or mr.sha != candidate_sha
        ):
            raise RepositoryError("reconciled merge request identity changed")
        if not _mr_evidence_is_exact(
            record,
            mr,
            gitlab=gitlab,
            repository=repository,
            source_sha=candidate_sha,
        ):
            raise RepositoryError("reconciled merge request evidence is not exact")
        repository.verify_worktree(worktree, record.source_branch, candidate_sha)
    except GitLabError:
        return _retry_or_block_recovery_transport_error(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            summary="Published merge request reconciliation could not be verified.",
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    except RepositoryTransientError:
        return _retry_or_block_recovery_transport_error(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            summary="Published merge request worktree could not be verified.",
            error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
        )
    except RepositoryError as error:
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    observing = ledger.persist(
        transition_record(
            record,
            RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
            worktree=worktree,
            merge_request_iid=mr.iid,
            merge_request_url=mr.web_url,
            source_sha=mr.sha,
            intent=None,
            retry_count=0,
            retry_at=None,
            prior_state=None,
            prior_phase=None,
            error_code=None,
            summary="Published merge request recovered and verified.",
            next_action="Observe the exact source-SHA pipeline.",
        )
    )
    gitlab.transition_labels(record.issue_iid, label_for_state(observing.state))
    return _observe(
        observing, ledger=ledger, gitlab=gitlab, repository=repository, now=now
    )


def _prepare(
    record: LedgerRecord,
    issue: GitLabIssue,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    newly_claimed = record.revision == 0
    if newly_claimed:
        record = ledger.persist(
            record.model_copy(
                update={
                    "intent": TransitionIntentRecord(
                        operation_id=f"{record.issue_iid}:prepare:0",
                        kind="prepare_worktree",
                        expected_evidence={"branch": record.source_branch},
                    )
                }
            )
        )
        gitlab.transition_labels(issue.iid, label_for_state(record.state))
    try:
        if record.work_kind in {
            WorkKind.EXISTING_MR_REMEDIATION,
            WorkKind.MERGE_CONFLICT_REPAIR,
        }:
            if record.merge_request_iid is None or record.source_sha is None:
                raise SegmentError(
                    "remediation record lacks exact merge request evidence"
                )
            mr = gitlab.fetch_merge_request(record.merge_request_iid)
            if (
                mr.state != "opened"
                or mr.source_project_id != record.project_id
                or mr.target_project_id != record.project_id
                or mr.source_branch != record.source_branch
                or mr.target_branch != repository.config.default_branch
                or mr.sha != record.source_sha
            ):
                raise SegmentError("remediation merge request evidence does not match")
            worktree = repository.worktree_for_existing_branch(
                issue.iid, record.source_branch, record.source_sha
            )
        else:
            if newly_claimed:
                repository.refresh_base()
            worktree = repository.create_issue_worktree(
                issue.iid,
                record.source_branch,
                repository.config.default_branch,
                allow_existing_branch=not newly_claimed,
            )
        source_sha = repository.head(worktree)
    except (RepositoryTransientError, GitLabError) as error:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=(
                ErrorCode.GIT_TRANSPORT_RETRYABLE
                if isinstance(error, RepositoryTransientError)
                else ErrorCode.GITLAB_RETRYABLE
            ),
        )
    except (RepositoryError, SegmentError) as error:
        error_code = (
            ErrorCode.REMEDIATION_MR_MISMATCH
            if record.work_kind
            in {WorkKind.EXISTING_MR_REMEDIATION, WorkKind.MERGE_CONFLICT_REPAIR}
            else ErrorCode.BRANCH_OWNERSHIP_AMBIGUOUS
        )
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                intent=None,
                error_code=error_code,
                summary=str(error),
                next_action="Review the retained branch/worktree evidence and retry explicitly.",
            )
        )
        gitlab.transition_labels(issue.iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            issue.iid,
            error_code=str(blocked.error_code),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    updates: dict[str, object] = {
        "worktree": worktree,
        "source_sha": source_sha,
        "intent": None,
        "summary": "Durable worktree prepared.",
        "next_action": "Run the recorded workflow phase.",
    }
    if newly_claimed and record.work_kind is WorkKind.ORDINARY:
        # This is the first post-refresh worktree observation; do not use a
        # potentially stale base-checkout SHA captured during discovery.
        updates["main_sha"] = source_sha
    return ledger.persist(
        transition_record(
            record,
            RunnerState.EXECUTING,
            **updates,
        )
    )


def _execute(
    record: LedgerRecord,
    issue: GitLabIssue,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    invoke: InvokerPort,
    timeout_seconds: int,
    now: datetime,
    remaining_seconds: Callable[[], float] | None = None,
) -> LedgerRecord | None:
    try:
        _verify_authenticated_user(gitlab)
        issue_notes = gitlab.fetch_issue_notes(record.issue_iid)
        feedback = (
            tuple(
                item
                if isinstance(item, MergeRequestFeedback)
                else MergeRequestFeedback.model_validate(item)
                for item in gitlab.fetch_merge_request_feedback(
                    record.merge_request_iid
                )
            )
            if record.pending_feedback is not None
            and record.merge_request_iid is not None
            else ()
        )
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    worker_timeout = timeout_seconds
    if remaining_seconds is not None:
        remaining = remaining_seconds()
        if remaining <= 0:
            return None
        worker_timeout = max(1, math.ceil(remaining))
    try:
        prompt = build_worker_prompt(
            record.model_dump(mode="json"),
            project_id=record.project_id,
            issue_iid=record.issue_iid,
            effective_worker_budget_seconds=worker_timeout,
            issue_title=issue.title,
            issue_description=issue.description,
            issue_notes=issue_notes,
            merge_request_feedback=tuple(
                {
                    "id": note.id,
                    "source": note.source,
                    "discussion_id": note.discussion_id,
                    "author": note.author,
                    "created_at": note.created_at,
                    "body": note.body,
                    "unseen": True,
                }
                for note in feedback
                if _feedback_is_qualifying(note, repository.config.factory_username)
            ),
        )
    except ValueError as error:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary=f"Feedback selection cannot be rendered safely: {error}",
        )
    invocation = invoke.invoke(record.worktree, prompt, worker_timeout)
    if invocation.returncode != 0:
        worker_exit = (
            worker_exit_evidence(
                invocation.returncode, invocation.stderr, prompt=prompt
            )
            if invocation.returncode is not None
            else None
        )
        return _record_retry(
            record.model_copy(update={"worker_exit": worker_exit}),
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=(
                ErrorCode.WORKER_TIMEOUT
                if invocation.returncode is None
                else ErrorCode.WORKER_EXIT
            ),
            retry_delay=timedelta(),
        )
    else:
        try:
            result = parse_worker_result(
                invocation.stdout,
                expected_project_id=record.project_id,
                expected_issue_iid=record.issue_iid,
                expected_revision=record.revision,
            )
        except WorkerProtocolError as error:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                retry_delay=timedelta(),
                detail=format_worker_result_diagnostic(error),
            )
        else:
            if (
                result.expected_state is not record.state
                or result.expected_phase is not record.phase
            ):
                raise SegmentError("worker result does not match the recorded phase")
            if result.result_kind == "retryable":
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=result.error_code or ErrorCode.WORKER_EXIT,
                    retry_delay=timedelta(),
                )
            try:
                validate_worker_result(
                    record,
                    result,
                    changed_paths=repository.changed_paths(record.worktree),
                )
                outcome = apply_worker_result(record, result, now=now).record
            except RepositoryTransientError:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
                )
            except RepositoryError as error:
                return _block_publication_identity(
                    record, ledger=ledger, gitlab=gitlab, summary=str(error)
                )
            except (ValueError, WorkerProtocolError) as error:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.WORKER_RECEIPT_INVALID,
                    retry_delay=timedelta(),
                    detail=(
                        format_worker_result_diagnostic(error)
                        if isinstance(error, WorkerProtocolError)
                        else "receipts: required receipts are not coherent"
                    ),
                )
            else:
                outcome = outcome.model_copy(update={"worker_exit": None})
    if (
        record.state is RunnerState.REPAIR
        and record.pending_feedback is not None
        and not repository.changed_paths(record.worktree)
    ):
        is_noop = (
            result.result_kind == "ready_to_publish"
            and result.phase_status == "complete"
            and result.expected_state is record.state
            and result.expected_phase is record.phase
        )
        if is_noop:
            pending = record.pending_feedback.model_copy(
                update={
                    "outcome": result.change_summary,
                    "acknowledgement_ready": True,
                }
            )
            decided = ledger.persist(
                record.model_copy(
                    update={
                        "pending_feedback": pending,
                        "receipts": outcome.receipts,
                        "workflow_lane": outcome.workflow_lane,
                        "worker_exit": None,
                        "summary": "Feedback acknowledgement is pending retry.",
                    }
                )
            )
            return _acknowledge_feedback(
                decided, ledger=ledger, gitlab=gitlab, repository=repository, now=now
            )
    if (
        record.pending_feedback is not None
        and record.pending_feedback.outcome is None
        and result.result_kind == "ready_to_publish"
    ):
        outcome = outcome.model_copy(
            update={
                "pending_feedback": record.pending_feedback.model_copy(
                    update={"outcome": result.change_summary}
                )
            }
        )
    if (
        record.phase is WorkflowPhase.POST_PUBLICATION
        and outcome.state is RunnerState.AWAITING_HUMAN
        and outcome.pending_feedback is not None
        and outcome.pending_feedback.outcome is not None
    ):
        outcome = outcome.model_copy(
            update={
                "pending_feedback": outcome.pending_feedback.model_copy(
                    update={"acknowledgement_ready": True}
                )
            }
        )
    if record.pending_feedback is not None and outcome.source_sha != record.source_sha:
        if outcome.pending_feedback is None:
            raise SegmentError("feedback publication lost its pending acknowledgement")
        settled_cursor = record.pending_feedback.target_cursor.model_copy(
            update={"feedback_source_sha": outcome.source_sha}
        )
        outcome = outcome.model_copy(
            update={
                "pending_feedback": outcome.pending_feedback.model_copy(
                    update={"target_cursor": settled_cursor}
                )
            }
        )
    if outcome.state is RunnerState.AWAITING_HUMAN:
        if record.merge_request_iid is None or record.source_sha is None:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Post-publication handoff lacks exact merge request evidence.",
            )
        try:
            repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha
            )
            mr = gitlab.fetch_merge_request(record.merge_request_iid)
            exact = _mr_evidence_is_exact(
                record,
                mr,
                gitlab=gitlab,
                repository=repository,
                source_sha=record.source_sha,
            )
        except GitLabError:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GITLAB_RETRYABLE,
            )
        except RepositoryError as error:
            return _block_publication_identity(
                record, ledger=ledger, gitlab=gitlab, summary=str(error)
            )
        if not exact:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Post-publication merge request evidence is not exact.",
            )
    outcome = ledger.persist(outcome)
    if (
        record.phase is WorkflowPhase.POST_PUBLICATION
        and outcome.state is RunnerState.AWAITING_HUMAN
        and outcome.pending_feedback is not None
        and outcome.pending_feedback.acknowledgement_ready
    ):
        return _acknowledge_feedback(
            outcome, ledger=ledger, gitlab=gitlab, repository=repository, now=now
        )
    if outcome.state is RunnerState.DECOMPOSING:
        return _decompose(
            outcome, ledger=ledger, gitlab=gitlab, now=now, close_parent=False
        )
    gitlab.transition_labels(issue.iid, label_for_state(outcome.state))
    if outcome.state is RunnerState.BLOCKED:
        gitlab.comment_outcome(
            issue.iid,
            error_code=str(outcome.error_code or ErrorCode.STATE_RECONCILIATION_FAILED),
            summary=outcome.summary,
            next_action=outcome.next_action,
        )
    return outcome


def _publish(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    try:
        _verify_authenticated_user(gitlab)
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    draft = None if record.pending_feedback is not None else record.merge_request_draft
    baseline_sha = record.source_sha
    remediation = record.pending_feedback is not None or record.work_kind in {
        WorkKind.EXISTING_MR_REMEDIATION,
        WorkKind.MERGE_CONFLICT_REPAIR,
    }
    if baseline_sha is None:
        raise SegmentError("worker did not provide exact publication metadata")
    if not remediation and draft is None:
        if record.intent is not None:
            raise SegmentError("worker did not provide exact publication metadata")
        replay_phase = (
            WorkflowPhase.WORKFLOW
            if _has_workflow_receipt(record)
            else {
                WorkflowLane.CODING: WorkflowPhase.CODING_FINALIZE,
                WorkflowLane.CONTENT: WorkflowPhase.CONTENT_FINALIZE,
            }.get(record.workflow_lane)
        )
        if replay_phase is None:
            raise SegmentError("worker did not provide exact publication metadata")
        return ledger.persist(
            transition_record(
                record,
                RunnerState.EXECUTING,
                phase=replay_phase,
                summary="Publication metadata is missing; the workflow must be replayed.",
                next_action="Resume the workflow to produce exact merge request metadata.",
            )
        )
    replaying = record.intent is not None
    if replaying and not _publication_intent_is_exact(
        record, baseline_sha=baseline_sha
    ):
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Durable publication intent is not exact.",
        )
    if not replaying:
        protected = tuple(
            path
            for path in repository.changed_paths(record.worktree)
            if is_protected_policy_path(path)
        )
        if protected and (
            not has_complete_workflow_receipt(record, requires_policy_review=True)
            or (draft is not None and not _draft_handoff_is_valid(draft))
        ):
            blocked = ledger.persist(
                transition_record(
                    record,
                    RunnerState.BLOCKED,
                    error_code=ErrorCode.WORKER_RECEIPT_INVALID,
                    summary="Protected agent-policy publication evidence is incomplete.",
                    next_action=(
                        "Resume the workflow and return complete applicable receipts "
                        "and coherent merge request metadata."
                    ),
                )
            )
            gitlab.transition_labels(record.issue_iid, label_for_state(blocked.state))
            gitlab.comment_outcome(
                record.issue_iid,
                error_code=str(blocked.error_code),
                summary=blocked.summary,
                next_action=blocked.next_action,
            )
            return blocked
        record = ledger.persist(
            record.model_copy(
                update={
                    "intent": TransitionIntentRecord(
                        operation_id=f"{record.issue_iid}:publish:{record.revision}",
                        kind="publish_merge_request",
                        expected_evidence={
                            "branch": record.source_branch,
                            "baseline_sha": baseline_sha,
                        },
                    )
                }
            )
        )
    try:
        if record.publication_sha is None:
            if replaying:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Retained publication commit was not durably bound.",
                )
            prepared_sha = repository.prepare_commit(
                record.worktree,
                record.source_branch,
                record.issue_iid,
                expected_head=baseline_sha,
            )
            if record.intent is None:
                raise SegmentError("publication intent is missing")
            record = ledger.persist(
                record.model_copy(
                    update={
                        "publication_sha": prepared_sha,
                        "intent": record.intent.model_copy(
                            update={
                                "expected_evidence": record.intent.expected_evidence
                                | {"publication_sha": prepared_sha}
                            }
                        ),
                    }
                )
            )
        publication_sha = record.publication_sha
        if publication_sha is None:
            raise SegmentError("publication commit is not durably bound")
        reconciliation_sha = repository.reconcile_default_branch(
            record.worktree, record.source_branch, publication_sha
        )
        if reconciliation_sha is not None:
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.EXECUTING,
                    phase=WorkflowPhase.WORKFLOW,
                    work_kind=(
                        WorkKind.MERGE_CONFLICT_REPAIR
                        if record.merge_request_iid is not None
                        else record.work_kind
                    ),
                    source_sha=publication_sha,
                    intent=None,
                    publication_sha=None,
                    summary="Current default branch requires reconciliation before publication.",
                    next_action="Resolve the retained merge and rerun the full workflow.",
                )
            )
        already_published = False
        if remediation:
            _verify_authenticated_user(gitlab)
            if record.merge_request_iid is None:
                raise SegmentError(
                    "remediation publication lacks its exact merge request"
                )
            retained_mr = gitlab.fetch_merge_request(record.merge_request_iid)
            already_published = (
                record.publication_sha is not None
                and _mr_evidence_is_exact(
                    record,
                    retained_mr,
                    gitlab=gitlab,
                    repository=repository,
                    source_sha=publication_sha,
                )
            )
            expected_mr_sha = publication_sha if already_published else baseline_sha
            if not _mr_evidence_is_exact(
                record,
                retained_mr,
                gitlab=gitlab,
                repository=repository,
                source_sha=expected_mr_sha,
            ):
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Feedback publication MR evidence is not exact.",
                )
        if already_published:
            repository.verify_worktree(
                record.worktree, record.source_branch, publication_sha
            )
            sha = publication_sha
        else:
            sha = repository.push_prepared(
                record.worktree, record.source_branch, publication_sha
            )
        if remediation:
            if record.merge_request_iid is None:
                raise SegmentError(
                    "remediation publication lacks its exact merge request"
                )
            mr = gitlab.fetch_merge_request(record.merge_request_iid)
            if record.pending_feedback is not None:
                # Feedback must retain every user-owned MR field verbatim.
                pass
            elif draft is not None:
                mr = gitlab.reconcile_merge_request(
                    mr, issue_iid=record.issue_iid, draft=draft
                )
            else:
                mr = gitlab.ensure_closing_issue_reference(mr, record.issue_iid)
        else:
            if draft is None:
                raise SegmentError("new merge request lacks publication metadata")
            if replaying and record.merge_request_iid is not None:
                mr = gitlab.fetch_merge_request(record.merge_request_iid)
                mr = gitlab.reconcile_merge_request(
                    mr, issue_iid=record.issue_iid, draft=draft
                )
            else:
                mr = gitlab.find_merge_request_by_branch(record.source_branch)
                if mr is None:
                    mr = gitlab.create_merge_request(
                        branch=record.source_branch,
                        target=repository.config.default_branch,
                        issue_iid=record.issue_iid,
                        draft=draft,
                    )
                else:
                    mr = gitlab.reconcile_merge_request(
                        mr, issue_iid=record.issue_iid, draft=draft
                    )
        identity_exact = _mr_publication_identity_is_exact(
            record, mr, repository=repository, source_sha=sha
        )
        closes_issue = identity_exact and gitlab.mr_closes_issue(
            mr.iid, record.issue_iid
        )
    except PublicationError as error:
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                error_code=error.code,
                summary=f"{error.code}: {error.failure.summary}",
                next_action=error.failure.next_action,
                retry_at=None,
            )
        )
        gitlab.transition_labels(record.issue_iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            record.issue_iid,
            error_code=str(blocked.error_code),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except (RepositoryTransientError, GitLabError) as error:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=(
                ErrorCode.GIT_TRANSPORT_RETRYABLE
                if isinstance(error, RepositoryTransientError)
                else ErrorCode.GITLAB_RETRYABLE
            ),
        )
    except RepositoryError as error:
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                error_code=ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
                summary=str(error),
                next_action="Review the retained publication evidence and retry explicitly.",
            )
        )
        gitlab.transition_labels(record.issue_iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            record.issue_iid,
            error_code=str(blocked.error_code),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    if not identity_exact:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Published merge request evidence is not exact.",
        )
    if not closes_issue:
        return _record_retry(
            record.model_copy(
                update={
                    "merge_request_iid": mr.iid,
                    "merge_request_url": mr.web_url,
                }
            ),
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    pending = record.pending_feedback
    if pending is not None:
        pending = pending.model_copy(
            update={
                "publication_sha": sha,
                "target_cursor": pending.target_cursor.model_copy(
                    update={"feedback_source_sha": sha}
                ),
            }
        )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
            merge_request_iid=mr.iid,
            merge_request_url=mr.web_url,
            source_sha=sha,
            pending_feedback=pending,
            intent=None,
            summary="Merge request published and verified.",
            next_action="Observe the exact source-SHA pipeline.",
        )
    )


def _observe(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    if record.merge_request_iid is None or record.source_sha is None:
        raise SegmentError("observation record lacks merge request evidence")
    try:
        _verify_authenticated_user(gitlab)
        mr = gitlab.fetch_merge_request(record.merge_request_iid)
        exact = _mr_evidence_is_exact(
            record,
            mr,
            gitlab=gitlab,
            repository=repository,
            source_sha=record.source_sha,
        )
        if exact and mr.has_conflicts:
            repair_phase = (
                WorkflowPhase.WORKFLOW
                if _has_workflow_receipt(record)
                else WorkflowPhase.CODING_GREEN
            )
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.REPAIR,
                    phase=repair_phase,
                    work_kind=WorkKind.MERGE_CONFLICT_REPAIR,
                    error_code=ErrorCode.MERGE_CONFLICT,
                    summary="The exact merge request has conflicts.",
                    next_action="Run the bounded merge-conflict repair segment.",
                )
            )
        pipeline = gitlab.latest_pipeline_for_sha(
            record.merge_request_iid, record.source_sha
        )
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    if not exact:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Observed merge request evidence is not exact.",
        )
    if pipeline is None or pipeline.status in {
        "created",
        "waiting_for_resource",
        "preparing",
        "scheduled",
        "pending",
        "running",
    }:
        return record
    return ledger.persist(
        transition_record(
            record,
            RunnerState.EXECUTING,
            phase=WorkflowPhase.POST_PUBLICATION,
            error_code=None,
            summary="The exact source-SHA pipeline reached a terminal state.",
            next_action="Run the fresh post-publication pipeline assessment.",
        )
    )


def run_segment(
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    invoke: InvokerPort,
    timeout_seconds: int,
    now: datetime,
    issue_iid: int | None = None,
    remaining_seconds: Callable[[], float] | None = None,
) -> LedgerRecord | None:
    """Run at most one recoverable segment and return its durable state."""
    record, issue = select_record(
        ledger, gitlab, now=now, issue_iid=issue_iid, repository=repository
    )
    if record is None or issue is None:
        return None
    if (
        record.state is RunnerState.BLOCKED
        and record.error_code is ErrorCode.RETRY_EXHAUSTED
        and record.prior_state is RunnerState.AWAITING_HUMAN
        and record.merge_request_iid is not None
    ):
        return record
    retryable_worker_errors = {
        ErrorCode.WORKER_EXIT,
        ErrorCode.WORKER_TIMEOUT,
        ErrorCode.WORKER_PROTOCOL_INVALID,
        ErrorCode.WORKER_RECEIPT_INVALID,
    }
    reexecute_due_worker_retry = (
        record.state is RunnerState.RETRY_WAIT
        and record.error_code in retryable_worker_errors
    )
    resumed_from_retry = record.state is RunnerState.RETRY_WAIT and _due(record, now)
    if resumed_from_retry:
        record = _resume_due_retry(record, ledger=ledger)
    try:
        _verify_authenticated_user(gitlab)
    except GitLabIdentityError as error:
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
            allow_remote_exhaustion_side_effects=False,
        )
    if (
        record.pending_feedback is not None
        and record.pending_feedback.acknowledgement_ready
    ):
        return _acknowledge_feedback(
            record, ledger=ledger, gitlab=gitlab, repository=repository, now=now
        )
    if (
        record.state is RunnerState.REPAIR
        and record.pending_feedback is not None
        and record.summary == "Unseen human merge-request feedback selected."
    ):
        return ledger.persist(
            record.model_copy(
                update={"summary": "Feedback workflow is ready to execute."}
            )
        )
    if issue.state != "opened":
        if record.state is RunnerState.COMPLETE:
            gitlab.transition_labels(issue.iid, None)
            return record
        complete = ledger.persist(
            transition_record(
                record,
                RunnerState.COMPLETE,
                intent=None,
                summary="Issue is closed; local record reconciled.",
                next_action="No action required.",
            )
        )
        gitlab.transition_labels(issue.iid, None)
        return complete
    if not _due(record, now):
        return None
    if (
        record.pending_feedback is not None
        and record.pending_feedback.outcome is None
        and record.source_sha != record.pending_feedback.source_sha
    ):
        record = _synchronize_pending_feedback(
            record, ledger=ledger, gitlab=gitlab, repository=repository, now=now
        )
        if record.state in {RunnerState.BLOCKED, RunnerState.RETRY_WAIT}:
            return record
    if record.state is RunnerState.BLOCKED and record.summary.startswith(
        "Feedback merge request"
    ):
        return record
    if _can_reconcile_published_ordinary_work(record):
        reconciled = _reconcile_published_ordinary_work(
            record, ledger=ledger, gitlab=gitlab, repository=repository, now=now
        )
        if reconciled is not None:
            return reconciled
    if _is_fresh_ordinary_preparation(record):
        blocked = _block_fresh_preparation_with_published_mr(
            record, ledger=ledger, gitlab=gitlab, now=now
        )
        if blocked is not None:
            return blocked
    if record.revision == 0 and record.state is RunnerState.BLOCKED:
        blocked = ledger.persist(record)
        gitlab.transition_labels(issue.iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            issue.iid,
            error_code=str(blocked.error_code),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    if record.state in ACTIVE_STATES - {
        RunnerState.PREPARING,
        RunnerState.RETRY_WAIT,
    } and not repository.worktree_exists(record.worktree):
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                error_code=ErrorCode.WORKTREE_MISSING,
                summary="The durable worktree is missing.",
                next_action="Review the recorded branch and commit before explicit recovery.",
            )
        )
        gitlab.transition_labels(issue.iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            issue.iid,
            error_code=str(blocked.error_code),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
    if record.state in {
        RunnerState.EXECUTING,
        RunnerState.OBSERVING,
        RunnerState.REPAIR,
    }:
        try:
            if record.source_sha is None:
                raise RepositoryError("durable worktree baseline is not recorded")
            repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha
            )
        except RepositoryError as error:
            blocked = ledger.persist(
                transition_record(
                    record,
                    RunnerState.BLOCKED,
                    error_code=ErrorCode.WORKTREE_OWNERSHIP_AMBIGUOUS,
                    summary=str(error),
                    next_action="Review the retained worktree and branch before recovery.",
                )
            )
            gitlab.transition_labels(issue.iid, label_for_state(blocked.state))
            gitlab.comment_outcome(
                issue.iid,
                error_code=str(blocked.error_code),
                summary=blocked.summary,
                next_action=blocked.next_action,
            )
            return blocked
    expected_label = label_for_state(record.state)
    lifecycle = set(LIFECYCLE_LABELS)
    if record.revision > 0 and set(issue.labels).intersection(lifecycle) != (
        {expected_label} if expected_label else set()
    ):
        issue = gitlab.transition_labels(issue.iid, expected_label)
    if record.state not in ACTIVE_STATES:
        return record
    if resumed_from_retry:
        if record.state is RunnerState.DECOMPOSING:
            return _decompose(record, ledger=ledger, gitlab=gitlab, now=now)
        if not reexecute_due_worker_retry:
            return record
    if record.state is RunnerState.PREPARING:
        return _prepare(
            record,
            issue,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
    if record.state in {RunnerState.EXECUTING, RunnerState.REPAIR}:
        return _execute(
            record,
            issue,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=invoke,
            timeout_seconds=timeout_seconds,
            now=now,
            remaining_seconds=remaining_seconds,
        )
    if record.state is RunnerState.DECOMPOSING:
        return _decompose(record, ledger=ledger, gitlab=gitlab, now=now)
    if record.state is RunnerState.PUBLISHING:
        return _publish(
            record,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
    if record.state is RunnerState.OBSERVING:
        return _observe(
            record,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
    raise SegmentError("record is not runnable")
