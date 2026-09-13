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
from .gitlab import GitLabError
from .models import (
    GitLabIssue,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
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
from .worker import build_worker_prompt, parse_worker_result, worker_exit_evidence


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
    except GitLabError:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
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
    issue: GitLabIssue, *, project_id: int, state_root: Path
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
    if record.work_kind is not WorkKind.ORDINARY or record.revision == 0:
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


def select_record(
    ledger: LedgerPort,
    gitlab: GitLabPort,
    *,
    now: datetime,
    issue_iid: int | None = None,
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
    for reconciliable in (
        record for record in records if _can_reconcile_published_ordinary_work(record)
    ):
        try:
            if gitlab.open_merge_requests_by_branch(reconciliable.source_branch):
                return reconciliable, issues[reconciliable.issue_iid]
        except GitLabError:
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
        _new_record(issue, project_id=project.id, state_root=ledger.state_root),
        issue,
    )


def _record_retry(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    error_code: ErrorCode,
    retry_delay: timedelta = timedelta(minutes=20),
) -> LedgerRecord:
    """Persist one bounded retry and project exhaustion as an actionable block."""
    outcome = plan_retry(record, now=now, retry_delay=retry_delay).record
    if outcome.state is RunnerState.RETRY_WAIT:
        outcome = outcome.model_copy(update={"error_code": error_code})
    outcome = ledger.persist(outcome)
    if outcome.state is RunnerState.BLOCKED:
        gitlab.transition_labels(record.issue_iid, label_for_state(outcome.state))
        gitlab.comment_outcome(
            record.issue_iid,
            error_code=str(outcome.error_code),
            summary=outcome.summary,
            next_action=outcome.next_action,
        )
    return outcome


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
    if record.state is RunnerState.RETRY_WAIT:
        record = transition_record(
            record,
            record.prior_state or RunnerState.EXECUTING,
            phase=record.prior_phase or record.phase,
            retry_at=None,
        )
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
    return ledger.persist(
        transition_record(
            record,
            RunnerState.EXECUTING,
            worktree=worktree,
            source_sha=source_sha,
            intent=None,
            summary="Durable worktree prepared.",
            next_action="Run the recorded workflow phase.",
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
        issue_notes = gitlab.fetch_issue_notes(record.issue_iid)
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
    prompt = build_worker_prompt(
        record.model_dump(mode="json"),
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        effective_worker_budget_seconds=worker_timeout,
        issue_title=issue.title,
        issue_description=issue.description,
        issue_notes=issue_notes,
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
        except ValueError:
            outcome = plan_retry(
                record, now=now, retry_delay=timedelta()
            ).record.model_copy(
                update={"error_code": ErrorCode.WORKER_PROTOCOL_INVALID}
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
                outcome = apply_worker_result(record, result, now=now).record
            except ValueError:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.WORKER_RECEIPT_INVALID,
                    retry_delay=timedelta(),
                )
            else:
                outcome = outcome.model_copy(update={"worker_exit": None})
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
    draft = record.merge_request_draft
    baseline_sha = record.source_sha
    remediation = record.work_kind in {
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
        sha = repository.push_prepared(
            record.worktree, record.source_branch, publication_sha
        )
        if remediation:
            if record.merge_request_iid is None:
                raise SegmentError(
                    "remediation publication lacks its exact merge request"
                )
            mr = gitlab.fetch_merge_request(record.merge_request_iid)
            if draft is not None:
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
    return ledger.persist(
        transition_record(
            record,
            RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
            merge_request_iid=mr.iid,
            merge_request_url=mr.web_url,
            source_sha=sha,
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
    repair_phase = (
        WorkflowPhase.WORKFLOW
        if _has_workflow_receipt(record)
        else WorkflowPhase.CODING_GREEN
    )
    if record.merge_request_iid is None or record.source_sha is None:
        raise SegmentError("observation record lacks merge request evidence")
    try:
        mr = gitlab.fetch_merge_request(record.merge_request_iid)
        exact = _mr_evidence_is_exact(
            record,
            mr,
            gitlab=gitlab,
            repository=repository,
            source_sha=record.source_sha,
        )
        if exact and mr.has_conflicts:
            reconciliation_sha = repository.reconcile_default_branch(
                record.worktree, record.source_branch, record.source_sha
            )
            if reconciliation_sha is None:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.MERGE_CONFLICT,
                )
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.EXECUTING,
                    phase=WorkflowPhase.WORKFLOW,
                    work_kind=WorkKind.MERGE_CONFLICT_REPAIR,
                    error_code=ErrorCode.MERGE_CONFLICT,
                    intent=None,
                    publication_sha=None,
                    summary=(
                        "The exact merge request has conflicts; its retained worktree "
                        "requires reconciliation."
                    ),
                    next_action="Resolve the retained merge and rerun the full workflow.",
                )
            )
        pipeline = gitlab.latest_pipeline_for_sha(
            record.merge_request_iid, record.source_sha
        )
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
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=str(error)
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
    if pipeline.status not in {"success", "skipped", "manual"}:
        return ledger.persist(
            transition_record(
                record,
                RunnerState.REPAIR,
                phase=repair_phase,
                error_code=ErrorCode.PIPELINE_REJECTED,
                summary="The exact source-SHA pipeline needs repair.",
                next_action="Run one bounded post-publication repair segment.",
            )
        )
    workflow_complete = _has_workflow_receipt(record)
    return ledger.persist(
        transition_record(
            record,
            (
                RunnerState.AWAITING_HUMAN
                if workflow_complete
                else RunnerState.EXECUTING
            ),
            phase=(
                WorkflowPhase.PIPELINE_WAIT
                if workflow_complete
                else WorkflowPhase.POST_PUBLICATION
            ),
            summary="The exact source-SHA pipeline completed.",
            next_action=(
                "Review and merge the verified merge request."
                if workflow_complete
                else "Run post-publication review and triage."
            ),
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
    record, issue = select_record(ledger, gitlab, now=now, issue_iid=issue_iid)
    if record is None or issue is None:
        return None
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
    if record.state is RunnerState.PREPARING:
        return _prepare(
            record,
            issue,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
    if record.state is RunnerState.RETRY_WAIT:
        resumed = ledger.persist(
            transition_record(
                record,
                record.prior_state or RunnerState.EXECUTING,
                phase=record.prior_phase or record.phase,
                retry_at=None,
            )
        )
        if resumed.state is RunnerState.DECOMPOSING:
            return _decompose(resumed, ledger=ledger, gitlab=gitlab, now=now)
        return resumed
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
