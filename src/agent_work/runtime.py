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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .domain import (
    ACTIVE_STATES,
    IMMEDIATE_OWNER_STATES,
    LIFECYCLE_LABELS,
    NONTERMINAL_PIPELINE_STATUSES,
    CommandRequest,
    ErrorCode,
    PrerequisiteLabelProjection,
    RepairAttemptEvidence,
    RunnerState,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    parse_split_marker,
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
    ResolutionChoice,
    ResolutionRequest,
    build_inventory,
    build_resolution_plan,
    classify_recovery_owner_capacity,
    verify_resolution_plan,
)
from .ledger import (
    JsonLedger,
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
    LedgerRecord,
    MergeRequestEvidence,
    MigrationAction,
    MigrationObservation,
    MigrationPlan,
    OperationalPlan,
    PipelineEvidence,
    PrerequisiteLabelProjectionReport,
    RecoveryCandidateReport,
    RecoverySummary,
    TransitionIntentRecord,
)
from .ports import (
    CommandRunner,
    GitLabPort,
    InvokerPort,
    LedgerPort,
    ProjectLockFactory,
    RepositoryPort,
)
from .repository import (
    GitRepository,
    RepositoryError,
    RepositoryTransientError,
    discover_config,
    run_subprocess,
)
from .serial import (
    AUTOMATIC_REPAIR_MAX_ATTEMPTS,
    SegmentError,
    activate_exact_pipeline_repair,
    collect_exact_failed_job_diagnostic,
    reconcile_closed_issues,
    record_failed_job_diagnostic_unavailable,
    resume_candidates,
    run_segment,
)
from .state_machine import transition_record
from .status import project_status
from .worker import AgentInvoker, is_safe_worker_result_diagnostic


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
) -> RecoveryObservation:
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
    paths = (
        ()
        if not exists or record is None or repository is None
        else repository.changed_paths(record.worktree)
    )
    blocked_cause = None
    if (
        record is not None
        and record.state is RunnerState.BLOCKED
        and record.error_code
        in {
            ErrorCode.WORKER_PROTOCOL_INVALID,
            ErrorCode.WORKER_RECEIPT_INVALID,
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
        if (
            latest is not None
            and latest.code
            in {
                ErrorCode.WORKER_PROTOCOL_INVALID.value,
                ErrorCode.WORKER_RECEIPT_INVALID.value,
            }
            and _is_safe_worker_protocol_diagnostic(latest.detail)
        ):
            blocked_cause = latest.detail
    return RecoveryObservation(
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
        worktree_dirty=None if exists is None else bool(paths),
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
    )


def _is_safe_worker_protocol_diagnostic(value: str) -> bool:
    """Keep the recovery boundary aligned with worker diagnostic generation."""
    return is_safe_worker_result_diagnostic(value)


def _recovery_split_dependencies(
    issue_iid: int, *, project: GitLabProject, gitlab: GitLabPort
) -> tuple[tuple[int, str], ...]:
    """Read validated split prerequisites for exactly one recovery target."""
    child = next(
        (
            candidate
            for candidate in gitlab.split_child_inventory()
            if candidate.iid == issue_iid
        ),
        None,
    )
    if child is None:
        return ()
    marker = parse_split_marker(child.description, project.id)
    if marker is None:
        return ()
    return tuple(
        (dependency_iid, gitlab.fetch_issue(dependency_iid).state)
        for dependency_iid in sorted(marker[2])
    )


def _recovery_plan(
    request: CommandRequest,
    *,
    project: GitLabProject,
    username: str,
    record: LedgerRecord | None,
    gitlab: GitLabPort,
    repository: RepositoryPort | None = None,
    controller_status: str | None = None,
    owner_status: str = "unknown",
    issue: GitLabIssue | None = None,
    merge_requests: tuple[MergeRequestEvidence, ...] | None = None,
) -> tuple[OperationalPlan, RecoveryObservation]:
    if request.issue_iid is None or request.choice is None or request.reason is None:
        raise RuntimeError("recovery resolution request is incomplete")
    try:
        choice = ResolutionChoice(request.choice)
        observation = _recovery_observation(
            record,
            issue_iid=request.issue_iid,
            gitlab=gitlab,
            repository=repository,
            project=project,
            controller_status=controller_status,
            owner_status=owner_status,
            issue=issue,
            merge_requests=merge_requests,
            split_dependencies=_recovery_split_dependencies(
                request.issue_iid, project=project, gitlab=gitlab
            ),
        )
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
            and record.work_kind is WorkKind.ORDINARY
            and repository.worktree_exists(record.worktree)
            and repository.head(record.worktree) == record.source_sha
            and not repository.has_changes(record.worktree)
            and not repository.changed_paths(record.worktree)
            and not gitlab.open_merge_requests_by_branch(record.source_branch)
            and record.merge_request_iid is None
            and record.publication_sha is None
            and record.merge_request_draft is None
            and record.feedback_cursor is None
            and record.pending_feedback is None
            and not record.receipts
            and record.intent is None
            and repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha or ""
            )
            is None
        )
    except (GitLabError, OSError, RepositoryError, RuntimeError, ValueError):
        return False


