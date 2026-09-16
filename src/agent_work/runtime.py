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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .domain import (
    ACTIVE_STATES,
    LIFECYCLE_LABELS,
    NONTERMINAL_PIPELINE_STATUSES,
    CommandRequest,
    ErrorCode,
    RunnerState,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    retries_remaining,
)
from .gitlab import GitLabError, GitLabGateway
from .ledger import JsonLedger, XdgProjectLock, observe_project_lock
from .migration import build_migration_plan
from .models import (
    AgentWorkConfig,
    CommandItemReport,
    CommandReport,
    ConditionEvidence,
    GitLabProject,
    LedgerRecord,
    MergeRequestEvidence,
    MigrationAction,
    MigrationObservation,
    MigrationPlan,
    OperationalPlan,
    PipelineEvidence,
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
from .serial import reconcile_closed_issues, run_segment
from .state_machine import transition_record
from .status import project_status
from .worker import AgentInvoker


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
) -> dict[str, str]:
    """Read bounded live evidence independently; status never mutates state."""
    evidence = {
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
        "has_decomposition": None
        if selected is None
        else selected.decomposition_plan is not None
        or bool(selected.decomposition_progress),
        "record_fingerprint": None
        if selected is None
        else hashlib.sha256(selected.model_dump_json().encode()).hexdigest(),
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
            and record.decomposition_plan is None
            and not record.decomposition_progress
            and record.intent is None
            and repository.verify_worktree(
                record.worktree, record.source_branch, record.source_sha or ""
            )
            is None
        )
    except (GitLabError, OSError, RepositoryError, RuntimeError, ValueError):
        return False


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
    )
    target = request.issue_iid
    action = "checkpoint"
    next_action = "Review the operational checkpoint; no issue was claimed."
    outcome = "checkpoint"
    if request.command == "loop.canary":
        if gate.repair_owner is not None:
            target = gate.repair_owner.issue_iid
            next_action = (
                f"Recover retained issue {target} for the failed exact pipeline."
            )
        elif gate.outcome is not None:
            action = gate.outcome
            next_action = gate.next_action or next_action
    else:
        if target is None:
            raise RuntimeError("recover requires an issue IID")
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
        conflicts = tuple(
            record
            for record in records
            if record.issue_iid != target and record.state in ACTIVE_STATES
        )
        if record is not None and (
            record is not None
            and record.state is RunnerState.EXECUTING
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
        elif record is not None and conflicts:
            conflict = min(conflicts, key=lambda record: record.issue_iid)
            next_action = f"Recover retained issue {conflict.issue_iid} before advancing issue {target}."
        elif record is not None:
            action = "run_segment"
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
    logger: logging.Logger
    read_plan: Callable[[Path], bytes]
    process_probe: Callable[[], str]


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


def find_exact_pipeline_owner(
    *,
    records: tuple[LedgerRecord, ...],
    merge_request: MergeRequestEvidence,
    project_id: int,
    default_branch: str,
    repository: RepositoryPort,
) -> LedgerRecord | None:
    """Return the sole exact observation owner, or ``None`` on any ambiguity."""
    candidates = tuple(
        record
        for record in records
        if record.merge_request_iid == merge_request.iid
        and record.project_id == project_id
        and record.state is RunnerState.OBSERVING
        and record.phase is WorkflowPhase.PIPELINE_WAIT
        and record.source_sha == merge_request.sha
        and record.source_branch == merge_request.source_branch
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
        for merge_request, pipeline in inspected:
            if pipeline.status != "failed":
                continue
            owner = find_exact_pipeline_owner(
                records=records,
                merge_request=merge_request,
                project_id=project_id,
                default_branch=default_branch,
                repository=repository,
            )
            if owner is not None:
                return _PipelineGateDecision(repair_owner=owner)
            return _pipeline_handoff(
                outcome="pipeline_blocked",
                pipeline_url=pipeline.web_url,
                detail="An open merge request failed for its current source SHA without one exact retained owner.",
                next_action="Inspect the pipeline and reconcile its exact retained owner before selecting work.",
            )
        for _merge_request, pipeline in inspected:
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
    return _PipelineGateDecision()


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

    now = lambda: datetime.now(UTC)
    return RuntimeDependencies(
        config=config,
        ledger=JsonLedger(config.state_root, clock=now),
        gitlab=GitLabGateway(config, run_subprocess),
        repository=GitRepository(
            config,
            run_subprocess,
            service_environment=os.environ,
            diagnostic_sink=lambda diagnostic: logger.info("%s", diagnostic),
        ),
        invoke=AgentInvoker(run_subprocess, environment=os.environ),
        lock=lambda: XdgProjectLock(config.state_root / "project.lock"),
        clock=now,
        monotonic=time.monotonic,
        logger=logger,
        read_plan=lambda path: path.read_bytes(),
        process_probe=probe_project_lock,
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
    project = gitlab.verify_project()
    identity = {"host": config.host, "project_id": project.id}

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
                raise RuntimeError(
                    "operational plan evidence changed; create a new preview"
                )
            if reviewed.action == "checkpoint":
                return current.model_copy(update={"applied": True})
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
            if reviewed.action == "run_segment":
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
                    ),
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
            retained_publication = (
                record.intent is not None
                and record.intent.kind == "publish_merge_request"
            )
            if retained_publication and record.publication_sha is None:
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
        records = ledger.list_records()
        active = tuple(
            record
            for record in records
            if record.state.value
            not in {"blocked", "awaiting-human", "failed", "complete"}
        )
        if active:
            return _report(
                request,
                applied=False,
                outcome="resume_available",
                records=active[:1],
                project=identity,
            )
        gate = _inspect_pipeline_gate(
            gitlab=gitlab,
            repository=repository,
            records=records,
            project_id=project.id,
            default_branch=project.default_branch,
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
            )
        if gate.repair_owner is not None:
            return _report(
                request,
                applied=False,
                outcome="pipeline_blocked",
                project=identity,
                stop_reason="pipeline_blocked",
                summary="An exact retained owner can resume failed-pipeline repair.",
                next_action="Run the applied loop command to resume bounded repair.",
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
        )
    wired.invoke.preflight()
    records: list[LedgerRecord] = []
    stop_reason = "idle"

    with wired.lock():
        reconcile_closed_issues(ledger, gitlab, project_id=project.id)
        started = wired.monotonic()

        inventory = tuple(ledger.list_records())
        gate = _inspect_pipeline_gate(
            gitlab=gitlab,
            repository=repository,
            records=inventory,
            project_id=project.id,
            default_branch=project.default_branch,
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
        if gate.repair_owner is not None:
            repaired = ledger.persist(
                transition_record(
                    gate.repair_owner,
                    RunnerState.REPAIR,
                    phase=WorkflowPhase.WORKFLOW,
                    error_code=ErrorCode.PIPELINE_REJECTED,
                    summary="The exact current-source pipeline failed.",
                    next_action="Run the bounded post-publication repair and revalidation segment.",
                )
            )
            return _report(
                request,
                applied=True,
                outcome="activation_complete",
                records=(repaired,),
                project=identity,
                segments_run=1,
                stop_reason="repair",
            )

        def remaining_activation_seconds() -> float:
            return request.max_activation_seconds - (wired.monotonic() - started)

        active = tuple(
            item for item in ledger.list_records() if item.state in ACTIVE_STATES
        )
        if gate.outcome == "pipeline_wait" and not active:
            return _report(
                request,
                applied=True,
                outcome=gate.outcome,
                project=identity,
                stop_reason=gate.outcome,
                summary=gate.summary,
                next_action=gate.next_action,
            )
        prior_state = active[0].state if len(active) == 1 else None
        issue_iid = active[0].issue_iid if len(active) == 1 else None
        for _segment_number in range(request.max_segments):
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
                remaining_seconds=remaining_activation_seconds,
            )
            if record is None:
                if remaining_activation_seconds() <= 0:
                    stop_reason = "time_limit"
                else:
                    stop_reason = (
                        "retry_wait"
                        if segment_start_state is RunnerState.RETRY_WAIT
                        else "idle"
                    )
                break
            if issue_iid is None:
                issue_iid = record.issue_iid
            elif record.issue_iid != issue_iid:
                raise RuntimeError("one activation cannot advance multiple issues")
            records.append(record)
            prior_state = record.state
            if record.state in {
                RunnerState.BLOCKED,
                RunnerState.FAILED,
                RunnerState.AWAITING_HUMAN,
                RunnerState.COMPLETE,
            }:
                stop_reason = record.state.value.replace("-", "_")
                break
            if record.state is RunnerState.RETRY_WAIT:
                if record.retry_at is not None and record.retry_at <= wired.clock():
                    continue
                stop_reason = "retry_wait"
                break
            if (
                record.state is RunnerState.REPAIR
                and record.phase is WorkflowPhase.WORKFLOW
            ):
                stop_reason = "repair"
                break
            if record.state is RunnerState.OBSERVING:
                if segment_start_state is RunnerState.PUBLISHING:
                    continue
                stop_reason = "pipeline_wait"
                break
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
        segments_run=len(records),
        stop_reason=stop_reason,
    )
