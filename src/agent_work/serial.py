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

import hashlib
import json
import math
import re
import secrets
import tempfile
from base64 import b64decode
from binascii import Error as BinasciiError
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from .candidate_boundary import (
    GIT_READ_MAX_CHUNK_BYTES,
    CandidateBoundaryError,
    validate_git_read_paths,
)
from .classification import ClassificationError, classify_work, is_protected_policy_path
from .domain import (
    ACTIVE_STATES,
    DEFAULT_MAX_ACTIVATION_SECONDS,
    IMMEDIATE_OWNER_STATES,
    LIFECYCLE_LABELS,
    NONTERMINAL_PIPELINE_STATUSES,
    ErrorCode,
    FailedJobDiagnosticEvidence,
    JobTraceEvidence,
    PipelineJobEvidence,
    RedCheckpointFailureStage,
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
    label_for_state,
    parse_split_marker,
    render_split_marker,
)
from .gitlab import GitLabError, GitLabIdentityError
from .green_commit import (
    GreenCommitDependencies,
    GreenCommitError,
    GreenCommitProof,
    create_green_commit,
    green_commit_ref,
)
from .ledger import LedgerError
from .models import (
    ConditionEvidence,
    GitLabIssue,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
    PipelineEvidence,
    PublicationCorrectionIdentity,
    RepairAttemptEvidence,
    SplitPlan,
    TransitionIntentRecord,
)
from .outcome_projection import replay_outcome_projection
from .ports import (
    CommittedCandidateRepositoryPort,
    GitLabPort,
    InvokerPort,
    LedgerPort,
    RepositoryPort,
)
from .red_checkpoint import (
    RedCheckpointError,
    RedCheckpointEvidence,
    RedCheckpointRequest,
)
from .red_commit import (
    RedCommitDependencies,
    RedCommitError,
    create_red_commit,
    red_commit_ref,
)
from .repository import (
    LegacyPublicationCandidate,
    PublicationCommitRejected,
    PublicationError,
    ReconciliationTargetChanged,
    RepositoryError,
    RepositoryTransientError,
    _UnsupportedTestExecutionHook,
)
from .state_machine import (
    apply_worker_result,
    plan_retry,
    retained_ordinary_is_untouched,
    transition_record,
)
from .timing import append_phase_timing
from .worker import (
    WorkerProtocolError,
    WorkerResultBrokerRequest,
    build_worker_prompt,
    build_worker_result,
    build_worker_result_artifact,
    format_worker_result_diagnostic,
    parse_worker_result_accepted_artifact,
    parse_worker_result_artifact,
    parse_worker_result_build_input,
    validate_worker_result,
    worker_exit_evidence,
)
from .worker_read import (
    WorkerGitBrokerSession,
    WorkerReadBrokerSession,
    WorkerReadSessionIdentity,
    WorkerValidationBrokerSession,
    create_worker_read_broker,
)


class SegmentError(RuntimeError):
    """A segment could not safely reconcile or record its outcome."""


class _CommitRepository(Protocol):
    """Narrow transaction capability used only by the serial coding lane."""

    def red_commit_dependencies(
        self, worktree: Path, isolated_worktree: Path
    ) -> RedCommitDependencies: ...

    def green_commit_dependencies(
        self, worktree: Path, isolated_worktree: Path
    ) -> GreenCommitDependencies: ...

    def committed_candidate_read_base(
        self, worktree: Path, baseline_sha: str, green_sha: str, red_sha: str
    ) -> str: ...

    def prepare_reconcile_commit(
        self,
        worktree: Path,
        branch: str,
        source_sha: str,
        target_sha: str,
        candidate_tree: str,
        red_ref: str,
        green_ref: str,
    ) -> str: ...


class _BoundReconcileRepository(Protocol):
    """Require a fresh target match before an ordered commit can publish."""

    def reconcile_default_branch(
        self,
        worktree: Path,
        branch: str,
        expected_head: str,
        *,
        expected_default_sha: str,
    ) -> str | None: ...


_COMMITTED_READ_ROLES = frozenset({"router", "judge", "finalizer"})


def read_committed_candidate(
    issue_iid: int,
    mr_iid: int,
    candidate_sha: str,
    candidate_tree: str,
    role: str,
    selector: dict[str, object],
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: CommittedCandidateRepositoryPort,
) -> dict[str, object]:
    """Read an exact published candidate using durable and current identities."""
    failure = WorkerProtocolError("candidate_broker", "candidate request is invalid")
    try:
        if not isinstance(selector, dict):
            raise failure
        record = ledger.load_read_only(issue_iid)
        if (
            type(issue_iid) is not int
            or type(mr_iid) is not int
            or issue_iid <= 0
            or mr_iid <= 0
            or role not in _COMMITTED_READ_ROLES
            or record is None
            or record.issue_iid != issue_iid
            or record.project_id != repository.config.project_id
            or record.merge_request_iid != mr_iid
            or record.publication_sha != candidate_sha
            or not all(
                isinstance(value, str)
                and len(value) == 40
                and all(character in "0123456789abcdef" for character in value)
                for value in (candidate_sha, candidate_tree)
            )
        ):
            raise failure
        base_sha = (
            record.correction_identity.baseline_sha
            if record.correction_identity is not None
            else record.main_sha
        )
        if base_sha is None:
            raise failure
        project = gitlab.verify_project()
        if project.id != record.project_id:
            raise failure

        def identity_current() -> bool:
            current = ledger.load_read_only(issue_iid)
            mr = gitlab.fetch_merge_request(mr_iid)
            return (
                current == record
                and mr.iid == mr_iid
                and mr.state == "opened"
                and mr.source_project_id == record.project_id
                and mr.target_project_id == record.project_id
                and mr.source_branch == record.source_branch
                and mr.target_branch == (record.main_branch or project.default_branch)
                and mr.sha == candidate_sha
                and gitlab.mr_closes_issue(mr_iid, issue_iid)
            )

        if not identity_current():
            raise failure
        if selector == {"mode": "changed_paths"}:
            result = dict(
                repository.changed_candidate_paths(
                    record.worktree,
                    candidate_tree,
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                )
            )
            paths = result.get("changed_paths")
            files = result.get("files")
            if (
                result.get("candidate_tree") != candidate_tree
                or not isinstance(result.get("base_tree"), str)
                or len(cast(str, result["base_tree"])) != 40
                or not isinstance(paths, list)
                or not isinstance(files, list)
                or len(paths) != len(files)
                or not all(isinstance(path, str) for path in paths)
            ):
                raise failure
            validate_git_read_paths(cast(tuple[str, ...], tuple(paths)))
            if any(
                not isinstance(item, dict)
                or cast(dict[str, object], item).get("path") != path
                or any(
                    cast(dict[str, object], item).get(f"{side}_mode")
                    not in {None, "100644", "100755"}
                    or (cast(dict[str, object], item).get(f"{side}_oid") is None)
                    != (cast(dict[str, object], item).get(f"{side}_size") is None)
                    for side in ("old", "new")
                )
                for path, item in zip(paths, files, strict=True)
            ):
                raise failure
        else:
            path = selector.get("path")
            side = selector.get("side")
            offset = selector.get("offset")
            length = selector.get("length")
            if (
                set(selector) != {"path", "side", "offset", "length"}
                or not isinstance(path, str)
                or not isinstance(side, str)
                or side not in {"old", "new"}
                or type(offset) is not int
                or offset < 0
                or type(length) is not int
                or not 0 < length <= GIT_READ_MAX_CHUNK_BYTES
            ):
                raise failure
            validate_git_read_paths((path,))
            result = dict(
                repository.read_candidate_range(
                    record.worktree,
                    candidate_tree,
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    path=path,
                    side=side,
                    offset=offset,
                    length=length,
                )
            )
            data = result.get("data_base64")
            if not isinstance(data, str):
                raise failure
            decoded = b64decode(data, validate=True)
            oid = result.get("oid")
            size = result.get("size")
            if (
                result.get("candidate_tree") != candidate_tree
                or not isinstance(result.get("base_tree"), str)
                or result.get("path") != path
                or result.get("side") != side
                or result.get("offset") != offset
                or result.get("length") != length
                or not isinstance(oid, str)
                or len(oid) != 40
                or any(character not in "0123456789abcdef" for character in oid)
                or type(size) is not int
                or offset + length > size
                or len(decoded) != length
                or (
                    offset == 0
                    and length == size
                    and hashlib.sha1(
                        b"blob " + str(size).encode() + b"\0" + decoded
                    ).hexdigest()
                    != oid
                )
            ):
                raise failure
        if not identity_current():
            raise failure
        return result | {
            "candidate_sha": candidate_sha,
            "issue_iid": issue_iid,
            "mr_iid": mr_iid,
            "role": role,
        }
    except (
        BinasciiError,
        CandidateBoundaryError,
        GitLabError,
        KeyError,
        LedgerError,
        OSError,
        RepositoryError,
        TypeError,
        ValueError,
    ) as error:
        raise failure from error


def create_live_worker_read_session(
    record: LedgerRecord,
    issue: GitLabIssue,
    *,
    ledger: LedgerPort,
    repository: RepositoryPort,
    timeout_seconds: float,
    temporary_directory: Path | None = None,
) -> WorkerReadBrokerSession:
    """Create an opt-in read session bound to a live controller record."""
    if (
        record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
        or record.source_sha is None
        or issue.iid != record.issue_iid
    ):
        raise SegmentError("worker read identity is incomplete")
    identity = WorkerReadSessionIdentity(
        nonce=secrets.token_hex(16),
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        checkout=record.worktree,
        phase=record.phase.value,
        ledger_revision=record.revision,
        source_sha=record.source_sha,
    )

    def identity_check(candidate: WorkerReadSessionIdentity) -> bool:
        current = ledger.load(record.issue_iid)
        return (
            candidate == identity
            and current is not None
            and current.project_id == record.project_id
            and current.issue_iid == record.issue_iid
            and current.worktree == record.worktree
            and current.revision == record.revision
            and current.state is record.state
            and current.phase is record.phase
            and current.workflow_lane is record.workflow_lane
            and current.source_branch == record.source_branch
            and current.source_sha == record.source_sha
            and repository.head(record.worktree) == record.source_sha
        )

    def read_action(action: str) -> dict[str, object]:
        if action == "context":
            return {
                "state": record.state.value,
                "phase": record.phase.value,
                "workflow_lane": record.workflow_lane.value,
            }
        if action == "issue":
            return {"title": issue.title, "description": issue.description}
        if action == "ledger":
            return {
                "revision": record.revision,
                "source_branch": record.source_branch,
                "source_sha": record.source_sha or "",
                "state": record.state.value,
                "phase": record.phase.value,
            }
        raise ValueError("worker_read: invalid action")

    return create_worker_read_broker(
        identity,
        identity_check=identity_check,
        read_action=read_action,
        timeout_seconds=timeout_seconds,
        temporary_directory=temporary_directory,
    )


def _worker_git_projection(
    action: str, response: dict[str, object]
) -> dict[str, object]:
    """Expose only bounded operation results from full controller evidence."""
    fields = {
        "git_stage_candidate": ("candidate_tree", "state", "changed_path_count"),
        "git_candidate_status": (
            "candidate_tree",
            "state",
            "timing_status",
            "changed_path_count",
        ),
        "git_commit_candidate": (
            "candidate_tree",
            "committed_tree",
            "commit_sha",
            "red_sha",
            "state",
            "timing_status",
        ),
    }
    result: dict[str, object] = {}
    for key in fields[action]:
        value = response.get(key)
        if value is None:
            continue
        if key in {"candidate_tree", "committed_tree", "commit_sha", "red_sha"}:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
                raise ValueError("worker_git: invalid result identity")
        elif key == "changed_path_count":
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise ValueError("worker_git: invalid path count")
        elif not isinstance(value, str) or len(value.encode("utf-8")) > 64:
            raise ValueError("worker_git: invalid result state")
        result[key] = value
    return result


