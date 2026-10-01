# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Composition root and command dispatcher for agent-work."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Protocol

from . import approved_scope, models, test_executor
from .classification import ClassificationError, classify_work
from .domain import (
    ACTIVE_STATES,
    IMMEDIATE_OWNER_STATES,
    LIFECYCLE_LABELS,
    MANUAL_TRACKER_LABEL,
    NONTERMINAL_PIPELINE_STATUSES,
    CommandRequest,
    ErrorCode,
    PrerequisiteLabelProjection,
    RedCheckpointFailureStage,
    RepairAttemptEvidence,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    prerequisite_dependency_proof,
    prerequisite_label_iid,
    prerequisite_label_reconciliation,
    prerequisite_projection_inventory,
    retries_remaining,
)
from .gitlab import GitLabError, GitLabGateway
from .human_recovery import (
    EvidenceDriftError,
    RecoveryCandidate,
    RecoveryClassification,
    RecoveryObservation,
    RecoveryOwnerCapacity,
    ResolutionChoice,
    ResolutionRequest,
    build_inventory,
    build_resolution_plan,
    classify_recovery_owner_capacity,
    verify_resolution_plan,
)
from .ledger import (
    JsonLedger,
    LedgerError,
    XdgIssueLock,
    XdgProjectLock,
    observe_issue_lock,
    observe_project_lock,
)
from .migration import build_migration_plan
from .models import (
    AgentWorkConfig,
    CommandItemReport,
    CommandReport,
    ConditionEvidence,
    GitLabIssue,
    GitLabProject,
    InterruptedClaimReceipt,
    LedgerRecord,
    MergeRequestEvidence,
    MigrationAction,
    MigrationObservation,
    MigrationPlan,
    OperationalPlan,
    PipelineEvidence,
    PrerequisiteLabelProjectionReport,
    PublicationCorrectionIdentity,
    RecoveryCandidateReport,
    RecoveryChildState,
    RecoverySummary,
    StageTimingIdentity,
    TransitionIntentRecord,
    ValidationAdmissionReport,
)
from .ports import (
    CommandResult,
    CommandRunner,
    GitLabPort,
    InvokerPort,
    LedgerPort,
    ProjectLockFactory,
    RepositoryPort,
)
from .red_checkpoint import (
    RedCheckpointDependencies,
    RedCheckpointError,
    RedCheckpointEvidence,
    RedCheckpointRequest,
    RedCheckpointStore,
    create_red_checkpoint,
)
from .repository import (
    CommandResult as RepositoryCommandResult,
)
from .repository import (
    GitRepository,
    LegacyPublicationCandidate,
    RepositoryError,
    RepositoryTransientError,
    WorktreeStatus,
    discover_config,
    run_subprocess,
)
from .serial import (
    AUTOMATIC_REPAIR_MAX_ATTEMPTS,
    SegmentError,
    activate_exact_pipeline_repair,
    automatic_blocked_static_rejection,
    build_authored_candidate_correction_identity,
    build_publication_correction_identity,
    collect_exact_failed_job_diagnostics,
    correction_identity_matches,
    new_record,
    read_committed_candidate,
    reconcile_closed_issues,
    record_failed_job_diagnostic_unavailable,
    resume_candidates,
    retained_precheckpoint_failure_is_untouched,
    run_segment,
)
from .state_machine import retained_ordinary_is_untouched, transition_record
from .status import project_status
from .timing import is_bounded_timing_append, stage_timing_identity, stage_timing_report
from .timing_admission import (
    InteractiveValidationRequest,
    mark_interactive_validation_yield,
    mark_validation_admission_resolved,
    mark_validation_admission_resumed,
    observe_waiting_interactive_validation,
    read_validation_admission,
)
from .worker import AgentInvoker, is_safe_worker_result_diagnostic

# Public host-embedding API, owned and implemented by test_executor.
OpenedTestAsset = test_executor.OpenedTestAsset
PreparedTestExecution = test_executor.PreparedTestExecution
TestExecutionError = test_executor.TestExecutionError
TestExecutionIdentity = test_executor.TestExecutionIdentity
TestExecutionPolicy = test_executor.TestExecutionPolicy
TestExecutionRequest = test_executor.TestExecutionRequest
TestExecutorPorts = test_executor.TestExecutorPorts
TestProtectedPaths = test_executor.TestProtectedPaths
build_test_execution_probe = test_executor.build_test_execution_probe
create_test_execution_policy = test_executor.create_test_execution_policy
parse_test_execution_probe = test_executor.parse_test_execution_probe


class RecoveryIneligibleError(RuntimeError):
    """The retry predicate rejected complete, current recovery evidence."""


_AUTOMATIC_BLOCKED_RETRY_REASON = "Automatic blocked retry continuation."


def _item(
    record: LedgerRecord, *, corroboration: dict[str, str] | None = None
) -> CommandItemReport:
    return CommandItemReport(
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        projected_status=project_status(
            state=record.state, phase=record.phase, source_sha=record.source_sha
        ),
        state=record.state,
        phase=record.phase,
        work_kind=record.work_kind,
        attempt=record.retry_count + 1,
        retries_remaining=retries_remaining(record.retry_count),
        retry_at=record.retry_at,
        automatic_repair_count=record.automatic_repair_count,
        automatic_repair_at=record.automatic_repair_at,
        source_branch=record.source_branch,
        source_sha=record.source_sha,
        main_branch=record.main_branch,
        main_sha=record.main_sha,
        publication_sha=record.publication_sha,
        worktree=record.worktree,
        merge_request_iid=record.merge_request_iid,
        merge_request_url=record.merge_request_url,
        error_code=record.error_code,
        worker_exit=record.worker_exit,
        audit_evidence=record.audit_evidence,
        condition_history=record.condition_history,
        phase_timings=record.phase_timings,
        stage_timing_report=stage_timing_report(
            record, expected_identity=stage_timing_identity(record)
        ),
        current_condition=_current_condition(record),
        activation_started_at=record.activation_started_at,
        latest_meaningful_update_at=record.latest_meaningful_update_at,
        completed_at=record.completed_at,
        issue_title=record.issue_title,
        corroboration=corroboration or {},
        summary=record.summary,
        next_action=record.next_action,
    )


def _current_condition(record: LedgerRecord) -> ConditionEvidence | None:
    """Expose a bounded current blocker even for legacy records without a code."""
    if record.state not in {RunnerState.BLOCKED, RunnerState.FAILED}:
        return None
    code = (
        record.error_code.value
        if record.error_code is not None
        else f"{record.state.value}_condition"
    )
    return ConditionEvidence(
        code=code,
        detail=record.summary[:2_048] or "Condition detail unavailable.",
    )


def _status_corroboration(
    record: LedgerRecord,
    *,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    clock: Callable[[], datetime],
    process_probe: Callable[[], str],
    issue_owner_probe: Callable[[int], str],
) -> dict[str, str]:
    """Read bounded live evidence independently; status never mutates state."""
    evidence: dict[str, str] = {
        "observed_at": clock().isoformat(),
        "gitlab": "unknown",
        "worktree": "unknown",
        "process": "unknown",
    }
    try:
        issue = gitlab.fetch_issue(record.issue_iid)
        evidence["issue_title"] = (
            "unknown" if record.issue_title is None else "confirmed"
        )
        evidence["gitlab"] = (
            "confirmed"
            if (
                issue.iid == record.issue_iid
                and (record.issue_title is None or issue.title == record.issue_title)
                and issue.state
                == ("closed" if record.state is RunnerState.COMPLETE else "opened")
            )
            else "mismatch"
        )
        if record.issue_title is not None and issue.title != record.issue_title:
            evidence["issue_title"] = "mismatch"
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        exists = repository.worktree_exists(record.worktree)
    except (OSError, RepositoryError):
        exists = None
    if exists is False:
        evidence["worktree"] = "missing"
    elif exists is True:
        try:
            evidence["worktree_head"] = repository.head(record.worktree)
            repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha or ""
            )
            changed_paths = repository.changed_paths(record.worktree)
            evidence["worktree_dirty"] = str(bool(changed_paths)).lower()
            evidence["worktree_changed_paths"] = ",".join(changed_paths[:128])
            evidence["worktree"] = "confirmed"
        except RepositoryTransientError:
            pass
        except RepositoryError:
            evidence["worktree"] = "mismatch"
        except (OSError, RuntimeError, ValueError):
            pass
    try:
        evidence["process"] = process_probe()
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        evidence["owner_status"] = issue_owner_probe(record.issue_iid)
    except (OSError, RuntimeError, ValueError):
        evidence["owner_status"] = "unknown"
    return evidence


def _report(
    request: CommandRequest,
    *,
    applied: bool,
    outcome: str,
    records: tuple[LedgerRecord, ...] = (),
    actions: tuple[MigrationAction, ...] = (),
    project: dict[str, str | int] | None = None,
    segments_run: int | None = None,
    stop_reason: str | None = None,
    summary: str | None = None,
    next_action: str | None = None,
    operational_plan: OperationalPlan | None = None,
    prerequisite_projections: tuple[PrerequisiteLabelProjectionReport, ...] = (),
    validation_admission: ValidationAdmissionReport | None = None,
) -> CommandReport:
    return CommandReport(
        command=request.command,
        applied=applied,
        outcome=outcome,
        project=project,
        items=tuple(_item(record) for record in records),
        actions=actions,
        segments_run=segments_run,
        stop_reason=stop_reason,
        summary=summary,
        next_action=next_action,
        operational_plan=operational_plan,
        prerequisite_projections=prerequisite_projections,
        validation_admission=validation_admission,
    )


def _prerequisite_projections(
    issues: tuple[GitLabIssue, ...], *, project_id: int
) -> tuple[PrerequisiteLabelProjectionReport, ...]:
    """Convert one validated GitLab snapshot into dynamic-label report evidence."""
    return _prerequisite_projection_reports(
        prerequisite_projection_inventory(issues, project_id=project_id)
    )


def _prerequisite_projection_reports(
    projections: tuple[PrerequisiteLabelProjection, ...],
) -> tuple[PrerequisiteLabelProjectionReport, ...]:
    """Convert validated dynamic projections into bounded report evidence."""
    return tuple(
        PrerequisiteLabelProjectionReport(
            issue_iid=item.issue_iid,
            current_labels=tuple(
                label
                for label in item.current_labels
                if prerequisite_label_iid(label) is not None
            ),
            intended_labels=tuple(
                label
                for label in item.intended_labels
                if prerequisite_label_iid(label) is not None
            ),
        )
        for item in projections
    )


def _operational_evidence(
    *,
    config: AgentWorkConfig,
    project: GitLabProject,
    identity: str,
    records: tuple[LedgerRecord, ...],
    target_iid: int | None,
    action: str,
    command: str,
    max_segments: int,
    max_activation_seconds: int,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    lifecycle_label_drift: tuple[str, ...],
    owner_states: tuple[tuple[int, str], ...] = (),
    extra_evidence: Mapping[str, str] | None = None,
) -> OperationalPlan:
    """Build compact public evidence for a reviewed non-claiming operation."""
    selected = next(
        (record for record in records if record.issue_iid == target_iid), None
    )
    evidence: dict[str, str | int | bool | None] = {
        "record_iids": ",".join(str(record.issue_iid) for record in records),
        "max_segments": max_segments,
        "max_activation_seconds": max_activation_seconds,
        "target_present": selected is not None,
        "record_revision": None if selected is None else selected.revision,
        "record_state": None if selected is None else selected.state.value,
        "record_phase": None if selected is None else selected.phase.value,
        "retry_count": None if selected is None else selected.retry_count,
        "retry_at": None
        if selected is None or selected.retry_at is None
        else selected.retry_at.isoformat(),
        "automatic_repair_count": None
        if selected is None
        else selected.automatic_repair_count,
        "automatic_repair_at": None
        if selected is None or selected.automatic_repair_at is None
        else selected.automatic_repair_at.isoformat(),
        "source_branch": None if selected is None else selected.source_branch,
        "source_sha": None if selected is None else selected.source_sha,
        "merge_request_iid": None if selected is None else selected.merge_request_iid,
        "publication_sha": None if selected is None else selected.publication_sha,
        "has_draft": None
        if selected is None
        else selected.merge_request_draft is not None,
        "has_feedback": None
        if selected is None
        else selected.pending_feedback is not None,
        "has_cursor": None
        if selected is None
        else selected.feedback_cursor is not None,
        "has_receipts": None if selected is None else bool(selected.receipts),
        "record_fingerprint": None
        if selected is None
        else hashlib.sha256(selected.model_dump_json().encode()).hexdigest(),
        "lifecycle_label_drift": ",".join(lifecycle_label_drift),
        "owner_states": ",".join(
            f"{issue_iid}={status}" for issue_iid, status in owner_states
        ),
    }
    if extra_evidence is not None:
        evidence.update(extra_evidence)
    if selected is not None:
        try:
            issue = gitlab.fetch_issue(selected.issue_iid)
            evidence["issue_state"] = issue.state
            evidence["lifecycle_labels"] = ",".join(
                sorted(set(issue.labels).intersection(LIFECYCLE_LABELS))
            )
            exists = repository.worktree_exists(selected.worktree)
            evidence["worktree_exists"] = exists
            if exists:
                evidence["worktree_head"] = repository.head(selected.worktree)
                evidence["worktree_clean"] = not repository.has_changes(
                    selected.worktree
                ) and not repository.changed_paths(selected.worktree)
                try:
                    repository.verify_worktree(
                        selected.worktree,
                        selected.source_branch,
                        selected.source_sha or "",
                    )
                    evidence["worktree_binding"] = "exact"
                except (OSError, RepositoryError, RuntimeError, ValueError):
                    evidence["worktree_binding"] = "mismatch"
            branch_mrs = gitlab.open_merge_requests_by_branch(selected.source_branch)
            evidence["open_branch_mr_iids"] = ",".join(str(mr.iid) for mr in branch_mrs)
            evidence["mr_presence"] = (
                "recorded" if selected.merge_request_iid is not None else "absent"
            )
            if selected.merge_request_iid is not None:
                mr = gitlab.fetch_merge_request(selected.merge_request_iid)
                pipeline = gitlab.latest_pipeline_for_sha(mr.iid, mr.sha)
                evidence["mr_state"] = mr.state
                evidence["mr_iid"] = mr.iid
                evidence["mr_sha"] = mr.sha
                evidence["mr_source_branch"] = mr.source_branch
                evidence["mr_target_branch"] = mr.target_branch
                evidence["mr_source_project"] = mr.source_project_id
                evidence["mr_target_project"] = mr.target_project_id
                evidence["mr_has_conflicts"] = mr.has_conflicts
                evidence["mr_closes_issue"] = gitlab.mr_closes_issue(
                    mr.iid, selected.issue_iid
                )
                evidence["pipeline_id"] = None if pipeline is None else pipeline.id
                evidence["pipeline_status"] = (
                    None if pipeline is None else pipeline.status
                )
                evidence["pipeline_sha"] = None if pipeline is None else pipeline.sha
        except (GitLabError, OSError, RepositoryError, RuntimeError, ValueError):
            evidence["operational_observation"] = "unavailable"
    return OperationalPlan(
        command=command,
        action=action,
        project={
            "host": config.host,
            "project_id": project.id,
            "default_branch": project.default_branch,
        },
        authenticated_username=identity,
        target_iid=target_iid,
        evidence=evidence,
    )


def _recovery_observation(
    record: LedgerRecord | None,
    *,
    issue_iid: int,
    gitlab: GitLabPort,
    issue: GitLabIssue | None = None,
    merge_requests: tuple[MergeRequestEvidence, ...] | None = None,
    repository: RepositoryPort | None = None,
    project: GitLabProject | None = None,
    controller_status: str | None = None,
    owner_status: str = "unknown",
    split_dependencies: tuple[tuple[int, str], ...] = (),
    state_root: Path | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
    select_retained_evidence: bool = True,
) -> tuple[RecoveryObservation, WorktreeStatus | None]:
    """Build bounded recovery evidence without reading untrusted issue prose."""
    observed_issue = gitlab.fetch_issue(issue_iid) if issue is None else issue
    pipeline_status = None
    pipeline_sha = None
    pipeline_id = None
    merge_request_iid = None
    merge_request_state = None
    merge_request_sha = None
    ambiguous = False
    if record is not None:
        branch_matches = tuple(
            merge_request
            for merge_request in (
                gitlab.open_merge_requests()
                if merge_requests is None
                else merge_requests
            )
            if merge_request.source_branch == record.source_branch
        )
        expected_sha = record.publication_sha or record.source_sha
        exact_matches = tuple(
            merge_request
            for merge_request in branch_matches
            if (
                (
                    record.merge_request_iid is None
                    or merge_request.iid == record.merge_request_iid
                )
                and (project is None or merge_request.source_project_id == project.id)
                and (project is None or merge_request.target_project_id == project.id)
                and (
                    project is None
                    or merge_request.target_branch == project.default_branch
                )
                and merge_request.state == "opened"
                and merge_request.sha == expected_sha
            )
        )
        if (branch_matches or record.merge_request_iid is not None) and len(
            exact_matches
        ) != 1:
            ambiguous = True
        elif len(exact_matches) == 1:
            merge_request = exact_matches[0]
            pipeline = gitlab.latest_pipeline_for_sha(
                merge_request.iid, merge_request.sha
            )
            pipeline_id = None if pipeline is None else pipeline.id
            pipeline_status = None if pipeline is None else pipeline.status
            pipeline_sha = None if pipeline is None else pipeline.sha
            merge_request_iid = merge_request.iid
            merge_request_state = merge_request.state
            merge_request_sha = merge_request.sha
    exists = (
        None
        if record is None or repository is None
        else repository.worktree_exists(record.worktree)
    )
    head = (
        None
        if not exists or record is None or repository is None
        else repository.head(record.worktree)
    )
    status = None
    status_failed = False
    if exists and record is not None and repository is not None:
        try:
            status = repository.observe_worktree_status(record.worktree)
        except RepositoryError:
            status_failed = True
    paths = () if status is None else status.changed_paths
    blocked_cause = None
    if (
        record is not None
        and record.state is RunnerState.BLOCKED
        and record.error_code
        in {
            ErrorCode.WORKER_PROTOCOL_INVALID,
            ErrorCode.WORKER_RECEIPT_INVALID,
            ErrorCode.WORKER_EXIT,
            ErrorCode.WORKER_TIMEOUT,
            ErrorCode.RED_CHECKPOINT_FAILED,
            ErrorCode.RETRY_EXHAUSTED,
        }
    ):
        latest = record.condition_history[-1] if record.condition_history else None
        if (
            latest is not None
            and latest.code == ErrorCode.RETRY_EXHAUSTED.value
            and len(record.condition_history) >= 2
        ):
            latest = record.condition_history[-2]
        if latest is not None and (
            (
                record.error_code is ErrorCode.RED_CHECKPOINT_FAILED
                and latest.code == ErrorCode.RED_CHECKPOINT_FAILED.value
                and _is_safe_red_checkpoint_diagnostic(latest.detail)
            )
            or (
                record.error_code is not ErrorCode.RED_CHECKPOINT_FAILED
                and latest.code
                in {
                    ErrorCode.WORKER_PROTOCOL_INVALID.value,
                    ErrorCode.WORKER_RECEIPT_INVALID.value,
                }
                and _is_safe_worker_protocol_diagnostic(latest.detail)
            )
            or (
                record.error_code is ErrorCode.RETRY_EXHAUSTED
                and latest.code
                in {ErrorCode.WORKER_EXIT.value, ErrorCode.WORKER_TIMEOUT.value}
                and retained_precheckpoint_failure_is_untouched(record)
            )
        ):
            blocked_cause = latest.detail
    precheckpoint = _precheckpoint_test_only_red_evidence(
        record,
        repository=repository,
        state_root=state_root,
        split_dependencies=split_dependencies,
    )
    retry_ineligible_reason = None
    discovery_marker_shape = _is_untouched_discovery_marker_record(
        record,
        issue=observed_issue,
        project=project,
        repository=repository,
        owner_status=owner_status,
    )
    if discovery_marker_shape and project is not None:
        try:
            _discovery_marker_recovery_evidence(
                record,
                issue=observed_issue,
                project=project,
                repository=repository,
                gitlab=gitlab,
                owner_status=owner_status,
            )
        except RecoveryIneligibleError:
            retry_ineligible_reason = "discovery marker recovery evidence is ineligible"
    dirty = None if status_failed or exists is None else bool(status and status.dirty)
    if status_failed:
        retry_ineligible_reason = "retained worktree evidence could not be verified"
    if (
        record is not None
        and record.state is RunnerState.BLOCKED
        and repository is not None
        and not _precheckpoint_test_only_red_family(record)
        and not discovery_marker_shape
        and select_retained_evidence
        and not status_failed
    ):
        try:
            _retained_dirty_recovery_evidence(
                record,
                repository=repository,
                load_red_checkpoint=load_red_checkpoint,
                status=status,
            )
        except RepositoryError:
            retry_ineligible_reason = "retained worktree evidence could not be verified"
            dirty = None
        except (OSError, RuntimeError, SegmentError, ValueError) as error:
            retry_ineligible_reason = _safe_retry_ineligible_reason(error)
    observation = RecoveryObservation(
        issue_iid=issue_iid,
        issue_state=observed_issue.state,
        labels=observed_issue.labels,
        record_state=None if record is None else record.state,
        pipeline_status=pipeline_status,
        split_dependencies=split_dependencies,
        stale=record is not None
        and record.state in {RunnerState.EXECUTING, RunnerState.OBSERVING}
        and owner_status in {"stale", "missing"}
        and not (
            record.state is RunnerState.OBSERVING
            and pipeline_status in NONTERMINAL_PIPELINE_STATUSES
        ),
        ambiguous=ambiguous,
        ledger_revision=None if record is None else record.revision,
        source_branch=None if record is None else record.source_branch,
        source_sha=None if record is None else record.source_sha,
        worktree=None if record is None else str(record.worktree),
        worktree_exists=exists,
        worktree_head=head,
        worktree_dirty=dirty,
        worktree_changed_paths=paths,
        merge_request_iid=merge_request_iid,
        merge_request_state=merge_request_state,
        merge_request_sha=merge_request_sha,
        pipeline_id=pipeline_id,
        pipeline_sha=pipeline_sha,
        controller_status=controller_status,
        owner_status=owner_status,
        merge_request_url=None if record is None else record.merge_request_url,
        blocked_cause=blocked_cause,
        error_code=None
        if record is None or record.error_code is None
        else record.error_code.value,
        precheckpoint_test_only_red=precheckpoint is not None,
        retry_ineligible_reason=retry_ineligible_reason,
    )
    return observation, status


def _safe_retry_ineligible_reason(error: Exception) -> str:
    """Expose only fixed recovery-validator categories, never arbitrary details."""
    known = {
        "correction recovery evidence is incomplete",
        "correction recovery evidence changed",
        "legacy publication recovery evidence is ineligible",
        "affected correction recovery evidence is ineligible",
        "retained dirty recovery evidence is ineligible",
    }
    detail = str(error)
    return (
        detail if detail in known else "retained dirty recovery evidence is ineligible"
    )


def _precheckpoint_test_only_red_family(record: LedgerRecord | None) -> bool:
    """Recognize the bounded historical Red-checkpoint failure family."""
    return (
        record is not None
        and record.state is RunnerState.BLOCKED
        and record.phase is WorkflowPhase.WORKFLOW
        and record.error_code is ErrorCode.RED_CHECKPOINT_FAILED
    )


