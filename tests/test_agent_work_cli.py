"""Behavior contracts for the recoverable CLI surface."""

from __future__ import annotations

import json

import pytest

from agent_work.cli import build_parser, main
from agent_work.ledger import LockBusy
from agent_work.models import CommandReport


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