def _owner_capacity(
    records: tuple[LedgerRecord, ...], *, target_iid: int, wired: RuntimeDependencies
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
            owner_status=_issue_owner_status(wired, record.issue_iid),
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
    next_action = "Review the operational checkpoint; no issue was claimed."
    outcome = "checkpoint"
    label_drift = wired.lifecycle_label_drift()
    capacity = None
    if request.command == "loop.recover" and target is not None:
        capacity = _owner_capacity(records, target_iid=target, wired=wired)
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
        if gate.outcome is not None and gate.outcome != "pipeline_wait":
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
            action = (
                "normalize_interrupted_claim"
                if _normalization_is_exact(
                    record, gitlab=wired.gitlab, repository=wired.repository
                )
                else "normalization_unverified"
            )
            next_action = (
                f"Apply the reviewed normalization for retained issue {target}."
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
    issue_owner_probe: Callable[[int], str] = lambda _issue_iid: "unknown"
    lifecycle_label_drift: Callable[[], tuple[str, ...]] = lambda: ()
    ensure_lifecycle_labels: Callable[[], None] = lambda: None
    issue_lock: Callable[[int], AbstractContextManager[None]] = lambda _issue_iid: (
        nullcontext()
    )


@dataclass(frozen=True)
class WorkerResultDependencies:
    """Read-only boundaries required to build one worker result."""

    load_record: Callable[[int], LedgerRecord | None]
    changed_paths: Callable[[Path], tuple[str, ...]]


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
    """Apply one exact gate consistently before or after pipeline polling."""
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
        job, trace = collect_exact_failed_job_diagnostic(
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
            failed_job=job,
            diagnostic=trace,
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
        if default_pipeline.status in NONTERMINAL_PIPELINE_STATUSES:
            return _pipeline_handoff(
                outcome="pipeline_wait",
                pipeline_url=default_pipeline.web_url,
                detail="The current default-branch pipeline is still running.",
                next_action="Wait for the default-branch pipeline to reach a terminal state.",
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
            # An ownerless or inexact failed MR is item-local evidence.  It
            # cannot reserve the project-wide serial slot or starve later
            # exact repairs and fresh eligible work.
            continue
        for _merge_request, pipeline in inspected:
            if pipeline.status == "failed":
                continue
            if pipeline.status in NONTERMINAL_PIPELINE_STATUSES:
                return _pipeline_handoff(
                    outcome="pipeline_wait",
                    pipeline_url=pipeline.web_url,
                    detail="An open merge request has a current-source pipeline still running.",
                    next_action="Wait for the current merge-request pipeline to reach a terminal state.",
                )
            if pipeline.status != "success":
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
    return _PipelineGateDecision(deferred_owner=deferred_owner)


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


def build_runtime_dependencies(*, debug: bool) -> RuntimeDependencies:
    """Wire real process and filesystem boundaries at the composition root."""
    config = discover_config(
        current_directory=Path.cwd(), environment=os.environ, run_command=run_subprocess
    )
    logger = _build_local_logger(config, debug=debug)

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
    gitlab = GitLabGateway(config, run_subprocess)
    return RuntimeDependencies(
        config=config,
        ledger=JsonLedger(config.state_root, clock=now),
        gitlab=gitlab,
        repository=GitRepository(
            config,
            run_subprocess,
            service_environment=os.environ,
            diagnostic_sink=lambda diagnostic: logger.info("%s", diagnostic),
        ),
        invoke=AgentInvoker(run_subprocess, environment=os.environ),
        lock=lambda: XdgProjectLock(config.state_root / "project.lock"),
        issue_lock=lambda issue_iid: XdgIssueLock(config.state_root, issue_iid),
        clock=now,
        monotonic=time.monotonic,
        sleep=time.sleep,
        logger=logger,
        read_plan=lambda path: path.read_bytes(),
        process_probe=probe_project_lock,
        issue_owner_probe=probe_issue_owner,
        lifecycle_label_drift=gitlab.lifecycle_label_drift,
        ensure_lifecycle_labels=gitlab.ensure_lifecycle_labels,
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
    return WorkerResultDependencies(
        load_record=ledger.load_read_only,
        changed_paths=repository.changed_paths,
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
        "stale": observation.stale,
        "ambiguous": observation.ambiguous,
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
            observation.blocked_cause
            if (
                observation.blocked_cause is not None
                and candidate.classification is RecoveryClassification.SAFELY_RETRYABLE
            )
            else "pipeline evidence disagrees"
            if observation.pipeline_status == "failed"
            else "retained evidence requires review"
            if observation.ambiguous or observation.stale
            else None
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
            "Resolve the blocker, then run agent-work loop retry "
            f"--issue-iid {candidate.issue_iid} --apply."
            if (
                observation.blocked_cause is not None
                and candidate.classification is RecoveryClassification.SAFELY_RETRYABLE
            )
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
    expected = _recovery_intent(plan, choice=choice, target=target)
    return (
        record.intent.operation_id == expected.operation_id
        and record.intent.kind == expected.kind
        and record.intent.expected_evidence == expected.expected_evidence
    )


def dispatch(
    request: CommandRequest,
    *,
    apply: bool,
    dependencies: RuntimeDependencies | None = None,
) -> CommandReport:
    """Dispatch one command through the single composition boundary."""
    wired = dependencies or build_runtime_dependencies(debug=request.debug)
    config = wired.config
    ledger = wired.ledger
    gitlab = wired.gitlab
    repository = wired.repository
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
            prerequisite_projections = _prerequisite_projections(
                gitlab.prerequisite_projection_inventory(), project_id=project.id
            )
            records = (
                ledger.list_records()
                if request.issue_iid is None
                else (ledger.load(request.issue_iid),)
            )
            retained = tuple(record for record in records if record is not None)
            projected = gitlab.list_projected()
            split_children = gitlab.split_child_inventory()
            projected = (*projected, *split_children)
            observed_issues = {issue.iid: issue for issue in projected}
            observed_issues.update(
                {
                    record.issue_iid: gitlab.fetch_issue(record.issue_iid)
                    for record in retained
                }
            )
            dependency_ids: set[int] = set()
            child_dependencies: dict[int, tuple[int, ...]] = {}
            for child in split_children:
                parsed_marker = parse_split_marker(child.description, project.id)
                if parsed_marker is None:
                    continue
                child_dependencies[child.iid] = parsed_marker[2]
                dependency_ids.update(parsed_marker[2])
            for dependency_iid in dependency_ids:
                observed_issues.setdefault(
                    dependency_iid, gitlab.fetch_issue(dependency_iid)
                )
            merge_requests = gitlab.open_merge_requests()
            controller = wired.process_probe()
            default_pipeline = gitlab.latest_pipeline_for_ref(project.default_branch)
            records_by_iid = {record.issue_iid: record for record in retained}
            candidate_iids = (
                set(observed_issues)
                if request.issue_iid is None
                else {request.issue_iid}
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
                        split_dependencies=tuple(
                            (dependency_iid, observed_issues[dependency_iid].state)
                            for dependency_iid in child_dependencies.get(iid, ())
                        ),
                    )
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
        snapshot_issue = gitlab.fetch_issue(request.issue_iid or 0)
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
            observation = _recovery_observation(
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
                    request.issue_iid or 0, project=project, gitlab=gitlab
                ),
            )
            plan = reviewed
        else:
            plan, observation = _recovery_plan(
                request,
                project=project,
                username=username,
                record=record,
                gitlab=gitlab,
                repository=repository,
                controller_status=snapshot_controller,
                owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                issue=snapshot_issue,
                merge_requests=snapshot_mrs,
            )
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
            current_issue = gitlab.fetch_issue(request.issue_iid or 0)
            current_mrs = gitlab.open_merge_requests()
            # The lock owner becomes this process after acquisition; retain the
            # pre-acquisition probe so the lock cannot make its own plan drift.
            current_controller = snapshot_controller
            replaying = _matches_recovery_intent(current_record, reviewed, choice)
            if replaying:
                current_observation = _recovery_observation(
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
                        request.issue_iid or 0, project=project, gitlab=gitlab
                    ),
                )
                current_plan = reviewed
            else:
                current_plan, current_observation = _recovery_plan(
                    request,
                    project=project,
                    username=username,
                    record=current_record,
                    gitlab=gitlab,
                    repository=repository,
                    controller_status=current_controller,
                    owner_status=_issue_owner_status(wired, request.issue_iid or 0),
                    issue=current_issue,
                    merge_requests=current_mrs,
                )
            if not replaying and current_plan != reviewed:
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
            if choice is ResolutionChoice.RETRY:
                if current_record is None:
                    raise RuntimeError("recovery record disappeared")
                _raise_for_immediate_owner_conflict(current_record, ledger=ledger)
                retained_publication = (
                    current_record.intent is not None
                    and current_record.intent.kind == "publish_merge_request"
                )
                if retained_publication:
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
                    result = run_segment(
                        ledger=ledger,
                        gitlab=gitlab,
                        repository=repository,
                        invoke=wired.invoke,
                        timeout_seconds=request.max_activation_seconds,
                        now=wired.clock(),
                        issue_iid=target,
                        allow_pinned_conflict=True,
                        issue_lock=wired.issue_lock,
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

    if request.command == "loop.retry":
        if request.issue_iid is None:
            raise RuntimeError("retry requires an issue IID")
        if not apply:
            record = ledger.load(request.issue_iid)
            return _report(
                request,
                applied=False,
                outcome="retry_preview",
                records=() if record is None else (record,),
                project=identity,
            )
        with wired.lock():
            record = ledger.load(request.issue_iid)
            if record is None or record.state is not RunnerState.BLOCKED:
                raise RuntimeError("only a recorded blocked issue can be retried")
            _raise_for_immediate_owner_conflict(record, ledger=ledger)
            retained_publication = (
                record.intent is not None
                and record.intent.kind == "publish_merge_request"
            )
            if (
                retained_publication
                and record.pending_feedback is None
                and record.publication_sha is None
            ):
                if record.source_sha is None:
                    raise RuntimeError("retained publication has no baseline SHA")
                publication_sha = wired.repository.bind_retained_commit(
                    record.worktree,
                    record.source_branch,
                    record.issue_iid,
                    record.source_sha,
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
                        update={
                            "occurred_at": wired.clock(),
                            "retry_count": record.retry_count,
                        }
                    ),
                )[-64:]
            resumed = ledger.persist(
                transition_record(
                    record,
                    RunnerState.PUBLISHING
                    if retained_publication
                    else RunnerState.PREPARING,
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
        return _report(
            request,
            applied=True,
            outcome="retry_scheduled",
            records=(resumed,),
            project=identity,
        )

    if request.command != "loop.run":
        raise RuntimeError("unsupported command")
    if not apply:
        prerequisite_projections = _prerequisite_projections(
            gitlab.prerequisite_projection_inventory(), project_id=project.id
        )
        records = ledger.list_records()
        resumable = resume_candidates(records, now=wired.clock())
        gate = _inspect_pipeline_gate(
            gitlab=gitlab,
            repository=repository,
            records=records,
            project_id=project.id,
            default_branch=project.default_branch,
            now=wired.clock(),
        )
        if gate.outcome is not None:
            if resumable and gate.outcome == "pipeline_wait":
                return _report(
                    request,
                    applied=False,
                    outcome="resume_available",
                    records=resumable[:1],
                    project=identity,
                    prerequisite_projections=prerequisite_projections,
                )
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

    with wired.lock():
        reconcile_closed_issues(
            ledger,
            gitlab,
            project_id=project.id,
            repository=repository,
            now=wired.clock(),
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
        if gate.outcome is not None and gate.outcome != "pipeline_wait":
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
        while gate.outcome == "pipeline_wait" and not immediate_owners:
            poll_delay, viable = _sleep_for_pipeline_poll(
                delay=poll_delay,
                remaining_seconds=remaining_activation_seconds,
                sleep=wired.sleep,
            )
            if not viable:
                return _report(
                    request,
                    applied=True,
                    outcome="activation_complete",
                    project=identity,
                    stop_reason="time_limit",
                )
            inventory = tuple(ledger.list_records())
            gate = _inspect_pipeline_gate(
                gitlab=gitlab,
                repository=repository,
                records=inventory,
                project_id=project.id,
                default_branch=project.default_branch,
                now=wired.clock(),
            )
        if gate.outcome is not None and not immediate_owners:
            return _report(
                request,
                applied=True,
                outcome=gate.outcome,
                project=identity,
                stop_reason=gate.outcome,
                summary=gate.summary,
                next_action=gate.next_action,
            )
        if not immediate_owners:
            activated_repair, repair_report = activate_pipeline_repair(gate, inventory)
            if repair_report is not None:
                return repair_report
            if activated_repair is not None:
                immediate_owners = (activated_repair,)

        prior_state = immediate_owners[0].state if len(immediate_owners) == 1 else None
        prior_revision = (
            immediate_owners[0].revision if len(immediate_owners) == 1 else None
        )
        issue_iid = (
            immediate_owners[0].issue_iid if len(immediate_owners) == 1 else None
        )
        repair_workflow_ran = prior_state is RunnerState.REPAIR
        segments_run = 0
        while segments_run < request.max_segments:
            if remaining_activation_seconds() <= 0:
                stop_reason = "time_limit"
                break
            segment_start_state = prior_state
            record = run_segment(
                ledger=ledger,
                gitlab=gitlab,
                repository=repository,
                invoke=wired.invoke,
                timeout_seconds=request.max_activation_seconds,
                now=wired.clock(),
                issue_iid=issue_iid,
                issue_lock=wired.issue_lock,
                remaining_seconds=remaining_activation_seconds,
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
            if issue_iid is None:
                issue_iid = record.issue_iid
            elif record.issue_iid != issue_iid:
                raise RuntimeError("one activation cannot advance multiple issues")
            records.append(record)
            prior_state = record.state
            if record.revision != prior_revision:
                segments_run += 1
            prior_revision = record.revision
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
    )