def _worker_validation_projection(
    response: dict[str, object], tree: str, selector: dict[str, object] | None
) -> dict[str, object]:
    """Project a controller report into one bounded public validation view."""
    if "raw_receipt" in response:
        raise ValueError("worker_validation: raw response is invalid")
    if response.get("candidate_tree") != tree:
        raise ValueError("worker_validation: candidate response changed")
    state = response.get("state")
    if not isinstance(state, str) or len(state) > 64:
        raise ValueError("worker_validation: response state invalid")
    verdict = response.get("verdict")
    if verdict is not None and verdict not in {"approve", "reject", "unknown"}:
        raise ValueError("worker_validation: response verdict invalid")
    timing_status = response.get("timing_status")
    if timing_status is None:
        timing_status = "unknown"
    if not isinstance(timing_status, str) or len(timing_status) > 64:
        raise ValueError("worker_validation: timing status invalid")
    result: dict[str, object] = {
        "candidate_tree": tree,
        "state": state,
        "verdict": verdict if verdict is not None else "unknown",
        "timing_status": timing_status,
    }
    if selector is not None and selector != {"view": "summary"} and "items" in response:
        raw_items = response["items"]
        total = response.get("total")
        next_offset = response.get("next_offset")
        offset = cast(int, selector["offset"])
        limit = cast(int, selector["limit"])
        if (
            not isinstance(raw_items, list)
            or len(raw_items) > limit
            or type(total) is not int
            or not 0 <= total <= 100_000
            or offset + len(raw_items) > total
            or next_offset != (offset + limit if offset + limit < total else None)
            or selector["view"] == "timing"
            and type(response.get("timing_complete")) is not bool
        ):
            raise ValueError("worker_validation: evidence page invalid")
        items: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise TypeError("worker_validation: evidence item invalid")
            item = cast(dict[str, object], item)
            if selector["view"] == "junit":
                environment, sha256, count = (
                    item.get("environment"),
                    item.get("sha256"),
                    item.get("test_count"),
                )
                if (
                    environment != selector["environment"]
                    or not isinstance(sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                    or type(count) is not int
                    or not 0 <= count <= 1_000_000
                ):
                    raise ValueError("worker_validation: JUnit page invalid")
                items.append(
                    {"environment": environment, "sha256": sha256, "test_count": count}
                )
            else:
                name, seconds = item.get("name"), item.get("seconds")
                if (
                    not isinstance(name, str)
                    or len(name) > 512
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in name.split("/")
                    or not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool)
                    or not math.isfinite(seconds)
                    or seconds < 0
                ):
                    raise ValueError("worker_validation: timing page invalid")
                items.append({"name": name, "seconds": seconds})
        result.update({"items": items, "total": total, "next_offset": next_offset})
        if selector["view"] == "timing":
            result["timing_complete"] = response["timing_complete"]
        return result
    artifacts = response.get("junit_artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("worker_validation: JUnit response invalid")
    public_artifacts: list[dict[str, object]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise TypeError("worker_validation: JUnit artifact invalid")
        item = cast(dict[str, object], item)
        environment, sha256, test_count = (
            item.get("environment"),
            item.get("sha256"),
            item.get("test_count"),
        )
        if (
            not isinstance(environment, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", environment) is None
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or type(test_count) is not int
            or not 0 <= test_count <= 1_000_000
        ):
            raise TypeError("worker_validation: JUnit artifact invalid")
        public_artifacts.append(
            {"environment": environment, "sha256": sha256, "test_count": test_count}
        )
    if selector is None or selector == {"view": "summary"}:
        result["junit_artifacts"] = public_artifacts
        return result
    environment = selector["environment"]
    offset = cast(int, selector["offset"])
    limit = cast(int, selector["limit"])
    if selector["view"] == "junit":
        items: list[dict[str, object]] = [
            item for item in public_artifacts if item["environment"] == environment
        ]
    else:
        timings = response.get("junit_timings", [])
        if not isinstance(timings, list) or len(timings) > 100:
            raise TypeError("worker_validation: timing response invalid")
        items = []
        timing_complete = False
        seen_timing = False
        for timing in timings:
            if not isinstance(timing, dict):
                raise TypeError("worker_validation: timing response invalid")
            timing = cast(dict[str, object], timing)
            if timing.get("environment") != environment:
                continue
            if seen_timing or type(timing.get("timing_complete")) is not bool:
                raise ValueError("worker_validation: timing completeness invalid")
            seen_timing = True
            timing_complete = cast(bool, timing["timing_complete"])
            cases = timing.get("over_100ms_cases", [])
            if not isinstance(cases, list) or len(cases) > 10_000:
                raise ValueError("worker_validation: timing cases invalid")
            for case in cases:
                if not isinstance(case, dict):
                    raise TypeError("worker_validation: timing case invalid")
                case = cast(dict[str, object], case)
                name, seconds = case.get("name"), case.get("seconds")
                if (
                    not isinstance(name, str)
                    or len(name) > 512
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in name.split("/")
                    or not isinstance(seconds, (int, float))
                    or isinstance(seconds, bool)
                    or not math.isfinite(seconds)
                    or seconds < 0
                ):
                    raise TypeError("worker_validation: timing case invalid")
                items.append({"name": name, "seconds": seconds})
        result["timing_complete"] = timing_complete
    total = len(items)
    result.update(
        {
            "items": items[offset : offset + limit],
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }
    )
    return result


def create_live_worker_validation_session(
    record: LedgerRecord,
    issue: GitLabIssue,
    *,
    ledger: LedgerPort,
    repository: RepositoryPort,
    candidate_action: Callable[
        [str, str | None, dict[str, object] | None], dict[str, object]
    ],
    candidate_tree: str,
    role: str,
    timeout_seconds: float,
    temporary_directory: Path | None = None,
) -> WorkerValidationBrokerSession:
    """Bind Judge validation to one staged candidate in a live workflow."""
    if role != "judge":
        raise ValueError("worker_validation: judge role required")
    if (
        record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
        or record.workflow_lane is not WorkflowLane.CODING
        or record.source_sha is None
        or issue.iid != record.issue_iid
        or re.fullmatch(r"[0-9a-f]{40}", candidate_tree) is None
    ):
        raise ValueError("worker_validation: identity is incomplete")
    expected_head = repository.head(record.worktree)
    if expected_head != record.source_sha:
        raise ValueError("worker_validation: source identity changed")
    repository.verify_worktree(record.worktree, record.source_branch, expected_head)
    identity = WorkerReadSessionIdentity(
        nonce=secrets.token_hex(16),
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        checkout=record.worktree,
        phase=record.phase.value,
        ledger_revision=record.revision,
        source_sha=record.source_sha,
    )

    def identity_check(candidate: WorkerReadSessionIdentity) -> bool:
        current = ledger.load_read_only(record.issue_iid)
        if (
            candidate != identity
            or current is None
            or current.project_id != record.project_id
            or current.issue_iid != record.issue_iid
            or current.revision != record.revision
            or current.state is not record.state
            or current.phase is not record.phase
            or current.workflow_lane is not record.workflow_lane
            or current.worktree != record.worktree
            or current.source_sha != record.source_sha
            or current.source_branch != record.source_branch
        ):
            return False
        try:
            repository.verify_worktree(
                record.worktree, record.source_branch, expected_head
            )
            return repository.head(record.worktree) == expected_head
        except (ValueError, RuntimeError, OSError):
            return False

    def validation_action(
        action: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        operation = "validate" if action == "validate" else "status"
        report = candidate_action(operation, candidate_tree, None)
        return _worker_validation_projection(report, candidate_tree, selector)

    return WorkerValidationBrokerSession(
        identity,
        identity_check=identity_check,
        validation_action=validation_action,
        candidate_tree=candidate_tree,
        timeout_seconds=timeout_seconds,
        temporary_directory=temporary_directory,
    )


def create_live_worker_git_session(
    record: LedgerRecord,
    issue: GitLabIssue,
    *,
    ledger: LedgerPort,
    repository: RepositoryPort,
    candidate_action: Callable[
        [str, str | None, dict[str, object] | None], dict[str, object]
    ],
    timeout_seconds: float,
    temporary_directory: Path | None = None,
) -> WorkerGitBrokerSession:
    """Create an opt-in Git session bound to one live worker and checkout."""
    if (
        record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
        or record.source_sha is None
        or issue.iid != record.issue_iid
    ):
        raise SegmentError("worker git identity is incomplete")
    source_sha = record.source_sha
    correction = record.correction_identity
    authored = (
        correction is not None and correction.mode == "authored_candidate_recovery"
    )
    candidate_base = correction.baseline_sha if authored and correction else source_sha
    expected_head = repository.head(record.worktree)
    if (
        authored
        and correction is not None
        and (
            correction.project_id != record.project_id
            or correction.issue_iid != record.issue_iid
            or correction.worktree != record.worktree
            or correction.source_branch != record.source_branch
            or correction.workflow_lane is not record.workflow_lane
            or correction.head_sha != source_sha
            or correction.red_checkpoint is None
            or correction.red_checkpoint.project_id != record.project_id
            or correction.red_checkpoint.issue_iid != record.issue_iid
            or correction.red_checkpoint.source_branch != record.source_branch
            or correction.red_checkpoint.base_sha != candidate_base
        )
    ):
        raise SegmentError("worker git correction identity is invalid")
    if expected_head != source_sha:
        if (
            record.workflow_lane is not WorkflowLane.CODING
            or record.source_branch != f"agent/issue-{record.issue_iid}"
            or not re.fullmatch(r"[0-9a-f]{40}", expected_head)
            or not hasattr(repository, "green_commit_dependencies")
            or not hasattr(repository, "red_commit_dependencies")
        ):
            raise SegmentError("worker git retained Green HEAD proof is unavailable")
        commit_repository = cast(_CommitRepository, repository)
        try:
            green = commit_repository.green_commit_dependencies(record.worktree, Path())
            red = commit_repository.red_commit_dependencies(record.worktree, Path())
            red_sha, green_tree, _message = green.commit_identity(expected_head)
            red_base, red_tree = red.commit_identity(red_sha)
            proven = (
                all(
                    re.fullmatch(r"[0-9a-f]{40}", value)
                    for value in (red_sha, green_tree, red_tree)
                )
                and red_base == candidate_base
                and (
                    not authored
                    or correction is not None
                    and correction.red_checkpoint is not None
                    and correction.red_checkpoint.tree_oid == red_tree
                )
                and red.read_ref(red_commit_ref(record.issue_iid, candidate_base))
                == red_sha
                and green.read_ref(
                    green_commit_ref(record.issue_iid, candidate_base, green_tree)
                )
                == expected_head
            )
        except (RepositoryError, ValueError, TypeError, AssertionError):
            proven = False
        if not proven:
            raise SegmentError("worker git retained Green HEAD proof is invalid")
    repository.verify_worktree(record.worktree, record.source_branch, expected_head)
    identity = WorkerReadSessionIdentity(
        nonce=secrets.token_hex(16),
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        checkout=record.worktree,
        phase=record.phase.value,
        ledger_revision=record.revision,
        source_sha=record.source_sha,
    )

    def identity_check(candidate: WorkerReadSessionIdentity) -> bool:
        current = ledger.load_read_only(record.issue_iid)
        if (
            candidate != identity
            or current is None
            or current.project_id != record.project_id
            or current.issue_iid != record.issue_iid
            or current.worktree != record.worktree
            or current.revision != record.revision
            or current.state is not record.state
            or current.phase is not record.phase
            or current.workflow_lane is not record.workflow_lane
            or current.work_kind is not record.work_kind
            or current.correction_identity != record.correction_identity
            or current.source_branch != record.source_branch
            or current.source_sha != record.source_sha
        ):
            return False
        return checkout_check()

    def checkout_check() -> bool:
        repository.verify_worktree(record.worktree, record.source_branch, expected_head)
        return repository.head(record.worktree) == expected_head

    def git_action(action: str, tree: str | None) -> dict[str, object]:
        nonlocal expected_head
        if action == "git_status":
            paths = repository.changed_paths(record.worktree)
            path_bytes = tuple(len(path.encode("utf-8")) for path in paths)
            if (
                len(paths) > 128
                or any(size > 512 for size in path_bytes)
                or sum(path_bytes) > 16_384
            ):
                raise ValueError("worker_git: status too large")
            return {"changed_paths": list(paths), "dirty": bool(paths)}
        if action == "git_stage_candidate":
            response = candidate_action("stage", None, None)
            return _worker_git_projection(action, response)
        if action == "git_candidate_status":
            response = candidate_action("status", tree, None)
            if response.get("candidate_tree") != tree:
                raise ValueError("worker_git: candidate status tree changed")
            return _worker_git_projection(action, response)
        if action == "git_commit_candidate":
            if record.workflow_lane is not WorkflowLane.CODING:
                raise ValueError("worker_git: commit requires coding lane")
            response = candidate_action("commit", tree, None)
            commit_sha = response.get("commit_sha")
            if (
                response.get("state") != "terminal-success"
                or response.get("candidate_tree") != tree
                or response.get("committed_tree") != tree
                or not isinstance(commit_sha, str)
                or len(commit_sha) != 40
                or any(char not in "0123456789abcdef" for char in commit_sha)
            ):
                raise ValueError("worker_git: candidate commit is invalid")
            repository.verify_worktree(
                record.worktree, record.source_branch, commit_sha
            )
            expected_head = commit_sha
            return _worker_git_projection(action, response)
        raise ValueError("worker_git: invalid action")

    return WorkerGitBrokerSession(
        identity,
        identity_check=identity_check,
        git_action=git_action,
        reply_check=checkout_check,
        timeout_seconds=timeout_seconds,
        temporary_directory=temporary_directory,
    )


def retained_precheckpoint_failure_is_untouched(record: LedgerRecord) -> bool:
    """Allow exact worker failure evidence without treating it as authored work."""
    latest = record.condition_history[-1] if record.condition_history else None
    if record.error_code is ErrorCode.RETRY_EXHAUSTED and latest is not None:
        worker_failure = latest.code in {
            ErrorCode.WORKER_EXIT.value,
            ErrorCode.WORKER_TIMEOUT.value,
        }
        if worker_failure:
            if latest.detail != "Automatic retry budget exhausted.":
                return False
            if latest.code == ErrorCode.WORKER_EXIT.value:
                if record.worker_exit is None:
                    return False
            elif record.worker_exit is not None:
                return False
            return retained_ordinary_is_untouched(
                record.model_copy(update={"worker_exit": None})
            )
    return retained_ordinary_is_untouched(record)


def automatic_blocked_static_rejection(
    record: LedgerRecord,
    *,
    now: datetime,
    project_id: int | None = None,
    state_root: Path | None = None,
) -> str | None:
    """Give one exact static reason before live recovery proof is attempted."""
    if record.state is not RunnerState.BLOCKED:
        return "record is not blocked"
    if project_id is not None and record.project_id != project_id:
        return "record belongs to another project"
    if record.work_kind is not WorkKind.ORDINARY:
        return "work kind requires reviewed recovery"
    if record.merge_request_iid is not None or record.merge_request_url is not None:
        return "retained merge request evidence requires reviewed recovery"
    if record.publication_sha is not None or record.merge_request_draft is not None:
        return "retained publication evidence requires reviewed recovery"
    if record.intent is not None:
        return "retained transition intent requires reviewed recovery"
    if record.phase not in {WorkflowPhase.PREPARING, WorkflowPhase.WORKFLOW}:
        return "workflow phase requires reviewed recovery"
    if record.workflow_lane is not WorkflowLane.UNKNOWN:
        return "workflow lane already has progress"
    if record.error_code not in {
        ErrorCode.WORKER_PROTOCOL_INVALID,
        ErrorCode.WORKER_RECEIPT_INVALID,
        ErrorCode.RETRY_EXHAUSTED,
    }:
        return "blocked failure code is not eligible"
    latest = record.condition_history[-1] if record.condition_history else None
    if latest is None or not (
        latest.code == record.error_code.value
        or (
            record.error_code is ErrorCode.RETRY_EXHAUSTED
            and latest.code
            in {ErrorCode.WORKER_EXIT.value, ErrorCode.WORKER_TIMEOUT.value}
        )
    ):
        return "retained failure condition is not exact"
    if record.source_sha is None or record.source_sha != record.main_sha:
        return "retained source does not match the baseline"
    if record.source_branch != f"agent/issue-{record.issue_iid}":
        return "retained source branch is not exact"
    if state_root is not None and record.worktree != (
        state_root / "worktrees" / str(record.issue_iid)
    ):
        return "retained worktree path is not exact"
    if not retained_precheckpoint_failure_is_untouched(record):
        return "retained workflow or worker failure evidence is not untouched"
    if record.worktree_dirty:
        return "retained worktree is dirty"
    if record.owner_status != "missing":
        return "retained owner is not missing"
    if any(state != "closed" for _, state in record.split_prerequisites):
        return "split prerequisite is still open"
    if record.retry_at is not None and record.retry_at > now:
        return "recovery cooldown is active"
    return None


def _broker_artifact_builder(
    record: LedgerRecord,
    request: WorkerResultBrokerRequest,
    *,
    repository: RepositoryPort,
    red_checkpoint: RedCheckpointEvidence | None,
    apply_red_checkpoint: Callable[
        [int, RedCheckpointRequest, str], RedCheckpointEvidence
    ]
    | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
) -> Callable[[bytes], bytes]:
    """Bind one live broker to the controller's exact current observations."""
    if (
        record.project_id != request.project_id
        or record.issue_iid != request.issue_iid
        or record.revision != request.ledger_revision
        or record.state is not request.expected_state
        or record.phase is not request.expected_phase
        or record.source_branch != request.source_branch
        or record.source_sha != request.source_sha
    ):
        raise WorkerProtocolError("worker_result_broker", "identity mismatch")

    uses_authored_base = (
        record.correction_identity is not None
        and record.correction_identity.mode == "authored_candidate_recovery"
    )
    candidate_base_sha = (
        record.correction_identity.baseline_sha
        if uses_authored_base and record.correction_identity is not None
        else record.source_sha
    )
    candidate_identity: tuple[str, tuple[str, ...], str] | None = None

    def build(input_bytes: bytes) -> bytes:
        nonlocal candidate_identity
        try:
            changed_paths = tuple(
                sorted(
                    set(
                        repository.publication_changed_paths(
                            record.worktree, candidate_base_sha or ""
                        )
                        if uses_authored_base
                        or (
                            red_checkpoint is not None
                            and record.workflow_lane is WorkflowLane.CODING
                            and repository.head(record.worktree) != candidate_base_sha
                        )
                        else repository.changed_paths(record.worktree)
                    )
                )
            )
        except RepositoryError as error:
            raise WorkerProtocolError(
                "controller_invariant.changed_paths", "could not be observed"
            ) from error
        try:
            observed_head = repository.head(record.worktree)
            candidate_tree = (
                record.source_sha
                if not changed_paths
                else repository.red_candidate_tree(
                    record.worktree, candidate_base_sha or "", changed_paths
                )
            )
        except RepositoryError as error:
            raise WorkerProtocolError(
                "controller_invariant.red_checkpoint", "candidate could not be observed"
            ) from error
        current_identity = (observed_head, changed_paths, candidate_tree)
        if candidate_identity is None:
            candidate_identity = current_identity
        elif current_identity != candidate_identity:
            raise WorkerProtocolError("worker_result_broker", "candidate changed")
        build_input = parse_worker_result_build_input(input_bytes)
        accepted_checkpoint = red_checkpoint
        if build_input.result_kind == "red_checkpoint_ready":
            if (
                record.workflow_lane is not WorkflowLane.UNKNOWN
                or build_input.workflow_lane is not WorkflowLane.CODING
                or build_input.red_checkpoint_request is None
                or apply_red_checkpoint is None
                or load_red_checkpoint is None
                or record.source_sha is None
            ):
                raise WorkerProtocolError(
                    "controller_invariant.red_checkpoint", "request is not exact"
                )
            if changed_paths != build_input.red_checkpoint_request.test_paths:
                raise WorkerProtocolError(
                    "controller_invariant.red_checkpoint", "changed paths do not match"
                )
            if candidate_tree is None:
                raise WorkerProtocolError(
                    "controller_invariant.red_checkpoint",
                    "candidate could not be observed",
                )
            try:
                accepted_checkpoint = apply_red_checkpoint(
                    record.issue_iid,
                    build_input.red_checkpoint_request,
                    candidate_tree,
                )
                reloaded = load_red_checkpoint(
                    record.project_id, record.issue_iid, record.source_sha
                )
            except (RedCheckpointError, RepositoryError, ValueError) as error:
                raise WorkerProtocolError(
                    "controller_invariant.red_checkpoint", "request was rejected"
                ) from error
            if accepted_checkpoint != reloaded:
                raise WorkerProtocolError(
                    "controller_invariant.red_checkpoint", "checkpoint did not reload"
                )
        result = build_worker_result(
            record,
            build_input,
            changed_paths=changed_paths,
            red_checkpoint=accepted_checkpoint,
        )
        return build_worker_result_artifact(
            record,
            result,
            nonce=request.nonce,
            changed_paths=changed_paths,
            red_checkpoint=(
                accepted_checkpoint
                if result.result_kind == "red_checkpoint_ready"
                else None
            ),
        )

    return build


def build_publication_correction_identity(
    record: LedgerRecord,
    candidate: LegacyPublicationCandidate,
    *,
    checkpoint: RedCheckpointEvidence | None,
) -> PublicationCorrectionIdentity:
    """Build one exact dirty-candidate snapshot without changing repository state."""
    if (
        record.source_sha is None
        or record.workflow_lane is WorkflowLane.UNKNOWN
        or candidate.branch != record.source_branch
        or candidate.head != record.source_sha
        or candidate.has_untracked
        or candidate.has_unmerged
        or candidate.has_renames
        or not candidate.staged_paths
    ):
        raise SegmentError("publication correction evidence is not exact")
    if record.workflow_lane is WorkflowLane.CODING:
        if (
            checkpoint is None
            or checkpoint.project_id != record.project_id
            or checkpoint.issue_iid != record.issue_iid
            or checkpoint.source_branch != record.source_branch
            or checkpoint.base_sha != record.source_sha
            or not checkpoint.test_paths
        ):
            raise SegmentError("publication correction Red evidence is not exact")
    elif checkpoint is not None:
        raise SegmentError("content correction cannot retain Red evidence")
    return PublicationCorrectionIdentity(
        mode="publication_commit_rejected",
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        worktree=record.worktree,
        source_branch=record.source_branch,
        baseline_sha=record.source_sha,
        head_sha=candidate.head,
        merge_head=candidate.merge_head,
        index_fingerprint=candidate.index_fingerprint,
        staged_paths=tuple(sorted(candidate.staged_paths)),
        unstaged_paths=tuple(sorted(candidate.unstaged_paths)),
        workflow_lane=record.workflow_lane,
        red_checkpoint=checkpoint,
    )


def build_authored_candidate_correction_identity(
    record: LedgerRecord,
    candidate: LegacyPublicationCandidate,
    *,
    original_base_sha: str | None,
    checkpoint: RedCheckpointEvidence | None,
    bootstrap: bool = True,
) -> PublicationCorrectionIdentity:
    """Bind a reviewed committed candidate to its original Red provenance."""
    if (
        record.source_sha is None
        or record.work_kind is not WorkKind.ORDINARY
        or record.workflow_lane is not WorkflowLane.CODING
        or record.source_branch != f"agent/issue-{record.issue_iid}"
        or record.publication_sha is not None
        or record.pending_feedback is not None
        or record.repair_attempt is not None
        or record.worker_exit is not None
        or candidate.branch != record.source_branch
        or candidate.head != record.source_sha
        or candidate.merge_head is not None
        or not candidate.staged_paths
        or candidate.unstaged_paths
        or candidate.has_untracked
        or candidate.has_unmerged
        or candidate.has_renames
        or original_base_sha is None
        or checkpoint is None
        or checkpoint.project_id != record.project_id
        or checkpoint.issue_iid != record.issue_iid
        or checkpoint.source_branch != record.source_branch
        or checkpoint.base_sha != original_base_sha
        or not checkpoint.test_paths
        or (
            bootstrap
            and (
                record.state is not RunnerState.BLOCKED
                or record.phase is not WorkflowPhase.WORKFLOW
                or record.prior_state is not RunnerState.EXECUTING
                or record.prior_phase is not WorkflowPhase.WORKFLOW
                or record.intent is not None
                or record.audit_evidence != "unknown"
                or record.merge_request_draft is None
                or record.error_code
                not in {
                    ErrorCode.WORKER_PROTOCOL_INVALID,
                    ErrorCode.STATE_RECONCILIATION_FAILED,
                }
                or not record.condition_history
                or (
                    record.error_code is ErrorCode.WORKER_PROTOCOL_INVALID
                    and (
                        not any(
                            item.code == ErrorCode.WORKER_PROTOCOL_INVALID.value
                            for item in record.condition_history
                        )
                        or not all(
                            item.code
                            in {
                                ErrorCode.WORKER_PROTOCOL_INVALID.value,
                                ErrorCode.RETRY_EXHAUSTED.value,
                            }
                            for item in record.condition_history
                        )
                    )
                )
                or (
                    record.error_code is ErrorCode.STATE_RECONCILIATION_FAILED
                    and not any(
                        item.code == ErrorCode.STATE_RECONCILIATION_FAILED.value
                        and item.detail == "candidate_broker: submission failed"
                        for item in record.condition_history
                    )
                )
            )
        )
    ):
        raise SegmentError("authored candidate recovery evidence is not exact")
    return PublicationCorrectionIdentity(
        mode="authored_candidate_recovery",
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        worktree=record.worktree,
        source_branch=record.source_branch,
        baseline_sha=original_base_sha,
        head_sha=candidate.head,
        merge_head=None,
        index_fingerprint=candidate.index_fingerprint,
        staged_paths=tuple(sorted(candidate.staged_paths)),
        unstaged_paths=(),
        workflow_lane=record.workflow_lane,
        red_checkpoint=checkpoint,
    )


def correction_identity_matches(
    record: LedgerRecord,
    candidate: LegacyPublicationCandidate,
    *,
    checkpoint: RedCheckpointEvidence | None,
    original_base_sha: str | None = None,
) -> bool:
    """Return whether a fresh read-only observation equals the frozen identity."""
    identity = record.correction_identity
    if identity is None:
        return False
    try:
        observed = (
            build_authored_candidate_correction_identity(
                record,
                candidate,
                original_base_sha=original_base_sha,
                checkpoint=checkpoint,
                bootstrap=False,
            )
            if identity.mode == "authored_candidate_recovery"
            else build_publication_correction_identity(
                record, candidate, checkpoint=checkpoint
            )
        )
    except SegmentError:
        return False
    return observed == identity


def _observed_correction_identity(
    record: LedgerRecord,
    *,
    repository: RepositoryPort,
    checkpoint: RedCheckpointEvidence | None,
) -> PublicationCorrectionIdentity:
    """Rebuild one correction identity using its retained provenance mode."""
    candidate = repository.observe_legacy_publication_candidate(record.worktree)
    if (
        record.correction_identity is not None
        and record.correction_identity.mode == "authored_candidate_recovery"
    ):
        return build_authored_candidate_correction_identity(
            record,
            candidate,
            original_base_sha=repository.single_parent(
                record.worktree, record.source_sha or ""
            ),
            checkpoint=checkpoint,
            bootstrap=False,
        )
    return build_publication_correction_identity(
        record, candidate, checkpoint=checkpoint
    )


def refresh_publication_correction_identity(
    record: LedgerRecord,
    *,
    repository: RepositoryPort,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
) -> LedgerRecord | None:
    """Replace a correction snapshot after an authorized worker attempt."""
    if record.correction_identity is None:
        return record
    checkpoint = _retained_red_checkpoint(record, load_red_checkpoint)
    try:
        identity = _observed_correction_identity(
            record, repository=repository, checkpoint=checkpoint
        )
    except (RepositoryError, SegmentError):
        return None
    return record.model_copy(update={"correction_identity": identity})


def _retained_red_checkpoint(
    record: LedgerRecord,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
) -> RedCheckpointEvidence | None:
    """Resolve trusted Red provenance across an existing-MR source update."""
    if record.workflow_lane is WorkflowLane.CONTENT or load_red_checkpoint is None:
        return None
    if (
        record.correction_identity is not None
        and record.correction_identity.mode == "authored_candidate_recovery"
    ):
        checkpoint = load_red_checkpoint(
            record.project_id,
            record.issue_iid,
            record.correction_identity.baseline_sha,
        )
        return (
            checkpoint
            if checkpoint == record.correction_identity.red_checkpoint
            else None
        )
    if record.source_sha is not None:
        current = load_red_checkpoint(
            record.project_id, record.issue_iid, record.source_sha
        )
        if current is not None:
            return current
    audit = record.audit_evidence
    checkpoint = None if audit in {None, "unknown"} else audit.red_checkpoint
    if checkpoint is None:
        return None
    if (
        checkpoint.project_id != record.project_id
        or checkpoint.issue_iid != record.issue_iid
        or checkpoint.source_branch != record.source_branch
    ):
        return None
    reloaded = load_red_checkpoint(
        record.project_id, record.issue_iid, checkpoint.base_sha
    )
    return reloaded if reloaded == checkpoint else None


AUTOMATIC_REPAIR_MAX_ATTEMPTS = 3
AUTOMATIC_REPAIR_BACKOFF_SECONDS = 20 * 60


def collect_exact_failed_job_diagnostics(
    *,
    jobs: tuple[PipelineJobEvidence | Mapping[str, object], ...],
    pipeline_id: int,
    source_sha: str,
    fetch_trace: Callable[[int], JobTraceEvidence | Mapping[str, object] | str | None],
) -> tuple[tuple[PipelineJobEvidence, JobTraceEvidence], ...]:
    """Collect at most eight current failures after validating the full inventory."""
    try:
        validated = tuple(PipelineJobEvidence.model_validate(job) for job in jobs)
    except ValueError as error:
        raise SegmentError("exact pipeline job evidence is invalid") from error
    if len({job.id for job in validated}) != len(validated) or any(
        job.pipeline_id != pipeline_id or job.pipeline_sha != source_sha
        for job in validated
    ):
        raise SegmentError("exact pipeline job inventory is ambiguous or stale")
    failed = tuple(
        sorted(
            (job for job in validated if job.status == "failed" and not job.retried),
            key=lambda job: job.id,
        )
    )
    if not failed or len(failed) > 8:
        raise SegmentError("exact pipeline failed-job count is invalid")
    diagnostics = []
    for job in failed:
        try:
            raw_trace = fetch_trace(job.id)
            trace = (
                JobTraceEvidence(text=raw_trace)
                if isinstance(raw_trace, str)
                else JobTraceEvidence.model_validate(raw_trace)
            )
        except (TypeError, ValueError, GitLabError) as error:
            raise SegmentError("exact failed-job trace is unavailable") from error
        if not trace.text.strip():
            raise SegmentError("exact failed-job trace is unavailable")
        diagnostics.append((job, trace))
    return tuple(diagnostics)


def _required_pipeline_jobs_succeeded(
    jobs: tuple[PipelineJobEvidence, ...], *, pipeline_id: int, source_sha: str
) -> bool | None:
    """Check a complete exact inventory; return None when no current job exists."""
    if len({job.id for job in jobs}) != len(jobs) or any(
        job.pipeline_id != pipeline_id or job.pipeline_sha != source_sha for job in jobs
    ):
        raise SegmentError("exact pipeline job inventory is ambiguous or stale")
    current = tuple(job for job in jobs if not job.retried)
    if not current:
        return None
    required = tuple(job for job in current if not job.allow_failure)
    if not required:
        return None
    return all(job.status == "success" for job in required)


def record_failed_job_diagnostic_unavailable(
    record: LedgerRecord, *, ledger: LedgerPort, gitlab: GitLabPort
) -> LedgerRecord:
    """Persist a safe, worker-free status when exact diagnostic collection fails."""
    if record.state is RunnerState.NEEDS_REPAIR:
        return record
    blocked = ledger.persist(
        transition_record(
            record,
            RunnerState.NEEDS_REPAIR,
            phase=WorkflowPhase.POST_PUBLICATION,
            error_code=ErrorCode.PIPELINE_REJECTED,
            summary="The exact failed-job diagnostic is unavailable.",
            next_action="Restore exact failed-job evidence before starting repair.",
        )
    )
    gitlab.transition_labels(blocked.issue_iid, label_for_state(blocked.state))
    return blocked


class SchedulingScope(str, Enum):
    """The durable scope affected by a scheduling decision."""

    ITEM = "item"


class ItemSchedulingAction(str, Enum):
    """The closed item-local scheduler action vocabulary."""

    RESUME = "resume"
    DEFER = "defer"
    QUARANTINE = "quarantine"
    SKIP = "skip"


class ProjectStopReason(str, Enum):
    """The only trusted conditions that stop the whole project."""

    PROJECT_IDENTITY = "project-identity"
    CREDENTIALS = "credentials"
    LOCK_OWNERSHIP = "lock-ownership"
    SHARED_STATE_CORRUPT = "shared-ledger-corrupt"
    CROSS_ITEM_IDENTITY_AMBIGUOUS = "cross-item-identity-ambiguous"


class AdoptionAction(str, Enum):
    """Controller fast-forward reconciliation outcomes."""

    ADOPT = "adopt"
    QUARANTINE = "quarantine"


@dataclass(frozen=True)
class SchedulerDisposition:
    """One pure durable scheduling decision."""

    scope: SchedulingScope
    action: ItemSchedulingAction


@dataclass(frozen=True)
class SchedulerInventoryPlan:
    """A deterministic plan for one serial scheduler inventory."""

    selected_issue_iid: int | None
    project_stop: ProjectStopReason | None
    item_actions: Mapping[int, SchedulerDisposition]


@dataclass(frozen=True)
class ControllerAdoptionDecision:
    """An item-local fast-forward adoption decision."""

    action: AdoptionAction


def classify_scheduler_disposition(
    record: LedgerRecord, *, now: datetime
) -> SchedulerDisposition:
    """Classify every runner state without deriving authority from task text."""
    if record.state in IMMEDIATE_OWNER_STATES:
        return SchedulerDisposition(SchedulingScope.ITEM, ItemSchedulingAction.RESUME)
    if record.state is RunnerState.RETRY_WAIT:
        return SchedulerDisposition(
            SchedulingScope.ITEM,
            ItemSchedulingAction.RESUME
            if _due(record, now)
            else ItemSchedulingAction.DEFER,
        )
    if record.state in {RunnerState.NEEDS_REPAIR, RunnerState.REPAIR_FAILED} or (
        record.state is RunnerState.BLOCKED
        and record.error_code is ErrorCode.REMEDIATION_MR_MISMATCH
    ):
        return SchedulerDisposition(
            SchedulingScope.ITEM, ItemSchedulingAction.QUARANTINE
        )
    return SchedulerDisposition(SchedulingScope.ITEM, ItemSchedulingAction.SKIP)


def plan_scheduler_inventory(
    records: tuple[LedgerRecord, ...],
    *,
    now: datetime,
    apply: bool = False,
    project_fault: str | None = None,
    untrusted_text: str = "",
) -> SchedulerInventoryPlan:
    """Plan serial ownership from trusted durable records only.

    ``apply`` and untrusted prose deliberately do not affect this pure plan; the
    caller performs the corresponding item-local persistence at its boundary.
    """
    del apply, untrusted_text
    fault = ProjectStopReason(project_fault) if project_fault is not None else None
    actions = {
        record.issue_iid: classify_scheduler_disposition(record, now=now)
        for record in records
    }
    if fault is not None:
        return SchedulerInventoryPlan(None, fault, MappingProxyType(actions))
    owners = sorted(
        (
            record
            for record in records
            if actions[record.issue_iid].action is ItemSchedulingAction.RESUME
            and record.state in IMMEDIATE_OWNER_STATES
        ),
        key=lambda record: record.issue_iid,
    )
    if len(owners) > 1:
        return SchedulerInventoryPlan(
            None,
            ProjectStopReason.CROSS_ITEM_IDENTITY_AMBIGUOUS,
            MappingProxyType(actions),
        )
    if owners:
        return SchedulerInventoryPlan(
            owners[0].issue_iid, None, MappingProxyType(actions)
        )
    due = sorted(
        (
            record
            for record in records
            if actions[record.issue_iid].action is ItemSchedulingAction.RESUME
        ),
        key=lambda record: (record.retry_at or now, record.issue_iid),
    )
    return SchedulerInventoryPlan(
        None if not due else due[0].issue_iid, None, MappingProxyType(actions)
    )


def classify_controller_descendant_adoption(
    record: LedgerRecord,
    merge_request: MergeRequestEvidence,
    *,
    corroboration: dict[str, bool],
) -> ControllerAdoptionDecision:
    """Classify an already-computed exact fast-forward corroboration boundary."""
    required = {
        "same_numeric_project",
        "exact_merge_request_iid",
        "exact_merge_request_url",
        "source_branch",
        "target_project_and_branch",
        "retained_worktree_exists",
        "retained_worktree_clean",
        "retained_worktree_branch",
        "worktree_head_matches_merge_request",
        "retained_sha_is_ancestor",
        "no_competing_identity",
    }
    del record, merge_request
    return ControllerAdoptionDecision(
        AdoptionAction.ADOPT
        if required == set(corroboration) and all(corroboration.values())
        else AdoptionAction.QUARANTINE
    )


def _automatic_repair_failure(
    record: LedgerRecord, *, now: datetime, reason: str
) -> tuple[int, datetime | None, str, str]:
    """Return durable item-local automatic repair eligibility and guidance."""
    attempts = record.automatic_repair_count
    if attempts >= AUTOMATIC_REPAIR_MAX_ATTEMPTS:
        return (
            attempts,
            None,
            "Automatic repair budget exhausted; this item is quarantined.",
            "Inspect the retained evidence; other eligible work may continue.",
        )
    return (
        attempts,
        now + timedelta(seconds=AUTOMATIC_REPAIR_BACKOFF_SECONDS),
        f"Automatic repair deferred after failure: {reason}",
        "Wait for this item's bounded automatic repair backoff.",
    )


def activate_exact_pipeline_repair(
    record: LedgerRecord,
    *,
    merge_request: MergeRequestEvidence,
    pipeline: PipelineEvidence,
    failed_job: PipelineJobEvidence | Mapping[str, object],
    diagnostic: JobTraceEvidence | Mapping[str, object],
    additional_job_diagnostics: tuple[
        tuple[PipelineJobEvidence, JobTraceEvidence], ...
    ] = (),
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
) -> LedgerRecord:
    """Persist one bounded repair attempt for an exact failed MR pipeline."""
    if (
        record.state
        not in {
            RunnerState.OBSERVING,
            RunnerState.NEEDS_REPAIR,
            RunnerState.REPAIR_FAILED,
        }
        or record.source_sha != merge_request.sha
        or record.source_branch != merge_request.source_branch
        or record.merge_request_iid != merge_request.iid
        or pipeline.status != "failed"
        or pipeline.sha != record.source_sha
    ):
        raise SegmentError("exact failed pipeline repair evidence is invalid")
    try:
        pairs = (
            (
                PipelineJobEvidence.model_validate(failed_job),
                JobTraceEvidence.model_validate(diagnostic),
            ),
            *additional_job_diagnostics,
        )
        if not 1 <= len(pairs) <= 8 or len({job.id for job, _ in pairs}) != len(pairs):
            raise ValueError("invalid failed-job count or identity")
        if tuple(job.id for job, _ in pairs) != tuple(
            sorted(job.id for job, _ in pairs)
        ):
            raise ValueError("failed jobs are not ordered")
        if any(
            job.pipeline_id != pipeline.id
            or job.pipeline_sha != pipeline.sha
            or job.status != "failed"
            or job.retried
            for job, _ in pairs
        ):
            raise ValueError("failed-job identity is invalid")
        diagnostics = tuple(
            FailedJobDiagnosticEvidence(
                project_id=record.project_id,
                merge_request_iid=merge_request.iid,
                source_sha=merge_request.sha,
                pipeline_id=pipeline.id,
                pipeline_sha=pipeline.sha,
                pipeline_status="failed",
                job_id=job.id,
                job_name=job.name,
                job_stage=job.stage,
                job_status="failed",
                failure_reason=job.failure_reason,
                retried=job.retried,
                trace_provenance=trace.provenance,
                trace_status=trace.status,
                trace=trace.text,
            )
            for job, trace in pairs
        )
    except ValueError as error:
        raise SegmentError("exact failed-job diagnostic evidence is invalid") from error
    failed_diagnostic = diagnostics[0]
    additional_diagnostics = diagnostics[1:]
    diagnostic_fingerprints = tuple(
        hashlib.sha256(
            json.dumps(
                item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        for item in diagnostics
    )
    snapshot: dict[str, object] = {
        "issue_iid": record.issue_iid,
        "project_id": record.project_id,
        "merge_request_iid": merge_request.iid,
        "merge_request_url": merge_request.web_url,
        "source_branch": merge_request.source_branch,
        "source_sha": merge_request.sha,
        "pipeline_id": pipeline.id,
        "pipeline_sha": pipeline.sha,
        "pipeline_status": pipeline.status,
        "pipeline_url": pipeline.web_url,
        "failed_job_id": failed_diagnostic.job_id,
        "failed_job_ids": [item.job_id for item in diagnostics],
        "trace_provenance": failed_diagnostic.trace_provenance,
        "trace_status": failed_diagnostic.trace_status,
        "diagnostic_fingerprint": diagnostic_fingerprints[0],
        "diagnostic_fingerprints": list(diagnostic_fingerprints),
    }
    fingerprint = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if record.automatic_repair_count >= AUTOMATIC_REPAIR_MAX_ATTEMPTS:
        exhausted = ledger.persist(
            transition_record(
                record,
                RunnerState.REPAIR_FAILED,
                phase=WorkflowPhase.POST_PUBLICATION,
                error_code=ErrorCode.RETRY_EXHAUSTED,
                automatic_repair_at=None,
                repair_attempt=RepairAttemptEvidence(
                    fingerprint=fingerprint,
                    snapshot=snapshot,
                    origin="automatic-exact-pipeline",
                    diagnostic=failed_diagnostic,
                    additional_diagnostics=additional_diagnostics,
                    failure_reason="Automatic repair budget exhausted.",
                    started_at=now.isoformat(),
                    elapsed_seconds=0.0,
                ),
                summary="Automatic repair budget exhausted; this item is quarantined.",
                next_action="Inspect the retained evidence; other eligible work may continue.",
            )
        )
        gitlab.transition_labels(exhausted.issue_iid, label_for_state(exhausted.state))
        return exhausted
    if (
        record.state is RunnerState.OBSERVING
        and record.repair_attempt is not None
        and record.repair_attempt.origin == "automatic-exact-pipeline"
    ):
        failed_attempt = RepairAttemptEvidence(
            fingerprint=fingerprint,
            snapshot=snapshot,
            origin="automatic-exact-pipeline",
            diagnostic=failed_diagnostic,
            additional_diagnostics=additional_diagnostics,
            evidence_delta={
                "pipeline_status": "failed",
                "prior_repair_fingerprint": record.repair_attempt.fingerprint,
            },
            failure_reason="The exact pipeline remained failed after repair.",
            started_at=record.repair_attempt.started_at,
            elapsed_seconds=record.repair_attempt.elapsed_seconds,
        )
        attempts, retry_at, summary, next_action = _automatic_repair_failure(
            record, now=now, reason="the exact pipeline remained failed"
        )
        failed = ledger.persist(
            transition_record(
                record,
                RunnerState.REPAIR_FAILED,
                phase=WorkflowPhase.POST_PUBLICATION,
                error_code=(
                    ErrorCode.RETRY_EXHAUSTED
                    if attempts >= AUTOMATIC_REPAIR_MAX_ATTEMPTS
                    else ErrorCode.PIPELINE_REJECTED
                ),
                automatic_repair_count=attempts,
                automatic_repair_at=retry_at,
                repair_attempt=failed_attempt,
                summary=summary,
                next_action=next_action,
            )
        )
        gitlab.transition_labels(failed.issue_iid, label_for_state(failed.state))
        return failed
    prior_failure = (
        record.repair_attempt.failure_reason
        if record.state is RunnerState.REPAIR_FAILED
        and record.repair_attempt is not None
        else ""
    )
    if record.state is RunnerState.REPAIR_FAILED:
        record = ledger.persist(
            transition_record(
                record,
                RunnerState.NEEDS_REPAIR,
                phase=WorkflowPhase.POST_PUBLICATION,
                summary="The exact failed pipeline was freshly corroborated.",
                next_action="Run one bounded repair workflow.",
            )
        )
    activated = ledger.persist(
        transition_record(
            record,
            RunnerState.REPAIR,
            phase=WorkflowPhase.WORKFLOW,
            error_code=ErrorCode.PIPELINE_REJECTED,
            automatic_repair_count=record.automatic_repair_count + 1,
            automatic_repair_at=None,
            repair_attempt=RepairAttemptEvidence(
                fingerprint=fingerprint,
                snapshot=snapshot,
                origin="automatic-exact-pipeline",
                diagnostic=failed_diagnostic,
                additional_diagnostics=additional_diagnostics,
                evidence_delta=(
                    {"prior_repair_failure": prior_failure} if prior_failure else {}
                ),
                failure_reason="",
                started_at=now.isoformat(),
                elapsed_seconds=0.0,
            ),
            summary="The exact current-source pipeline failed; repair activated.",
            next_action="Run the bounded repair workflow.",
        )
    )
    gitlab.transition_labels(activated.issue_iid, label_for_state(activated.state))
    return activated


def _record_repair_failure(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    evidence_delta: dict[str, object],
    failure_reason: str,
) -> LedgerRecord:
    """Close one bounded repair attempt with origin-accurate guidance."""
    attempt = record.repair_attempt
    if record.state is not RunnerState.REPAIR or attempt is None:
        raise ValueError("repair failure requires an active repair attempt")
    started = datetime.fromisoformat(attempt.started_at)
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("repair attempt lacks a timezone-aware start time")
    elapsed = min(
        DEFAULT_MAX_ACTIVATION_SECONDS,
        max(0.0, (now.astimezone(UTC) - started.astimezone(UTC)).total_seconds()),
    )
    delta: dict[str, object] = dict(evidence_delta)
    if not delta:
        delta["repair_failure"] = "recorded"
    failed_attempt = RepairAttemptEvidence(
        fingerprint=attempt.fingerprint,
        snapshot=attempt.snapshot,
        origin=attempt.origin,
        diagnostic=attempt.diagnostic,
        additional_diagnostics=attempt.additional_diagnostics,
        evidence_delta=delta,
        failure_reason=failure_reason[:2_048] or "repair failed",
        started_at=attempt.started_at,
        elapsed_seconds=elapsed,
    )
    retry_count = record.automatic_repair_count
    retry_at = record.automatic_repair_at
    summary = "Reviewed repair failed."
    next_action = "Review a fresh exact-IID repair plan."
    if attempt.origin == "automatic-exact-pipeline":
        retry_count, retry_at, summary, next_action = _automatic_repair_failure(
            record, now=now, reason="the repair workflow failed"
        )
        if retry_count >= AUTOMATIC_REPAIR_MAX_ATTEMPTS:
            error_code = ErrorCode.RETRY_EXHAUSTED
        else:
            error_code = record.error_code
    else:
        error_code = record.error_code
    failed = ledger.persist(
        transition_record(
            record,
            RunnerState.REPAIR_FAILED,
            phase=WorkflowPhase.POST_PUBLICATION,
            repair_attempt=failed_attempt,
            automatic_repair_count=retry_count,
            automatic_repair_at=retry_at,
            error_code=error_code,
            summary=summary,
            next_action=next_action,
        )
    )
    gitlab.transition_labels(failed.issue_iid, label_for_state(failed.state))
    return failed


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


def _fulfill_split(
    record: LedgerRecord,
    plan: SplitPlan,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
) -> LedgerRecord:
    """Create or recover all transient plan children, then close the parent."""
    children: list[int] = []
    try:
        inventory = gitlab.split_child_inventory()
        try:
            parsed_inventory = tuple(
                (issue, parse_split_marker(issue.description, record.project_id))
                for issue in inventory
            )
        except ValueError as error:
            raise SegmentError("split child marker evidence is invalid") from error
        existing_keys = {
            key
            for _issue, marker in parsed_inventory
            if marker is not None and marker[0] == record.issue_iid
            for key in (marker[1],)
        }
        plan_keys = {slice_.key for slice_ in plan.slices}
        if not existing_keys.issubset(plan_keys):
            raise SegmentError("split child marker evidence conflicts with the plan")
        for slice_ in plan.slices:
            dependency_iids = tuple(
                children[ordinal - 1] for ordinal in slice_.depends_on
            )
            marker = render_split_marker(
                record.project_id, record.issue_iid, slice_.key, dependency_iids
            )
            matches = tuple(
                issue
                for issue, parsed in parsed_inventory
                if parsed is not None
                and parsed[0] == record.issue_iid
                and parsed[1] == slice_.key
            )
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
                    assigned_iids=set(children),
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
                    assigned_iids=set(children),
                ):
                    raise SegmentError("created split child evidence is not exact")
            children.append(child.iid)
        note = "\n".join(
            ["<!-- agent-work-decomposition:v1 -->", "Decomposed into:"]
            + [f"- #{child_iid}" for child_iid in children]
        )
        gitlab.ensure_decomposition_note(record.issue_iid, note)
        gitlab.close_issue(record.issue_iid)
        gitlab.transition_labels(record.issue_iid, None)
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
            summary="Parent decomposed into ordinary child issues.",
            next_action="Child issues are eligible through the normal queue.",
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


def new_record(
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
    """Return whether durable evidence proves an ordinary record was published.

    A PUBLISHING record without ``publication_sha`` still needs `_publish()` to
    bind and push its repaired work, even when an earlier merge request exists.
    """
    if (
        record.work_kind is not WorkKind.ORDINARY
        or record.revision == 0
        or record.phase is WorkflowPhase.POST_PUBLICATION
    ):
        return False
    if record.state in {RunnerState.PREPARING, RunnerState.EXECUTING}:
        return True
    if record.state is RunnerState.PUBLISHING:
        return record.publication_sha is not None
    if record.state is RunnerState.RETRY_WAIT:
        return record.prior_state in {RunnerState.PREPARING, RunnerState.EXECUTING} or (
            record.prior_state is RunnerState.PUBLISHING
            and record.publication_sha is not None
        )
    return (
        record.state is RunnerState.BLOCKED
        and record.error_code
        in {ErrorCode.RETRY_EXHAUSTED, ErrorCode.STATE_RECONCILIATION_FAILED}
        and (
            record.phase is not WorkflowPhase.READY_TO_PUBLISH
            or record.publication_sha is not None
        )
    )


def _is_fresh_ordinary_preparation(record: LedgerRecord) -> bool:
    """Return whether an ordinary record has not yet bound a base commit."""
    return (
        record.work_kind is WorkKind.ORDINARY
        and record.state is RunnerState.PREPARING
        and (record.revision == 0 or record.source_sha is None)
    )


def _merged_remediation_is_exact(
    record: LedgerRecord,
    *,
    gitlab: GitLabPort,
    default_branch: str,
) -> bool:
    """Treat a merged MR as provenance only after exact identity checks."""
    if record.merge_request_iid is None or record.source_sha is None:
        return False
    mr = gitlab.fetch_merge_request(record.merge_request_iid)
    return (
        mr.iid == record.merge_request_iid
        and mr.state == "merged"
        and mr.source_project_id == record.project_id
        and mr.target_project_id == record.project_id
        and mr.source_branch == record.source_branch
        and mr.target_branch == default_branch
        and gitlab.merge_request_contains_commit(mr.iid, record.source_sha)
    )


def _fresh_ordinary_from_merged(
    record: LedgerRecord, *, refreshed_base_sha: str
) -> LedgerRecord:
    """Keep the issue while dropping the merged branch's publication identity."""
    return record.model_copy(
        update={
            "state": RunnerState.PREPARING,
            "phase": WorkflowPhase.WORKFLOW,
            "work_kind": WorkKind.ORDINARY,
            "source_branch": f"agent/issue-{record.issue_iid}",
            "source_sha": None,
            "main_sha": refreshed_base_sha,
            "merge_request_iid": None,
            "merge_request_url": None,
            "error_code": None,
            "summary": "Merged review verified; prepare fresh issue work.",
            "next_action": "Prepare a new branch from current main.",
        }
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
    acknowledged = ledger.persist(
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
    try:
        _ensure_awaiting_handoff(acknowledged, gitlab=gitlab, repository=repository)
        gitlab.transition_labels(
            acknowledged.issue_iid, label_for_state(acknowledged.state)
        )
    except GitLabError:
        return _record_retry(
            acknowledged,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    except RepositoryError as error:
        return _block_publication_identity(
            acknowledged, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    return acknowledged


def _ensure_awaiting_handoff(
    record: LedgerRecord,
    *,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    merge_request: MergeRequestEvidence | None = None,
) -> bool:
    """Verify the exact publication and ensure its factory-owned handoff note."""
    if record.merge_request_iid is None or record.source_sha is None:
        raise RepositoryError(
            "Post-publication handoff lacks exact merge request evidence."
        )
    repository.verify_worktree(record.worktree, record.source_branch, record.source_sha)
    merge_request = merge_request or gitlab.fetch_merge_request(
        record.merge_request_iid
    )
    if not _mr_evidence_is_exact(
        record,
        merge_request,
        gitlab=gitlab,
        repository=repository,
        source_sha=record.source_sha,
    ):
        raise RepositoryError("Post-publication merge request evidence is not exact.")
    return gitlab.ensure_human_handoff(
        issue_iid=record.issue_iid, merge_request=merge_request
    )


def _wake_for_feedback(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
    merge_request: MergeRequestEvidence | None = None,
) -> LedgerRecord | None:
    """Atomically select unseen qualifying feedback for one exact open MR."""
    if record.merge_request_iid is None or record.source_sha is None:
        return None
    try:
        _verify_authenticated_user(gitlab)
        mr = merge_request or gitlab.fetch_merge_request(record.merge_request_iid)
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


def resume_candidates(
    records: tuple[LedgerRecord, ...], *, now: datetime
) -> tuple[LedgerRecord, ...]:
    """Return immediate owners, or one deterministically due queued retry."""
    immediate_owners = tuple(
        record for record in records if record.state in IMMEDIATE_OWNER_STATES
    )
    return immediate_owners or tuple(
        sorted(
            (
                record
                for record in records
                if record.state is RunnerState.RETRY_WAIT and _due(record, now)
            ),
            key=lambda record: (record.retry_at, record.issue_iid),
        )[:1]
    )


def _adopt_controller_descendant(
    record: LedgerRecord,
    *,
    merge_request: MergeRequestEvidence,
    records: tuple[LedgerRecord, ...],
    repository: RepositoryPort,
    default_branch: str,
) -> ControllerAdoptionDecision:
    """Compute fast-forward adoption from GitLab and repository evidence."""
    retained_sha = record.source_sha
    corroboration = {
        "same_numeric_project": (
            merge_request.source_project_id == record.project_id
            and merge_request.target_project_id == record.project_id
        ),
        "exact_merge_request_iid": (
            record.merge_request_iid is None
            or record.merge_request_iid == merge_request.iid
        ),
        "exact_merge_request_url": (
            record.merge_request_url is None
            or record.merge_request_url == merge_request.web_url
        ),
        "source_branch": record.source_branch == merge_request.source_branch,
        "target_project_and_branch": merge_request.target_branch == default_branch,
        "retained_worktree_exists": repository.worktree_exists(record.worktree),
        "retained_worktree_clean": False,
        "retained_worktree_branch": False,
        "worktree_head_matches_merge_request": False,
        "retained_sha_is_ancestor": False,
        "no_competing_identity": not any(
            item.issue_iid != record.issue_iid
            and (
                item.source_branch == record.source_branch
                or item.worktree == record.worktree
                or (
                    item.merge_request_iid is not None
                    and item.merge_request_iid == merge_request.iid
                )
                or (
                    item.merge_request_url is not None
                    and item.merge_request_url == merge_request.web_url
                )
            )
            for item in records
        ),
    }
    if corroboration["retained_worktree_exists"] and retained_sha is not None:
        try:
            corroboration["retained_worktree_clean"] = not repository.has_changes(
                record.worktree
            ) and not repository.changed_paths(record.worktree)
            repository.verify_worktree(
                record.worktree, record.source_branch, merge_request.sha
            )
            corroboration["retained_worktree_branch"] = True
            corroboration["worktree_head_matches_merge_request"] = (
                repository.head(record.worktree) == merge_request.sha
            )
            corroboration["retained_sha_is_ancestor"] = repository.is_ancestor(
                retained_sha, merge_request.sha, record.worktree
            )
        except (OSError, RepositoryError, RuntimeError, ValueError):
            pass
    return classify_controller_descendant_adoption(
        record, merge_request, corroboration=corroboration
    )


def select_record(
    ledger: LedgerPort,
    gitlab: GitLabPort,
    *,
    now: datetime,
    issue_iid: int | None = None,
    allow_pinned_conflict: bool = False,
    repository: RepositoryPort,
    deferred_observer_iids: frozenset[int] = frozenset(),
) -> tuple[LedgerRecord | None, GitLabIssue | None]:
    """Reconcile work before ranking eligible fresh issues by priority and age."""
    project = gitlab.verify_project()
    all_records = ledger.list_records()
    if any(record.project_id != project.id for record in all_records):
        raise SegmentError("ledger record belongs to another project")
    for record in all_records:
        projection = record.outcome_projection
        if projection is not None and not projection.note_complete:
            replay_outcome_projection(record, ledger=ledger, gitlab=gitlab)
            return None, None
    records = tuple(
        record
        for record in all_records
        if record.issue_iid not in deferred_observer_iids
    )
    scheduler_plan = plan_scheduler_inventory(records, now=now)
    if scheduler_plan.project_stop is not None and not (
        allow_pinned_conflict and issue_iid is not None
    ):
        raise SegmentError("multiple resumable records require reconciliation")
    immediate_owners = tuple(
        record for record in records if record.state in IMMEDIATE_OWNER_STATES
    )
    if issue_iid is not None:
        pinned = next(
            (record for record in records if record.issue_iid == issue_iid), None
        )
        if pinned is None:
            raise SegmentError("pinned issue is not the active durable record")
        if _can_reconcile_published_ordinary_work(pinned):
            return pinned, gitlab.fetch_issue(pinned.issue_iid)
        if pinned.state is RunnerState.SUGGEST_CLOSE:
            return pinned, gitlab.fetch_issue(pinned.issue_iid)
        if pinned.state not in ACTIVE_STATES:
            raise SegmentError("pinned issue is not the active durable record")
        if pinned.state is RunnerState.RETRY_WAIT and immediate_owners:
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
        if (
            record.state is RunnerState.SUGGEST_CLOSE
            and scheduler_plan.selected_issue_iid is None
        ):
            return record, issue
    if scheduler_plan.selected_issue_iid is not None:
        record = next(
            item
            for item in records
            if item.issue_iid == scheduler_plan.selected_issue_iid
        )
        if _can_reconcile_published_ordinary_work(record):
            return record, issues[record.issue_iid]
        if not _due(record, now):
            return None, None
        return record, issues[record.issue_iid]
    awaiting_merge_requests: dict[int, MergeRequestEvidence] = {}
    for waiting in sorted(
        (record for record in records if record.state is RunnerState.AWAITING_HUMAN),
        key=lambda record: record.issue_iid,
    ):
        issue = issues[waiting.issue_iid]
        try:
            _verify_authenticated_user(gitlab)
            if waiting.merge_request_iid is None or waiting.source_sha is None:
                blocked = _block_publication_identity(
                    waiting,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Post-publication handoff lacks exact merge request evidence.",
                )
                return blocked, issue.model_copy(
                    update={
                        "labels": tuple(
                            label
                            for label in issue.labels
                            if label not in LIFECYCLE_LABELS
                        )
                        + (label_for_state(blocked.state),)
                    }
                )
            merge_request = gitlab.fetch_merge_request(waiting.merge_request_iid)
            awaiting_merge_requests[waiting.issue_iid] = merge_request
            if merge_request.sha != waiting.source_sha:
                continue
            handoff_created = _ensure_awaiting_handoff(
                waiting,
                gitlab=gitlab,
                repository=repository,
                merge_request=merge_request,
            )
        except GitLabIdentityError as error:
            _block_identity_drift(waiting, ledger=ledger, summary=str(error))
            return None, None
        except IdentityVerificationTransportError:
            _record_identity_retry(waiting, ledger=ledger, gitlab=gitlab, now=now)
            return None, None
        except GitLabError:
            _record_retry(
                waiting,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GITLAB_RETRYABLE,
            )
            return None, None
        except RepositoryError as error:
            blocked = _block_publication_identity(
                waiting, ledger=ledger, gitlab=gitlab, summary=str(error)
            )
            return blocked, issue.model_copy(
                update={
                    "labels": tuple(
                        label for label in issue.labels if label not in LIFECYCLE_LABELS
                    )
                    + (label_for_state(blocked.state),)
                }
            )
        expected = label_for_state(waiting.state)
        label_repaired = set(issue.labels).intersection(LIFECYCLE_LABELS) != {expected}
        if label_repaired:
            gitlab.transition_labels(waiting.issue_iid, expected)
        if handoff_created or label_repaired:
            return None, None
    for waiting in sorted(
        (record for record in records if record.state is RunnerState.AWAITING_HUMAN),
        key=lambda record: record.issue_iid,
    ):
        woken = _wake_for_feedback(
            waiting,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
            merge_request=awaiting_merge_requests.get(waiting.issue_iid),
        )
        if woken is not None:
            if woken.state is RunnerState.RETRY_WAIT:
                return None, None
            return woken, issues[woken.issue_iid]
    for waiting in sorted(
        (record for record in records if record.state is RunnerState.AWAITING_HUMAN),
        key=lambda record: record.issue_iid,
    ):
        merge_request = awaiting_merge_requests.get(waiting.issue_iid)
        if merge_request is None or merge_request.sha == waiting.source_sha:
            continue
        _block_publication_identity(
            waiting,
            ledger=ledger,
            gitlab=gitlab,
            summary="Post-publication merge request evidence is not exact.",
        )
    for reconciliable in (
        record
        for record in records
        if _can_reconcile_published_ordinary_work(record)
        and not (record.state is RunnerState.RETRY_WAIT and not _due(record, now))
    ):
        try:
            if gitlab.open_merge_requests_by_branch(reconciliable.source_branch):
                return reconciliable, issues[reconciliable.issue_iid]
        except (GitLabError, TypeError):
            return reconciliable, issues[reconciliable.issue_iid]
    for retained in records:
        if not (
            retained.state is RunnerState.BLOCKED
            and retained.error_code is ErrorCode.REMEDIATION_MR_MISMATCH
            and retained.work_kind is WorkKind.EXISTING_MR_REMEDIATION
            and retained.phase is WorkflowPhase.WORKFLOW
            and retained.source_sha is not None
            and retained.main_sha is None
            and retained.publication_sha is None
            and retained.intent is None
            and not retained.receipts
            and retained.audit_evidence in {None, "unknown"}
            and retained.merge_request_draft is None
            and retained.pending_feedback is None
            and retained.repair_attempt is None
            and not repository.worktree_exists(retained.worktree)
        ):
            continue
        issue = issues[retained.issue_iid]
        if issue.state != "opened":
            continue
        try:
            classification = classify_work(issue, project_id=project.id)
            if (
                classification.kind is not WorkKind.EXISTING_MR_REMEDIATION
                or classification.merge_request_iid != retained.merge_request_iid
                or classification.source_branch != retained.source_branch
                or classification.source_sha != retained.source_sha
                or not _merged_remediation_is_exact(
                    retained,
                    gitlab=gitlab,
                    default_branch=repository.config.default_branch,
                )
            ):
                continue
            refreshed_base_sha = repository.refresh_default_branch()
            if repository.worktree_exists(retained.worktree):
                continue
            if not repository.fresh_issue_branch_available(retained.issue_iid):
                continue
        except (ClassificationError, GitLabError, RepositoryError):
            continue
        requeued = ledger.persist(
            _fresh_ordinary_from_merged(
                retained,
                refreshed_base_sha=refreshed_base_sha,
            )
        )
        return requeued, issue
    lifecycle = set(LIFECYCLE_LABELS)
    for record in records:
        if (record.state is RunnerState.RETRY_WAIT and not _due(record, now)) or (
            record.state is RunnerState.BLOCKED
            and record.error_code
            in {
                ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
                ErrorCode.REMEDIATION_MR_MISMATCH,
            }
            and record.work_kind
            in {WorkKind.EXISTING_MR_REMEDIATION, WorkKind.MERGE_CONFLICT_REPAIR}
        ):
            continue
        issue = issues[record.issue_iid]
        expected = label_for_state(record.state)
        if set(issue.labels).intersection(lifecycle) != (
            {expected} if expected else set()
        ):
            return record, issue
    record_iids = {record.issue_iid for record in all_records}
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
    closing_issue_owners = _open_closing_issue_owners(
        gitlab,
        project_id=project.id,
        default_branch=repository.config.default_branch,
        issue_iids=tuple(issue.iid for issue in eligible),
    )
    eligible = tuple(
        issue for issue in eligible if issue.iid not in closing_issue_owners
    )
    if not eligible:
        return None, None
    issue = eligible[0]
    return (
        new_record(
            issue,
            project_id=project.id,
            state_root=ledger.state_root,
            main_branch=repository.config.default_branch,
        ),
        issue,
    )


class IdentityVerificationTransportError(GitLabError):
    """Mark an ordinary transport failure that leaves identity unverified."""


def _open_closing_issue_owners(
    gitlab: GitLabPort,
    *,
    project_id: int,
    default_branch: str,
    issue_iids: tuple[int, ...],
) -> dict[int, int]:
    """Map eligible issues to exact open closing merge requests."""
    closing_issue_owners: dict[int, int] = {}
    eligible_iids = frozenset(issue_iids)
    for merge_request in gitlab.open_merge_requests():
        if not (
            merge_request.state == "opened"
            and merge_request.target_project_id == project_id
            and merge_request.target_branch == default_branch
        ):
            continue
        for issue_iid in eligible_iids.intersection(
            gitlab.closing_issue_iids(merge_request.iid)
        ):
            closing_issue_owners[issue_iid] = merge_request.iid
    return closing_issue_owners


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
    ledger: LedgerPort,
    gitlab: GitLabPort,
    *,
    project_id: int,
    repository: RepositoryPort | None = None,
    now: datetime | None = None,
) -> None:
    """Persist closed records before clearing their lifecycle-label projections."""
    inventory = gitlab.list_closed_projected()
    for issue in sorted(inventory, key=lambda item: item.iid):
        record = ledger.load(issue.iid)
        if record is not None:
            if record.project_id != project_id:
                raise ValueError("ledger record project does not match")
            if repository is not None and now is not None:
                published = _reconcile_closed_published_issue(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    repository=repository,
                    now=now,
                )
                if published is not None:
                    continue
            completed = _completed_closed_record(record)
            if completed != record:
                ledger.persist(completed)
        _verify_authenticated_user(gitlab)
        gitlab.transition_labels(issue.iid, None)


def _record_timing_deferred(
    record: LedgerRecord, *, ledger: LedgerPort, now: datetime
) -> LedgerRecord:
    """Requeue a busy timing window without charging the failure retry budget."""
    condition = ConditionEvidence(
        occurred_at=now,
        retry_count=record.retry_count,
        code=ErrorCode.TIMING_ADMISSION_BUSY.value,
        detail="Project timing admission was busy; validation was deferred.",
    )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.RETRY_WAIT,
            prior_state=record.state,
            prior_phase=record.phase,
            retry_at=now + timedelta(seconds=30),
            error_code=ErrorCode.TIMING_ADMISSION_BUSY,
            summary="Project timing admission was busy; validation was deferred.",
            next_action="Repeat exact-tree validation after the timing window clears.",
            condition_history=(*record.condition_history, condition)[-64:],
        )
    )


def _record_capacity_deferred(
    record: LedgerRecord, *, ledger: LedgerPort, now: datetime
) -> LedgerRecord:
    """Requeue a clean capacity refusal without charging the failure budget."""
    detail = "Live-agent capacity was unavailable before ordinary work began."
    condition = ConditionEvidence(
        occurred_at=now,
        retry_count=record.retry_count,
        code=ErrorCode.AGENT_CAPACITY_UNAVAILABLE.value,
        detail=detail,
    )
    return ledger.persist(
        transition_record(
            record,
            RunnerState.RETRY_WAIT,
            prior_state=record.state,
            prior_phase=record.phase,
            retry_at=now + timedelta(minutes=20),
            error_code=ErrorCode.AGENT_CAPACITY_UNAVAILABLE,
            summary=detail,
            next_action="Retry the workflow when live-agent capacity is available.",
            condition_history=(*record.condition_history, condition)[-64:],
        )
    )


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
    stop_on_unchanged_condition: bool = False,
) -> LedgerRecord:
    """Persist one bounded retry and optionally publish its exhaustion block."""
    condition = ConditionEvidence(
        occurred_at=now,
        retry_count=record.retry_count,
        code=error_code.value,
        detail=detail or "Transient failure; retry scheduled.",
    )
    if stop_on_unchanged_condition and _is_unchanged_retry_condition(record, condition):
        outcome = transition_record(
            record,
            RunnerState.BLOCKED,
            retry_at=None,
            error_code=error_code,
            summary="Automatic retry stopped because validation evidence did not change.",
            next_action=(
                "Resolve the blocker, then preview agent-work loop retry --issue-iid "
                f"{record.issue_iid} and apply its exact --plan-file."
            ),
        )
    else:
        outcome = plan_retry(record, now=now, retry_delay=retry_delay).record
        condition = condition.model_copy(
            update={
                "retry_count": outcome.retry_count,
                "detail": detail or outcome.summary,
            }
        )
    history = (
        *record.condition_history,
        condition,
    )[-64:]
    if outcome.state is RunnerState.RETRY_WAIT:
        outcome = outcome.model_copy(
            update={"error_code": error_code, "condition_history": history}
        )
    else:
        outcome = outcome.model_copy(
            update={
                "condition_history": history,
                "summary": f"{outcome.summary} {condition.code}: {condition.detail}",
            }
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


def _is_unchanged_retry_condition(
    record: LedgerRecord, condition: ConditionEvidence
) -> bool:
    """Return whether a retry repeats the latest uninterrupted condition."""
    if not record.condition_history:
        return False
    latest = record.condition_history[-1]
    return (
        record.prior_phase is record.phase
        and latest.retry_count == record.retry_count
        and latest.code == condition.code
        and latest.detail == condition.detail
    )


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


def _unsafe_due_protocol_retry_reason(
    record: LedgerRecord,
    *,
    repository: RepositoryPort,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None] | None,
) -> str | None:
    """Require a dirty retry candidate to remain the retained Red snapshot."""
    if record.source_sha is None:
        return "the retained worktree baseline is unavailable"
    try:
        if repository.head(record.worktree) != record.source_sha:
            return "the retained worktree head changed"
        status = repository.observe_worktree_status(record.worktree)
    except RepositoryError:
        return "the retained worktree could not be verified"
    if not status.dirty:
        return None
    checkpoint = _retained_red_checkpoint(record, load_red_checkpoint)
    if checkpoint is None:
        return "the retained Red checkpoint is unavailable"
    paths = tuple(sorted(set(status.changed_paths)))
    if paths != checkpoint.test_paths:
        return "the retained dirty paths differ from the Red checkpoint"
    try:
        if (
            repository.red_candidate_tree(record.worktree, checkpoint.base_sha, paths)
            != checkpoint.tree_oid
        ):
            return "the retained dirty tree differs from the Red checkpoint"
    except RepositoryError:
        return "the retained dirty tree could not be verified"
    return None


def _block_unsafe_due_protocol_retry(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    detail: str,
) -> LedgerRecord:
    """Keep an unverified dirty candidate available for explicit recovery."""
    blocked = ledger.persist(
        transition_record(
            record,
            RunnerState.BLOCKED,
            error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
            summary=f"Automatic workflow retry stopped: {detail}.",
            next_action="Review the retained candidate and Red checkpoint before explicit recovery.",
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


def _mr_publication_identity_is_exact(
    record: LedgerRecord,
    mr: MergeRequestEvidence,
    *,
    repository: RepositoryPort,
    source_sha: str,
    merged: bool = False,
) -> bool:
    """Verify required merge-request publication identity and state."""
    return (
        (record.merge_request_iid is None or mr.iid == record.merge_request_iid)
        and mr.state == ("merged" if merged else "opened")
        and mr.source_project_id == record.project_id
        and mr.target_project_id == record.project_id
        and mr.source_branch == record.source_branch
        and mr.target_branch == repository.config.default_branch
        and mr.sha == source_sha
    )


def _is_published_completion_candidate(record: LedgerRecord) -> bool:
    """Return whether closed-issue completion requires exact publication proof."""
    return (
        record.state is not RunnerState.COMPLETE
        and record.phase
        in {WorkflowPhase.PIPELINE_WAIT, WorkflowPhase.POST_PUBLICATION}
        and record.merge_request_iid is not None
        and record.source_sha is not None
        and record.publication_sha == record.source_sha
    )


def _preserve_closed_published_wrapper(
    record: LedgerRecord, *, ledger: LedgerPort, now: datetime
) -> LedgerRecord:
    """Keep a closed completion wrapper fail-closed without a due retry spin."""
    if record.state is RunnerState.RETRY_WAIT and _due(record, now):
        return ledger.persist(
            record.model_copy(update={"retry_at": now + timedelta(minutes=20)})
        )
    return record


def _reconcile_closed_published_issue(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord | None:
    """Complete a closed published issue only from fresh exact success evidence."""
    if not _is_published_completion_candidate(record):
        return None
    merge_request_iid = record.merge_request_iid
    source_sha = record.source_sha
    assert merge_request_iid is not None
    assert source_sha is not None
    try:
        _verify_authenticated_user(gitlab)
        mr = gitlab.fetch_merge_request(merge_request_iid)
        exact = _mr_publication_identity_is_exact(
            record,
            mr,
            repository=repository,
            source_sha=source_sha,
            merged=True,
        )
        closes_issue = exact and gitlab.mr_closes_issue(mr.iid, record.issue_iid)
        pipeline = (
            gitlab.latest_pipeline_for_sha(mr.iid, source_sha) if closes_issue else None
        )
    except GitLabIdentityError as error:
        if record.state in {RunnerState.BLOCKED, RunnerState.RETRY_WAIT}:
            return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
        return _block_identity_drift(record, ledger=ledger, summary=str(error))
    except IdentityVerificationTransportError:
        if record.state in {RunnerState.BLOCKED, RunnerState.RETRY_WAIT}:
            return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
        return _record_identity_retry(record, ledger=ledger, gitlab=gitlab, now=now)
    except GitLabError:
        if record.state in {RunnerState.BLOCKED, RunnerState.RETRY_WAIT}:
            return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    if not exact or not closes_issue:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Closed issue lacks exact published merge request evidence.",
        )
    if pipeline is None or pipeline.status != "success" or pipeline.sha != source_sha:
        return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
    try:
        jobs_succeeded = _required_pipeline_jobs_succeeded(
            gitlab.list_pipeline_jobs(pipeline.id, source_sha),
            pipeline_id=pipeline.id,
            source_sha=source_sha,
        )
    except (GitLabError, SegmentError):
        return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
    if jobs_succeeded is not True:
        return _preserve_closed_published_wrapper(record, ledger=ledger, now=now)
    complete = ledger.persist(_completed_closed_record(record))
    gitlab.transition_labels(record.issue_iid, None)
    return complete


def _source_has_retained_green_proof(
    record: LedgerRecord, repository: RepositoryPort
) -> bool:
    """Recognize an issue-bound Green source even when intent omits its refs."""
    source_sha = record.source_sha
    if (
        source_sha is None
        or not hasattr(repository, "green_commit_dependencies")
        or not hasattr(repository, "red_commit_dependencies")
    ):
        return False
    commit_repository = cast(_CommitRepository, repository)
    green = commit_repository.green_commit_dependencies(record.worktree, Path())
    red = commit_repository.red_commit_dependencies(record.worktree, Path())
    try:
        red_sha, green_tree, _message = green.commit_identity(source_sha)
        base_sha, _red_tree = red.commit_identity(red_sha)
    except RepositoryError:
        return False
    if not all(
        re.fullmatch(r"[0-9a-f]{40}", oid)
        for oid in (source_sha, red_sha, green_tree, base_sha)
    ):
        return False
    return (
        red.read_ref(red_commit_ref(record.issue_iid, base_sha)) == red_sha
        and green.read_ref(green_commit_ref(record.issue_iid, base_sha, green_tree))
        == source_sha
    )


def _publication_intent_is_exact(
    record: LedgerRecord, *, baseline_sha: str, repository: RepositoryPort
) -> bool:
    """Return whether durable intent authorizes this publication replay."""
    intent = record.intent
    if intent is None:
        return False
    if (
        record.publication_sha is not None
        and record.publication_sha != record.source_sha
        and record.main_sha is not None
        and record.main_sha != record.source_sha
        and (
            record.workflow_lane is not WorkflowLane.CODING
            or record.work_kind is not WorkKind.ORDINARY
            or record.pending_feedback is not None
        )
        and _source_has_retained_green_proof(record, repository)
    ):
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
    expected_evidence: dict[str, str | int | None] = {
        "branch": record.source_branch,
        "baseline_sha": baseline_sha,
    }
    if record.publication_sha is not None:
        expected_evidence["publication_sha"] = record.publication_sha
    if "candidate_tree" in intent.expected_evidence:
        reconciliation = {
            key: value
            for key, value in intent.expected_evidence.items()
            if key
            in {
                "branch",
                "source_sha",
                "target_sha",
                "candidate_tree",
                "red_ref",
                "green_ref",
            }
        }
        if record.publication_sha is None or not _reconcile_evidence_is_exact(
            record, reconciliation
        ):
            return False
        expected_evidence |= {
            key: value for key, value in reconciliation.items() if key != "branch"
        }
    if record.pending_feedback is not None:
        validated_paths = _feedback_validated_paths(record)
        if validated_paths is None:
            return False
        expected_evidence |= _validated_path_evidence(validated_paths)
    return (
        intent.kind == "publish_merge_request"
        and issue_iid == str(record.issue_iid)
        and issue_separator == ":"
        and action == "publish"
        and action_separator == ":"
        and revision_is_canonical
        and (
            revision_is_older
            or record.workflow_lane is WorkflowLane.CODING
            and record.work_kind is WorkKind.ORDINARY
            and record.publication_sha is None
            and revision == record_revision
        )
        and intent.expected_evidence == expected_evidence
    )


def _reconcile_intent_is_exact(record: LedgerRecord) -> bool:
    """Require one canonical ordered-merge intent for the retained source."""
    intent = record.intent
    source_sha = record.source_sha
    target_sha = record.main_sha
    if (
        intent is None
        or intent.kind != "reconcile_commit"
        or record.workflow_lane is not WorkflowLane.CODING
        or record.work_kind is not WorkKind.ORDINARY
        or record.pending_feedback is not None
        or source_sha is None
        or target_sha is None
        or record.publication_sha is not None
    ):
        return False
    match = re.fullmatch(
        rf"{record.issue_iid}:reconcile:(0|[1-9][0-9]*)", intent.operation_id
    )
    if match is None or int(match.group(1)) > record.revision:
        return False
    return _reconcile_evidence_is_exact(record, intent.expected_evidence)


def _reconcile_evidence_is_exact(
    record: LedgerRecord, evidence: Mapping[str, str | int | None]
) -> bool:
    """Validate durable source, target, candidate, and retained refs."""
    source_sha = record.source_sha
    target_sha = record.main_sha
    if source_sha is None or target_sha is None:
        return False
    red_ref = evidence.get("red_ref")
    green_ref = evidence.get("green_ref")
    candidate_tree = evidence.get("candidate_tree")
    if not all(isinstance(item, str) for item in (red_ref, green_ref, candidate_tree)):
        return False
    red_ref = cast(str, red_ref)
    green_ref = cast(str, green_ref)
    candidate_tree = cast(str, candidate_tree)
    red_match = re.fullmatch(
        rf"refs/agent-work/red-commits/{record.issue_iid}/([0-9a-f]{{40}})",
        red_ref,
    )
    return (
        red_match is not None
        and re.fullmatch(r"[0-9a-f]{40}", candidate_tree) is not None
        and re.fullmatch(
            rf"refs/agent-work/green-commits/{record.issue_iid}/"
            rf"{red_match.group(1) if red_match else ''}/[0-9a-f]{{40}}",
            green_ref,
        )
        is not None
        and evidence
        == {
            "branch": record.source_branch,
            "source_sha": source_sha,
            "target_sha": target_sha,
            "candidate_tree": candidate_tree,
            "red_ref": red_ref,
            "green_ref": green_ref,
        }
    )


def _feedback_validated_paths(record: LedgerRecord) -> tuple[str, ...] | None:
    """Return the canonical controller-validated feedback commit set."""
    intent = record.intent
    if intent is None:
        return None
    try:
        raw_paths = intent.expected_evidence.get("validated_paths")
        decoded = json.loads(raw_paths) if isinstance(raw_paths, str) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(decoded, list)
        or not decoded
        or not all(isinstance(path, str) and path for path in decoded)
    ):
        return None
    paths = tuple(decoded)
    if paths != tuple(sorted(set(paths))):
        return None
    expected = _validated_path_evidence(paths)
    if not all(
        intent.expected_evidence.get(key) == value for key, value in expected.items()
    ):
        return None
    return paths


def _validated_path_evidence(paths: tuple[str, ...]) -> dict[str, str | int | None]:
    """Bind a non-secret stable fingerprint of the exact commit path set."""
    return {
        "validated_path_count": len(paths),
        "validated_paths_fingerprint": hashlib.sha256(
            "".join(f"{path}\0" for path in paths).encode()
        ).hexdigest(),
        "validated_paths": json.dumps(paths, separators=(",", ":")),
    }


def _block_feedback_path_mismatch(
    record: LedgerRecord, *, ledger: LedgerPort, gitlab: GitLabPort
) -> LedgerRecord:
    """Fail closed without exposing the paths rejected at the commit boundary."""
    blocked = ledger.persist(
        transition_record(
            record,
            RunnerState.BLOCKED,
            error_code=ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
            summary="Feedback publication paths no longer match controller evidence.",
            next_action="Re-run the feedback workflow against the exact changed paths.",
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


def _block_red_checkpoint(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    now: datetime,
    stage: RedCheckpointFailureStage,
) -> LedgerRecord:
    """Fail closed when trusted Red evidence cannot be established exactly."""
    detail = stage.value
    condition = ConditionEvidence(
        occurred_at=now,
        retry_count=record.retry_count,
        code=ErrorCode.RED_CHECKPOINT_FAILED.value,
        detail=detail,
    )
    blocked = ledger.persist(
        transition_record(
            record,
            RunnerState.BLOCKED,
            error_code=ErrorCode.RED_CHECKPOINT_FAILED,
            summary=(
                "The trusted Red checkpoint could not be established. "
                f"{ErrorCode.RED_CHECKPOINT_FAILED.value}: {detail}"
            ),
            next_action=(
                "Review the retained tests and checkpoint evidence before an "
                "explicit retry."
            ),
        ).model_copy(
            update={"condition_history": (*record.condition_history, condition)[-64:]}
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
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    summary: str,
    error_code: ErrorCode = ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
    next_action: str = "Review the exact MR, branch, target, SHA, and closing evidence.",
) -> LedgerRecord:
    """Persist and project one actionable publication-identity block."""
    updates = {
        "error_code": error_code,
        "summary": summary,
        "next_action": next_action,
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
        if not _mr_publication_identity_is_exact(
            record, mr, repository=repository, source_sha=mr.sha
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
        if not _mr_publication_identity_is_exact(
            record,
            mr,
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
    newly_claimed = record.revision == 0 or (
        record.work_kind is WorkKind.ORDINARY
        and record.state is RunnerState.PREPARING
        and record.source_sha is None
    )
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
            if mr.state == "merged":
                try:
                    marker = classify_work(issue, project_id=record.project_id)
                except ClassificationError as error:
                    raise SegmentError(
                        "merged remediation marker is invalid"
                    ) from error
                if (
                    marker.kind is not record.work_kind
                    or marker.merge_request_iid != record.merge_request_iid
                    or marker.source_branch != record.source_branch
                    or marker.source_sha != record.source_sha
                ):
                    raise SegmentError("merged remediation marker has changed")
                if not _merged_remediation_is_exact(
                    record,
                    gitlab=gitlab,
                    default_branch=repository.config.default_branch,
                ):
                    raise SegmentError("merged remediation origin is not exact")
                if repository.worktree_exists(record.worktree):
                    raise SegmentError("fresh issue worktree already exists")
                refreshed_base_sha = repository.refresh_default_branch()
                if not repository.fresh_issue_branch_available(record.issue_iid):
                    raise SegmentError("fresh issue branch already exists")
                record = ledger.persist(
                    _fresh_ordinary_from_merged(
                        record,
                        refreshed_base_sha=refreshed_base_sha,
                    )
                )
                if repository.default_branch_head() != refreshed_base_sha:
                    raise RepositoryError("default branch changed before preparation")
                worktree = repository.create_issue_worktree(
                    issue.iid,
                    record.source_branch,
                    repository.config.default_branch,
                    allow_existing_branch=False,
                )
                source_sha = repository.head(worktree)
                if source_sha != refreshed_base_sha:
                    raise RepositoryError(
                        "fresh issue worktree does not match refreshed default branch"
                    )
                return ledger.persist(
                    transition_record(
                        record,
                        RunnerState.EXECUTING,
                        worktree=worktree,
                        source_sha=source_sha,
                        main_sha=source_sha,
                        intent=None,
                        summary="Fresh issue worktree prepared from current main.",
                        next_action="Run the recorded workflow phase.",
                    )
                )
            if (
                mr.state != "opened"
                or mr.source_project_id != record.project_id
                or mr.target_project_id != record.project_id
                or mr.source_branch != record.source_branch
                or mr.target_branch != repository.config.default_branch
            ):
                raise SegmentError("remediation merge request evidence does not match")
            if mr.sha != record.source_sha:
                adoption = _adopt_controller_descendant(
                    record,
                    merge_request=mr,
                    records=ledger.list_records(),
                    repository=repository,
                    default_branch=repository.config.default_branch,
                )
                if adoption.action is not AdoptionAction.ADOPT:
                    raise SegmentError("controller fast-forward evidence is not exact")
                record = ledger.persist(
                    record.model_copy(
                        update={
                            "source_sha": mr.sha,
                            "merge_request_iid": mr.iid,
                            "merge_request_url": mr.web_url,
                            "retry_at": None,
                            "error_code": None,
                            "summary": "Controller fast-forward publication was corroborated.",
                            "next_action": "Prepare the exact retained remediation worktree.",
                        }
                    )
                )
            worktree = repository.worktree_for_existing_branch(
                issue.iid, record.source_branch, mr.sha
            )
        else:
            if newly_claimed and record.main_sha is None:
                repository.refresh_base()
            if (
                newly_claimed
                and record.main_sha is not None
                and repository.default_branch_head() != record.main_sha
            ):
                raise RepositoryError(
                    "recorded default branch changed before preparation"
                )
            worktree = repository.create_issue_worktree(
                issue.iid,
                record.source_branch,
                repository.config.default_branch,
                allow_existing_branch=not newly_claimed,
            )
            if newly_claimed:
                source_sha = repository.head(worktree)
                if record.main_sha is not None and source_sha != record.main_sha:
                    raise RepositoryError(
                        "fresh issue worktree does not match recorded default branch"
                    )
            else:
                expected_head = record.source_sha
                if expected_head is None or not retained_ordinary_is_untouched(record):
                    raise RepositoryError(
                        "retained ordinary work has authored or workflow evidence"
                    )
                source_sha = repository.refresh_retained_worktree(
                    worktree, record.source_branch, expected_head
                )
        if record.work_kind in {
            WorkKind.EXISTING_MR_REMEDIATION,
            WorkKind.MERGE_CONFLICT_REPAIR,
        }:
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
    if record.work_kind is WorkKind.ORDINARY:
        # This is the first post-refresh worktree observation; do not use a
        # potentially stale base-checkout or retained ledger SHA.
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
    completion_clock: Callable[[], datetime],
    remaining_seconds: Callable[[], float] | None = None,
    issue_lock: Callable[[int], AbstractContextManager[None]] | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
    apply_red_checkpoint: (
        Callable[[int, RedCheckpointRequest, str], RedCheckpointEvidence] | None
    ) = None,
    observe_interactive_validation: Callable[[], object] = lambda: None,
    on_worker_invoke: Callable[[int], None] = lambda _issue_iid: None,
) -> LedgerRecord | None:
    if not invoke.supports_worker_result_broker:
        raise SegmentError("worker invoker does not support the live result broker")
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
    attempt = record.repair_attempt
    if attempt is not None and attempt.origin == "automatic-exact-pipeline":
        diagnostic = attempt.diagnostic
        diagnostics = (
            () if diagnostic is None else (diagnostic,)
        ) + attempt.additional_diagnostics
        snapshot = attempt.snapshot
        snapshot_fingerprint = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        diagnostic_fingerprints = tuple(
            hashlib.sha256(
                json.dumps(
                    item.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            for item in diagnostics
        )
        legacy_single_snapshot = (
            not attempt.additional_diagnostics
            and "failed_job_ids" not in snapshot
            and "diagnostic_fingerprints" not in snapshot
        )
        if (
            diagnostic is None
            or not 1 <= len(diagnostics) <= 8
            or tuple(item.job_id for item in diagnostics)
            != tuple(sorted({item.job_id for item in diagnostics}))
            or any(
                item.project_id != record.project_id
                or item.merge_request_iid != record.merge_request_iid
                or item.source_sha != record.source_sha
                or item.pipeline_sha != record.source_sha
                or item.pipeline_id != diagnostic.pipeline_id
                or item.pipeline_status != "failed"
                or item.job_status != "failed"
                or item.retried
                for item in diagnostics
            )
            or diagnostic.project_id != record.project_id
            or diagnostic.merge_request_iid != record.merge_request_iid
            or diagnostic.source_sha != record.source_sha
            or diagnostic.pipeline_sha != record.source_sha
            or diagnostic.pipeline_status != "failed"
            or diagnostic.job_status != "failed"
            or diagnostic.retried
            or attempt.fingerprint != snapshot_fingerprint
            or snapshot.get("project_id") != diagnostic.project_id
            or snapshot.get("merge_request_iid") != diagnostic.merge_request_iid
            or snapshot.get("source_sha") != diagnostic.source_sha
            or snapshot.get("pipeline_id") != diagnostic.pipeline_id
            or snapshot.get("pipeline_sha") != diagnostic.pipeline_sha
            or snapshot.get("pipeline_status") != diagnostic.pipeline_status
            or snapshot.get("failed_job_id") != diagnostic.job_id
            or snapshot.get("diagnostic_fingerprint") != diagnostic_fingerprints[0]
            or (
                not legacy_single_snapshot
                and snapshot.get("failed_job_ids")
                != [item.job_id for item in diagnostics]
            )
            or (
                not legacy_single_snapshot
                and snapshot.get("diagnostic_fingerprints")
                != list(diagnostic_fingerprints)
            )
        ):
            return record_failed_job_diagnostic_unavailable(
                record, ledger=ledger, gitlab=gitlab
            )
    worker_timeout = timeout_seconds
    if remaining_seconds is not None:
        remaining = remaining_seconds()
        if remaining <= 0:
            return None
        worker_timeout = max(1, math.ceil(remaining))
    try:
        if (
            record.error_code is ErrorCode.PUBLICATION_COMMIT_REJECTED
            and record.workflow_lane is WorkflowLane.UNKNOWN
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Publication correction workflow lane is unknown.",
            )
        retained_checkpoint = (
            _retained_red_checkpoint(record, load_red_checkpoint)
            if record.phase is WorkflowPhase.WORKFLOW
            else None
        )
        if (
            record.error_code is ErrorCode.PUBLICATION_COMMIT_REJECTED
            and record.workflow_lane is WorkflowLane.CODING
            and (
                retained_checkpoint is None
                or retained_checkpoint.project_id != record.project_id
                or retained_checkpoint.issue_iid != record.issue_iid
                or retained_checkpoint.source_branch != record.source_branch
                or retained_checkpoint.base_sha
                != (
                    record.correction_identity.baseline_sha
                    if record.correction_identity is not None
                    and record.correction_identity.mode == "authored_candidate_recovery"
                    else record.source_sha
                )
                or not retained_checkpoint.test_paths
            )
        ):
            return _block_red_checkpoint(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                stage=RedCheckpointFailureStage.RETAINED_PRECHECK,
            )
        if record.correction_identity is not None:
            try:
                exact_correction = correction_identity_matches(
                    record,
                    repository.observe_legacy_publication_candidate(record.worktree),
                    checkpoint=retained_checkpoint,
                    original_base_sha=(
                        repository.single_parent(
                            record.worktree, record.source_sha or ""
                        )
                        if record.correction_identity.mode
                        == "authored_candidate_recovery"
                        else None
                    ),
                )
            except RepositoryError:
                exact_correction = False
            if not exact_correction:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Publication correction identity changed before worker invocation.",
                )
        prompt_record = record.model_dump(mode="json")
        if retained_checkpoint is not None:
            prompt_record["red_checkpoint"] = retained_checkpoint.model_dump(
                mode="json"
            )
        source_sha = record.source_sha or repository.head(record.worktree)
        nonce = secrets.token_hex(16)
        broker_request = WorkerResultBrokerRequest(
            nonce=nonce,
            project_id=record.project_id,
            issue_iid=record.issue_iid,
            ledger_revision=record.revision,
            expected_state=record.state,
            expected_phase=record.phase,
            source_branch=record.source_branch,
            source_sha=source_sha,
        )
        prompt = build_worker_prompt(
            prompt_record,
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
            worker_result_broker=broker_request,
            worker_result_builder=(
                repository.config.checkout / "bin" / "agent-work-from-checkout"
            ),
            retained_red_checkpoint=retained_checkpoint,
        )
    except RedCheckpointError:
        return _block_red_checkpoint(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            stage=RedCheckpointFailureStage.CONTEXT_PREPARATION,
        )
    except ValueError as error:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary=f"Feedback selection cannot be rendered safely: {error}",
        )
    validated_candidate_paths: tuple[str, ...] | None = None
    successfully_validated_tree: str | None = None
    pending_deferred_tree: str | None = None
    prepared_candidate: str | None = None
    committed_candidate: GreenCommitProof | None = None
    uses_authored_base = (
        record.correction_identity is not None
        and record.correction_identity.mode == "authored_candidate_recovery"
    )
    candidate_base_sha = (
        record.correction_identity.baseline_sha
        if uses_authored_base and record.correction_identity is not None
        else record.source_sha
    )
    if candidate_base_sha is None:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Candidate validation base is unavailable.",
        )
    pending_coding_merge = (
        retained_checkpoint is not None
        and record.workflow_lane is WorkflowLane.CODING
        and record.work_kind is WorkKind.ORDINARY
        and record.main_sha is not None
        and retained_checkpoint.base_sha != record.source_sha
        and hasattr(repository, "green_commit_dependencies")
    )
    candidate_head = record.source_sha if uses_authored_base else candidate_base_sha
    if (
        retained_checkpoint is not None
        and record.workflow_lane is WorkflowLane.CODING
        and record.work_kind is WorkKind.ORDINARY
        and hasattr(repository, "green_commit_dependencies")
        and repository.head(record.worktree) != candidate_base_sha
    ):
        observed_head = repository.head(record.worktree)
        try:
            dependencies = cast(
                _CommitRepository, repository
            ).green_commit_dependencies(record.worktree, Path())
            red_sha, green_tree, _ = dependencies.commit_identity(observed_head)
            expected_red_ref = (
                f"refs/agent-work/red-commits/{record.issue_iid}/{candidate_base_sha}"
            )
            if (
                dependencies.read_ref(expected_red_ref) == red_sha
                and dependencies.read_ref(
                    green_commit_ref(record.issue_iid, candidate_base_sha, green_tree)
                )
                == observed_head
            ):
                repository.verify_worktree(
                    record.worktree, record.source_branch, observed_head
                )
                candidate_head = observed_head
        except (RepositoryError, ValueError):
            pass

    def observed_candidate_paths() -> tuple[str, ...]:
        """Return the candidate scope for its exact validation base."""
        paths = (
            repository.publication_changed_paths(record.worktree, candidate_base_sha)
            if uses_authored_base or candidate_head != candidate_base_sha
            else repository.changed_paths(record.worktree)
        )
        return tuple(sorted(set(paths)))

    def committed_identity_is_exact() -> bool:
        if committed_candidate is None:
            return False
        proof = committed_candidate
        dependencies = cast(_CommitRepository, repository).green_commit_dependencies(
            record.worktree, Path()
        )
        return (
            dependencies.read_ref(proof.ref) == proof.commit_sha
            and dependencies.read_ref(
                red_commit_ref(record.issue_iid, candidate_base_sha)
            )
            == proof.red_sha
            and dependencies.commit_identity(proof.commit_sha)[:2]
            == (proof.red_sha, proof.tree_oid)
            and dependencies.worker_state(record.worktree)
            == (record.source_branch, proof.commit_sha, proof.tree_oid, proof.tree_oid)
        )

    def pending_merge_identity_is_exact() -> bool:
        """Re-prove the retained Red and Green source plus exact pending target."""
        if not pending_coding_merge or retained_checkpoint is None:
            return False
        source_sha = record.source_sha
        target_sha = record.main_sha
        if source_sha is None or target_sha is None:
            return False
        observation = repository.observe_legacy_publication_candidate(record.worktree)
        if (
            observation.branch != record.source_branch
            or observation.head != source_sha
            or observation.merge_head != target_sha
            or observation.has_unmerged
            or repository.observe_current_default(
                record.worktree, record.source_branch, source_sha
            )
            != target_sha
        ):
            return False
        dependencies = cast(_CommitRepository, repository).green_commit_dependencies(
            record.worktree, Path()
        )
        red_dependencies = cast(_CommitRepository, repository).red_commit_dependencies(
            record.worktree, Path()
        )
        red_sha, green_tree, _ = dependencies.commit_identity(source_sha)
        red_ref = red_commit_ref(record.issue_iid, retained_checkpoint.base_sha)
        green_ref = green_commit_ref(
            record.issue_iid, retained_checkpoint.base_sha, green_tree
        )
        return (
            dependencies.read_ref(red_ref) == red_sha
            and dependencies.read_ref(green_ref) == source_sha
            and red_dependencies.commit_identity(red_sha)
            == (retained_checkpoint.base_sha, retained_checkpoint.tree_oid)
            and all(
                dependencies.blob_oid(source_sha, path)
                == dependencies.blob_oid(red_sha, path)
                for path in retained_checkpoint.test_paths
            )
        )

    def review_base() -> str:
        if pending_coding_merge:
            if record.main_sha is None:
                raise WorkerProtocolError(
                    "candidate_broker", "pending review target is unavailable"
                )
            return record.main_sha
        if committed_candidate is None:
            return candidate_base_sha
        select_base = getattr(repository, "committed_candidate_read_base", None)
        if not callable(select_base):
            raise WorkerProtocolError(
                "candidate_broker", "original review base is unavailable"
            )
        base = select_base(
            record.worktree,
            candidate_base_sha,
            committed_candidate.commit_sha,
            committed_candidate.red_sha,
        )
        if base != candidate_base_sha:
            raise WorkerProtocolError(
                "candidate_broker", "original review base changed"
            )
        return base

    def candidate_action(
        action: str, tree: str | None, selector: dict[str, object] | None = None
    ) -> dict[str, object]:
        nonlocal validated_candidate_paths, successfully_validated_tree
        nonlocal pending_deferred_tree
        nonlocal prepared_candidate
        nonlocal committed_candidate, candidate_head
        range_read = (
            action == "read"
            and selector is not None
            and set(selector)
            == {
                "path",
                "side",
                "offset",
                "length",
            }
        )
        early_content = (
            record.state is RunnerState.EXECUTING
            and record.phase is WorkflowPhase.WORKFLOW
            and record.workflow_lane is WorkflowLane.UNKNOWN
            and retained_checkpoint is None
        )
        if early_content and action not in {"stage", "read"}:
            raise WorkerProtocolError(
                "candidate_broker", "candidate request is invalid"
            )
        if early_content and not range_read:
            observed = repository.changed_paths(record.worktree)
            if not observed or any(
                not path.endswith((".md", ".md.jinja")) for path in observed
            ):
                raise WorkerProtocolError(
                    "candidate_broker", "candidate request is invalid"
                )
        current = ledger.load(record.issue_iid)
        if (
            current is None
            or current.revision != record.revision
            or current.state is not record.state
            or current.phase is not record.phase
            or current.source_sha != record.source_sha
            or record.source_sha is None
            or repository.head(record.worktree) != candidate_head
            and not (
                action == "stage"
                and committed_candidate is not None
                and repository.head(record.worktree) == candidate_base_sha
            )
        ):
            raise WorkerProtocolError("candidate_broker", "identity changed")
        try:
            if pending_coding_merge and not pending_merge_identity_is_exact():
                raise WorkerProtocolError("candidate_broker", "identity changed")
            if action == "stage":
                successfully_validated_tree = None
                candidate_head = repository.head(record.worktree)
                staged_tree = repository.stage_candidate(
                    record.worktree,
                    candidate_base_sha,
                    allow_base_ancestor=uses_authored_base
                    or candidate_head != candidate_base_sha,
                )
                committed_candidate = None
                prepared_candidate = staged_tree
                pending_deferred_tree = None
                validated_candidate_paths = observed_candidate_paths()
                return {
                    "candidate_tree": staged_tree,
                    "state": "staged",
                    "changed_path_count": len(validated_candidate_paths),
                }
            if (
                tree is None
                or tree != prepared_candidate
                or validated_candidate_paths is None
                or not range_read
                and (
                    validated_candidate_paths != observed_candidate_paths()
                    or repository.red_candidate_tree(
                        record.worktree, candidate_base_sha, validated_candidate_paths
                    )
                    != tree
                )
            ):
                raise WorkerProtocolError("candidate_broker", "candidate changed")
            if (
                action in {"validate", "status"}
                and retained_checkpoint is not None
                and record.workflow_lane is WorkflowLane.CODING
                and record.work_kind is WorkKind.ORDINARY
                and hasattr(repository, "green_commit_dependencies")
                and not pending_coding_merge
            ):
                raise WorkerProtocolError(
                    "candidate_broker", "candidate request is invalid"
                )
            if action == "commit":
                if (
                    pending_coding_merge
                    or retained_checkpoint is None
                    or record.work_kind is not WorkKind.ORDINARY
                    or record.workflow_lane is not WorkflowLane.CODING
                    or not hasattr(repository, "red_commit_dependencies")
                    or not hasattr(repository, "green_commit_dependencies")
                    or retained_checkpoint.tree_oid == tree
                ):
                    raise WorkerProtocolError(
                        "candidate_broker", "candidate request is invalid"
                    )
                if committed_candidate is not None:
                    if not committed_identity_is_exact():
                        raise WorkerProtocolError(
                            "candidate_broker", "candidate changed"
                        )
                    proof = committed_candidate
                else:
                    try:
                        request = RedCheckpointRequest(
                            argv=retained_checkpoint.argv,
                            expected_failure=retained_checkpoint.expected_failure,
                            test_paths=retained_checkpoint.test_paths,
                        )
                        with tempfile.TemporaryDirectory(
                            prefix="agent-work-commit-"
                        ) as scratch:
                            red = create_red_commit(
                                issue_iid=record.issue_iid,
                                worktree=record.worktree,
                                base_sha=candidate_base_sha,
                                candidate_tree=retained_checkpoint.tree_oid,
                                request=request,
                                dependencies=cast(
                                    _CommitRepository, repository
                                ).red_commit_dependencies(
                                    record.worktree, Path(scratch) / "red"
                                ),
                                previous_head=candidate_head,
                            )
                            proof = create_green_commit(
                                issue_iid=record.issue_iid,
                                worktree=record.worktree,
                                branch=record.source_branch,
                                base_sha=candidate_base_sha,
                                candidate_tree=tree,
                                red_proof=red,
                                message=f"Resolve issue #{record.issue_iid}",
                                dependencies=cast(
                                    _CommitRepository, repository
                                ).green_commit_dependencies(
                                    record.worktree, Path(scratch) / "green"
                                ),
                                previous_head=candidate_head,
                                immutable_ref=True,
                            )
                    except (
                        RedCommitError,
                        GreenCommitError,
                        RepositoryError,
                        AssertionError,
                    ) as error:
                        raise WorkerProtocolError(
                            "candidate_broker", "trusted candidate commit failed"
                        ) from error
                    committed_candidate = proof
                    candidate_head = proof.commit_sha
                successfully_validated_tree = tree
                return {
                    "candidate_tree": tree,
                    "committed_tree": proof.tree_oid,
                    "commit_sha": proof.commit_sha,
                    "red_sha": proof.red_sha,
                    "red_ref": red_commit_ref(record.issue_iid, candidate_base_sha),
                    "state": "terminal-success",
                    "timing_status": proof.timing_status,
                    "junit_timings": list(proof.junit_timings),
                    **(
                        {"timing_unknown": {"reason": "fixed JUnit unavailable"}}
                        if proof.timing_status == "unknown"
                        else {}
                    ),
                }
            if action == "read":
                if (
                    committed_candidate is not None
                    and not committed_identity_is_exact()
                ):
                    raise WorkerProtocolError("candidate_broker", "candidate changed")
                if (
                    record.phase is WorkflowPhase.WORKFLOW
                    and (
                        record.workflow_lane is WorkflowLane.CODING
                        or retained_checkpoint is not None
                    )
                    and successfully_validated_tree != tree
                ):
                    raise WorkerProtocolError(
                        "candidate_broker", "candidate request is invalid"
                    )
                if selector == {"mode": "changed_paths"}:
                    read_base = review_base()
                    evidence = repository.changed_candidate_paths(
                        record.worktree,
                        tree,
                        base_sha=read_base,
                        allow_base_ancestor=uses_authored_base
                        or read_base != repository.head(record.worktree),
                    )
                elif selector is not None and set(selector) == {
                    "path",
                    "side",
                    "offset",
                    "length",
                }:
                    evidence = repository.read_candidate_range(
                        record.worktree,
                        tree,
                        base_sha=review_base(),
                        allow_base_ancestor=uses_authored_base
                        or review_base() != repository.head(record.worktree),
                        path=cast(str, selector["path"]),
                        side=cast(str, selector["side"]),
                        offset=cast(int, selector["offset"]),
                        length=cast(int, selector["length"]),
                    )
                else:
                    raise WorkerProtocolError(
                        "candidate_broker", "candidate request is invalid"
                    )
                refreshed = ledger.load(record.issue_iid)
                if (
                    refreshed is None
                    or refreshed.revision != record.revision
                    or refreshed.state is not record.state
                    or refreshed.phase is not record.phase
                    or refreshed.source_sha != record.source_sha
                    or repository.head(record.worktree)
                    not in (
                        {record.source_sha}
                        if committed_candidate is None
                        else {committed_candidate.commit_sha}
                    )
                    or not range_read
                    and validated_candidate_paths != observed_candidate_paths()
                ):
                    raise WorkerProtocolError("candidate_broker", "identity changed")
                result = dict(evidence) | {"base_sha": review_base()}
                if committed_candidate is not None:
                    result |= {
                        "commit_sha": committed_candidate.commit_sha,
                        "committed_tree": committed_candidate.tree_oid,
                    }
                return result
            if action == "review":
                if (
                    committed_candidate is not None
                    and not committed_identity_is_exact()
                ):
                    raise WorkerProtocolError("candidate_broker", "candidate changed")
                if committed_candidate is not None:
                    review_base()
                if (
                    successfully_validated_tree != tree
                    or selector is None
                    or set(selector) != {"verdict", "summary", "findings"}
                ):
                    raise WorkerProtocolError(
                        "candidate_broker", "candidate review is invalid"
                    )
                refreshed = ledger.load(record.issue_iid)
                if (
                    refreshed is None
                    or refreshed.revision != record.revision
                    or refreshed.state is not record.state
                    or refreshed.phase is not record.phase
                    or refreshed.source_sha != record.source_sha
                    or repository.head(record.worktree)
                    != (
                        record.source_sha
                        if committed_candidate is None
                        else committed_candidate.commit_sha
                    )
                    or validated_candidate_paths != observed_candidate_paths()
                    or repository.red_candidate_tree(
                        record.worktree, candidate_base_sha, validated_candidate_paths
                    )
                    != tree
                ):
                    raise WorkerProtocolError("candidate_broker", "identity changed")
                return {"candidate_tree": tree, "state": "review-recorded", **selector}
            report = repository.judge_candidate(
                record.worktree,
                tree,
                base_sha=candidate_base_sha,
                allow_base_ancestor=uses_authored_base,
                changed_paths=validated_candidate_paths,
                action=action,
                timeout_seconds=worker_timeout,
                timing_review_required=True,
            )
            if pending_coding_merge:
                report = dict(report) | {
                    "source_sha": record.source_sha,
                    "target_sha": record.main_sha,
                }
            timing_review = report.get("timing_review")
            artifacts = report.get("junit_artifacts")
            timings = report.get("junit_timings")
            comparison_unknown = (
                report.get("timing_status") == "complete"
                and report.get("timing_comparison_status") == "unknown"
                and report.get("timing_comparison_reason") == "baseline_unavailable"
                and timing_review is None
                and report.get("timing_unknown") is None
                and isinstance(artifacts, list)
                and len(artifacts) == 2
                and all(isinstance(item, dict) for item in artifacts)
                and {
                    item.get("environment")
                    for item in cast(list[dict[str, object]], artifacts)
                }
                == {"py311", "py314"}
                and isinstance(timings, list)
                and len(timings) == 2
                and all(isinstance(item, dict) for item in timings)
                and {
                    item.get("environment")
                    for item in cast(list[dict[str, object]], timings)
                    if item.get("timing_complete") is True
                }
                == {"py311", "py314"}
            )
            if report.get("state") != "deferred" and pending_deferred_tree == tree:
                pending_deferred_tree = None
            if report.get("state") == "terminal-success" and (
                report.get("timing_status") == "unknown"
                and isinstance(report.get("timing_unknown"), dict)
                or comparison_unknown
                or isinstance(timing_review, dict)
                and cast(dict[str, object], timing_review).get("outcome")
                in {"clear", "report_only"}
            ):
                successfully_validated_tree = tree
                pending_deferred_tree = None
            elif action == "validate" and report.get("state") == "deferred":
                pending_deferred_tree = tree
            return report
        except RepositoryError as error:
            reason = str(error)
            safe_reasons = {
                "candidate changed paths are invalid",
                "candidate tree is invalid",
                "candidate changed paths changed during staging",
                "candidate changed during staging",
                "candidate action is invalid",
                "candidate tree changed",
                "candidate validation response is malformed",
                "candidate validation response is invalid",
                "candidate validation evidence is invalid",
                "candidate validation state is invalid",
                "candidate validation exit is inconsistent",
                "candidate failure evidence is invalid",
                "candidate timing evidence is invalid",
                "candidate JUnit timing evidence is invalid",
                "candidate changed during validation",
                "candidate changed",
                "candidate base tree is invalid",
                "candidate read failed",
                "candidate object is invalid",
                "candidate content is oversize or binary",
            }
            raise WorkerProtocolError(
                "candidate_broker",
                reason
                if reason in safe_reasons
                else "trusted candidate operation failed",
            ) from error

    lock_factory = issue_lock or (lambda _issue_iid: nullcontext())
    invocation = None
    if (
        record.state is RunnerState.EXECUTING
        and record.work_kind is WorkKind.ORDINARY
        and record.phase is WorkflowPhase.WORKFLOW
        and record.workflow_lane is not WorkflowLane.CONTENT
        and apply_red_checkpoint is not None
        and retained_checkpoint is None
    ):
        with lock_factory(record.issue_iid):
            on_worker_invoke(record.issue_iid)
            red_invocation = invoke.invoke(
                record.worktree,
                prompt,
                worker_timeout,
                broker_request=broker_request,
                build_artifact=_broker_artifact_builder(
                    record,
                    broker_request,
                    repository=repository,
                    red_checkpoint=retained_checkpoint,
                    apply_red_checkpoint=apply_red_checkpoint,
                    load_red_checkpoint=load_red_checkpoint,
                ),
                candidate_action=candidate_action,
            )
        if red_invocation.protocol_error is not None:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                retry_delay=timedelta(),
                detail=format_worker_result_diagnostic(red_invocation.protocol_error),
                stop_on_unchanged_condition=True,
            )
        if red_invocation.returncode != 0:
            worker_exit = (
                worker_exit_evidence(
                    red_invocation.returncode, red_invocation.stderr, prompt=prompt
                )
                if red_invocation.returncode is not None
                else None
            )
            return _record_retry(
                record.model_copy(update={"worker_exit": worker_exit}),
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                error_code=(
                    ErrorCode.WORKER_TIMEOUT
                    if red_invocation.returncode is None
                    else ErrorCode.WORKER_EXIT
                ),
                retry_delay=timedelta(),
            )
        try:
            red_changed_paths = tuple(
                sorted(set(repository.changed_paths(record.worktree)))
            )
            accepted_artifact = parse_worker_result_accepted_artifact(
                red_invocation.worker_result,
                request=broker_request,
                changed_paths=red_changed_paths,
            )
            red_result = accepted_artifact.envelope
        except WorkerProtocolError as error:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                retry_delay=timedelta(),
                detail=format_worker_result_diagnostic(error),
                stop_on_unchanged_condition=True,
            )
        try:
            if (
                red_result.expected_state is not record.state
                or red_result.expected_phase is not record.phase
            ):
                raise RedCheckpointError("worker result identity is not exact")
            checkpoint_request = red_result.red_checkpoint_request
            if (
                red_result.workflow_lane is WorkflowLane.CONTENT
                or red_result.result_kind
                in {"split_required", "needs_human", "retryable"}
            ):
                invocation = red_invocation
            elif (
                red_result.result_kind != "red_checkpoint_ready"
                or checkpoint_request is None
                or (red_changed_paths) != checkpoint_request.test_paths
            ):
                raise RedCheckpointError("red checkpoint handoff is not exact")
            if invocation is None:
                if checkpoint_request is None:
                    raise RedCheckpointError("red checkpoint request is missing")
                try:
                    retained_checkpoint = _retained_red_checkpoint(
                        record, load_red_checkpoint
                    )
                    if retained_checkpoint is None:
                        raise RedCheckpointError("checkpoint did not reload")
                    if retained_checkpoint != accepted_artifact.red_checkpoint:
                        raise RedCheckpointError("checkpoint artifact did not reload")
                    record = ledger.persist(
                        transition_record(
                            record,
                            RunnerState.EXECUTING,
                            phase=WorkflowPhase.WORKFLOW,
                            workflow_lane=WorkflowLane.UNKNOWN,
                            summary=red_result.change_summary,
                            next_action=red_result.next_action,
                            error_code=None,
                        )
                    )
                    if observe_interactive_validation():
                        return record
                except (RedCheckpointError, RepositoryError, ValueError):
                    return _record_retry(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=completion_clock(),
                        error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                        retry_delay=timedelta(),
                        detail="worker_result_broker: red checkpoint was not retained",
                        stop_on_unchanged_condition=True,
                    )
                if remaining_seconds is not None:
                    remaining = remaining_seconds()
                    if remaining <= 0:
                        return None
                    worker_timeout = max(1, math.ceil(remaining))
                prompt_record = record.model_dump(mode="json")
                prompt_record["red_checkpoint"] = retained_checkpoint.model_dump(
                    mode="json"
                )
                previous_nonce = broker_request.nonce
                source_sha = record.source_sha or repository.head(record.worktree)
                nonce = secrets.token_hex(16)
                while nonce == previous_nonce:
                    nonce = secrets.token_hex(16)
                broker_request = WorkerResultBrokerRequest(
                    nonce=nonce,
                    project_id=record.project_id,
                    issue_iid=record.issue_iid,
                    ledger_revision=record.revision,
                    expected_state=record.state,
                    expected_phase=record.phase,
                    source_branch=record.source_branch,
                    source_sha=source_sha,
                )
                prompt = build_worker_prompt(
                    prompt_record,
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
                        if _feedback_is_qualifying(
                            note, repository.config.factory_username
                        )
                    ),
                    worker_result_broker=broker_request,
                    worker_result_builder=(
                        repository.config.checkout / "bin" / "agent-work-from-checkout"
                    ),
                    retained_red_checkpoint=retained_checkpoint,
                )
        except (RedCheckpointError, RepositoryError, ValueError):
            return _block_red_checkpoint(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                stage=RedCheckpointFailureStage.RESULT_REQUEST_PATH,
            )
    if invocation is None:
        with lock_factory(record.issue_iid):
            on_worker_invoke(record.issue_iid)
            invocation = invoke.invoke(
                record.worktree,
                prompt,
                worker_timeout,
                broker_request=broker_request,
                build_artifact=_broker_artifact_builder(
                    record,
                    broker_request,
                    repository=repository,
                    red_checkpoint=retained_checkpoint,
                    apply_red_checkpoint=apply_red_checkpoint,
                    load_red_checkpoint=load_red_checkpoint,
                ),
                candidate_action=candidate_action,
            )
    refreshed_correction = refresh_publication_correction_identity(
        record, repository=repository, load_red_checkpoint=load_red_checkpoint
    )
    if refreshed_correction is None:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Publication correction identity changed after worker invocation.",
        )
    record = refreshed_correction
    if pending_deferred_tree is not None:
        return _record_timing_deferred(record, ledger=ledger, now=completion_clock())
    if invocation.protocol_error is not None:
        if record.state is RunnerState.REPAIR and record.repair_attempt is not None:
            return _record_repair_failure(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                evidence_delta={"worker_protocol": "invalid"},
                failure_reason="repair worker returned an invalid result",
            )
        return _record_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            now=completion_clock(),
            error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
            retry_delay=timedelta(),
            detail=format_worker_result_diagnostic(invocation.protocol_error),
            stop_on_unchanged_condition=True,
        )
    if invocation.returncode != 0:
        worker_exit = (
            worker_exit_evidence(
                invocation.returncode, invocation.stderr, prompt=prompt
            )
            if invocation.returncode is not None
            else None
        )
        if record.state is RunnerState.REPAIR and record.repair_attempt is not None:
            return _record_repair_failure(
                record.model_copy(update={"worker_exit": worker_exit}),
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                evidence_delta={"worker_returncode": invocation.returncode},
                failure_reason="repair worker returned a non-zero result",
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
            final_changed_paths = observed_candidate_paths()
            result = parse_worker_result_artifact(
                invocation.worker_result,
                request=broker_request,
                changed_paths=final_changed_paths,
            )
        except RepositoryTransientError:
            if record.state is RunnerState.REPAIR and record.repair_attempt is not None:
                return _record_repair_failure(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    evidence_delta={"validation": "repository_transient"},
                    failure_reason="repair validation encountered a repository transient",
                )
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
            )
        except RepositoryError as error:
            if record.state is RunnerState.REPAIR and record.repair_attempt is not None:
                return _record_repair_failure(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    evidence_delta={"validation": "repository_error"},
                    failure_reason=f"repair validation failed: {error}",
                )
            return _block_publication_identity(
                record, ledger=ledger, gitlab=gitlab, summary=str(error)
            )
        except WorkerProtocolError as error:
            if record.state is RunnerState.REPAIR and record.repair_attempt is not None:
                return _record_repair_failure(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    evidence_delta={"worker_protocol": "invalid"},
                    failure_reason="repair worker returned an invalid result",
                )
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                retry_delay=timedelta(),
                detail=format_worker_result_diagnostic(error),
                stop_on_unchanged_condition=True,
            )
        else:
            if (
                result.expected_state is not record.state
                or result.expected_phase is not record.phase
            ):
                if (
                    record.state is RunnerState.REPAIR
                    and record.repair_attempt is not None
                ):
                    return _record_repair_failure(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                        evidence_delta={"worker_result": "state_or_phase_mismatch"},
                        failure_reason="repair worker result did not match the recorded phase",
                    )
                raise SegmentError("worker result does not match the recorded phase")
            if result.result_kind == "retryable":
                if (
                    record.state is RunnerState.REPAIR
                    and record.repair_attempt is not None
                ):
                    return _record_repair_failure(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                        evidence_delta={"worker_result": "retryable"},
                        failure_reason="repair worker requested a retry",
                    )
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=result.error_code or ErrorCode.WORKER_EXIT,
                    retry_delay=timedelta(),
                )
            try:
                if result.workflow_lane is WorkflowLane.CODING:
                    retained_checkpoint = _retained_red_checkpoint(
                        record, load_red_checkpoint
                    )
                    validate_worker_result(
                        record,
                        result,
                        changed_paths=(final_changed_paths),
                        red_checkpoint=retained_checkpoint,
                    )
                else:
                    validate_worker_result(
                        record,
                        result,
                        changed_paths=(final_changed_paths),
                        red_checkpoint=None,
                    )
                if result.result_kind == "split_required":
                    if result.split_plan is None:
                        raise WorkerProtocolError(
                            "controller_invariant.split_required", "is required"
                        )
                    if (
                        record.state is not RunnerState.EXECUTING
                        or record.work_kind is not WorkKind.ORDINARY
                        or record.merge_request_iid is not None
                        or record.merge_request_url is not None
                        or record.merge_request_draft is not None
                        or record.publication_sha is not None
                        or record.pending_feedback is not None
                        or record.feedback_cursor is not None
                    ):
                        raise WorkerProtocolError(
                            "controller_invariant.split_required",
                            "is limited to fresh ordinary intake",
                        )
                    return _fulfill_split(
                        record,
                        result.split_plan,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                    )
                if (
                    record.state is RunnerState.EXECUTING
                    and record.phase is WorkflowPhase.WORKFLOW
                    and record.workflow_lane is WorkflowLane.UNKNOWN
                    and retained_checkpoint is None
                    and retained_ordinary_is_untouched(record)
                    and result.result_kind == "needs_human"
                    and result.error_code is ErrorCode.AGENT_CAPACITY_UNAVAILABLE
                    and not final_changed_paths
                    and record.source_sha is not None
                ):
                    try:
                        repository.verify_worktree(
                            record.worktree, record.source_branch, record.source_sha
                        )
                        status = repository.observe_worktree_status(record.worktree)
                    except RepositoryError:
                        pass
                    else:
                        if not status.dirty and not status.changed_paths:
                            return _record_capacity_deferred(
                                record, ledger=ledger, now=now
                            )
                outcome = apply_worker_result(record, result, now=now).record
            except RepositoryTransientError:
                if (
                    record.state is RunnerState.REPAIR
                    and record.repair_attempt is not None
                ):
                    return _record_repair_failure(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                        evidence_delta={"validation": "repository_transient"},
                        failure_reason="repair validation encountered a repository transient",
                    )
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
                )
            except RepositoryError as error:
                if (
                    record.state is RunnerState.REPAIR
                    and record.repair_attempt is not None
                ):
                    return _record_repair_failure(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                        evidence_delta={"validation": "repository_error"},
                        failure_reason=f"repair validation failed: {error}",
                    )
                return _block_publication_identity(
                    record, ledger=ledger, gitlab=gitlab, summary=str(error)
                )
            except (ValueError, WorkerProtocolError) as error:
                if (
                    record.state is RunnerState.REPAIR
                    and record.repair_attempt is not None
                ):
                    return _record_repair_failure(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        now=now,
                        evidence_delta={"validation": "protocol_invalid"},
                        failure_reason="repair result validation was not coherent",
                    )
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
                    retry_delay=timedelta(),
                    detail=(
                        format_worker_result_diagnostic(error)
                        if isinstance(error, WorkerProtocolError)
                        else "controller_invariant.transition: is not coherent with recorded state"
                    ),
                    stop_on_unchanged_condition=True,
                )
            else:
                outcome = outcome.model_copy(
                    update={
                        "worker_exit": None,
                        "correction_identity": (
                            None
                            if outcome.state is RunnerState.PUBLISHING
                            else outcome.correction_identity
                        ),
                    }
                )
    if outcome.state is RunnerState.PUBLISHING and result.workflow_lane in {
        WorkflowLane.CODING,
        WorkflowLane.CONTENT,
    }:
        try:
            actual_tree = repository.red_candidate_tree(
                record.worktree, candidate_base_sha, final_changed_paths
            )
        except RepositoryError:
            actual_tree = None
        if (
            invocation.inventory_candidate is None
            or (
                result.workflow_lane is WorkflowLane.CODING
                and invocation.validated_candidate != invocation.inventory_candidate
            )
            or validated_candidate_paths != final_changed_paths
            or actual_tree != invocation.inventory_candidate
        ):
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=completion_clock(),
                error_code=ErrorCode.WORKER_PROTOCOL_INVALID,
                retry_delay=timedelta(),
                detail="candidate_broker: validated candidate changed or is missing",
                stop_on_unchanged_condition=True,
            )
    if (
        outcome.state is RunnerState.PUBLISHING
        and record.pending_feedback is not None
        and bool(final_changed_paths)
    ):
        validated_paths = tuple(sorted(set(final_changed_paths)))
        if (
            not validated_paths
            or validated_paths != final_changed_paths
            or record.source_sha is None
        ):
            return _block_feedback_path_mismatch(record, ledger=ledger, gitlab=gitlab)
        outcome = outcome.model_copy(
            update={
                "intent": TransitionIntentRecord(
                    operation_id=f"{record.issue_iid}:publish:{record.revision}",
                    kind="publish_merge_request",
                    expected_evidence={
                        "branch": record.source_branch,
                        "baseline_sha": candidate_base_sha,
                    }
                    | _validated_path_evidence(validated_paths),
                )
            }
        )
    if outcome.state is RunnerState.PUBLISHING and pending_coding_merge:
        if (
            successfully_validated_tree is None
            or invocation.validated_candidate != successfully_validated_tree
            or not pending_merge_identity_is_exact()
            or retained_checkpoint is None
            or record.source_sha is None
            or record.main_sha is None
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Validated pending reconciliation identity changed.",
            )
        dependencies = cast(_CommitRepository, repository).green_commit_dependencies(
            record.worktree, Path()
        )
        _, green_tree, _ = dependencies.commit_identity(record.source_sha)
        outcome = outcome.model_copy(
            update={
                "intent": TransitionIntentRecord(
                    operation_id=f"{record.issue_iid}:reconcile:{record.revision}",
                    kind="reconcile_commit",
                    expected_evidence={
                        "branch": record.source_branch,
                        "source_sha": record.source_sha,
                        "target_sha": record.main_sha,
                        "candidate_tree": successfully_validated_tree,
                        "red_ref": red_commit_ref(
                            record.issue_iid, retained_checkpoint.base_sha
                        ),
                        "green_ref": green_commit_ref(
                            record.issue_iid,
                            retained_checkpoint.base_sha,
                            green_tree,
                        ),
                    },
                )
            }
        )
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
        try:
            retained_changes = repository.has_changes(record.worktree)
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
        if retained_changes:
            validated_paths = tuple(sorted(set(final_changed_paths)))
            if not validated_paths or validated_paths != final_changed_paths:
                return _block_feedback_path_mismatch(
                    record, ledger=ledger, gitlab=gitlab
                )
            if record.source_sha is None:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Feedback publication has no exact baseline SHA.",
                )
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.PUBLISHING,
                    phase=WorkflowPhase.READY_TO_PUBLISH,
                    workflow_lane=outcome.workflow_lane,
                    merge_request_draft=outcome.merge_request_draft,
                    pending_feedback=outcome.pending_feedback.model_copy(
                        update={"acknowledgement_ready": False}
                    ),
                    intent=TransitionIntentRecord(
                        operation_id=f"{record.issue_iid}:publish:{record.revision}",
                        kind="publish_merge_request",
                        expected_evidence={
                            "branch": record.source_branch,
                            "baseline_sha": candidate_base_sha,
                        }
                        | _validated_path_evidence(validated_paths),
                    ),
                    publication_sha=None,
                    summary="Validated feedback changes require publication.",
                    next_action=(
                        "Bind and publish the exact validated feedback paths, then "
                        "observe the exact source-SHA pipeline."
                    ),
                    error_code=None,
                )
            )
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
    if outcome.state is RunnerState.AWAITING_HUMAN:
        try:
            _ensure_awaiting_handoff(outcome, gitlab=gitlab, repository=repository)
        except GitLabError:
            return _record_retry(
                outcome,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GITLAB_RETRYABLE,
            )
        except RepositoryError as error:
            return _block_publication_identity(
                outcome, ledger=ledger, gitlab=gitlab, summary=str(error)
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
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
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
    ordinary_coding = (
        record.workflow_lane is WorkflowLane.CODING
        and record.work_kind is WorkKind.ORDINARY
        and record.pending_feedback is None
        and hasattr(repository, "green_commit_dependencies")
        and hasattr(repository, "red_commit_dependencies")
    )
    reconciled_now = False
    if record.intent is not None and record.intent.kind == "reconcile_commit":
        if not _reconcile_intent_is_exact(record) or not hasattr(
            repository, "prepare_reconcile_commit"
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Durable reconciliation intent is not exact.",
            )
        evidence = record.intent.expected_evidence
        try:
            reconciled_sha = cast(
                _CommitRepository, repository
            ).prepare_reconcile_commit(
                record.worktree,
                record.source_branch,
                baseline_sha,
                cast(str, record.main_sha),
                cast(str, evidence["candidate_tree"]),
                cast(str, evidence["red_ref"]),
                cast(str, evidence["green_ref"]),
            )
        except RepositoryTransientError:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
            )
        except ReconciliationTargetChanged:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Current target changed after reconciliation commit.",
                next_action=(
                    "Review the retained Green source and start a fresh reconciliation "
                    "against the current target."
                ),
            )
        except RepositoryError as error:
            return _block_publication_identity(
                record, ledger=ledger, gitlab=gitlab, summary=str(error)
            )
        record = ledger.persist(
            record.model_copy(
                update={
                    "publication_sha": reconciled_sha,
                    "intent": TransitionIntentRecord(
                        operation_id=f"{record.issue_iid}:publish:{record.revision}",
                        kind="publish_merge_request",
                        expected_evidence={
                            "branch": record.source_branch,
                            "baseline_sha": baseline_sha,
                            "publication_sha": reconciled_sha,
                            "source_sha": baseline_sha,
                            "target_sha": record.main_sha,
                            "candidate_tree": evidence["candidate_tree"],
                            "red_ref": evidence["red_ref"],
                            "green_ref": evidence["green_ref"],
                        },
                    ),
                }
            )
        )
        ordinary_coding = False
        reconciled_now = True

    def verified_green_tip() -> str:
        checkpoint = _retained_red_checkpoint(record, load_red_checkpoint)
        if checkpoint is None or checkpoint.base_sha != baseline_sha:
            raise RepositoryError("accepted Red checkpoint is unavailable")
        commit_repository = cast(_CommitRepository, repository)
        dependencies = commit_repository.green_commit_dependencies(
            record.worktree, Path()
        )
        red_dependencies = commit_repository.red_commit_dependencies(
            record.worktree, Path()
        )
        branch, head, index_tree, worktree_tree = dependencies.worker_state(
            record.worktree
        )
        ref = green_commit_ref(record.issue_iid, baseline_sha, index_tree)
        red_ref = f"refs/agent-work/red-commits/{record.issue_iid}/{baseline_sha}"
        red_sha = dependencies.read_ref(red_ref)
        if (
            branch != record.source_branch
            or index_tree != worktree_tree
            or red_sha is None
            or red_dependencies.commit_identity(red_sha)
            != (baseline_sha, checkpoint.tree_oid)
            or dependencies.read_ref(ref) != head
            or dependencies.commit_identity(head)[:2] != (red_sha, index_tree)
            or any(
                dependencies.blob_oid(head, path)
                != dependencies.blob_oid(red_sha, path)
                for path in checkpoint.test_paths
            )
        ):
            raise RepositoryError("verified Green commit identity changed")
        return head

    if not remediation and draft is None:
        if record.intent is not None:
            raise SegmentError("worker did not provide exact publication metadata")
        replay_phase = {
            WorkflowLane.CODING: WorkflowPhase.CODING_FINALIZE,
            WorkflowLane.CONTENT: WorkflowPhase.CONTENT_FINALIZE,
        }.get(record.workflow_lane)
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
        record, baseline_sha=baseline_sha, repository=repository
    ):
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Durable publication intent is not exact.",
        )
    if (
        record.intent is not None
        and "candidate_tree" in record.intent.expected_evidence
    ):
        evidence = record.intent.expected_evidence
        if not reconciled_now:
            try:
                proof_sha = cast(
                    _CommitRepository, repository
                ).prepare_reconcile_commit(
                    record.worktree,
                    record.source_branch,
                    baseline_sha,
                    cast(str, record.main_sha),
                    cast(str, evidence["candidate_tree"]),
                    cast(str, evidence["red_ref"]),
                    cast(str, evidence["green_ref"]),
                )
                if proof_sha != record.publication_sha:
                    raise RepositoryError("reconciliation commit identity changed")
            except RepositoryTransientError:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.GIT_TRANSPORT_RETRYABLE,
                )
            except ReconciliationTargetChanged:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Current target changed after reconciliation commit.",
                    next_action=(
                        "Review the retained Green source and start a fresh "
                        "reconciliation against the current target."
                    ),
                )
            except _UnsupportedTestExecutionHook:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="unsupported_hook",
                )
            except RepositoryError:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="reconciliation commit identity changed",
                )
        ordinary_coding = False
    if replaying and record.pending_feedback is not None:
        feedback_paths = _feedback_validated_paths(record)
        if feedback_paths is None:
            return _block_feedback_path_mismatch(record, ledger=ledger, gitlab=gitlab)
        feedback_protected = tuple(
            path for path in feedback_paths if is_protected_policy_path(path)
        )
        if feedback_protected and (
            draft is not None and not _draft_handoff_is_valid(draft)
        ):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Protected agent-policy merge-request metadata is incomplete.",
            )
    if not replaying:
        target_sha = record.main_sha or baseline_sha
        try:
            prospective_paths = repository.publication_changed_paths(
                record.worktree, target_sha
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
        protected = tuple(
            path for path in prospective_paths if is_protected_policy_path(path)
        )
        if protected and (draft is not None and not _draft_handoff_is_valid(draft)):
            blocked = ledger.persist(
                transition_record(
                    record,
                    RunnerState.BLOCKED,
                    error_code=ErrorCode.PUBLICATION_IDENTITY_MISMATCH,
                    summary="Protected agent-policy merge-request metadata is incomplete.",
                    next_action=(
                        "Resume the workflow and return coherent merge request metadata."
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
        expected_evidence: dict[str, str | int | None] = {
            "branch": record.source_branch,
            "baseline_sha": baseline_sha,
        }
        if record.pending_feedback is not None:
            validated_paths = _feedback_validated_paths(record)
            if validated_paths is None:
                return _block_feedback_path_mismatch(
                    record, ledger=ledger, gitlab=gitlab
                )
            expected_evidence |= _validated_path_evidence(validated_paths)
        record = ledger.persist(
            record.model_copy(
                update={
                    "intent": TransitionIntentRecord(
                        operation_id=f"{record.issue_iid}:publish:{record.revision}",
                        kind="publish_merge_request",
                        expected_evidence=expected_evidence,
                    )
                }
            )
        )
    publication_was_bound = record.publication_sha is not None
    try:
        if record.publication_sha is None:
            validated_paths = (
                _feedback_validated_paths(record)
                if record.pending_feedback is not None
                else None
            )
            if record.pending_feedback is not None and validated_paths is None:
                return _block_feedback_path_mismatch(
                    record, ledger=ledger, gitlab=gitlab
                )
            current_head = (
                repository.head(record.worktree)
                if record.pending_feedback is not None
                else baseline_sha
            )
            if (
                record.pending_feedback is not None
                and current_head == baseline_sha
                and tuple(sorted(set(repository.changed_paths(record.worktree))))
                != validated_paths
            ):
                return _block_feedback_path_mismatch(
                    record, ledger=ledger, gitlab=gitlab
                )
            if (
                replaying
                and record.pending_feedback is not None
                and current_head != baseline_sha
            ):
                if repository.has_changes(record.worktree):
                    return _block_publication_identity(
                        record,
                        ledger=ledger,
                        gitlab=gitlab,
                        summary="Retained feedback publication commit is not clean.",
                    )
                if (
                    tuple(
                        sorted(
                            set(
                                repository.publication_changed_paths(
                                    record.worktree, baseline_sha
                                )
                            )
                        )
                    )
                    != validated_paths
                ):
                    return _block_feedback_path_mismatch(
                        record, ledger=ledger, gitlab=gitlab
                    )
            if record.pending_feedback is not None:
                default_sha = repository.observe_current_default(
                    record.worktree, record.source_branch, current_head
                )
                # Probe pre-bind ancestry; reconciliation still owns the only
                # merge decision after the exact commit is bound.
                _ = repository.is_ancestor(default_sha, current_head, record.worktree)
            if (
                replaying
                and record.pending_feedback is not None
                and current_head != baseline_sha
            ):
                prepared_sha = repository.bind_retained_commit(
                    record.worktree,
                    record.source_branch,
                    record.issue_iid,
                    baseline_sha,
                )
            elif replaying and record.pending_feedback is not None:
                prepared_sha = repository.prepare_commit(
                    record.worktree,
                    record.source_branch,
                    record.issue_iid,
                    expected_head=baseline_sha,
                    validated_paths=validated_paths,
                )
            elif ordinary_coding:
                prepared_sha = verified_green_tip()
            elif replaying:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Retained publication commit was not durably bound.",
                )
            else:
                prepared_sha = repository.prepare_commit(
                    record.worktree,
                    record.source_branch,
                    record.issue_iid,
                    expected_head=baseline_sha,
                    validated_paths=validated_paths,
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
        if ordinary_coding and verified_green_tip() != publication_sha:
            raise RepositoryError("verified Green commit differs from publication")
        bound_reconciliation = (
            record.intent is not None
            and "candidate_tree" in record.intent.expected_evidence
        )
        try:
            if bound_reconciliation:
                reconciliation_sha = cast(
                    _BoundReconcileRepository, repository
                ).reconcile_default_branch(
                    record.worktree,
                    record.source_branch,
                    publication_sha,
                    expected_default_sha=cast(str, record.main_sha),
                )
            else:
                reconciliation_sha = repository.reconcile_default_branch(
                    record.worktree, record.source_branch, publication_sha
                )
        except ReconciliationTargetChanged:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Current target changed after reconciliation commit.",
                next_action=(
                    "Review the retained Green source and start a fresh "
                    "reconciliation against the current target."
                ),
            )
        if reconciliation_sha is not None:
            if bound_reconciliation:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Current target changed after reconciliation commit.",
                    next_action=(
                        "Review the retained Green source and start a fresh "
                        "reconciliation against the current target."
                    ),
                )
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
                    main_sha=reconciliation_sha,
                    intent=None,
                    publication_sha=None,
                    summary="Current default branch requires reconciliation before publication.",
                    next_action="Resolve the retained merge and rerun the full workflow.",
                )
            )
        already_published = (
            replaying
            and publication_was_bound
            and record.merge_request_iid is not None
            and record.publication_sha is not None
        )
        if remediation:
            _verify_authenticated_user(gitlab)
            if record.merge_request_iid is None:
                raise SegmentError(
                    "remediation publication lacks its exact merge request"
                )
            retained_mr = gitlab.fetch_merge_request(record.merge_request_iid)
            already_published = (
                publication_was_bound
                and record.publication_sha is not None
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
        elif already_published:
            if record.merge_request_iid is None:
                raise SegmentError("retained ordinary merge request IID is missing")
            retained_mr = gitlab.fetch_merge_request(record.merge_request_iid)
            if not _mr_evidence_is_exact(
                record,
                retained_mr,
                gitlab=gitlab,
                repository=repository,
                source_sha=publication_sha,
            ):
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Retained ordinary merge request evidence is not exact.",
                )
        if not remediation and not already_published:
            try:
                competing_owner = _open_closing_issue_owners(
                    gitlab,
                    project_id=gitlab.verify_project().id,
                    default_branch=repository.config.default_branch,
                    issue_iids=(record.issue_iid,),
                ).get(record.issue_iid)
            except GitLabError:
                return _record_retry(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    now=now,
                    error_code=ErrorCode.GITLAB_RETRYABLE,
                    detail="pre-publication closing-MR ownership check",
                )
            if competing_owner is not None:
                return _block_publication_identity(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary=(
                        f"Open merge request !{competing_owner} now closes this issue; "
                        "local work was retained."
                    ),
                    error_code=ErrorCode.EXTERNAL_OWNER,
                    next_action=(
                        f"Review merge request !{competing_owner} and the retained "
                        "worktree before deciding whether to resume this issue."
                    ),
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
        if remediation and repository.has_changes(record.worktree):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Published feedback worktree is not clean.",
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
    except PublicationCommitRejected as error:
        correction_state = (
            RunnerState.REPAIR
            if record.repair_attempt is not None
            else RunnerState.EXECUTING
        )
        checkpoint = (
            load_red_checkpoint(
                record.project_id, record.issue_iid, record.source_sha or ""
            )
            if record.workflow_lane is WorkflowLane.CODING
            and load_red_checkpoint is not None
            else None
        )
        try:
            correction_identity = build_publication_correction_identity(
                record,
                repository.observe_legacy_publication_candidate(record.worktree),
                checkpoint=checkpoint,
            )
        except (RepositoryError, SegmentError):
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Publication correction identity could not be captured.",
            )
        return ledger.persist(
            transition_record(
                record,
                correction_state,
                phase=WorkflowPhase.WORKFLOW,
                intent=None,
                publication_sha=None,
                merge_request_draft=None,
                worker_exit=None,
                correction_identity=correction_identity,
                retry_at=None,
                error_code=ErrorCode.PUBLICATION_COMMIT_REJECTED,
                summary=error.failure.diagnostic,
                next_action=(
                    "Correct the retained change, then rerun the complete workflow "
                    "and validation before publication."
                ),
            )
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
    if remediation:
        try:
            closes_issue = gitlab.mr_closes_issue(mr.iid, record.issue_iid)
        except GitLabError:
            return _record_retry(
                record,
                ledger=ledger,
                gitlab=gitlab,
                now=now,
                error_code=ErrorCode.GITLAB_RETRYABLE,
            )
        if not closes_issue:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Feedback publication merge request does not close its issue.",
            )
    else:
        return _reconcile_new_publication(
            record,
            mr=mr,
            sha=sha,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
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


def _reconcile_new_publication(
    record: LedgerRecord,
    *,
    mr: MergeRequestEvidence,
    sha: str,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
) -> LedgerRecord:
    """Bind and re-read a new MR without replaying its publication transport."""
    bound = record
    if record.merge_request_iid != mr.iid or record.merge_request_url != mr.web_url:
        bound = ledger.persist(
            record.model_copy(
                update={
                    "merge_request_iid": mr.iid,
                    "merge_request_url": mr.web_url,
                    "source_sha": sha,
                }
            )
        )
    try:
        reconciled_mr = mr
        closes_issue = gitlab.mr_closes_issue(mr.iid, bound.issue_iid)
        for _ in range(3):
            if closes_issue:
                break
            reconciled_mr = gitlab.fetch_merge_request(bound.merge_request_iid or 0)
            if not _mr_publication_identity_is_exact(
                bound,
                reconciled_mr,
                repository=repository,
                source_sha=sha,
            ):
                return _block_publication_identity(
                    bound,
                    ledger=ledger,
                    gitlab=gitlab,
                    summary="Post-publication merge request evidence is not exact.",
                )
            closes_issue = gitlab.mr_closes_issue(reconciled_mr.iid, bound.issue_iid)
    except GitLabIdentityError as error:
        return _block_identity_drift(bound, ledger=ledger, summary=str(error))
    except GitLabError:
        return _record_retry(
            bound,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
            detail="post-publication check: closing relationship",
        )
    if not closes_issue:
        return ledger.persist(
            transition_record(
                bound,
                RunnerState.OBSERVING,
                phase=WorkflowPhase.PIPELINE_WAIT,
                source_sha=sha,
                intent=None,
                summary="Merge request is published; its closing projection is pending.",
                next_action="Reconcile the exact merge request before observing its pipeline.",
            )
        )
    published = ledger.persist(
        transition_record(
            bound,
            RunnerState.OBSERVING,
            phase=WorkflowPhase.PIPELINE_WAIT,
            merge_request_iid=reconciled_mr.iid,
            merge_request_url=reconciled_mr.web_url,
            source_sha=sha,
            intent=None,
            summary="Merge request published and verified.",
            next_action="Observe the exact source-SHA pipeline.",
        )
    )
    return _observe(
        published,
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        now=now,
        merge_request=reconciled_mr,
        closing_evidence=True,
    )


def _observe(
    record: LedgerRecord,
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    now: datetime,
    merge_request: MergeRequestEvidence | None = None,
    closing_evidence: bool | None = None,
) -> LedgerRecord:
    if record.merge_request_iid is None or record.source_sha is None:
        raise SegmentError("observation record lacks merge request evidence")
    try:
        _verify_authenticated_user(gitlab)
        mr = merge_request or gitlab.fetch_merge_request(record.merge_request_iid)
        if mr.state == "merged":
            refreshed_issue = gitlab.fetch_issue(record.issue_iid)
            if refreshed_issue.state != "opened":
                published = _reconcile_closed_published_issue(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    repository=repository,
                    now=now,
                )
                if published is not None:
                    return published
        exact = _mr_publication_identity_is_exact(
            record, mr, repository=repository, source_sha=record.source_sha
        )
        closes_issue = exact and (
            closing_evidence
            if closing_evidence is not None
            else gitlab.mr_closes_issue(mr.iid, record.issue_iid)
        )
        if exact and closes_issue and record.intent is not None:
            record = ledger.persist(record.model_copy(update={"intent": None}))
        if exact and closes_issue and mr.has_conflicts:
            return ledger.persist(
                transition_record(
                    record,
                    RunnerState.REPAIR,
                    phase=WorkflowPhase.CODING_GREEN,
                    work_kind=WorkKind.MERGE_CONFLICT_REPAIR,
                    error_code=ErrorCode.MERGE_CONFLICT,
                    summary="The exact merge request has conflicts.",
                    next_action="Run the bounded merge-conflict repair segment.",
                )
            )
        if record.merge_request_iid is None or record.source_sha is None:
            raise SegmentError("observation record lacks merge request evidence")
        pipeline = (
            gitlab.latest_pipeline_for_sha(record.merge_request_iid, record.source_sha)
            if exact and closes_issue
            else None
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
    if not closes_issue:
        return record
    if pipeline is not None and pipeline.sha != record.source_sha:
        return _block_publication_identity(
            record,
            ledger=ledger,
            gitlab=gitlab,
            summary="Observed pipeline source SHA does not match the publication.",
        )
    if pipeline is None or pipeline.status in NONTERMINAL_PIPELINE_STATUSES:
        if (
            record.intent is None
            and record.summary
            != "Merge request is published; its closing projection is pending."
        ):
            return record
        return ledger.persist(
            record.model_copy(
                update={
                    "intent": None,
                    "summary": "Merge request closing projection is verified; pipeline is pending.",
                    "next_action": "Observe the exact source-SHA pipeline.",
                }
            )
        )
    if pipeline.status == "failed":
        try:
            diagnostics = collect_exact_failed_job_diagnostics(
                jobs=gitlab.list_pipeline_jobs(pipeline.id, pipeline.sha),
                pipeline_id=pipeline.id,
                source_sha=pipeline.sha,
                fetch_trace=lambda job_id: gitlab.fetch_job_trace(pipeline.id, job_id),
            )
        except (GitLabError, SegmentError):
            return record_failed_job_diagnostic_unavailable(
                record, ledger=ledger, gitlab=gitlab
            )
        return activate_exact_pipeline_repair(
            record,
            merge_request=mr,
            pipeline=pipeline,
            failed_job=diagnostics[0][0],
            diagnostic=diagnostics[0][1],
            additional_job_diagnostics=diagnostics[1:],
            ledger=ledger,
            gitlab=gitlab,
            now=now,
        )
    if pipeline.status != "success":
        needs_repair = ledger.persist(
            transition_record(
                record,
                RunnerState.NEEDS_REPAIR,
                phase=WorkflowPhase.POST_PUBLICATION,
                error_code=ErrorCode.PIPELINE_REJECTED,
                summary="The exact source-SHA pipeline ended without success.",
                next_action="Inspect the terminal result before explicit recovery.",
            )
        )
        gitlab.transition_labels(
            needs_repair.issue_iid, label_for_state(needs_repair.state)
        )
        return needs_repair
    try:
        jobs_succeeded = _required_pipeline_jobs_succeeded(
            gitlab.list_pipeline_jobs(pipeline.id, pipeline.sha),
            pipeline_id=pipeline.id,
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
    except SegmentError as error:
        return _block_publication_identity(
            record, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    if jobs_succeeded is not True:
        missing_required = jobs_succeeded is None
        if record.retry_count >= 3:
            needs_repair = ledger.persist(
                transition_record(
                    record,
                    RunnerState.NEEDS_REPAIR,
                    phase=WorkflowPhase.POST_PUBLICATION,
                    error_code=ErrorCode.PIPELINE_REJECTED,
                    summary=(
                        "The exact pipeline lacks current required job evidence."
                        if missing_required
                        else "A current required job has not succeeded in the exact pipeline."
                    ),
                    next_action=(
                        "Restore a complete exact job inventory for recovery."
                        if missing_required
                        else "Inspect the contradictory required-job result before explicit recovery."
                    ),
                )
            )
            gitlab.transition_labels(
                needs_repair.issue_iid, label_for_state(needs_repair.state)
            )
            return needs_repair
        return ledger.persist(
            record.model_copy(
                update={
                    "retry_count": record.retry_count + 1,
                    "summary": (
                        "The exact pipeline has no current required job proof."
                        if missing_required
                        else "A current required job has not yet succeeded in the exact pipeline."
                    ),
                    "next_action": "Reobserve its complete current job inventory.",
                }
            )
        )
    if record.pending_feedback is not None:
        if record.pending_feedback.outcome is None:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Published feedback lacks a validated outcome.",
            )
        try:
            retained_changes = repository.has_changes(record.worktree)
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
        if retained_changes:
            repair = ledger.persist(
                transition_record(
                    record,
                    RunnerState.REPAIR,
                    phase=WorkflowPhase.WORKFLOW,
                    pending_feedback=record.pending_feedback.model_copy(
                        update={"acknowledgement_ready": False}
                    ),
                    summary="Feedback worktree changed after publication.",
                    next_action="Validate and publish the retained feedback changes.",
                )
            )
            gitlab.transition_labels(repair.issue_iid, label_for_state(repair.state))
            return repair
        return _acknowledge_feedback(
            record.model_copy(
                update={
                    "pending_feedback": record.pending_feedback.model_copy(
                        update={"acknowledgement_ready": True}
                    )
                }
            ),
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
    awaiting = ledger.persist(
        transition_record(
            record,
            RunnerState.AWAITING_HUMAN,
            phase=WorkflowPhase.PIPELINE_WAIT,
            error_code=None,
            summary="The exact source-SHA pipeline and required jobs succeeded.",
            next_action="Await human review and merge.",
        )
    )
    try:
        _ensure_awaiting_handoff(
            awaiting, gitlab=gitlab, repository=repository, merge_request=mr
        )
    except GitLabError:
        return _record_retry(
            awaiting,
            ledger=ledger,
            gitlab=gitlab,
            now=now,
            error_code=ErrorCode.GITLAB_RETRYABLE,
        )
    except RepositoryError as error:
        return _block_publication_identity(
            awaiting, ledger=ledger, gitlab=gitlab, summary=str(error)
        )
    gitlab.transition_labels(awaiting.issue_iid, label_for_state(awaiting.state))
    return awaiting


def run_segment(
    *,
    ledger: LedgerPort,
    gitlab: GitLabPort,
    repository: RepositoryPort,
    invoke: InvokerPort,
    timeout_seconds: int,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    issue_iid: int | None = None,
    allow_pinned_conflict: bool = False,
    remaining_seconds: Callable[[], float] | None = None,
    issue_lock: Callable[[int], AbstractContextManager[None]] | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
    apply_red_checkpoint: (
        Callable[[int, RedCheckpointRequest, str], RedCheckpointEvidence] | None
    ) = None,
    deferred_observer_iids: frozenset[int] = frozenset(),
    observe_interactive_validation: Callable[[], object] = lambda: None,
    on_worker_invoke: Callable[[int], None] = lambda _issue_iid: None,
) -> LedgerRecord | None:
    """Run at most one recoverable segment and return its durable state."""
    record, issue = select_record(
        ledger,
        gitlab,
        now=now,
        issue_iid=issue_iid,
        allow_pinned_conflict=allow_pinned_conflict,
        repository=repository,
        deferred_observer_iids=deferred_observer_iids,
    )
    if record is None or issue is None:
        return None
    if (
        record.state is RunnerState.BLOCKED
        and record.prior_state is RunnerState.AWAITING_HUMAN
        and record.merge_request_iid is None
        and record.blocked_cause is not None
    ):
        return record
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
        ErrorCode.TIMING_ADMISSION_BUSY,
        ErrorCode.AGENT_CAPACITY_UNAVAILABLE,
    }
    reexecute_due_worker_retry = record.state is RunnerState.RETRY_WAIT and (
        record.error_code in retryable_worker_errors
        or (
            record.error_code is ErrorCode.GITLAB_RETRYABLE
            and record.prior_state is RunnerState.EXECUTING
            and record.prior_phase is WorkflowPhase.WORKFLOW
        )
    )
    resumed_from_retry = record.state is RunnerState.RETRY_WAIT and _due(record, now)
    unsafe_due_protocol_retry_reason = None
    if (
        resumed_from_retry
        and record.error_code is ErrorCode.WORKER_PROTOCOL_INVALID
        and record.prior_state is RunnerState.EXECUTING
        and record.prior_phase is WorkflowPhase.WORKFLOW
    ):
        unsafe_due_protocol_retry_reason = _unsafe_due_protocol_retry_reason(
            record,
            repository=repository,
            load_red_checkpoint=load_red_checkpoint,
        )
    if resumed_from_retry and unsafe_due_protocol_retry_reason is None:
        record = _resume_due_retry(record, ledger=ledger)
    if record.state is RunnerState.RETRY_WAIT and not _due(record, now):
        return None
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
    if unsafe_due_protocol_retry_reason is not None:
        return _block_unsafe_due_protocol_retry(
            record,
            ledger=ledger,
            gitlab=gitlab,
            detail=unsafe_due_protocol_retry_reason,
        )
    if (
        record.pending_feedback is not None
        and record.pending_feedback.acknowledgement_ready
        and not (
            record.state is RunnerState.OBSERVING
            and record.phase is WorkflowPhase.PIPELINE_WAIT
        )
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
        published = _reconcile_closed_published_issue(
            record,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
        if published is not None:
            return published
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
    if record.state is RunnerState.BLOCKED and set(issue.labels).intersection(
        LIFECYCLE_LABELS
    ) == {label_for_state(record.state)}:
        return record
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
    if (
        record.state is RunnerState.EXECUTING
        and record.phase is WorkflowPhase.POST_PUBLICATION
    ):
        if record.merge_request_iid is None:
            return _block_publication_identity(
                record,
                ledger=ledger,
                gitlab=gitlab,
                summary="Retained post-publication record has no exact merge request.",
            )
        record = ledger.persist(
            transition_record(
                record,
                RunnerState.OBSERVING,
                phase=WorkflowPhase.PIPELINE_WAIT,
                summary="Reobserving the retained exact source-SHA pipeline.",
                next_action="Verify current required jobs before human handoff.",
            )
        )
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
    if record.state is RunnerState.AWAITING_HUMAN:
        try:
            _ensure_awaiting_handoff(record, gitlab=gitlab, repository=repository)
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
        expected = label_for_state(record.state)
        label_repaired = set(issue.labels).intersection(LIFECYCLE_LABELS) != {expected}
        if label_repaired:
            gitlab.transition_labels(issue.iid, expected)
        return record
    expected_label = label_for_state(record.state)
    lifecycle = set(LIFECYCLE_LABELS)
    if record.revision > 0 and set(issue.labels).intersection(lifecycle) != (
        {expected_label} if expected_label else set()
    ):
        issue = gitlab.transition_labels(issue.iid, expected_label)
        if _is_published_completion_candidate(record):
            refreshed_issue = gitlab.fetch_issue(record.issue_iid)
            if refreshed_issue.state != "opened":
                published = _reconcile_closed_published_issue(
                    record,
                    ledger=ledger,
                    gitlab=gitlab,
                    repository=repository,
                    now=now,
                )
                if published is not None:
                    return published
    if record.state not in ACTIVE_STATES:
        return record
    if resumed_from_retry and not reexecute_due_worker_retry:
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
    boundary_clock = clock or (lambda: now)
    if record.state in {RunnerState.EXECUTING, RunnerState.REPAIR}:
        started_at = boundary_clock()
        outcome = _execute(
            record,
            issue,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            invoke=invoke,
            timeout_seconds=timeout_seconds,
            now=now,
            completion_clock=boundary_clock,
            remaining_seconds=remaining_seconds,
            issue_lock=issue_lock,
            load_red_checkpoint=load_red_checkpoint,
            apply_red_checkpoint=apply_red_checkpoint,
            observe_interactive_validation=observe_interactive_validation,
            on_worker_invoke=on_worker_invoke,
        )
        finished_at = boundary_clock()
        if outcome is None:
            return None
        capacity_wait = (
            record.phase is WorkflowPhase.WORKFLOW
            and outcome.error_code is ErrorCode.AGENT_CAPACITY_UNAVAILABLE
        )
        phase_or_role = "capacity-wait" if capacity_wait else record.phase.value
        if capacity_wait:
            timing_outcome = ErrorCode.AGENT_CAPACITY_UNAVAILABLE.value
        elif outcome.worker_exit is not None:
            timing_outcome = ErrorCode.WORKER_EXIT.value
        elif outcome.error_code is not None:
            timing_outcome = outcome.error_code.value
        else:
            timing_outcome = "complete"
        return ledger.persist(
            outcome.model_copy(
                update={
                    "phase_timings": append_phase_timing(
                        outcome.phase_timings,
                        phase_or_role=phase_or_role,
                        attempt=record.retry_count + 1,
                        started_at=started_at,
                        finished_at=finished_at,
                        outcome=timing_outcome,
                        classification="wait" if capacity_wait else "execution",
                    )
                }
            )
        )
    if record.state is RunnerState.PUBLISHING:
        started_at = boundary_clock()
        outcome = _publish(
            record,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
            load_red_checkpoint=load_red_checkpoint,
        )
        finished_at = boundary_clock()
        timing_outcome = (
            "published"
            if outcome.state is RunnerState.OBSERVING
            else (
                outcome.error_code.value
                if outcome.error_code is not None
                else outcome.state.value
            )
        )
        return ledger.persist(
            outcome.model_copy(
                update={
                    "phase_timings": append_phase_timing(
                        outcome.phase_timings,
                        phase_or_role=record.phase.value,
                        attempt=record.retry_count + 1,
                        started_at=started_at,
                        finished_at=finished_at,
                        outcome=timing_outcome,
                        classification="execution",
                    )
                }
            )
        )
    if record.state is RunnerState.OBSERVING:
        started_at = boundary_clock()
        outcome = _observe(
            record,
            ledger=ledger,
            gitlab=gitlab,
            repository=repository,
            now=now,
        )
        finished_at = boundary_clock()
        if outcome.error_code is not None:
            timing_outcome = outcome.error_code.value
        elif outcome.state is RunnerState.OBSERVING:
            timing_outcome = "pending"
        else:
            timing_outcome = outcome.state.value
        return ledger.persist(
            outcome.model_copy(
                update={
                    "phase_timings": append_phase_timing(
                        outcome.phase_timings,
                        phase_or_role="pipeline-poll",
                        attempt=record.retry_count + 1,
                        started_at=started_at,
                        finished_at=finished_at,
                        outcome=timing_outcome,
                        classification="execution",
                    )
                }
            )
        )
    raise SegmentError("record is not runnable")