def _precheckpoint_test_only_red_evidence(
    record: LedgerRecord | None,
    *,
    repository: RepositoryPort | None,
    state_root: Path | None,
    split_dependencies: tuple[tuple[int, str], ...],
) -> dict[str, str | int | bool] | None:
    """Bind the sole historical test-only Red retry shape without changing state."""
    if (
        not _precheckpoint_test_only_red_family(record)
        or repository is None
        or state_root is None
    ):
        return None
    assert record is not None
    try:
        candidate = repository.observe_legacy_publication_candidate(record.worktree)
    except RepositoryError:
        return None
    paths = candidate.unstaged_paths
    normalized_paths = tuple(PurePosixPath(path).as_posix() for path in paths)
    expected_worktree = state_root / "worktrees" / str(record.issue_iid)
    eligible = (
        record.work_kind is WorkKind.ORDINARY
        and record.workflow_lane is WorkflowLane.UNKNOWN
        and record.prior_state is None
        and record.prior_phase is None
        and record.retry_count == 0
        and record.retry_at is None
        and record.automatic_repair_count == 0
        and record.automatic_repair_at is None
        and record.condition_history == ()
        and record.phase_timings == ()
        and record.source_sha is not None
        and (record.main_sha is None or record.main_sha == record.source_sha)
        and record.source_branch == f"agent/issue-{record.issue_iid}"
        and record.worktree.resolve() == expected_worktree.resolve()
        and record.intent is None
        and record.audit_evidence is None
        and record.receipts == ()
        and record.publication_sha is None
        and record.merge_request_iid is None
        and record.merge_request_url is None
        and record.merge_request_draft is None
        and record.feedback_cursor is None
        and record.pending_feedback is None
        and record.repair_attempt is None
        and record.suggest_close_evidence is None
        and record.worker_exit is None
        and record.correction_identity is None
        and record.completed_at is None
        and candidate.branch == record.source_branch
        and candidate.head == record.source_sha
        and candidate.merge_head is None
        and candidate.staged_paths == ()
        and bool(paths)
        and paths == normalized_paths
        and paths == tuple(sorted(set(paths)))
        and all(path.startswith("tests/") and path.endswith(".py") for path in paths)
        and not candidate.has_untracked
        and not candidate.has_unmerged
        and not candidate.has_renames
        and not any(state == "opened" for _, state in split_dependencies)
    )
    if not eligible:
        return None
    return {
        "precheckpoint_test_only_red": True,
        "precheckpoint_project_id": record.project_id,
        "precheckpoint_issue_iid": record.issue_iid,
        "precheckpoint_source_branch": record.source_branch,
        "precheckpoint_base_sha": record.source_sha,
        "precheckpoint_head_sha": candidate.head,
        "precheckpoint_worktree": str(record.worktree),
        "precheckpoint_index_fingerprint": candidate.index_fingerprint,
        "precheckpoint_test_paths": json.dumps(paths),
    }


def _is_safe_worker_protocol_diagnostic(value: str) -> bool:
    """Keep the recovery boundary aligned with worker diagnostic generation."""
    return is_safe_worker_result_diagnostic(value)


def _is_safe_red_checkpoint_diagnostic(value: str) -> bool:
    """Recognize only fixed diagnostics authored by the Red controller boundary."""
    try:
        RedCheckpointFailureStage(value)
    except ValueError:
        return False
    return True


def _recovery_split_dependencies(
    issue_iid: int,
    *,
    project: GitLabProject,
    issues: tuple[GitLabIssue, ...],
) -> tuple[tuple[int, str], ...]:
    """Read validated split prerequisites for exactly one recovery target."""
    proofs = dict(prerequisite_dependency_proof(issues, project_id=project.id))
    try:
        return proofs[issue_iid]
    except KeyError as error:
        raise ValueError("recovery target is missing from issue inventory") from error


def _recovery_plan(
    request: CommandRequest,
    *,
    config: AgentWorkConfig,
    project: GitLabProject,
    username: str,
    record: LedgerRecord | None,
    gitlab: GitLabPort,
    repository: RepositoryPort | None = None,
    controller_status: str | None = None,
    owner_status: str = "unknown",
    issue: GitLabIssue | None = None,
    dependency_inventory: tuple[GitLabIssue, ...],
    merge_requests: tuple[MergeRequestEvidence, ...] | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
) -> tuple[OperationalPlan, RecoveryObservation]:
    if request.issue_iid is None or request.choice is None or request.reason is None:
        raise RuntimeError("recovery resolution request is incomplete")
    try:
        choice = ResolutionChoice(request.choice)
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    split_dependencies = _recovery_split_dependencies(
        request.issue_iid, project=project, issues=dependency_inventory
    )
    precheckpoint = _precheckpoint_test_only_red_evidence(
        record,
        repository=repository,
        state_root=config.state_root,
        split_dependencies=split_dependencies,
    )
    discovery_shape = _is_untouched_discovery_marker_record(
        record,
        issue=issue,
        project=project,
        repository=repository,
        owner_status=owner_status,
    )
    try:
        observation, status = _recovery_observation(
            record,
            issue_iid=request.issue_iid,
            gitlab=gitlab,
            repository=repository,
            project=project,
            controller_status=controller_status,
            owner_status=owner_status,
            issue=issue,
            merge_requests=merge_requests,
            split_dependencies=split_dependencies,
            state_root=config.state_root,
            load_red_checkpoint=load_red_checkpoint,
            select_retained_evidence=False,
        )
        if observation.worktree_exists and status is None:
            raise RepositoryError("could not inspect retained worktree status")
        dirty_evidence = None
        if (
            choice is ResolutionChoice.RETRY
            and not _precheckpoint_test_only_red_family(record)
            and not discovery_shape
        ):
            if observation.retry_ineligible_reason is not None:
                raise RecoveryIneligibleError(observation.retry_ineligible_reason)
            dirty_evidence = _retained_dirty_recovery_evidence(
                record,
                repository=repository,
                load_red_checkpoint=load_red_checkpoint,
                status=status,
            )
        if choice is ResolutionChoice.RETRY and request._automatic_blocked_retry:
            _require_safely_retryable_automatic_retry(observation)
        resolution = build_resolution_plan(
            ResolutionRequest(
                request.issue_iid,
                choice,
                request.reason,
                request.evidence_urls,
                request.replacement_issue_iid,
                request.target_label,
            ),
            observation,
        )
    except ValueError as error:
        raise RuntimeError(str(error)) from error
    evidence: dict[str, str | int | bool | None] = {
        key: json.dumps(value, sort_keys=True, default=str)
        if isinstance(value, tuple)
        else value
        if isinstance(value, (str, int, bool)) or value is None
        else json.dumps(value, sort_keys=True, default=str)
        for key, value in resolution.evidence
    }
    evidence.update(
        {
            "reason": resolution.reason,
            "evidence_urls": ",".join(resolution.evidence_urls),
            "replacement_issue_iid": resolution.replacement_issue_iid,
            "target_label": resolution.target_label,
            "resolution_digest": resolution.digest,
        }
    )
    if dirty_evidence is not None:
        evidence.update(dirty_evidence)
    if (
        choice is ResolutionChoice.RETRY
        and _precheckpoint_test_only_red_family(record)
        and precheckpoint is None
    ):
        raise RecoveryIneligibleError(
            "pre-checkpoint test-only Red recovery evidence is ineligible"
        )
    if precheckpoint is not None:
        evidence.update(precheckpoint)
    discovery_marker = (
        _discovery_marker_recovery_evidence(
            record,
            issue=issue,
            project=project,
            repository=repository,
            gitlab=gitlab,
            owner_status=owner_status,
        )
        if choice is ResolutionChoice.RETRY
        else None
    )
    if discovery_marker is not None:
        evidence.update(discovery_marker)
    return OperationalPlan(
        command=request.command,
        action=choice.value,
        project={
            "host": project.host,
            "project_id": project.id,
            "default_branch": project.default_branch,
        },
        authenticated_username=username,
        target_iid=request.issue_iid,
        evidence=evidence,
    ), observation


def _discovery_marker_recovery_evidence(
    record: LedgerRecord | None,
    *,
    issue: GitLabIssue | None,
    project: GitLabProject,
    repository: RepositoryPort | None,
    gitlab: GitLabPort,
    owner_status: str,
) -> dict[str, str | int | bool] | None:
    """Bind the sole clean discovery-marker repair before reclassification.

    The original blocked record contains no authored work.  Current issue and
    merged-MR evidence therefore supplies the only classification input, while
    every fact that makes replacing that empty record safe is plan-bound.
    """
    if record is None or record.error_code is not ErrorCode.REMEDIATION_MARKER_INVALID:
        return None
    if not _is_untouched_discovery_marker_record(
        record,
        issue=issue,
        project=project,
        repository=repository,
        owner_status=owner_status,
    ):
        raise RecoveryIneligibleError(
            "discovery marker recovery requires untouched evidence"
        )
    assert issue is not None
    try:
        classification = classify_work(issue, project_id=project.id)
    except ClassificationError as error:
        raise RecoveryIneligibleError(
            "discovery marker recovery classification is invalid"
        ) from error
    evidence: dict[str, str | int | bool] = {
        "discovery_marker_reclassification": True,
        "issue_title": issue.title,
        "issue_description_sha256": hashlib.sha256(
            issue.description.encode()
        ).hexdigest(),
        "issue_labels": json.dumps(issue.labels),
        "ledger_revision": record.revision,
        "expected_worktree_exists": False,
        "expected_issue_branch_available": True,
        "expected_owner_status": owner_status,
        "classified_work_kind": classification.kind.value,
        "classified_merge_request_iid": classification.merge_request_iid or 0,
        "classified_source_branch": classification.source_branch or "",
        "classified_source_sha": classification.source_sha or "",
    }
    if classification.kind is WorkKind.ORDINARY:
        if gitlab.open_merge_requests_by_branch(record.source_branch):
            raise RecoveryIneligibleError(
                "discovery marker recovery branch is occupied"
            )
        evidence["open_branch_merge_request_count"] = 0
        return evidence
    if (
        classification.kind
        not in {WorkKind.EXISTING_MR_REMEDIATION, WorkKind.MERGE_CONFLICT_REPAIR}
        or classification.merge_request_iid is None
        or classification.source_branch is None
        or classification.source_sha is None
    ):
        raise RecoveryIneligibleError(
            "discovery marker recovery classification is unsafe"
        )
    merge_request = gitlab.fetch_merge_request(classification.merge_request_iid)
    if not (
        merge_request.iid == classification.merge_request_iid
        and merge_request.state in {"opened", "merged"}
        and merge_request.source_project_id == project.id
        and merge_request.target_project_id == project.id
        and merge_request.target_branch == project.default_branch
        and merge_request.source_branch == classification.source_branch
        and merge_request.sha == classification.source_sha
    ):
        raise RecoveryIneligibleError(
            "discovery marker recovery merge-request evidence changed"
        )
    evidence.update(
        {
            "remediation_mr_state": merge_request.state,
            "remediation_mr_source_branch": merge_request.source_branch,
            "remediation_mr_sha": merge_request.sha,
        }
    )
    return evidence


def _is_untouched_discovery_marker_record(
    record: LedgerRecord | None,
    *,
    issue: GitLabIssue | None,
    project: GitLabProject | None,
    repository: RepositoryPort | None,
    owner_status: str,
) -> bool:
    """Return whether an invalid discovery record contains no retained work."""
    if record is None or issue is None or repository is None or project is None:
        return False
    return (
        record.project_id == project.id
        and record.issue_iid == issue.iid
        and issue.state == "opened"
        and set(issue.labels).intersection(LIFECYCLE_LABELS) == {"agent::blocked"}
        and record.state is RunnerState.BLOCKED
        and record.phase is WorkflowPhase.DISCOVERY
        and record.error_code is ErrorCode.REMEDIATION_MARKER_INVALID
        and record.workflow_lane is WorkflowLane.UNKNOWN
        and record.work_kind is WorkKind.ORDINARY
        and record.source_branch == f"agent/issue-{record.issue_iid}"
        and record.worktree
        == repository.config.state_root / "worktrees" / str(record.issue_iid)
        and record.source_sha is None
        and record.main_sha is None
        and record.retry_count == 0
        and record.retry_at is None
        and record.automatic_repair_count == 0
        and record.automatic_repair_at is None
        and record.blocked_cause is None
        and record.prior_state is None
        and record.prior_phase is None
        and record.merge_request_iid is None
        and record.merge_request_url is None
        and record.publication_sha is None
        and record.intent is None
        and not record.receipts
        and record.feedback_cursor is None
        and record.pending_feedback is None
        and record.merge_request_draft is None
        and record.repair_attempt is None
        and record.suggest_close_evidence is None
        and record.outcome_projection is None
        and record.audit_evidence in {None, "unknown"}
        and record.correction_identity is None
        and record.worker_exit is None
        and record.activation_started_at is not None
        and record.completed_at is None
        and not record.split_prerequisites
        and not record.condition_history
        and not record.phase_timings
        and not repository.worktree_exists(record.worktree)
        and repository.fresh_issue_branch_available(record.issue_iid)
        and owner_status in {"missing", "stale"}
    )


def _discovery_marker_replay_is_exact(
    record: LedgerRecord,
    plan: OperationalPlan,
    *,
    issue: GitLabIssue,
    project: GitLabProject,
    repository: RepositoryPort,
    gitlab: GitLabPort,
    owner_status: str,
) -> bool:
    """Recheck a pre-projection reclassification before replaying its label."""
    evidence = plan.evidence
    expected_labels = evidence.get("issue_labels")
    expected_revision = evidence.get("ledger_revision")
    if not isinstance(expected_labels, str) or type(expected_revision) is not int:
        return False
    try:
        original_labels = tuple(json.loads(expected_labels))
    except (TypeError, ValueError):
        return False
    projected_labels = tuple(
        label for label in original_labels if label not in LIFECYCLE_LABELS
    ) + ("agent::running",)
    if not (
        record.state is RunnerState.PREPARING
        and record.phase is WorkflowPhase.WORKFLOW
        and record.project_id == project.id
        and record.issue_iid == issue.iid
        and record.revision == expected_revision + 2
        and record.worktree
        == repository.config.state_root / "worktrees" / str(record.issue_iid)
        and not repository.worktree_exists(record.worktree)
        and repository.fresh_issue_branch_available(record.issue_iid)
        and owner_status == evidence.get("expected_owner_status")
        and issue.state == "opened"
        and issue.labels in {original_labels, projected_labels}
        and evidence.get("issue_title") == issue.title
        and evidence.get("issue_description_sha256")
        == hashlib.sha256(issue.description.encode()).hexdigest()
    ):
        return False
    try:
        classification = classify_work(issue, project_id=project.id)
    except ClassificationError:
        return False
    if not (
        evidence.get("classified_work_kind") == classification.kind.value
        and evidence.get("classified_merge_request_iid")
        == (classification.merge_request_iid or 0)
        and evidence.get("classified_source_branch")
        == (classification.source_branch or "")
        and evidence.get("classified_source_sha") == (classification.source_sha or "")
        and record.work_kind is classification.kind
        and record.merge_request_iid == classification.merge_request_iid
        and record.source_branch
        == (classification.source_branch or f"agent/issue-{record.issue_iid}")
        and record.source_sha == classification.source_sha
    ):
        return False
    if classification.kind is WorkKind.ORDINARY:
        return not gitlab.open_merge_requests_by_branch(record.source_branch)
    if classification.merge_request_iid is None:
        return False
    merge_request = gitlab.fetch_merge_request(classification.merge_request_iid)
    return (
        merge_request.iid == classification.merge_request_iid
        and merge_request.state == evidence.get("remediation_mr_state")
        and merge_request.source_project_id == project.id
        and merge_request.target_project_id == project.id
        and merge_request.target_branch == project.default_branch
        and merge_request.source_branch == classification.source_branch
        and merge_request.sha == classification.source_sha
        and evidence.get("remediation_mr_source_branch") == merge_request.source_branch
        and evidence.get("remediation_mr_sha") == merge_request.sha
    )


def _require_safely_retryable_automatic_retry(
    observation: RecoveryObservation,
) -> None:
    """Require the inventory's current safe classification for direct retry."""
    candidates = build_inventory((observation,)).candidates
    if (
        len(candidates) != 1
        or candidates[0].classification is not RecoveryClassification.SAFELY_RETRYABLE
    ):
        raise RuntimeError(
            "automatic blocked retry requires safely retryable recovery evidence"
        )


@dataclass(frozen=True)
class _AutomaticResumeReview:
    """One reason and its exact live inputs for a blocked issue."""

    reason: str | None
    owner_status: str = "missing"
    issue: GitLabIssue | None = None
    observation: RecoveryObservation | None = None
    capacity: RecoveryOwnerCapacity | None = None


def _automatic_resume_review(
    record: LedgerRecord,
    *,
    records: tuple[LedgerRecord, ...],
    wired: RuntimeDependencies,
    project: GitLabProject,
    now: datetime,
    dependency_inventory: tuple[GitLabIssue, ...] | None = None,
    target_owner_status: str | None = None,
) -> _AutomaticResumeReview:
    """Classify static and current live proof, returning a specific rejection."""
    reason = automatic_blocked_static_rejection(
        record, now=now, project_id=project.id, state_root=wired.config.state_root
    )
    if reason is not None:
        return _AutomaticResumeReview(reason)
    inventory = (
        wired.gitlab.prerequisite_projection_inventory()
        if dependency_inventory is None
        else dependency_inventory
    )
    issue = next((item for item in inventory if item.iid == record.issue_iid), None)
    if issue is None:
        return _AutomaticResumeReview("issue is absent from prerequisite inventory")
    if wired.gitlab.fetch_issue(record.issue_iid) != issue:
        return _AutomaticResumeReview(
            "issue evidence changed during observation", issue=issue
        )
    if issue.state != "opened":
        return _AutomaticResumeReview("issue is not open", issue=issue)
    if "agent::blocked" not in issue.labels:
        return _AutomaticResumeReview("blocked lifecycle label is missing", issue=issue)
    if set(issue.labels).intersection(LIFECYCLE_LABELS) != {"agent::blocked"}:
        return _AutomaticResumeReview(
            "blocked lifecycle label is not exact", issue=issue
        )
    try:
        classification = classify_work(issue, project_id=project.id)
    except ClassificationError:
        return _AutomaticResumeReview(
            "issue classification requires review", issue=issue
        )
    if classification.kind is not WorkKind.ORDINARY:
        return _AutomaticResumeReview("issue is not ordinary work", issue=issue)
    owner_status = target_owner_status or _issue_owner_status(wired, record.issue_iid)
    capacity = _owner_capacity(
        records,
        target_iid=record.issue_iid,
        wired=wired,
        target_owner_status=owner_status,
    )
    if capacity.identity_conflict:
        return _AutomaticResumeReview(
            "competing retained identity", owner_status, issue, capacity=capacity
        )
    if capacity.live_owner_iid is not None or capacity.unknown_owner_iid is not None:
        return _AutomaticResumeReview(
            "another retained owner has serial priority",
            owner_status,
            issue,
            capacity=capacity,
        )
    if owner_status != "missing":
        return _AutomaticResumeReview(
            f"retained owner is {owner_status}", owner_status, issue, capacity=capacity
        )
    try:
        dependencies = _recovery_split_dependencies(
            record.issue_iid, project=project, issues=inventory
        )
    except ValueError:
        return _AutomaticResumeReview(
            "split prerequisite evidence is unavailable",
            owner_status,
            issue,
            capacity=capacity,
        )
    if any(state != "closed" for _, state in dependencies):
        return _AutomaticResumeReview(
            "split prerequisite is still open", owner_status, issue, capacity=capacity
        )
    try:
        observation, _ = _recovery_observation(
            record,
            issue_iid=record.issue_iid,
            gitlab=wired.gitlab,
            issue=issue,
            merge_requests=wired.gitlab.open_merge_requests(),
            repository=wired.repository,
            project=project,
            owner_status=owner_status,
            split_dependencies=dependencies,
            select_retained_evidence=False,
        )
    except RepositoryError:
        return _AutomaticResumeReview(
            "retained worktree evidence is unavailable",
            owner_status,
            issue,
            capacity=capacity,
        )
    reason = _automatic_observation_rejection(record, observation)
    if reason is not None:
        return _AutomaticResumeReview(
            reason, owner_status, issue, observation, capacity
        )
    if wired.load_red_checkpoint is None:
        reason = "retained Red checkpoint evidence is unavailable"
    else:
        try:
            checkpoint = wired.load_red_checkpoint(
                record.project_id, record.issue_iid, record.source_sha or ""
            )
        except (OSError, RuntimeError, ValueError):
            reason = "retained Red checkpoint evidence is unavailable"
        else:
            reason = "retained Red checkpoint is present" if checkpoint else None
    if reason is not None:
        return _AutomaticResumeReview(
            reason, owner_status, issue, observation, capacity
        )
    try:
        wired.repository.verify_worktree(
            record.worktree, record.source_branch, record.source_sha or ""
        )
    except RepositoryError:
        reason = "retained branch or baseline is not exact"
    return _AutomaticResumeReview(reason, owner_status, issue, observation, capacity)


def _automatic_observation_rejection(
    record: LedgerRecord, observation: RecoveryObservation
) -> str | None:
    """Explain why shared recovery observation cannot authorize clean retry."""
    if observation.ambiguous:
        return "conflicting merge request evidence"
    if observation.worktree_exists is not True:
        return "retained worktree is absent"
    if observation.worktree_dirty is None:
        return "retained worktree status is unavailable"
    if observation.worktree_dirty or observation.worktree_changed_paths:
        return "retained worktree is dirty"
    if observation.worktree_head != record.source_sha:
        return "retained worktree head differs from the baseline"
    if observation.merge_request_iid is not None:
        return "retained merge request evidence requires reviewed recovery"
    if observation.retry_ineligible_reason is not None:
        return observation.retry_ineligible_reason
    if (
        build_inventory((observation,)).candidates[0].classification
        is not RecoveryClassification.SAFELY_RETRYABLE
    ):
        return "shared recovery classification requires review"
    if observation.blocked_cause is not None:
        return None
    if (
        record.error_code is ErrorCode.WORKER_PROTOCOL_INVALID
        and record.phase is WorkflowPhase.WORKFLOW
        and record.retry_count == 0
        and len(record.condition_history) == 1
        and retained_precheckpoint_failure_is_untouched(record)
    ):
        return None
    return "retained failure diagnostic is not controller-verified"


def _identical_blocked_failure_repeated(record: LedgerRecord) -> bool:
    """Detect repeated controller failure across transient and terminal retry rows."""
    history = record.condition_history
    if len(history) < 2 or record.retry_at is not None:
        return False
    latest = history[-1]
    if latest.code in {ErrorCode.WORKER_EXIT.value, ErrorCode.WORKER_TIMEOUT.value}:
        return latest.detail == "Automatic retry budget exhausted." and any(
            item.code == latest.code for item in history[-4:-1]
        )
    previous = history[-2]
    return previous.code == latest.code and previous.detail == latest.detail


