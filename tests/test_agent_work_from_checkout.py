# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the checkout-local agent-work entry point."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(
    params=(
        Path("bin/agent-work-from-checkout"),
        Path("template/bin/agent-work-from-checkout"),
    )
)
def entrypoint(project_root: Path, request: pytest.FixtureRequest) -> Path:
    """Return each maintained checkout-local entry point."""
    return project_root / request.param


def _write_command(path: Path, body: str) -> None:
    """Create an executable POSIX-shell command stub."""
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.mark.parametrize(
    "arguments", (("--help",), ("merge-request", "publish", "--apply"))
)
def test_entrypoint_runs_checkout_project_and_passes_every_argument(
    entrypoint: Path, tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    """The wrapper uses its own checkout instead of an ambient agent-work."""
    commands = tmp_path / "commands"
    commands.mkdir()
    recorded_arguments = tmp_path / "uv-arguments"
    global_agent_work_ran = tmp_path / "global-agent-work-ran"
    _write_command(
        commands / "uv",
        'printf "%s\\n" "$@" > "$UV_ARGUMENTS"',
    )
    _write_command(
        commands / "agent-work",
        'touch "$GLOBAL_AGENT_WORK_RAN"\nexit 97',
    )
    environment = os.environ | {
        "GLOBAL_AGENT_WORK_RAN": str(global_agent_work_ran),
        "PATH": f"{commands}:{os.environ['PATH']}",
        "UV_ARGUMENTS": str(recorded_arguments),
    }

    completed = subprocess.run(
        ["bash", str(entrypoint), *arguments],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert recorded_arguments.read_text().splitlines() == [
        "run",
        "--project",
        str(entrypoint.parent.parent.resolve()),
        "agent-work",
        *arguments,
    ]
    assert not global_agent_work_ran.exists()


def test_entrypoint_propagates_the_checkout_project_command_failure(
    entrypoint: Path, tmp_path: Path
) -> None:
    """The wrapper preserves an underlying command failure for callers."""
    commands = tmp_path / "commands"
    commands.mkdir()
    _write_command(commands / "uv", "exit 23")
    environment = os.environ | {"PATH": f"{commands}:{os.environ['PATH']}"}

    completed = subprocess.run(
        ["bash", str(entrypoint), "--help"],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert completed.returncode == 23
