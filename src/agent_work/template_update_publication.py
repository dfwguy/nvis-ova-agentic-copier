# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Trusted, replayable publication boundary for one prepared template update."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gitlab import GitLabMergeRequestPublisher
from .ledger import XdgProjectLock
from .models import AgentWorkConfig
from .ports import CommandRunner
from .repository import (
    CommandResult,
    build_git_environment,
    discover_config,
    run_subprocess,
)
from .template_update import (
    TemplateUpdateCheckout,
    TemplateUpdateDependencies,
    TemplateUpdateProject,
    TemplateUpdateRecord,
    TemplateUpdateReport,
    TemplateUpdateState,
    TemplateUpdateWorktree,
)
from .validation import (
    FilesystemJUnitSnapshotStore,
    JudgeValidationDependencies,
    JudgeValidationError,
    run_judge_validation,
)

_REVIEW_TITLE = "Update generated project from template"


def _review_description(
    current_revision: str, candidate_revision: str, validated_tree: str, sha: str
) -> str:
    """Render the controller-owned initial review text."""
    return (
        "Review the validated generated template update.\n\n"
        f"Current template revision: `{current_revision}`.\n"
        f"Candidate template revision: `{candidate_revision}`.\n"
        f"Validated tree: `{validated_tree}`.\n"
        "Validation: the installed full hook passed with fresh py311 and "
        "py314 JUnit results.\n"
        f"Published commit: `{sha}`.\n\n"
        "Only a human may approve or merge."
    )


