# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Recoverable, opt-in Copier updates for one generated project.

The module deliberately prepares an isolated worktree and a validated workflow
handoff.  Publication is outside this boundary.
"""

from __future__ import annotations

import os
import re
import subprocess
import xml.etree.ElementTree as element_tree
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .domain import WorkflowLane
from .ledger import XdgProjectLock
from .models import MergeRequestDraft
from .repository import CommandResult as TextCommandResult
from .repository import parse_porcelain_paths
from .state_machine import WORKFLOW_RECEIPTS
from .template_comparison import CommandResult
from .worker import AgentInvoker, WorkerProtocolError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_MAX_COMPLETION_BYTES = 65_536
_COPIER_TIMEOUT_SECONDS = 300.0


class TemplateUpdateState(str, Enum):
    """The non-replayable update state machine."""

    PLANNED = "planned"
    PREPARED = "prepared"
    APPLYING = "applying"
    WORKFLOW = "workflow"
    READY = "ready"
    CONFLICT = "conflict"
    NEEDS_RECOVERY = "needs-recovery"


class _BoundaryModel(BaseModel):
    """Strict immutable model for untrusted durable and worker data."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class JUnitArtifact(_BoundaryModel):
    """One immutable supported-version JUnit snapshot."""

    environment: str = Field(pattern=r"^py(311|314)$")
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_count: int = Field(ge=0)
    failures: int = Field(ge=0)
    errors: int = Field(ge=0)
    skips: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        """Require a retained absolute snapshot path."""
        if not value.is_absolute():
            raise ValueError("artifact path must be absolute")
        return value


class TemplateUpdateCompletion(_BoundaryModel):
    """Strict single completion emitted by the bounded generated worker."""

    workflow_lane: WorkflowLane
    workflow_receipts: tuple[str, ...]
    changed_paths: tuple[str, ...]
    conflict_disposition: str
    merge_request_draft: MergeRequestDraft
    audit_receipt: str
    capacity_receipt: str
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    junit_artifacts: tuple[JUnitArtifact, ...] = ()


class TemplateUpdateRecord(_BoundaryModel):
    """Durable identity and state for one deterministic update."""

    schema_version: int = Field(1)
    revision: int = Field(ge=0)
    update_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: TemplateUpdateState
    project_id: int = Field(gt=0)
    source: str = Field(min_length=1)
    current_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    checkout: Path
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    branch: str = Field(pattern=r"^agent/template-update-[A-Za-z0-9-]+$")
    worktree: Path
    changed_paths: tuple[str, ...] = ()
    conflict_paths: tuple[str, ...] = ()
    recovery_stage: str | None = None
    completion: TemplateUpdateCompletion | None = None


@dataclass(frozen=True)
class TemplateUpdateProject:
    """Configured project identity needed for an update."""

    project_id: int
    checkout: Path
    default_branch: str
    state_root: Path


@dataclass(frozen=True)
class TemplateUpdateCheckout:
    """Read-only default-branch observation before any effect."""

    branch: str
    head: str
    clean: bool


@dataclass(frozen=True)
class TemplateUpdateWorktree:
    """Bounded post-Copier worktree observation."""

    branch: str
    head: str
    changed_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    candidate_tree: str | None


@dataclass(frozen=True)
class TemplateUpdateWorkflowRequest:
    """The only request passed to the generated repository worker."""

    checkout: Path
    worktree: Path
    changed_paths: tuple[str, ...]
    candidate_commit: str


@dataclass(frozen=True)
class TemplateUpdateReport:
    """Stable result of a plan or one bounded apply attempt."""

    outcome: str
    state: TemplateUpdateState
    update_id: str
    current_commit: str
    candidate_commit: str
    branch: str | None
    worktree: Path | None
    changed_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...]
    recovery_stage: str | None
    next_action: str
    completion: TemplateUpdateCompletion | None


class _ProjectPort(Protocol):
    def __call__(self) -> TemplateUpdateProject: ...