def _retain_automatic_resume_rejection(
    record: LedgerRecord,
    review: _AutomaticResumeReview,
    *,
    wired: RuntimeDependencies,
    project: GitLabProject,
    now: datetime,
) -> None:
    """Persist one current skip reason while preserving human recovery instructions."""
    assert review.reason is not None
    note = f"Automatic resume deferred: {review.reason}."
    if record.next_action and not record.next_action.startswith(
        "Automatic resume deferred:"
    ):
        original_summary = record.summary.split("Automatic resume deferred:", 1)[
            0
        ].rstrip()
        updates = {"summary": f"{original_summary} {note}".strip()}
    else:
        updates = {"next_action": note}
    if all(getattr(record, key) == value for key, value in updates.items()):
        return
    lock = (
        nullcontext()
        if review.owner_status != "missing"
        else wired.issue_lock(record.issue_iid)
    )
    with lock:
        current = wired.ledger.load(record.issue_iid)
        if current is None or current != record:
            return
        refreshed = _automatic_resume_review(
            current,
            records=tuple(wired.ledger.list_records()),
            wired=wired,
            project=project,
            now=now,
            target_owner_status=(
                review.owner_status if review.owner_status == "missing" else None
            ),
        )
        if refreshed.reason == review.reason:
            wired.ledger.persist(record.model_copy(update=updates))


def _automatic_clean_blocked_retry(
    *,
    wired: RuntimeDependencies,
    project: GitLabProject,
    dependency_inventory: tuple[GitLabIssue, ...],
    now: datetime,
) -> LedgerRecord | None:
    """Resume at most one clean blocked issue under the caller's project lock."""
    records = tuple(wired.ledger.list_records())
    if resume_candidates(records, now=now):
        return None
    candidates = tuple(
        record
        for record in sorted(records, key=lambda item: item.issue_iid)
        if record.state is RunnerState.BLOCKED and record.work_kind is WorkKind.ORDINARY
    )
    for record in candidates:
        review = _automatic_resume_review(
            record,
            records=records,
            wired=wired,
            project=project,
            now=now,
            dependency_inventory=dependency_inventory,
        )
        if review.reason is not None:
            _retain_automatic_resume_rejection(
                record, review, wired=wired, project=project, now=now
            )
            continue
        with wired.issue_lock(record.issue_iid):
            current = wired.ledger.load(record.issue_iid)
            if current is None or current != record:
                continue
            refreshed = _automatic_resume_review(
                current,
                records=tuple(wired.ledger.list_records()),
                wired=wired,
                project=project,
                now=now,
                target_owner_status=review.owner_status,
            )
            if refreshed != review:
                continue
            if _identical_blocked_failure_repeated(record):
                wired.ledger.persist(
                    record.model_copy(update={"retry_at": now + timedelta(minutes=20)})
                )
                continue
            resumed = wired.ledger.persist(
                transition_record(
                    record,
                    RunnerState.PREPARING,
                    retry_at=None,
                    retry_count=0,
                    error_code=None,
                    worker_exit=None,
                    summary="Automatically resumed after exact recovery checks.",
                    next_action="Continue ordinary preparation.",
                )
            )
            wired.gitlab.transition_labels(
                record.issue_iid, label_for_state(resumed.state)
            )
            return resumed
    return None


def _recovery_child_marker(project_id: int, issue_iid: int) -> str:
    """Return a stable marker that cannot be mistaken for a split child."""
    digest = hashlib.sha256(f"{project_id}:{issue_iid}:recovery".encode()).hexdigest()
    return f"<!-- agent-work-recovery-child:v1 project={project_id} original={issue_iid} fingerprint={digest} -->"


def _cooldown_recovery_advice(
    wired: RuntimeDependencies,
    record: LedgerRecord,
    condition_fingerprint: str,
    attempts: int,
    now: datetime,
) -> None:
    """Retain one adviser attempt and defer the same condition."""
    cooldown = now + timedelta(minutes=20)
    wired.ledger.persist(
        record.model_copy(
            update={
                "retry_at": cooldown,
                "recovery_child": RecoveryChildState(
                    condition_fingerprint=condition_fingerprint,
                    attempts=attempts + 1,
                    cooldown_until=cooldown,
                ),
            }
        )
    )


def _idle_recovery_child(
    *,
    wired: RuntimeDependencies,
    project: GitLabProject,
    now: datetime,
    remaining_seconds: Callable[[], float],
) -> tuple[str, LedgerRecord] | None:
    """Create or adopt one clean child after ordinary loop selection is idle."""
    for selected in sorted(
        wired.ledger.list_records(), key=lambda item: item.issue_iid
    ):
        if remaining_seconds() <= 0:
            return None
        if (
            selected.project_id != project.id
            or selected.state is not RunnerState.BLOCKED
            or selected.phase is not WorkflowPhase.WORKFLOW
            or selected.work_kind is not WorkKind.ORDINARY
            or selected.error_code is not ErrorCode.RED_CHECKPOINT_FAILED
            or not selected.worktree_dirty
            or selected.retry_at is not None
            and selected.retry_at > now
            or selected.source_sha is None
            or selected.source_branch != f"agent/issue-{selected.issue_iid}"
            or selected.worktree.resolve()
            != (
                wired.config.state_root / "worktrees" / str(selected.issue_iid)
            ).resolve()
        ):
            continue
        with wired.issue_lock(selected.issue_iid):
            record = wired.ledger.load(selected.issue_iid)
            if record != selected:
                continue
            assert record is not None
            recovery_state = record.recovery_child
            if (
                record.intent is None
                and recovery_state is not None
                and recovery_state.child_iid is not None
            ):
                continue
            marker = _recovery_child_marker(project.id, record.issue_iid)
            title = f"Recover blocked issue #{record.issue_iid} in a clean checkout"
            description = (
                f"{marker}\n\nParent: #{record.issue_iid}\n"
                "Recreate the intended change in a clean checkout. Inspect the blocked "
                "original as evidence; do not mutate or close it. Complete the ordinary "
                "workflow with human review."
            )
            if record.intent is not None:
                if (
                    record.intent.kind != "recovery_child_create"
                    or record.intent.operation_id != marker
                    or recovery_state is None
                    or record.intent.expected_evidence
                    != {
                        "project_id": project.id,
                        "issue_iid": record.issue_iid,
                        "condition_fingerprint": recovery_state.condition_fingerprint,
                    }
                ):
                    continue
                try:
                    child_inventory = wired.gitlab.recovery_child_inventory()
                except (GitLabError, OSError, RuntimeError, ValueError):
                    continue
                children = tuple(
                    child for child in child_inventory if marker in child.description
                )
                conflicting_children = tuple(
                    child
                    for child in child_inventory
                    if f"original={record.issue_iid} " in child.description
                    and marker not in child.description
                )
                if len(children) != 1 or conflicting_children:
                    continue
                child = children[0]
                if (
                    child.iid == record.issue_iid
                    or child.title != title
                    or child.description != description
                    or child.confidential
                    or "confidential" not in child.model_fields_set
                ):
                    continue
                adopted = wired.ledger.persist(
                    record.model_copy(
                        update={
                            "intent": None,
                            "retry_at": None,
                            "recovery_child": RecoveryChildState(
                                condition_fingerprint=recovery_state.condition_fingerprint,
                                attempts=recovery_state.attempts,
                                child_iid=child.iid,
                            ),
                        }
                    )
                )
                return "recovery_child_adopted", adopted
            try:
                issue = wired.gitlab.fetch_issue(record.issue_iid)
                inventory = wired.gitlab.prerequisite_projection_inventory()
                dependencies = _recovery_split_dependencies(
                    record.issue_iid, project=project, issues=inventory
                )
                owner = _issue_owner_status(wired, record.issue_iid)
                wired.repository.verify_worktree(
                    record.worktree, record.source_branch, record.source_sha or ""
                )
                candidate = wired.repository.observe_legacy_publication_candidate(
                    record.worktree
                )
                snapshot = wired.repository.retained_worktree_fingerprint(
                    record.worktree
                )
                child_inventory = wired.gitlab.recovery_child_inventory()
                children = tuple(
                    child for child in child_inventory if marker in child.description
                )
                conflicting_children = tuple(
                    child
                    for child in child_inventory
                    if f"original={record.issue_iid} " in child.description
                    and marker not in child.description
                )
                merge_requests = wired.gitlab.open_merge_requests()
                checkpoint = (
                    None
                    if wired.load_red_checkpoint is None
                    else wired.load_red_checkpoint(
                        record.project_id, record.issue_iid, record.source_sha or ""
                    )
                )
                exact = (
                    issue.state == "opened"
                    and not issue.confidential
                    and "confidential" in issue.model_fields_set
                    and MANUAL_TRACKER_LABEL not in issue.labels
                    and set(issue.labels).intersection(LIFECYCLE_LABELS)
                    == {"agent::blocked"}
                    and next(
                        (item for item in inventory if item.iid == issue.iid), None
                    )
                    == issue
                    and classify_work(issue, project_id=project.id).kind
                    is WorkKind.ORDINARY
                    and owner == "missing"
                    and not any(state != "closed" for _, state in dependencies)
                    and not any(
                        mr.source_branch == record.source_branch
                        or wired.gitlab.mr_closes_issue(mr.iid, record.issue_iid)
                        for mr in merge_requests
                    )
                    and wired.repository.worktree_exists(record.worktree)
                    and wired.repository.has_changes(record.worktree)
                    and candidate.branch == record.source_branch
                    and candidate.head == record.source_sha
                    and candidate.merge_head is None
                    and not candidate.has_untracked
                    and not candidate.has_unmerged
                    and not candidate.has_renames
                    and bool(candidate.staged_paths or candidate.unstaged_paths)
                    and checkpoint is None
                    and len(children) <= 1
                    and not conflicting_children
                    and wired.repository.head(record.worktree) == record.source_sha
                )
            except (
                ClassificationError,
                GitLabError,
                RepositoryError,
                OSError,
                RuntimeError,
                ValueError,
            ):
                continue
            if children:
                continue
            if not exact:
                continue
            condition_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "project_id": project.id,
                        "issue_iid": record.issue_iid,
                        "source_sha": record.source_sha,
                        "issue": issue.model_dump(mode="json"),
                        "dependencies": dependencies,
                        "merge_requests": [mr.iid for mr in merge_requests],
                        "index": candidate.index_fingerprint,
                        "worktree": snapshot,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            attempts = (
                recovery_state.attempts
                if recovery_state is not None
                and recovery_state.condition_fingerprint == condition_fingerprint
                else 0
            )
            if _identical_blocked_failure_repeated(record):
                cooldown = now + timedelta(minutes=20)
                wired.ledger.persist(
                    record.model_copy(
                        update={
                            "retry_at": cooldown,
                            "recovery_child": RecoveryChildState(
                                condition_fingerprint=condition_fingerprint,
                                attempts=attempts + 1,
                                cooldown_until=cooldown,
                            ),
                        }
                    )
                )
                continue
            adviser_timeout = min(120, int(remaining_seconds()))
            if adviser_timeout <= 0:
                return None
            try:
                advice = wired.invoke.invoke(
                    wired.config.checkout,
                    "RECOVERY_ADVISORY_V1\nAct as $idle-recovery-adviser. You have no tools. "
                    "Return one JSON object with action create_child or defer and the supplied identity only. "
                    f"project_id={project.id} issue_iid={record.issue_iid} "
                    f"source_branch={record.source_branch} source_sha={record.source_sha} "
                    f"ledger_revision={record.revision}",
                    adviser_timeout,
                )
            except (OSError, RuntimeError, ValueError):
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            if len(advice.stdout) > 2_048:
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            try:
                decision = json.loads(advice.stdout)
            except (TypeError, ValueError):
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            identity = {
                "project_id": project.id,
                "issue_iid": record.issue_iid,
                "source_branch": record.source_branch,
                "source_sha": record.source_sha,
                "ledger_revision": record.revision,
            }
            if (
                advice.returncode != 0
                or not isinstance(decision, dict)
                or set(decision) != {"action", *identity}
                or any(decision.get(key) != value for key, value in identity.items())
            ):
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            if decision["action"] == "defer":
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            if decision["action"] != "create_child":
                _cooldown_recovery_advice(
                    wired, record, condition_fingerprint, attempts, now
                )
                return None
            try:
                refreshed = wired.repository.observe_legacy_publication_candidate(
                    record.worktree
                )
                refreshed_merge_requests = wired.gitlab.open_merge_requests()
                refreshed_issue = wired.gitlab.fetch_issue(record.issue_iid)
                refreshed_has_closing_mr = any(
                    mr.source_branch == record.source_branch
                    or wired.gitlab.mr_closes_issue(mr.iid, record.issue_iid)
                    for mr in refreshed_merge_requests
                )
                if (
                    remaining_seconds() <= 0
                    or wired.ledger.load(record.issue_iid) != record
                    or refreshed_issue != issue
                    or refreshed_issue.confidential
                    or "confidential" not in refreshed_issue.model_fields_set
                    or MANUAL_TRACKER_LABEL in refreshed_issue.labels
                    or wired.gitlab.prerequisite_projection_inventory() != inventory
                    or refreshed_merge_requests != merge_requests
                    or refreshed_has_closing_mr
                    or _issue_owner_status(wired, record.issue_iid) != owner
                    or refreshed != candidate
                    or wired.repository.head(record.worktree) != record.source_sha
                    or wired.repository.retained_worktree_fingerprint(record.worktree)
                    != snapshot
                    or wired.gitlab.recovery_child_inventory() != child_inventory
                ):
                    return None
            except (GitLabError, RepositoryError, OSError, RuntimeError, ValueError):
                return None
            if remaining_seconds() <= 0:
                return None
            pending = wired.ledger.persist(
                record.model_copy(
                    update={
                        "intent": TransitionIntentRecord(
                            operation_id=marker,
                            kind="recovery_child_create",
                            expected_evidence={
                                "project_id": project.id,
                                "issue_iid": record.issue_iid,
                                "condition_fingerprint": condition_fingerprint,
                            },
                        ),
                        "recovery_child": RecoveryChildState(
                            condition_fingerprint=condition_fingerprint,
                            attempts=attempts + 1,
                        ),
                    }
                )
            )
            if remaining_seconds() <= 0:
                return None
            try:
                child = wired.gitlab.create_recovery_child(
                    title=title, description=description, marker=marker
                )
            except GitLabError:
                return None
            if (
                child.iid == record.issue_iid
                or child.title != title
                or child.description != description
                or child.state != "opened"
                or child.labels
                or child.confidential
                or "confidential" not in child.model_fields_set
            ):
                return None
            finished = wired.ledger.persist(
                pending.model_copy(
                    update={
                        "intent": None,
                        "retry_at": None,
                        "recovery_child": RecoveryChildState(
                            condition_fingerprint=condition_fingerprint,
                            attempts=attempts + 1,
                            child_iid=child.iid,
                        ),
                    }
                )
            )
            return "recovery_child_created", finished
    return None


def _retained_dirty_recovery_evidence(
    record: LedgerRecord | None,
    *,
    repository: RepositoryPort | None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
    status: WorktreeStatus | None,
) -> dict[str, str] | None:
    """Select exact retained dirty recovery evidence for preview and inventory."""
    legacy = _legacy_dirty_publication_evidence(
        record,
        repository=repository,
        load_red_checkpoint=load_red_checkpoint,
        status=status,
    )
    if legacy is not None:
        return legacy
    authored = _authored_candidate_recovery_evidence(
        record,
        repository=repository,
        load_red_checkpoint=load_red_checkpoint,
        status=status,
    )
    if authored is not None:
        return authored
    affected = _affected_dirty_correction_evidence(
        record,
        repository=repository,
        load_red_checkpoint=load_red_checkpoint,
        status=status,
    )
    if affected is not None:
        return affected
    if (
        record is not None
        and record.state is RunnerState.BLOCKED
        and repository is not None
        and status is not None
        and status.dirty
    ):
        raise RecoveryIneligibleError("retained dirty recovery evidence is ineligible")
    return None


def _legacy_dirty_publication_evidence(
    record: LedgerRecord | None,
    *,
    repository: RepositoryPort | None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
    status: WorktreeStatus | None,
) -> dict[str, str] | None:
    """Bind one historical dirty publication candidate or reject it fail-closed."""
    if record is not None and record.correction_identity is not None:
        if repository is None or record.source_sha is None:
            raise RuntimeError("correction recovery evidence is incomplete")
        checkpoint_base = (
            record.correction_identity.baseline_sha
            if record.correction_identity.mode == "authored_candidate_recovery"
            else record.source_sha
        )
        checkpoint = (
            None
            if record.correction_identity.workflow_lane is WorkflowLane.CONTENT
            or load_red_checkpoint is None
            else load_red_checkpoint(
                record.project_id, record.issue_iid, checkpoint_base
            )
        )
        candidate = repository.observe_legacy_publication_candidate(
            record.worktree, status=status
        )
        if not correction_identity_matches(
            record,
            candidate,
            checkpoint=checkpoint,
            original_base_sha=(
                repository.single_parent(record.worktree, record.source_sha)
                if record.correction_identity.mode == "authored_candidate_recovery"
                else None
            ),
        ):
            raise RuntimeError("correction recovery evidence changed")
        return {"correction_identity": record.correction_identity.model_dump_json()}
    if (
        record is None
        or record.intent is None
        or record.intent.kind != "publish_merge_request"
    ):
        return None
    if repository is None or status is None or not status.dirty:
        return None
    if (
        record.error_code is not ErrorCode.PUBLICATION_IDENTITY_MISMATCH
        or record.state is not RunnerState.BLOCKED
        or record.phase is not WorkflowPhase.READY_TO_PUBLISH
        or record.source_sha is None
    ):
        raise RecoveryIneligibleError(
            "legacy publication recovery evidence is ineligible"
        )
    candidate = repository.observe_legacy_publication_candidate(
        record.worktree, status=status
    )
    expected = record.intent.expected_evidence
    eligible = (
        record.work_kind is WorkKind.ORDINARY
        and record.publication_sha is None
        and record.pending_feedback is None
        and record.merge_request_draft is not None
        and expected.get("branch") == record.source_branch
        and expected.get("baseline_sha") == record.source_sha
        and candidate.branch == record.source_branch
        and candidate.head == record.source_sha
        and candidate.merge_head is None
        and bool(candidate.staged_paths)
        and bool(candidate.unstaged_paths)
        and set(candidate.unstaged_paths).issubset(set(candidate.staged_paths))
        and not candidate.has_untracked
        and not candidate.has_unmerged
        and not candidate.has_renames
    )
    checkpoint = None
    if record.workflow_lane is WorkflowLane.CODING:
        checkpoint = (
            None
            if load_red_checkpoint is None
            else load_red_checkpoint(
                record.project_id, record.issue_iid, record.source_sha
            )
        )
        eligible = eligible and checkpoint is not None
    elif record.workflow_lane is not WorkflowLane.CONTENT:
        eligible = False
    if not eligible:
        raise RecoveryIneligibleError(
            "legacy publication recovery evidence is ineligible"
        )
    identity = build_publication_correction_identity(
        record, candidate, checkpoint=checkpoint
    )
    return {
        "legacy_index_fingerprint": candidate.index_fingerprint,
        "legacy_staged_paths": ",".join(candidate.staged_paths),
        "legacy_unstaged_paths": ",".join(candidate.unstaged_paths),
        "legacy_branch": candidate.branch,
        "legacy_head": candidate.head,
        "legacy_red_checkpoint": (
            "" if checkpoint is None else checkpoint.model_dump_json()
        ),
        "correction_identity": identity.model_dump_json(),
    }


def _affected_dirty_correction_evidence(
    record: LedgerRecord | None,
    *,
    repository: RepositoryPort | None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
    status: WorktreeStatus | None,
) -> dict[str, str] | None:
    """Bind the one retained worker-protocol failure shape that predates snapshots."""
    if record is None or repository is None or record.correction_identity is not None:
        return None
    if status is None or not status.dirty:
        return None
    if not (
        record.state is RunnerState.BLOCKED
        and record.phase is WorkflowPhase.WORKFLOW
        and record.prior_state is RunnerState.EXECUTING
        and record.prior_phase is WorkflowPhase.WORKFLOW
        and record.work_kind is WorkKind.ORDINARY
        and record.source_sha is not None
        and record.source_branch == f"agent/issue-{record.issue_iid}"
        and record.workflow_lane in {WorkflowLane.CODING, WorkflowLane.CONTENT}
        and record.intent is None
        and record.audit_evidence is None
        and record.receipts == ()
        and record.publication_sha is None
        and record.merge_request_draft is None
        and record.pending_feedback is None
        and record.repair_attempt is None
        and record.suggest_close_evidence is None
        and record.worker_exit is None
        and bool(record.condition_history)
        and any(
            item.code == ErrorCode.WORKER_PROTOCOL_INVALID.value
            for item in record.condition_history
        )
        and all(
            item.code
            in {
                ErrorCode.WORKER_PROTOCOL_INVALID.value,
                ErrorCode.RETRY_EXHAUSTED.value,
            }
            for item in record.condition_history
        )
    ):
        raise RecoveryIneligibleError(
            "affected correction recovery evidence is ineligible"
        )
    candidate = repository.observe_legacy_publication_candidate(
        record.worktree, status=status
    )
    checkpoint = (
        None
        if record.workflow_lane is WorkflowLane.CONTENT or load_red_checkpoint is None
        else load_red_checkpoint(record.project_id, record.issue_iid, record.source_sha)
    )
    identity = build_publication_correction_identity(
        record, candidate, checkpoint=checkpoint
    )
    if candidate.merge_head is not None:
        raise RecoveryIneligibleError(
            "affected correction recovery evidence is ineligible"
        )
    return {
        "correction_identity": identity.model_dump_json(),
        "affected_correction": "true",
    }


def _authored_candidate_recovery_evidence(
    record: LedgerRecord | None,
    *,
    repository: RepositoryPort | None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
    status: WorktreeStatus | None,
) -> dict[str, str] | None:
    """Bind one reviewed committed candidate to its original Red checkpoint."""
    if record is None or repository is None or record.correction_identity is not None:
        return None
    if status is None or not status.dirty or record.source_sha is None:
        return None
    original_base_sha = repository.single_parent(record.worktree, record.source_sha)
    checkpoint = (
        None
        if load_red_checkpoint is None or original_base_sha is None
        else load_red_checkpoint(record.project_id, record.issue_iid, original_base_sha)
    )
    try:
        identity = build_authored_candidate_correction_identity(
            record,
            repository.observe_legacy_publication_candidate(
                record.worktree, status=status
            ),
            original_base_sha=original_base_sha,
            checkpoint=checkpoint,
        )
    except SegmentError:
        return None
    assert record.merge_request_draft is not None
    return {
        "correction_identity": identity.model_dump_json(),
        "authored_candidate": "true",
        "retained_audit_evidence": "unknown",
        "retained_merge_request_draft": record.merge_request_draft.model_dump_json(),
    }


def _normalization_is_exact(
    record: LedgerRecord, *, gitlab: GitLabPort, repository: RepositoryPort
) -> bool:
    """Prove the narrowly safe interrupted pre-worker claim predicate."""
    if (
        record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
    ):
        return False
    try:
        issue = gitlab.fetch_issue(record.issue_iid)
        return (
            issue.state == "opened"
            and set(issue.labels).intersection(LIFECYCLE_LABELS) == {"agent::running"}
            and record.source_branch == f"agent/issue-{record.issue_iid}"
            and retained_ordinary_is_untouched(record)
            and repository.worktree_exists(record.worktree)
            and repository.head(record.worktree) == record.source_sha
            and not repository.has_changes(record.worktree)
            and not repository.changed_paths(record.worktree)
            and not gitlab.open_merge_requests_by_branch(record.source_branch)
            and repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha or ""
            )
            is None
        )
    except (GitLabError, OSError, RepositoryError, RuntimeError, ValueError):
        return False


