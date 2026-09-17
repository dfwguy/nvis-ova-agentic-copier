# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the recoverable CLI surface."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import _render, build_parser, main
from agent_work.domain import CommandRequest, RunnerState, WorkflowPhase, WorkKind
from agent_work.ledger import LockBusy
from agent_work.models import (
    CommandItemReport,
    CommandReport,
    ConditionEvidence,
    PrerequisiteLabelProjectionReport,
    RecoveryCandidateReport,
    RecoverySummary,
)
from agent_work.repository import CommandResult
from agent_work.upstream_finding import UpstreamFindingPreview


def test_human_status_renders_one_concise_answer_line_for_each_item() -> None:
    def item(iid: int, state: RunnerState, summary: str) -> CommandItemReport:
        return CommandItemReport(
            issue_iid=iid,
            state=state,
            phase=WorkflowPhase.CODING_GREEN,
            work_kind=WorkKind.ORDINARY,
            attempt=1,
            retries_remaining=3,
            source_branch=f"agent/issue-{iid}",
            source_sha="a" * 40,
            worktree=Path(f"/state/worktrees/{iid}"),
            summary=summary,
            current_condition=(
                ConditionEvidence(code="failed_condition", detail="blocked\nunsafe")
                if state is RunnerState.FAILED
                else None
            ),
            next_action="Start a fresh whole workflow and repeat validation.",
        )

    rendered = _render(
        CommandReport(
            command="loop.status",
            applied=False,
            outcome="status",
            items=(
                item(12, RunnerState.EXECUTING, "changed=1 validated=passed"),
                item(13, RunnerState.FAILED, "changed=0 validated=failed"),
            ),
        ),
        as_json=False,
    )

    lines = rendered.splitlines()
    assert len(lines) == 2
    assert "issue=#12" in lines[0] and "running" in lines[0]
    assert "changed=1" in lines[0] and "validated=passed" in lines[0]
    assert "issue=#13" in lines[1] and "failed" in lines[1]
    assert "changed=0" in lines[1] and "validated=failed" in lines[1]
    assert "current=failed_condition:blocked unsafe" in lines[1]
    assert all("next=" in line for line in lines)


def test_recovery_inventory_v2_json_and_human_output_are_compact_and_sanitized() -> (
    None
):
    """Inventory output exposes actionable state without leaking multiline evidence."""
    candidate = RecoveryCandidateReport(
        issue_iid=12,
        classification="needs_human_decision",
        issue_state="opened",
        priority=0,
        recommendation="ignored legacy recommendation",
        active=True,
        human_action_required=True,
        blocked_cause="pipeline evidence disagrees",
        dependency_explanation="trusted prerequisite #9 remains open",
        next_action="Review issue #12 before selecting one resolution.",
    )
    report = CommandReport(
        schema_version=2,
        command="recovery.inventory",
        applied=False,
        outcome="inventory",
        recovery_summary=RecoverySummary(
            total=4,
            open=3,
            closed=1,
            active=2,
            blocked=1,
            dependency_gated=1,
            human_action=1,
            no_action=3,
        ),
        recovery_candidates=(candidate,),
    )

    payload = json.loads(_render(report, as_json=True))
    rendered = _render(report, as_json=False)

    assert payload["schema_version"] == 2
    assert payload["recovery_summary"]["human_action"] == 1
    assert payload["recovery_candidates"][0]["next_action"].startswith("Review")
    assert "human_action_required=true" in rendered
    assert "next_action=Review issue #12 before selecting one resolution." in rendered
    assert "\n" not in rendered and "recommendation=" not in rendered


def test_human_prerequisite_projection_labels_are_single_line() -> None:
    """GitLab-controlled display labels cannot split human command output."""
    report = CommandReport(
        command="loop.run",
        applied=False,
        outcome="idle",
        prerequisite_projections=(
            PrerequisiteLabelProjectionReport(
                issue_iid=12,
                current_labels=("customer::priority\nunsafe",),
                intended_labels=("agent::prereq_37\r\nunsafe",),
            ),
        ),
    )

    rendered = _render(report, as_json=False)

    assert "\n" not in rendered and "\r" not in rendered
    assert "current_labels=customer::priority unsafe" in rendered
    assert "intended_labels=agent::prereq_37 unsafe" in rendered


