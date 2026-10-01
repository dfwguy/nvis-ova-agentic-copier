# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Real controller readers and consumers carry only verified coarse timings."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

from agent_work import gitlab, models, runtime, timing
from agent_work.domain import CommandRequest, RunnerState, WorkflowLane, WorkflowPhase
from agent_work.gitlab import GitLabError, GitLabGateway
from agent_work.ledger import JsonLedger
from agent_work.models import (
    AgentWorkConfig,
    GitLabIssue,
    LedgerRecord,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
)
from agent_work.ports import GitLabPort, InvokerPort, LedgerPort, RepositoryPort
from agent_work.repository import CommandResult, LegacyPublicationCandidate
from agent_work.serial import run_segment
from agent_work.worker import build_worker_prompt
from tests.test_agent_work_serial_runtime import (
    AdvancingMonotonicClock,
    BatchingInvoker,
    FakeGitLab,
    FakeInvoker,
    FakeRepository,
)
from tests.test_agent_work_stage_timing import (
    ISSUE,
    NOW,
    PROJECT,
    SHA,
    TREE,
    history,
    identity,
    record,
    report_control,
)


class _RunSegment(Protocol):
    """Exact future identity observation DI on the existing real controller."""

    def __call__(
        self,
        *,
        ledger: LedgerPort,
        gitlab: GitLabPort,
        repository: RepositoryPort,
        invoke: InvokerPort,
        timeout_seconds: int,
        now: datetime,
        clock: Callable[[], datetime] | None = None,
        observe_stage_identity: Callable[
            [LedgerRecord], models.StageTimingIdentity | None
        ]
        | None = None,
    ) -> LedgerRecord | None: ...


def _snapshot() -> LegacyPublicationCandidate:
    """A complete immutable clean repository observation."""
    return LegacyPublicationCandidate(
        branch="agent/issue-12",
        head=SHA,
        merge_head=None,
        index_fingerprint="c" * 64,
        staged_paths=(),
        unstaged_paths=(),
        has_untracked=False,
        has_unmerged=False,
        has_renames=False,
    )


def observed_identity(bound: LedgerRecord) -> models.StageTimingIdentity | None:
    """Execute the actual reader against synthetic repository boundaries."""
    return runtime.observe_stage_timing_identity(
        bound,
        project_id=PROJECT,
        observe_worktree=lambda _path: _snapshot(),
        read_tree=lambda _sha: TREE,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "project",
        "head",
        "branch",
        "source",
        "merge",
        "staged",
        "unstaged",
        "untracked",
        "unmerged",
        "renames",
        "tree",
        "drift",
        "read-error",
        "tree-error",
    ],
)
def test_real_source_reader_independent_fault_returns_no_timing_identity(
    fault: str,
) -> None:
    bound = record()
    assert observed_identity(bound) == identity()
    snapshot = _snapshot()
    tree: str | None = TREE
    if fault in {
        "head",
        "branch",
        "merge",
        "staged",
        "unstaged",
        "untracked",
        "unmerged",
        "renames",
    }:
        changes: Mapping[str, object] = {
            "head": {"head": "d" * 40},
            "branch": {"branch": "agent/other"},
            "merge": {"merge_head": "d" * 40},
            "staged": {"staged_paths": ("x",)},
            "unstaged": {"unstaged_paths": ("x",)},
            "untracked": {"has_untracked": True},
            "unmerged": {"has_unmerged": True},
            "renames": {"has_renames": True},
        }[fault]
        snapshot = replace(snapshot, **changes)
    if fault == "source":
        bound = bound.model_copy(update={"source_sha": "invalid"})
    if fault == "tree":
        tree = "invalid"
    reads = 0

    def observe(path: Path) -> LegacyPublicationCandidate:
        nonlocal reads
        assert path == bound.worktree
        reads += 1
        if fault == "read-error":
            raise OSError("synthetic-private-token")
        return (
            replace(snapshot, index_fingerprint="d" * 64)
            if fault == "drift" and reads > 1
            else snapshot
        )

    def read_tree(sha: str) -> str | None:
        assert sha == SHA
        if fault == "tree-error":
            raise OSError("synthetic-private-token")
        return tree

    result = runtime.observe_stage_timing_identity(
        bound,
        project_id=PROJECT + 1 if fault == "project" else PROJECT,
        observe_worktree=observe,
        read_tree=read_tree,
    )
    assert result is None


