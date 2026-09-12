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
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import build_parser, main
from agent_work.domain import CommandRequest, RunnerState, WorkflowPhase, WorkKind
from agent_work.ledger import LockBusy
from agent_work.models import CommandItemReport, CommandReport


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "run"],
        ["loop", "status"],
        ["loop", "retry", "--issue-iid", "12"],
        ["migration", "preview"],
        ["migration", "apply", "--plan-file", "plan.json"],
    ],
)
def test_parser_exposes_the_nested_resource_action_contract(
    arguments: list[str],
) -> None:
    assert build_parser().parse_args(arguments)


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


def test_apply_flag_reaches_the_single_dispatch_boundary() -> None:
    seen: list[bool] = []

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        seen.append(apply)
        return CommandReport(
            command="loop.run", applied=apply, outcome="segment_complete"
        )

    assert main(["loop", "run", "--apply"], dispatch=dispatch) == 0
    assert seen == [True]


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
    assert defaults.timeout_seconds == 1800
    assert defaults.max_segments == 16
    assert defaults.max_activation_seconds == 3600
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
    assert "maximum seconds for each workflow session (default: 1800)" in help_text
    assert (
        "maximum persisted workflow attempts in one applied activation (default: 16)"
        in help_text
    )
    assert "maximum seconds for one applied activation (default: 3600)" in help_text
    assert captured.err == ""


@pytest.mark.parametrize(
    ("flag", "sentinel"),
    [
        ("--timeout-seconds", 1801),
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