def test_worker_exit_evidence_is_projected_to_json_and_human_output() -> None:
    item = CommandItemReport.model_validate(
        {
            "issue_iid": 12,
            "state": "retry-wait",
            "phase": "coding-green",
            "work_kind": "ordinary",
            "attempt": 2,
            "retries_remaining": 2,
            "source_branch": "agent/issue-12",
            "worktree": "/tmp/issue-12",
            "worker_exit": {"returncode": -15, "diagnostic": "transport closed"},
        }
    )
    report = CommandReport(
        command="loop.run", applied=True, outcome="activation_complete", items=(item,)
    )

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        assert apply
        return report

    output: list[str] = []
    assert (
        main(
            ["loop", "run", "--apply", "--json"],
            dispatch=dispatch,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output[0])["items"][0]["worker_exit"] == {
        "returncode": -15,
        "diagnostic": "transport closed",
    }
    output.clear()
    assert (
        main(["loop", "run", "--apply"], dispatch=dispatch, write_stdout=output.append)
        == 0
    )
    assert "returncode=-15" in output[0]
    assert "worker_diagnostic=transport closed" in output[0]


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "run"],
        ["loop", "status"],
        ["loop", "retry", "--issue-iid", "12"],
        ["migration", "preview"],
        ["migration", "apply", "--plan-file", "plan.json"],
        ["recovery", "inventory", "--issue-iid", "12", "--json", "--apply"],
        [
            "recovery",
            "resolve",
            "--issue-iid",
            "12",
            "--choice",
            "retry",
            "--reason",
            "trusted prerequisite is closed",
        ],
    ],
)
def test_parser_exposes_the_nested_resource_action_contract(
    arguments: list[str],
) -> None:
    assert build_parser().parse_args(arguments)


def test_credential_rotate_parser_requires_noninteractive_target_inputs() -> None:
    parsed = build_parser().parse_args(
        [
            "credential",
            "rotate",
            "--environment-file",
            "/etc/agent-work.env",
            "--unit-name",
            "agent-work",
            "--apply",
            "--json",
            "--debug",
        ]
    )

    assert parsed.resource == "credential"
    assert parsed.action == "rotate"
    assert parsed.environment_file == Path("/etc/agent-work.env")
    assert parsed.unit_name == "agent-work"
    assert parsed.apply is True
    assert parsed.json is True
    assert parsed.debug is True


def test_bare_loop_run_is_a_read_only_preview() -> None:
    seen: list[bool] = []

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        seen.append(apply)
        return CommandReport(command="loop.run", applied=apply, outcome="idle")

    output: list[str] = []
    assert (
        main(["loop", "run", "--json"], dispatch=dispatch, write_stdout=output.append)
        == 0
    )
    assert seen == [False]
    assert json.loads(output[0])["applied"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "canary"],
        ["loop", "canary", "--json"],
        ["loop", "canary", "--apply", "--plan-file", "canary-plan.json"],
        ["loop", "recover", "--issue-iid", "76"],
        [
            "loop",
            "recover",
            "--issue-iid",
            "76",
            "--apply",
            "--plan-file",
            "recover-76.json",
            "--max-segments",
            "2",
            "--max-activation-seconds",
            "120",
        ],
    ],
)
def test_operational_plan_commands_expose_the_plan_bound_cli_contract(
    arguments: list[str],
) -> None:
    """Canary and recovery keep preview harmless and bind apply to a plan file."""
    assert build_parser().parse_args(arguments)