@dataclass(frozen=True)
class TemplateUpdateDependencies:
    """Injected side-effect ports; none perform publication."""

    project: _ProjectPort
    observe_checkout: Callable[[], TemplateUpdateCheckout]
    lock: Callable[[], AbstractContextManager[None]]
    load_record: Callable[[str], TemplateUpdateRecord | None]
    save_record: Callable[[TemplateUpdateRecord, TemplateUpdateRecord | None], None]
    create_worktree: Callable[[str, str, Path], Path]
    run_copier: Callable[..., CommandResult]
    observe_worktree: Callable[[Path], TemplateUpdateWorktree]
    invoke_workflow: Callable[[TemplateUpdateWorkflowRequest], bytes]
    read_artifact: Callable[[Path], bytes | None]
    state_root: Path


def _safe_paths(paths: tuple[str, ...]) -> bool:
    """Accept only simple relative paths from bounded Git observations."""
    return all(
        path and not path.startswith(("/", "-")) and ".." not in path.split("/")
        for path in paths
    )


def _identity(
    project: TemplateUpdateProject, comparison: object
) -> tuple[str, str, str]:
    """Validate the comparator identity before any mutation boundary."""
    source = getattr(comparison, "source", "")
    current = getattr(comparison, "current_commit", "")
    candidate = getattr(comparison, "candidate_commit", "")
    if (
        not isinstance(source, str)
        or not source
        or not isinstance(current, str)
        or not isinstance(candidate, str)
    ):
        raise ValueError("comparison identity is invalid")
    if _SHA.fullmatch(current) is None or _SHA.fullmatch(candidate) is None:
        raise ValueError("comparison identity is invalid")
    digest = sha256(
        f"{project.project_id}\0{source}\0{current}\0{candidate}".encode()
    ).hexdigest()
    return source, current, candidate if digest else ""


def _report(
    record: TemplateUpdateRecord, *, outcome: str, next_action: str
) -> TemplateUpdateReport:
    """Render a durable record without exposing unsafe details."""
    return TemplateUpdateReport(
        outcome,
        record.state,
        record.update_id,
        record.current_commit,
        record.candidate_commit,
        record.branch,
        record.worktree,
        record.changed_paths,
        record.conflict_paths,
        record.recovery_stage,
        next_action,
        record.completion,
    )


def _transition(
    record: TemplateUpdateRecord, state: TemplateUpdateState, **updates: object
) -> TemplateUpdateRecord:
    """Create one immutable, monotonic durable transition."""
    return record.model_copy(
        update={"state": state, "revision": record.revision + 1, **updates}
    )


def _completion_is_valid(
    completion: TemplateUpdateCompletion,
    worktree: TemplateUpdateWorktree,
    read_artifact: Callable[[Path], bytes | None],
) -> bool:
    """Verify exact path, receipt, and coding-evidence requirements."""
    if (
        completion.changed_paths != worktree.changed_paths
        or completion.conflict_disposition != "none"
    ):
        return False
    if completion.workflow_lane not in {WorkflowLane.CODING, WorkflowLane.CONTENT}:
        return False
    required_receipts = WORKFLOW_RECEIPTS[completion.workflow_lane]
    if len(completion.workflow_receipts) != len(required_receipts) or any(
        not value.startswith(f"{skill}:") or not value.split(":", 1)[1].strip()
        for value, skill in zip(
            completion.workflow_receipts, required_receipts, strict=True
        )
    ):
        return False
    if (
        not completion.audit_receipt
        or not completion.capacity_receipt
        or not completion.merge_request_draft
    ):
        return False
    if completion.candidate_tree != worktree.candidate_tree:
        return False
    if completion.workflow_lane is WorkflowLane.CONTENT:
        return not completion.junit_artifacts
    if len(completion.junit_artifacts) != 2 or {
        artifact.environment for artifact in completion.junit_artifacts
    } != {
        "py311",
        "py314",
    }:
        return False
    for artifact in completion.junit_artifacts:
        contents = read_artifact(artifact.path)
        if (
            contents is None
            or len(contents) > 1_048_576
            or sha256(contents).hexdigest() != artifact.sha256
        ):
            return False
        try:
            root = element_tree.fromstring(contents)
            tag = root.tag.rsplit("}", 1)[-1]
            if tag not in {"testsuites", "testsuite"}:
                return False
            all_suites = tuple(
                node
                for node in root.iter()
                if node.tag.rsplit("}", 1)[-1] == "testsuite"
            )
            suites = tuple(
                suite
                for suite in all_suites
                if not any(
                    child.tag.rsplit("}", 1)[-1] == "testsuite" for child in suite
                )
            )
            if not suites or any(
                name not in suite.attrib
                for suite in suites
                for name in ("tests", "failures", "errors", "skipped")
            ):
                return False
            counts = tuple(
                sum(int(item.attrib.get(name, "0")) for item in suites)
                for name in ("tests", "failures", "errors", "skipped")
            )
        except (ValueError, element_tree.ParseError):
            return False
        if (
            counts
            != (
                artifact.test_count,
                artifact.failures,
                artifact.errors,
                artifact.skips,
            )
            or counts[0] <= 0
        ):
            return False
        if artifact.failures or artifact.errors or artifact.skips:
            return False
    return True


