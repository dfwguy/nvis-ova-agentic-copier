# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Run inexpensive read-only checks against an isolated staged-tree checkout."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from .domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from .ports import CommandRunner
from .repository import CommandResult, run_subprocess

_TREE = re.compile(r"[0-9a-f]{40}\Z")
_YAML_CONFIG = (
    "{extends: default, rules: {line-length: disable, document-start: disable}}"
)


def _checked(
    command: tuple[str, ...],
    directory: Path,
    run_command: CommandRunner,
    environment: Mapping[str, str] | None = None,
) -> str:
    result = run_command(
        command,
        current_directory=directory,
        environment=_environment(environment),
        timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise RuntimeError("candidate snapshot could not be materialized")
    return result.stdout.strip()


@contextmanager
def candidate_snapshot(
    candidate_tree: str,
    source: Path,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] | None = None,
) -> Iterator[Path]:
    """Check out an exact Git tree with a private index and shared objects."""
    if _TREE.fullmatch(candidate_tree) is None:
        raise ValueError("candidate tree is malformed")
    source = source.resolve()
    source_head = _checked(
        ("git", "rev-parse", "HEAD"), source, run_command, environment
    )
    if _TREE.fullmatch(source_head) is None:
        raise RuntimeError("source HEAD is malformed")
    common = Path(
        _checked(
            ("git", "rev-parse", "--git-common-dir"), source, run_command, environment
        )
    )
    if not common.is_absolute():
        common = source / common
    with tempfile.TemporaryDirectory(prefix="agent-work-cheap-gate-") as temporary:
        snapshot = Path(temporary)
        _checked(("git", "init", "--quiet"), snapshot, run_command, environment)
        alternate = snapshot / ".git" / "objects" / "info" / "alternates"
        alternate.parent.mkdir(parents=True, exist_ok=True)
        alternate.write_text(f"{(common / 'objects').resolve()}\n", encoding="utf-8")
        _checked(
            ("git", "update-ref", "HEAD", source_head),
            snapshot,
            run_command,
            environment,
        )
        _checked(
            ("git", "read-tree", candidate_tree), snapshot, run_command, environment
        )
        _checked(("git", "checkout-index", "--all"), snapshot, run_command, environment)
        if (
            _checked(("git", "write-tree"), snapshot, run_command, environment)
            != candidate_tree
        ):
            raise RuntimeError("candidate snapshot tree differs from staged tree")
        try:
            yield snapshot
        finally:
            if (
                _checked(("git", "rev-parse", "HEAD"), source, run_command, environment)
                != source_head
            ):
                raise RuntimeError("source HEAD changed during cheap gate")