def test_recover_help_explains_the_bounded_activation_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Recovery help documents both reviewed execution bounds and their defaults."""
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["loop", "recover", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())

    assert "maximum recovery segments per applied activation (default: 16)" in help_text
    assert "maximum seconds for one applied activation (default: 10800)" in help_text


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "canary", "--apply"],
        ["loop", "recover", "--issue-iid", "76", "--apply"],
    ],
)
def test_operational_apply_requires_a_reviewed_plan_before_dispatch(
    arguments: list[str],
) -> None:
    """An apply invocation without its preview plan is CLI misuse, never a mutation."""
    dispatched = False
    errors: list[str] = []

    def dispatch_must_not_run(_request: object, *, apply: bool) -> CommandReport:
        nonlocal dispatched
        del apply
        dispatched = True
        raise AssertionError("planless apply must be rejected before dispatch")

    assert (
        main(
            arguments,
            dispatch=dispatch_must_not_run,
            write_stderr=errors.append,
        )
        == 2
    )
    assert not dispatched
    assert errors and "plan-file" in errors[0]


def test_canary_default_preview_and_recover_apply_preserve_exact_request_boundary() -> (
    None
):
    """The CLI supplies a read-only canary and one exact recovery target to DI."""
    seen: list[tuple[CommandRequest, bool]] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        typed = cast(CommandRequest, request)
        seen.append((typed, apply))
        return CommandReport(
            command=typed.command,
            applied=apply,
            outcome="checkpoint",
        )

    preview: list[str] = []
    assert (
        main(
            ["loop", "canary", "--json"],
            dispatch=dispatch,
            write_stdout=preview.append,
        )
        == 0
    )
    assert json.loads(preview[0])["applied"] is False
    assert seen[0] == (CommandRequest(command="loop.canary"), False)

    assert (
        main(
            [
                "loop",
                "recover",
                "--issue-iid",
                "76",
                "--apply",
                "--plan-file",
                "recover-76.json",
                "--max-segments",
                "3",
                "--max-activation-seconds",
                "180",
            ],
            dispatch=dispatch,
        )
        == 0
    )
    assert seen[1] == (
        CommandRequest(
            command="loop.recover",
            issue_iid=76,
            plan_file=Path("recover-76.json"),
            max_segments=3,
            max_activation_seconds=180,
        ),
        True,
    )


@pytest.mark.parametrize("action", ["canary", "recover"])
def test_operational_plan_command_help_describes_preview_and_plan_bound_apply(
    action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["loop", action]
    if action == "recover":
        arguments.extend(["--issue-iid", "76"])
    arguments.append("--help")

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(arguments)

    help_text = " ".join(capsys.readouterr().out.split()).lower()
    assert raised.value.code == 0
    assert "preview" in help_text
    assert "plan-file" in help_text
    assert "apply" in help_text


def test_apply_flag_reaches_the_single_dispatch_boundary() -> None:
    seen: list[tuple[CommandRequest, bool]] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        seen.append((cast(CommandRequest, request), apply))
        return CommandReport(
            command="loop.run", applied=apply, outcome="segment_complete"
        )

    assert main(["loop", "run", "--apply"], dispatch=dispatch) == 0
    assert seen[0][1] is True
    assert seen[0][0].max_activation_seconds == 10_800


def test_loop_run_exposes_bounded_activation_controls() -> None:
    defaults = build_parser().parse_args(["loop", "run"])
    seen: list[CommandRequest] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        assert apply
        seen.append(cast(CommandRequest, request))
        return CommandReport(
            command="loop.run",
            applied=True,
            outcome="activation_complete",
            segments_run=3,
            stop_reason="segment_limit",
        )

    output: list[str] = []
    result = main(
        [
            "loop",
            "run",
            "--apply",
            "--json",
            "--max-segments",
            "3",
            "--max-activation-seconds",
            "120",
        ],
        dispatch=dispatch,
        write_stdout=output.append,
    )

    assert result == 0
    assert defaults.max_segments == 16
    assert defaults.max_activation_seconds == 10_800
    assert seen[0].max_segments == 3
    assert seen[0].max_activation_seconds == 120
    assert json.loads(output[0]) == {
        "schema_version": 1,
        "command": "loop.run",
        "applied": True,
        "outcome": "activation_complete",
        "items": [],
        "actions": [],
        "segments_run": 3,
        "stop_reason": "segment_limit",
    }


def test_loop_run_help_documents_preview_apply_and_default_bounds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", "--help"])

    captured = capsys.readouterr()
    help_text = " ".join(captured.out.split())
    assert raised.value.code == 0
    assert "Preview the next safe action without mutation" in help_text
    assert "omit for a read-only preview" in help_text
    assert (
        "maximum persisted workflow attempts in one applied activation (default: 16)"
        in help_text
    )
    assert "maximum seconds for one applied activation (default: 10800)" in help_text
    assert "--timeout-seconds" not in help_text
    assert captured.err == ""


@pytest.mark.parametrize(
    ("flag", "sentinel"),
    [
        ("--max-segments", 17),
        ("--max-activation-seconds", 3601),
    ],
)
def test_loop_run_help_interpolates_the_current_activation_limit_default(
    flag: str, sentinel: int
) -> None:
    parser = build_parser()
    resources = cast(
        Any,
        next(action for action in parser._actions if getattr(action, "choices", None)),
    )
    loop = resources.choices["loop"]
    loop_actions = next(
        action for action in loop._actions if getattr(action, "choices", None)
    )
    run = loop_actions.choices["run"]
    activation_limit = next(
        action for action in run._actions if flag in action.option_strings
    )
    activation_limit.default = sentinel

    help_text = " ".join(run.format_help().split())

    assert f"(default: {sentinel})" in help_text


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-segments", "0"),
        ("--max-segments", "-1"),
        ("--max-segments", "not-an-integer"),
        ("--max-activation-seconds", "0"),
        ("--max-activation-seconds", "-1"),
        ("--max-activation-seconds", "not-an-integer"),
    ],
)
def test_loop_run_rejects_invalid_activation_bounds(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", flag, value])

    assert raised.value.code == 2


def test_loop_run_rejects_the_removed_per_session_timeout_option() -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", "--timeout-seconds", "1800"])

    assert raised.value.code == 2


def test_command_request_defaults_to_one_three_hour_activation_budget() -> None:
    request = CommandRequest(command="loop.run")

    assert request.max_activation_seconds == 10_800
    assert not hasattr(request, "timeout_seconds")


def test_human_output_reports_the_final_activation_checkpoint() -> None:
    def item(state: RunnerState, phase: WorkflowPhase) -> CommandItemReport:
        return CommandItemReport(
            issue_iid=12,
            state=state,
            phase=phase,
            work_kind=WorkKind.ORDINARY,
            attempt=1,
            retries_remaining=3,
            source_branch="agent/issue-12",
            worktree=Path("/tmp/issue-12"),
            next_action=f"Continue from {phase}.",
        )

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        assert apply
        return CommandReport(
            command="loop.run",
            applied=True,
            outcome="activation_complete",
            items=(
                item(RunnerState.EXECUTING, WorkflowPhase.CODING_RED_DOCS),
                item(RunnerState.AWAITING_HUMAN, WorkflowPhase.POST_PUBLICATION),
            ),
            segments_run=2,
            stop_reason="awaiting_human",
        )

    output: list[str] = []
    assert (
        main(
            ["loop", "run", "--apply"],
            dispatch=dispatch,
            write_stdout=output.append,
        )
        == 0
    )
    assert output == [
        (
            "activation_complete stop_reason=awaiting_human issue=#12 "
            "state=awaiting-human phase=post-publication "
            "next_action=Continue from post-publication."
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ledger unavailable"),
        ValueError("migration plan invalid"),
        OSError("migration plan unreadable"),
    ],
)
def test_unrecordable_boundary_failure_returns_one(failure: Exception) -> None:
    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        del apply
        raise failure

    errors: list[str] = []
    assert main(["loop", "status"], dispatch=dispatch, write_stderr=errors.append) == 1
    assert errors == [str(failure)]


def test_invalid_cli_input_returns_two() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["loop", "retry"])
    assert raised.value.code == 2


def test_recovery_resolve_apply_requires_the_exact_reviewed_plan_file() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(
            [
                "recovery",
                "resolve",
                "--issue-iid",
                "12",
                "--choice",
                "retry",
                "--reason",
                "trusted prerequisite is closed",
                "--apply",
            ]
        )

    assert raised.value.code == 2


def test_lock_contention_returns_temporary_exit_75() -> None:
    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        del apply
        raise LockBusy("another segment owns the project lock")

    errors: list[str] = []
    assert (
        main(["loop", "run", "--apply"], dispatch=dispatch, write_stderr=errors.append)
        == 75
    )
    assert errors == ["another segment owns the project lock"]


def test_upstream_finding_preview_is_local_json_and_bypasses_runtime_dispatch() -> None:
    output: list[str] = []
    seen_paths: list[Path] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for a local preview")

    def preview_manifest(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        seen_paths.append(path)
        assert callable(read_bytes)
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--json",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=preview_manifest,
            preview_upstream=lambda preview, **_kwargs: preview,
            read_bytes=lambda _path: b"unused",
            write_stdout=output.append,
        )
        == 0
    )
    assert seen_paths == [Path("finding.yaml")]
    rendered = json.loads(output[0])
    assert set(rendered) == {
        "schema_version",
        "command",
        "outcome",
        "project_fingerprint",
        "revision_fingerprint",
        "semantic_finding_fingerprint",
        "copied_path_count",
        "evidence_link_count",
    }
    assert rendered["semantic_finding_fingerprint"] == "c" * 64
    assert "src/widget.py" not in output[0] and "high" not in output[0]


def test_upstream_finding_preview_help_and_harmless_apply_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        build_parser().parse_args(["upstream-finding", "preview", "--help"])
    help_text = capsys.readouterr().out
    assert help_exit.value.code == 0
    assert (
        "read-only" in help_text.lower()
        and "GitLab" in help_text
        and "write" in help_text.lower()
    )
    parsed = build_parser().parse_args(
        [
            "upstream-finding",
            "preview",
            "--manifest-file",
            "finding.yaml",
            "--upstream-host",
            "upstream.example.com",
            "--upstream-project-id",
            "77",
            "--upstream-credential-file",
            "/dedicated/token",
            "--apply",
        ]
    )
    assert parsed.apply is True


def test_upstream_finding_preview_apply_remains_local_and_bypasses_dispatch() -> None:
    """The consistency flag cannot turn the local preview into an operation."""
    output: list[str] = []
    read_paths: list[Path] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for a local preview")

    def read_bytes(path: Path) -> bytes:
        read_paths.append(path)
        return b"manifest bytes"

    def preview_manifest(
        path: Path, *, read_bytes: Callable[[Path], bytes]
    ) -> UpstreamFindingPreview:
        assert read_bytes(path) == b"manifest bytes"
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--apply",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=preview_manifest,
            preview_upstream=lambda preview, **_kwargs: preview,
            read_bytes=read_bytes,
            write_stdout=output.append,
        )
        == 0
    )
    assert read_paths == [Path("finding.yaml")]
    assert output == [
        "outcome=valid project_fingerprint="
        + "a" * 64
        + " revision_fingerprint="
        + "b" * 64
        + " semantic_finding_fingerprint="
        + "c" * 64
        + " paths=1 evidence_links=1"
    ]


def test_upstream_finding_preview_renders_safe_human_summary_and_sanitizes_errors() -> (
    None
):
    output: list[str] = []
    errors: list[str] = []

    def preview_success(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=2,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=preview_success,
            preview_upstream=lambda preview, **_kwargs: preview,
            write_stdout=output.append,
        )
        == 0
    )
    assert "valid" in output[0]
    assert "a" * 64 in output[0]
    assert "paths=2" in output[0] and "evidence_links=1" in output[0]
    assert all(
        value not in output[0] for value in ("src/widget.py", "high", "medium", "both")
    )

    def preview_failure(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        from agent_work.upstream_finding import ManifestError

        raise ManifestError("finding.validation_evidence is invalid")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=preview_failure,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["finding.validation_evidence is invalid"]


def test_upstream_finding_preview_requires_a_manifest_file_and_returns_one_for_read_failure() -> (
    None
):
    with pytest.raises(SystemExit) as missing_file:
        build_parser().parse_args(["upstream-finding", "preview"])
    assert missing_file.value.code == 2

    errors: list[str] = []

    def unreadable(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        from agent_work.upstream_finding import ManifestError

        raise ManifestError("manifest file could not be read")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=unreadable,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["manifest file could not be read"]


def test_upstream_finding_preview_requires_explicit_upstream_boundary_and_apply_stays_read_only() -> (
    None
):
    """Preview takes no generated-project identity and never reaches dispatch."""
    with pytest.raises(SystemExit) as missing_upstream:
        build_parser().parse_args(
            ["upstream-finding", "preview", "--manifest-file", "finding.yaml"]
        )
    assert missing_upstream.value.code == 2

    output: list[str] = []
    calls: list[tuple[str, int, Path]] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for an upstream preview")

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def upstream_preview(
        preview: UpstreamFindingPreview,
        *,
        host: str,
        project_id: int,
        credential_file: Path,
        read_credential: object,
        run_command: object,
        environment: object,
    ) -> UpstreamFindingPreview:
        assert preview.outcome == "valid"
        assert callable(read_credential) and callable(run_command)
        assert isinstance(environment, dict)
        calls.append((host, project_id, credential_file))
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="match",
            project_fingerprint=preview.project_fingerprint,
            revision_fingerprint=preview.revision_fingerprint,
            semantic_finding_fingerprint=preview.semantic_finding_fingerprint,
            copied_path_count=preview.copied_path_count,
            evidence_link_count=preview.evidence_link_count,
            matching_iid=23,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--apply",
                "--json",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=local_preview,
            preview_upstream=upstream_preview,
            read_bytes=lambda _path: b"unused",
            read_credential=lambda _path: b"dedicated-token",
            run_command=lambda *_args, **_kwargs: CommandResult(0, "", ""),
            environment={"GITLAB_TOKEN": "generated-token"},
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [("upstream.example.com", 77, Path("/dedicated/token"))]
    rendered = json.loads(output[0])
    assert rendered["outcome"] == "match"
    assert rendered["matching_iid"] == 23


def test_upstream_finding_preview_composes_the_default_read_only_boundary() -> None:
    """The CLI wires only the dedicated token into its GET-only reader."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    credential_paths: list[Path] = []
    output: list[str] = []

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        response = (
            '{"id":77,"issues_enabled":true,"permissions":'
            '{"project_access":{"access_level":20},"group_access":null}}'
            if command[-1] == "projects/77"
            else "[]"
        )
        return CommandResult(0, response, "")

    def read_credential(path: Path) -> bytes:
        credential_paths.append(path)
        return b"dedicated-token"

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for an upstream preview")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=local_preview,
            read_credential=read_credential,
            run_command=run,
            environment={"GITLAB_TOKEN": "generated-token"},
            find_executable=lambda name: "/opt/bin/glab" if name == "glab" else None,
            write_stdout=output.append,
        )
        == 0
    )
    assert credential_paths == [Path("/dedicated/token")]
    assert "outcome=would-create" in output[0]
    assert [command[-1] for command, _kwargs in calls] == [
        "projects/77",
        "projects/77/issues?state=opened&per_page=100&page=1",
    ]
    for command, kwargs in calls:
        assert command[1:6] == (
            "api",
            "--hostname",
            "upstream.example.com",
            "-X",
            "GET",
        )
        assert kwargs["environment"] == {"GITLAB_TOKEN": "dedicated-token"}