def _parse_completion(
    raw: bytes,
    worktree: TemplateUpdateWorktree,
    read_artifact: Callable[[Path], bytes | None],
) -> TemplateUpdateCompletion | None:
    """Parse exactly one bounded JSON envelope and validate it fully."""
    if (
        not raw
        or len(raw) > _MAX_COMPLETION_BYTES
        or raw.rstrip() != raw
        or b"\n" in raw
    ):
        return None
    try:
        completion = TemplateUpdateCompletion.model_validate_json(raw)
    except ValidationError:
        return None
    return (
        completion
        if _completion_is_valid(completion, worktree, read_artifact)
        else None
    )


def execute_template_update(
    candidate_revision: str,
    project_root: Path,
    *,
    apply: bool,
    compare: Callable[..., object],
    dependencies: object,
) -> TemplateUpdateReport:
    """Plan safely, or apply one non-replayable isolated Copier update."""
    comparison = compare(project_root / ".copier-answers.yml", candidate_revision)
    resolved_dependencies = cast(TemplateUpdateDependencies, dependencies)
    project = resolved_dependencies.project() if apply else None
    paths = tuple(getattr(comparison, "paths", ()))
    if not _safe_paths(paths):
        raise ValueError("comparison paths are invalid")
    if not apply:
        current = getattr(comparison, "current_commit", "")
        candidate = getattr(comparison, "candidate_commit", "")
        return TemplateUpdateReport(
            "planned",
            TemplateUpdateState.PLANNED,
            "",
            current,
            candidate,
            None,
            None,
            paths,
            (),
            None,
            "review plan",
            None,
        )
    assert project is not None
    source, current, candidate = _identity(project, comparison)
    update_id = sha256(
        f"{project.project_id}\0{source}\0{current}\0{candidate}".encode()
    ).hexdigest()
    destination = (
        project.state_root / "template-updates" / str(project.project_id) / update_id
    )
    branch = f"agent/template-update-{update_id[:16]}"
    record = TemplateUpdateRecord(
        schema_version=1,
        revision=0,
        update_id=update_id,
        state=TemplateUpdateState.PLANNED,
        project_id=project.project_id,
        source=source,
        current_commit=current,
        candidate_commit=candidate,
        checkout=project.checkout,
        base_sha="0" * 40,
        branch=branch,
        worktree=destination,
        changed_paths=paths,
        conflict_paths=(),
        recovery_stage=None,
        completion=None,
    )
    with resolved_dependencies.lock():
        existing = resolved_dependencies.load_record(update_id)
        if existing is not None:
            if (
                existing.project_id != project.project_id
                or existing.source != source
                or existing.current_commit != current
                or existing.candidate_commit != candidate
                or existing.checkout != project.checkout
            ):
                failed = _transition(
                    existing,
                    TemplateUpdateState.NEEDS_RECOVERY,
                    recovery_stage="identity",
                )
                resolved_dependencies.save_record(failed, existing)
                return _report(
                    failed,
                    outcome="needs-recovery",
                    next_action="inspect retained update record",
                )
            return _report(
                existing,
                outcome="ready"
                if existing.state is TemplateUpdateState.READY
                else "needs-recovery",
                next_action="inspect retained update record",
            )
        checkout = resolved_dependencies.observe_checkout()
        if (
            checkout.branch != project.default_branch
            or not checkout.clean
            or _SHA.fullmatch(checkout.head) is None
        ):
            failed = _transition(
                record,
                TemplateUpdateState.NEEDS_RECOVERY,
                base_sha=checkout.head if _SHA.fullmatch(checkout.head) else "0" * 40,
                recovery_stage="checkout",
            )
            resolved_dependencies.save_record(failed, None)
            return _report(
                failed,
                outcome="needs-recovery",
                next_action="restore a clean default branch",
            )
        record = record.model_copy(update={"base_sha": checkout.head})
        resolved_dependencies.save_record(record, None)
        prepared = _transition(record, TemplateUpdateState.PREPARED)
        resolved_dependencies.save_record(prepared, record)
        try:
            worktree_path = resolved_dependencies.create_worktree(
                branch, checkout.head, destination
            )
        except (OSError, RuntimeError, ValueError):
            failed = _transition(
                prepared, TemplateUpdateState.NEEDS_RECOVERY, recovery_stage="worktree"
            )
            resolved_dependencies.save_record(failed, prepared)
            return _report(
                failed,
                outcome="needs-recovery",
                next_action="inspect retained update record",
            )
        applying = _transition(
            prepared, TemplateUpdateState.APPLYING, worktree=worktree_path
        )
        resolved_dependencies.save_record(applying, prepared)
        try:
            copier = resolved_dependencies.run_copier(
                (
                    "copier",
                    "update",
                    "--vcs-ref",
                    candidate,
                    "--trust",
                    "--defaults",
                    "--skip-tasks",
                    "--conflict",
                    "rej",
                ),
                cwd=worktree_path,
                timeout_seconds=_COPIER_TIMEOUT_SECONDS,
            )
            observed = resolved_dependencies.observe_worktree(worktree_path)
        except (OSError, RuntimeError, TimeoutError, ValueError):
            failed = _transition(
                applying, TemplateUpdateState.NEEDS_RECOVERY, recovery_stage="copier"
            )
            resolved_dependencies.save_record(failed, applying)
            return _report(
                failed,
                outcome="needs-recovery",
                next_action="inspect retained Copier evidence",
            )
        if copier.returncode != 0 or observed.conflict_paths:
            conflict = _transition(
                applying,
                TemplateUpdateState.CONFLICT,
                changed_paths=observed.changed_paths,
                conflict_paths=observed.conflict_paths,
                recovery_stage="copier",
            )
            resolved_dependencies.save_record(conflict, applying)
            return _report(
                conflict,
                outcome="conflict",
                next_action="resolve retained Copier conflicts",
            )
        if not observed.changed_paths:
            ready = _transition(
                applying, TemplateUpdateState.READY, changed_paths=(), completion=None
            )
            resolved_dependencies.save_record(ready, applying)
            return _report(ready, outcome="ready", next_action="no generated changes")
        workflow = _transition(
            applying, TemplateUpdateState.WORKFLOW, changed_paths=observed.changed_paths
        )
        resolved_dependencies.save_record(workflow, applying)
        try:
            raw = resolved_dependencies.invoke_workflow(
                TemplateUpdateWorkflowRequest(
                    project.checkout, worktree_path, observed.changed_paths, candidate
                )
            )
            final_observation = resolved_dependencies.observe_worktree(worktree_path)
            completion = _parse_completion(
                raw, final_observation, resolved_dependencies.read_artifact
            )
        except (OSError, RuntimeError, TimeoutError, ValueError):
            completion = None
        if completion is None:
            failed = _transition(
                workflow, TemplateUpdateState.NEEDS_RECOVERY, recovery_stage="workflow"
            )
            resolved_dependencies.save_record(failed, workflow)
            return _report(
                failed,
                outcome="needs-recovery",
                next_action="inspect retained workflow evidence",
            )
        ready = _transition(workflow, TemplateUpdateState.READY, completion=completion)
        resolved_dependencies.save_record(ready, workflow)
        return _report(
            ready, outcome="ready", next_action="human publication remains required"
        )


