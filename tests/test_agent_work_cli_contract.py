# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Advisory CLI inspection consumes bounded grammar facts without executing work."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_work import cli
from agent_work.domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from agent_work.repository import CommandResult, RepositoryError

VERSION = "codex-cli 0.159.2\n"
ROOT_HELP = "Usage: codex [OPTIONS] [COMMAND]\nOptions:\n  --ask-for-approval <POLICY>\n  -c, --config <KEY=VALUE>\n  -h, --help\n"
EXEC_HELP = "Usage: codex exec [OPTIONS] [PROMPT] [COMMAND]\nOptions:\n  --sandbox <MODE>\n  --ignore-user-config\n  --strict-config\n  -C, --cd <DIR>\n  -h, --help\n"
SANDBOX_HELP = "Usage: codex sandbox [OPTIONS] [COMMAND]...\nOptions:\n  -C, --cd <DIR>\n  -c, --config <KEY=VALUE>\n  -h, --help\n"
SECRET = "secret-value-must-never-appear"


def _expected_commands(
    binary: Path, surface: str, checkout: Path
) -> tuple[tuple[str, ...], ...]:
    if surface == "exec":
        placement = (
            str(binary),
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            str(checkout),
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--strict-config",
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.unified_exec=false",
            "-c",
            "features.code_mode_host=false",
            "--help",
        )
    else:
        placement = (
            str(binary),
            "--ask-for-approval",
            "never",
            "sandbox",
            "-C",
            str(checkout),
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            'approval_policy="never"',
            "--help",
        )
    return (
        (str(binary), "--version"),
        (str(binary), "--help"),
        (str(binary), surface, "--help"),
        placement,
        (str(binary), "--version"),
    )


class _ProbeRunner:
    """Check the complete harmless argv allowlist at the subprocess boundary."""

    def __init__(
        self,
        binary: Path,
        surface: str,
        checkout: Path,
        outcomes: list[CommandResult | Exception] | None = None,
    ) -> None:
        self.expected = _expected_commands(binary, surface, checkout)
        help_text = EXEC_HELP if surface == "exec" else SANDBOX_HELP
        self.outcomes: list[CommandResult | Exception] = []
        if outcomes is None:
            self.outcomes.extend(
                CommandResult(0, text, "")
                for text in (VERSION, ROOT_HELP, help_text, help_text, VERSION)
            )
        else:
            self.outcomes.extend(outcomes)
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        assert command == self.expected[len(self.calls)]
        assert stdin is None
        assert (
            timeout_seconds is not None
            and 0 < timeout_seconds <= HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
        )
        assert environment is not None
        assert set(environment) <= {"HOME", "CODEX_HOME", "PATH"}
        assert Path(environment["HOME"]).is_dir()
        assert Path(environment["CODEX_HOME"]).is_dir()
        assert not list(Path(environment["HOME"]).iterdir())
        assert not list(Path(environment["CODEX_HOME"]).iterdir())
        assert current_directory is None or current_directory.is_absolute()
        self.calls.append(command)
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@contextmanager
def _empty_home(home: Path) -> Iterator[Path]:
    home.mkdir()
    yield home


