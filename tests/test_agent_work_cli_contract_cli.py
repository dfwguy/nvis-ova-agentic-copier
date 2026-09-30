# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The checkout CLI dispatches advisory inspection through its real subprocess seam."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Protocol, cast

import pytest

from agent_work.cli import build_parser, main
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult


class _ContractMain(Protocol):
    """Declare only the approved CLI inspection seam and existing injected ports."""

    def __call__(
        self,
        arguments: Sequence[str] | None = None,
        *,
        run_command: CommandRunner = ...,
        environment: Mapping[str, str] = ...,
        find_executable: Callable[[str], str | None] = ...,
        current_checkout: Callable[[], Path] = ...,
        write_stdout: Callable[[str], None] = ...,
        write_stderr: Callable[[str], None] = ...,
        cli_contract_home: Callable[[], AbstractContextManager[Path]] = ...,
    ) -> int:
        """Run CLI dispatch with a test-owned empty-home provider."""
        ...


_contract_main = cast(_ContractMain, main)


@contextmanager
def _fixture_home(root: Path) -> Iterator[Path]:
    home = root / "isolated-home"
    home.mkdir(exist_ok=True)
    yield home


ROOT_HELP = "Usage: codex [OPTIONS] [COMMAND]\nOptions:\n --ask-for-approval <POLICY>\n -c, --config <KEY=VALUE>\n --help\n"
EXEC_HELP = "Usage: codex exec [OPTIONS] [PROMPT] [COMMAND]\nOptions:\n --sandbox <MODE>\n --ignore-user-config\n --strict-config\n -C, --cd <DIR>\n --help\n"
SANDBOX_HELP = "Usage: codex sandbox [OPTIONS] [COMMAND]...\nOptions:\n -C, --cd <DIR>\n -c, --config <KEY=VALUE>\n --help\n"