def default_dependencies(
    *,
    project: TemplateUpdateProject,
    run_process: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    agent_invoker: AgentInvoker | None = None,
) -> TemplateUpdateDependencies:
    """Compose conservative local ports at the CLI edge."""
    root = project.state_root
    records = root / "template-updates" / "records"

    def checked(
        *argv: str, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        result = run_process(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("template update Git observation failed")
        return result

    def decode(output: bytes) -> str:
        try:
            return output.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("template update Git output was not UTF-8") from error

    def load(update_id: str) -> TemplateUpdateRecord | None:
        path = records / f"{update_id}.json"
        if not path.exists():
            return None
        try:
            payload = path.read_bytes()
            if len(payload) > _MAX_COMPLETION_BYTES:
                raise ValueError("template update record is too large")
            return TemplateUpdateRecord.model_validate_json(payload)
        except (OSError, ValidationError, ValueError) as error:
            raise RuntimeError("template update record is invalid") from error

    def save(
        record: TemplateUpdateRecord, expected: TemplateUpdateRecord | None
    ) -> None:
        current = load(record.update_id)
        if current != expected:
            raise RuntimeError("template update record changed")
        records.mkdir(parents=True, exist_ok=True, mode=0o700)
        records.chmod(0o700)
        destination = records / f"{record.update_id}.json"
        temporary = records / f".{record.update_id}.{record.revision}.tmp"
        try:
            with temporary.open("xb") as stream:
                temporary.chmod(0o600)
                stream.write(record.model_dump_json().encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
            destination.chmod(0o600)
            directory = records.open()
            try:
                os.fsync(directory.fileno())
            finally:
                directory.close()
        finally:
            temporary.unlink(missing_ok=True)

    def checkout() -> TemplateUpdateCheckout:
        status = checked(
            "git", "-C", str(project.checkout), "status", "--porcelain"
        ).stdout
        branch = decode(
            checked(
                "git", "-C", str(project.checkout), "branch", "--show-current"
            ).stdout
        )
        head = decode(
            checked("git", "-C", str(project.checkout), "rev-parse", "HEAD").stdout
        )
        return TemplateUpdateCheckout(branch, head, not status)

    def create(branch: str, base: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = run_process(
            (
                "git",
                "-C",
                str(project.checkout),
                "worktree",
                "add",
                "-b",
                branch,
                str(destination),
                base,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode:
            raise RuntimeError("worktree creation failed")
        return destination

    def copier(
        argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float
    ) -> CommandResult:
        result = run_process(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        return CommandResult(result.returncode, result.stdout, result.stderr)

    def observe(path: Path) -> TemplateUpdateWorktree:
        status = checked("git", "-C", str(path), "status", "--porcelain", "-z").stdout
        names = tuple(sorted(set(parse_porcelain_paths(decode(status)))))
        conflicts = tuple(name for name in names if name.endswith(".rej"))
        if names:
            stage = checked("git", "-C", str(path), "add", "--all")
            if stage.stdout or stage.stderr:
                raise RuntimeError("template update staging emitted unexpected output")
        head = decode(checked("git", "-C", str(path), "rev-parse", "HEAD").stdout)
        tree = (
            decode(checked("git", "-C", str(path), "write-tree").stdout)
            if names
            else None
        )
        return TemplateUpdateWorktree("", head, names, conflicts, tree)

    def worker(request: TemplateUpdateWorkflowRequest) -> bytes:
        prompt = "Read AGENTS.md, classify the actual diff, complete the applicable workflow, and do not commit, push, create an MR, approve, or merge. Return one completion JSON object."

        def command_runner(
            command: tuple[str, ...],
            *,
            stdin: str | None = None,
            timeout_seconds: float | None = None,
            environment: Mapping[str, str] | None = None,
            current_directory: Path | None = None,
        ) -> TextCommandResult:
            result = run_process(
                command,
                input=stdin,
                cwd=current_directory,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
                text=True,
            )
            return TextCommandResult(
                result.returncode, cast(str, result.stdout), cast(str, result.stderr)
            )

        invoker = agent_invoker or AgentInvoker(command_runner, environment=os.environ)
        invoker.preflight()
        result = invoker.invoke(request.worktree, prompt, int(_COPIER_TIMEOUT_SECONDS))
        if result.returncode != 0 or not result.stdout:
            raise WorkerProtocolError("generated workflow failed")
        return result.stdout.encode("utf-8")

    return TemplateUpdateDependencies(
        lambda: project,
        checkout,
        lambda: XdgProjectLock(root / "project.lock"),
        load,
        save,
        create,
        copier,
        observe,
        worker,
        lambda path: path.read_bytes() if path.exists() else None,
        root,
    )