@pytest.mark.parametrize("surface", ["exec", "sandbox"])
def test_supported_inspection_checks_exact_binary_and_all_five_probes(
    tmp_path: Path, surface: Literal["exec", "sandbox"]
) -> None:
    binary = tmp_path / "configured-codex"
    runner = _ProbeRunner(binary, surface, tmp_path)
    request = cli.CliContractRequest(
        executable=binary, surface=surface, checkout=tmp_path
    )
    report = cli.inspect_cli_contract(
        request,
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "supported"
    assert report.version == "0.159.2"
    assert report.checked_argv == tuple(runner.calls) == runner.expected
    assert report.diagnostics == ()
    assert report.effective_config == report.isolation == "unknown"
    assert report.evidence_limit == "accepted-output-only"


@pytest.mark.parametrize("surface", ["exec", "sandbox"])
@pytest.mark.parametrize("warning", ["a harmless warning", SECRET])
def test_help_warnings_and_repeated_flags_do_not_become_raw_report_evidence(
    tmp_path: Path, surface: Literal["exec", "sandbox"], warning: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, surface, tmp_path)
    runner.outcomes[1] = CommandResult(
        0, ROOT_HELP + "\n  --ask-for-approval <POLICY>\n", warning
    )
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface=surface, checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "supported"
    assert warning not in repr(report)
    assert ROOT_HELP not in repr(report)


@pytest.mark.parametrize(
    ("surface", "phase", "token"),
    [
        ("exec", 1, "--ask-for-approval"),
        ("exec", 1, "--config"),
        ("exec", 1, "--help"),
        ("exec", 2, "--sandbox"),
        ("exec", 2, "--ignore-user-config"),
        ("exec", 2, "--strict-config"),
        ("exec", 2, "--cd"),
        ("exec", 2, "--help"),
        ("sandbox", 2, "--cd"),
        ("sandbox", 2, "--config"),
        ("sandbox", 2, "--help"),
    ],
)
def test_missing_required_flag_or_lookalike_cannot_claim_supported(
    tmp_path: Path, surface: Literal["exec", "sandbox"], phase: int, token: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, surface, tmp_path)
    original = runner.outcomes[phase]
    assert isinstance(original, CommandResult)
    text = original.stdout.replace(token, token + "-lookalike")
    if token == "--config":
        text = text.replace("-c,", "-z,")
    if token == "--cd":
        text = text.replace("-C,", "-Z,")
    runner.outcomes[phase] = CommandResult(0, text, "")
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface=surface, checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "unsupported"
    assert report.diagnostics


@pytest.mark.parametrize("surface", ["exec", "sandbox"])
def test_rejected_global_option_placement_cannot_claim_supported(
    tmp_path: Path, surface: Literal["exec", "sandbox"]
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, surface, tmp_path)
    runner.outcomes[3] = CommandResult(2, "", "unexpected argument " + SECRET)
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface=surface, checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "unsupported"
    assert SECRET not in repr(report)
    assert report.diagnostics


@pytest.mark.parametrize(
    ("phase", "output"),
    [
        (0, ""),
        (0, "codex 0.159.2\n"),
        (0, VERSION + VERSION),
        (0, "codex-cli x.y.z\n"),
        (4, ""),
        (4, VERSION + VERSION),
        (1, ""),
        (1, "unrelated help"),
        (1, ROOT_HELP.replace("Usage: codex", "Usage: other")),
        (2, EXEC_HELP.replace("Usage: codex exec", "Usage: codex sandbox")),
        (2, ""),
        (3, "not help"),
        (2, EXEC_HELP + SANDBOX_HELP),
    ],
)
def test_empty_malformed_or_ambiguous_output_never_proves_grammar(
    tmp_path: Path, phase: int, output: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    runner.outcomes[phase] = CommandResult(0, output, "")
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "invalid_output"
    assert report.diagnostics


def test_subcommand_only_sandbox_help_is_unsupported(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "sandbox", tmp_path)
    runner.outcomes[2] = CommandResult(
        0,
        "Usage: codex sandbox [OPTIONS] <COMMAND>\nCommands:\n  linux\nOptions:\n -C, --cd <DIR>\n -c, --config <KEY=VALUE>\n --help\n",
        "",
    )
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="sandbox", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "unsupported"


def test_changed_sampled_version_invalidates_otherwise_supported_help(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    runner.outcomes[4] = CommandResult(0, "codex-cli 0.160.0\n", "")
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "version_changed"
    assert tuple(runner.calls) == runner.expected
    assert report.diagnostics


@pytest.mark.parametrize("phase", range(5))
@pytest.mark.parametrize("failure", ["missing", "timeout", "nonzero"])
def test_every_probe_failure_refuses_supported_and_suppresses_diagnostic_payload(
    tmp_path: Path, phase: int, failure: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    if failure == "missing":
        outcome = FileNotFoundError(SECRET)
    elif failure == "timeout":
        outcome = subprocess.TimeoutExpired("codex", 1, output=SECRET, stderr=SECRET)
    else:
        outcome = CommandResult(1, SECRET, SECRET)
    runner.outcomes[phase] = outcome
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state in {"unavailable", "unsupported"}
    assert report.diagnostics
    assert SECRET not in repr(report)
    assert report.checked_argv == tuple(runner.calls)


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("payload", ["x" * 65537, "é" * 32769])
def test_evidence_byte_limit_rejects_each_output_channel(
    tmp_path: Path, channel: str, payload: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    runner.outcomes[1] = CommandResult(
        0,
        payload if channel == "stdout" else ROOT_HELP,
        payload if channel == "stderr" else "",
    )
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "invalid_output"
    assert report.diagnostics
    assert payload not in repr(report)


def test_repository_boundary_error_is_sanitized_and_unavailable(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    runner.outcomes[0] = RepositoryError(SECRET)
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "unavailable"
    assert SECRET not in repr(report)


@pytest.mark.parametrize(
    "invalid", ["relative-executable", "relative-checkout", "surface", "extra"]
)
def test_strict_request_rejects_invalid_inputs_before_any_probe(
    tmp_path: Path, invalid: str
) -> None:
    payload: dict[str, object] = {
        "executable": tmp_path / "codex",
        "surface": "exec",
        "checkout": tmp_path,
    }
    if invalid == "relative-executable":
        payload["executable"] = Path("codex")
    elif invalid == "relative-checkout":
        payload["checkout"] = Path("checkout")
    elif invalid == "surface":
        payload["surface"] = "linux"
    else:
        payload["arbitrary_argv"] = ("dangerous",)
    with pytest.raises(ValueError):
        cli.CliContractRequest.model_validate(payload)


@pytest.mark.parametrize("phase", range(5))
@pytest.mark.parametrize("error_kind", ["decode", "unicode"])
def test_unicode_boundary_failure_is_invalid_output_and_stops_remaining_probes(
    tmp_path: Path, phase: int, error_kind: str
) -> None:
    binary = tmp_path / "codex"
    runner = _ProbeRunner(binary, "exec", tmp_path)
    runner.outcomes[phase] = (
        UnicodeDecodeError("utf-8", SECRET.encode(), 0, 1, SECRET)
        if error_kind == "decode"
        else UnicodeError(SECRET)
    )
    report = cli.inspect_cli_contract(
        cli.CliContractRequest(executable=binary, surface="exec", checkout=tmp_path),
        run_command=runner,
        isolated_home=lambda: _empty_home(tmp_path / "empty-home"),
    )
    assert report.state == "invalid_output"
    assert len(runner.calls) == phase + 1
    assert report.checked_argv == tuple(runner.calls)
    assert report.diagnostics
    assert SECRET not in repr(report)
    assert report.effective_config == report.isolation == "unknown"


@contextmanager
def _synthetic_home() -> Iterator[Path]:
    yield Path("/synthetic-empty-home")


def _generated_inspection(
    surface: Literal["exec", "sandbox"], version: str, help_text: str
):
    """Exercise public inspection with pure generated command-boundary evidence."""
    calls: list[tuple[str, ...]] = []
    responses = (version, ROOT_HELP, help_text, help_text, version)
    expected = _expected_commands(Path("/configured-codex"), surface, Path("/checkout"))

    def run_command(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResult:
        assert command == expected[len(calls)]
        assert stdin is None
        assert timeout_seconds is not None
        assert environment is not None
        assert current_directory is None or current_directory.is_absolute()
        assert (
            environment["HOME"] == environment["CODEX_HOME"] == "/synthetic-empty-home"
        )
        calls.append(command)
        return CommandResult(0, responses[len(calls) - 1], "")

    return cli.inspect_cli_contract(
        cli.CliContractRequest(
            executable=Path("/configured-codex"),
            surface=surface,
            checkout=Path("/checkout"),
        ),
        run_command=run_command,
        isolated_home=_synthetic_home,
    )


@settings(max_examples=20, deadline=None)
@given(
    surface=st.sampled_from(("exec", "sandbox")),
    components=st.tuples(st.integers(0, 999), st.integers(0, 999), st.integers(0, 999)),
    repetitions=st.integers(0, 3),
)
def test_generated_valid_version_and_repeated_help_flags_remain_advisory(
    surface: Literal["exec", "sandbox"],
    components: tuple[int, int, int],
    repetitions: int,
) -> None:
    semver = ".".join(map(str, components))
    text = (
        EXEC_HELP if surface == "exec" else SANDBOX_HELP
    ) + "  --help\n" * repetitions
    report = _generated_inspection(surface, f"codex-cli {semver}\n", text)
    assert report.state == "supported"
    assert report.version == semver
    assert report.diagnostics == ()
    assert report.effective_config == report.isolation == "unknown"
    assert report.evidence_limit == "accepted-output-only"


@settings(max_examples=20, deadline=None)
@given(
    flag=st.sampled_from(
        ("--sandbox", "--ignore-user-config", "--strict-config", "--cd", "--help")
    ),
    suffix=st.sampled_from(("", "-lookalike", "_lookalike", "extra")),
)
def test_generated_omitted_or_lookalike_help_flags_never_prove_support(
    flag: str, suffix: str
) -> None:
    text = EXEC_HELP.replace(flag, "" if not suffix else flag + suffix)
    if flag == "--cd":
        text = text.replace("-C,", "-Z,")
    report = _generated_inspection("exec", VERSION, text)
    assert report.state == "unsupported"
    assert report.diagnostics
    comparison = _generated_inspection(
        "exec", VERSION, EXEC_HELP.replace("--strict-config", "--strict-config-invalid")
    )
    assert report.diagnostics == comparison.diagnostics
    assert report.effective_config == report.isolation == "unknown"


@settings(max_examples=20, deadline=None)
@given(
    payload=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
        min_size=1,
        max_size=40,
    )
)
def test_generated_malformed_output_uses_fixed_diagnostics_and_unknown_semantics(
    payload: str,
) -> None:
    report = _generated_inspection(
        "exec", VERSION, "malformed-private-output:" + payload
    )
    comparison = _generated_inspection(
        "exec", VERSION, "malformed-private-output:other"
    )
    assert report.state == "invalid_output"
    assert report.diagnostics == comparison.diagnostics
    assert report.diagnostics
    assert "malformed-private-output:" not in repr(report)
    assert report.effective_config == report.isolation == "unknown"