def _config(root: Path) -> AgentWorkConfig:
    """Keep every real ledger path in the fixture directory."""
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=PROJECT,
        checkout=root,
        state_root=root / "state",
        git_path=root / "git",
        glab_path=root / "glab",
        base_branch="agent/work-base",
    )


def _dependencies(
    root: Path, ledger: JsonLedger, gitlab: FakeGitLab, repository: FakeRepository
) -> runtime.RuntimeDependencies:
    """Compose actual dispatcher ports without host IO."""
    wired = runtime.RuntimeDependencies(
        config=_config(root),
        ledger=ledger,
        gitlab=gitlab,
        repository=repository,
        invoke=FakeInvoker(),
        lock=nullcontext,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        logger=logging.getLogger(f"timing-fixture-{root.name}"),
        read_plan=lambda path: path.read_bytes(),
        process_probe=lambda: "missing",
    )
    return replace(wired, observe_stage_identity=observed_identity)


def _prompt_report(payload: dict[str, object]) -> models.StageTimingReport:
    """Decode the structured controller field of the actual worker prompt."""
    prompt = build_worker_prompt(
        payload,
        project_id=PROJECT,
        issue_iid=ISSUE,
        effective_worker_budget_seconds=120,
    )
    lines = [
        line
        for line in prompt.splitlines()
        if "controller_stage_timing" in line and "{" in line
    ]
    assert len(lines) == 1
    data = ast.literal_eval(lines[0][lines[0].index("{") :])
    assert isinstance(data, dict)
    return models.StageTimingReport.model_validate(data["controller_stage_timing"])


@pytest.mark.parametrize(
    "phase", [WorkflowPhase.CODING_FINALIZE, WorkflowPhase.CONTENT_FINALIZE]
)
def test_real_runtime_and_finalizer_prompt_share_computed_controller_report(
    tmp_path: Path, phase: WorkflowPhase
) -> None:
    report_control()
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "phase": phase,
            "workflow_lane": WorkflowLane.CODING
            if phase == WorkflowPhase.CODING_FINALIZE
            else WorkflowLane.CONTENT,
        }
    )
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    stored = ledger.persist(bound)
    expected = timing.stage_timing_report(stored, expected_identity=identity())
    assert timing.stage_timing_identity(stored) == identity()
    gitlab = FakeGitLab()
    repository = FakeRepository(_config(tmp_path))
    report = runtime.dispatch(
        CommandRequest(command="loop.status", issue_iid=ISSUE),
        apply=False,
        dependencies=_dependencies(tmp_path, ledger, gitlab, repository),
    )
    actual = models.StageTimingReport.model_validate(
        report.items[0].model_dump()["stage_timing_report"]
    )
    assert (
        actual == expected
        and _prompt_report(stored.model_dump(mode="json")) == expected
    )
    assert ledger.load_read_only(ISSUE) == stored


@pytest.mark.parametrize("input_kind", ["minimal", "legacy", "forged"])
def test_worker_input_never_promotes_provided_report_or_legacy_unbound_time(
    input_kind: str,
) -> None:
    report_control()
    bound = history()
    if input_kind == "legacy":
        bound = record().model_copy(
            update={
                "phase_timings": tuple(
                    row.model_copy(update={"provenance": None})
                    for row in bound.phase_timings
                )
            }
        )
    payload: dict[str, object] = (
        {"phase": "coding-finalize"}
        if input_kind == "minimal"
        else bound.model_dump(mode="json")
    )
    payload["controller_stage_timing"] = {
        "execution_seconds": 999999,
        "coverage": "complete",
    }
    actual = _prompt_report(payload)
    if input_kind == "forged":
        assert actual == timing.stage_timing_report(bound, expected_identity=identity())
    else:
        assert actual.coverage == "unavailable" and actual.execution_seconds is None
    assert actual.internal_roles == "unavailable" and actual.wait_seconds is None