def _dirty_claim_evidence(
    record: LedgerRecord,
    *,
    owner_status: str,
    project_id: int,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
    state_root: Path,
) -> dict[str, str] | None:
    """Bind one stale dirty pre-worker claim to its retained content and Red."""
    unprogressed = retained_ordinary_is_untouched(
        record.model_copy(update={"main_sha": record.source_sha})
    )
    if (
        owner_status not in {"stale", "missing"}
        or record.project_id != project_id
        or record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
        or record.workflow_lane not in {WorkflowLane.UNKNOWN, WorkflowLane.CODING}
        or record.source_branch != f"agent/issue-{record.issue_iid}"
        or record.worktree.resolve()
        != (state_root / "worktrees" / str(record.issue_iid)).resolve()
        or record.main_sha is None
        or not unprogressed
        or record.prior_state is not None
        or record.prior_phase is not None
        or record.retry_count != 0
        or record.retry_at is not None
        or record.correction_identity is not None
        or record.outcome_projection is not None
        or record.completed_at is not None
        or load_red_checkpoint is None
    ):
        return None
    try:
        issue = gitlab.fetch_issue(record.issue_iid)
        checkpoint = load_red_checkpoint(
            record.project_id, record.issue_iid, record.source_sha or ""
        )
        candidate = repository.observe_legacy_publication_candidate(record.worktree)
        if (
            issue.state != "opened"
            or set(issue.labels).intersection(LIFECYCLE_LABELS) != {"agent::running"}
            or not repository.worktree_exists(record.worktree)
            or not repository.has_changes(record.worktree)
            or candidate.branch != record.source_branch
            or candidate.head != record.source_sha
            or candidate.merge_head is not None
            or candidate.has_untracked
            or candidate.has_unmerged
            or candidate.has_renames
            or not (candidate.staged_paths or candidate.unstaged_paths)
            or gitlab.open_merge_requests_by_branch(record.source_branch)
            or (
                checkpoint is not None
                and (
                    checkpoint.project_id != record.project_id
                    or checkpoint.issue_iid != record.issue_iid
                    or checkpoint.source_branch != record.source_branch
                    or checkpoint.base_sha != record.source_sha
                )
            )
        ):
            return None
        repository.verify_worktree(
            record.worktree, record.source_branch, record.source_sha or ""
        )
        return {
            "dirty_index_fingerprint": candidate.index_fingerprint,
            "dirty_snapshot_fingerprint": repository.retained_worktree_fingerprint(
                record.worktree
            ),
            "dirty_red_fingerprint": (
                "absent"
                if checkpoint is None
                else hashlib.sha256(checkpoint.model_dump_json().encode()).hexdigest()
            ),
            "dirty_branch": candidate.branch,
            "dirty_head": candidate.head,
            "dirty_staged_paths": json.dumps(candidate.staged_paths),
            "dirty_unstaged_paths": json.dumps(candidate.unstaged_paths),
            "dirty_changed_paths": json.dumps(
                repository.changed_paths(record.worktree)
            ),
        }
    except (GitLabError, OSError, RepositoryError, RuntimeError, ValueError):
        return None


def _handoff_record_fingerprint(record: LedgerRecord) -> str:
    """Bind the blocked record while excluding ledger-managed timestamps and revision."""
    snapshot = record.model_dump_json(
        exclude={
            "revision",
            "activation_started_at",
            "latest_meaningful_update_at",
            "interrupted_claim_receipt",
        }
    )
    return hashlib.sha256(snapshot.encode()).hexdigest()


def _owner_capacity(
    records: tuple[LedgerRecord, ...],
    *,
    target_iid: int,
    wired: RuntimeDependencies,
    target_owner_status: str | None = None,
):
    """Read every retained issue marker once for an evidence-bound recovery plan."""
    observations = tuple(
        RecoveryObservation(
            issue_iid=record.issue_iid,
            record_state=record.state,
            source_branch=record.source_branch,
            worktree=str(record.worktree),
            merge_request_iid=record.merge_request_iid,
            merge_request_url=record.merge_request_url,
            owner_status=(
                target_owner_status
                if record.issue_iid == target_iid and target_owner_status is not None
                else _issue_owner_status(wired, record.issue_iid)
            ),
        )
        for record in sorted(records, key=lambda item: item.issue_iid)
    )
    return classify_recovery_owner_capacity(observations, target_iid=target_iid)


def _issue_owner_status(wired: RuntimeDependencies, issue_iid: int) -> str:
    """Return bounded passive owner evidence when the probe is unavailable."""
    try:
        return wired.issue_owner_probe(issue_iid)
    except (OSError, RuntimeError, ValueError):
        return "unknown"


def _repair_evidence_copy(evidence: Mapping[str, object]) -> dict[str, object]:
    """Copy reviewed operational evidence into the bounded repair record shape."""
    return {key: value for key, value in evidence.items()}


def _operational_preview(
    request: CommandRequest,
    *,
    wired: RuntimeDependencies,
    project: GitLabProject,
    username: str,
    target_owner_status: str | None = None,
) -> CommandReport:
    """Choose one harmless canary/recovery checkpoint without selecting work."""
    records = tuple(wired.ledger.list_records())
    gate = _inspect_pipeline_gate(
        gitlab=wired.gitlab,
        repository=wired.repository,
        records=records,
        project_id=project.id,
        default_branch=project.default_branch,
        now=wired.clock(),
    )
    target = request.issue_iid
    action = "checkpoint"
    dirty_claim_evidence: dict[str, str] | None = None
    next_action = "Review the operational checkpoint; no issue was claimed."
    outcome = "checkpoint"
    label_drift = wired.lifecycle_label_drift()
    capacity = None
    if request.command == "loop.recover" and target is not None:
        capacity = _owner_capacity(
            records,
            target_iid=target,
            wired=wired,
            target_owner_status=target_owner_status,
        )
    if request.command == "loop.canary" and label_drift:
        action = "provision_lifecycle_labels"
        next_action = "Review and apply the fixed lifecycle-label provisioning plan."
    if request.command == "loop.canary":
        if action == "provision_lifecycle_labels":
            pass
        elif gate.repair_owner is not None or gate.deferred_owner is not None:
            deferred = gate.deferred_owner is not None
            selected_owner = gate.deferred_owner or gate.repair_owner
            assert selected_owner is not None
            target = selected_owner.issue_iid
            next_action = (
                (
                    f"Inspect quarantined retained issue {target}'s automatic repair evidence."
                    if selected_owner.automatic_repair_count
                    >= AUTOMATIC_REPAIR_MAX_ATTEMPTS
                    and selected_owner.automatic_repair_at is None
                    else f"Inspect deferred retained issue {target}'s automatic repair evidence."
                )
                if deferred
                else f"Recover retained issue {target} for the failed exact pipeline."
            )
        elif gate.outcome is not None:
            action = gate.outcome
            next_action = gate.next_action or next_action
    else:
        if target is None:
            raise RuntimeError("recover requires an issue IID")
        assert capacity is not None
        if (
            gate.repair_owner is not None
            and gate.repair_owner.issue_iid != target
            and gate.repair_owner.state
            in {RunnerState.NEEDS_REPAIR, RunnerState.REPAIR_FAILED}
        ):
            action = "pipeline_blocked"
            next_action = (
                f"Requested issue {target} cannot be recovered while retained issue "
                f"{gate.repair_owner.issue_iid} owns the failed exact pipeline."
            )
            plan = _operational_evidence(
                config=wired.config,
                project=project,
                identity=username,
                records=records,
                target_iid=target,
                action=action,
                command=request.command,
                max_segments=request.max_segments,
                max_activation_seconds=request.max_activation_seconds,
                gitlab=wired.gitlab,
                repository=wired.repository,
                lifecycle_label_drift=label_drift,
                owner_states=() if capacity is None else capacity.owner_states,
            )
            return _report(
                request,
                applied=False,
                outcome="pipeline_blocked",
                project={"host": wired.config.host, "project_id": project.id},
                summary="Read-only operational plan.",
                next_action=next_action,
                operational_plan=plan,
            )
        if gate.outcome is not None:
            action = gate.outcome
            next_action = gate.next_action or next_action
            plan = _operational_evidence(
                config=wired.config,
                project=project,
                identity=username,
                records=records,
                target_iid=target,
                action=action,
                command=request.command,
                max_segments=request.max_segments,
                max_activation_seconds=request.max_activation_seconds,
                gitlab=wired.gitlab,
                repository=wired.repository,
                lifecycle_label_drift=label_drift,
                owner_states=() if capacity is None else capacity.owner_states,
            )
            return _report(
                request,
                applied=False,
                outcome=outcome,
                project={"host": wired.config.host, "project_id": project.id},
                summary="Read-only operational plan.",
                next_action=next_action,
                operational_plan=plan,
            )
        record = wired.ledger.load(target)
        if record is None:
            action = "target_missing"
            next_action = (
                f"Retained issue {target} does not exist; do not recover another item."
            )
        elif capacity.identity_conflict:
            action = "identity_conflict"
            next_action = (
                "Resolve the shared retained identity before recovering issue "
                f"{target}."
            )
        elif capacity.unknown_owner_iid is not None:
            action = "ownership_unverified"
            next_action = (
                "Verify the owner marker for retained issue "
                f"{capacity.unknown_owner_iid} before recovering issue {target}."
            )
        elif capacity.live_owner_iid is not None:
            action = "capacity_blocked"
            next_action = (
                f"Retained issue {capacity.live_owner_iid} has the verified live owner; "
                f"do not advance issue {target}."
            )
        elif record is not None and (
            record.state is RunnerState.EXECUTING
            and record.phase is WorkflowPhase.WORKFLOW
        ):
            dirty_claim_evidence = _dirty_claim_evidence(
                record,
                owner_status=dict(capacity.owner_states).get(target, "unknown"),
                project_id=project.id,
                gitlab=wired.gitlab,
                repository=wired.repository,
                load_red_checkpoint=wired.load_red_checkpoint,
                state_root=wired.config.state_root,
            )
            action = (
                "normalize_interrupted_claim"
                if _normalization_is_exact(
                    record, gitlab=wired.gitlab, repository=wired.repository
                )
                else "handoff_stale_dirty_claim"
                if dirty_claim_evidence is not None
                else "normalization_unverified"
            )
            next_action = (
                f"Apply the reviewed dirty-claim handoff for retained issue {target}."
                if action == "handoff_stale_dirty_claim"
                else f"Apply the reviewed normalization for retained issue {target}."
            )
        elif record is not None:
            action = (
                "repair"
                if record.state in {RunnerState.NEEDS_REPAIR, RunnerState.REPAIR_FAILED}
                else "run_segment"
            )
            next_action = (
                f"Apply the reviewed repair checkpoint for retained issue {target}."
            )
    plan = _operational_evidence(
        config=wired.config,
        project=project,
        identity=username,
        records=records,
        target_iid=target,
        action=action,
        command=request.command,
        max_segments=request.max_segments,
        max_activation_seconds=request.max_activation_seconds,
        gitlab=wired.gitlab,
        repository=wired.repository,
        lifecycle_label_drift=label_drift,
        owner_states=() if capacity is None else capacity.owner_states,
        extra_evidence=dirty_claim_evidence,
    )
    return _report(
        request,
        applied=False,
        outcome=outcome,
        project={"host": wired.config.host, "project_id": project.id},
        summary="Read-only operational plan.",
        next_action=next_action,
        operational_plan=plan,
    )


ScopeCriterion = models.ScopeCriterion
ScopeRevision = models.ScopeRevision
ScopeApprovalRequest = models.ScopeApprovalRequest
ScopeApprovalEvent = models.ScopeApprovalEvent
ScopeSourceNote = models.ScopeSourceNote
ScopeSourceAuthor = models.ScopeSourceAuthor
ScopeSourceObservation = models.ScopeSourceObservation
ScopeApprovalBinding = models.ScopeApprovalBinding
HostScopeDecision = approved_scope.HostScopeDecision
ScopeError = approved_scope.ScopeError
canonical_scope_digest = approved_scope.canonical_scope_digest
capture_scope = approved_scope.capture_scope
verify_scope = approved_scope.verify_scope
bind_scope_record = approved_scope.bind_scope_record


@dataclass(frozen=True)
class RuntimeDependencies:
    """Injected stateful boundaries for command dispatch."""

    config: AgentWorkConfig
    ledger: LedgerPort
    gitlab: GitLabPort
    repository: RepositoryPort
    invoke: InvokerPort
    lock: ProjectLockFactory
    clock: Callable[[], datetime]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    logger: logging.Logger
    read_plan: Callable[[Path], bytes]
    process_probe: Callable[[], str]
    scope_read_source: (
        Callable[[ScopeApprovalRequest], ScopeSourceObservation] | None
    ) = None
    scope_verify_event: (
        Callable[[ScopeApprovalRequest, ScopeSourceObservation], HostScopeDecision]
        | None
    ) = None
    issue_owner_probe: Callable[[int], str] = lambda _issue_iid: "unknown"
    lifecycle_label_drift: Callable[[], tuple[str, ...]] = lambda: ()
    ensure_lifecycle_labels: Callable[[], None] = lambda: None
    issue_lock: Callable[[int], AbstractContextManager[None]] = lambda _issue_iid: (
        nullcontext()
    )
    load_red_checkpoint: (
        Callable[[int, int, str], RedCheckpointEvidence | None] | None
    ) = None
    apply_red_checkpoint: (
        Callable[[int, RedCheckpointRequest, str], RedCheckpointEvidence] | None
    ) = None
    run_segment_boundary: Callable[..., LedgerRecord | None] = run_segment
    observe_stage_identity: (
        Callable[[LedgerRecord], StageTimingIdentity | None] | None
    ) = None
    observe_interactive_validation: Callable[[], object] = lambda: None
    observe_yielded_interactive_validation: Callable[
        [], InteractiveValidationRequest | None
    ] = lambda: None
    read_validation_admission: Callable[[], dict[str, object] | None] = lambda: None


