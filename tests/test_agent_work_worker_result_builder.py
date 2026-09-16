# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only worker-result builder."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_work.cli import main
from agent_work.domain import (
    RunnerState,
    WorkflowLane,
    WorkflowPhase,
    WorkKind,
)
from agent_work.ledger import LedgerCorrupt
from agent_work.models import AgentWorkConfig, LedgerRecord
from agent_work.repository import CommandResult
from agent_work.runtime import build_worker_result_dependencies
from agent_work.state_machine import (
    WORKFLOW_RECEIPTS,
    apply_worker_result,
    has_complete_workflow_receipt,
    plan_retry,
)
from agent_work.worker import (
    WorkerProtocolError,
    WorkerResultBuildInput,
    build_worker_prompt,
    build_worker_result,
    format_worker_result_diagnostic,
    parse_worker_result_build_input,
)


def _record(tmp_path: Path) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=81,
        revision=7,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-81",
        worktree=tmp_path / "retained-worktree",
        source_sha="a" * 40,
    )


def _completed_input() -> WorkerResultBuildInput:
    receipts = tuple(
        f"{skill}: approved; uv run pytest tests/test_agent_work_worker_result_builder.py"
        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
    )
    return WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": receipts,
            "change_summary": "Added a read-only worker result builder.",
            "next_action": "Publish the reviewed change.",
            "merge_request_draft": {
                "title": "Add worker result builder",
                "description": "Summary and test evidence for human review.",
            },
            "audit_summary": {
                "capacity": {
                    "required": 13,
                    "available": 16,
                    "shortfall": 0,
                    "decision": "run",
                },
                "tests": [
                    "uv run pytest tests/test_agent_work_worker_result_builder.py"
                ],
                "findings": [],
                "corrections": [],
            },
        }
    )


def test_builder_derives_trusted_identity_and_validates_completed_workflow(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    result = build_worker_result(
        record,
        _completed_input(),
        changed_paths=("src/agent_work/worker.py", "tests/test_agent_work_worker.py"),
    )

    assert result.protocol_version == 2
    assert (result.project_id, result.issue_iid, result.ledger_revision) == (
        310967,
        81,
        7,
    )
    assert result.expected_state is RunnerState.EXECUTING
    assert result.phase_status == "complete"
    assert result.completed_phase is WorkflowPhase.WORKFLOW
    assert result.audit_summary is not None
    assert result.audit_summary.changed_paths == (
        "src/agent_work/worker.py",
        "tests/test_agent_work_worker.py",
    )
    assert result.audit_summary.role_receipts == result.receipts
    assert "$tdd-judge: approved" in "\n".join(result.audit_summary.reviewers)
    assert all(
        reviewer in "\n".join(result.receipts)
        for reviewer in result.audit_summary.reviewers
    )
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 14, tzinfo=UTC)
    ).record
    assert transitioned.state is RunnerState.PUBLISHING
    assert transitioned.phase is WorkflowPhase.READY_TO_PUBLISH


def test_builder_produces_a_structured_human_only_close_suggestion(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "suggest_close",
            "change_summary": "The work is already satisfied by the exact merged change.",
            "next_action": "A human may review the evidence and close the issue.",
            "suggest_close_evidence": {
                "reason": "already-satisfied",
                "issue_iid": 81,
                "evidence": [
                    "https://gitlab.example.test/group/project/-/merge_requests/9"
                ],
            },
        }
    )

    result = build_worker_result(record, build_input, changed_paths=())
    outcome = apply_worker_result(record, result, now=datetime(2026, 9, 16, tzinfo=UTC))

    assert result.phase_status == "complete"
    assert outcome.record.state is RunnerState.SUGGEST_CLOSE
    assert outcome.record.suggest_close_evidence is not None
    assert outcome.record.suggest_close_evidence.issue_iid == record.issue_iid


def test_builder_rejects_a_close_suggestion_bound_to_another_issue(
    tmp_path: Path,
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "suggest_close",
            "change_summary": "The work is already satisfied.",
            "next_action": "A human may review the evidence.",
            "suggest_close_evidence": {
                "reason": "already-satisfied",
                "issue_iid": 82,
                "evidence": [
                    "https://gitlab.example.test/group/project/-/merge_requests/9"
                ],
            },
        }
    )

    with pytest.raises(WorkerProtocolError):
        build_worker_result(record, build_input, changed_paths=())


