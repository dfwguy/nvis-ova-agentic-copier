# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure durable-state protocol for one exact Judge validation candidate."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ports import CommandRunner

_TREE = re.compile(r"^[0-9a-f]{40}$")


class JudgeValidationError(RuntimeError):
    """A validation candidate or its durable record is invalid."""


class _Record(BaseModel):
    """Bounded persisted state for a single staged tree."""

    model_config = ConfigDict(extra="forbid")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["running", "terminal-success", "terminal-failure"]
    returncode: int | None = Field(default=None, strict=True)

    def model_post_init(self, _context: object) -> None:
        terminal = self.state in {"terminal-success", "terminal-failure"}
        if terminal != (self.returncode is not None):
            raise ValueError("invalid validation lifecycle")
        if self.state == "terminal-success" and self.returncode != 0:
            raise ValueError("success requires zero exit")
        if self.state == "terminal-failure" and self.returncode == 0:
            raise ValueError("failure requires nonzero exit")


@dataclass(frozen=True)
class JudgeValidationReport:
    """Bounded command result with no hook output."""

    candidate_tree: str
    applied: bool
    action: Literal["preview", "ran", "existing", "status"]
    state: Literal["preview", "running", "terminal-success", "terminal-failure"]
    terminal: bool
    returncode: int | None

    @property
    def exit_code(self) -> int | None:
        """Expose the CLI's terminal-only exit-code name."""
        return self.returncode if self.terminal else None


@dataclass(frozen=True)
class JudgeValidationDependencies:
    """All state, lock, Git, and command boundaries for validation."""

    current_staged_tree: Callable[[], str]
    read_record: Callable[[str], object | None]
    atomic_write_record: Callable[[str, dict[str, object]], None]
    candidate_lock: Callable[[str], AbstractContextManager[None]]
    run_command: CommandRunner
    monotonic_clock: Callable[[], float]
    sleeper: Callable[[float], None]


def _valid_tree(value: str) -> bool:
    return bool(_TREE.fullmatch(value))


def _record(candidate_tree: str, value: object | None) -> _Record | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise JudgeValidationError("validation record is malformed")
    try:
        record = _Record.model_validate(value)
    except ValidationError as error:
        raise JudgeValidationError("validation record is malformed") from error
    if record.candidate_tree != candidate_tree:
        raise JudgeValidationError("validation record tree does not match candidate")
    return record


def _report(
    candidate_tree: str,
    *,
    applied: bool,
    action: Literal["ran", "existing", "status"],
    record: _Record,
) -> JudgeValidationReport:
    terminal = record.state in {"terminal-success", "terminal-failure"}
    return JudgeValidationReport(
        candidate_tree, applied, action, record.state, terminal, record.returncode
    )


def run_judge_validation(
    *, candidate_tree: str, apply: bool, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Preview or run exactly one hook for the current, locked staged tree."""
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    if not apply:
        return JudgeValidationReport(
            candidate_tree, False, "preview", "preview", False, None
        )
    if dependencies.current_staged_tree() != candidate_tree:
        raise JudgeValidationError("candidate tree is not the current staged tree")
    with dependencies.candidate_lock(candidate_tree):
        record = _record(candidate_tree, dependencies.read_record(candidate_tree))
        if record is not None:
            return _report(
                candidate_tree, applied=True, action="existing", record=record
            )
        running = _Record(candidate_tree=candidate_tree, state="running")
        dependencies.atomic_write_record(
            candidate_tree, running.model_dump(exclude_none=True)
        )
        result = dependencies.run_command(("git", "hook", "run", "pre-commit"))
        state: Literal["terminal-success", "terminal-failure"] = (
            "terminal-success" if result.returncode == 0 else "terminal-failure"
        )
        terminal = _Record(
            candidate_tree=candidate_tree, state=state, returncode=result.returncode
        )
        dependencies.atomic_write_record(
            candidate_tree, terminal.model_dump(exclude_none=True)
        )
        return _report(candidate_tree, applied=True, action="ran", record=terminal)


def observe_judge_validation(
    *,
    candidate_tree: str,
    dependencies: JudgeValidationDependencies,
    wait_seconds: int | None = None,
) -> JudgeValidationReport:
    """Read persisted validation state without invoking the hook."""
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    record = _record(candidate_tree, dependencies.read_record(candidate_tree))
    if record is None:
        raise JudgeValidationError("validation record is missing")
    deadline = (
        dependencies.monotonic_clock() + wait_seconds
        if wait_seconds is not None
        else None
    )
    while record.state == "running" and deadline is not None:
        remaining = deadline - dependencies.monotonic_clock()
        if remaining <= 0:
            break
        dependencies.sleeper(min(1.0, remaining))
        record = _record(candidate_tree, dependencies.read_record(candidate_tree))
        if record is None:
            raise JudgeValidationError("validation record is missing")
    return _report(candidate_tree, applied=False, action="status", record=record)