@pytest.mark.parametrize("lane", ["coding", "content"])
def test_real_serial_clock_append_binds_observed_result_and_entry_revision(
    tmp_path: Path, lane: str
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    initial = record(tmp_path).model_copy(
        update={
            "state": RunnerState.EXECUTING,
            "phase": WorkflowPhase.DISCOVERY
            if lane == "coding"
            else WorkflowPhase.CONTENT_WRITE,
            "workflow_lane": WorkflowLane.CODING
            if lane == "coding"
            else WorkflowLane.CONTENT,
            "main_sha": SHA,
        }
    )
    entry = ledger.persist(initial)
    moments = iter((NOW, NOW + timedelta(seconds=2)))
    repository = FakeRepository(_config(tmp_path))
    repository.paths = ()
    outcome = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=FakeGitLab(),
        repository=repository,
        invoke=BatchingInvoker(lane=lane),
        timeout_seconds=120,
        now=NOW,
        clock=lambda: next(moments),
        observe_stage_identity=observed_identity,
    )
    assert outcome is not None and outcome.state is RunnerState.EXECUTING
    assert outcome.phase != entry.phase
    row = outcome.phase_timings[-1].model_dump()
    assert (
        row["provenance"]
        == models.StageTimingProvenance(
            identity=identity(), boundary_revision=entry.revision
        ).model_dump()
    )
    assert (
        row["elapsed_seconds"] == 2 and outcome.model_dump()["phase_timing_count"] == 1
    )
    assert (
        timing.stage_timing_report(
            outcome, expected_identity=identity()
        ).execution_seconds
        == 2
    )


@pytest.mark.parametrize(
    "fault", ["missing-clock", "clock-error", "observer-error", "missing-observer"]
)
def test_timing_boundary_fault_never_changes_successful_publication_or_fabricates_verified_zero(
    tmp_path: Path, fault: str
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    ledger.persist(
        record(tmp_path).model_copy(
            update={
                "state": RunnerState.PUBLISHING,
                "phase": WorkflowPhase.READY_TO_PUBLISH,
                "workflow_lane": WorkflowLane.CONTENT,
                "merge_request_draft": MergeRequestDraft(
                    title="Fixture", description="Fixture change"
                ),
            }
        )
    )

    def bad_clock() -> datetime:
        raise OSError("synthetic-private-token")

    def bad_observer(bound: LedgerRecord) -> models.StageTimingIdentity | None:
        del bound
        raise OSError("synthetic-private-token")

    moments = iter((NOW, NOW + timedelta(seconds=2)))
    outcome = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=FakeGitLab(),
        repository=FakeRepository(_config(tmp_path)),
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW,
        clock=None
        if fault == "missing-clock"
        else bad_clock
        if fault == "clock-error"
        else lambda: next(moments),
        observe_stage_identity=None
        if fault == "missing-observer"
        else bad_observer
        if fault == "observer-error"
        else observed_identity,
    )
    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    result = timing.stage_timing_report(outcome, expected_identity=identity())
    assert result.execution_seconds is None
    assert (
        "clock_unavailable" in result.unavailable_reasons
        if fault in {"missing-clock", "clock-error"}
        else "missing_provenance" in result.unavailable_reasons
    )
    assert "synthetic-private-token" not in outcome.model_dump_json()