def test_builder_preserves_a_complete_multiline_coding_receipt_audit(
    tmp_path: Path,
) -> None:
    """A realistic all-role result remains publishable after durable storage."""
    record = _record(tmp_path)
    receipts = tuple(
        f"{skill}: approved\n"
        f"evidence: {skill} checked the final diff\n"
        + (
            "not applicable — no CLI surface changed"
            if skill == "$cli-syntax-judge"
            else "command: uv run pytest tests/test_agent_work_worker_result_builder.py"
        )
        for skill in WORKFLOW_RECEIPTS[WorkflowLane.CODING]
    )
    build_input = _completed_input().model_copy(update={"receipts": receipts})

    result = build_worker_result(
        record,
        build_input,
        changed_paths=("src/agent_work/worker.py",),
    )
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 14, tzinfo=UTC)
    ).record

    assert transitioned.state is RunnerState.PUBLISHING
    assert transitioned.phase is WorkflowPhase.READY_TO_PUBLISH
    assert has_complete_workflow_receipt(transitioned)


def test_builder_completes_successful_post_publication_assessment_for_human_handoff(
    tmp_path: Path,
) -> None:
    """A healthy exact-source assessment records its receipt before handoff."""
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    assessment = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": ("$pipeline-performance-judge: exact source is healthy",),
            "change_summary": "The exact-source pipeline is healthy.",
            "next_action": "Await human review of the merge request.",
        }
    )

    result = build_worker_result(record, assessment, changed_paths=())
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 15, tzinfo=UTC)
    ).record

    assert result.phase_status == "complete"
    assert transitioned.state is RunnerState.AWAITING_HUMAN
    assert transitioned.phase is WorkflowPhase.POST_PUBLICATION
    assert transitioned.receipts[-1].verdict == "complete"
    assert "exact source is healthy" in transitioned.receipts[-1].detail


@pytest.mark.parametrize(
    ("result_kind", "error_code", "expected_state", "preserves_error_code"),
    [
        ("needs_human", "pipeline_rejected", RunnerState.BLOCKED, True),
        ("retryable", "pipeline_pending", RunnerState.RETRY_WAIT, False),
    ],
)
def test_builder_keeps_actionable_post_publication_assessments_rejected(
    tmp_path: Path,
    result_kind: str,
    error_code: str,
    expected_state: RunnerState,
    preserves_error_code: bool,
) -> None:
    """Actionable assessments retain their judge evidence instead of handoff."""
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )
    assessment = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "coding",
            "result_kind": result_kind,
            "receipts": ("$pipeline-performance-judge: actionable evidence",),
            "change_summary": "The exact-source pipeline needs follow-up.",
            "next_action": "Resolve the recorded pipeline feedback.",
            "error_code": error_code,
        }
    )

    result = build_worker_result(record, assessment, changed_paths=())
    transitioned = apply_worker_result(
        record, result, now=datetime(2026, 9, 15, tzinfo=UTC)
    ).record

    assert result.phase_status == "rejected"
    assert result.receipts == assessment.receipts
    assert transitioned.state is expected_state
    if preserves_error_code:
        assert transitioned.error_code is result.error_code


@pytest.mark.parametrize(
    "assessment",
    [
        {
            "workflow_lane": "coding",
            "result_kind": "ready_to_publish",
            "receipts": ("$tdd-finalizer: unrelated receipt",),
            "change_summary": "Malformed assessment.",
            "next_action": "Do not hand off.",
        },
        {
            "workflow_lane": "content",
            "result_kind": "ready_to_publish",
            "receipts": ("$pipeline-performance-judge: healthy",),
            "change_summary": "Contradictory lane.",
            "next_action": "Do not hand off.",
        },
    ],
    ids=("malformed-receipt", "contradictory-lane"),
)
def test_builder_rejects_incoherent_successful_post_publication_assessments(
    tmp_path: Path, assessment: dict[str, object]
) -> None:
    record = _record(tmp_path).model_copy(
        update={"phase": WorkflowPhase.POST_PUBLICATION}
    )

    with pytest.raises(WorkerProtocolError, match=r"receipts:.*coherent"):
        build_worker_result(
            record,
            WorkerResultBuildInput.model_validate(assessment),
            changed_paths=(),
        )


def test_builder_input_forbids_worker_control_of_derived_phase_status() -> None:
    payload = {
        "workflow_lane": "coding",
        "result_kind": "ready_to_publish",
        "receipts": ["$pipeline-performance-judge: healthy"],
        "phase_status": "complete",
        "change_summary": "Contradictory worker intent.",
        "next_action": "Do not accept derived fields.",
    }

    with pytest.raises(WorkerProtocolError, match="input: invalid value"):
        parse_worker_result_build_input(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "untrusted_value"),
    [
        ("protocol_version", "99: super-secret-token"),
        ("receipts.0", "x" * 10_000),
    ],
)
def test_builder_diagnostic_is_field_specific_bounded_and_never_echoes_input(
    field: str, untrusted_value: str
) -> None:
    with pytest.raises(WorkerProtocolError) as captured:
        parse_worker_result_build_input(json.dumps({field: untrusted_value}).encode())

    diagnostic = format_worker_result_diagnostic(captured.value)

    expected = (
        "protocol_version: must be omitted"
        if field == "protocol_version"
        else "input: invalid value"
    )
    assert diagnostic == expected
    assert len(diagnostic.encode("utf-8")) <= 2048
    assert untrusted_value not in diagnostic