def _environment(
    values: Mapping[str, str] | None = None, source: Path | None = None
) -> dict[str, str]:
    environment = {
        name: value
        for name, value in (values if values is not None else os.environ).items()
        if not name.startswith(("GIT_", "AGENT_WORK_GIT_CREDENTIAL"))
        and name not in {"GITLAB_TOKEN", "SSH_ASKPASS"}
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    if source is not None:
        environment["UV_PROJECT_ENVIRONMENT"] = str((source / ".venv").resolve())
    return environment


def _shell_files(
    snapshot: Path,
    run_command: CommandRunner,
    environment: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    files: list[str] = []
    tracked = _checked(
        ("git", "ls-files", "-z"), snapshot, run_command, environment
    ).split("\0")
    for relative in tracked:
        if not relative:
            continue
        path = snapshot / relative
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            first_line = stream.readline(128)
        if path.suffix == ".sh" or first_line.startswith(
            (b"#!/bin/bash", b"#!/usr/bin/env bash", b"#!/bin/sh")
        ):
            files.append(relative)
    return tuple(sorted(files))


def _snapshot_unchanged(
    snapshot: Path,
    candidate_tree: str,
    baseline_status: str,
    run_command: CommandRunner,
    environment: Mapping[str, str] | None = None,
) -> bool:
    if not (snapshot / ".git").is_dir():
        return False
    return (
        _checked(("git", "write-tree"), snapshot, run_command, environment)
        == candidate_tree
        and _checked(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            snapshot,
            run_command,
            environment,
        )
        == baseline_status
    )


def run_candidate_gate(
    candidate_tree: str,
    *,
    candidate_snapshot: Callable[[str], AbstractContextManager[Path]],
    current_staged_tree: Callable[[], str],
    run_command: CommandRunner,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Stop at the first failed cheap check and reject staged-tree drift."""
    if current_staged_tree() != candidate_tree:
        return CommandResult(2, "", "candidate tree changed before cheap gate")
    with candidate_snapshot(candidate_tree) as snapshot:
        if not (snapshot / ".git").is_dir():
            return CommandResult(2, "", "private candidate snapshot unavailable")
        baseline_status = _checked(
            ("git", "status", "--porcelain", "--untracked-files=all"),
            snapshot,
            run_command,
            environment,
        )
        shell_files = _shell_files(snapshot, run_command, environment)
        commands = (
            (
                "uv",
                "run",
                "--no-sync",
                "yamllint",
                f"--config-data={_YAML_CONFIG}",
                ".",
            ),
            (
                "uv",
                "run",
                "--no-sync",
                "python",
                "-m",
                "agent_work.readability",
                "--root",
                ".",
                "--scope",
                "all",
            ),
            ("uv", "run", "--no-sync", "ty", "check"),
            (
                "uv",
                "run",
                "--no-sync",
                "kingfisher",
                "scan",
                ".",
                "--staged",
                "--no-validate",
                "--no-update-check",
            ),
            ("uv", "run", "--no-sync", "ruff", "check", "--no-cache", "."),
            ("uv", "run", "--no-sync", "ruff", "format", "--check", "--no-cache", "."),
        )
        checks: list[tuple[str, tuple[str, ...]]] = [("yamllint", commands[0])]
        if shell_files:
            checks.append(("shellcheck", ("shellcheck", *shell_files)))
        checks.extend(
            zip(
                ("readability", "ty", "kingfisher", "ruff-check", "ruff-format"),
                commands[1:],
                strict=True,
            )
        )
        for name, command in checks:
            if current_staged_tree() != candidate_tree:
                return CommandResult(2, "", "candidate tree changed during cheap gate")
            result = run_command(
                command,
                current_directory=snapshot,
                environment=_environment(environment),
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            )
            if not _snapshot_unchanged(
                snapshot, candidate_tree, baseline_status, run_command, environment
            ):
                return CommandResult(
                    2, "", "candidate snapshot changed during cheap gate"
                )
            if current_staged_tree() != candidate_tree:
                return CommandResult(2, "", "candidate tree changed during cheap gate")
            if result.returncode:
                return CommandResult(result.returncode, "", f"{name} failed")
    if current_staged_tree() != candidate_tree:
        return CommandResult(2, "", "candidate tree changed during cheap gate")
    return CommandResult(0, "", "")


def check_candidate_gate(
    candidate_tree: str,
    *,
    source: Path,
    current_staged_tree: Callable[[], str],
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Wire the real isolated snapshot for a source repository."""
    result = run_candidate_gate(
        candidate_tree,
        candidate_snapshot=lambda tree: candidate_snapshot(
            tree, source, run_command, environment
        ),
        current_staged_tree=current_staged_tree,
        run_command=run_command,
        environment=_environment(environment, source),
    )
    if result.returncode:
        return result
    try:
        source = source.resolve()
        hook_path = Path(
            _checked(
                ("git", "rev-parse", "--git-path", "hooks/pre-commit"),
                source,
                run_command,
                environment,
            )
        )
        if not hook_path.is_absolute():
            hook_path = source / hook_path
        hook_ready = hook_path.is_file() and os.access(hook_path, os.X_OK)
    except (OSError, RuntimeError):
        hook_ready = False
    if current_staged_tree() != candidate_tree:
        return CommandResult(2, "", "candidate tree changed during cheap gate")
    if not hook_ready:
        return CommandResult(2, "", "pre-commit-hook failed")
    return result