@pytest.mark.parametrize("surface", ["exec", "sandbox"])
@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("selection", ["explicit", "configured", "path"])
def test_cli_dispatch_inspects_resolved_binary_once_without_operational_launch(
    tmp_path: Path, surface: str, *, apply: bool, selection: str
) -> None:
    binary = tmp_path / "selected-codex"
    other_binary = tmp_path / "other-codex"
    lookups: list[str] = []
    commands: list[tuple[str, ...]] = []
    outputs: list[str] = []
    environment = {
        "AGENT_WORK_AGENT_PATH": str(
            binary if selection == "configured" else other_binary
        ),
        "GITLAB_TOKEN": "private-secret",
        "CODEX_HOME": str(tmp_path / "private-home"),
    }
    if selection == "path":
        environment.pop("AGENT_WORK_AGENT_PATH")

    def resolve(name: str) -> str:
        lookups.append(name)
        return str(binary if len(lookups) == 1 else other_binary)

    def run_command(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        assert command[0] == str(binary)
        assert command[-1] in {"--version", "--help"}
        assert stdin is None
        assert timeout_seconds is not None and timeout_seconds > 0
        assert environment is not None
        assert set(environment) <= {"HOME", "CODEX_HOME", "PATH"}
        for key in ("HOME", "CODEX_HOME"):
            home = Path(environment[key])
            assert home != tmp_path / "private-home"
            assert home.is_dir() and not list(home.iterdir())
        assert not any("mcp_servers" in token for token in command)
        if "--ask-for-approval" in command:
            assert command.index("--ask-for-approval") < command.index(surface)
        assert "linux" not in command
        commands.append(command)
        if command[-1] == "--version":
            return CommandResult(0, "codex-cli 0.159.2\n", "")
        return CommandResult(
            0,
            (EXEC_HELP if surface == "exec" else SANDBOX_HELP)
            if surface in command
            else ROOT_HELP,
            "",
        )

    arguments = ["cli-contract", "inspect", "--surface", surface, "--json"]
    if apply:
        arguments.append("--apply")
    if selection == "explicit":
        arguments.extend(("--executable", str(binary)))
    exit_code = _contract_main(
        arguments,
        cli_contract_home=lambda: _fixture_home(tmp_path),
        run_command=run_command,
        environment=environment,
        find_executable=resolve,
        current_checkout=lambda: tmp_path,
        write_stdout=outputs.append,
        write_stderr=lambda _message: None,
    )
    assert exit_code == 0
    assert len(commands) == 5
    assert commands[0] == commands[-1] == (str(binary), "--version")
    assert commands[1] == (str(binary), "--help")
    assert commands[2] == (str(binary), surface, "--help")
    assert lookups == (["codex"] if selection == "path" else [])
    report = json.loads(outputs[0])
    assert report["state"] == "supported"
    assert report["version"] == "0.159.2"
    assert report["effective_config"] == report["isolation"] == "unknown"
    assert report["evidence_limit"] == "accepted-output-only"
    assert report["checked_argv"] == [list(command) for command in commands]
    assert "private-secret" not in outputs[0]


@pytest.mark.parametrize(
    "failure", ["unsupported", "unavailable", "version_changed", "invalid_output"]
)
def test_cli_inspection_failure_returns_one_without_echoing_process_text(
    tmp_path: Path, failure: str
) -> None:
    binary = tmp_path / "codex"
    outputs: list[str] = []
    errors: list[str] = []
    calls: list[tuple[str, ...]] = []
    secret = "raw-private-diagnostic"

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if failure == "unavailable":
            raise FileNotFoundError(secret)
        if command[-1] == "--version":
            return CommandResult(
                0,
                "codex-cli 0.160.0\n"
                if failure == "version_changed" and len(calls) == 5
                else "codex-cli 0.159.2\n",
                "",
            )
        if failure == "invalid_output":
            return CommandResult(0, secret, "")
        if failure == "unsupported" and "exec" in command:
            return CommandResult(2, secret, secret)
        return CommandResult(0, EXEC_HELP if "exec" in command else ROOT_HELP, "")

    exit_code = _contract_main(
        [
            "cli-contract",
            "inspect",
            "--surface",
            "exec",
            "--executable",
            str(binary),
            "--json",
        ],
        cli_contract_home=lambda: _fixture_home(tmp_path),
        run_command=run_command,
        environment={},
        current_checkout=lambda: tmp_path,
        write_stdout=outputs.append,
        write_stderr=errors.append,
    )
    assert exit_code == 1
    assert json.loads(outputs[0])["state"] == failure
    assert secret not in "".join((*outputs, *errors))


@pytest.mark.parametrize(
    "arguments",
    [
        ["cli-contract", "inspect"],
        ["cli-contract", "inspect", "--surface", "linux"],
        [
            "cli-contract",
            "inspect",
            "--surface",
            "exec",
            "--arbitrary-command",
            "false",
        ],
        [
            "cli-contract",
            "inspect",
            "--surface",
            "exec",
            "--executable",
            "relative-codex",
        ],
    ],
)
def test_invalid_cli_request_returns_usage_two_without_child_launch(
    arguments: list[str],
    tmp_path: Path,
) -> None:
    assert (
        build_parser()
        .parse_args(["cli-contract", "inspect", "--surface", "exec"])
        .resource
        == "cli-contract"
    )
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "unexpected", "")

    try:
        exit_code = _contract_main(
            arguments,
            cli_contract_home=lambda: _fixture_home(tmp_path),
            run_command=run_command,
            environment={},
            find_executable=lambda _name: "/codex",
            write_stdout=lambda _message: None,
            write_stderr=lambda _message: None,
        )
    except SystemExit as error:
        exit_code = error.code
    assert exit_code == 2
    assert calls == []