@pytest.mark.parametrize(
    "receipts",
    [
        ("$architect: approved",),
        lambda complete: (*complete[:-1], complete[0]),
        lambda complete: tuple(receipt.removeprefix("$") for receipt in complete),
        lambda complete: ("$architect", *complete[1:]),
    ],
    ids=("incomplete", "duplicate", "malformed", "missing-detail"),
)
def test_builder_rejects_incomplete_duplicate_or_malformed_receipts(
    tmp_path: Path,
    receipts: tuple[str, ...] | Callable[[tuple[str, ...]], tuple[str, ...]],
) -> None:
    complete = _completed_input().receipts
    resolved = receipts if isinstance(receipts, tuple) else receipts(complete)
    invalid = _completed_input().model_copy(update={"receipts": resolved})

    with pytest.raises(WorkerProtocolError, match=r"receipts:.*required"):
        build_worker_result(_record(tmp_path), invalid, changed_paths=())


@pytest.mark.parametrize(
    "issue_iid",
    [
        40,
        78,
    ],
)
def test_worker_validation_failures_keep_the_retained_worktree_retryable(
    tmp_path: Path, issue_iid: int
) -> None:
    record = _record(tmp_path).model_copy(update={"issue_iid": issue_iid})

    with pytest.raises(WorkerProtocolError):
        build_worker_result(
            record,
            _completed_input().model_copy(update={"receipts": ("$architect",)}),
            changed_paths=(),
        )

    retry = plan_retry(record, now=datetime(2026, 9, 14, tzinfo=UTC)).record

    assert retry.state is RunnerState.RETRY_WAIT
    assert retry.prior_state is RunnerState.EXECUTING
    assert retry.prior_phase is WorkflowPhase.WORKFLOW
    assert retry.retry_count == record.retry_count + 1
    assert retry.worktree == record.worktree


def test_worker_prompt_requires_the_builder_and_v2_contract(tmp_path: Path) -> None:
    prompt = build_worker_prompt(
        _record(tmp_path).model_dump(mode="json"),
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=600,
    )

    assert "agent-work worker-result build" in prompt
    assert "protocol_version=2" in prompt
    assert "WORKFLOW_RECEIPTS" in prompt


def test_worker_result_build_cli_writes_only_one_envelope_to_stdout(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--apply",
        ],
        load_record=lambda issue_iid: _record(tmp_path) if issue_iid == 81 else None,
        changed_paths=lambda worktree: ("src/agent_work/worker.py",),
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 0
    assert stderr
    assert len(stdout) == 1
    assert stdout[0].startswith("<agent-work-result>{")
    assert stdout[0].endswith("}</agent-work-result>")
    assert "\n" not in stdout[0]