class RuntimeCheckpointRepository(RepositoryPort, Protocol):
    """Repository operations needed to compose trusted checkpoint capture."""

    def head(self, worktree: Path) -> str: ...

    def checkpoint_tree(self, ref: str) -> str | None: ...

    def run_red_command(
        self, worktree: Path, argv: tuple[str, ...]
    ) -> CommandResult: ...

    def snapshot_test_tree(
        self, worktree: Path, base_sha: str, paths: tuple[str, ...]
    ) -> str: ...

    def write_red_checkpoint_ref(
        self, worktree: Path, evidence: RedCheckpointEvidence
    ) -> None: ...

    def read_red_checkpoint_ref(
        self, worktree: Path, project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None: ...


class RuntimeGitLabPort(GitLabPort, Protocol):
    """GitLab operations needed only while composing runtime dependencies."""

    def lifecycle_label_drift(self) -> tuple[str, ...]: ...

    def ensure_lifecycle_labels(self) -> None: ...


class RuntimeCheckpointStore(Protocol):
    """Durable checkpoint-sidecar operations needed by composition."""

    def read(
        self, project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None: ...


class RuntimeCheckpointCreator(Protocol):
    """Trusted checkpoint transaction callable used by the composition root."""

    def __call__(
        self,
        *,
        issue_iid: int,
        request: RedCheckpointRequest,
        dependencies: RedCheckpointDependencies,
    ) -> RedCheckpointEvidence: ...


@dataclass(frozen=True)
class RuntimeCompositionDependencies:
    """Explicit constructors and boundaries for composition-root testing."""

    config: AgentWorkConfig
    logger: logging.Logger
    gitlab: RuntimeGitLabPort
    repository: RuntimeCheckpointRepository
    invoke: InvokerPort
    checkpoint_store: RuntimeCheckpointStore
    create_checkpoint: RuntimeCheckpointCreator = create_red_checkpoint


@dataclass(frozen=True)
class WorkerResultDependencies:
    """Read-only boundaries required to build one worker result."""

    load_record: Callable[[int], LedgerRecord | None]
    changed_paths: Callable[[Path], tuple[str, ...]]
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] = (
        lambda _project_id, _issue_iid, _base_sha: None
    )


@dataclass(frozen=True)
class _PipelineGateDecision:
    """Read-only pre-selection pipeline decision for one loop activation."""

    outcome: str | None = None
    summary: str | None = None
    next_action: str | None = None
    repair_owner: LedgerRecord | None = None
    merge_request: MergeRequestEvidence | None = None
    pipeline: PipelineEvidence | None = None
    deferred_owner: LedgerRecord | None = None
    deferred_observer_iids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _RepairActivationDecision:
    """One exact-pipeline repair activation or fail-closed ownership result."""

    record: LedgerRecord | None = None
    competing_active_owner: bool = False


_PIPELINE_POLL_INITIAL_SECONDS = 5.0
_PIPELINE_POLL_MAX_SECONDS = 30.0


def _has_competing_repair_owner(
    records: tuple[LedgerRecord, ...], owner: LedgerRecord
) -> bool:
    """Return whether another retained record prevents this repair activation."""
    return any(
        record.issue_iid != owner.issue_iid and record.state in IMMEDIATE_OWNER_STATES
        for record in records
    )


def _activate_pipeline_repair_gate(
    *,
    gate: _PipelineGateDecision,
    records: tuple[LedgerRecord, ...],
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
) -> _RepairActivationDecision | None:
    """Apply one exact failed-pipeline gate."""
    owner = gate.repair_owner
    if (
        owner is None
        or owner.state
        not in {
            RunnerState.OBSERVING,
            RunnerState.NEEDS_REPAIR,
            RunnerState.REPAIR_FAILED,
        }
        or gate.merge_request is None
        or gate.pipeline is None
    ):
        return None
    if _has_competing_repair_owner(records, owner):
        return _RepairActivationDecision(competing_active_owner=True)
    pipeline = gate.pipeline
    assert pipeline is not None
    try:
        diagnostics = collect_exact_failed_job_diagnostics(
            jobs=gitlab.list_pipeline_jobs(pipeline.id, pipeline.sha),
            pipeline_id=pipeline.id,
            source_sha=pipeline.sha,
            fetch_trace=lambda job_id: gitlab.fetch_job_trace(pipeline.id, job_id),
        )
    except (GitLabError, SegmentError):
        return _RepairActivationDecision(
            record=record_failed_job_diagnostic_unavailable(
                owner, ledger=ledger, gitlab=gitlab
            )
        )
    return _RepairActivationDecision(
        record=activate_exact_pipeline_repair(
            owner,
            merge_request=gate.merge_request,
            pipeline=pipeline,
            failed_job=diagnostics[0][0],
            diagnostic=diagnostics[0][1],
            additional_job_diagnostics=diagnostics[1:],
            ledger=ledger,
            gitlab=gitlab,
            now=now,
        )
    )


def _sleep_for_pipeline_poll(
    *,
    delay: float,
    remaining_seconds: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[float, bool]:
    """Sleep within the activation budget and return the next delay and viability."""
    remaining = remaining_seconds()
    if remaining <= 0:
        return delay, False
    sleep(min(delay, remaining))
    return min(delay * 2, _PIPELINE_POLL_MAX_SECONDS), remaining_seconds() > 0


def _pipeline_handoff(
    *, outcome: str, pipeline_url: str | None, detail: str, next_action: str
) -> _PipelineGateDecision:
    """Build bounded operator-facing evidence without exposing API payloads."""
    link = f" Pipeline: {pipeline_url}." if pipeline_url else ""
    return _PipelineGateDecision(
        outcome=outcome,
        summary=f"{detail}{link}",
        next_action=next_action,
    )


def _exact_pipeline_owner_candidates(
    *,
    records: tuple[LedgerRecord, ...],
    merge_request: MergeRequestEvidence,
    project_id: int,
) -> tuple[LedgerRecord, ...]:
    """Return durable records that claim one exact merge-request pipeline."""
    return tuple(
        record
        for record in records
        if record.merge_request_iid == merge_request.iid
        and record.project_id == project_id
        and (
            (
                record.state is RunnerState.OBSERVING
                and record.phase is WorkflowPhase.PIPELINE_WAIT
            )
            or (
                record.state in {RunnerState.NEEDS_REPAIR, RunnerState.REPAIR_FAILED}
                and record.phase is WorkflowPhase.POST_PUBLICATION
            )
            or (
                record.state is RunnerState.REPAIR
                and record.phase is WorkflowPhase.WORKFLOW
                and record.repair_attempt is not None
            )
        )
        and record.source_sha == merge_request.sha
        and record.source_branch == merge_request.source_branch
    )


def find_exact_pipeline_owner(
    *,
    records: tuple[LedgerRecord, ...],
    merge_request: MergeRequestEvidence,
    project_id: int,
    default_branch: str,
    repository: RepositoryPort,
) -> LedgerRecord | None:
    """Return the sole exact observation owner, or ``None`` on any ambiguity."""
    candidates = _exact_pipeline_owner_candidates(
        records=records, merge_request=merge_request, project_id=project_id
    )
    if len(candidates) != 1:
        return None
    record = candidates[0]
    if (
        merge_request.source_project_id != project_id
        or merge_request.target_project_id != project_id
        or merge_request.target_branch != default_branch
    ):
        return None
    try:
        if not repository.worktree_exists(record.worktree):
            return None
        repository.verify_worktree(
            record.worktree, record.source_branch, record.source_sha or ""
        )
    except (OSError, RepositoryError, RuntimeError, ValueError):
        return None
    return record


def _inspect_pipeline_gate(
    *,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    records: tuple[LedgerRecord, ...],
    project_id: int,
    default_branch: str,
    now: datetime,
) -> _PipelineGateDecision:
    """Inspect default and current-MR pipelines before any fresh issue selection."""
    try:
        default_pipeline = gitlab.latest_pipeline_for_ref(default_branch)
        if default_pipeline is None:
            return _pipeline_handoff(
                outcome="pipeline_inventory_blocked",
                pipeline_url=None,
                detail="The current default-branch pipeline could not be found.",
                next_action="Validate default-branch pipeline evidence before selecting work.",
            )
        if default_pipeline.status == "failed":
            return _pipeline_handoff(
                outcome="pipeline_blocked",
                pipeline_url=default_pipeline.web_url,
                detail="The current default-branch pipeline failed.",
                next_action="Inspect and resolve the failed default-branch pipeline before selecting work.",
            )
        if (
            default_pipeline.status not in NONTERMINAL_PIPELINE_STATUSES
            and default_pipeline.status != "success"
        ):
            return _pipeline_handoff(
                outcome="pipeline_inventory_blocked",
                pipeline_url=default_pipeline.web_url,
                detail="The current default-branch pipeline has an unrecognized terminal status.",
                next_action="Verify the pipeline status before selecting work.",
            )
        inspected: list[tuple[MergeRequestEvidence, PipelineEvidence]] = []
        for merge_request in sorted(
            gitlab.open_merge_requests(), key=lambda item: item.iid
        ):
            pipeline = gitlab.latest_pipeline_for_sha(
                merge_request.iid, merge_request.sha
            )
            if pipeline is None:
                return _pipeline_handoff(
                    outcome="pipeline_inventory_blocked",
                    pipeline_url=None,
                    detail="An open merge request has no current-source pipeline evidence.",
                    next_action="Validate the current merge-request pipeline before selecting work.",
                )
            if pipeline.sha != merge_request.sha:
                return _pipeline_handoff(
                    outcome="pipeline_inventory_blocked",
                    pipeline_url=pipeline.web_url,
                    detail="An open merge request pipeline does not match its current source SHA.",
                    next_action="Validate the current merge-request pipeline before selecting work.",
                )
            inspected.append((merge_request, pipeline))
        deferred_owner: LedgerRecord | None = None
        for merge_request, pipeline in inspected:
            if pipeline.status != "failed":
                continue
            exact_owner_records = _exact_pipeline_owner_candidates(
                records=records, merge_request=merge_request, project_id=project_id
            )
            if len(exact_owner_records) > 1:
                return _pipeline_handoff(
                    outcome="pipeline_blocked",
                    pipeline_url=pipeline.web_url,
                    detail="Multiple retained records claim one exact failed pipeline.",
                    next_action="Reconcile the competing retained records before selecting work.",
                )
            owner = find_exact_pipeline_owner(
                records=records,
                merge_request=merge_request,
                project_id=project_id,
                default_branch=default_branch,
                repository=repository,
            )
            if owner is not None:
                if owner.state is RunnerState.REPAIR_FAILED and (
                    owner.automatic_repair_count >= AUTOMATIC_REPAIR_MAX_ATTEMPTS
                    or (
                        owner.automatic_repair_at is not None
                        and owner.automatic_repair_at > now
                    )
                ):
                    deferred_owner = owner
                    continue
                return _PipelineGateDecision(
                    repair_owner=owner,
                    merge_request=merge_request,
                    pipeline=pipeline,
                )
            # Ownerless failed-MR evidence is item-local and cannot reserve
            # the project-wide serial slot. An inexact retained claimant stays
            # scheduler-visible for its own reconciliation.
            continue
        deferred_observer_iids: set[int] = set()
        for merge_request, pipeline in inspected:
            exact_owner_records = _exact_pipeline_owner_candidates(
                records=records, merge_request=merge_request, project_id=project_id
            )
            if len(exact_owner_records) > 1:
                return _pipeline_handoff(
                    outcome="pipeline_blocked",
                    pipeline_url=pipeline.web_url,
                    detail="Multiple retained records claim one exact merge-request pipeline.",
                    next_action="Reconcile the competing retained records before selecting work.",
                )
            owner = find_exact_pipeline_owner(
                records=records,
                merge_request=merge_request,
                project_id=project_id,
                default_branch=default_branch,
                repository=repository,
            )
            if pipeline.status == "failed":
                continue
            if pipeline.status in NONTERMINAL_PIPELINE_STATUSES:
                if (
                    owner is not None
                    and owner.state is RunnerState.OBSERVING
                    and owner.phase is WorkflowPhase.PIPELINE_WAIT
                    and not merge_request.has_conflicts
                ):
                    deferred_observer_iids.add(owner.issue_iid)
                continue
            if pipeline.status in {"success", "canceled", "skipped", "manual"}:
                continue
            return _pipeline_handoff(
                outcome="pipeline_inventory_blocked",
                pipeline_url=pipeline.web_url,
                detail="An open merge request pipeline has an unrecognized terminal status.",
                next_action="Verify the pipeline status before selecting work.",
            )
    except (GitLabError, OSError, RuntimeError, ValueError):
        return _pipeline_handoff(
            outcome="pipeline_inventory_blocked",
            pipeline_url=None,
            detail="Pipeline inventory could not be validated.",
            next_action="Restore GitLab access and validate pipeline inventory before selecting work.",
        )
    return _PipelineGateDecision(
        deferred_owner=deferred_owner,
        deferred_observer_iids=frozenset(deferred_observer_iids),
    )


def _build_local_logger(config: AgentWorkConfig, *, debug: bool) -> logging.Logger:
    """Configure one application-owned local operational log."""
    config.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("agent-work")
    for existing in tuple(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = logging.FileHandler(config.state_root / "agent-work.log")
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def observe_stage_timing_identity(
    record: LedgerRecord,
    *,
    project_id: int,
    observe_worktree: Callable[[Path], LegacyPublicationCandidate],
    read_tree: Callable[[str], str | None],
) -> StageTimingIdentity | None:
    """Bind timing only to stable clean snapshots and an immutable source tree."""
    if record.project_id != project_id or record.source_sha is None:
        return None
    try:
        before = observe_worktree(record.worktree)
        if (
            before.branch != record.source_branch
            or before.head != record.source_sha
            or before.merge_head is not None
            or before.staged_paths
            or before.unstaged_paths
            or before.has_untracked
            or before.has_unmerged
            or before.has_renames
        ):
            return None
        tree = read_tree(record.source_sha)
        if tree is None:
            return None
        after = observe_worktree(record.worktree)
        if before != after:
            return None
        return StageTimingIdentity(
            project_id=record.project_id,
            issue_iid=record.issue_iid,
            source_sha=record.source_sha,
            candidate_tree=tree,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def build_runtime_dependencies(
    *,
    debug: bool,
    composition: RuntimeCompositionDependencies | None = None,
    scope_verify_event: Callable[
        [ScopeApprovalRequest, ScopeSourceObservation], HostScopeDecision
    ]
    | None = None,
    test_execution_policy: TestExecutionPolicy | None = None,
) -> RuntimeDependencies:
    """Wire real process and filesystem boundaries at the composition root."""
    if composition is None:
        config = discover_config(
            current_directory=Path.cwd(),
            environment=os.environ,
            run_command=run_subprocess,
        )
        logger = _build_local_logger(config, debug=debug)
        gitlab: RuntimeGitLabPort = GitLabGateway(config, run_subprocess)
        repository: RuntimeCheckpointRepository = GitRepository(
            config,
            run_subprocess,
            service_environment=os.environ,
            diagnostic_sink=lambda diagnostic: logger.info("%s", diagnostic),
            test_execution_policy=test_execution_policy,
        )
        invoke: InvokerPort = AgentInvoker(
            run_subprocess,
            environment=os.environ,
            controller_state_root=config.state_root,
        )
        checkpoint_store: RuntimeCheckpointStore = RedCheckpointStore(config.state_root)
        create_checkpoint: RuntimeCheckpointCreator = create_red_checkpoint
    else:
        config = composition.config
        logger = composition.logger
        gitlab = composition.gitlab
        repository = composition.repository
        invoke = composition.invoke
        checkpoint_store = composition.checkpoint_store
        create_checkpoint = composition.create_checkpoint

    if test_execution_policy is not None:
        try:
            bind = getattr(repository, "bind_test_execution_policy", None)
            if bind is None or bind(test_execution_policy) is not True:
                raise TestExecutionError("policy_binding_failed")
        except (OSError, RuntimeError) as error:
            raise TestExecutionError("policy_binding_failed") from error

    def process_alive(pid: int) -> bool | None:
        """Return liveness when this platform can verify the marker PID."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        return True

    def probe_project_lock() -> str:
        """Passively report the project-wide activation owner marker."""
        return observe_project_lock(
            config.state_root / "project.lock", process_alive=process_alive
        )

    def probe_issue_owner(issue_iid: int) -> str:
        """Passively report one retained issue's advisory owner marker."""
        return observe_issue_lock(
            config.state_root, issue_iid, process_alive=process_alive
        )

    now = lambda: datetime.now(UTC)
    ledger = JsonLedger(config.state_root, clock=now)

    def load_red_checkpoint(
        project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None:
        record = ledger.load_read_only(issue_iid)
        if record is None:
            return None
        evidence = repository.read_red_checkpoint_ref(
            record.worktree, project_id, issue_iid, base_sha
        )
        if evidence is not None:
            return evidence
        # Retained blocked v1 work uses a sidecar/tree ref. It is read-only
        # compatibility; every new checkpoint is written as one commit ref.
        legacy = checkpoint_store.read(project_id, issue_iid, base_sha)
        if (
            legacy is not None
            and repository.checkpoint_tree(legacy.ref) != legacy.tree_oid
        ):
            raise RedCheckpointError("red checkpoint ref does not match retained tree")
        return legacy

    def apply_red_checkpoint(
        issue_iid: int, request: RedCheckpointRequest, expected_tree_oid: str
    ) -> RedCheckpointEvidence:
        original = (
            ledger.load_read_only(issue_iid)
            if test_execution_policy is not None
            else None
        )

        def run_selected_red(worktree: Path, argv: tuple[str, ...]) -> CommandResult:
            if test_execution_policy is None:
                return repository.run_red_command(worktree, argv)
            if original is None or original.source_sha is None:
                raise RedCheckpointError("red checkpoint record is unavailable")

            base_sha = original.source_sha

            def verify_identity() -> None:
                current = ledger.load_read_only(issue_iid)
                if (
                    current is None
                    or current.project_id != original.project_id
                    or current.issue_iid != original.issue_iid
                    or current.revision != original.revision
                    or current.source_branch != original.source_branch
                    or current.source_sha != original.source_sha
                    or current.worktree != original.worktree
                    or current.state != original.state
                    or current.phase != original.phase
                    or current.project_id != config.project_id
                    or worktree != original.worktree
                    or repository.head(worktree) != original.source_sha
                    or tuple(sorted(repository.changed_paths(worktree)))
                    != request.test_paths
                    or repository.snapshot_test_tree(
                        worktree, base_sha, request.test_paths
                    )
                    != expected_tree_oid
                ):
                    raise RedCheckpointError("red execution identity changed")

            verify_identity()
            try:
                bound = TestExecutionIdentity(
                    project_id=original.project_id,
                    issue_iid=original.issue_iid,
                    ledger_revision=original.revision,
                    source_branch=original.source_branch,
                    base_sha=original.source_sha,
                    candidate_tree=expected_tree_oid,
                    test_paths=request.test_paths,
                    nonce=test_execution_policy.fresh_nonce(),
                )
                result = test_execution_policy.execute_red(
                    TestExecutionRequest(identity=bound, worktree=worktree, argv=argv)
                )
            except (TestExecutionError, ValueError) as error:
                raise RedCheckpointError("isolated red execution refused") from error
            verify_identity()
            return result

        def snapshot_test_tree(base_sha: str, paths: tuple[str, ...]) -> str:
            record = ledger.load_read_only(issue_iid)
            if record is None:
                raise RedCheckpointError("red checkpoint record is unavailable")
            return repository.snapshot_test_tree(record.worktree, base_sha, paths)

        def write_evidence(evidence: RedCheckpointEvidence) -> None:
            record = ledger.load_read_only(issue_iid)
            if record is None:
                raise RedCheckpointError("red checkpoint record is unavailable")
            repository.write_red_checkpoint_ref(record.worktree, evidence)

        return create_checkpoint(
            issue_iid=issue_iid,
            request=request,
            dependencies=RedCheckpointDependencies(
                load_record=ledger.load_read_only,
                current_head=repository.head,
                changed_paths=repository.changed_paths,
                run_red_command=(
                    repository.run_red_command
                    if test_execution_policy is None
                    else run_selected_red
                ),
                snapshot_test_tree=snapshot_test_tree,
                expected_tree_oid=expected_tree_oid,
                read_evidence=load_red_checkpoint,
                write_evidence=write_evidence,
                verify_test_assets=getattr(repository, "verify_test_assets", None),
                run_red_preflight=getattr(
                    repository,
                    "run_red_preflight",
                    lambda _worktree, _paths, _tree: RepositoryCommandResult(
                        1, "", "Red preflight boundary is unavailable"
                    ),
                ),
            ),
        )

    def observe_stage_identity(record: LedgerRecord) -> StageTimingIdentity | None:
        if not hasattr(repository, "checkpoint_tree"):
            return None
        return observe_stage_timing_identity(
            record,
            project_id=config.project_id,
            observe_worktree=repository.observe_legacy_publication_candidate,
            read_tree=repository.checkpoint_tree,
        )

    return RuntimeDependencies(
        config=config,
        observe_stage_identity=observe_stage_identity,
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=invoke,
        lock=lambda: XdgProjectLock(config.state_root / "project.lock"),
        issue_lock=lambda issue_iid: XdgIssueLock(config.state_root, issue_iid),
        clock=now,
        monotonic=time.monotonic,
        sleep=time.sleep,
        logger=logger,
        read_plan=lambda path: path.read_bytes(),
        scope_read_source=gitlab.fetch_scope_source
        if isinstance(gitlab, GitLabGateway)
        else None,
        scope_verify_event=scope_verify_event,
        process_probe=probe_project_lock,
        issue_owner_probe=probe_issue_owner,
        lifecycle_label_drift=gitlab.lifecycle_label_drift,
        ensure_lifecycle_labels=gitlab.ensure_lifecycle_labels,
        load_red_checkpoint=load_red_checkpoint,
        apply_red_checkpoint=apply_red_checkpoint,
        observe_interactive_validation=lambda: observe_waiting_interactive_validation(
            host=config.host, project_id=config.project_id, environment=os.environ
        ),
        observe_yielded_interactive_validation=lambda: (
            observed
            if (
                observed := observe_waiting_interactive_validation(
                    host=config.host,
                    project_id=config.project_id,
                    environment=os.environ,
                )
            )
            is not None
            and observed.yielded_issue_iid is not None
            else None
        ),
        read_validation_admission=lambda: read_validation_admission(
            host=config.host, project_id=config.project_id, environment=os.environ
        ),
    )


def read_committed_candidate_from_current(
    issue_iid: int,
    mr_iid: int,
    candidate_sha: str,
    candidate_tree: str,
    role: str,
    selector: dict[str, object],
) -> dict[str, object]:
    """Wire a fresh, read-only committed candidate inspection invocation."""
    config = discover_config(
        current_directory=Path.cwd(),
        environment=os.environ,
        run_command=run_subprocess,
    )
    return read_committed_candidate(
        issue_iid,
        mr_iid,
        candidate_sha,
        candidate_tree,
        role,
        selector,
        ledger=JsonLedger(config.state_root, clock=lambda: datetime.now(UTC)),
        gitlab=GitLabGateway(config, run_subprocess),
        repository=GitRepository(
            config,
            run_subprocess,
            service_environment=os.environ,
            diagnostic_sink=lambda _diagnostic: None,
        ),
    )


def build_worker_result_dependencies(
    *,
    config: AgentWorkConfig | None = None,
    current_directory: Path | None = None,
    environment: Mapping[str, str] = os.environ,
    run_command: CommandRunner = run_subprocess,
) -> WorkerResultDependencies:
    """Wire only read-only ledger and repository boundaries for result building."""
    if config is None:
        config = discover_config(
            current_directory=current_directory or Path.cwd(),
            environment=environment,
            run_command=run_command,
        )
    ledger = JsonLedger(config.state_root, clock=lambda: datetime.now(UTC))
    repository = GitRepository(
        config,
        run_command,
        service_environment=environment,
        diagnostic_sink=lambda _diagnostic: None,
    )
    checkpoint_store = RedCheckpointStore(config.state_root)

    def load_red_checkpoint(
        project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None:
        record = ledger.load_read_only(issue_iid)
        if record is None:
            return None
        evidence = repository.read_red_checkpoint_ref(
            record.worktree, project_id, issue_iid, base_sha
        )
        if evidence is not None:
            return evidence
        evidence = checkpoint_store.read(project_id, issue_iid, base_sha)
        if evidence is not None and repository.checkpoint_tree(evidence.ref) != (
            evidence.tree_oid
        ):
            raise RedCheckpointError("red checkpoint ref does not match retained tree")
        return evidence

    return WorkerResultDependencies(
        load_record=ledger.load_read_only,
        changed_paths=repository.changed_paths,
        load_red_checkpoint=load_red_checkpoint,
    )


def _migration_observation(
    project_id: int, gitlab: GitLabPort, repository: RepositoryPort
) -> MigrationObservation:
    branches, worktrees, stashes = repository.inventory()
    issues = tuple(issue.model_dump(mode="json") for issue in gitlab.list_projected())
    return MigrationObservation(
        project_id=project_id,
        issues=issues,
        branches=branches,
        worktrees=worktrees,
        stashes=stashes,
    )


def _migration_record(
    config: AgentWorkConfig,
    *,
    project_id: int,
    issue_iid: int,
    action: MigrationAction,
) -> LedgerRecord:
    """Build the deterministic ledger projection for one migration action."""
    return LedgerRecord(
        project_id=project_id,
        issue_iid=issue_iid,
        state=action.target_state,
        phase=WorkflowPhase.DISCOVERY,
        work_kind=WorkKind.ORDINARY,
        source_branch=f"agent/issue-{issue_iid}",
        worktree=config.state_root / "worktrees" / str(issue_iid),
        error_code=ErrorCode.ORPHAN_EVIDENCE,
        summary="Legacy evidence requires review.",
        next_action=action.next_action,
    )


def _is_matching_migration_record(
    existing: LedgerRecord,
    expected: LedgerRecord,
    expected_intent: TransitionIntentRecord,
) -> bool:
    """Return whether a ledger record is safe to replay for the same plan."""
    intent = existing.intent
    comparable = existing.model_copy(
        update={
            "schema_version": 1,
            "revision": 0,
            "intent": None,
            "activation_started_at": None,
            "latest_meaningful_update_at": None,
            "completed_at": None,
        }
    )
    return comparable == expected and (intent is None or intent == expected_intent)


def _migration_intent(action: MigrationAction) -> TransitionIntentRecord:
    """Build the canonical replay intent for one migration action."""
    return TransitionIntentRecord(
        operation_id=f"migration:{action.issue_iid}",
        kind="migrate_projection",
        expected_evidence={"label": label_for_state(action.target_state)},
    )


def _migration_issue_state_matches(action: MigrationAction, issue_state: str) -> bool:
    """Return whether the issue state still agrees with the reviewed action."""
    expected_state = (
        "closed" if action.target_state is RunnerState.COMPLETE else "opened"
    )
    return issue_state == expected_state


def _recovery_identity(
    gitlab: GitLabPort, config: AgentWorkConfig
) -> tuple[GitLabProject, str]:
    """Verify configured project and factory identity for recovery commands."""
    try:
        project = gitlab.verify_project()
        username = gitlab.verify_authenticated_user()
    except GitLabError as error:
        raise RuntimeError(
            "credential rotate is required; follow issue #84 before recovery"
        ) from error
    if not isinstance(username, str) or username != config.factory_username:
        raise RuntimeError(
            "credential rotate is required; follow issue #84 before recovery"
        )
    return project, username


def _recovery_candidate_report(
    candidate: RecoveryCandidate, *, priority: int
) -> RecoveryCandidateReport:
    """Render bounded, actual corroboration for one non-claiming candidate."""
    observation = candidate.observation
    evidence = {
        "ledger_revision": observation.ledger_revision,
        "source_branch": observation.source_branch,
        "source_sha": observation.source_sha,
        "worktree_exists": observation.worktree_exists,
        "worktree_head": observation.worktree_head,
        "worktree_dirty": observation.worktree_dirty,
        "worktree_changed_paths": observation.worktree_changed_paths,
        "merge_request_iid": observation.merge_request_iid,
        "merge_request_state": observation.merge_request_state,
        "merge_request_sha": observation.merge_request_sha,
        "pipeline_id": observation.pipeline_id,
        "pipeline_status": observation.pipeline_status,
        "pipeline_sha": observation.pipeline_sha,
        "split_dependencies": observation.split_dependencies,
        "controller_status": observation.controller_status,
        "owner_status": observation.owner_status,
        "blocked_cause": observation.blocked_cause,
        "retry_ineligible_reason": observation.retry_ineligible_reason,
        "stale": observation.stale,
        "ambiguous": observation.ambiguous,
        "precheckpoint_test_only_red": (
            observation.precheckpoint_test_only_red
            if observation.error_code == ErrorCode.RED_CHECKPOINT_FAILED.value
            else None
        ),
    }
    open_dependencies = tuple(
        str(iid) for iid, state in observation.split_dependencies if state == "opened"
    )
    active = _recovery_is_active(candidate)
    human_action = _recovery_requires_human_action(candidate)
    recommendation = {
        "dependency_gated": "Keep this item gated until its trusted prerequisites close.",
        "safely_retryable": "Review and explicitly choose retry for this blocked record.",
        "needs_human_decision": (
            "Use agent-work loop recover for this stale active record."
            if observation.stale
            else "Inspect the retained worktree and ledger evidence before choosing a reviewed resolution."
            if observation.retry_ineligible_reason is not None
            else "Review the contradictory evidence before choosing a resolution."
        ),
        "closure_candidate": "Review closure evidence and choose close only with a reason and link.",
        "superseded_duplicate": "Review replacement evidence before an explicit supersede decision.",
        "legitimately_waiting": "No recovery action is recommended while this evidence remains current.",
        "closed_no_action": "No recovery action is required.",
    }[candidate.classification.value]
    return RecoveryCandidateReport(
        issue_iid=candidate.issue_iid,
        classification=candidate.classification.value,
        issue_state=observation.issue_state,
        labels=observation.labels,
        record_state=observation.record_state,
        priority=priority,
        corroboration={
            key: json.dumps(value, sort_keys=True, default=str)
            for key, value in evidence.items()
            if value not in {None, (), ""}
        },
        recommendation=recommendation,
        active=active,
        human_action_required=human_action,
        blocked_cause=(
            observation.retry_ineligible_reason
            if observation.retry_ineligible_reason is not None
            else (
                observation.blocked_cause
                if (
                    observation.blocked_cause is not None
                    and (
                        candidate.classification
                        is RecoveryClassification.SAFELY_RETRYABLE
                        or observation.error_code
                        == ErrorCode.RED_CHECKPOINT_FAILED.value
                    )
                )
                else "pipeline evidence disagrees"
                if observation.pipeline_status == "failed"
                else "retained evidence requires review"
                if observation.ambiguous or observation.stale
                else None
            )
        ),
        dependency_explanation=(
            "Trusted prerequisite"
            + ("s" if len(open_dependencies) != 1 else "")
            + " #"
            + ", #".join(open_dependencies)
            + " remain open."
            if open_dependencies
            else None
        ),
        next_action=(
            "Resolve the blocker, then preview agent-work loop retry "
            f"--issue-iid {candidate.issue_iid} and apply its exact --plan-file."
            if (
                observation.blocked_cause is not None
                and candidate.classification is RecoveryClassification.SAFELY_RETRYABLE
            )
            else (
                "Inspect the retained worktree and ledger evidence for issue "
                f"#{candidate.issue_iid} before choosing a reviewed resolution."
            )
            if observation.retry_ineligible_reason is not None
            else f"Review issue #{candidate.issue_iid} before selecting one resolution."
            if human_action
            else "Monitor the active retained work; do not recover it."
            if active
            else "Wait until the named trusted prerequisites close; retry is invalid before then."
            if candidate.classification is RecoveryClassification.DEPENDENCY_GATED
            else "No recovery action is required."
            if candidate.classification is RecoveryClassification.CLOSED_NO_ACTION
            else "Continue waiting while the retained evidence remains current."
            if candidate.classification is RecoveryClassification.LEGITIMATELY_WAITING
            else f"Review issue #{candidate.issue_iid} before selecting one resolution."
        ),
    )


def _recovery_is_active(candidate: RecoveryCandidate) -> bool:
    """Return the single inventory definition of retained active work."""
    observation = candidate.observation
    return (
        observation.issue_state != "closed"
        and observation.record_state in ACTIVE_STATES
        and not observation.stale
    )


def _recovery_requires_human_action(candidate: RecoveryCandidate) -> bool:
    """Return whether this classification requires one reviewed choice."""
    return candidate.classification in {
        RecoveryClassification.SAFELY_RETRYABLE,
        RecoveryClassification.SUPERSEDED_DUPLICATE,
        RecoveryClassification.CLOSURE_CANDIDATE,
        RecoveryClassification.NEEDS_HUMAN_DECISION,
    }


def _resume_blocked_retry(
    record: LedgerRecord,
    *,
    wired: RuntimeDependencies,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    conflict_checked: bool = False,
) -> LedgerRecord:
    """Apply the one shared human-directed retry transition and projection."""
    if record.state is not RunnerState.BLOCKED:
        raise RuntimeError("only a recorded blocked issue can be retried")
    if not conflict_checked:
        _raise_for_immediate_owner_conflict(record, ledger=ledger)
    retained_publication = (
        record.intent is not None and record.intent.kind == "publish_merge_request"
    )
    if (
        retained_publication
        and record.pending_feedback is None
        and record.publication_sha is None
    ):
        if record.source_sha is None:
            raise RuntimeError("retained publication has no baseline SHA")
        publication_sha = wired.repository.bind_retained_commit(
            record.worktree, record.source_branch, record.issue_iid, record.source_sha
        )
        if record.intent is None:
            raise RuntimeError("retained publication has no exact intent")
        record = ledger.persist(
            record.model_copy(
                update={
                    "publication_sha": publication_sha,
                    "intent": record.intent.model_copy(
                        update={
                            "expected_evidence": record.intent.expected_evidence
                            | {"publication_sha": publication_sha}
                        }
                    ),
                }
            )
        )
    current = _current_condition(record)
    history = record.condition_history
    if current is not None:
        history = (
            *history,
            current.model_copy(
                update={"occurred_at": wired.clock(), "retry_count": record.retry_count}
            ),
        )[-64:]
    resumed = ledger.persist(
        transition_record(
            record,
            RunnerState.PUBLISHING if retained_publication else RunnerState.PREPARING,
            retry_count=0,
            retry_at=None,
            error_code=None,
            condition_history=history,
            summary="Human-directed retry requested.",
            next_action=(
                "Replay the retained publication intent."
                if retained_publication
                else "Reconcile and resume preparation."
            ),
        )
    )
    gitlab.transition_labels(record.issue_iid, label_for_state(resumed.state))
    return resumed


def _raise_for_immediate_owner_conflict(
    record: LedgerRecord, *, ledger: LedgerPort
) -> None:
    """Reject a retry before it can change another record's one-owner boundary."""
    conflicting_owner = next(
        (
            item
            for item in ledger.list_records()
            if item.issue_iid != record.issue_iid
            and item.state in IMMEDIATE_OWNER_STATES
        ),
        None,
    )
    if conflicting_owner is not None:
        raise RuntimeError(
            "another immediate owner exists: issue "
            f"{conflicting_owner.issue_iid}; run `agent-work loop recover "
            f"--issue-iid {conflicting_owner.issue_iid}` before retrying"
        )


def _recovery_intent(
    plan: OperationalPlan, *, choice: ResolutionChoice, target: RunnerState | None
) -> TransitionIntentRecord:
    """Bind one replayable human recovery effect to its reviewed plan fingerprint."""
    return TransitionIntentRecord(
        operation_id=f"human-recovery:{choice.value}:{plan.target_iid}:{plan.fingerprint}",
        kind=f"human_recovery_{choice.value}",
        expected_evidence={
            "plan_fingerprint": plan.fingerprint,
            "target_state": None if target is None else target.value,
            "target_label": plan.evidence.get("target_label"),
        },
    )


def _matches_recovery_intent(
    record: LedgerRecord | None, plan: OperationalPlan, choice: ResolutionChoice
) -> bool:
    """Accept only the exact retained intent for a reviewed replay."""
    if record is None or record.intent is None:
        return False
    target_label = plan.evidence.get("target_label")
    target_states = {
        "agent::blocked": RunnerState.BLOCKED,
        "agent::awaiting-human": RunnerState.AWAITING_HUMAN,
        "agent::failed": RunnerState.FAILED,
    }
    target = (
        target_states.get(target_label)
        if isinstance(target_label, str)
        else RunnerState.COMPLETE
        if choice in {ResolutionChoice.CLOSE, ResolutionChoice.SUPERSEDE}
        else RunnerState.PREPARING
        if choice is ResolutionChoice.RETRY
        else None
    )
    if (
        choice is ResolutionChoice.RETRY
        and record.state is RunnerState.EXECUTING
        and record.phase is WorkflowPhase.WORKFLOW
        and record.correction_identity is not None
        and record.correction_identity.mode == "authored_candidate_recovery"
    ):
        target = RunnerState.EXECUTING
    expected = _recovery_intent(plan, choice=choice, target=target)
    return (
        record.intent.operation_id == expected.operation_id
        and record.intent.kind == expected.kind
        and record.intent.expected_evidence == expected.expected_evidence
    )


def _dispatch_reviewed_blocked_retry(
    request: CommandRequest,
    *,
    apply: bool,
    dependencies: RuntimeDependencies,
) -> CommandReport:
    """Route a blocked loop retry through the shared reviewed recovery proof."""
    if request.issue_iid is None:
        raise RuntimeError("retry requires an issue IID")
    if apply and request.plan_file is None:
        raise RuntimeError(
            "blocked retry requires exact `agent-work recovery.resolve "
            f"--issue-iid {request.issue_iid} --choice retry` evidence: "
            "recovery apply requires --plan-file"
        )
    recovery_request = replace(
        request,
        command="recovery.resolve",
        choice=ResolutionChoice.RETRY.value,
        reason=_AUTOMATIC_BLOCKED_RETRY_REASON,
        _automatic_blocked_retry=True,
    )
    try:
        report = dispatch(recovery_request, apply=apply, dependencies=dependencies)
    except RecoveryIneligibleError as error:
        if not apply:
            project, username = _recovery_identity(
                dependencies.gitlab, dependencies.config
            )
            return _report(
                request,
                applied=False,
                outcome="retry_ineligible",
                records=(
                    (record,)
                    if (record := dependencies.ledger.load(request.issue_iid))
                    is not None
                    else ()
                ),
                project={
                    "host": dependencies.config.host,
                    "project_id": project.id,
                },
                summary="Blocked retry is ineligible.",
                next_action=(
                    "Use `agent-work recovery.resolve "
                    f"--issue-iid {request.issue_iid} --choice retry` after resolving "
                    "the evidence error."
                ),
                operational_plan=OperationalPlan(
                    command=request.command,
                    action="retry_ineligible",
                    project={
                        "host": project.host,
                        "project_id": project.id,
                        "default_branch": project.default_branch,
                    },
                    authenticated_username=username,
                    target_iid=request.issue_iid,
                    evidence={"retry_resolution": "ineligible"},
                ),
            )
        raise RuntimeError(
            "blocked retry requires exact `agent-work recovery.resolve "
            f"--issue-iid {request.issue_iid} --choice retry` evidence: {error}"
        ) from error
    correction_activated = apply and any(
        item.state is RunnerState.EXECUTING for item in report.items
    )
    return report.model_copy(
        update={
            "command": request.command,
            "outcome": (
                "correction_activated"
                if correction_activated
                else "retry_scheduled"
                if apply
                else "retry_preview"
            ),
            "summary": (
                "Reviewed retained correction activated."
                if correction_activated
                else report.summary
            ),
            "next_action": (
                "Complete correction and validation before publication."
                if correction_activated
                else report.next_action
            ),
        }
    )


def _is_reviewed_dirty_handoff_replay(
    request: CommandRequest, record: LedgerRecord, wired: RuntimeDependencies
) -> bool:
    """Route only the original handoff plan to its idempotent label replay."""
    receipt = record.interrupted_claim_receipt
    if request.plan_file is None or receipt is None:
        return False
    try:
        payload = json.loads(wired.read_plan(request.plan_file))
        if isinstance(payload, dict) and "operational_plan" in payload:
            payload = payload["operational_plan"]
        reviewed = OperationalPlan.model_validate(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        reviewed.command == "loop.recover"
        and reviewed.action == "handoff_stale_dirty_claim"
        and reviewed.target_iid == request.issue_iid == receipt.issue_iid
        and reviewed.fingerprint == receipt.plan_fingerprint
    )


def _dispatch_scope(
    request: CommandRequest, *, apply: bool, dependencies: RuntimeDependencies
) -> CommandReport:
    """Store informational evidence only in an existing matching workflow."""
    wired = dependencies
    binding: ScopeApprovalBinding | None = None

    def _report(reason: models.ScopeReason, *, applied: bool = False) -> CommandReport:
        wired.logger.info(
            "command=%s issue_iid=%s scope_reason=%s",
            request.command,
            request.issue_iid,
            reason,
        )
        return CommandReport(
            command=request.command,
            applied=applied,
            outcome=binding.state if binding is not None else "unknown",
            stop_reason=reason,
            scope_binding=binding,
            summary=f"Scope verification: {reason}",
        )

    def _read_source(bound: ScopeApprovalRequest) -> ScopeSourceObservation:
        if wired.scope_read_source is None:
            raise ScopeError("source_unavailable")
        return wired.scope_read_source(bound)

    def _identity_matches(record: LedgerRecord) -> bool:
        if (record.project_id, record.issue_iid) != (
            wired.config.project_id,
            request.issue_iid,
        ):
            return False
        retained = record.scope_binding
        return retained is None or (
            retained.request.scope.host,
            retained.request.scope.project_id,
            retained.request.scope.issue_iid,
        ) == (wired.config.host, wired.config.project_id, request.issue_iid)

    try:
        if request.issue_iid is None:
            return _report("invalid_record")
        record = wired.ledger.load_read_only(request.issue_iid)
        if record is None:
            return _report("invalid_record")
        if not _identity_matches(record):
            return _report("identity_mismatch")
        if request.command == "scope.capture":
            if request.plan_file is None:
                return _report("invalid_input")
            try:
                raw = wired.read_plan(request.plan_file)
                if len(raw) > models.SCOPE_EVIDENCE_MAX_BYTES:
                    return _report("invalid_input")
                bound = ScopeApprovalRequest.model_validate_json(raw)
            except (OSError, RuntimeError, ValueError):
                return _report("invalid_input")
            if (bound.scope.host, bound.scope.project_id, bound.scope.issue_iid) != (
                wired.config.host,
                record.project_id,
                record.issue_iid,
            ):
                binding = ScopeApprovalBinding(
                    request=bound,
                    reason="identity_mismatch",
                    scope_sha256=canonical_scope_digest(bound.scope),
                )
                return _report("identity_mismatch")
            with wired.lock() if apply else nullcontext():
                if apply:
                    current = wired.ledger.load_read_only(request.issue_iid)
                    if current is None:
                        return _report("invalid_record")
                    if not _identity_matches(current):
                        return _report("identity_mismatch")
                    if current != record:
                        return _report("invalid_record")
                binding = capture_scope(
                    bound,
                    read_source=_read_source,
                    verify_event=wired.scope_verify_event,
                )
                if binding.state == "approved":
                    updated = bind_scope_record(record, binding)
                    if apply:
                        wired.ledger.persist(updated)
                        return _report(binding.reason, applied=True)
        else:
            if record.scope_binding is None:
                return _report("invalid_record")
            binding = verify_scope(
                record.scope_binding,
                read_source=_read_source,
                verify_event=wired.scope_verify_event,
            )
        return _report(binding.reason)
    except ScopeError as error:
        if binding is not None:
            binding = ScopeApprovalBinding(
                request=binding.request,
                event=binding.event,
                state="revoked"
                if error.reason == "revoked"
                else "stale"
                if error.reason in {"stale_binding", "scope_changed"}
                else "unknown",
                reason=error.reason,
                scope_sha256=canonical_scope_digest(binding.request.scope),
            )
        return _report(error.reason)
    except (LedgerError, OSError, ValueError):
        binding = None
        return _report("invalid_record")


def dispatch(
    request: CommandRequest,
    *,
    apply: bool,
    dependencies: RuntimeDependencies | None = None,
) -> CommandReport:
    """Dispatch one command through the single composition boundary."""
    wired = dependencies or build_runtime_dependencies(debug=request.debug)
    if request.command in {"scope.capture", "scope.inspect"}:
        return _dispatch_scope(request, apply=apply, dependencies=wired)
    config = wired.config
    ledger = wired.ledger
    gitlab = wired.gitlab
    repository = wired.repository
    if request.command == "loop.retry":
        return _dispatch_reviewed_blocked_retry(
            request, apply=apply, dependencies=wired
        )
    if (
        request.command == "loop.recover"
        and request.issue_iid is not None
        and (record := ledger.load(request.issue_iid)) is not None
        and (
            (
                record.state is RunnerState.BLOCKED
                and not (
                    apply and _is_reviewed_dirty_handoff_replay(request, record, wired)
                )
            )
            or (
                apply
                and request.plan_file is not None
                and record.state in {RunnerState.PREPARING, RunnerState.EXECUTING}
                and record.intent is not None
                and record.intent.kind == "human_recovery_retry"
            )
        )
    ):
        return _dispatch_reviewed_blocked_retry(
            request, apply=apply, dependencies=wired
        )
    wired.logger.info("command=%s applied=%s", request.command, apply)
    wired.logger.debug(
        "issue_iid=%s max_activation_seconds=%s",
        request.issue_iid,
        request.max_activation_seconds,
    )
    if request.command in {"recovery.inventory", "recovery.resolve"}:
        project, recovery_username = _recovery_identity(gitlab, config)
    else:
        project = gitlab.verify_project()
    identity = {"host": config.host, "project_id": project.id}

    if request.command in {"recovery.inventory", "recovery.resolve"}:
        username = recovery_username
        if request.command == "recovery.inventory":
            dependency_inventory = gitlab.prerequisite_projection_inventory()
            prerequisite_reconciliation = prerequisite_label_reconciliation(
                dependency_inventory, project_id=project.id
            )
            prerequisite_projections = _prerequisite_projection_reports(
                prerequisite_reconciliation.projections
            )
            dependency_proofs = dict(prerequisite_reconciliation.dependency_proofs)
            records = (
                ledger.list_records()
                if request.issue_iid is None
                else (ledger.load(request.issue_iid),)
            )
            retained = tuple(record for record in records if record is not None)
            observed_issues = {issue.iid: issue for issue in dependency_inventory}
            observed_iids = {
                issue.iid
                for issue in dependency_inventory
                if set(issue.labels).intersection(LIFECYCLE_LABELS)
                or "<!-- agent-work-split:v1" in issue.description
            }
            observed_iids.update(record.issue_iid for record in retained)
            merge_requests = gitlab.open_merge_requests()
            controller = wired.process_probe()
            default_pipeline = gitlab.latest_pipeline_for_ref(project.default_branch)
            records_by_iid = {record.issue_iid: record for record in retained}
            candidate_iids = (
                observed_iids if request.issue_iid is None else {request.issue_iid}
            )
            inventory = build_inventory(
                tuple(
                    _recovery_observation(
                        records_by_iid.get(iid),
                        issue_iid=iid,
                        gitlab=gitlab,
                        issue=observed_issues[iid],
                        merge_requests=merge_requests,
                        repository=repository,
                        project=project,
                        controller_status=controller,
                        owner_status=_issue_owner_status(wired, iid),
                        split_dependencies=dependency_proofs[iid],
                        state_root=config.state_root,
                        load_red_checkpoint=wired.load_red_checkpoint,
                    )[0]
                    for iid in sorted(candidate_iids)
                )
            )
            all_candidates = inventory.candidates
            visible_candidates = tuple(
                candidate
                for candidate in all_candidates
                if request.issue_iid is not None
                or candidate.classification
                is not RecoveryClassification.CLOSED_NO_ACTION
            )
            human_action_count = sum(
                _recovery_requires_human_action(candidate)
                for candidate in all_candidates
            )
            active_count = sum(
                _recovery_is_active(candidate) for candidate in all_candidates
            )
            items = tuple(
                _item(
                    records_by_iid[candidate.issue_iid],
                    corroboration={
                        "classification": candidate.classification.value,
                        "issue_label": next(
                            iter(observed_issues[candidate.issue_iid].labels), ""
                        ),
                        "controller": controller,
                        "owner_status": candidate.observation.owner_status,
                        "pipeline_sha": next(
                            (
                                mr.sha
                                for mr in merge_requests
                                if mr.source_branch
                                == records_by_iid[candidate.issue_iid].source_branch
                            ),
                            "",
                        ),
                    },
                )
                for candidate in visible_candidates
                if candidate.issue_iid in records_by_iid
            )
            return CommandReport(
                schema_version=2,
                command=request.command,
                applied=False,
                outcome="inventory",
                project=identity,
                items=items,
                total_items=len(all_candidates),
                items_truncated=len(visible_candidates) != len(all_candidates),
                summary="Read-only retained recovery inventory.",
                next_action=(
                    "Review one exact action-required issue."
                    if human_action_count
                    else "Monitor active retained work."
                    if active_count
                    else "Wait for trusted dependencies to close."
                    if any(
                        candidate.classification
                        is RecoveryClassification.DEPENDENCY_GATED
                        for candidate in all_candidates
                    )
                    else "No recovery action is required."
                ),
                operational_plan=OperationalPlan(
                    command=request.command,
                    action="inventory",
                    project={
                        "host": config.host,
                        "project_id": project.id,
                        "default_branch": project.default_branch,
                    },
                    authenticated_username=username,
                    evidence={
                        "default_pipeline_id": None
                        if default_pipeline is None
                        else default_pipeline.id,
                        "default_pipeline_status": None
                        if default_pipeline is None
                        else default_pipeline.status,
                        "default_pipeline_sha": None
                        if default_pipeline is None
                        else default_pipeline.sha,
                        "controller_status": controller,
                        "fresh_work_selection": False,
                    },
                ),
                recovery_summary=RecoverySummary(
                    total=len(all_candidates),
                    open=sum(
                        candidate.observation.issue_state != "closed"
                        for candidate in all_candidates
                    ),
                    closed=sum(
                        candidate.observation.issue_state == "closed"
                        for candidate in all_candidates
                    ),
                    active=active_count,
                    blocked=sum(
                        candidate.observation.record_state is RunnerState.BLOCKED
                        for candidate in all_candidates
                    ),
                    dependency_gated=sum(
                        candidate.classification
                        is RecoveryClassification.DEPENDENCY_GATED
                        for candidate in all_candidates
                    ),
                    human_action=human_action_count,
                    no_action=len(all_candidates) - human_action_count,
                ),
                recovery_candidates=tuple(
                    _recovery_candidate_report(candidate, priority=position)
                    for position, candidate in enumerate(visible_candidates)
                ),
                prerequisite_projections=prerequisite_projections,
            )
        record = ledger.load(request.issue_iid or 0)
        choice = ResolutionChoice(request.choice or "")
        if record is None and choice is not ResolutionChoice.REOPEN:
            raise RuntimeError("recovery issue has no retained record")
        snapshot_dependency_inventory = gitlab.prerequisite_projection_inventory()
        snapshot_issues = {issue.iid: issue for issue in snapshot_dependency_inventory}
        try:
            snapshot_issue = snapshot_issues[request.issue_iid or 0]
        except KeyError as error:
            raise RuntimeError(
                "recovery target is missing from issue inventory"
            ) from error
        snapshot_mrs = gitlab.open_merge_requests()
        snapshot_controller = wired.process_probe()
        reviewed: OperationalPlan | None = None
        if apply:
            if request.plan_file is None:
                raise RuntimeError("recovery apply requires --plan-file")
            try:
                payload = json.loads(wired.read_plan(request.plan_file))
                reviewed = OperationalPlan.model_validate(
                    payload.get("operational_plan", payload)
                )
            except (
                AttributeError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise RuntimeError("recovery plan is invalid") from error
        replaying = reviewed is not None and _matches_recovery_intent(
            record, reviewed, choice
        )
        if replaying:
            observation, _ = _recovery_observation(
                record,
                issue_iid=request.issue_iid or 0,
                gitlab=gitlab,
                repository=repository,
                project=project,
                controller_status=snapshot_controller,
                owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                issue=snapshot_issue,
                merge_requests=snapshot_mrs,
                split_dependencies=_recovery_split_dependencies(
                    request.issue_iid or 0,
                    project=project,
                    issues=snapshot_dependency_inventory,
                ),
                state_root=config.state_root,
            )
            plan = reviewed
        else:
            try:
                plan, observation = _recovery_plan(
                    request,
                    config=config,
                    project=project,
                    username=username,
                    record=record,
                    gitlab=gitlab,
                    repository=repository,
                    controller_status=snapshot_controller,
                    owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                    issue=snapshot_issue,
                    dependency_inventory=snapshot_dependency_inventory,
                    merge_requests=snapshot_mrs,
                    load_red_checkpoint=wired.load_red_checkpoint,
                )
            except RecoveryIneligibleError as error:
                if request._automatic_blocked_retry:
                    raise
                if apply and reviewed is not None:
                    raise RuntimeError(
                        "recovery evidence changed; create a new preview"
                    ) from error
                raise
            except RuntimeError as error:
                if apply and reviewed is not None:
                    raise RuntimeError(
                        "recovery evidence changed; create a new preview"
                    ) from error
                raise
        if not apply:
            return _report(
                request,
                applied=False,
                outcome="planned",
                records=() if record is None else (record,),
                project=identity,
                summary="Read-only reviewed recovery plan.",
                next_action="Review the plan, then apply its exact plan file.",
                operational_plan=plan,
            )
        if reviewed is None:
            raise RuntimeError("recovery plan is invalid")
        with wired.lock():
            current_project, current_username = _recovery_identity(gitlab, config)
            if (current_project, current_username) != (project, username):
                raise RuntimeError(
                    "credential or project evidence changed; create a new preview"
                )
            current_record = ledger.load(request.issue_iid or 0)
            if current_record is None and choice is not ResolutionChoice.REOPEN:
                raise RuntimeError("recovery record disappeared")
            current_dependency_inventory = gitlab.prerequisite_projection_inventory()
            current_issues = {
                issue.iid: issue for issue in current_dependency_inventory
            }
            try:
                current_issue = current_issues[request.issue_iid or 0]
            except KeyError as error:
                raise RuntimeError(
                    "recovery evidence changed; create a new preview"
                ) from error
            current_mrs = gitlab.open_merge_requests()
            # The lock owner becomes this process after acquisition; retain the
            # pre-acquisition probe so the lock cannot make its own plan drift.
            current_controller = snapshot_controller
            replaying = _matches_recovery_intent(current_record, reviewed, choice)
            blocked_automatic_replay = (
                replaying
                and request._automatic_blocked_retry
                and current_record is not None
                and current_record.state is RunnerState.BLOCKED
            )
            if replaying and not blocked_automatic_replay:
                current_observation, _ = _recovery_observation(
                    current_record,
                    issue_iid=request.issue_iid or 0,
                    gitlab=gitlab,
                    repository=repository,
                    project=project,
                    controller_status=current_controller,
                    owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                    issue=current_issue,
                    merge_requests=current_mrs,
                    split_dependencies=_recovery_split_dependencies(
                        request.issue_iid or 0,
                        project=project,
                        issues=current_dependency_inventory,
                    ),
                    state_root=config.state_root,
                )
                current_plan = reviewed
            else:
                plan_record = current_record
                if blocked_automatic_replay and current_record is not None:
                    reviewed_revision = reviewed.evidence.get("ledger_revision")
                    if (
                        type(reviewed_revision) is not int
                        or current_record.revision != reviewed_revision + 1
                    ):
                        raise RuntimeError(
                            "recovery evidence changed; create a new preview"
                        )
                    plan_record = current_record.model_copy(
                        update={"revision": reviewed_revision, "intent": None}
                    )
                try:
                    current_plan, current_observation = _recovery_plan(
                        request,
                        config=config,
                        project=project,
                        username=username,
                        record=plan_record,
                        gitlab=gitlab,
                        repository=repository,
                        controller_status=current_controller,
                        owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                        issue=current_issue,
                        dependency_inventory=current_dependency_inventory,
                        merge_requests=current_mrs,
                        load_red_checkpoint=wired.load_red_checkpoint,
                    )
                except RuntimeError as error:
                    raise RuntimeError(
                        "recovery evidence changed; create a new preview"
                    ) from error
            if (
                choice is ResolutionChoice.RETRY
                and request._automatic_blocked_retry
                and not (
                    replaying
                    and current_record is not None
                    and current_record.state
                    in {RunnerState.PREPARING, RunnerState.EXECUTING}
                )
            ):
                _require_safely_retryable_automatic_retry(current_observation)
            if (not replaying or blocked_automatic_replay) and current_plan != reviewed:
                raise RuntimeError("recovery evidence changed; create a new preview")
            if not replaying:
                try:
                    verify_resolution_plan(
                        build_resolution_plan(
                            ResolutionRequest(
                                request.issue_iid or 0,
                                ResolutionChoice(request.choice or ""),
                                request.reason or "",
                                request.evidence_urls,
                                request.replacement_issue_iid,
                                request.target_label,
                            ),
                            observation,
                        ),
                        current_observation,
                    )
                except EvidenceDriftError as error:
                    raise RuntimeError(str(error)) from error
            before_state = gitlab.fetch_issue(request.issue_iid or 0).state
            if (
                choice is ResolutionChoice.RETRY
                and replaying
                and current_record is not None
                and current_record.state is RunnerState.EXECUTING
                and current_record.phase is WorkflowPhase.WORKFLOW
                and current_record.correction_identity is not None
                and current_record.correction_identity.mode
                == "authored_candidate_recovery"
            ):
                checkpoint = (
                    None
                    if wired.load_red_checkpoint is None
                    else wired.load_red_checkpoint(
                        current_record.project_id,
                        current_record.issue_iid,
                        current_record.correction_identity.baseline_sha,
                    )
                )
                if not correction_identity_matches(
                    current_record,
                    repository.observe_legacy_publication_candidate(
                        current_record.worktree
                    ),
                    checkpoint=checkpoint,
                    original_base_sha=repository.single_parent(
                        current_record.worktree, current_record.source_sha or ""
                    ),
                ):
                    raise RuntimeError("correction recovery evidence changed")
                return CommandReport(
                    command=request.command,
                    applied=True,
                    outcome="applied",
                    project=identity,
                    items=(
                        _item(
                            current_record,
                            corroboration={
                                "before_state": before_state,
                                "after_state": before_state,
                            },
                        ),
                    ),
                    operational_plan=reviewed,
                    summary="Reviewed authored-candidate recovery was already applied.",
                    next_action="Continue complete correction and validation.",
                )
            if choice is ResolutionChoice.RETRY:
                if current_record is None:
                    raise RuntimeError("recovery record disappeared")
                _raise_for_immediate_owner_conflict(current_record, ledger=ledger)
                if reviewed.evidence.get("discovery_marker_reclassification") is True:
                    if replaying and current_record.state is RunnerState.PREPARING:
                        if not _discovery_marker_replay_is_exact(
                            current_record,
                            reviewed,
                            issue=current_issue,
                            project=project,
                            repository=repository,
                            gitlab=gitlab,
                            owner_status=_issue_owner_status(
                                wired, request.issue_iid or 0
                            ),
                        ):
                            raise RuntimeError(
                                "recovery evidence changed; create a new preview"
                            )
                        updated = current_record
                    else:
                        prepared = ledger.persist(
                            current_record.model_copy(
                                update={
                                    "intent": _recovery_intent(
                                        reviewed,
                                        choice=choice,
                                        target=RunnerState.PREPARING,
                                    )
                                }
                            )
                        )
                        rebuilt = new_record(
                            current_issue,
                            project_id=project.id,
                            state_root=config.state_root,
                            main_branch=repository.config.default_branch,
                        ).model_copy(
                            update={
                                # Reclassification replaces only the empty discovery
                                # projection.  Keep the durable revision sequence and
                                # worktree location, plus replay evidence until the
                                # GitLab projection has completed.
                                "revision": prepared.revision,
                                "worktree": prepared.worktree,
                                "intent": _recovery_intent(
                                    reviewed,
                                    choice=choice,
                                    target=RunnerState.PREPARING,
                                ),
                            }
                        )
                        updated = ledger.persist(rebuilt)
                    gitlab.transition_labels(current_record.issue_iid, "agent::running")
                    if updated.intent is not None:
                        updated = ledger.persist(
                            updated.model_copy(update={"intent": None})
                        )
                    return CommandReport(
                        command=request.command,
                        applied=True,
                        outcome="applied",
                        project=identity,
                        items=(
                            _item(
                                updated,
                                corroboration={
                                    "before_state": before_state,
                                    "after_state": gitlab.fetch_issue(
                                        request.issue_iid or 0
                                    ).state,
                                },
                            ),
                        ),
                        operational_plan=reviewed,
                        summary="Reviewed discovery marker reclassification applied.",
                        next_action="Prepare the newly classified durable worktree.",
                    )
                retained_publication = (
                    current_record.intent is not None
                    and current_record.intent.kind == "publish_merge_request"
                )
                legacy_dirty_publication = (
                    retained_publication
                    and "legacy_index_fingerprint" in reviewed.evidence
                )
                precheckpoint_test_only_red = (
                    reviewed.evidence.get("precheckpoint_test_only_red") is True
                )
                correction_snapshot = reviewed.evidence.get("correction_identity")
                if legacy_dirty_publication or correction_snapshot is not None:
                    if not isinstance(correction_snapshot, str):
                        raise RuntimeError("reviewed correction identity is invalid")
                    try:
                        correction_identity = (
                            PublicationCorrectionIdentity.model_validate_json(
                                correction_snapshot
                            )
                        )
                    except ValueError as error:
                        raise RuntimeError(
                            "reviewed correction identity is invalid"
                        ) from error
                    if correction_identity.workflow_lane is WorkflowLane.CONTENT:
                        checkpoint = None
                    elif wired.load_red_checkpoint is None:
                        raise RuntimeError(
                            "correction recovery Red evidence is unavailable"
                        )
                    else:
                        checkpoint_base = (
                            correction_identity.baseline_sha
                            if correction_identity.mode == "authored_candidate_recovery"
                            else current_record.source_sha or ""
                        )
                        checkpoint = wired.load_red_checkpoint(
                            current_record.project_id,
                            current_record.issue_iid,
                            checkpoint_base,
                        )
                    if (
                        not correction_identity_matches(
                            current_record,
                            repository.observe_legacy_publication_candidate(
                                current_record.worktree
                            ),
                            checkpoint=checkpoint,
                            original_base_sha=(
                                repository.single_parent(
                                    current_record.worktree,
                                    current_record.source_sha or "",
                                )
                                if correction_identity.mode
                                == "authored_candidate_recovery"
                                else None
                            ),
                        )
                        and current_record.correction_identity is not None
                    ):
                        raise RuntimeError("correction recovery evidence changed")
                    # The reviewed snapshot may establish the narrowly retained
                    # pre-snapshot record; subsequent retries compare it above.
                    updated = ledger.persist(
                        transition_record(
                            current_record,
                            RunnerState.EXECUTING,
                            phase=WorkflowPhase.WORKFLOW,
                            intent=(
                                _recovery_intent(
                                    reviewed,
                                    choice=choice,
                                    target=RunnerState.EXECUTING,
                                )
                                if correction_identity.mode
                                == "authored_candidate_recovery"
                                else None
                            ),
                            publication_sha=None,
                            merge_request_draft=None,
                            worker_exit=None,
                            retry_at=None,
                            retry_count=0,
                            correction_identity=correction_identity,
                            error_code=ErrorCode.PUBLICATION_COMMIT_REJECTED,
                            summary=(
                                "The retained publication candidate requires "
                                "complete correction and validation."
                            ),
                            next_action=(
                                "Correct the retained change, then rerun the complete "
                                "workflow and validation before publication."
                            ),
                        )
                    )
                    gitlab.transition_labels(current_record.issue_iid, "agent::running")
                    continue_recovery = False
                elif precheckpoint_test_only_red:
                    updated = _resume_blocked_retry(
                        current_record,
                        wired=wired,
                        ledger=ledger,
                        gitlab=gitlab,
                        conflict_checked=True,
                    )
                    continue_recovery = False
                elif retained_publication:
                    updated = _resume_blocked_retry(
                        current_record,
                        wired=wired,
                        ledger=ledger,
                        gitlab=gitlab,
                        conflict_checked=True,
                    )
                    # The publication intent is deliberately retained for its
                    # existing replay path; recovery never overwrites it.
                    continue_recovery = False
                else:
                    continue_recovery = True
                if (
                    continue_recovery
                    and replaying
                    and current_record.state is RunnerState.PREPARING
                ):
                    gitlab.transition_labels(current_record.issue_iid, "agent::running")
                    updated = ledger.persist(
                        current_record.model_copy(update={"intent": None})
                    )
                elif continue_recovery:
                    pending = (
                        current_record
                        if replaying
                        else ledger.persist(
                            current_record.model_copy(
                                update={
                                    "intent": _recovery_intent(
                                        reviewed,
                                        choice=choice,
                                        target=RunnerState.PREPARING,
                                    )
                                }
                            )
                        )
                    )
                    updated = ledger.persist(
                        _resume_blocked_retry(
                            pending,
                            wired=wired,
                            ledger=ledger,
                            gitlab=gitlab,
                            conflict_checked=True,
                        ).model_copy(update={"intent": None})
                    )
            elif choice in {ResolutionChoice.CLOSE, ResolutionChoice.SUPERSEDE}:
                if current_record is None:
                    raise RuntimeError("close requires a retained recovery record")
                completed = (
                    current_record.model_copy(update={"intent": None})
                    if current_record.state is RunnerState.COMPLETE
                    else transition_record(
                        current_record,
                        RunnerState.COMPLETE,
                        error_code=None,
                        summary="Explicit human recovery closure approved.",
                        next_action="Closure evidence has been recorded.",
                        intent=None,
                    )
                )
                if not replaying:
                    ledger.persist(
                        current_record.model_copy(
                            update={
                                "intent": _recovery_intent(
                                    reviewed,
                                    choice=choice,
                                    target=RunnerState.COMPLETE,
                                )
                            }
                        )
                    )
                replacement = (
                    ""
                    if request.replacement_issue_iid is None
                    else f" Replacement issue: #{request.replacement_issue_iid}."
                )
                if not replaying:
                    gitlab.comment_outcome(
                        request.issue_iid or 0,
                        error_code="human_recovery",
                        summary=(
                            f"{request.reason}{replacement} "
                            f"{' '.join(request.evidence_urls)}"
                        ).strip(),
                        next_action="Closure was explicitly reviewed.",
                    )
                gitlab.close_issue(request.issue_iid or 0)
                gitlab.transition_labels(request.issue_iid or 0, None)
                updated = ledger.persist(completed)
            elif choice is ResolutionChoice.RELABEL:
                if current_record is None:
                    raise RuntimeError("relabel requires a retained recovery record")
                target_states = {
                    "agent::blocked": RunnerState.BLOCKED,
                    "agent::awaiting-human": RunnerState.AWAITING_HUMAN,
                    "agent::failed": RunnerState.FAILED,
                }
                reviewed_label = reviewed.evidence.get("target_label")
                if not isinstance(reviewed_label, str):
                    raise RuntimeError("reviewed relabel target is invalid")
                target_state = target_states.get(reviewed_label)
                if target_state is None:
                    raise RuntimeError("reviewed relabel target is invalid")
                transitioned = (
                    current_record
                    if target_state is None or target_state is current_record.state
                    else transition_record(
                        current_record,
                        target_state,
                        error_code=current_record.error_code,
                        summary="Explicit human lifecycle relabel approved.",
                        next_action="Follow the reviewed lifecycle decision.",
                    )
                )
                if not replaying:
                    ledger.persist(
                        current_record.model_copy(
                            update={
                                "intent": _recovery_intent(
                                    reviewed, choice=choice, target=target_state
                                )
                            }
                        )
                    )
                gitlab.transition_labels(request.issue_iid or 0, reviewed_label)
                updated = ledger.persist(
                    transitioned.model_copy(update={"intent": None})
                )
            else:
                gitlab.reopen_issue(request.issue_iid or 0)
                updated = current_record
            after_state = (
                "closed"
                if choice in {ResolutionChoice.CLOSE, ResolutionChoice.SUPERSEDE}
                else gitlab.fetch_issue(request.issue_iid or 0).state
            )
            return CommandReport(
                command=request.command,
                applied=True,
                outcome="applied",
                project=identity,
                items=(
                    _item(
                        updated,
                        corroboration={
                            "before_state": before_state,
                            "after_state": after_state,
                        },
                    ),
                )
                if updated is not None
                else (),
                operational_plan=reviewed,
                summary="Reviewed human recovery decision applied.",
                next_action="Review the bounded before-and-after receipt.",
            )

    if request.command in {"loop.canary", "loop.recover"}:
        username = gitlab.verify_authenticated_user()
        if not isinstance(username, str) or not username:
            raise RuntimeError("authenticated identity evidence is unavailable")
        if not apply:
            return _operational_preview(
                request, wired=wired, project=project, username=username
            )
        if request.plan_file is None:
            raise RuntimeError("operational apply requires --plan-file")
        try:
            payload = json.loads(wired.read_plan(request.plan_file))
            if isinstance(payload, dict) and "operational_plan" in payload:
                payload = payload["operational_plan"]
            reviewed = OperationalPlan.model_validate(payload)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("operational plan is invalid") from error
        if reviewed.command != request.command or (
            request.command == "loop.recover"
            and reviewed.target_iid != request.issue_iid
        ):
            raise RuntimeError("operational plan does not match the requested command")
        with wired.lock():
            current_project = gitlab.verify_project()
            if current_project != project:
                raise RuntimeError(
                    "configured project evidence changed; create a new preview"
                )
            current_username = gitlab.verify_authenticated_user()
            if not isinstance(current_username, str) or not current_username:
                raise RuntimeError("authenticated identity evidence is unavailable")
            if reviewed.action == "handoff_stale_dirty_claim":
                target = reviewed.target_iid
                if target is None:
                    raise RuntimeError("dirty handoff target is missing")
                if (
                    reviewed.project
                    != {
                        "host": config.host,
                        "project_id": current_project.id,
                        "default_branch": current_project.default_branch,
                    }
                    or reviewed.authenticated_username != current_username
                ):
                    raise RuntimeError("dirty handoff identity evidence changed")
                target_owner_status = _issue_owner_status(wired, target)
                if target_owner_status not in {"stale", "missing"}:
                    raise RuntimeError("dirty handoff owner evidence changed")
                with wired.issue_lock(target):
                    retained = ledger.load(target)
                    if retained is None:
                        raise RuntimeError("dirty handoff record disappeared")
                    receipt = retained.interrupted_claim_receipt
                    if receipt is not None:
                        if wired.load_red_checkpoint is None:
                            raise RuntimeError("dirty handoff Red evidence unavailable")
                        candidate = repository.observe_legacy_publication_candidate(
                            retained.worktree
                        )
                        repository.verify_worktree(
                            retained.worktree,
                            retained.source_branch,
                            retained.source_sha or "",
                        )
                        checkpoint = wired.load_red_checkpoint(
                            retained.project_id,
                            target,
                            retained.source_sha or "",
                        )
                        issue = gitlab.fetch_issue(target)
                        if (
                            retained.state is not RunnerState.BLOCKED
                            or retained.phase is not WorkflowPhase.WORKFLOW
                            or retained.project_id != current_project.id
                            or retained.revision
                            != int(reviewed.evidence.get("record_revision") or -1) + 1
                            or retained.source_branch
                            != reviewed.evidence.get("source_branch")
                            or retained.source_sha
                            != reviewed.evidence.get("source_sha")
                            or receipt.issue_iid != target
                            or receipt.plan_fingerprint != reviewed.fingerprint
                            or receipt.blocked_record_fingerprint
                            != _handoff_record_fingerprint(retained)
                            or receipt.dirty_snapshot_fingerprint
                            != repository.retained_worktree_fingerprint(
                                retained.worktree
                            )
                            or candidate.index_fingerprint
                            != reviewed.evidence.get("dirty_index_fingerprint")
                            or candidate.branch != reviewed.evidence.get("dirty_branch")
                            or candidate.head != reviewed.evidence.get("dirty_head")
                            or candidate.merge_head is not None
                            or candidate.has_untracked
                            or candidate.has_unmerged
                            or candidate.has_renames
                            or json.dumps(candidate.staged_paths)
                            != reviewed.evidence.get("dirty_staged_paths")
                            or json.dumps(candidate.unstaged_paths)
                            != reviewed.evidence.get("dirty_unstaged_paths")
                            or json.dumps(repository.changed_paths(retained.worktree))
                            != reviewed.evidence.get("dirty_changed_paths")
                            or (
                                "absent"
                                if checkpoint is None
                                else hashlib.sha256(
                                    checkpoint.model_dump_json().encode()
                                ).hexdigest()
                            )
                            != reviewed.evidence.get("dirty_red_fingerprint")
                            or f"{target}={target_owner_status}"
                            not in str(reviewed.evidence.get("owner_states", "")).split(
                                ","
                            )
                            or issue.state != "opened"
                            or set(issue.labels).intersection(LIFECYCLE_LABELS)
                            not in ({"agent::running"}, {"agent::blocked"})
                            or gitlab.open_merge_requests_by_branch(
                                retained.source_branch
                            )
                        ):
                            raise RuntimeError("dirty handoff replay evidence changed")
                        gitlab.transition_labels(
                            target, label_for_state(retained.state)
                        )
                        return _report(
                            request,
                            applied=True,
                            outcome="dirty_claim_handed_off",
                            records=(retained,),
                            project=identity,
                            operational_plan=reviewed,
                        )
                    current = _operational_preview(
                        request,
                        wired=wired,
                        project=current_project,
                        username=current_username,
                        target_owner_status=target_owner_status,
                    )
                    if current.operational_plan != reviewed:
                        raise RuntimeError(
                            "dirty handoff evidence changed; create a new preview"
                        )
                    blocked_record = transition_record(
                        retained,
                        RunnerState.BLOCKED,
                        error_code=ErrorCode.STATE_RECONCILIATION_FAILED,
                        summary="Interrupted dirty claim retained for manual recovery.",
                        next_action=f"Review retained issue {target} and its dirty checkout before a new recovery decision.",
                    )
                    blocked = ledger.persist(
                        blocked_record.model_copy(
                            update={
                                "interrupted_claim_receipt": InterruptedClaimReceipt(
                                    issue_iid=target,
                                    plan_fingerprint=reviewed.fingerprint,
                                    dirty_snapshot_fingerprint=str(
                                        reviewed.evidence["dirty_snapshot_fingerprint"]
                                    ),
                                    blocked_record_fingerprint=_handoff_record_fingerprint(
                                        blocked_record
                                    ),
                                )
                            }
                        )
                    )
                    gitlab.transition_labels(target, label_for_state(blocked.state))
                    return _report(
                        request,
                        applied=True,
                        outcome="dirty_claim_handed_off",
                        records=(blocked,),
                        project=identity,
                        operational_plan=reviewed,
                    )
            current = _operational_preview(
                request, wired=wired, project=current_project, username=current_username
            )
            if current.operational_plan != reviewed:
                if request.command == "loop.recover" and request.issue_iid is not None:
                    retained = ledger.load(request.issue_iid)
                    if retained is not None and retained.state in {
                        RunnerState.NEEDS_REPAIR,
                        RunnerState.REPAIR_FAILED,
                    }:
                        repair_attempt = RepairAttemptEvidence(
                            fingerprint=reviewed.fingerprint,
                            snapshot=_repair_evidence_copy(reviewed.evidence),
                            origin="reviewed-plan",
                            evidence_delta={"current": "changed"},
                            failure_reason="reviewed repair plan evidence changed",
                            started_at=wired.clock().isoformat(),
                            elapsed_seconds=0.0,
                        )
                        failed = ledger.persist(
                            retained.model_copy(
                                update={"repair_attempt": repair_attempt}
                            )
                            if retained.state is RunnerState.REPAIR_FAILED
                            else transition_record(
                                retained,
                                RunnerState.REPAIR_FAILED,
                                phase=WorkflowPhase.POST_PUBLICATION,
                                repair_attempt=repair_attempt,
                            )
                        )
                        gitlab.transition_labels(
                            failed.issue_iid, label_for_state(failed.state)
                        )
                raise RuntimeError(
                    "operational plan evidence changed; create a new preview"
                )
            if reviewed.action == "checkpoint":
                return current.model_copy(update={"applied": True})
            if reviewed.action == "provision_lifecycle_labels":
                wired.ensure_lifecycle_labels()
                return _report(
                    request,
                    applied=True,
                    outcome="lifecycle_labels_provisioned",
                    project=identity,
                    summary="Fixed lifecycle labels were provisioned.",
                    next_action="Run a new read-only canary checkpoint.",
                    operational_plan=reviewed,
                )
            if reviewed.action == "target_missing":
                raise RuntimeError("operational recovery target is absent")
            if reviewed.action in {
                "pipeline_blocked",
                "pipeline_wait",
                "pipeline_inventory_blocked",
            }:
                raise RuntimeError("operational checkpoint cannot advance")
            target = reviewed.target_iid
            if target is None:
                raise RuntimeError("operational plan target is missing")
            record = ledger.load(target)
            if record is None:
                raise RuntimeError("operational plan record disappeared")
            if reviewed.action == "normalize_interrupted_claim":
                if not _normalization_is_exact(
                    record, gitlab=gitlab, repository=repository
                ):
                    raise RuntimeError("normalization evidence changed")
                repository.verify_worktree(
                    record.worktree, record.source_branch, record.source_sha or ""
                )
                blocked = ledger.persist(
                    transition_record(
                        record,
                        RunnerState.BLOCKED,
                        main_sha=record.source_sha,
                        error_code=ErrorCode.STATE_RECONCILIATION_FAILED,
                        summary="Interrupted pre-worker claim retained for exact recovery.",
                        next_action=f"Review and explicitly recover retained issue {target}.",
                    )
                )
                gitlab.transition_labels(target, label_for_state(blocked.state))
                return _report(
                    request,
                    applied=True,
                    outcome="normalized",
                    records=(blocked,),
                    project=identity,
                    operational_plan=reviewed,
                )
            if reviewed.action in {"run_segment", "repair"}:
                wired.invoke.preflight()
                if record.state is RunnerState.BLOCKED:
                    record = ledger.persist(
                        transition_record(
                            record,
                            RunnerState.PREPARING,
                            error_code=None,
                            summary="Explicit exact-IID recovery resumed.",
                            next_action="Reconcile and resume preparation.",
                        )
                    )
                    gitlab.transition_labels(target, label_for_state(record.state))
                elif record.state in {
                    RunnerState.NEEDS_REPAIR,
                    RunnerState.REPAIR_FAILED,
                }:
                    record = ledger.persist(
                        transition_record(
                            record,
                            RunnerState.REPAIR,
                            phase=WorkflowPhase.WORKFLOW,
                            error_code=ErrorCode.PIPELINE_REJECTED,
                            repair_attempt=RepairAttemptEvidence(
                                fingerprint=reviewed.fingerprint,
                                snapshot=_repair_evidence_copy(reviewed.evidence),
                                origin="reviewed-plan",
                                evidence_delta={},
                                failure_reason="",
                                started_at=wired.clock().isoformat(),
                                elapsed_seconds=0.0,
                            ),
                            summary="Exact-IID reviewed repair activated.",
                            next_action="Run the repair workflow.",
                        )
                    )
                    gitlab.transition_labels(target, label_for_state(record.state))
                    return _report(
                        request,
                        applied=True,
                        outcome="activation_complete",
                        records=(record,),
                        project=identity,
                        operational_plan=reviewed,
                    )
                started = wired.monotonic()
                results: list[LedgerRecord] = []
                for _segment_number in range(request.max_segments):
                    remaining = request.max_activation_seconds - (
                        wired.monotonic() - started
                    )
                    if remaining <= 0:
                        break
                    result = wired.run_segment_boundary(
                        ledger=ledger,
                        gitlab=gitlab,
                        repository=repository,
                        invoke=wired.invoke,
                        timeout_seconds=request.max_activation_seconds,
                        now=wired.clock(),
                        clock=wired.clock,
                        observe_stage_identity=wired.observe_stage_identity,
                        issue_iid=target,
                        allow_pinned_conflict=True,
                        issue_lock=wired.issue_lock,
                        load_red_checkpoint=wired.load_red_checkpoint,
                        apply_red_checkpoint=wired.apply_red_checkpoint,
                        remaining_seconds=lambda: (
                            request.max_activation_seconds
                            - (wired.monotonic() - started)
                        ),
                    )
                    if result is None:
                        break
                    results.append(result)
                    if result.state not in ACTIVE_STATES:
                        break
                    if result.state is RunnerState.OBSERVING:
                        break
                return _report(
                    request,
                    applied=True,
                    outcome="activation_complete",
                    records=tuple(results),
                    project=identity,
                    operational_plan=reviewed,
                )
            if reviewed.action == "normalization_unverified":
                raise RuntimeError("normalization evidence cannot be proven")
            raise RuntimeError("operational plan action is unsupported")

    if request.command == "loop.status":
        records = (
            (() if (record := ledger.load(request.issue_iid)) is None else (record,))
            if request.issue_iid is not None
            else ledger.list_records()
        )
        selected = tuple(records)
        ordered = tuple(sorted(selected, key=lambda record: record.issue_iid))
        shown = ordered if request.issue_iid is not None else ordered[:100]
        try:
            process = wired.process_probe()
        except (OSError, RuntimeError, ValueError):
            process = "unknown"
        report = CommandReport(
            schema_version=2,
            command=request.command,
            applied=False,
            outcome="status",
            project=identity,
            items=tuple(
                _item(
                    record,
                    corroboration=_status_corroboration(
                        record,
                        gitlab=gitlab,
                        repository=repository,
                        clock=wired.clock,
                        process_probe=lambda: process,
                        issue_owner_probe=wired.issue_owner_probe,
                    )
                    if request.issue_iid is not None
                    else {
                        "gitlab": "unknown",
                        "worktree": "unknown",
                        "process": process,
                        "owner_status": _issue_owner_status(wired, record.issue_iid),
                    },
                )
                for record in shown
            ),
        )
        return report.model_copy(
            update={
                "schema_version": 2,
                "total_items": len(selected),
                "items_truncated": request.issue_iid is None and len(selected) > 100,
            }
        )

    if request.command == "migration.preview":
        plan = build_migration_plan(
            _migration_observation(project.id, gitlab, repository)
        )
        return _report(
            request,
            applied=False,
            outcome="preview",
            actions=plan.actions,
            project=identity,
        )

    if request.command == "migration.apply":
        if not apply or request.plan_file is None:
            raise RuntimeError("migration apply requires --apply and --plan-file")
        raw_plan = json.loads(wired.read_plan(request.plan_file))
        if (
            isinstance(raw_plan, dict)
            and raw_plan.get("command") == "migration.preview"
        ):
            project_data = raw_plan.get("project")
            if not isinstance(project_data, dict):
                raise RuntimeError("migration preview lacks project identity")
            raw_plan = {
                "schema_version": raw_plan.get("schema_version"),
                "project_id": project_data.get("project_id"),
                "actions": raw_plan.get("actions", []),
            }
        plan = MigrationPlan.model_validate(raw_plan)
        if plan.project_id != project.id:
            raise RuntimeError("migration plan project does not match")
        with wired.lock():
            current = build_migration_plan(
                _migration_observation(project.id, gitlab, repository)
            )
            current_actions = {action.issue_iid: action for action in current.actions}
            approved_actions = {action.issue_iid: action for action in plan.actions}
            if any(
                approved_actions.get(iid) != action
                for iid, action in current_actions.items()
            ):
                raise RuntimeError("migration evidence changed; create a new preview")
            issues = {
                action.issue_iid: gitlab.fetch_issue(action.issue_iid)
                for action in plan.actions
            }
            expected_records = {
                action.issue_iid: _migration_record(
                    config,
                    project_id=project.id,
                    issue_iid=action.issue_iid,
                    action=action,
                )
                for action in plan.actions
            }
            existing_records = {
                action.issue_iid: ledger.load(action.issue_iid)
                for action in plan.actions
            }
            for action in plan.actions:
                current_action = current_actions.get(action.issue_iid)
                issue = issues[action.issue_iid]
                if not _migration_issue_state_matches(action, issue.state):
                    raise RuntimeError(
                        "migration evidence changed; create a new preview"
                    )
                expected_label = label_for_state(action.target_state)
                projected = set(issue.labels).intersection(LIFECYCLE_LABELS)
                existing = existing_records[action.issue_iid]
                expected_intent = _migration_intent(action)
                if existing is not None and not _is_matching_migration_record(
                    existing, expected_records[action.issue_iid], expected_intent
                ):
                    raise RuntimeError("migration cannot overwrite a ledger record")
                expected_projection = {expected_label} if expected_label else set()
                replaying_empty_projection = (
                    not projected
                    and existing is not None
                    and existing.intent == expected_intent
                )
                if (
                    current_action is None
                    and projected != expected_projection
                    and not replaying_empty_projection
                ):
                    raise RuntimeError(
                        "migration evidence changed; create a new preview"
                    )
            for action in plan.actions:
                record = existing_records[action.issue_iid]
                if record is None:
                    record = expected_records[action.issue_iid]
                if record.intent is None:
                    record = ledger.persist(
                        record.model_copy(update={"intent": _migration_intent(action)})
                    )
                gitlab.transition_labels(
                    action.issue_iid, label_for_state(action.target_state)
                )
                ledger.persist(record.model_copy(update={"intent": None}))
        return _report(
            request,
            applied=True,
            outcome="migration_applied",
            actions=plan.actions,
            project=identity,
        )

    if request.command != "loop.run":
        raise RuntimeError("unsupported command")
    if not apply:
        prerequisite_projections = _prerequisite_projections(
            gitlab.prerequisite_projection_inventory(), project_id=project.id
        )
        records = ledger.list_records()
        gate = _inspect_pipeline_gate(
            gitlab=gitlab,
            repository=repository,
            records=records,
            project_id=project.id,
            default_branch=project.default_branch,
            now=wired.clock(),
        )
        resumable = resume_candidates(
            tuple(
                record
                for record in records
                if record.issue_iid not in gate.deferred_observer_iids
            ),
            now=wired.clock(),
        )
        if gate.outcome is not None:
            return _report(
                request,
                applied=False,
                outcome=gate.outcome,
                project=identity,
                stop_reason=gate.outcome,
                summary=gate.summary,
                next_action=gate.next_action,
                prerequisite_projections=prerequisite_projections,
            )
        if gate.repair_owner is not None:
            if _has_competing_repair_owner(records, gate.repair_owner):
                return _report(
                    request,
                    applied=False,
                    outcome="pipeline_blocked",
                    project=identity,
                    stop_reason="pipeline_blocked",
                    summary="The exact failed pipeline has competing retained work.",
                    next_action="Reconcile the competing retained records before repair.",
                    prerequisite_projections=prerequisite_projections,
                )
            if gate.repair_owner.state is RunnerState.REPAIR:
                return _report(
                    request,
                    applied=False,
                    outcome="resume_available",
                    records=(gate.repair_owner,),
                    project=identity,
                    prerequisite_projections=prerequisite_projections,
                )
            if gate.repair_owner.state in {
                RunnerState.OBSERVING,
                RunnerState.NEEDS_REPAIR,
                RunnerState.REPAIR_FAILED,
            }:
                return _report(
                    request,
                    applied=False,
                    outcome="repair_ready",
                    records=(gate.repair_owner,),
                    project=identity,
                    stop_reason="repair_ready",
                    summary="An exact retained owner will repair the failed current-source pipeline when applied.",
                    next_action="Run the ordinary loop with --apply.",
                    prerequisite_projections=prerequisite_projections,
                )
        if resumable:
            return _report(
                request,
                applied=False,
                outcome="resume_available",
                records=resumable[:1],
                project=identity,
                prerequisite_projections=prerequisite_projections,
            )
        record_iids = {record.issue_iid for record in records}
        eligible = tuple(
            issue for issue in gitlab.list_eligible() if issue.iid not in record_iids
        )
        return _report(
            request,
            applied=False,
            outcome="eligible" if eligible else "idle",
            project=identity,
            prerequisite_projections=prerequisite_projections,
        )
    with wired.lock():
        prerequisite_inventory = gitlab.prerequisite_projection_inventory()
        prerequisite_reconciliation = prerequisite_label_reconciliation(
            prerequisite_inventory, project_id=project.id
        )
        projection_drift = prerequisite_reconciliation.projections
        prerequisite_projections = _prerequisite_projection_reports(projection_drift)
        intended_definitions = prerequisite_reconciliation.label_names
        definitions_changed = gitlab.ensure_prerequisite_labels(intended_definitions)
        if projection_drift:
            for projection in projection_drift:
                gitlab.transition_prerequisite_labels(
                    projection.issue_iid,
                    tuple(
                        label
                        for label in projection.intended_labels
                        if prerequisite_label_iid(label) is not None
                    ),
                )
        legacy_definitions_retired = gitlab.retire_legacy_prerequisite_labels()
        if projection_drift or definitions_changed or legacy_definitions_retired:
            return _report(
                request,
                applied=True,
                outcome="prerequisite_labels_reconciled",
                project=identity,
                stop_reason="prerequisite_labels_reconciled",
                summary="Dynamic prerequisite display labels were reconciled.",
                next_action="Run the ordinary loop again to select work.",
                prerequisite_projections=prerequisite_projections,
            )
    wired.invoke.preflight()
    records: list[LedgerRecord] = []
    stop_reason = "idle"
    completed_admission = wired.read_validation_admission()
    resumed_at: datetime | None = None
    resumed_disposition_saved = False
    idle_recovery: tuple[str, LedgerRecord] | None = None

    with wired.lock():
        reconcile_closed_issues(
            ledger,
            gitlab,
            project_id=project.id,
            repository=repository,
            now=wired.clock(),
        )
        yielded_issue_iid = (
            completed_admission.get("issue_iid")
            if completed_admission is not None
            else None
        )
        if (
            completed_admission is not None
            and completed_admission.get("status") == "complete"
            and completed_admission.get("yield_at") is not None
            and isinstance(yielded_issue_iid, int)
        ):
            yielded_record = next(
                (
                    record
                    for record in ledger.list_records()
                    if record.issue_iid == yielded_issue_iid
                ),
                None,
            )
            if yielded_record is not None and yielded_record.state not in ACTIVE_STATES:
                resolution = (
                    "closed_without_resume"
                    if yielded_record.state is RunnerState.COMPLETE
                    else "ineligible_without_resume"
                )
                resolved_at = wired.clock()
                if mark_validation_admission_resolved(
                    completed_admission,
                    at=resolved_at,
                    issue_iid=yielded_issue_iid,
                    resolution=resolution,
                ):
                    return _report(
                        request,
                        applied=True,
                        outcome="activation_complete",
                        project=identity,
                        stop_reason="validation_admission_resolved",
                        validation_admission=ValidationAdmissionReport(
                            request_id=str(completed_admission["request_id"]),
                            request_at=datetime.fromisoformat(
                                str(completed_admission["request_at"])
                            ),
                            yield_at=datetime.fromisoformat(
                                str(completed_admission["yield_at"])
                            ),
                            validation_started_at=datetime.fromisoformat(
                                str(completed_admission["validation_started_at"])
                            ),
                            validation_finished_at=datetime.fromisoformat(
                                str(completed_admission["validation_finished_at"])
                            ),
                            resolved_at=resolved_at,
                            issue_iid=yielded_issue_iid,
                            resolution=resolution,
                            wait_elapsed_seconds=float(
                                str(completed_admission["wait_elapsed_seconds"])
                            ),
                            unavailable=False,
                        ),
                    )
        started = wired.monotonic()

        def activate_pipeline_repair(
            gate: _PipelineGateDecision, inventory: tuple[LedgerRecord, ...]
        ) -> tuple[LedgerRecord | None, CommandReport | None]:
            """Activate one exact repair or return its bounded terminal report."""
            decision = _activate_pipeline_repair_gate(
                gate=gate,
                records=inventory,
                ledger=ledger,
                gitlab=gitlab,
                now=wired.clock(),
            )
            if decision is None:
                return None, None
            if decision.competing_active_owner:
                return None, _report(
                    request,
                    applied=True,
                    outcome="pipeline_blocked",
                    project=identity,
                    stop_reason="pipeline_blocked",
                    summary="The exact failed pipeline has a competing active owner.",
                    next_action="Reconcile the active retained owner before repair.",
                )
            assert decision.record is not None
            if decision.record.state is not RunnerState.REPAIR:
                return decision.record, _report(
                    request,
                    applied=True,
                    outcome="activation_complete",
                    records=(decision.record,),
                    project=identity,
                    segments_run=1,
                    stop_reason=decision.record.state.value.replace("-", "_"),
                )
            return decision.record, None

        inventory = tuple(ledger.list_records())
        gate = _inspect_pipeline_gate(
            gitlab=gitlab,
            repository=repository,
            records=inventory,
            project_id=project.id,
            default_branch=project.default_branch,
            now=wired.clock(),
        )
        if gate.outcome is not None:
            return _report(
                request,
                applied=True,
                outcome=gate.outcome,
                project=identity,
                stop_reason=gate.outcome,
                summary=gate.summary,
                next_action=gate.next_action,
            )
        activated_repair, repair_report = activate_pipeline_repair(gate, inventory)
        if repair_report is not None:
            return repair_report

        def remaining_activation_seconds() -> float:
            return request.max_activation_seconds - (wired.monotonic() - started)

        immediate_owners = (
            (activated_repair,)
            if activated_repair is not None
            else tuple(
                item
                for item in ledger.list_records()
                if item.state in IMMEDIATE_OWNER_STATES
                and item.issue_iid not in gate.deferred_observer_iids
            )
        )
        if len(immediate_owners) > 1:
            return _report(
                request,
                applied=True,
                outcome="pipeline_blocked",
                project=identity,
                stop_reason="cross_item_identity_ambiguous",
                summary=(
                    "Multiple active retained records have conflicting serial "
                    "ownership; reconcile their shared identity before resuming."
                ),
                next_action=(
                    "Reconcile the active retained records before selecting or "
                    "repairing work."
                ),
            )
        poll_delay = _PIPELINE_POLL_INITIAL_SECONDS
        if not immediate_owners and not resume_candidates(
            tuple(ledger.list_records()), now=wired.clock()
        ):
            recovered = _automatic_clean_blocked_retry(
                wired=wired,
                project=project,
                dependency_inventory=gitlab.prerequisite_projection_inventory(),
                now=wired.clock(),
            )
            if recovered is not None:
                immediate_owners = (recovered,)

        prior_state = immediate_owners[0].state if len(immediate_owners) == 1 else None
        prior_revision = (
            immediate_owners[0].revision if len(immediate_owners) == 1 else None
        )
        prior_record = immediate_owners[0] if len(immediate_owners) == 1 else None
        issue_iid = (
            immediate_owners[0].issue_iid if len(immediate_owners) == 1 else None
        )
        repair_workflow_ran = prior_state is RunnerState.REPAIR
        segments_run = 0
        while segments_run < request.max_segments:
            if remaining_activation_seconds() <= 0:
                stop_reason = "time_limit"
                break
            yielded_request = wired.observe_yielded_interactive_validation()
            if (
                yielded_request is not None
                and yielded_request.status == "waiting"
                and yielded_request.yielded_issue_iid is not None
                and wired.clock() < yielded_request.expires_at
            ):
                stop_reason = "interactive_validation_yield"
                break
            segment_start_state = prior_state
            observed_in_segment = False
            pending_validation: object = None

            def on_worker_invoke(invoked_issue_iid: int) -> None:
                nonlocal resumed_at
                if (
                    completed_admission is not None
                    and completed_admission.get("status") == "complete"
                    and completed_admission.get("issue_iid") == invoked_issue_iid
                    and resumed_at is None
                ):
                    resumed_at = wired.clock()

            def observe_for_segment() -> object:
                nonlocal observed_in_segment, pending_validation
                observed_in_segment = True
                pending_validation = wired.observe_interactive_validation()
                if (
                    isinstance(pending_validation, InteractiveValidationRequest)
                    and pending_validation.status != "waiting"
                ):
                    return None
                return pending_validation

            record = wired.run_segment_boundary(
                ledger=ledger,
                gitlab=gitlab,
                repository=repository,
                invoke=wired.invoke,
                timeout_seconds=request.max_activation_seconds,
                now=wired.clock(),
                clock=wired.clock,
                observe_stage_identity=wired.observe_stage_identity,
                issue_iid=issue_iid,
                issue_lock=wired.issue_lock,
                load_red_checkpoint=wired.load_red_checkpoint,
                apply_red_checkpoint=wired.apply_red_checkpoint,
                remaining_seconds=remaining_activation_seconds,
                deferred_observer_iids=gate.deferred_observer_iids,
                observe_interactive_validation=observe_for_segment,
                on_worker_invoke=on_worker_invoke,
            )
            if record is None:
                if remaining_activation_seconds() <= 0:
                    stop_reason = "time_limit"
                else:
                    stop_reason = (
                        "retry_wait"
                        if segment_start_state is RunnerState.RETRY_WAIT
                        or any(
                            item.state is RunnerState.RETRY_WAIT
                            for item in ledger.list_records()
                        )
                        else "idle"
                    )
                break
            if (
                resumed_at is not None
                and completed_admission is not None
                and record.issue_iid == completed_admission.get("issue_iid")
                and not resumed_disposition_saved
            ):
                resumed_disposition_saved = mark_validation_admission_resumed(
                    completed_admission,
                    at=resumed_at,
                    issue_iid=record.issue_iid,
                )
            if issue_iid is None:
                issue_iid = record.issue_iid
            elif record.issue_iid != issue_iid:
                raise RuntimeError("one activation cannot advance multiple issues")
            records.append(record)
            prior_state = record.state
            timing_only_pipeline_poll = (
                segment_start_state is RunnerState.OBSERVING
                and record.state is RunnerState.OBSERVING
                and prior_record is not None
                and is_bounded_timing_append(
                    prior_record.phase_timings, record.phase_timings
                )
                and prior_record.model_copy(
                    update={
                        "revision": record.revision,
                        "phase_timings": record.phase_timings,
                        "phase_timing_count": record.phase_timing_count,
                        "phase_timing_unavailable_reasons": record.phase_timing_unavailable_reasons,
                        "latest_meaningful_update_at": (
                            record.latest_meaningful_update_at
                        ),
                    }
                )
                == record
            )
            if record.revision != prior_revision and not timing_only_pipeline_poll:
                segments_run += 1
            prior_revision = record.revision
            prior_record = record
            if not observed_in_segment:
                pending_validation = wired.observe_interactive_validation()
            if (
                pending_validation
                and record.state in IMMEDIATE_OWNER_STATES
                and (
                    not isinstance(pending_validation, InteractiveValidationRequest)
                    or (
                        pending_validation.status == "waiting"
                        and mark_interactive_validation_yield(
                            pending_validation,
                            at=wired.clock(),
                            issue_iid=record.issue_iid,
                        )
                    )
                )
            ):
                stop_reason = "interactive_validation_yield"
                break
            if segment_start_state is RunnerState.REPAIR:
                repair_workflow_ran = True
            if record.state is RunnerState.REPAIR and repair_workflow_ran:
                stop_reason = "repair"
                break
            if record.state in {
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.AWAITING_HUMAN,
                RunnerState.COMPLETE,
                RunnerState.NEEDS_REPAIR,
                RunnerState.REPAIR_FAILED,
                RunnerState.SUGGEST_CLOSE,
            }:
                stop_reason = record.state.value.replace("-", "_")
                break
            if record.state is RunnerState.RETRY_WAIT:
                if record.retry_at is not None and record.retry_at <= wired.clock():
                    continue
                stop_reason = "retry_wait"
                break
            if record.state is RunnerState.OBSERVING:
                if segment_start_state is RunnerState.PUBLISHING:
                    stop_reason = "pipeline_wait"
                    break
                poll_delay, viable = _sleep_for_pipeline_poll(
                    delay=poll_delay,
                    remaining_seconds=remaining_activation_seconds,
                    sleep=wired.sleep,
                )
                if not viable:
                    stop_reason = "time_limit"
                    break
                continue
        else:
            stop_reason = "segment_limit"
        if not records and stop_reason == "idle" and issue_iid is None:
            idle_recovery = _idle_recovery_child(
                wired=wired,
                project=project,
                now=wired.clock(),
                remaining_seconds=remaining_activation_seconds,
            )
    admission_evidence: ValidationAdmissionReport | None = None
    if completed_admission is not None and (
        completed_admission.get("status") == "unavailable" or resumed_disposition_saved
    ):
        try:
            request_at = datetime.fromisoformat(str(completed_admission["request_at"]))
            resume_at = resumed_at or wired.clock()
            unavailable = completed_admission.get("status") == "unavailable"
            validation_started_at = (
                None
                if unavailable
                else datetime.fromisoformat(
                    str(completed_admission["validation_started_at"])
                )
            )
            validation_finished_at = datetime.fromisoformat(
                str(completed_admission["validation_finished_at"])
            )
            yield_at = (
                None
                if unavailable
                else datetime.fromisoformat(str(completed_admission["yield_at"]))
            )
            admission_evidence = ValidationAdmissionReport(
                request_id=str(completed_admission["request_id"]),
                request_at=request_at,
                yield_at=yield_at,
                validation_started_at=validation_started_at,
                validation_finished_at=validation_finished_at,
                resume_at=resume_at,
                issue_iid=(
                    int(str(completed_admission["issue_iid"]))
                    if not unavailable
                    and completed_admission.get("issue_iid") is not None
                    else None
                ),
                resolution="unavailable" if unavailable else "resumed",
                wait_elapsed_seconds=float(
                    str(completed_admission["wait_elapsed_seconds"])
                ),
                unavailable=unavailable,
            )
            if unavailable and not mark_validation_admission_resumed(
                completed_admission, at=resume_at
            ):
                admission_evidence = None
        except (KeyError, TypeError, ValueError):
            admission_evidence = None
    if idle_recovery is not None:
        outcome, original = idle_recovery
        return _report(
            request,
            applied=True,
            outcome=outcome,
            records=(original,),
            project=identity,
            segments_run=1,
            stop_reason=outcome,
            validation_admission=admission_evidence,
        )
    return _report(
        request,
        applied=True,
        outcome="idle"
        if not records and stop_reason == "idle"
        else "activation_complete",
        records=tuple(records),
        project=identity,
        segments_run=segments_run,
        stop_reason=stop_reason,
        validation_admission=admission_evidence,
    )
