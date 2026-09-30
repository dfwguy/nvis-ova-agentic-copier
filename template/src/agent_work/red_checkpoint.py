# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable, Git-native checkpoints for verified failing tests."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from .red_preflight import parse_red_preflight_success

if TYPE_CHECKING:
    from .models import LedgerRecord
    from .ports import CommandResult


class RedCheckpointError(RuntimeError):
    """A Red checkpoint could not be proven or retained safely."""


class _CheckpointModel(BaseModel):
    """Strict immutable model for a checkpoint boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RedCheckpointRequest(_CheckpointModel):
    """Bounded worker-supplied evidence used to verify one Red run."""

    argv: tuple[str, ...]
    expected_failure: str
    test_paths: tuple[str, ...]


class _ValidatedRedCheckpointRequest(_CheckpointModel):
    """Validated projection kept behind the transaction error boundary."""

    argv: tuple[str, ...]
    expected_failure: str
    test_paths: tuple[str, ...]

    @field_validator("argv")
    @classmethod
    def validate_argv_entries(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require a bounded, shell-free pytest argument vector."""
        if value[:3] != ("uv", "run", "pytest"):
            raise ValueError("argv must begin with exact uv run pytest")
        if not 4 <= len(value) <= 128:
            raise ValueError("argv must contain between 4 and 128 entries")
        if any(
            not item or len(item.encode()) > 4_096 or "\x00" in item for item in value
        ):
            raise ValueError("argv entries must be bounded nonempty strings")
        return value

    @field_validator("expected_failure")
    @classmethod
    def validate_expected_failure(cls, value: str) -> str:
        """Reject blank or control-bearing failure markers."""
        if not value.strip() or "\x00" in value or len(value) > 2_048:
            raise ValueError("expected failure marker is invalid or too long")
        return value

    @field_validator("test_paths")
    @classmethod
    def validate_test_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require unique normalized repository-relative Python tests."""
        if not 1 <= len(value) <= 64:
            raise ValueError("test paths must contain between 1 and 64 entries")
        if len(set(value)) != len(value) or tuple(sorted(value)) != value:
            raise ValueError("test paths must be unique and normalized")
        for item in value:
            path = PurePosixPath(item)
            if (
                not item.startswith("tests/")
                or not item.endswith(".py")
                or path.is_absolute()
                or path.as_posix() != item
                or ".." in path.parts
                or len(item.encode()) > 512
            ):
                raise ValueError("test paths must be normalized tests/*.py paths")
        return value

    @model_validator(mode="after")
    def validate_argv_paths(self) -> _ValidatedRedCheckpointRequest:
        """Require every declared test to be represented by a pytest target."""
        targets = self.argv[3:]
        if any(
            not any(
                target == path or target.startswith(f"{path}::") for target in targets
            )
            for path in self.test_paths
        ):
            raise ValueError("argv must represent every declared test path")
        return self


class RedCheckpointEvidence(_CheckpointModel):
    """Bounded retained identity for one verified failing-test snapshot."""

    schema_version: Literal[1] = 1
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    source_branch: str = Field(min_length=1, max_length=255)
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree_oid: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str = Field(min_length=1, max_length=512)
    test_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    argv: tuple[str, ...] = Field(min_length=4, max_length=128)
    exit_code: int
    expected_failure: str = Field(min_length=1, max_length=2_048)
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    residual_details: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def validate_identity(self) -> RedCheckpointEvidence:
        """Keep the durable identity internally coherent and bounded."""
        request = _ValidatedRedCheckpointRequest(
            argv=self.argv,
            expected_failure=self.expected_failure,
            test_paths=self.test_paths,
        )
        del request
        expected_ref = checkpoint_ref(self.issue_iid, self.base_sha)
        if self.ref != expected_ref:
            raise ValueError("checkpoint ref does not match issue and base")
        if self.exit_code == 0:
            raise ValueError("checkpoint exit code must be nonzero")
        if len(self.model_dump_json().encode()) + 1 > 32_768:
            raise ValueError("checkpoint evidence exceeds the durable bound")
        return self


class _LoadRecord(Protocol):
    def __call__(self, issue_iid: int) -> LedgerRecord | None: ...


@dataclass(frozen=True)
class RedCheckpointDependencies:
    """Injected trusted boundaries for one checkpoint transaction."""

    load_record: _LoadRecord
    current_head: Callable[[Path], str]
    changed_paths: Callable[[Path], tuple[str, ...]]
    run_red_command: Callable[[Path, tuple[str, ...]], CommandResult]
    snapshot_test_tree: Callable[[str, tuple[str, ...]], str]
    expected_tree_oid: str
    read_evidence: Callable[[int, int, str], RedCheckpointEvidence | None]
    write_evidence: Callable[[RedCheckpointEvidence], None]
    run_red_preflight: Callable[[Path, tuple[str, ...], str], CommandResult] | None = (
        None
    )


def checkpoint_ref(issue_iid: int, base_sha: str) -> str:
    """Return the only local-ref identity allowed for one issue and base."""
    return f"refs/agent-work/red-checkpoints/{issue_iid}/{base_sha}"


def validate_red_checkpoint_request(
    request: RedCheckpointRequest,
) -> RedCheckpointRequest:
    """Validate untrusted request fields behind a sanitized error boundary."""
    try:
        validated = _ValidatedRedCheckpointRequest.model_validate(request.model_dump())
    except ValidationError as error:
        message = str(error.errors(include_url=False)[0]["msg"])
        raise RedCheckpointError(message.removeprefix("Value error, ")) from error
    return RedCheckpointRequest.model_validate(validated.model_dump())


def _validate_record(record: LedgerRecord | None, issue_iid: int) -> LedgerRecord:
    if (
        record is None
        or record.issue_iid != issue_iid
        or record.state is not RunnerState.EXECUTING
        or record.phase is not WorkflowPhase.WORKFLOW
        or record.workflow_lane is not WorkflowLane.UNKNOWN
        or record.work_kind is not WorkKind.ORDINARY
        or record.source_sha is None
    ):
        raise RedCheckpointError(
            "red checkpoint requires the matching executing workflow record"
        )
    return record


def _matches(
    evidence: RedCheckpointEvidence,
    *,
    record: LedgerRecord,
    request: RedCheckpointRequest,
) -> bool:
    base_sha = record.source_sha
    if base_sha is None:
        return False
    return (
        evidence.project_id == record.project_id
        and evidence.issue_iid == record.issue_iid
        and evidence.source_branch == record.source_branch
        and evidence.base_sha == base_sha
        and evidence.ref == checkpoint_ref(record.issue_iid, base_sha)
        and evidence.test_paths == request.test_paths
        and evidence.argv == request.argv
        and evidence.expected_failure == request.expected_failure
    )


def create_red_checkpoint(
    *,
    issue_iid: int,
    request: RedCheckpointRequest,
    dependencies: RedCheckpointDependencies,
) -> RedCheckpointEvidence:
    """Verify and retain one idempotent Red checkpoint transaction."""
    request = validate_red_checkpoint_request(request)
    record = _validate_record(dependencies.load_record(issue_iid), issue_iid)
    assert record.source_sha is not None
    if dependencies.current_head(record.worktree) != record.source_sha:
        raise RedCheckpointError("worktree head does not match the recorded base")
    observed_paths = tuple(sorted(dependencies.changed_paths(record.worktree)))
    if observed_paths != request.test_paths:
        raise RedCheckpointError("changed test paths do not match declared test paths")
    retained = dependencies.read_evidence(
        record.project_id, record.issue_iid, record.source_sha
    )
    if retained is not None:
        if not _matches(retained, record=record, request=request):
            raise RedCheckpointError(
                "retained red checkpoint conflicts with this request"
            )
        tree_oid = dependencies.snapshot_test_tree(
            record.source_sha, request.test_paths
        )
        if tree_oid != dependencies.expected_tree_oid:
            raise RedCheckpointError("red checkpoint candidate tree changed")
        if tree_oid != retained.tree_oid:
            raise RedCheckpointError("retained red checkpoint conflicts with local ref")
        return retained
    result = dependencies.run_red_command(record.worktree, request.argv)
    if dependencies.current_head(record.worktree) != record.source_sha:
        raise RedCheckpointError("worktree head changed during red checkpoint")
    fresh_paths = tuple(sorted(dependencies.changed_paths(record.worktree)))
    if fresh_paths != request.test_paths:
        raise RedCheckpointError("changed test paths changed during red checkpoint")
    output = f"{result.stdout}{result.stderr}"
    if result.returncode == 0:
        raise RedCheckpointError("red checkpoint command must fail")
    if request.expected_failure not in output:
        raise RedCheckpointError("expected failure marker was not emitted")
    ref = checkpoint_ref(record.issue_iid, record.source_sha)
    try:
        evidence = RedCheckpointEvidence(
            project_id=record.project_id,
            issue_iid=record.issue_iid,
            source_branch=record.source_branch,
            base_sha=record.source_sha,
            tree_oid="0" * 40,
            ref=ref,
            test_paths=request.test_paths,
            argv=request.argv,
            exit_code=result.returncode,
            expected_failure=request.expected_failure,
            output_sha256=hashlib.sha256(output.encode()).hexdigest(),
        )
    except ValidationError as error:
        if "durable bound" in str(error):
            raise RedCheckpointError(
                "red checkpoint evidence exceeds the durable bound"
            ) from error
        raise RedCheckpointError("red checkpoint evidence is invalid") from error
    tree_oid = dependencies.snapshot_test_tree(record.source_sha, request.test_paths)
    if tree_oid != dependencies.expected_tree_oid:
        raise RedCheckpointError("red checkpoint candidate tree changed")
    if dependencies.run_red_preflight is None:
        raise RedCheckpointError("red checkpoint preflight boundary is unavailable")
    preflight = dependencies.run_red_preflight(
        record.worktree, request.test_paths, tree_oid
    )
    details = (
        parse_red_preflight_success(preflight.stdout)
        if preflight.returncode == 0 and not preflight.stderr
        else None
    )
    if details is None:
        raise RedCheckpointError("red checkpoint preflight rejected candidate tests")
    if (
        dependencies.current_head(record.worktree) != record.source_sha
        or tuple(sorted(dependencies.changed_paths(record.worktree)))
        != request.test_paths
        or dependencies.snapshot_test_tree(record.source_sha, request.test_paths)
        != tree_oid
    ):
        raise RedCheckpointError("red checkpoint candidate changed during preflight")
    try:
        evidence = RedCheckpointEvidence.model_validate(
            evidence.model_dump() | {"tree_oid": tree_oid, "residual_details": details}
        )
    except ValidationError as error:
        raise RedCheckpointError("red checkpoint snapshot tree is invalid") from error
    dependencies.write_evidence(evidence)
    return evidence


class RedCheckpointStore:
    """Read-only legacy sidecar compatibility for retained v1 checkpoints."""

    def __init__(self, state_root: Path) -> None:
        self._root = state_root / "red-checkpoints"

    def _path(self, project_id: int, issue_iid: int, base_sha: str) -> Path:
        return self._root / str(project_id) / str(issue_iid) / f"{base_sha}.json"

    def read(
        self, project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None:
        """Load one exact bounded record, failing closed on malformed bytes."""
        path = self._path(project_id, issue_iid, base_sha)
        try:
            with path.open("rb") as handle:
                raw = handle.read(32_769)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RedCheckpointError(
                "red checkpoint evidence could not be read"
            ) from error
        if len(raw) > 32_768:
            raise RedCheckpointError(
                "red checkpoint evidence exceeds the durable bound"
            )
        try:
            evidence = RedCheckpointEvidence.model_validate_json(raw)
        except ValueError as error:
            raise RedCheckpointError("red checkpoint evidence is malformed") from error
        if (
            evidence.project_id != project_id
            or evidence.issue_iid != issue_iid
            or evidence.base_sha != base_sha
        ):
            raise RedCheckpointError("red checkpoint evidence identity is invalid")
        return evidence