def test_worker_result_build_json_writes_one_bare_compact_object(
    tmp_path: Path,
) -> None:
    """The explicit JSON mode is directly consumable by JSON tooling."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stdout: list[str] = []

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--json",
        ],
        load_record=lambda issue_iid: _record(tmp_path) if issue_iid == 81 else None,
        changed_paths=lambda worktree: ("src/agent_work/worker.py",),
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert len(stdout) == 1
    assert json.loads(stdout[0])["protocol_version"] == 2


def test_worker_result_build_help_and_usage_follow_argparse_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["worker-result", "build", "--help"])

    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "agent-work worker-result build" in help_text
    assert "--input-file" in help_text
    assert "retain bounded diagnostics on failure" in help_text

    with pytest.raises(SystemExit) as usage_exit:
        main(["worker-result", "build"])

    assert usage_exit.value.code == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("case", "input_payload"),
    [
        ("invalid", {"raw-worker-prose-secret": "raw-worker-prose-secret"}),
        ("missing", None),
    ],
)
def test_worker_result_build_failure_is_bounded_and_emits_no_envelope(
    tmp_path: Path,
    case: str,
    input_payload: dict[str, str] | None,
) -> None:
    """Invalid input and missing ledger evidence never produce a partial result."""
    input_path = tmp_path / "result.json"
    payload = _completed_input().model_dump(mode="json") if case == "invalid" else {}
    if input_payload is not None:
        payload.update(input_payload)
    input_path.write_text(json.dumps(payload))
    stdout: list[str] = []
    stderr: list[str] = []

    def load_record(issue_iid: int) -> LedgerRecord | None:
        return _record(tmp_path) if case == "invalid" and issue_iid == 81 else None

    def changed_paths(worktree: Path) -> tuple[str, ...]:
        return ()

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
        ],
        load_record=load_record,  # type: ignore[arg-type]
        changed_paths=changed_paths,
        read_bytes=Path.read_bytes,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert len(stderr) == 1
    assert "raw-worker-prose-secret" not in stderr[0]
    assert len(stderr[0].encode("utf-8")) <= 2048


def test_worker_result_build_runtime_failure_is_sanitized_even_in_debug_mode(
    tmp_path: Path,
) -> None:
    """Default-safe diagnostics never expose a runtime boundary's detail."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    stderr: list[str] = []

    def fail_load(issue_iid: int) -> LedgerRecord | None:
        _ = issue_iid
        raise RuntimeError("untrusted runtime detail")

    def observe_paths(worktree: Path) -> tuple[str, ...]:
        _ = worktree
        return ()

    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
            "--debug",
        ],
        load_record=fail_load,
        changed_paths=observe_paths,
        read_bytes=Path.read_bytes,
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stderr == ["worker_result: could not be read or validated"]


def test_builder_runtime_uses_read_only_ledger_loading(
    tmp_path: Path,
) -> None:
    """The builder's default dependency composition never preserves corruption."""
    config = AgentWorkConfig(
        host="git.example.test",
        project_id=310967,
        checkout=tmp_path,
        state_root=tmp_path / "state",
        git_path=Path("/usr/bin/git"),
        glab_path=Path("/usr/bin/glab"),
        base_branch="agent/issue-81",
    )
    record_path = config.state_root / "records" / "81.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{")
    dependencies = build_worker_result_dependencies(config=config)

    with pytest.raises(LedgerCorrupt):
        dependencies.load_record(81)

    assert not (config.state_root / "recovery").exists()


def test_cli_default_builder_dependencies_do_not_preserve_corrupt_ledger(
    tmp_path: Path,
) -> None:
    """Normal CLI wiring retains corrupt-ledger reads as a no-write failure."""
    input_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(_completed_input().model_dump(mode="json")))
    state_root = tmp_path / "state" / "agent-work" / "projects" / "310967-e80a65b3aa73"
    record_path = state_root / "records" / "81.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{")
    environment = {
        "AGENT_WORK_BASE_BRANCH": "agent/issue-81",
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_GIT": "/usr/bin/git",
        "AGENT_WORK_GLAB": "/usr/bin/glab",
        "GITLAB_HOST": "git.example.test",
        "GITLAB_PROJECT_ID": "310967",
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-3:] == ("rev-parse", "--show-toplevel"):
            return CommandResult(0, f"{tmp_path}\n", "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, "https://git.example.test/group/project.git\n", "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "path_with_namespace": "group/project",
                    "http_url_to_repo": "https://git.example.test/group/project.git",
                }
            ),
            "",
        )

    stdout: list[str] = []
    stderr: list[str] = []
    exit_code = main(
        [
            "worker-result",
            "build",
            "--issue-iid",
            "81",
            "--input-file",
            str(input_path),
        ],
        environment=environment,
        run_command=run_command,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["worker_result: could not be read or validated"]
    assert not (state_root / "recovery").exists()
    assert not (state_root / "agent-work.log").exists()


def test_builder_derives_no_content_reviewers(tmp_path: Path) -> None:
    """Content audits retain no coding-reviewer receipts."""
    record = _record(tmp_path).model_copy(
        update={"workflow_lane": WorkflowLane.CONTENT}
    )
    receipts = tuple(
        f"{skill}: approved" for skill in WORKFLOW_RECEIPTS[WorkflowLane.CONTENT]
    )
    build_input = WorkerResultBuildInput.model_validate(
        {
            "workflow_lane": "content",
            "result_kind": "ready_to_publish",
            "receipts": receipts,
            "change_summary": "Completed content workflow.",
            "next_action": "Publish after validation.",
            "merge_request_draft": {"title": "Content", "description": "Ready."},
            "audit_summary": {
                "capacity": {
                    "required": 3,
                    "available": 16,
                    "shortfall": 0,
                    "decision": "run",
                },
            },
        }
    )

    result = build_worker_result(record, build_input, changed_paths=())

    assert result.audit_summary is not None
    assert result.audit_summary.reviewers == ()
