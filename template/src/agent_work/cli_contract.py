# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Advisory inspection of fixed, harmless installed Codex CLI grammar probes."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import StrictStr, field_validator

from .domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from .models import BoundaryModel
from .ports import CommandRunner
from .repository import RepositoryError

_CliSurface = Literal["exec", "sandbox"]
_CliState = Literal[
    "supported", "unsupported", "unavailable", "invalid_output", "version_changed"
]
_CliDiagnostic = Literal[
    "CLI probe unavailable",
    "CLI grammar unsupported",
    "CLI output invalid",
    "Sampled CLI version changed",
]
_MAX_EVIDENCE_BYTES = 65536


class CliContractRequest(BoundaryModel):
    """Explicit executable, supported surface, and checkout for grammar inspection."""

    executable: Path
    surface: _CliSurface
    checkout: Path

    @field_validator("executable", "checkout")
    @classmethod
    def _absolute_path(cls, path: Path) -> Path:
        if not path.is_absolute():
            raise ValueError("CLI contract paths must be absolute")
        return path


class CliContractReport(BoundaryModel):
    """Parsed grammar evidence without raw command output or isolation claims."""

    executable: Path
    surface: _CliSurface
    checkout: Path
    state: _CliState
    version: StrictStr | None
    checked_argv: tuple[tuple[StrictStr, ...], ...]
    diagnostics: tuple[_CliDiagnostic, ...]
    effective_config: Literal["unknown"] = "unknown"
    isolation: Literal["unknown"] = "unknown"
    evidence_limit: Literal["accepted-output-only"] = "accepted-output-only"


@contextmanager
def empty_cli_home() -> Iterator[Path]:
    """Provide one temporary empty home without reading existing configuration."""
    with TemporaryDirectory(prefix="agent-work-cli-contract-") as directory:
        yield Path(directory)


def _probe_commands(request: CliContractRequest) -> tuple[tuple[str, ...], ...]:
    executable = str(request.executable)
    if request.surface == "exec":
        placement = (
            executable,
            "--ask-for-approval",
            "never",
            "exec",
            "-C",
            str(request.checkout),
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
            executable,
            "--ask-for-approval",
            "never",
            "sandbox",
            "-C",
            str(request.checkout),
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            'approval_policy="never"',
            "--help",
        )
    return (
        (executable, "--version"),
        (executable, "--help"),
        (executable, request.surface, "--help"),
        placement,
        (executable, "--version"),
    )


def _version(output: str) -> str | None:
    match = re.fullmatch(r"codex-cli ([0-9]+\.[0-9]+\.[0-9]+)\s*", output)
    return match[1] if match else None


def _help_state(output: str, surface: _CliSurface | None) -> _CliState:
    usages = tuple(
        line.strip()
        for line in output.splitlines()
        if line.lstrip().startswith("Usage:")
    )
    prefix = "Usage: codex" + (f" {surface}" if surface else "") + " [OPTIONS]"
    if len(usages) != 1 or not (
        usages[0] == prefix or usages[0].startswith(prefix + " ")
    ):
        return "invalid_output"
    if surface is None and usages[0][len(prefix) :].startswith((" exec", " sandbox")):
        return "invalid_output"
    if surface == "sandbox" and usages[0] != prefix + " [COMMAND]...":
        return "unsupported"
    flags = frozenset(
        match[1]
        for line in output.splitlines()
        if (match := re.match(r"\s+(?:-[A-Za-z],\s+)?(--[a-z][a-z-]*)(?=\s|=|$)", line))
    )
    required = (
        frozenset({"--ask-for-approval", "--config", "--help"})
        if surface is None
        else frozenset({"--cd", "--config", "--help"})
        if surface == "sandbox"
        else frozenset(
            {"--sandbox", "--ignore-user-config", "--strict-config", "--cd", "--help"}
        )
    )
    return "supported" if required <= flags else "unsupported"


def _report(
    request: CliContractRequest,
    state: _CliState,
    checked: list[tuple[str, ...]],
    version: str | None,
) -> CliContractReport:
    diagnostics: dict[_CliState, tuple[_CliDiagnostic, ...]] = {
        "supported": (),
        "unavailable": ("CLI probe unavailable",),
        "unsupported": ("CLI grammar unsupported",),
        "invalid_output": ("CLI output invalid",),
        "version_changed": ("Sampled CLI version changed",),
    }
    return CliContractReport(
        executable=request.executable,
        surface=request.surface,
        checkout=request.checkout,
        state=state,
        version=version,
        checked_argv=tuple(checked),
        diagnostics=diagnostics[state],
    )


def inspect_cli_contract(
    request: CliContractRequest,
    *,
    run_command: CommandRunner,
    isolated_home: Callable[[], AbstractContextManager[Path]],
) -> CliContractReport:
    """Inspect at most five help/version probes; config and isolation stay unknown.

    The byte limit rejects oversized evidence after capture. It does not bound
    subprocess memory. Sampled versions detect drift, not artifact replacement.
    """
    checked: list[tuple[str, ...]] = []
    version: str | None = None
    try:
        with isolated_home() as home:
            environment = {
                "HOME": str(home),
                "CODEX_HOME": str(home),
                "PATH": os.defpath,
            }
            for phase, command in enumerate(_probe_commands(request)):
                checked.append(command)
                try:
                    result = run_command(
                        command,
                        stdin=None,
                        timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
                        environment=environment,
                        current_directory=request.checkout,
                    )
                except UnicodeError:
                    return _report(request, "invalid_output", checked, version)
                except (OSError, subprocess.SubprocessError, RepositoryError):
                    return _report(request, "unavailable", checked, version)
                if result.returncode != 0:
                    return _report(request, "unsupported", checked, version)
                try:
                    oversized = any(
                        len(output.encode("utf-8")) > _MAX_EVIDENCE_BYTES
                        for output in (result.stdout, result.stderr)
                    )
                except UnicodeError:
                    return _report(request, "invalid_output", checked, version)
                if oversized:
                    return _report(request, "invalid_output", checked, version)
                if phase in {0, 4}:
                    sampled = _version(result.stdout)
                    if sampled is None:
                        return _report(request, "invalid_output", checked, version)
                    if phase == 4 and sampled != version:
                        return _report(request, "version_changed", checked, version)
                    version = sampled
                else:
                    state = _help_state(
                        result.stdout, None if phase == 1 else request.surface
                    )
                    if state != "supported":
                        return _report(request, state, checked, version)
    except OSError:
        return _report(request, "unavailable", checked, version)
    return _report(request, "supported", checked, version)
