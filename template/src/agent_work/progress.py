# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Strict remote progress receipts and rebuildable local projections."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field, StrictInt, StrictStr, model_validator

from .domain import (
    ErrorCode,
    PipelineJobEvidence,
    RepairAttemptEvidence,
    RunnerState,
    SuggestCloseEvidence,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from .ledger import JsonLedger, LedgerError
from .models import (
    AgentWorkConfig,
    BoundaryModel,
    GitLabIssue,
    LedgerRecord,
    MergeRequestEvidence,
    PipelineEvidence,
)
from .red_checkpoint import RedCheckpointEvidence

MARKER = "<!-- agent-work-progress:v1 -->"
SHA_PATTERN = r"^[0-9a-f]{40}$"


class ProgressError(LedgerError):
    """Remote evidence is insufficient for a safe recovery operation."""


@dataclass(frozen=True)
class VerifiedCheckpointSource:
    """Internal configured-GitLab source facts, never deserialized worker authority."""

    project_id: int
    issue_iid: int
    work_kind: WorkKind
    source_branch: str
    source_sha: str
    merge_request_iid: int | None
    source_project_id: int
    target_project_id: int
    target_branch: str


def _source_shape(
    issue_iid: int, branch: str, kind: WorkKind, mr_iid: int | None
) -> bool:
    if branch == f"agent/issue-{issue_iid}":
        return True
    return (
        kind in {WorkKind.EXISTING_MR_REMEDIATION, WorkKind.MERGE_CONFLICT_REPAIR}
        and mr_iid is not None
        and bool(
            re.fullmatch(
                r"agent/issue-[1-9][0-9]*|clanker/[A-Za-z0-9][A-Za-z0-9._/-]*", branch
            )
        )
    )


class ProgressFailure(BoundaryModel):
    """Bounded recovery classification without external diagnostics."""

    code: StrictStr = Field(min_length=1, max_length=128)
    action: StrictStr = Field(min_length=1, max_length=4096)


class ControllerIdentity(BoundaryModel):
    """Host process identity supplied by the trusted runtime composition."""

    machine: StrictStr = Field(min_length=1, max_length=255)
    boot_id: StrictStr = Field(min_length=1, max_length=128)
    process_id: StrictInt = Field(gt=0)


class ProgressRepairFailure(BoundaryModel):
    """Portable classified repair proof without transcripts or arbitrary snapshots."""

    fingerprint: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    origin: Literal["reviewed-plan", "automatic-exact-pipeline"]
    failure_code: StrictStr = Field(min_length=1, max_length=128)
    started_at: StrictStr = Field(default="", max_length=128)
    elapsed_seconds: float = Field(default=0, ge=0, le=10800)


class ProgressCloseSuggestion(BoundaryModel):
    """Bounded human-only closure suggestion, never closure authority."""

    reason: Literal[
        "duplicate", "superseded", "already-satisfied", "no-longer-applicable"
    ]
    issue_iid: StrictInt = Field(gt=0)
    evidence: tuple[StrictStr, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_evidence(self) -> ProgressCloseSuggestion:
        """Refuse oversized or empty nested evidence."""
        if any(not text or len(text) > 2048 for text in self.evidence):
            raise ValueError("close suggestion evidence bound")
        return self


@dataclass(frozen=True)
class SourceVerificationContext:
    """Controller-derived immutable baseline and exact current source identity."""

    issue_iid: int
    work_kind: WorkKind
    source_branch: str
    merge_request_iid: int | None
    baseline_sha: str
    expected_current_source_sha: str

    def __post_init__(self) -> None:
        if (
            type(self.issue_iid) is not int
            or self.issue_iid <= 0
            or not _source_shape(
                self.issue_iid,
                self.source_branch,
                self.work_kind,
                self.merge_request_iid,
            )
            or re.fullmatch(SHA_PATTERN, self.baseline_sha) is None
            or re.fullmatch(SHA_PATTERN, self.expected_current_source_sha) is None
        ):
            raise ProgressError("source context identity or baseline SHA invalid")


@dataclass(frozen=True)
class CheckpointSourceSelection:
    """Separate physical work, accepted source and durable handoff facts."""

    context: SourceVerificationContext
    work_event: ProgressEvent
    candidate_event: ProgressEvent | None
    handoff_event: ProgressEvent | None


@dataclass(frozen=True)
class CheckpointProductObservation:
    """Independently observed object identity, content and live cache changes."""

    work_sha: str
    candidate_sha: str | None
    same_tree: bool | None
    local_changes: bool | None


class PublishedProgressObservation(BoundaryModel):
    """Fresh factual publication evidence, never cached handoff permission."""

    issue: GitLabIssue
    merge_request: MergeRequestEvidence
    source_sha: StrictStr = Field(pattern=SHA_PATTERN)
    pipeline: PipelineEvidence | None = None
    jobs: tuple[PipelineJobEvidence, ...] = ()
    disposition: Literal["passed", "pending", "failed", "missing"]


class CandidateProvenanceObservation(BoundaryModel):
    """Fresh read-only remote ancestry for one exact candidate and baseline."""

    project_id: StrictInt = Field(gt=0)
    baseline_sha: StrictStr = Field(pattern=SHA_PATTERN)
    commit_sha: StrictStr = Field(pattern=SHA_PATTERN)


class CompletedProgressObservation(BoundaryModel):
    """Fresh independently fetched terminal issue, MR and pipeline facts."""

    issue: GitLabIssue
    provenance: CandidateProvenanceObservation | None = None
    merge_request: MergeRequestEvidence | None = None
    pipeline: PipelineEvidence | None = None
    jobs: tuple[PipelineJobEvidence, ...] = ()


class ProgressEvent(BoundaryModel):
    """Versioned immutable issue note, never authority from model text."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    host: StrictStr = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$", max_length=255)
    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    run_id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$", max_length=128)
    operation_id: StrictStr = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$", max_length=128
    )
    sequence: StrictInt = Field(gt=0)
    predecessor: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_branch: StrictStr
    push_ref: StrictStr | None = None
    expected_remote_sha: StrictStr | None = Field(default=None, pattern=SHA_PATTERN)
    claim_sha: StrictStr | None = Field(default=None, pattern=SHA_PATTERN)
    expected_claim_sha: StrictStr | None = Field(default=None, pattern=SHA_PATTERN)
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind = WorkKind.ORDINARY
    phase: WorkflowPhase
    state: RunnerState
    status: Literal["prepared", "accepted", "aborted", "released"]
    commit_sha: StrictStr = Field(pattern=SHA_PATTERN)
    base_sha: StrictStr = Field(pattern=SHA_PATTERN)
    checkpoint_kind: Literal["wip", "candidate"]
    completed: tuple[StrictStr, ...] = Field(max_length=64)
    next_action: StrictStr = Field(max_length=4096)
    failure: ProgressFailure | None = None
    repair_failure: ProgressRepairFailure | None = None
    close_suggestion: ProgressCloseSuggestion | None = None
    merge_request_iid: StrictInt | None = Field(default=None, gt=0)
    completion_kind: Literal["merged", "closed-without-publication"] | None = None
    claim_generation: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def bounded_identity(self) -> ProgressEvent:
        """Bind the reserved source to the issue and bound every summary."""
        if (
            self.close_suggestion is not None
            and self.close_suggestion.issue_iid != self.issue_iid
        ):
            raise ValueError("close suggestion issue identity mismatch")
        if self.completion_kind == "closed-without-publication" and (
            self.checkpoint_kind == "candidate" or self.merge_request_iid is not None
        ):
            raise ValueError("published candidate cannot downgrade completion")
        if self.completion_kind == "merged" and (
            self.checkpoint_kind != "candidate" or self.merge_request_iid is None
        ):
            raise ValueError("merged completion requires publication MR")
        if not _source_shape(
            self.issue_iid, self.source_branch, self.work_kind, self.merge_request_iid
        ):
            raise ValueError("progress source branch does not bind issue")
        if (
            self.push_ref is not None
            and self.push_ref != f"refs/heads/{self.source_branch}"
            and not re.fullmatch(
                rf"refs/heads/agent/recovery-issue-{self.issue_iid}-[A-Za-z0-9][A-Za-z0-9-]*",
                self.push_ref,
            )
        ):
            raise ValueError("progress checkpoint ref identity mismatch")
        if any(len(item.encode()) > 4096 for item in self.completed):
            raise ValueError("completed text exceeds receipt bound")
        return self

    @property
    def event_id(self) -> str:
        """Content fingerprint including exact status and predecessor."""
        return hashlib.sha256(_canonical(self).encode()).hexdigest()


def _canonical(event: ProgressEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def render_progress_note(event: ProgressEvent) -> str:
    """Render one exact canonical note body."""
    return MARKER + "\n" + _canonical(event)


def parse_progress_note(body: str) -> ProgressEvent:
    """Reject duplicate keys, extra text, extra fields and wrong versions."""
    if len(body.encode()) > 300_000 or not body.startswith(MARKER + "\n"):
        raise ValueError("invalid progress marker or bound")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate progress key")
            result[key] = value
        return result

    event = ProgressEvent.model_validate(
        json.loads(body[len(MARKER) + 1 :], object_pairs_hook=unique)
    )
    if render_progress_note(event) != body:
        raise ValueError("noncanonical progress note")
    return event


def validate_progress_chain(
    events: tuple[ProgressEvent, ...],
) -> tuple[ProgressEvent, ...]:
    """Resolve exact duplicate delivery; refuse forks and conflicting operations."""
    unique = {event.event_id: event for event in events}
    ordered = tuple(sorted(unique.values(), key=lambda item: item.sequence))
    previous: ProgressEvent | None = None
    operations: dict[tuple[str, str], ProgressEvent] = {}
    for event in ordered:
        if previous is None:
            if event.sequence != 1 or event.predecessor is not None:
                raise ProgressError("missing progress chain predecessor")
        elif (
            event.sequence != previous.sequence + 1
            or event.predecessor != previous.event_id
            or (event.host, event.project_id, event.issue_iid)
            != (previous.host, previous.project_id, previous.issue_iid)
            or (
                event.run_id != previous.run_id
                and (
                    previous.status != "released"
                    or event.claim_generation <= previous.claim_generation
                )
            )
        ):
            raise ProgressError("conflicting or ambiguous progress chain")
        key = (event.run_id, event.operation_id)
        prior = operations.get(key)
        if prior is not None and (
            prior.commit_sha != event.commit_sha
            or prior.base_sha != event.base_sha
            or prior.status == event.status
        ):
            raise ProgressError("conflicting checkpoint operation")
        if prior is not None and (prior.status, event.status) not in {
            ("prepared", "accepted"),
            ("prepared", "aborted"),
            ("aborted", "released"),
            ("accepted", "released"),
        }:
            raise ProgressError("conflicting checkpoint operation status")
        if (
            previous is not None
            and previous.run_id == event.run_id
            and previous.claim_generation != event.claim_generation
        ):
            raise ProgressError("claim generation changed within one run")
        operations[key] = event
        previous = event
    return ordered


def required_pipeline_jobs_succeeded(
    jobs: tuple[PipelineJobEvidence, ...], *, pipeline_id: int, source_sha: str
) -> bool | None:
    """One exact-source required-job verdict for observation and completion."""
    if len({job.id for job in jobs}) != len(jobs) or any(
        job.pipeline_id != pipeline_id or job.pipeline_sha != source_sha for job in jobs
    ):
        raise ProgressError("exact pipeline job inventory is ambiguous or stale")
    required = tuple(job for job in jobs if not job.retried and not job.allow_failure)
    return all(job.status == "success" for job in required) if required else None


def accepted_candidate_history(
    events: tuple[ProgressEvent, ...],
) -> tuple[ProgressEvent, ...]:
    """Only accepted nonaborted transactions may protect a candidate source."""
    if not any(
        item.status == "accepted" and item.checkpoint_kind == "candidate"
        for item in events
    ):
        # No operation can confer candidate protection without acceptance.
        # Owning note readers separately validate the entire workflow chain.
        return ()
    chain = validate_progress_chain(events)
    aborted = {
        (item.run_id, item.operation_id) for item in chain if item.status == "aborted"
    }
    return tuple(
        item
        for item in chain
        if item.status == "accepted"
        and item.checkpoint_kind == "candidate"
        and (item.run_id, item.operation_id) not in aborted
    )


def select_checkpoint_source(
    chain: tuple[ProgressEvent, ...],
    *,
    issue_iid: int,
) -> CheckpointSourceSelection:
    """Select accepted provenance without replacing physical work by state aliases."""
    chain = validate_progress_chain(chain)
    if not chain or any(item.issue_iid != issue_iid for item in chain):
        raise ProgressError("checkpoint source task issue identity mismatch")
    candidates = accepted_candidate_history(chain)
    candidate = candidates[-1] if candidates else None
    aborted = {
        (item.run_id, item.operation_id) for item in chain if item.status == "aborted"
    }
    work = None
    handoff = None
    for item in chain:
        if item.status != "accepted" or (item.run_id, item.operation_id) in aborted:
            continue
        alias = (
            item.checkpoint_kind == "candidate"
            and work is not None
            and item.expected_remote_sha is None
            and item.claim_sha is None
            and item.expected_claim_sha is None
            and item.state
            in {
                RunnerState.PUBLISHING,
                RunnerState.OBSERVING,
                RunnerState.AWAITING_HUMAN,
                RunnerState.COMPLETE,
            }
            and item.phase
            in {
                WorkflowPhase.READY_TO_PUBLISH,
                WorkflowPhase.PIPELINE_WAIT,
                WorkflowPhase.POST_PUBLICATION,
            }
        )
        if alias:
            if item.merge_request_iid is not None:
                handoff = item
        else:
            work = item
            if item.checkpoint_kind == "candidate":
                handoff = (
                    item
                    if item.merge_request_iid is not None
                    and item.state
                    in {
                        RunnerState.OBSERVING,
                        RunnerState.AWAITING_HUMAN,
                        RunnerState.COMPLETE,
                    }
                    else None
                )
    work = work or chain[-1]
    source = candidate or work
    context = SourceVerificationContext(
        issue_iid,
        source.work_kind,
        source.source_branch,
        (
            handoff.merge_request_iid
            if handoff is not None
            else work.merge_request_iid or source.merge_request_iid
        ),
        work.base_sha,
        source.commit_sha if candidate is not None else work.base_sha,
    )
    return CheckpointSourceSelection(context, work, candidate, handoff)


def retained_checkpoint_ref(issue_iid: int, sha: str) -> str:
    """Immutable task-bound retention address for one exact work object."""
    if (
        type(issue_iid) is not int
        or issue_iid <= 0
        or re.fullmatch(SHA_PATTERN, sha) is None
    ):
        raise ProgressError("retained checkpoint issue or full SHA invalid")
    return f"refs/heads/agent/retained-issue-{issue_iid}-{sha}"


def _work_event(chain: tuple[ProgressEvent, ...]) -> ProgressEvent:
    aborted = {
        (item.run_id, item.operation_id) for item in chain if item.status == "aborted"
    }
    return next(
        (
            item
            for item in reversed(chain)
            if item.status in {"accepted", "released"}
            and (item.run_id, item.operation_id) not in aborted
        ),
        chain[-1],
    )


class CheckpointObservation(BoundaryModel):
    """Remote branch and trusted claim metadata observed independently of notes."""

    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind = WorkKind.ORDINARY
    issue_iid: StrictInt = Field(gt=0)
    source_branch: StrictStr
    commit_sha: StrictStr = Field(pattern=SHA_PATTERN)
    base_sha: StrictStr = Field(pattern=SHA_PATTERN)
    checkpoint_kind: Literal["wip", "candidate"]
    operation_id: StrictStr
    run_id: StrictStr
    claim_generation: StrictInt = Field(gt=0)
    owner_status: Literal["owned", "active", "stale", "unknown", "released"]


class CheckpointRequest(BoundaryModel):
    """Controller constructed checkpoint intent at a drained worker boundary."""

    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    run_id: StrictStr
    operation_id: StrictStr
    source_branch: StrictStr
    worktree: Path
    base_sha: StrictStr = Field(pattern=SHA_PATTERN)
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind = WorkKind.ORDINARY
    phase: WorkflowPhase
    state: RunnerState
    checkpoint_kind: Literal["wip", "candidate"]
    completed: tuple[str, ...]
    next_action: str
    failure: ProgressFailure | None = None
    repair_failure: ProgressRepairFailure | None = None
    close_suggestion: ProgressCloseSuggestion | None = None
    merge_request_iid: StrictInt | None = Field(default=None, gt=0)
    completion_kind: Literal["merged", "closed-without-publication"] | None = None
    claim_generation: StrictInt = Field(gt=0)


class PreparedCheckpoint(BoundaryModel):
    """Exact object and two leased Git refs; proof refs cannot be exported."""

    issue_iid: StrictInt = Field(gt=0)
    worktree: Path
    source_branch: StrictStr
    work_kind: WorkKind = WorkKind.ORDINARY
    merge_request_iid: StrictInt | None = Field(default=None, gt=0)
    commit_sha: StrictStr = Field(pattern=SHA_PATTERN)
    expected_remote_sha: StrictStr | None = Field(default=None, pattern=SHA_PATTERN)
    claim_ref: Literal["refs/heads/agent/claim-project"]
    claim_sha: StrictStr = Field(pattern=SHA_PATTERN)
    expected_claim_sha: StrictStr | None = Field(default=None, pattern=SHA_PATTERN)
    push_ref: StrictStr | None = None

    @model_validator(mode="after")
    def exact_targets(self) -> PreparedCheckpoint:
        """Allow only this issue's work or deterministic recovery branch."""
        if not _source_shape(
            self.issue_iid, self.source_branch, self.work_kind, self.merge_request_iid
        ):
            raise ValueError("checkpoint source branch identity mismatch")
        if (
            self.push_ref is not None
            and self.push_ref != f"refs/heads/{self.source_branch}"
            and not re.fullmatch(
                rf"refs/heads/agent/recovery-issue-{self.issue_iid}-[A-Za-z0-9][A-Za-z0-9-]*",
                self.push_ref,
            )
        ):
            raise ValueError("checkpoint push target is not owned by issue")
        return self


class ClaimRecord(BoundaryModel):
    """Strict remotely leased serial dispatcher identity; never a timeout claim."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    host: StrictStr
    project_id: StrictInt = Field(gt=0)
    issue_iid: StrictInt = Field(gt=0)
    run_id: StrictStr = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$", max_length=128)
    machine: StrictStr = Field(min_length=1, max_length=255)
    boot_id: StrictStr = Field(min_length=1, max_length=128)
    process_id: StrictInt = Field(gt=0)
    generation: StrictInt = Field(gt=0)
    operation_id: StrictStr = Field(max_length=128)
    status: Literal["owned", "released"]


class CheckpointMetadata(BoundaryModel):
    """Portable commit metadata excludes paths, summaries and model output."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    host: StrictStr
    project_id: StrictInt = Field(gt=0)
    workflow_lane: WorkflowLane = WorkflowLane.UNKNOWN
    work_kind: WorkKind = WorkKind.ORDINARY
    issue_iid: StrictInt = Field(gt=0)
    run_id: StrictStr
    operation_id: StrictStr
    source_branch: StrictStr
    base_sha: StrictStr = Field(pattern=SHA_PATTERN)
    checkpoint_kind: Literal["wip", "candidate"]
    claim_generation: StrictInt = Field(gt=0)


def parse_remote_metadata(body: str, marker: str) -> dict[str, object]:
    """Decode strict bounded commit JSON with duplicate-key rejection."""
    if not body.startswith(marker + "\n") or len(body.encode()) > 8192:
        raise ProgressError("remote metadata marker or bound mismatch")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProgressError("duplicate remote metadata key")
            result[key] = value
        return result

    try:
        payload = json.loads(body.partition("\n")[2], object_pairs_hook=unique)
    except json.JSONDecodeError:
        raise ProgressError("remote metadata JSON is invalid") from None
    if not isinstance(payload, dict):
        raise ProgressError("remote metadata object required")
    return payload


def verify_candidate_commit_message(body: str) -> None:
    """Refuse explicit WIP or ambiguous checkpoint metadata as a candidate."""
    if not isinstance(body, str) or len(body.encode("utf-8")) > 8192:
        raise ProgressError("candidate commit message exceeds classification bound")
    prefix = "agent-work-checkpoint-v"
    if prefix in body:
        marker = "agent-work-checkpoint-v1"
        if body.count(prefix) != 1 or marker + "\n" not in body:
            raise ProgressError(
                "candidate checkpoint metadata version is unknown or ambiguous"
            )
        metadata = CheckpointMetadata.model_validate(
            parse_remote_metadata(
                marker + "\n" + body.split(marker + "\n", 1)[1], marker
            )
        )
        if metadata.checkpoint_kind == "wip":
            raise ProgressError(
                "explicit WIP checkpoint cannot become a validated candidate"
            )
    if body.lstrip().startswith("WIP:"):
        raise ProgressError("explicit WIP commit cannot become a validated candidate")


class ProgressGitLab(Protocol):
    """Narrow trusted remote issue boundary."""

    def read_progress(self, iid: int) -> tuple[ProgressEvent, ...]: ...
    def ensure_progress_event(self, event: ProgressEvent) -> ProgressEvent: ...
    def list_projected(self) -> tuple[GitLabIssue, ...]: ...
    def list_closed_projected(self) -> tuple[GitLabIssue, ...]: ...
    def find_merge_request_by_branch(
        self, branch: str
    ) -> MergeRequestEvidence | None: ...


class ProgressRepository(Protocol):
    """Trusted Git operations needed by remote reconstruction."""

    def observe_checkpoint(self, iid: int) -> CheckpointObservation: ...
    def restore_checkpoint(
        self,
        *,
        issue_iid: int,
        source_branch: str,
        commit_sha: str,
        base_sha: str,
        worktree: Path,
        fresh_red: bool,
    ) -> None: ...
    def prepare_checkpoint(self, request: CheckpointRequest) -> PreparedCheckpoint: ...
    def push_checkpoint(self, prepared: PreparedCheckpoint) -> str: ...
    def progress_owner_status(self) -> str: ...
    def acquire_progress_claim(self, issue_iid: int) -> None: ...
    def release_progress_claim(self) -> None: ...


@dataclass(frozen=True)
class CheckpointDependencies:
    """Injected controller boundaries, with no worker write capability."""

    config: AgentWorkConfig
    gitlab: ProgressGitLab
    repository: ProgressRepository
    cache: JsonLedger
    clock: Callable[[], datetime]
    pause: Callable[[], object]
    drain: Callable[[], object]
    owner_status: Callable[[], str]


def _append(
    event: ProgressEvent, chain: tuple[ProgressEvent, ...], status: str
) -> ProgressEvent:
    return ProgressEvent.model_validate(
        event.model_dump()
        | {
            "status": status,
            "sequence": len(chain) + 1,
            "predecessor": chain[-1].event_id if chain else None,
        }
    )


def progress_state_fields(record: LedgerRecord) -> dict[str, object]:
    """Validate and project one bounded state consistently at every writer."""
    LedgerRecord.model_validate(record.model_dump())
    return {
        "failure": ProgressFailure(
            code=record.error_code.value,
            action="Inspect the controller's classified failure before retry",
        )
        if record.error_code is not None
        else None,
        "repair_failure": ProgressRepairFailure(
            fingerprint=record.repair_attempt.fingerprint,
            origin=record.repair_attempt.origin,
            failure_code=record.error_code.value
            if record.error_code is not None
            else "repair_failed",
            started_at=record.repair_attempt.started_at,
            elapsed_seconds=record.repair_attempt.elapsed_seconds,
        )
        if record.state is RunnerState.REPAIR_FAILED
        and record.repair_attempt is not None
        else None,
        "close_suggestion": ProgressCloseSuggestion.model_validate(
            {
                "reason": record.suggest_close_evidence.reason,
                "issue_iid": record.issue_iid,
                "evidence": (
                    f"Controller recorded {record.suggest_close_evidence.reason} for issue #{record.issue_iid}; verify current GitLab evidence before human closure",
                ),
            }
        )
        if record.state is RunnerState.SUGGEST_CLOSE
        and record.suggest_close_evidence is not None
        else None,
        "merge_request_iid": record.merge_request_iid,
        "completion_kind": (
            "merged"
            if record.merge_request_iid is not None
            else "closed-without-publication"
        )
        if record.state is RunnerState.COMPLETE
        else None,
    }


def prepared_disposition(
    event: ProgressEvent, *, work_sha: str | None, claim_sha: str | None, released: bool
) -> Literal["applied", "unapplied"]:
    """Classify exact ref/lease observations before downloading a work object."""
    if work_sha == event.commit_sha:
        if (
            event.claim_sha is None
            or claim_sha == event.claim_sha
            or (released and work_sha != event.expected_remote_sha)
        ):
            return "applied"
        raise ProgressError(
            "prepared claim transaction ambiguous; inspect exact claim history"
        )
    if work_sha == event.expected_remote_sha and released:
        return "unapplied"
    raise ProgressError(
        "prepared ref or claim conflict; verify release and original leases"
    )


def _record(
    event: ProgressEvent, config: AgentWorkConfig, *, restored: bool = False
) -> LedgerRecord:
    return LedgerRecord(
        project_id=event.project_id,
        issue_iid=event.issue_iid,
        source_branch=event.source_branch,
        merge_request_iid=event.merge_request_iid,
        work_kind=event.work_kind,
        worktree=config.state_root / "worktrees" / str(event.issue_iid),
        source_sha=event.base_sha if restored else event.commit_sha,
        error_code=ErrorCode(event.failure.code)
        if event.failure is not None
        and event.failure.code in {item.value for item in ErrorCode}
        else None,
        remote_baseline_sha=event.base_sha,
        remote_checkpoint_sha=event.commit_sha,
        remote_event_id=event.event_id,
        repair_attempt=(
            RepairAttemptEvidence(
                fingerprint=event.repair_failure.fingerprint,
                origin=event.repair_failure.origin,
                failure_reason=event.repair_failure.failure_code,
                started_at=event.repair_failure.started_at,
                elapsed_seconds=event.repair_failure.elapsed_seconds,
            )
            if event.repair_failure is not None
            else None
        ),
        suggest_close_evidence=(
            SuggestCloseEvidence(**event.close_suggestion.model_dump())
            if event.close_suggestion is not None
            and event.state is RunnerState.SUGGEST_CLOSE
            else None
        ),
        state=event.state
        if event.state
        in {
            RunnerState.COMPLETE,
            RunnerState.BLOCKED,
            RunnerState.FAILED,
            RunnerState.NEEDS_REPAIR,
            RunnerState.REPAIR_FAILED,
            RunnerState.SUGGEST_CLOSE,
        }
        else RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        summary="Recovered remote checkpoint; verification must run again",
        next_action=event.next_action,
        owner_status="released",
    )


def verify_checkpoint_source(
    gitlab: ProgressGitLab,
    repository: ProgressRepository,
    *,
    issue_iid: int,
    source_branch: str,
    source_sha: str,
    work_kind: WorkKind,
    merge_request_iid: int | None,
    baseline_sha: str | None = None,
) -> None:
    """Obtain source identity from configured APIs and bind only internal facts."""
    verifier = getattr(gitlab, "verify_checkpoint_source", None)
    if verifier is not None:
        source = verifier(
            issue_iid=issue_iid,
            source_branch=source_branch,
            source_sha=source_sha,
            work_kind=work_kind,
            merge_request_iid=merge_request_iid,
            baseline_sha=baseline_sha,
        )
        bind = getattr(repository, "bind_checkpoint_source", None)
        if bind is None:
            raise ProgressError("verified source repository boundary unavailable")
        bind(source)
    elif not _source_shape(issue_iid, source_branch, work_kind, merge_request_iid):
        raise ProgressError("checkpoint source identity unavailable")


def create_checkpoint(
    request: CheckpointRequest, *, dependencies: CheckpointDependencies
) -> ProgressEvent:
    """Prepare note, atomically push leased refs, accept note, then project cache."""
    deps = dependencies
    if request.project_id != deps.config.project_id or not _source_shape(
        request.issue_iid,
        request.source_branch,
        request.work_kind,
        request.merge_request_iid,
    ):
        raise ProgressError("checkpoint identity mismatch")
    if deps.owner_status() != "owned":
        raise ProgressError("checkpoint requires current claim owner")
    current_claim = getattr(deps.repository, "current_progress_claim", None)
    if current_claim is not None:
        owner = current_claim()
        if owner is None or (
            owner.project_id,
            owner.issue_iid,
            owner.run_id,
            owner.generation,
            owner.status,
        ) != (
            request.project_id,
            request.issue_iid,
            request.run_id,
            request.claim_generation,
            "owned",
        ):
            raise ProgressError(
                "checkpoint actual claim run, issue or generation mismatch"
            )
    deps.pause()
    deps.drain()
    chain = validate_progress_chain(deps.gitlab.read_progress(request.issue_iid))
    matches = tuple(
        item
        for item in chain
        if item.operation_id == request.operation_id and item.run_id == request.run_id
    )
    expected_source = request.base_sha
    if chain:
        selected = select_checkpoint_source(chain, issue_iid=request.issue_iid)
        if (
            selected.candidate_event is not None
            and selected.context.source_branch == request.source_branch
        ):
            expected_source = selected.context.expected_current_source_sha
    if matches:
        retry = matches[-1]
        if any(
            getattr(retry, key) != getattr(request, key)
            for key in (
                "project_id",
                "issue_iid",
                "run_id",
                "base_sha",
                "source_branch",
                "claim_generation",
                "checkpoint_kind",
                "work_kind",
                "merge_request_iid",
                "workflow_lane",
                "phase",
                "state",
            )
        ):
            raise ProgressError(
                "checkpoint retry operation or baseline identity changed"
            )
        if retry.status not in {"prepared", "accepted"}:
            raise ProgressError(
                "checkpoint retry requires unresolved or accepted operation"
            )
        read_refs = getattr(deps.repository, "progress_recovery_refs", None)
        if read_refs is not None:
            facts = read_refs(request.issue_iid, retry)
            work_sha = facts.get("remote_work_sha")
            claim_sha = facts.get("remote_claim_sha")
            if work_sha == retry.commit_sha:
                if retry.claim_sha is not None and claim_sha != retry.claim_sha:
                    raise ProgressError("checkpoint applied claim lease conflict")
                if retry.push_ref in {None, f"refs/heads/{request.source_branch}"}:
                    expected_source = retry.commit_sha
            elif (
                retry.status != "prepared"
                or work_sha != retry.expected_remote_sha
                or claim_sha != retry.expected_claim_sha
            ):
                raise ProgressError(
                    "checkpoint retry original source or claim lease conflict"
                )
            elif retry.push_ref in {None, f"refs/heads/{request.source_branch}"}:
                expected_source = retry.expected_remote_sha or request.base_sha
    context = SourceVerificationContext(
        request.issue_iid,
        request.work_kind,
        request.source_branch,
        request.merge_request_iid,
        request.base_sha,
        expected_source,
    )
    verify_checkpoint_source(
        deps.gitlab,
        deps.repository,
        issue_iid=context.issue_iid,
        source_branch=context.source_branch,
        source_sha=context.expected_current_source_sha,
        work_kind=context.work_kind,
        merge_request_iid=context.merge_request_iid,
        baseline_sha=context.baseline_sha,
    )
    bind_history = getattr(deps.repository, "bind_progress_history", None)
    if bind_history is not None:
        bind_history(chain)
    if matches:
        event = matches[-1]
        if event.status == "accepted":
            if (
                event.issue_iid,
                event.base_sha,
                event.source_branch,
                event.claim_generation,
            ) != (
                request.issue_iid,
                request.base_sha,
                request.source_branch,
                request.claim_generation,
            ):
                raise ProgressError(
                    "accepted checkpoint retry request identity changed"
                )
            read_refs = getattr(deps.repository, "progress_recovery_refs", None)
            bind = getattr(deps.repository, "bind_progress_observation", None)
            if bind is not None:
                bind(event)
            observed_sha = (
                read_refs(request.issue_iid, event).get("remote_work_sha")
                if read_refs is not None
                else deps.repository.observe_checkpoint(request.issue_iid).commit_sha
            )
            if observed_sha != event.commit_sha:
                raise ProgressError(
                    "accepted checkpoint SHA conflicts with remote ref; inspect exact history"
                )
            deps.cache.persist(_record(event, deps.config))
            return event
        if (
            event.project_id,
            event.issue_iid,
            event.run_id,
            event.base_sha,
            event.source_branch,
            event.claim_generation,
        ) != (
            request.project_id,
            request.issue_iid,
            request.run_id,
            request.base_sha,
            request.source_branch,
            request.claim_generation,
        ):
            raise ProgressError("prepared checkpoint retry request identity changed")
        bind = getattr(deps.repository, "bind_progress_observation", None)
        if bind is not None:
            bind(event)
        observe_refs = getattr(deps.repository, "progress_recovery_refs", None)
        remote_sha = (
            observe_refs(request.issue_iid, event).get("remote_work_sha")
            if observe_refs is not None
            else deps.repository.observe_checkpoint(request.issue_iid).commit_sha
        )
        if remote_sha != event.commit_sha:
            resume = getattr(deps.repository, "resume_prepared_checkpoint", None)
            if resume is None:
                raise ProgressError(
                    "prepared checkpoint SHA not remote; retain local object and retry exact push"
                )
            prepared = resume(request, event)
            if deps.repository.push_checkpoint(prepared) != event.commit_sha:
                raise ProgressError(
                    "resumed checkpoint SHA differs from prepared receipt"
                )
    else:
        prepared = deps.repository.prepare_checkpoint(request)
        event = ProgressEvent(
            host=deps.config.host,
            project_id=request.project_id,
            issue_iid=request.issue_iid,
            run_id=request.run_id,
            operation_id=request.operation_id,
            sequence=len(chain) + 1,
            predecessor=chain[-1].event_id if chain else None,
            source_branch=request.source_branch,
            push_ref=prepared.push_ref,
            expected_remote_sha=prepared.expected_remote_sha,
            claim_sha=prepared.claim_sha,
            expected_claim_sha=prepared.expected_claim_sha,
            workflow_lane=request.workflow_lane,
            work_kind=request.work_kind,
            repair_failure=request.repair_failure,
            close_suggestion=request.close_suggestion,
            merge_request_iid=request.merge_request_iid,
            completion_kind=request.completion_kind,
            phase=request.phase,
            state=request.state,
            status="prepared",
            commit_sha=prepared.commit_sha,
            base_sha=request.base_sha,
            checkpoint_kind=request.checkpoint_kind,
            completed=request.completed,
            next_action=request.next_action,
            failure=request.failure,
            claim_generation=request.claim_generation,
        )
        deps.gitlab.ensure_progress_event(event)
        chain += (event,)
        if deps.repository.push_checkpoint(prepared) != event.commit_sha:
            raise ProgressError("checkpoint push SHA mismatch")
    accepted = _append(event, chain, "accepted")
    deps.gitlab.ensure_progress_event(accepted)
    deps.cache.persist(_record(accepted, deps.config))
    return accepted


class MigrationPreview(BoundaryModel):
    """Optimistic binding for an explicit legacy evidence migration."""

    expected_revision: int
    expected_source_sha: str | None


class RemoteLedger:
    """GitLab inventory with disposable JSON projections and local compatibility."""

    def __init__(
        self,
        *,
        config: AgentWorkConfig,
        cache: JsonLedger,
        gitlab: ProgressGitLab,
        repository: ProgressRepository,
        clock: Callable[[], datetime],
        run_id: str,
    ) -> None:
        self.config = config
        self.cache = cache
        self.gitlab = gitlab
        self.repository = repository
        self.clock = clock
        self.run_id = run_id
        self.state_root = config.state_root
        bind = getattr(repository, "bind_progress_run", None)
        if bind is not None:
            bind(run_id)
        self._checkpointing = False
        self._worker_live = False
        self._legacy_issues = {
            record.issue_iid
            for record in cache.list_records_read_only()
            if record.remote_event_id is None
        }

    @contextmanager
    def worker_scope(self) -> Iterator[None]:
        """Mark the actual synchronous worker lifetime, including broker draining."""
        self._worker_live = True
        try:
            yield
        finally:
            self._worker_live = False

    def assert_worker_stopped(self) -> None:
        """Check the controller boundary rather than treating a model request as pause."""
        if self._worker_live:
            raise ProgressError("checkpoint waits for worker and broker to drain")

    def _completed_observation(
        self, event: ProgressEvent
    ) -> CompletedProgressObservation | None:
        verifier = getattr(self.gitlab, "verify_completed_progress", None)
        if (
            verifier is None
            or event.checkpoint_kind != "candidate"
            or event.state
            not in {
                RunnerState.COMPLETE,
                RunnerState.OBSERVING,
                RunnerState.AWAITING_HUMAN,
            }
        ):
            if verifier is not None and event.state is RunnerState.COMPLETE:
                return verifier(event)
            return None
        return verifier(event)

    def _complete_record(
        self, event: ProgressEvent, observed: CompletedProgressObservation
    ) -> LedgerRecord:
        complete = ProgressEvent.model_validate(
            event.model_dump()
            | {
                "state": RunnerState.COMPLETE,
                "completion_kind": "merged"
                if observed.merge_request is not None
                else "closed-without-publication",
                "merge_request_iid": observed.merge_request.iid
                if observed.merge_request is not None
                else None,
            }
        )
        record = _record(complete, self.config)
        if observed.merge_request is not None:
            record = record.model_copy(
                update={
                    "merge_request_iid": observed.merge_request.iid,
                    "merge_request_url": observed.merge_request.web_url,
                    "source_sha": observed.merge_request.sha,
                    "publication_sha": observed.merge_request.sha,
                }
            )
        return record

    def _pending_work(
        self, chain: tuple[ProgressEvent, ...], *, apply: bool
    ) -> tuple[tuple[ProgressEvent, ...], ProgressEvent | None]:
        pending = chain[-1]
        if pending.status != "prepared":
            work = select_checkpoint_source(
                chain, issue_iid=pending.issue_iid
            ).work_event
            if any(
                item.status == "aborted"
                and item.operation_id == work.operation_id
                and item.run_id == work.run_id
                for item in chain
            ):
                return chain, None
            return chain, work
        refs = getattr(self.repository, "progress_recovery_refs", None)
        if refs is None:
            observed = self.repository.observe_checkpoint(pending.issue_iid)
            if observed.commit_sha == pending.commit_sha:
                return chain, pending
            previous = next(
                (item for item in reversed(chain) if item.status == "accepted"), None
            )
            if previous is not None and observed.commit_sha == previous.commit_sha:
                # Older injected adapters can corroborate retained work, but cannot
                # authorize a transaction disposition without exact ref/claim facts.
                return chain, previous
            raise ProgressError(
                "prepared checkpoint conflicts with observed remote work"
            )
        facts = refs(pending.issue_iid, pending)
        prove = getattr(self.repository, "checkpoint_claim_released", None)
        released = prove(pending) if prove is not None else False
        decision = prepared_disposition(
            pending,
            work_sha=facts.get("remote_work_sha"),
            claim_sha=facts.get("remote_claim_sha"),
            released=released,
        )
        if decision == "applied":
            return chain, pending
        if apply:
            aborted = self.gitlab.ensure_progress_event(
                _append(pending, chain, "aborted")
            )
            ended = self.gitlab.ensure_progress_event(
                _append(aborted, (*chain, aborted), "released")
            )
            chain = (*chain, aborted, ended)
        previous = next(
            (item for item in reversed(chain) if item.status == "accepted"), None
        )
        return chain, previous

    def _preparing_record(self, event: ProgressEvent) -> LedgerRecord:
        verify_checkpoint_source(
            self.gitlab,
            self.repository,
            issue_iid=event.issue_iid,
            source_branch=event.source_branch,
            source_sha=event.base_sha,
            work_kind=event.work_kind,
            merge_request_iid=event.merge_request_iid,
        )
        return LedgerRecord(
            project_id=self.config.project_id,
            issue_iid=event.issue_iid,
            source_branch=event.source_branch,
            work_kind=event.work_kind,
            merge_request_iid=event.merge_request_iid,
            source_sha=None,
            worktree=self.state_root / "worktrees" / str(event.issue_iid),
            state=RunnerState.PREPARING,
            phase=WorkflowPhase.WORKFLOW,
            remote_event_id=event.event_id,
            next_action="Prepare the verified source again; unpushed checkpoint must be regenerated",
        )

    def _candidate_handoff(
        self,
        selected: CheckpointSourceSelection,
    ) -> ProgressEvent | None:
        candidate = selected.candidate_event
        if candidate is None or selected.context.merge_request_iid is None:
            return None
        work = selected.work_event
        if work.checkpoint_kind == "wip" and (
            work.phase
            not in {WorkflowPhase.READY_TO_PUBLISH, WorkflowPhase.PIPELINE_WAIT}
            or work.state
            not in {
                RunnerState.PUBLISHING,
                RunnerState.RETRY_WAIT,
                RunnerState.OBSERVING,
                RunnerState.AWAITING_HUMAN,
            }
        ):
            return None
        return candidate.model_copy(
            update={
                "merge_request_iid": selected.context.merge_request_iid,
                "state": RunnerState.OBSERVING
                if candidate.state is not RunnerState.COMPLETE
                else candidate.state,
            }
        )

    def _selection_published(
        self,
        selected: CheckpointSourceSelection,
        local: LedgerRecord | None,
        *,
        completed: CompletedProgressObservation | None = None,
    ) -> ProgressEvent | None:
        candidate = self._candidate_handoff(selected)
        if candidate is None:
            return None
        work = selected.work_event
        # State aliases never certify physical content. Compare both actual objects
        # and live cache changes before treating a bookkeeping snapshot as handoff.
        if work.checkpoint_kind == "wip" or completed is not None:
            observe = getattr(self.repository, "observe_checkpoint_products", None)
            if observe is None:
                # Older injected observation ports have no physical-product port.
                # Fresh Gateway completion suffices only when there is no WIP to discard.
                return (
                    candidate
                    if work.checkpoint_kind == "candidate" and completed is not None
                    else None
                )
            products = observe(
                work,
                candidate,
                worktree=local.worktree if local is not None else None,
                completed=completed,
            )
            if (
                completed is not None
                and work.checkpoint_kind == "candidate"
                and work.commit_sha == candidate.commit_sha
                and products.same_tree is None
                and products.local_changes is not True
            ):
                provenance = getattr(self.gitlab, "verify_candidate_provenance", None)
                if provenance is None:
                    raise ProgressError(
                        "cold candidate recovery requires remote provenance evidence"
                    )
                completed = completed.model_copy(
                    update={"provenance": provenance(candidate)}
                )
                products = observe(
                    work,
                    candidate,
                    worktree=local.worktree if local is not None else None,
                    completed=completed,
                )
            if products.same_tree is not True or products.local_changes is True:
                return None
        return candidate.model_copy(
            update={
                "merge_request_iid": selected.context.merge_request_iid,
                "state": RunnerState.OBSERVING
                if candidate.state is not RunnerState.COMPLETE
                else candidate.state,
            }
        )

    def _recovery_selection(
        self,
        chain: tuple[ProgressEvent, ...],
        local: LedgerRecord | None,
    ) -> tuple[
        CheckpointSourceSelection,
        ProgressEvent | None,
        CompletedProgressObservation | None,
    ]:
        selected = select_checkpoint_source(chain, issue_iid=chain[-1].issue_iid)
        if chain[-1].status == "prepared":
            return selected, None, None
        candidate = selected.candidate_event
        if candidate is not None and selected.context.merge_request_iid is not None:
            candidate = candidate.model_copy(
                update={
                    "merge_request_iid": selected.context.merge_request_iid,
                    "state": RunnerState.OBSERVING
                    if candidate.state is not RunnerState.COMPLETE
                    else candidate.state,
                }
            )
        else:
            candidate = None
        terminal = candidate or (
            selected.work_event if selected.candidate_event is None else None
        )
        completed = (
            self._completed_observation(terminal) if terminal is not None else None
        )
        published = self._selection_published(selected, local, completed=completed)
        if completed is not None and candidate is not None and published is None:
            raise ProgressError(
                "unfinished or unknown checkpoint work prevents completion"
            )
        return selected, published, completed

    def _published_record(self, event: ProgressEvent) -> LedgerRecord:
        observe = getattr(self.gitlab, "verify_published_progress", None)
        if observe is None:
            raise ProgressError(
                "published recovery requires fresh MR and pipeline observation"
            )
        owner = getattr(self.repository, "progress_owner_status", None)
        if owner is not None and owner() not in {"owned", "released"}:
            raise ProgressError(
                "published recovery claim owner requires verified handover"
            )
        products = getattr(self.repository, "observe_checkpoint_products", None)
        if products is not None:
            products(event, event, worktree=None)
        verified = observe(event)
        return _record(event, self.config).model_copy(
            update={
                "source_sha": verified.source_sha,
                "publication_sha": verified.source_sha,
                "merge_request_iid": verified.merge_request.iid,
                "merge_request_url": verified.merge_request.web_url,
                "state": RunnerState.OBSERVING,
                "phase": WorkflowPhase.PIPELINE_WAIT,
                "next_action": f"Observe fresh exact-source pipeline ({verified.disposition}); repeat review checks before handoff",
            }
        )

    def load_read_only(self, issue_iid: int) -> LedgerRecord | None:
        """Corroborate terminal/pending history before requiring a live work ref."""
        chain = validate_progress_chain(self.gitlab.read_progress(issue_iid))
        local = self.cache.load_read_only(issue_iid)
        if not chain:
            return local
        original = chain[-1]
        selection, published, observed = self._recovery_selection(chain, local)
        terminal = published or (
            selection.work_event if selection.candidate_event is None else None
        )
        if observed is not None and terminal is not None:
            return self._complete_record(terminal, observed)
        if (
            published is not None
            and getattr(self.gitlab, "verify_published_progress", None) is not None
        ):
            return self._published_record(published)
        chain, event = self._pending_work(chain, apply=False)
        if event is None:
            return self._preparing_record(original)
        if (event.host, event.project_id, event.issue_iid) != (
            self.config.host,
            self.config.project_id,
            issue_iid,
        ):
            raise ProgressError("remote progress identity mismatch")
        if event.state is RunnerState.COMPLETE:
            return _record(event, self.config)
        verify_checkpoint_source(
            self.gitlab,
            self.repository,
            issue_iid=issue_iid,
            source_branch=event.source_branch,
            source_sha=select_checkpoint_source(
                chain, issue_iid=issue_iid
            ).context.expected_current_source_sha
            if event.checkpoint_kind == "wip"
            else event.commit_sha,
            baseline_sha=event.base_sha,
            work_kind=event.work_kind,
            merge_request_iid=select_checkpoint_source(
                chain, issue_iid=issue_iid
            ).context.merge_request_iid,
        )
        refs = getattr(self.repository, "progress_recovery_refs", None)
        if (
            refs is not None
            and refs(issue_iid, event).get("remote_work_sha") != event.commit_sha
        ):
            raise ProgressError(
                "remote progress SHA conflicts with its own checkpoint ref"
            )
        if (
            local is not None
            and local.remote_checkpoint_sha == event.commit_sha
            and local.remote_event_id in {event.event_id, chain[-1].event_id}
        ):
            return local
        return _record(event, self.config)

    def load(self, issue_iid: int) -> LedgerRecord | None:
        """Load without treating local cache absence as workflow absence."""
        return self.load_read_only(issue_iid)

    def list_records(self) -> tuple[LedgerRecord, ...]:
        """Inventory remote projections plus retained legacy records."""
        inventory = getattr(self.gitlab, "progress_inventory", None)
        issues = (
            inventory()
            if inventory is not None
            else self.gitlab.list_projected() + self.gitlab.list_closed_projected()
        )
        identities = {issue.iid for issue in issues} | {
            item.issue_iid for item in self.cache.list_records_read_only()
        }
        claim_ref = getattr(self.repository, "progress_claim_ref", None)
        read_claim = getattr(self.gitlab, "read_progress_claim", None)
        cached_claim = getattr(
            self.repository, "current_progress_claim_read_only", None
        )
        claim = cached_claim() if cached_claim is not None else None
        if claim is None and claim_ref is not None and read_claim is not None:
            sha = claim_ref()
            if sha is not None:
                claim = read_claim(sha)
        if claim is not None:
            identities.add(claim.issue_iid)
        records = {
            iid: record
            for iid in sorted(identities)
            if (record := self.load_read_only(iid)) is not None
        }
        if claim is not None and claim.issue_iid not in records:
            records[claim.issue_iid] = LedgerRecord(
                project_id=self.config.project_id,
                issue_iid=claim.issue_iid,
                source_branch=f"agent/issue-{claim.issue_iid}",
                worktree=self.state_root / "worktrees" / str(claim.issue_iid),
                state=RunnerState.PREPARING,
                phase=WorkflowPhase.WORKFLOW,
                owner_status=claim.status,
                next_action="Verify the recorded dispatcher claim before preparing the issue checkout",
            )
        return tuple(records[iid] for iid in sorted(records))

    def _restore_published_record(
        self,
        selected: CheckpointSourceSelection,
        record: LedgerRecord,
    ) -> LedgerRecord:
        """Rebuild the exact source cache required by native published observers."""
        context = selected.context
        history = validate_progress_chain(self.gitlab.read_progress(record.issue_iid))
        bind = getattr(self.repository, "bind_progress_history", None)
        if bind is not None:
            bind(history)
        verify_checkpoint_source(
            self.gitlab,
            self.repository,
            issue_iid=context.issue_iid,
            source_branch=context.source_branch,
            source_sha=context.expected_current_source_sha,
            baseline_sha=context.baseline_sha,
            work_kind=context.work_kind,
            merge_request_iid=context.merge_request_iid,
        )
        owner = getattr(self.repository, "progress_owner_status", None)
        if owner is not None and owner() not in {"owned", "released"}:
            raise ProgressError(
                "published checkout restoration requires resolved claim owner"
            )
        claim_ref = getattr(self.repository, "progress_claim_ref", None)
        expected_claim = claim_ref() if claim_ref is not None else None
        restore = getattr(self.repository, "restore_published_checkpoint", None)
        if restore is None:
            raise ProgressError("published checkout restoration boundary unavailable")
        restore(
            issue_iid=record.issue_iid,
            source_branch=record.source_branch,
            commit_sha=context.expected_current_source_sha,
            worktree=record.worktree,
        )
        candidate = selected.candidate_event
        refs = getattr(self.repository, "progress_recovery_refs", None)
        if candidate is None or (
            refs is not None
            and refs(record.issue_iid, candidate).get("remote_work_sha")
            != context.expected_current_source_sha
        ):
            raise ProgressError("published source changed during checkout restoration")
        if (owner is not None and owner() not in {"owned", "released"}) or (
            claim_ref is not None and claim_ref() != expected_claim
        ):
            raise ProgressError("published checkout claim changed during restoration")
        return self.cache.persist(record)

    def _completion_owner(
        self,
        selection: CheckpointSourceSelection,
        chain: tuple[ProgressEvent, ...],
    ) -> ClaimRecord | None:
        """Bind terminal facts to the exact current owned or finished ownership epoch."""
        getter = getattr(self.repository, "current_progress_claim_read_only", None)
        ref = getattr(self.repository, "progress_claim_ref", None)
        epoch = getter() if getter is not None else None
        physical = selection.work_event
        if (
            physical.checkpoint_kind == "candidate"
            and chain[-1].status in {"accepted", "released"}
            and (
                chain[-1].issue_iid,
                chain[-1].source_branch,
                chain[-1].commit_sha,
                chain[-1].run_id,
                chain[-1].claim_generation,
                chain[-1].checkpoint_kind,
            )
            == (
                physical.issue_iid,
                physical.source_branch,
                physical.commit_sha,
                physical.run_id,
                physical.claim_generation,
                "candidate",
            )
            and epoch is not None
            and epoch.status == "released"
            and (
                epoch.host,
                epoch.project_id,
                epoch.issue_iid,
                epoch.run_id,
                epoch.generation,
            )
            == (
                self.config.host,
                self.config.project_id,
                physical.issue_iid,
                physical.run_id,
                physical.claim_generation,
            )
        ):
            if ref is None or getter is None:
                raise ProgressError("completion released epoch ref proof missing")
            exact_ref = ref()
            current_chain = validate_progress_chain(
                self.gitlab.read_progress(physical.issue_iid)
            )
            if (
                exact_ref is None
                or getter() != epoch
                or ref() != exact_ref
                or not current_chain
                or current_chain[-1].event_id != chain[-1].event_id
            ):
                raise ProgressError(
                    "completion released claim epoch or progress tip changed"
                )
            return epoch
        acquire = getattr(self.repository, "acquire_progress_claim", None)
        if acquire is not None:
            acquire(physical.issue_iid)
        claim = self._owned_claim(physical.issue_iid, chain)
        owner = getattr(self.repository, "progress_owner_status", None)
        if owner is not None and owner() != "owned":
            raise ProgressError("completion fact requires current claim owner")
        return claim

    def reconcile(self, issue_iid: int, *, apply: bool) -> LedgerRecord:
        """Validate remote claims before restoring tests at the original baseline."""
        chain = validate_progress_chain(self.gitlab.read_progress(issue_iid))
        local = self.cache.load_read_only(issue_iid)
        if not chain:
            raise ProgressError("legacy recovery requires explicit migration")
        if not apply:
            preview = self.load_read_only(issue_iid)
            if preview is None:
                raise ProgressError("remote reconstruction preview is unavailable")
            return preview
        selection, published, completed = self._recovery_selection(chain, local)
        work = published or (
            selection.work_event if selection.candidate_event is None else None
        )
        if completed is not None and work is not None:
            record = self._complete_record(work, completed)
            if apply and work.state is not RunnerState.COMPLETE:
                owner = getattr(self.repository, "progress_owner_status", None)
                if owner is not None and owner() not in {"owned", "released"}:
                    raise ProgressError(
                        "completion observer requires resolved claim owner"
                    )
                claim = self._completion_owner(selection, chain)
                event = ProgressEvent.model_validate(
                    work.model_dump()
                    | {
                        "sequence": len(chain) + 1,
                        "predecessor": chain[-1].event_id,
                        "operation_id": f"complete-{work.commit_sha}",
                        "run_id": claim.run_id
                        if claim is not None
                        else chain[-1].run_id,
                        "claim_generation": claim.generation
                        if claim is not None
                        else chain[-1].claim_generation,
                        "status": "accepted",
                        "state": RunnerState.COMPLETE,
                        "completion_kind": "merged",
                        "merge_request_iid": completed.merge_request.iid
                        if completed.merge_request is not None
                        else None,
                        "expected_remote_sha": None,
                        "claim_sha": None,
                        "expected_claim_sha": None,
                    }
                )
                self.gitlab.ensure_progress_event(event)
                record = record.model_copy(update={"remote_event_id": event.event_id})
            return self.cache.persist(record) if apply else record
        if (
            published is not None
            and getattr(self.gitlab, "verify_published_progress", None) is not None
        ):
            return self._restore_published_record(
                selection, self._published_record(published)
            )
        chain, event = self._pending_work(chain, apply=apply)
        if event is None:
            record = self._preparing_record(chain[-1])
            return self.cache.persist(record) if apply else record
        verify_checkpoint_source(
            self.gitlab,
            self.repository,
            issue_iid=issue_iid,
            source_branch=event.source_branch,
            source_sha=select_checkpoint_source(
                chain, issue_iid=issue_iid
            ).context.expected_current_source_sha
            if event.checkpoint_kind == "wip"
            else event.commit_sha,
            baseline_sha=event.base_sha,
            work_kind=event.work_kind,
            merge_request_iid=select_checkpoint_source(
                chain, issue_iid=issue_iid
            ).context.merge_request_iid,
        )
        history = getattr(self.repository, "bind_progress_history", None)
        if history is not None:
            history(chain)
        bind = getattr(self.repository, "bind_progress_observation", None)
        if bind is not None:
            bind(event)
        observation = self.repository.observe_checkpoint(issue_iid)
        if observation.owner_status not in {"released", "owned"}:
            raise ProgressError(
                "claim owner requires verified fence and explicit handover"
            )
        if observation.commit_sha != event.commit_sha:
            raise ProgressError(
                "remote checkpoint SHA conflict; inspect issue branch and receipts"
            )
        if (
            observation.issue_iid,
            observation.source_branch,
            observation.base_sha,
            observation.checkpoint_kind,
            observation.operation_id,
            observation.run_id,
            observation.claim_generation,
            observation.workflow_lane,
            observation.work_kind,
        ) != (
            event.issue_iid,
            event.source_branch,
            event.base_sha,
            event.checkpoint_kind,
            event.operation_id,
            event.run_id,
            event.claim_generation,
            event.workflow_lane,
            event.work_kind,
        ):
            raise ProgressError("remote checkpoint metadata identity conflict")
        if apply and chain[-1].status == "prepared" and event == chain[-1]:
            event = _append(event, chain, "accepted")
        if (
            local is not None
            and local.remote_checkpoint_sha == event.commit_sha
            and local.remote_event_id in {event.event_id, chain[-1].event_id}
            and local.worktree.exists()
        ):
            return local
        record = _record(event, self.config, restored=True)
        if chain[-1].status == "released" and chain[-1].commit_sha != event.commit_sha:
            record = record.model_copy(
                update={
                    "remote_event_id": chain[-1].event_id,
                    "next_action": "Retry checkpoint after verified aborted transaction; previous accepted work was retained",
                }
            )
        if chain[-1].status == "prepared" and event != chain[-1]:
            record = record.model_copy(
                update={
                    "next_action": "Recover previous accepted checkpoint; inspect lost prepared checkpoint before retry"
                }
            )
        mr = self.gitlab.find_merge_request_by_branch(event.source_branch)
        restore = True
        if mr is not None:
            verify_mr = getattr(self.gitlab, "verify_recovery_merge_request", None)
            if verify_mr is not None:
                verify_mr(issue_iid, mr)
            if (
                mr.source_project_id != self.config.project_id
                or mr.target_project_id != self.config.project_id
                or mr.source_branch != event.source_branch
            ):
                raise ProgressError(
                    "MR checkpoint identity conflict; preserve review branch"
                )
            if event.checkpoint_kind == "candidate" and mr.sha == event.commit_sha:
                restore = False
                record = record.model_copy(
                    update={
                        "merge_request_iid": mr.iid,
                        "merge_request_url": mr.web_url,
                        "publication_sha": mr.sha,
                        "source_sha": mr.sha,
                        "state": RunnerState.OBSERVING,
                        "phase": WorkflowPhase.PIPELINE_WAIT,
                    }
                )
            elif (
                event.checkpoint_kind == "wip"
                and event.push_ref is not None
                and event.push_ref.startswith(
                    f"refs/heads/agent/recovery-issue-{issue_iid}-"
                )
                and mr.sha == selection.context.expected_current_source_sha
            ):
                record = record.model_copy(
                    update={
                        "merge_request_iid": mr.iid,
                        "merge_request_url": mr.web_url,
                        "work_kind": event.work_kind,
                    }
                )
            else:
                raise ProgressError(
                    "MR and checkpoint SHA conflict; preserve review branch"
                )
        if apply:
            if (
                chain[-1].status == "prepared"
                and event.commit_sha == chain[-1].commit_sha
            ):
                self.gitlab.ensure_progress_event(event)
            prove_release = getattr(self.repository, "checkpoint_claim_released", None)
            if (
                event.status == "accepted"
                and chain[-1].status != "released"
                and prove_release is not None
                and prove_release(event)
            ):
                current_chain = validate_progress_chain(
                    self.gitlab.read_progress(issue_iid)
                )
                event = self.gitlab.ensure_progress_event(
                    _append(event, current_chain, "released")
                )
                record = record.model_copy(update={"remote_event_id": event.event_id})
            if not restore:
                restore_published = getattr(
                    self.repository, "restore_published_checkpoint", None
                )
                if restore_published is not None:
                    restore_published(
                        issue_iid=issue_iid,
                        source_branch=event.source_branch,
                        commit_sha=event.commit_sha,
                        worktree=record.worktree,
                    )
            if restore and (
                event.workflow_lane is WorkflowLane.CONTENT
                or event.work_kind is not WorkKind.ORDINARY
            ):
                complete_restore = getattr(
                    self.repository, "restore_complete_checkpoint", None
                )
                if complete_restore is None:
                    raise ProgressError("complete recovery port unavailable")
                complete_restore(
                    issue_iid=issue_iid,
                    source_branch=event.source_branch,
                    commit_sha=event.commit_sha,
                    base_sha=event.base_sha,
                    worktree=record.worktree,
                )
            elif restore:
                self.repository.restore_checkpoint(
                    issue_iid=issue_iid,
                    source_branch=event.source_branch,
                    commit_sha=event.commit_sha,
                    base_sha=event.base_sha,
                    worktree=record.worktree,
                    fresh_red=True,
                )
            return self.cache.persist(record)
        return record

    def restore_after_red(
        self, record: LedgerRecord, proof: RedCheckpointEvidence
    ) -> None:
        """Restore production only after the actual trusted Red producer succeeds."""
        if record.remote_checkpoint_sha is None:
            return
        if (proof.project_id, proof.issue_iid, proof.source_branch, proof.base_sha) != (
            record.project_id,
            record.issue_iid,
            record.source_branch,
            record.source_sha,
        ):
            raise ProgressError("fresh Red proof does not bind reconstructed baseline")
        self.repository.restore_checkpoint(
            issue_iid=record.issue_iid,
            source_branch=record.source_branch,
            commit_sha=record.remote_checkpoint_sha,
            base_sha=proof.base_sha,
            worktree=record.worktree,
            fresh_red=False,
        )

    def _owned_claim(
        self, issue_iid: int, chain: tuple[ProgressEvent, ...]
    ) -> ClaimRecord | None:
        getter = getattr(self.repository, "current_progress_claim", None)
        if getter is None:
            return None
        claim = getter()
        if (
            claim is None
            or claim.status != "owned"
            or claim.run_id != self.run_id
            or claim.issue_iid != issue_iid
            or claim.project_id != self.config.project_id
            or claim.host != self.config.host
        ):
            raise ProgressError("current project claim identity mismatch")
        if (
            chain
            and chain[-1].run_id != self.run_id
            and (
                chain[-1].status != "released"
                or claim.generation <= chain[-1].claim_generation
            )
        ):
            raise ProgressError(
                "claim handover requires released history and newer project generation"
            )
        return claim

    def _persist_candidate_fact(
        self,
        record: LedgerRecord,
        chain: tuple[ProgressEvent, ...],
        claim: ClaimRecord | None,
        state_fields: dict[str, object],
    ) -> LedgerRecord | None:
        if not chain or record.merge_request_iid is None:
            return None
        selected = select_checkpoint_source(chain, issue_iid=record.issue_iid)
        candidate = selected.candidate_event
        if candidate is None or record.source_sha != candidate.commit_sha:
            return None
        if record.state not in {
            RunnerState.PUBLISHING,
            RunnerState.OBSERVING,
            RunnerState.AWAITING_HUMAN,
            RunnerState.COMPLETE,
        }:
            return None
        products = getattr(self.repository, "observe_checkpoint_products", None)
        if products is None:
            observed = self.repository.observe_checkpoint(record.issue_iid)
            if (
                selected.work_event.checkpoint_kind != "candidate"
                or record.publication_sha != candidate.commit_sha
                or observed.commit_sha != candidate.commit_sha
                or observed.checkpoint_kind != "candidate"
            ):
                return None
        else:
            observed = products(
                selected.work_event, candidate, worktree=record.worktree
            )
            if observed.same_tree is not True or observed.local_changes is not False:
                return None
        if (
            selected.work_event.checkpoint_kind == "wip"
            and selected.work_event.phase
            not in {WorkflowPhase.READY_TO_PUBLISH, WorkflowPhase.PIPELINE_WAIT}
        ):
            if record.state is RunnerState.AWAITING_HUMAN:
                raise ProgressError(
                    "unfinished checkpoint requires fresh verification before human handoff"
                )
            return None
        observer = getattr(self.gitlab, "verify_published_progress", None)
        event = candidate.model_copy(
            update={"merge_request_iid": record.merge_request_iid}
        )
        if record.state is RunnerState.COMPLETE:
            if (
                self._completed_observation(
                    event.model_copy(
                        update={
                            "state": RunnerState.COMPLETE,
                            "completion_kind": "merged",
                        }
                    )
                )
                is None
            ):
                raise ProgressError(
                    "completion evidence unavailable for candidate fact"
                )
        elif observer is not None:
            published = observer(event)
            if (
                record.state is RunnerState.AWAITING_HUMAN
                and published.disposition != "passed"
            ):
                raise ProgressError(
                    "human handoff requires fresh passing pipeline jobs"
                )
        elif products is not None:
            return None
        current = self._owned_claim(record.issue_iid, chain)
        if self.repository.progress_owner_status() != "owned" or current != claim:
            raise ProgressError("candidate handoff claim ownership changed before note")
        generation = (
            claim.generation if claim is not None else chain[-1].claim_generation
        )
        fields = {
            "run_id": self.run_id,
            "claim_generation": generation,
            "commit_sha": candidate.commit_sha,
            "base_sha": candidate.base_sha,
            "merge_request_iid": record.merge_request_iid,
            "state": record.state,
            "phase": record.phase,
            "workflow_lane": record.workflow_lane,
            "work_kind": record.work_kind,
            **state_fields,
        }
        last = chain[-1]
        equivalent = (
            last.status == "accepted"
            and last.checkpoint_kind == "candidate"
            and all(getattr(last, key) == value for key, value in fields.items())
        )
        if equivalent:
            accepted = last
        else:
            operation = (
                "handoff-"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "predecessor": last.event_id,
                            "source": candidate.commit_sha,
                            "mr": record.merge_request_iid,
                            "state": record.state.value,
                            "phase": record.phase.value,
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest()[:32]
            )
            accepted = ProgressEvent.model_validate(
                candidate.model_dump()
                | fields
                | {
                    "sequence": len(chain) + 1,
                    "predecessor": last.event_id,
                    "operation_id": operation,
                    "status": "accepted",
                    "expected_remote_sha": None,
                    "claim_sha": None,
                    "expected_claim_sha": None,
                    "completed": (
                        "Controller corroborated exact published candidate handoff",
                    ),
                    "next_action": "Observe current MR and fresh exact-source pipeline; retained unfinished work requires verification",
                }
            )
            self.gitlab.ensure_progress_event(accepted)
        return self.cache.persist(
            record.model_copy(
                update={
                    "remote_event_id": accepted.event_id,
                    "remote_baseline_sha": candidate.base_sha,
                    "remote_checkpoint_sha": selected.work_event.commit_sha,
                }
            )
        )

    def persist(self, record: LedgerRecord) -> LedgerRecord:
        """Persist local live-process projections; checkpoints own remote commits."""
        state_fields = progress_state_fields(record)
        if record.issue_iid in self._legacy_issues:
            raise ProgressError(
                "legacy recovery requires reviewed migration before cache or remote writes"
            )
        if self._checkpointing or self._worker_live:
            return self.cache.persist(record)
        acquire = getattr(self.repository, "acquire_progress_claim", None)
        if acquire is None:
            return self.cache.persist(record)
        acquire(record.issue_iid)
        chain = validate_progress_chain(self.gitlab.read_progress(record.issue_iid))
        claim = self._owned_claim(record.issue_iid, chain)
        self.assert_worker_stopped()
        if chain and record.merge_request_iid is None:
            selected = select_checkpoint_source(chain, issue_iid=record.issue_iid)
            if selected.candidate_event is not None:
                mr = self.gitlab.find_merge_request_by_branch(record.source_branch)
                if mr is not None:
                    if (
                        mr.source_project_id != self.config.project_id
                        or mr.target_project_id != self.config.project_id
                        or mr.source_branch != record.source_branch
                        or mr.sha != selected.candidate_event.commit_sha
                    ):
                        raise ProgressError(
                            "candidate handoff MR source identity conflict"
                        )
                    verify = getattr(self.gitlab, "verify_recovery_merge_request", None)
                    if verify is not None:
                        verify(record.issue_iid, mr)
                    record = record.model_copy(
                        update={
                            "merge_request_iid": mr.iid,
                            "merge_request_url": mr.web_url,
                        }
                    )
                    state_fields = progress_state_fields(record)
        factual = self._persist_candidate_fact(record, chain, claim, state_fields)
        if factual is not None:
            return factual
        if not record.worktree.exists() or record.source_sha is None:
            return self.cache.persist(record)
        self._checkpointing = True
        try:
            chain = validate_progress_chain(self.gitlab.read_progress(record.issue_iid))
            operation = (
                chain[-1].operation_id
                if chain
                and chain[-1].status == "prepared"
                and chain[-1].run_id == self.run_id
                else "phase-"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "revision": record.revision,
                            "phase": record.phase.value,
                            "state": record.state.value,
                            "source_sha": record.source_sha,
                            "intent": record.intent.operation_id
                            if record.intent is not None
                            else None,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:32]
            )
            unfinished_phase = record.phase
            if (
                chain
                and record.merge_request_iid is not None
                and record.phase is WorkflowPhase.READY_TO_PUBLISH
            ):
                selection = select_checkpoint_source(chain, issue_iid=record.issue_iid)
                observe = getattr(self.repository, "observe_checkpoint_products", None)
                if selection.candidate_event is not None and observe is not None:
                    products = observe(
                        selection.work_event,
                        selection.candidate_event,
                        worktree=record.worktree,
                    )
                    if products.local_changes is True:
                        unfinished_phase = WorkflowPhase.WORKFLOW
            request = CheckpointRequest(
                project_id=record.project_id,
                issue_iid=record.issue_iid,
                run_id=self.run_id,
                operation_id=operation,
                source_branch=record.source_branch,
                worktree=record.worktree,
                base_sha=(
                    record.source_sha
                    if record.merge_request_iid is not None
                    and record.phase is WorkflowPhase.WORKFLOW
                    and record.workflow_lane is WorkflowLane.UNKNOWN
                    else record.remote_baseline_sha or record.source_sha
                ),
                workflow_lane=record.workflow_lane,
                work_kind=record.work_kind,
                phase=unfinished_phase,
                state=record.state,
                checkpoint_kind="wip",
                completed=(f"Controller reached {record.phase.value}",),
                next_action="Reconstruct and run fresh controller verification",
                claim_generation=(
                    claim.generation
                    if claim is not None
                    else (
                        chain[-1].claim_generation + (chain[-1].run_id != self.run_id)
                        if chain
                        else 1
                    )
                ),
            )
            request = CheckpointRequest.model_validate(
                request.model_dump() | state_fields
            )
            history = getattr(self.repository, "bind_progress_history", None)
            if history is not None:
                history(chain)
            bind = getattr(self.repository, "bind_progress_observation", None)
            if bind is not None and chain:
                bind(_work_event(chain))
            event = create_checkpoint(
                request,
                dependencies=CheckpointDependencies(
                    config=self.config,
                    cache=self.cache,
                    gitlab=self.gitlab,
                    repository=self.repository,
                    clock=self.clock,
                    pause=self.assert_worker_stopped,
                    drain=self.assert_worker_stopped,
                    owner_status=self.repository.progress_owner_status,
                ),
            )
            return self.cache.persist(
                record.model_copy(
                    update={
                        "remote_baseline_sha": event.base_sha,
                        "remote_checkpoint_sha": event.commit_sha,
                        "remote_event_id": event.event_id,
                    }
                )
            )
        finally:
            self._checkpointing = False

    def before_publish(
        self, record: LedgerRecord, publication_sha: str
    ) -> LedgerRecord:
        """Checkpoint only the verified publication SHA at the existing handoff gate."""
        authorize = getattr(self.repository, "authorize_checkpoint_publication", None)
        if authorize is None:
            return record
        self.assert_worker_stopped()
        authorize(record.source_branch, publication_sha)
        self.repository.acquire_progress_claim(record.issue_iid)
        chain = validate_progress_chain(self.gitlab.read_progress(record.issue_iid))
        claim = self._owned_claim(record.issue_iid, chain)
        request = CheckpointRequest(
            project_id=record.project_id,
            issue_iid=record.issue_iid,
            run_id=self.run_id,
            operation_id=f"publication-{publication_sha}",
            merge_request_iid=record.merge_request_iid,
            source_branch=record.source_branch,
            worktree=record.worktree,
            base_sha=record.remote_baseline_sha or record.source_sha or publication_sha,
            workflow_lane=record.workflow_lane,
            work_kind=record.work_kind,
            phase=WorkflowPhase.READY_TO_PUBLISH,
            state=RunnerState.PUBLISHING,
            checkpoint_kind="candidate",
            completed=("Controller verified exact publication candidate",),
            next_action="Discover existing MR and observe fresh exact-source pipeline",
            claim_generation=claim.generation
            if claim is not None
            else chain[-1].claim_generation
            if chain
            else 1,
        )
        event = create_checkpoint(
            request,
            dependencies=CheckpointDependencies(
                config=self.config,
                gitlab=self.gitlab,
                repository=self.repository,
                cache=self.cache,
                clock=self.clock,
                pause=self.assert_worker_stopped,
                drain=self.assert_worker_stopped,
                owner_status=self.repository.progress_owner_status,
            ),
        )
        return self.cache.persist(
            record.model_copy(
                update={
                    "remote_checkpoint_sha": event.commit_sha,
                    "remote_event_id": event.event_id,
                    "remote_baseline_sha": event.base_sha,
                }
            )
        )

    def release(self) -> None:
        """Publish released receipts before relinquishing the server-side lease."""
        release = getattr(self.repository, "release_progress_claim", None)
        if release is None:
            return
        if self.repository.progress_owner_status() != "owned":
            return
        for record in self.cache.list_records_read_only():
            chain = validate_progress_chain(self.gitlab.read_progress(record.issue_iid))
            if (
                chain
                and chain[-1].run_id == self.run_id
                and chain[-1].status == "accepted"
            ):
                event = self.gitlab.ensure_progress_event(
                    _append(chain[-1], chain, "released")
                )
                self.cache.persist(
                    record.model_copy(update={"remote_event_id": event.event_id})
                )
        release()

    def migrate(
        self,
        issue_iid: int,
        *,
        apply: bool,
        expected_revision: int | None = None,
        expected_source_sha: str | None = None,
    ) -> MigrationPreview | LedgerRecord:
        """Preserve exact old bytes before binding an explicit remote checkpoint."""
        record = self.cache.load_read_only(issue_iid)
        if record is None:
            raise ProgressError("legacy migration record missing")
        preview = MigrationPreview(
            expected_revision=record.revision, expected_source_sha=record.source_sha
        )
        if not apply:
            return preview
        if (
            expected_revision != record.revision
            or expected_source_sha != record.source_sha
            or record.source_sha is None
        ):
            raise ProgressError("legacy revision changed since migration preview")
        state_fields = progress_state_fields(record)
        kind = "candidate" if record.publication_sha is not None else "wip"
        preview_event = ProgressEvent.model_validate(
            {
                "host": self.config.host,
                "project_id": record.project_id,
                "issue_iid": issue_iid,
                "run_id": self.run_id,
                "operation_id": f"migration-{record.revision}",
                "sequence": 1,
                "predecessor": None,
                "source_branch": record.source_branch,
                "commit_sha": record.publication_sha or record.source_sha,
                "base_sha": record.remote_baseline_sha or record.source_sha,
                "state": record.state,
                "phase": record.phase,
                "workflow_lane": record.workflow_lane,
                "work_kind": record.work_kind,
                "checkpoint_kind": kind,
                "claim_generation": 1,
                "status": "accepted",
                "completed": ("Preserved legacy recovery evidence",),
                "next_action": "Reconstruct and run fresh verification",
                **state_fields,
            }
        )
        _record(preview_event, self.config)
        self.assert_worker_stopped()
        chain = validate_progress_chain(self.gitlab.read_progress(issue_iid))
        acquire = getattr(self.repository, "acquire_progress_claim", None)
        if acquire is not None:
            acquire(issue_iid)
        claim = self._owned_claim(issue_iid, chain)
        generation = claim.generation if claim is not None else 1
        preview_event = preview_event.model_copy(
            update={"claim_generation": generation}
        )
        completed = None
        published = None
        if record.state is RunnerState.COMPLETE:
            completed = self._completed_observation(preview_event)
            if completed is None:
                raise ProgressError(
                    "completion requires fresh issue and publication evidence before migration"
                )
        elif record.publication_sha is not None:
            observer = getattr(self.gitlab, "verify_published_progress", None)
            if observer is None:
                raise ProgressError(
                    "published migration requires fresh handoff observation"
                )
            published = observer(preview_event)
        # Recheck the actual current owner AFTER the fresh external observations,
        # so a conflicting lease cannot publish a new-run terminal receipt.
        current = self._owned_claim(issue_iid, chain)
        owner = getattr(self.repository, "progress_owner_status", lambda: "owned")
        if owner() != "owned" or (claim is not None and current != claim):
            raise ProgressError(
                "migration current claim identity or generation changed"
            )
        unchanged = self.cache.load_read_only(issue_iid)
        if unchanged is None or (unchanged.revision, unchanged.source_sha) != (
            expected_revision,
            expected_source_sha,
        ):
            raise ProgressError(
                "legacy migration revision or source changed before note"
            )
        raw = (self.cache.records / f"{issue_iid}.json").read_bytes()
        recovery = self.state_root / "recovery"
        recovery.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = recovery / f"legacy-{issue_iid}-{hashlib.sha256(raw).hexdigest()}.json"
        target.write_bytes(raw)
        target.chmod(0o600)
        if completed is not None or published is not None:
            fields = {
                "sequence": len(chain) + 1,
                "predecessor": chain[-1].event_id if chain else None,
                "claim_generation": generation,
            }
            if published is not None:
                fields.update(
                    {
                        "state": RunnerState.OBSERVING,
                        "phase": WorkflowPhase.PIPELINE_WAIT,
                        "next_action": f"Observe fresh pipeline evidence ({published.disposition}); retry or repair as required",
                    }
                )
            event = ProgressEvent.model_validate(preview_event.model_dump() | fields)
            if chain:
                matches = [
                    item
                    for item in chain
                    if item.run_id == event.run_id
                    and item.operation_id == event.operation_id
                ]
                if matches:
                    prior = matches[-1]
                    if (
                        prior.commit_sha != event.commit_sha
                        or prior.claim_generation != generation
                    ):
                        raise ProgressError(
                            "migration operation replay identity conflict"
                        )
                    event = prior
            self.gitlab.ensure_progress_event(event)
            self._legacy_issues.discard(issue_iid)
            if completed is not None:
                migrated = self._complete_record(event, completed)
            else:
                assert published is not None
                migrated = _record(event, self.config).model_copy(
                    update={
                        "state": RunnerState.OBSERVING,
                        "phase": WorkflowPhase.PIPELINE_WAIT,
                        "source_sha": published.source_sha,
                        "publication_sha": published.source_sha,
                        "merge_request_iid": published.merge_request.iid,
                        "merge_request_url": published.merge_request.web_url,
                    }
                )
            return self.cache.persist(migrated)
        request = CheckpointRequest.model_validate(
            {
                "project_id": record.project_id,
                "issue_iid": issue_iid,
                "run_id": self.run_id,
                "operation_id": f"migration-{record.revision}",
                "source_branch": record.source_branch,
                "worktree": record.worktree,
                "base_sha": record.source_sha,
                "workflow_lane": record.workflow_lane,
                "work_kind": record.work_kind,
                "phase": record.phase,
                "state": record.state,
                "checkpoint_kind": kind,
                "completed": ("Preserved legacy recovery evidence",),
                "next_action": "Reconstruct and run fresh verification",
                "claim_generation": generation,
                **state_fields,
            }
        )
        accepted = create_checkpoint(
            request,
            dependencies=CheckpointDependencies(
                config=self.config,
                cache=self.cache,
                gitlab=self.gitlab,
                repository=self.repository,
                clock=self.clock,
                pause=self.assert_worker_stopped,
                drain=self.assert_worker_stopped,
                owner_status=owner,
            ),
        )
        self._legacy_issues.discard(issue_iid)
        migrated = self.cache.persist(
            _record(accepted, self.config, restored=True).model_copy(
                update={
                    "next_action": "Retained legacy work is checkpointed; reconstruct a fresh cache and run verification before publication"
                }
            )
        )
        if migrated is None:
            raise ProgressError("migration cache projection missing")
        return migrated
