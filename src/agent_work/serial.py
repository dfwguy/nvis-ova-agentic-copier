"""One bounded, recoverable state-machine segment."""

from __future__ import annotations

from datetime import datetime
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
)
from .gitlab import GitLabError
from .models import (
    GitLabIssue,
    LedgerRecord,
    MergeRequestEvidence,
    TransitionIntentRecord,
)
from .ports import GitLabPort, InvokerPort, LedgerPort, RepositoryPort
from .repository import RepositoryError, RepositoryTransientError
from .state_machine import apply_worker_result, plan_retry, transition_record
from .worker import build_worker_prompt, parse_worker_result


class SegmentError(RuntimeError):
    """A segment could not safely reconcile or record its outcome."""


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
        phase=WorkflowPhase.DISCOVERY,
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


def select_record(
    ledger: LedgerPort, gitlab: GitLabPort, *, now: datetime
) -> tuple[LedgerRecord | None, GitLabIssue | None]:
    """Reconcile durable work before selecting the oldest eligible issue."""
    project = gitlab.verify_project()
    records = ledger.list_records()
    if any(record.project_id != project.id for record in records):
        raise SegmentError("ledger record belongs to another project")
    active = tuple(record for record in records if record.state in ACTIVE_STATES)
    if len(active) > 1:
        raise SegmentError("multiple resumable records require reconciliation")
    issues = {
        record.issue_iid: gitlab.fetch_issue(record.issue_iid) for record in records
    }
    for record in records:
        issue = issues[record.issue_iid]
        if issue.state != "opened" and record.state is not RunnerState.COMPLETE:
            return record, issue
    if active:
        record = active[0]
        if not _due(record, now):
            return None, None
        return record, issues[record.issue_iid]
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
) -> LedgerRecord:
    """Persist one bounded retry and project exhaustion as an actionable block."""
    outcome = plan_retry(record, now=now).record
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
    blocked = ledger.persist(
        transition_record(
            record,
            RunnerState.BLOCKED,
            error_code=ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
            summary=summary,
            next_action="Review the exact MR, branch, target, SHA, and closing evidence.",
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
    if record.work_kind is WorkKind.PROTECTED_POLICY_MANUAL:
        blocked = ledger.persist(
            transition_record(
                record,
                RunnerState.BLOCKED,
                intent=None,
                error_code=ErrorCode.PROTECTED_POLICY_MANUAL,
                summary="Protected agent-policy work requires a human-directed lane.",
                next_action="Run the documented policy workflow manually.",
            )
        )
        gitlab.transition_labels(issue.iid, label_for_state(blocked.state))
        gitlab.comment_outcome(
            issue.iid,
            error_code=str(ErrorCode.PROTECTED_POLICY_MANUAL),
            summary=blocked.summary,
            next_action=blocked.next_action,
        )
        return blocked
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
) -> LedgerRecord:
    prompt = build_worker_prompt(
        record.model_dump(mode="json"),
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        issue_title=issue.title,
        issue_description=issue.description,
    )
    invocation = invoke.invoke(record.worktree, prompt, timeout_seconds)
    if invocation.returncode != 0:
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=(
                ErrorCode.WORKER_TIMEOUT
                if invocation.returncode is None
                else ErrorCode.WORKER_EXIT
            ),
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
            outcome = plan_retry(record, now=now).record.model_copy(
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
        finalizer = {
            WorkflowLane.CODING: WorkflowPhase.CODING_FINALIZE,
            WorkflowLane.CONTENT: WorkflowPhase.CONTENT_FINALIZE,
        }.get(record.workflow_lane)
        if finalizer is None:
            raise SegmentError("worker did not provide exact publication metadata")
        return ledger.persist(
            transition_record(
                record,
                RunnerState.EXECUTING,
                phase=finalizer,
                summary="Publication metadata is missing; finalization must be replayed.",
                next_action="Rerun the lane finalizer to produce exact merge request metadata.",
            )
        )
    replaying = record.intent is not None
    if not replaying:
        protected = tuple(
            path
            for path in repository.changed_paths(record.worktree)
            if is_protected_policy_path(path)
        )
        if protected:
            blocked = ledger.persist(
                transition_record(
                    record,
                    RunnerState.BLOCKED,
                    error_code=ErrorCode.PROTECTED_POLICY_MANUAL,
                    summary="Actual changes include protected agent-policy paths.",
                    next_action="Publish these changes through a human-directed policy lane.",
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
        sha = repository.commit_and_push(
            record.worktree,
            record.source_branch,
            record.issue_iid,
            expected_head=baseline_sha,
            allow_replay=replaying,
        )
        if remediation:
            if record.merge_request_iid is None:
                raise SegmentError(
                    "remediation publication lacks its exact merge request"
                )
            mr = gitlab.fetch_merge_request(record.merge_request_iid)
            mr = gitlab.ensure_closing_issue_reference(mr, record.issue_iid)
        else:
            if draft is None:
                raise SegmentError("new merge request lacks publication metadata")
            if replaying and record.merge_request_iid is not None:
                mr = gitlab.fetch_merge_request(record.merge_request_iid)
                mr = gitlab.ensure_closing_issue_reference(mr, record.issue_iid)
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
                    mr = gitlab.ensure_closing_issue_reference(mr, record.issue_iid)
        identity_exact = _mr_publication_identity_is_exact(
            record, mr, repository=repository, source_sha=sha
        )
        closes_issue = identity_exact and gitlab.mr_closes_issue(
            mr.iid, record.issue_iid
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
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.REPAIR,
                    phase=WorkflowPhase.CODING_GREEN,
                    work_kind=WorkKind.MERGE_CONFLICT_REPAIR,
                    error_code=ErrorCode.MERGE_CONFLICT,
                    summary="The exact merge request has conflicts.",
                    next_action="Run one bounded merge-conflict repair segment.",
                )
            )
        pipeline = gitlab.latest_pipeline_for_sha(
            record.merge_request_iid, record.source_sha
        )
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
    if pipeline.status not in {"success", "skipped", "manual"}:
        return ledger.persist(
            transition_record(
                record,
                RunnerState.REPAIR,
                phase=WorkflowPhase.CODING_GREEN,
                error_code=ErrorCode.PIPELINE_REJECTED,
                summary="The exact source-SHA pipeline needs repair.",
                next_action="Run one bounded post-publication repair segment.",
            )
        )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.EXECUTING,
            phase=WorkflowPhase.POST_PUBLICATION,
            summary="The exact source-SHA pipeline completed.",
            next_action="Run post-publication review and triage.",
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
) -> LedgerRecord | None:
    """Run at most one recoverable segment and return its durable state."""
    record, issue = select_record(ledger, gitlab, now=now)
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
        return ledger.persist(
            transition_record(
                record,
                record.prior_state or RunnerState.EXECUTING,
                phase=record.prior_phase or record.phase,
                retry_at=None,
            )
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
        )
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
