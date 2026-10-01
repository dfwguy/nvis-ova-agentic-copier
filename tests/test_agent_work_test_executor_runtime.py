# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The actual controller and native-hook consumers enforce the same host policy."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest

from agent_work import runtime
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.gitlab import GitLabGateway
from agent_work.ledger import JsonLedger
from agent_work.models import (
    AgentWorkConfig,
    AuditCapacity,
    AuditReceiptSummary,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestFeedbackCursor,
    PendingMergeRequestFeedback,
    TransitionIntentRecord,
)
from agent_work.ports import CommandRunner
from agent_work.red_checkpoint import (
    RedCheckpointError,
    RedCheckpointEvidence,
    RedCheckpointRequest,
    RedCheckpointStore,
)
from agent_work.repository import CommandResult, GitRepository, RepositoryError
from agent_work.serial import run_segment
from agent_work.validation import (
    JudgeValidationDependencies,
    JudgeValidationError,
    run_judge_validation,
)
from agent_work.worker import AgentInvoker
from tests.test_agent_work_serial_runtime import FakeGitLab, FakeInvoker, FakeRepository
from tests.test_agent_work_test_executor import (
    BASE,
    NONCE,
    PATHS,
    TREE,
    FixtureHost,
    protected_paths,
)


class _RuntimeBuilder(Protocol):
    def __call__(
        self,
        *,
        debug: bool,
        composition: runtime.RuntimeCompositionDependencies | None = None,
        test_execution_policy: runtime.TestExecutionPolicy | None = None,
    ) -> runtime.RuntimeDependencies: ...


class _RepositoryConstructor(Protocol):
    def __call__(
        self,
        config: AgentWorkConfig,
        run_command: CommandRunner,
        *,
        service_environment: Mapping[str, str] | None = None,
        test_execution_policy: runtime.TestExecutionPolicy | None = None,
    ) -> GitRepository: ...


class _ValidationConstructor(Protocol):
    def __call__(
        self,
        *,
        current_staged_tree: Callable[[], str],
        read_record: Callable[[str], object | None],
        atomic_write_record: Callable[[str, dict[str, object]], None],
        candidate_lock: Callable[[str], AbstractContextManager[None]],
        run_command: CommandRunner,
        run_license_preflight: Callable[[str], object],
        monotonic_clock: Callable[[], float],
        sleeper: Callable[[float], None],
        test_execution_policy: runtime.TestExecutionPolicy | None = None,
    ) -> JudgeValidationDependencies: ...


def config(root: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.test",
        project_id=310967,
        checkout=root / "checkout",
        state_root=root / "state",
        git_path=root / "git",
        glab_path=root / "glab",
        base_branch="agent/work-base",
    )