def test_cli_contract_help_is_registered_without_any_child_launch(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        _contract_main(
            ["cli-contract", "inspect", "--help"],
            cli_contract_home=lambda: _fixture_home(tmp_path),
        )
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "--surface" in output
    assert "--json" in output
    assert "--apply" in output
    assert "--executable" in output


@pytest.mark.parametrize("phase", range(5))
@pytest.mark.parametrize("error_kind", ["decode", "unicode"])
@pytest.mark.parametrize("debug", [False, True])
def test_cli_unicode_failure_reports_invalid_output_without_traceback_or_next_probe(
    tmp_path: Path, phase: int, error_kind: str, *, debug: bool
) -> None:
    calls: list[tuple[str, ...]] = []
    outputs: list[str] = []
    errors: list[str] = []
    secret = "private-decoding-error"

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if len(calls) == phase + 1:
            if error_kind == "decode":
                raise UnicodeDecodeError("utf-8", secret.encode(), 0, 1, secret)
            raise UnicodeError(secret)
        return CommandResult(
            0,
            "codex-cli 0.159.2\n"
            if command[-1] == "--version"
            else EXEC_HELP
            if "exec" in command
            else ROOT_HELP,
            "",
        )

    arguments = [
        "cli-contract",
        "inspect",
        "--surface",
        "exec",
        "--executable",
        str(tmp_path / "codex"),
        "--json",
    ]
    if debug:
        arguments.append("--debug")
    exit_code = _contract_main(
        arguments,
        cli_contract_home=lambda: _fixture_home(tmp_path),
        run_command=run_command,
        environment={},
        current_checkout=lambda: tmp_path,
        write_stdout=outputs.append,
        write_stderr=errors.append,
    )
    assert exit_code == 1
    assert len(calls) == phase + 1
    assert json.loads(outputs[0])["state"] == "invalid_output"
    assert json.loads(outputs[0])["diagnostics"]
    assert errors == []
    assert secret not in "".join(outputs)
    assert "Traceback" not in "".join(outputs)


@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("debug", [False, True])
@pytest.mark.parametrize(
    "failure", ["unsupported", "unavailable", "version_changed", "invalid_output"]
)
def test_failed_reports_use_human_stderr_or_json_stdout_with_safe_debug_diagnostics(
    tmp_path: Path, failure: str, *, json_output: bool, debug: bool
) -> None:
    outputs: list[str] = []
    errors: list[str] = []
    calls: list[tuple[str, ...]] = []
    secret = "private-child-output"

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if failure == "unavailable":
            raise FileNotFoundError(secret)
        if command[-1] == "--version":
            version = (
                "0.160.0"
                if failure == "version_changed" and len(calls) == 5
                else "0.159.2"
            )
            return CommandResult(0, f"codex-cli {version}\n", "")
        if failure == "invalid_output":
            return CommandResult(0, secret, secret)
        if failure == "unsupported" and "exec" in command:
            return CommandResult(2, secret, secret)
        return CommandResult(0, EXEC_HELP if "exec" in command else ROOT_HELP, "")

    arguments = [
        "cli-contract",
        "inspect",
        "--surface",
        "exec",
        "--executable",
        str(tmp_path / "codex"),
    ]
    if json_output:
        arguments.append("--json")
    if debug:
        arguments.append("--debug")
    exit_code = _contract_main(
        arguments,
        cli_contract_home=lambda: _fixture_home(tmp_path),
        run_command=run_command,
        environment={},
        current_checkout=lambda: tmp_path,
        write_stdout=outputs.append,
        write_stderr=errors.append,
    )
    assert exit_code == 1
    if json_output:
        assert errors == []
        assert json.loads(outputs[0])["state"] == failure
        assert json.loads(outputs[0])["diagnostics"]
    else:
        assert outputs == []
        assert errors
    rendered = "".join((*outputs, *errors))
    assert secret not in rendered
    assert "Traceback" not in rendered


@pytest.mark.parametrize("json_output", [False, True])
def test_supported_reports_use_stdout_and_leave_stderr_empty(
    tmp_path: Path, *, json_output: bool
) -> None:
    outputs: list[str] = []
    errors: list[str] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            "codex-cli 0.159.2\n"
            if command[-1] == "--version"
            else EXEC_HELP
            if "exec" in command
            else ROOT_HELP,
            "",
        )

    arguments = [
        "cli-contract",
        "inspect",
        "--surface",
        "exec",
        "--executable",
        str(tmp_path / "codex"),
    ]
    if json_output:
        arguments.append("--json")
    exit_code = _contract_main(
        arguments,
        cli_contract_home=lambda: _fixture_home(tmp_path),
        run_command=run_command,
        environment={},
        current_checkout=lambda: tmp_path,
        write_stdout=outputs.append,
        write_stderr=errors.append,
    )
    assert exit_code == 0
    assert errors == []
    assert outputs
    if json_output:
        assert json.loads(outputs[0])["state"] == "supported"
    else:
        assert "supported" in outputs[0]