def test_upstream_finding_preview_maps_sanitized_reader_failure_to_one_and_hides_absent_iid() -> (
    None
):
    """The read boundary has distinct syntax, operational, and success exits."""
    from agent_work.upstream_gitlab import UpstreamGitLabError

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def failure(*_args: object, **_kwargs: object) -> UpstreamFindingPreview:
        raise UpstreamGitLabError("upstream access failed")

    arguments = [
        "upstream-finding",
        "preview",
        "--manifest-file",
        "finding.yaml",
        "--upstream-host",
        "upstream.example.com",
        "--upstream-project-id",
        "77",
        "--upstream-credential-file",
        "/dedicated/token",
    ]
    errors: list[str] = []
    assert (
        main(
            arguments,
            preview_manifest=local_preview,
            preview_upstream=failure,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["upstream access failed"]
    with pytest.raises(UpstreamGitLabError):
        main(
            [*arguments, "--debug"],
            preview_manifest=local_preview,
            preview_upstream=failure,
        )

    def would_create(*_args: object, **_kwargs: object) -> UpstreamFindingPreview:
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="would-create",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    output: list[str] = []
    assert (
        main(
            arguments,
            preview_manifest=local_preview,
            preview_upstream=would_create,
            write_stdout=output.append,
        )
        == 0
    )
    assert "outcome=would-create" in output[0]
    assert "matching_iid" not in output[0]