class PublicationIntent(BaseModel):
    """Controller-owned identity saved before publication side effects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    update_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: int = Field(gt=0)
    revision: int = Field(ge=0)
    branch: str
    target_branch: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    changed_paths: tuple[str, ...]
    publication_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    merge_request_iid: int | None = Field(default=None, gt=0)


@dataclass(frozen=True)
class TemplateUpdatePublicationReport:
    """Small human-facing result, with MR identity only after exact readback."""

    outcome: str
    state: str
    next_action: str
    merge_request_iid: int | None = None
    merge_request_url: str | None = None


class _Dependencies(Protocol):
    def project(self) -> TemplateUpdateProject: ...

    def lock(self) -> AbstractContextManager[None]: ...

    def load_record(self, update_id: str) -> TemplateUpdateRecord | None: ...

    def observe_checkout(self) -> TemplateUpdateCheckout: ...

    def observe_worktree(self, path: Path) -> TemplateUpdateWorktree: ...

    def validate_tree(self, path: Path, tree: str) -> object: ...

    def load_intent(self, update_id: str) -> PublicationIntent | None: ...

    def save_intent(self, intent: PublicationIntent) -> None: ...

    def commit(self, path: Path, branch: str, tree: str) -> str: ...

    def observe_commit(self, path: Path, sha: str) -> object: ...

    def push(self, path: Path, branch: str, sha: str) -> None: ...

    def observe_remote_sha(self, path: Path, branch: str) -> str | None: ...

    def verify_publication_target(
        self, project: TemplateUpdateProject, path: Path, base_sha: str
    ) -> bool: ...

    def publish_merge_request(
        self,
        project_id: int,
        branch: str,
        target_branch: str,
        sha: str,
        current_revision: str,
        candidate_revision: str,
        validated_tree: str,
    ) -> int: ...

    def observe_merge_request(self, project_id: int, iid: int) -> object: ...


def _recovery() -> TemplateUpdatePublicationReport:
    return TemplateUpdatePublicationReport(
        "needs-recovery", "needs-recovery", "inspect retained template update evidence"
    )


def _ready_identity(
    report: TemplateUpdateReport,
    record: TemplateUpdateRecord,
    project: TemplateUpdateProject,
    checkout: TemplateUpdateCheckout,
    worktree: TemplateUpdateWorktree,
    intent: PublicationIntent | None,
) -> bool:
    """Compare fresh controller observations with durable update identity."""
    completion = record.completion
    return bool(
        report.outcome == "ready"
        and report.state is TemplateUpdateState.READY
        and record.state is TemplateUpdateState.READY
        and completion is not None
        and report.update_id == record.update_id
        and report.current_commit == record.current_commit
        and report.candidate_commit == record.candidate_commit
        and report.branch == record.branch
        and report.worktree == record.worktree
        and report.changed_paths == record.changed_paths
        and report.completion == completion
        and project.project_id == record.project_id
        and project.checkout == record.checkout
        and project.default_branch == checkout.branch
        and checkout.clean
        and checkout.head == record.base_sha
        and worktree.branch == record.branch
        and (
            (
                worktree.head == record.base_sha
                and worktree.changed_paths == completion.changed_paths
            )
            or (
                intent is not None
                and worktree.head == (intent.publication_sha or worktree.head)
                and worktree.head != record.base_sha
                and not worktree.changed_paths
            )
        )
        and not worktree.conflict_paths
        and (
            worktree.candidate_tree == completion.candidate_tree
            if worktree.head == record.base_sha
            else worktree.candidate_tree is None
        )
        and bool(completion.changed_paths)
    )


def publish_ready_template_update(
    report: TemplateUpdateReport, *, dependencies: _Dependencies
) -> TemplateUpdatePublicationReport:
    """Publish only the freshly observed and controller-validated exact tree."""
    if report.state is not TemplateUpdateState.READY or not report.changed_paths:
        return _recovery()
    try:
        with dependencies.lock():
            project = dependencies.project()
            record = dependencies.load_record(report.update_id)
            if record is None or record.worktree is None:
                return _recovery()
            intent = dependencies.load_intent(record.update_id)
            checkout = dependencies.observe_checkout()
            worktree = dependencies.observe_worktree(record.worktree)
            if not _ready_identity(report, record, project, checkout, worktree, intent):
                return _recovery()
            assert record.completion is not None
            tree = record.completion.candidate_tree
            expected = PublicationIntent(
                update_id=record.update_id,
                project_id=record.project_id,
                revision=record.revision,
                branch=record.branch,
                target_branch=project.default_branch,
                base_sha=record.base_sha,
                candidate_tree=tree,
                changed_paths=record.completion.changed_paths,
            )
            if (
                intent is not None
                and intent.model_copy(
                    update={"publication_sha": None, "merge_request_iid": None}
                )
                != expected
            ):
                return _recovery()
            if intent is None:
                validation = dependencies.validate_tree(record.worktree, tree)
                if (
                    getattr(validation, "tree", None) != tree
                    or getattr(validation, "success", None) is not True
                    or set(getattr(validation, "junit_artifacts", ()))
                    != {"py311", "py314"}
                ):
                    return _recovery()
                dependencies.save_intent(expected)
                intent = expected
            sha = intent.publication_sha
            if sha is None:
                sha = (
                    dependencies.commit(record.worktree, record.branch, tree)
                    if worktree.head == record.base_sha
                    else worktree.head
                )
                if (
                    not isinstance(sha, str)
                    or re.fullmatch(r"[0-9a-f]{40}", sha) is None
                ):
                    return _recovery()
                observed = dependencies.observe_commit(record.worktree, sha)
                if (
                    getattr(observed, "tree", None) != tree
                    or getattr(observed, "parent", None) != record.base_sha
                ):
                    return _recovery()
                intent = intent.model_copy(update={"publication_sha": sha})
                dependencies.save_intent(intent)
            else:
                observed = dependencies.observe_commit(record.worktree, sha)
                if (
                    getattr(observed, "tree", None) != tree
                    or getattr(observed, "parent", None) != record.base_sha
                ):
                    return _recovery()
            if not dependencies.verify_publication_target(
                project, record.worktree, record.base_sha
            ):
                return _recovery()
            remote_sha = dependencies.observe_remote_sha(record.worktree, record.branch)
            if remote_sha not in {None, sha}:
                return _recovery()
            if remote_sha is None:
                dependencies.push(record.worktree, record.branch, sha)
                remote_sha = dependencies.observe_remote_sha(
                    record.worktree, record.branch
                )
            if remote_sha != sha:
                return _recovery()
            iid = intent.merge_request_iid
            first_readback = iid is None
            if iid is None:
                iid = dependencies.publish_merge_request(
                    record.project_id,
                    record.branch,
                    project.default_branch,
                    sha,
                    record.current_commit,
                    record.candidate_commit,
                    tree,
                )
                if not isinstance(iid, int) or iid <= 0:
                    return _recovery()
            merge_request = dependencies.observe_merge_request(record.project_id, iid)
            if (
                getattr(merge_request, "iid", None) != iid
                or getattr(merge_request, "source_branch", None) != record.branch
                or getattr(merge_request, "target_branch", None)
                != project.default_branch
                or getattr(merge_request, "sha", None) != sha
                or getattr(merge_request, "state", "opened") != "opened"
                or getattr(merge_request, "has_conflicts", False)
                or getattr(merge_request, "source_project_id", record.project_id)
                != record.project_id
                or getattr(merge_request, "target_project_id", record.project_id)
                != record.project_id
                or not isinstance(getattr(merge_request, "web_url", None), str)
                or not getattr(merge_request, "web_url", None)
                or (
                    first_readback
                    and (
                        getattr(merge_request, "title", None) != _REVIEW_TITLE
                        or getattr(merge_request, "description", None)
                        != _review_description(
                            record.current_commit, record.candidate_commit, tree, sha
                        )
                    )
                )
            ):
                return _recovery()
            if first_readback:
                intent = intent.model_copy(update={"merge_request_iid": iid})
                dependencies.save_intent(intent)
            return TemplateUpdatePublicationReport(
                "published",
                "published",
                "review the merge request",
                iid,
                getattr(merge_request, "web_url", None),
            )
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        subprocess.SubprocessError,
        JudgeValidationError,
    ):
        return _recovery()


@dataclass(frozen=True)
class _ValidationEvidence:
    tree: str
    success: bool
    junit_artifacts: tuple[str, ...]


@dataclass(frozen=True)
class _CommitEvidence:
    tree: str
    parent: str


@dataclass(frozen=True)
class _DefaultDependencies:
    """Controller-owned filesystem, Git, hook, and GitLab ports."""

    config: AgentWorkConfig
    update: TemplateUpdateDependencies
    service_environment: Mapping[str, str]
    run_command: CommandRunner
    run_process: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run

    def project(self) -> TemplateUpdateProject:
        """Return the already verified numeric project and target."""
        return self.update.project()

    def lock(self) -> AbstractContextManager[None]:
        """Use the same project lock as the prepare boundary."""
        return self.update.lock()

    def load_record(self, update_id: str) -> TemplateUpdateRecord | None:
        """Read the controller's bounded update record."""
        return self.update.load_record(update_id)

    def observe_checkout(self) -> TemplateUpdateCheckout:
        """Recheck the generated default checkout."""
        return self.update.observe_checkout()

    def observe_worktree(self, path: Path) -> TemplateUpdateWorktree:
        """Recheck the retained candidate worktree."""
        expected_root = (
            self.config.state_root / "template-updates" / str(self.config.project_id)
        ).resolve()
        if not path.is_dir() or not path.resolve().is_relative_to(expected_root):
            raise RuntimeError("template update worktree is missing or outside state")
        return self.update.observe_worktree(path)

    def _intent_path(self, update_id: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", update_id) is None:
            raise ValueError("template update identity is malformed")
        return (
            self.config.state_root
            / "template-updates"
            / "publications"
            / (f"{update_id}.json")
        )

    def load_intent(self, update_id: str) -> PublicationIntent | None:
        """Load a bounded controller publication intent, if one exists."""
        path = self._intent_path(update_id)
        if not path.exists():
            return None
        try:
            with path.open("rb") as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("publication intent is too large")
            return PublicationIntent.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as error:
            raise RuntimeError("publication intent is invalid") from error

    def save_intent(self, intent: PublicationIntent) -> None:
        """Atomically persist a monotonic publication checkpoint."""
        previous = self.load_intent(intent.update_id)
        if previous is not None and (
            previous.model_copy(
                update={"publication_sha": None, "merge_request_iid": None}
            )
            != intent.model_copy(
                update={"publication_sha": None, "merge_request_iid": None}
            )
            or (
                previous.publication_sha is not None
                and previous.publication_sha != intent.publication_sha
            )
            or (
                previous.merge_request_iid is not None
                and previous.merge_request_iid != intent.merge_request_iid
            )
        ):
            raise RuntimeError("publication intent changed")
        path = self._intent_path(intent.update_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = intent.model_dump_json().encode()
        if len(payload) > 4096:
            raise ValueError("publication intent is too large")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                temporary.chmod(0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            path.chmod(0o600)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _git(self, path: Path, *arguments: str, network: bool = False) -> str:
        environment = (
            build_git_environment(
                self.service_environment,
                self.config.checkout / "src" / "agent-work-git-credential.sh",
            )
            if network
            else {
                key: value
                for key, value in self.service_environment.items()
                if key not in {"GITLAB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"}
                and not key.startswith(("GIT_CONFIG_", "AGENT_WORK_GIT_CREDENTIAL"))
            }
        )
        completed = self.run_process(
            (str(self.config.git_path), "-C", str(path), *arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120 if network else 30,
            env=environment,
        )
        if completed.returncode or len(completed.stdout) > 8192:
            raise RuntimeError("template update Git publication failed")
        try:
            return completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise RuntimeError("Git publication output is invalid") from error

    def validate_tree(self, path: Path, tree: str) -> _ValidationEvidence:
        """Run the durable exact-tree Judge boundary in this update's state."""
        if self._git(path, "write-tree") != tree:
            raise RuntimeError("template update tree changed before validation")
        state = self.config.state_root / "template-updates" / "validations" / path.name
        if not re.fullmatch(r"[0-9a-f]{64}", path.name):
            raise RuntimeError("template update validation identity is invalid")
        snapshot_store = FilesystemJUnitSnapshotStore(root=state)

        def read_record(candidate: str) -> object | None:
            record_path = state / f"{candidate}.json"
            if not record_path.exists():
                return None
            with record_path.open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise JudgeValidationError("validation record is too large")
            return json.loads(raw)

        def write_record(candidate: str, record: dict[str, object]) -> None:
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            payload = json.dumps(record, separators=(",", ":")).encode()
            if len(payload) > 16384:
                raise JudgeValidationError("validation record is too large")
            destination = state / f"{candidate}.json"
            temporary = state / f".{candidate}.{os.getpid()}.tmp"
            try:
                with temporary.open("xb") as stream:
                    temporary.chmod(0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(destination)
                descriptor = os.open(state, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)

        safe_environment = {
            key: value
            for key, value in self.service_environment.items()
            if key not in {"GITLAB_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS"}
            and not key.startswith(("GIT_CONFIG_", "AGENT_WORK_GIT_CREDENTIAL"))
        }

        def run_hook(
            command: tuple[str, ...],
            *,
            stdin: str | None = None,
            timeout_seconds: float | None = None,
            environment: Mapping[str, str] | None = None,
            current_directory: Path | None = None,
        ) -> CommandResult:
            if command != ("git", "hook", "run", "pre-commit"):
                raise RuntimeError("unexpected template update validation command")
            result = self.run_process(
                (
                    str(self.config.git_path),
                    "-C",
                    str(path),
                    "hook",
                    "run",
                    "pre-commit",
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=safe_environment,
                check=False,
                timeout=11_400,
            )
            return CommandResult(result.returncode, "", "")

        dependencies = JudgeValidationDependencies(
            current_staged_tree=lambda: self._git(path, "write-tree"),
            read_record=read_record,
            atomic_write_record=write_record,
            candidate_lock=lambda candidate: XdgProjectLock(
                state / f"{candidate}.lock"
            ),
            run_command=run_hook,
            monotonic_clock=time.monotonic,
            sleeper=time.sleep,
            read_artifact=lambda artifact: self._read_junit(path / artifact),
            remove_junit_artifact=lambda artifact: (path / artifact).unlink(
                missing_ok=True
            ),
            capture_junit_artifacts=lambda candidate, artifacts: snapshot_store.capture(
                candidate_tree=candidate, artifacts=artifacts
            ),
        )
        report = run_judge_validation(
            candidate_tree=tree, apply=True, dependencies=dependencies
        )
        if report.state != "terminal-success" or report.returncode != 0:
            raise RuntimeError("template update validation did not pass")
        for artifact in report.junit_artifacts:
            if (
                artifact.test_count <= 0
                or artifact.path.parent != snapshot_store.root / tree
            ):
                raise RuntimeError("template update JUnit path is unbound")
            with artifact.path.open("rb") as stream:
                raw = stream.read(1_048_577)
            if len(raw) > 1_048_576 or sha256(raw).hexdigest() != artifact.sha256:
                raise RuntimeError("template update JUnit snapshot changed")
        if self._git(path, "write-tree") != tree:
            raise RuntimeError("template update tree changed after validation")
        self._git(path, "diff", "--quiet")
        return _ValidationEvidence(
            tree, True, tuple(item.environment for item in report.junit_artifacts)
        )

    @staticmethod
    def _read_junit(path: Path) -> bytes | None:
        with path.open("rb") as stream:
            return stream.read(1_048_577)

    def commit(self, path: Path, branch: str, tree: str) -> str:
        """Commit the exact staged tree with normal local hooks."""
        if self._git(path, "branch", "--show-current") != branch:
            raise RuntimeError("template update publication branch changed")
        if self._git(path, "write-tree") != tree:
            raise RuntimeError("template update publication tree changed")
        self._git(path, "diff", "--quiet")
        self._git(path, "commit", "-m", "Update generated project from template")
        return self._git(path, "rev-parse", "HEAD")

    def observe_commit(self, path: Path, sha: str) -> _CommitEvidence:
        """Read back the immutable commit tree and its sole parent."""
        if self._git(path, "rev-parse", "HEAD") != sha:
            raise RuntimeError("template update publication HEAD changed")
        if self._git(path, "status", "--porcelain"):
            raise RuntimeError("template update publication worktree is dirty")
        tree = self._git(path, "rev-parse", f"{sha}^{{tree}}")
        parents = self._git(path, "rev-list", "--parents", "-n", "1", sha).split()
        if len(parents) != 2 or parents[0] != sha:
            raise RuntimeError("template update commit parents are invalid")
        return _CommitEvidence(tree, parents[1])

    def push(self, path: Path, branch: str, sha: str) -> None:
        """Push without force using the active checkout's credential route."""
        self._git(path, "push", "origin", f"{sha}:refs/heads/{branch}", network=True)

    def observe_remote_sha(self, path: Path, branch: str) -> str | None:
        """Read the server branch SHA, including an absent branch."""
        value = self._git(
            path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}", network=True
        )
        if not value:
            return None
        pieces = value.split()
        if len(pieces) != 2 or pieces[1] != f"refs/heads/{branch}":
            raise RuntimeError("template update remote branch is ambiguous")
        return pieces[0]

    def verify_publication_target(
        self, project: TemplateUpdateProject, path: Path, base_sha: str
    ) -> bool:
        """Rebind both Git remotes to the authoritative numeric GitLab project."""
        current = discover_config(
            current_directory=project.checkout,
            environment=self.service_environment,
            run_command=self.run_command,
        )
        if (
            current.project_id != project.project_id
            or current.checkout != project.checkout
            or current.default_branch != project.default_branch
        ):
            return False
        expected_origin = self._git(project.checkout, "remote", "get-url", "origin")
        return (
            self._git(path, "remote", "get-url", "origin") == expected_origin
            and self.observe_remote_sha(path, project.default_branch) == base_sha
        )

    def _gitlab(self) -> GitLabMergeRequestPublisher:
        return GitLabMergeRequestPublisher(
            host=self.config.host,
            project_id=self.config.project_id,
            glab_path=str(self.config.glab_path),
            run_command=self.run_command,
        )

    def publish_merge_request(
        self,
        project_id: int,
        branch: str,
        target_branch: str,
        sha: str,
        current_revision: str,
        candidate_revision: str,
        validated_tree: str,
    ) -> int:
        """Find one exact existing MR or create one for human review."""
        if project_id != self.config.project_id:
            raise RuntimeError("template update project changed")
        gitlab = self._gitlab()
        matches = gitlab.list_any_merge_requests(
            project_id=project_id, source_branch=branch
        )
        if len(matches) > 1:
            raise RuntimeError("template update has multiple open merge requests")
        if matches:
            existing = matches[0]
            if (
                existing.source_project_id != project_id
                or existing.target_project_id != project_id
                or existing.target_branch != target_branch
                or existing.sha != sha
                or existing.state != "opened"
            ):
                raise RuntimeError("existing template update merge request drifted")
            return existing.iid
        created = gitlab.create_merge_request(
            project_id=project_id,
            source_branch=branch,
            target_branch=target_branch,
            title=_REVIEW_TITLE,
            description=_review_description(
                current_revision, candidate_revision, validated_tree, sha
            ),
        )
        return created.iid

    def observe_merge_request(self, project_id: int, iid: int) -> object:
        """Read MR identity and source SHA back from the numeric project."""
        return self._gitlab().fetch_merge_request(project_id=project_id, iid=iid)


def default_publication_dependencies(
    *,
    config: AgentWorkConfig,
    update: TemplateUpdateDependencies,
    service_environment: Mapping[str, str],
    run_command: CommandRunner = run_subprocess,
    run_process: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> _Dependencies:
    """Compose real publication ports only at the controller CLI edge."""
    return _DefaultDependencies(
        config, update, service_environment, run_command, run_process
    )
