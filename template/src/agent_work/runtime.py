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

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .domain import (
    ACTIVE_STATES,
    LIFECYCLE_LABELS,
    CommandRequest,
    ErrorCode,
    RunnerState,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    retries_remaining,
)
from .gitlab import GitLabGateway
from .ledger import JsonLedger, XdgProjectLock
from .migration import build_migration_plan
from .models import (
    AgentWorkConfig,
    CommandItemReport,
    CommandReport,
    LedgerRecord,
    MigrationAction,
    MigrationObservation,
    MigrationPlan,
    TransitionIntentRecord,
)
from .ports import (
    GitLabPort,
    InvokerPort,
    LedgerPort,
    ProjectLockFactory,
    RepositoryPort,
)
from .repository import GitRepository, discover_config, run_subprocess
from .serial import run_segment
from .state_machine import transition_record
from .worker import AgentInvoker


def _item(record: LedgerRecord) -> CommandItemReport:
    return CommandItemReport(
        issue_iid=record.issue_iid,
        state=record.state,
        phase=record.phase,
        work_kind=record.work_kind,
        attempt=record.retry_count + 1,
        retries_remaining=retries_remaining(record.retry_count),
        retry_at=record.retry_at,
        source_branch=record.source_branch,
        worktree=record.worktree,
        merge_request_iid=record.merge_request_iid,
        merge_request_url=record.merge_request_url,
        error_code=record.error_code,
        summary=record.summary,
        next_action=record.next_action,
    )


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
    return RuntimeDependencies(
        config=config,
        ledger=JsonLedger(config.state_root),
        gitlab=GitLabGateway(config, run_subprocess),
        repository=GitRepository(config, run_subprocess),
        invoke=AgentInvoker(run_subprocess, environment=os.environ),
        lock=lambda: XdgProjectLock(config.state_root / "project.lock"),
        clock=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        logger=_build_local_logger(config, debug=debug),
        read_plan=lambda path: path.read_bytes(),
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
    return existing.model_copy(update={"revision": 0, "intent": None}) == expected and (
        intent is None or intent == expected_intent
    )


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
        "issue_iid=%s timeout_seconds=%s", request.issue_iid, request.timeout_seconds
    )
    project = gitlab.verify_project()
    identity = {"host": config.host, "project_id": project.id}

    if request.command == "loop.status":
        records = ledger.list_records()
        if request.issue_iid is not None:
            records = tuple(r for r in records if r.issue_iid == request.issue_iid)
        return _report(
            request, applied=False, outcome="status", records=records, project=identity
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
            resumed = ledger.persist(
                transition_record(
                    record,
                    RunnerState.PREPARING,
                    retry_count=0,
                    retry_at=None,
                    error_code=None,
                    summary="Human-directed retry requested.",
                    next_action="Reconcile and resume preparation.",
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
    started = wired.monotonic()

    def remaining_activation_seconds() -> float:
        return request.max_activation_seconds - (wired.monotonic() - started)

    with wired.lock():
        active = tuple(
            item for item in ledger.list_records() if item.state in ACTIVE_STATES
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
                timeout_seconds=request.timeout_seconds,
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