class SnapshotRepository(GitRepository):
    """Fixture Git observations retain evidence only through the real transaction."""

    def __init__(
        self,
        settings: AgentWorkConfig,
        runner: CommandRunner,
        *,
        binding: str = "accept",
    ) -> None:
        super().__init__(settings, runner, service_environment={})
        self.binding = binding
        self.bound: runtime.TestExecutionPolicy | None = None
        self.retained: list[RedCheckpointEvidence] = []
        self.observed_head = BASE
        self.observed_paths = PATHS
        self.observed_tree = TREE

    def bind_test_execution_policy(self, policy: runtime.TestExecutionPolicy) -> bool:
        if self.binding == "exception":
            raise OSError("private binding exception")
        if self.binding in {"reject", "mismatch"}:
            return False
        self.bound = policy
        return self.bound is policy

    def head(self, worktree: Path) -> str:
        return self.observed_head

    def changed_paths(self, worktree: Path) -> tuple[str, ...]:
        return self.observed_paths

    def snapshot_test_tree(
        self, worktree: Path, base_sha: str, paths: tuple[str, ...]
    ) -> str:
        return self.observed_tree

    def checkpoint_tree(self, ref: str) -> str | None:
        return None

    def read_red_checkpoint_ref(
        self, worktree: Path, project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None:
        return None

    def write_red_checkpoint_ref(
        self, worktree: Path, evidence: RedCheckpointEvidence
    ) -> None:
        self.retained.append(evidence)

    def run_red_preflight(
        self, worktree: Path, test_paths: tuple[str, ...], candidate_tree: str
    ) -> CommandResult:
        return CommandResult(0, '{"residual_diagnostics":[],"residual_details":[]}', "")


def composition(
    settings: AgentWorkConfig, repository: SnapshotRepository, runner: CommandRunner
) -> runtime.RuntimeCompositionDependencies:
    return runtime.RuntimeCompositionDependencies(
        config=settings,
        logger=logging.getLogger("fixture-executor"),
        gitlab=GitLabGateway(settings, runner),
        repository=repository,
        invoke=AgentInvoker(
            runner, environment={}, temporary_directory=settings.checkout
        ),
        checkpoint_store=RedCheckpointStore(settings.state_root),
    )


def retained_record(settings: AgentWorkConfig) -> LedgerRecord:
    record = LedgerRecord(
        project_id=settings.project_id,
        issue_iid=387,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        workflow_lane=WorkflowLane.UNKNOWN,
        source_branch="agent/issue-387",
        worktree=settings.checkout,
        source_sha=BASE,
    )
    ledger = JsonLedger(
        settings.state_root, clock=lambda: datetime(2026, 9, 30, tzinfo=UTC)
    )
    return ledger.persist(record)


def checkpoint_request() -> RedCheckpointRequest:
    return RedCheckpointRequest(
        argv=("uv", "run", "pytest", *PATHS),
        expected_failure="FAILED example",
        test_paths=PATHS,
    )


def test_actual_runtime_checkpoint_uses_fresh_observed_identity_without_host_fallback(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    settings.checkout.mkdir()
    record = retained_record(settings)
    host = FixtureHost(settings.checkout)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(1, "FAILED example", "")

    repository = SnapshotRepository(settings, run_command)
    wired = cast(_RuntimeBuilder, runtime.build_runtime_dependencies)(
        debug=False,
        composition=composition(settings, repository, run_command),
        test_execution_policy=policy,
    )
    assert wired.apply_red_checkpoint is not None
    evidence = wired.apply_red_checkpoint(387, checkpoint_request(), TREE)
    assert repository.bound is policy
    assert commands == []
    assert len(host.prepared_requests) == 1
    bound = host.prepared_requests[0].identity
    assert (
        bound.project_id,
        bound.issue_iid,
        bound.ledger_revision,
        bound.source_branch,
        bound.base_sha,
        bound.candidate_tree,
        bound.test_paths,
    ) == (
        record.project_id,
        record.issue_iid,
        record.revision,
        record.source_branch,
        BASE,
        TREE,
        PATHS,
    )
    assert bound.nonce == NONCE
    assert host.events.count("nonce") == 1
    assert evidence.tree_oid == TREE
    assert repository.retained == [evidence]


@pytest.mark.parametrize(
    "failure", ["probe-unknown", "cleanup", "attest-3", "close", "surviving-descendant"]
)
def test_actual_runtime_selected_refusal_cannot_be_saved_as_failing_red_checkpoint(
    tmp_path: Path, failure: str
) -> None:
    settings = config(tmp_path)
    settings.checkout.mkdir()
    retained_record(settings)
    host = FixtureHost(settings.checkout, failure=failure)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(1, "FAILED example", "")

    repository = SnapshotRepository(settings, run_command)
    wired = cast(_RuntimeBuilder, runtime.build_runtime_dependencies)(
        debug=False,
        composition=composition(settings, repository, run_command),
        test_execution_policy=policy,
    )
    assert wired.apply_red_checkpoint is not None
    with pytest.raises(RedCheckpointError):
        wired.apply_red_checkpoint(387, checkpoint_request(), TREE)
    assert repository.retained == []
    assert commands == []


@pytest.mark.parametrize("binding", ["reject", "exception", "mismatch"])
def test_runtime_rejects_custom_repository_without_exact_policy_affirmation(
    tmp_path: Path, binding: str
) -> None:
    settings = config(tmp_path)
    settings.checkout.mkdir()
    host = FixtureHost(settings.checkout)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    repository = SnapshotRepository(settings, run_command, binding=binding)
    with pytest.raises((runtime.TestExecutionError, RedCheckpointError)):
        cast(_RuntimeBuilder, runtime.build_runtime_dependencies)(
            debug=False,
            composition=composition(settings, repository, run_command),
            test_execution_policy=policy,
        )
    assert calls == []
    assert host.events == []


@pytest.mark.parametrize("operation", ["judge", "serial-green", "direct-red"])
def test_real_repository_keeps_default_red_and_refuses_selected_unbound_or_native_routes(
    tmp_path: Path, operation: str
) -> None:
    settings = config(tmp_path)
    host_commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        host_commands.append(command)
        return CommandResult(1, "FAILED example", "")

    trusted = GitRepository(settings, run_command, service_environment={})
    assert (
        trusted.run_red_command(settings.checkout, checkpoint_request().argv).stdout
        == "FAILED example"
    )
    assert host_commands == [checkpoint_request().argv]
    host_commands.clear()
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    repository = cast(_RepositoryConstructor, GitRepository)(
        settings, run_command, service_environment={}, test_execution_policy=policy
    )
    with pytest.raises((runtime.TestExecutionError, RepositoryError)):
        if operation == "judge":
            repository.judge_candidate(
                settings.checkout,
                TREE,
                base_sha=BASE,
                changed_paths=PATHS,
                action="validate",
                timeout_seconds=120,
            )
        elif operation == "serial-green":
            repository.create_isolated_green_commit(
                worktree=settings.checkout,
                isolated_worktree=tmp_path / "isolated",
                red_sha=BASE,
                candidate_tree=TREE,
                message="fixture",
            )
        else:
            repository.run_red_command(settings.checkout, checkpoint_request().argv)
    assert host_commands == []
    assert host.events == []


def test_direct_validation_dependency_refuses_selected_native_hook_before_spawn_or_record_mutation(
    tmp_path: Path,
) -> None:
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    commands: list[tuple[str, ...]] = []
    writes: list[dict[str, object]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "", "")

    def write_record(tree: str, value: dict[str, object]) -> None:
        writes.append(value)

    dependencies = cast(_ValidationConstructor, JudgeValidationDependencies)(
        current_staged_tree=lambda: TREE,
        read_record=lambda tree: None,
        atomic_write_record=write_record,
        candidate_lock=lambda tree: nullcontext(),
        run_command=run_command,
        run_license_preflight=lambda tree: CommandResult(0, '{"compliant":true}', ""),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda duration: None,
        test_execution_policy=policy,
    )
    with pytest.raises((runtime.TestExecutionError, JudgeValidationError)):
        run_judge_validation(candidate_tree=TREE, apply=True, dependencies=dependencies)
    assert commands == []
    assert writes == []


@pytest.mark.parametrize(
    "change",
    ["project", "revision", "branch", "base", "state", "phase", "tree", "paths"],
)
def test_actual_checkpoint_revalidates_observed_record_and_candidate_after_execution(
    tmp_path: Path, change: str
) -> None:
    settings = config(tmp_path)
    settings.checkout.mkdir()
    record = retained_record(settings)
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(1, "FAILED example", "")

    repository = SnapshotRepository(settings, run_command)

    class DriftingHost(FixtureHost):
        def execute(self, prepared: runtime.PreparedTestExecution) -> CommandResult:
            result = super().execute(prepared)
            if change == "tree":
                repository.observed_tree = "d" * 40
            elif change == "paths":
                repository.observed_paths = ("tests/different.py",)
            else:
                updates: dict[str, dict[str, object]] = {
                    "project": {"project_id": 1},
                    "revision": {},
                    "branch": {"source_branch": "agent/issue-other"},
                    "base": {"source_sha": "d" * 40},
                    "state": {"state": RunnerState.BLOCKED},
                    "phase": {"phase": WorkflowPhase.CODING_GREEN},
                }
                changes = updates[change]
                ledger = JsonLedger(
                    settings.state_root, clock=lambda: datetime(2026, 9, 30, tzinfo=UTC)
                )
                ledger.persist(record.model_copy(update=changes))
            return result

    host = DriftingHost(settings.checkout)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    wired = cast(_RuntimeBuilder, runtime.build_runtime_dependencies)(
        debug=False,
        composition=composition(settings, repository, run_command),
        test_execution_policy=policy,
    )
    assert wired.apply_red_checkpoint is not None
    with pytest.raises(RedCheckpointError):
        wired.apply_red_checkpoint(387, checkpoint_request(), TREE)
    assert repository.retained == []
    assert calls == []


@pytest.mark.parametrize("operation", ["publication", "reconciliation"])
def test_public_commit_preparation_refuses_selected_policy_before_first_git_command(
    tmp_path: Path, operation: str
) -> None:
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(tmp_path)
    )
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        raise AssertionError("Selected commit preparation must refuse before Git")

    repository = cast(_RepositoryConstructor, GitRepository)(
        config(tmp_path),
        run_command,
        service_environment={},
        test_execution_policy=policy,
    )
    with pytest.raises(RepositoryError) as error:
        if operation == "publication":
            repository.prepare_commit(
                tmp_path,
                "agent/issue-387",
                387,
                expected_head=BASE,
                validated_paths=PATHS,
            )
        else:
            repository.prepare_reconcile_commit(
                tmp_path,
                "agent/issue-387",
                BASE,
                "d" * 40,
                TREE,
                f"refs/agent-work/red-commits/387/{BASE}",
                f"refs/agent-work/green-commits/387/{BASE}/{TREE}",
            )
    assert "unsupported_hook" in str(error.value)
    assert commands == []
    assert host.events == []


@pytest.mark.parametrize(
    "route",
    [
        "fresh-publication",
        "feedback-replay",
        "reconciliation-intent",
        "reconciliation-proof",
    ],
)
def test_actual_serial_publication_routes_refuse_selected_hooks_and_preserve_default(
    tmp_path: Path, route: str
) -> None:
    settings = config(tmp_path)
    settings.checkout.mkdir()
    host = FixtureHost(settings.checkout)
    native_calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        native_calls.append(command)
        raise AssertionError("Selected serial route must not reach Git")

    native: GitRepository

    class SerialRepository(FakeRepository):
        def __init__(self, *, selected: bool) -> None:
            super().__init__(settings)
            self.selected = selected
            self.attempts: list[str] = []
            self.paths = PATHS
            self.publication_paths = PATHS
            self.proof_sha = "e" * 40

        def prepare_commit(
            self,
            worktree: Path,
            branch: str,
            issue_iid: int,
            expected_head: str | None = None,
            validated_paths: tuple[str, ...] | None = None,
        ) -> str:
            self.attempts.append("publication")
            if self.selected:
                assert expected_head is not None
                return native.prepare_commit(
                    worktree,
                    branch,
                    issue_iid,
                    expected_head=expected_head,
                    validated_paths=validated_paths,
                )
            return super().prepare_commit(
                worktree,
                branch,
                issue_iid,
                expected_head=expected_head,
                validated_paths=validated_paths,
            )

        def prepare_reconcile_commit(
            self,
            worktree: Path,
            branch: str,
            source_sha: str,
            target_sha: str,
            candidate_tree: str,
            red_ref: str,
            green_ref: str,
        ) -> str:
            self.attempts.append("reconciliation")
            if self.selected:
                return native.prepare_reconcile_commit(
                    worktree,
                    branch,
                    source_sha,
                    target_sha,
                    candidate_tree,
                    red_ref,
                    green_ref,
                )
            self.current_head = self.proof_sha
            return self.proof_sha

    now = datetime(2026, 9, 30, tzinfo=UTC)

    def publication_record(root: Path) -> LedgerRecord:
        branch = "agent/issue-12"
        reconciliation: dict[str, str | int | None] = {
            "branch": branch,
            "source_sha": BASE,
            "target_sha": "d" * 40,
            "candidate_tree": TREE,
            "red_ref": f"refs/agent-work/red-commits/12/{BASE}",
            "green_ref": f"refs/agent-work/green-commits/12/{BASE}/{TREE}",
        }
        record = LedgerRecord(
            project_id=settings.project_id,
            issue_iid=12,
            state=RunnerState.PUBLISHING,
            phase=WorkflowPhase.READY_TO_PUBLISH,
            workflow_lane=WorkflowLane.CONTENT,
            source_branch=branch,
            worktree=settings.checkout,
            source_sha=BASE,
            merge_request_draft=MergeRequestDraft(
                title="Fixture change", description="Fixture review"
            ),
        )
        if route == "reconciliation-intent":
            return record.model_copy(
                update={
                    "workflow_lane": WorkflowLane.CODING,
                    "main_sha": "d" * 40,
                    "intent": TransitionIntentRecord(
                        operation_id="12:reconcile:0",
                        kind="reconcile_commit",
                        expected_evidence=reconciliation,
                    ),
                }
            )
        if route == "reconciliation-proof":
            return record.model_copy(
                update={
                    "workflow_lane": WorkflowLane.CODING,
                    "main_sha": "d" * 40,
                    "publication_sha": "e" * 40,
                    "intent": TransitionIntentRecord(
                        operation_id="12:publish:0",
                        kind="publish_merge_request",
                        expected_evidence={
                            **reconciliation,
                            "baseline_sha": BASE,
                            "publication_sha": "e" * 40,
                        },
                    ),
                }
            )
        if route == "feedback-replay":
            fingerprint = hashlib.sha256((PATHS[0] + "\0").encode()).hexdigest()
            audit = AuditReceiptSummary(
                lane="coding",
                capacity=AuditCapacity(
                    required=12, available=16, shortfall=0, decision="run"
                ),
                role_receipts=("$tdd-finalizer: approved",),
                changed_paths=PATHS,
                tests=("uv run pytest",),
                reviewers=("$tdd-judge: approved",),
                findings=(),
                corrections=(),
            )
            feedback = PendingMergeRequestFeedback(
                target_cursor=MergeRequestFeedbackCursor(
                    last_processed_general_note_id=31, feedback_source_sha=BASE
                ),
                source_sha=BASE,
                source="general",
                note_id=31,
                outcome="Fixture fix",
            )
            return record.model_copy(
                update={
                    "workflow_lane": WorkflowLane.CODING,
                    "work_kind": WorkKind.EXISTING_MR_REMEDIATION,
                    "merge_request_iid": 88,
                    "merge_request_url": "https://gitlab.example.test/mr/88",
                    "pending_feedback": feedback,
                    "audit_evidence": audit,
                    "intent": TransitionIntentRecord(
                        operation_id="12:publish:0",
                        kind="publish_merge_request",
                        expected_evidence={
                            "branch": branch,
                            "baseline_sha": BASE,
                            "validated_path_count": 1,
                            "validated_paths_fingerprint": fingerprint,
                            "validated_paths": json.dumps(PATHS, separators=(",", ":")),
                        },
                    ),
                }
            )
        return record

    expected_attempt = (
        "reconciliation" if route.startswith("reconciliation") else "publication"
    )
    default_repository = SerialRepository(selected=False)
    default_ledger = JsonLedger(tmp_path / "default-state", clock=lambda: now)
    default_ledger.persist(publication_record(tmp_path))
    default_result = run_segment(
        ledger=default_ledger,
        gitlab=FakeGitLab(),
        repository=default_repository,
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=now,
    )
    assert default_result is not None
    assert expected_attempt in default_repository.attempts
    assert native_calls == []

    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    native = cast(_RepositoryConstructor, GitRepository)(
        settings, run_command, service_environment={}, test_execution_policy=policy
    )
    selected_repository = SerialRepository(selected=True)
    selected_gitlab = FakeGitLab()
    selected_ledger = JsonLedger(tmp_path / "selected-state", clock=lambda: now)
    initial = selected_ledger.persist(publication_record(tmp_path))
    result = run_segment(
        ledger=selected_ledger,
        gitlab=selected_gitlab,
        repository=selected_repository,
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=now,
    )
    assert result is not None and result.state is RunnerState.BLOCKED
    assert expected_attempt in selected_repository.attempts
    assert "unsupported_hook" in result.summary
    assert result.publication_sha == initial.publication_sha
    assert result.source_sha == initial.source_sha
    assert selected_repository.pushes == 0
    assert selected_gitlab.created_merge_requests == 0
    assert native_calls == []
    assert host.events == []