class _DraftGitLab(FakeGitLab):
    """Observe the controller's actual publication request at the API boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.drafts: list[MergeRequestDraft] = []

    def create_merge_request(
        self, *, branch: str, target: str, issue_iid: int, draft: MergeRequestDraft
    ) -> MergeRequestEvidence:
        self.drafts.append(draft)
        return super().create_merge_request(
            branch=branch, target=target, issue_iid=issue_iid, draft=draft
        )


@pytest.mark.parametrize("source_changed", [False, True])
def test_actual_publication_draft_uses_fresh_source_context_not_precommit_totals(
    tmp_path: Path, *, source_changed: bool
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "state": RunnerState.PUBLISHING,
            "phase": WorkflowPhase.READY_TO_PUBLISH,
            "workflow_lane": WorkflowLane.CONTENT,
            "merge_request_draft": MergeRequestDraft(
                title="Fixture", description="Controller draft"
            ),
        }
    )
    if source_changed:
        bound = bound.model_copy(
            update={
                "phase_timings": tuple(
                    row.model_copy(
                        update={
                            "provenance": models.StageTimingProvenance(
                                identity=identity().model_copy(
                                    update={"source_sha": "d" * 40}
                                ),
                                boundary_revision=index,
                            )
                        }
                    )
                    for index, row in enumerate(bound.phase_timings)
                )
            }
        )
    expected = timing.stage_timing_report(bound, expected_identity=identity())
    ledger.persist(bound)
    gitlab = _DraftGitLab()
    moments = iter((NOW + timedelta(seconds=30), NOW + timedelta(seconds=32)))
    outcome = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=gitlab,
        repository=FakeRepository(_config(tmp_path)),
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW + timedelta(seconds=30),
        clock=lambda: next(moments),
        observe_stage_identity=observed_identity,
    )
    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert (
        gitlab.drafts
        and timing.render_stage_timing_report(expected) in gitlab.drafts[-1].description
    )
    if source_changed:
        assert (
            expected.execution_seconds is None
            and "identity_mismatch" in expected.unavailable_reasons
        )
    else:
        assert expected.execution_seconds == 4


def test_actual_runtime_pending_pipeline_metadata_does_not_consume_work_segment_budget(
    tmp_path: Path,
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "state": RunnerState.OBSERVING,
            "phase": WorkflowPhase.PIPELINE_WAIT,
            "merge_request_iid": 88,
            "publication_sha": SHA,
        }
    )
    ledger.persist(bound)
    gitlab = FakeGitLab()
    gitlab.pipeline = PipelineEvidence(
        id=4, status="running", sha=SHA, web_url="https://pipeline.test/4"
    )
    repository = FakeRepository(_config(tmp_path))
    clock = AdvancingMonotonicClock()
    wired = replace(
        _dependencies(tmp_path, ledger, gitlab, repository),
        clock=lambda: NOW + timedelta(seconds=30 + clock.value),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    report = runtime.dispatch(
        CommandRequest(command="loop.run", max_segments=1, max_activation_seconds=10),
        apply=True,
        dependencies=wired,
    )
    latest = ledger.load_read_only(ISSUE)
    assert latest is not None
    assert report.segments_run == 0 and clock.delays
    assert latest.model_dump()["phase_timing_count"] > 2
    assert latest.phase_timings[-1].phase_or_role == "pipeline-poll"
    assert (
        timing.stage_timing_report(latest, expected_identity=identity()).wait_seconds
        is None
    )


def test_actual_merged_completion_retains_verified_coarse_report_in_summary(
    tmp_path: Path,
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "state": RunnerState.OBSERVING,
            "phase": WorkflowPhase.PIPELINE_WAIT,
            "merge_request_iid": 88,
            "publication_sha": SHA,
        }
    )
    ledger.persist(bound)
    gitlab = FakeGitLab()
    gitlab.pipeline = PipelineEvidence(
        id=4, status="success", sha=SHA, web_url="https://pipeline.test/4"
    )
    gitlab.pipeline_jobs = (
        {
            "id": 1,
            "name": "unit",
            "stage": "test",
            "status": "success",
            "retried": False,
            "allow_failure": False,
            "pipeline_id": 4,
            "pipeline_sha": SHA,
        },
    )
    gitlab.issue = gitlab.issue.model_copy(update={"state": "closed"})
    gitlab.existing_mr = MergeRequestEvidence(
        iid=88,
        web_url="https://mr.test/88",
        state="merged",
        source_project_id=PROJECT,
        target_project_id=PROJECT,
        source_branch="agent/issue-12",
        target_branch="main",
        sha=SHA,
        description="Closes #12",
    )
    moments = iter((NOW + timedelta(seconds=30), NOW + timedelta(seconds=32)))
    completed = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=gitlab,
        repository=FakeRepository(_config(tmp_path)),
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW + timedelta(seconds=30),
        clock=lambda: next(moments),
        observe_stage_identity=observed_identity,
    )
    assert completed is not None and completed.state is RunnerState.COMPLETE
    expected = timing.stage_timing_report(completed, expected_identity=identity())
    assert (
        expected.stages
        and timing.render_stage_timing_report(expected) in completed.summary
    )


class _ClosingAfterSelectionGitLab(FakeGitLab):
    """Observe merge auto-close between selection and normal pipeline observation."""

    def fetch_issue(self, iid: int) -> GitLabIssue:
        observed = super().fetch_issue(iid)
        return (
            observed.model_copy(update={"state": "closed"})
            if len(self.fetch_issue_calls) > 1
            else observed
        )


def test_actual_normal_merged_completion_reports_final_appended_poll_history(
    tmp_path: Path,
) -> None:
    report_control()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "state": RunnerState.OBSERVING,
            "phase": WorkflowPhase.PIPELINE_WAIT,
            "merge_request_iid": 88,
            "publication_sha": SHA,
        }
    )
    entry = ledger.persist(bound)
    remote = _ClosingAfterSelectionGitLab()
    remote.issue = remote.issue.model_copy(update={"labels": ("agent::running",)})
    assert remote.issue.state == "opened"
    remote.pipeline = PipelineEvidence(
        id=4, status="success", sha=SHA, web_url="https://pipeline.test/4"
    )
    remote.pipeline_jobs = (
        {
            "id": 1,
            "name": "unit",
            "stage": "test",
            "status": "success",
            "retried": False,
            "allow_failure": False,
            "pipeline_id": 4,
            "pipeline_sha": SHA,
        },
    )
    remote.existing_mr = MergeRequestEvidence(
        iid=88,
        web_url="https://mr.test/88",
        state="merged",
        source_project_id=PROJECT,
        target_project_id=PROJECT,
        source_branch="agent/issue-12",
        target_branch="main",
        sha=SHA,
        description="Closes #12",
    )
    moments = iter((NOW + timedelta(seconds=30), NOW + timedelta(seconds=32)))
    completed = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=remote,
        repository=FakeRepository(_config(tmp_path)),
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW + timedelta(seconds=30),
        clock=lambda: next(moments),
        observe_stage_identity=observed_identity,
    )
    assert completed is not None and completed.state is RunnerState.COMPLETE
    assert remote.fetch_issue_calls == [ISSUE, ISSUE]
    assert len(completed.phase_timings) == 3
    assert completed.phase_timings[:-1] == entry.phase_timings
    final_row = completed.phase_timings[-1].model_dump()
    assert (
        final_row["phase_or_role"] == "pipeline-poll"
        and final_row["elapsed_seconds"] == 2
    )
    assert (
        final_row["provenance"]
        == models.StageTimingProvenance(
            identity=identity(), boundary_revision=entry.revision
        ).model_dump()
    )
    expected = timing.stage_timing_report(completed, expected_identity=identity())
    assert (
        expected.execution_seconds == 6
        and sum(stage.observations for stage in expected.stages) == 3
    )
    assert timing.render_stage_timing_report(expected) in completed.summary
    assert ledger.load_read_only(ISSUE) == completed


class _GatewayFixture:
    """Actual authenticated adapter with exact response echo at process boundary."""

    def __init__(self, root: Path) -> None:
        self.config = _config(root)
        self.calls: list[tuple[str, ...]] = []
        self.bodies: list[str] = []
        self.gateway = GitLabGateway(self.config, self.run)
        self.mr = MergeRequestEvidence(
            iid=88,
            web_url="https://mr.test/88",
            state="opened",
            source_project_id=PROJECT,
            target_project_id=PROJECT,
            source_branch="agent/issue-12",
            target_branch="main",
            sha=SHA,
            title="Original",
            description="Original description",
        )

    def run(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append(command)
        assert command[:4] == (
            str(self.config.glab_path),
            "api",
            "--hostname",
            self.config.host,
        )
        assert command[4] == "-X" and command[5] in {"POST", "PUT"}
        assert command[6] == f"projects/{PROJECT}/merge_requests" + (
            "/88" if command[5] == "PUT" else ""
        )
        assert kwargs.get("timeout_seconds") is not None
        fields = dict(part.split("=", 1) for part in command[8::2])
        body = fields["description"]
        self.bodies.append(body)
        payload = self.mr.model_dump(mode="json")
        payload.update(
            {"title": fields.get("title", self.mr.title), "description": body}
        )
        return CommandResult(0, json.dumps(payload), "")

    def call(self, operation: str, draft: MergeRequestDraft) -> MergeRequestEvidence:
        if operation == "create":
            return self.gateway.create_merge_request(
                branch="agent/issue-12", target="main", issue_iid=ISSUE, draft=draft
            )
        return self.gateway.reconcile_merge_request(
            self.mr, issue_iid=ISSUE, draft=draft
        )


def _canonical_draft(size: int, character: str = "x") -> MergeRequestDraft:
    """Use an original-valid raw draft whose final canonical field has exact size."""
    tiny = MergeRequestDraft(title="Fixture", description=character)
    overhead = (
        len(gitlab.canonical_merge_request_description(tiny, issue_iid=ISSUE)) - 1
    )
    draft = MergeRequestDraft(
        title="Fixture", description=character * (size - overhead)
    )
    assert MergeRequestDraft.model_validate(draft.model_dump()) == draft
    assert (
        len(gitlab.canonical_merge_request_description(draft, issue_iid=ISSUE)) == size
    )
    return draft


def _validate_final_description(body: str) -> str:
    """Apply the existing final description field bound without raw-draft reserve."""
    return TypeAdapter[str](
        Annotated[str, MergeRequestDraft.model_fields["description"]]
    ).validate_python(body)


@pytest.mark.parametrize("operation", ["create", "reconcile"])
@pytest.mark.parametrize(
    ("size", "character"), [(19_000, "x"), (20_000, "x"), (20_000, "界")]
)
def test_actual_gateway_transports_only_full_schema_valid_canonical_body(
    tmp_path: Path, operation: str, size: int, character: str
) -> None:
    draft = _canonical_draft(size, character)
    fixture = _GatewayFixture(tmp_path)
    result = fixture.call(operation, draft)
    assert len(fixture.calls) == 1
    assert fixture.bodies == [
        gitlab.canonical_merge_request_description(draft, issue_iid=ISSUE)
    ]
    assert result.description == fixture.bodies[0]
    assert _validate_final_description(result.description) == result.description


@pytest.mark.parametrize("operation", ["create", "reconcile"])
@pytest.mark.parametrize(
    "fault",
    ["final-one-overflow", "raw-max-overflow", "invalid-original"],
)
def test_actual_gateway_invalid_original_or_canonical_overflow_has_zero_transport(
    tmp_path: Path, operation: str, fault: str
) -> None:
    control = _canonical_draft(20_000)
    fixture = _GatewayFixture(tmp_path)
    assert fixture.call(
        operation, control
    ).description == gitlab.canonical_merge_request_description(
        control, issue_iid=ISSUE
    )
    fixture.calls.clear()
    fixture.bodies.clear()
    bad = MergeRequestDraft(title="Fixture", description="x" * 19_980)
    if fault == "final-one-overflow":
        overhead = 20_000 - len(control.description)
        bad = MergeRequestDraft(title="Fixture", description="x" * (20_001 - overhead))
        assert MergeRequestDraft.model_validate(bad.model_dump()) == bad
    elif fault == "invalid-original":
        bad = bad.model_copy(update={"description": "x" * 20_001})
    with pytest.raises(GitLabError):
        gitlab.canonical_merge_request_description(bad, issue_iid=ISSUE)
    with pytest.raises(GitLabError):
        fixture.call(operation, bad)
    assert fixture.calls == [] and fixture.bodies == []


@pytest.mark.parametrize(
    ("size", "character", "fits"),
    [(10_000, "x", True), (20_000, "x", False), (19_999, "界", False)],
)
def test_actual_serial_optional_summary_fits_full_canonical_body_or_preserves_human_text(
    tmp_path: Path, size: int, character: str, *, fits: bool
) -> None:
    report_control()
    original = _canonical_draft(size, character)
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    bound = history().model_copy(
        update={
            "worktree": tmp_path,
            "state": RunnerState.PUBLISHING,
            "phase": WorkflowPhase.READY_TO_PUBLISH,
            "workflow_lane": WorkflowLane.CONTENT,
            "merge_request_draft": original,
        }
    )
    expected = timing.stage_timing_report(bound, expected_identity=identity())
    ledger.persist(bound)
    remote = _DraftGitLab()
    moments = iter((NOW + timedelta(seconds=30), NOW + timedelta(seconds=32)))
    outcome = cast(_RunSegment, run_segment)(
        ledger=ledger,
        gitlab=remote,
        repository=FakeRepository(_config(tmp_path)),
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW + timedelta(seconds=30),
        clock=lambda: next(moments),
        observe_stage_identity=observed_identity,
    )
    assert outcome is not None and outcome.state is RunnerState.OBSERVING
    assert len(remote.drafts) == 1
    selected = remote.drafts[0]
    full = gitlab.canonical_merge_request_description(selected, issue_iid=ISSUE)
    assert _validate_final_description(full) == full
    if fits:
        assert original.description in selected.description
        assert timing.render_stage_timing_report(expected) in selected.description
    else:
        assert selected.description == original.description
    gateway = _GatewayFixture(tmp_path)
    gateway.call("create", selected)
    assert gateway.bodies == [full] and len(gateway.calls) == 1


@settings(max_examples=20)
@given(
    human=st.text(alphabet="abcXYZ012 界é", min_size=1, max_size=500),
    issue=st.integers(min_value=1, max_value=1_000_000),
)
def test_generated_canonical_description_preserves_human_text_and_refuses_invalid_transport(
    human: str, issue: int
) -> None:
    draft = MergeRequestDraft(title="Fixture", description="Human: " + human + " end")
    original = draft.model_dump()
    body = gitlab.canonical_merge_request_description(draft, issue_iid=issue)
    assert draft.description in body
    assert body.count(f"Closes #{issue}") == 1
    assert body.count("<!-- agent-work-feedback-notice:v1 -->") == 1
    assert body == gitlab.canonical_merge_request_description(draft, issue_iid=issue)
    assert _validate_final_description(body) == body
    ordinary = MergeRequestDraft(title=draft.title, description=body)
    assert gitlab.canonical_merge_request_description(ordinary, issue_iid=issue) == body
    assert draft.model_dump() == original
    # Exercise actual Gateway refusal at the configured issue, with a valid transport control.
    for operation in ("create", "reconcile"):
        fixture = _GatewayFixture(Path("/synthetic-fixture"))
        assert fixture.call(
            operation, draft
        ).description == gitlab.canonical_merge_request_description(
            draft, issue_iid=ISSUE
        )
        fixture.calls.clear()
        fixture.bodies.clear()
        for invalid in (
            draft.model_copy(update={"description": "x" * 20_001}),
            MergeRequestDraft(title=draft.title, description="x" * 19_980),
        ):
            with pytest.raises(GitLabError):
                gitlab.canonical_merge_request_description(invalid, issue_iid=ISSUE)
            with pytest.raises(GitLabError):
                fixture.call(operation, invalid)
            assert fixture.calls == [] and fixture.bodies == []


def _baseline_publication(
    root: Path, description: str, *, reconcile: bool
) -> tuple[LedgerRecord | None, FakeRepository, FakeGitLab]:
    """Use only existing controller/model contracts, independent of timing APIs."""
    root.mkdir()
    ledger = JsonLedger(root / "state", clock=lambda: NOW)
    draft = MergeRequestDraft(title="Fixture", description=description)
    assert MergeRequestDraft.model_validate(draft.model_dump()) == draft
    bound = record(root).model_copy(
        update={
            "state": RunnerState.PUBLISHING,
            "phase": WorkflowPhase.READY_TO_PUBLISH,
            "workflow_lane": WorkflowLane.CONTENT,
            "merge_request_draft": draft,
        }
    )
    ledger.persist(bound)
    repository = FakeRepository(_config(root))
    remote = FakeGitLab()
    if reconcile:
        remote.existing_mr = MergeRequestEvidence(
            iid=88,
            web_url="https://mr.test/88",
            state="opened",
            source_project_id=PROJECT,
            target_project_id=PROJECT,
            source_branch="agent/issue-12",
            target_branch="main",
            sha=SHA,
            description="Closes #12",
        )
    outcome = run_segment(
        ledger=ledger,
        gitlab=remote,
        repository=repository,
        invoke=FakeInvoker(),
        timeout_seconds=120,
        now=NOW,
    )
    return outcome, repository, remote


@pytest.mark.parametrize("character", ["x", "界"])
@pytest.mark.parametrize("reconcile", [False, True])
def test_actual_publication_rejects_original_canonical_overflow_before_push_or_mr_mutation(
    tmp_path: Path, character: str, *, reconcile: bool
) -> None:
    # Establish the exact character overhead through the existing actual Gateway.
    fixture = _GatewayFixture(tmp_path)
    small = MergeRequestDraft(title="Fixture", description="Control")
    canonical = fixture.call("create", small).description
    overhead = len(canonical) - len(small.description)
    assert overhead > 20
    small_outcome, small_repository, small_remote = _baseline_publication(
        tmp_path / "small", small.description, reconcile=reconcile
    )
    assert small_outcome is not None and small_outcome.state is RunnerState.OBSERVING
    assert small_repository.pushes == 1
    assert small_remote.created_merge_requests == int(not reconcile)
    assert len(small_remote.reconciled_drafts) == int(reconcile)
    positive_text = character * 19_000
    assert len(positive_text) + overhead < 20_000
    if character == "界":
        assert len(positive_text.encode("utf-8")) > 20_000
    accepted, positive_repository, positive_remote = _baseline_publication(
        tmp_path / "control", positive_text, reconcile=reconcile
    )
    assert accepted is not None and accepted.state is RunnerState.OBSERVING
    assert positive_repository.pushes == 1
    assert positive_remote.created_merge_requests == int(not reconcile)
    assert len(positive_remote.reconciled_drafts) == int(reconcile)
    invalid_text = character * 19_980
    invalid_draft = MergeRequestDraft(title="Fixture", description=invalid_text)
    assert MergeRequestDraft.model_validate(invalid_draft.model_dump()) == invalid_draft
    assert len(invalid_text) + overhead > 20_000
    rejected, repository, remote = _baseline_publication(
        tmp_path / "overflow", invalid_text, reconcile=reconcile
    )
    assert repository.pushes == 0
    assert remote.created_merge_requests == 0 and remote.reconciled_drafts == []
    assert rejected is not None and rejected.state is not RunnerState.OBSERVING
    assert rejected.merge_request_iid is None
