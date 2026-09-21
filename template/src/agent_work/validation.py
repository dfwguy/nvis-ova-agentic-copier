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

import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ports import CommandRunner

_TREE = re.compile(r"^[0-9a-f]{40}$")
_NONNEGATIVE_INTEGER = re.compile(r"^[0-9]+$")
_DIAGNOSTIC = re.compile(
    r"^git hook run pre-commit failed with exit code (?P<returncode>-?[0-9]+); "
    r"(?:no failing JUnit summary available|JUnit "
    r"(?:(?:py311 failures=[0-9]+ errors=[0-9]+)"
    r"(?:; py314 failures=[0-9]+ errors=[0-9]+)?|"
    r"py314 failures=[0-9]+ errors=[0-9]+))$"
)
_JUNIT_ARTIFACTS = (
    ("py311", Path("test-results-py311.xml")),
    ("py314", Path("test-results-py314.xml")),
)
_MAX_ARTIFACT_BYTES = 1_048_576
_MAX_COUNTER = 999_999_999
_MAX_DIAGNOSTIC_BYTES = 512
_MAX_HOOK_FAILURES = 16
_MAX_HOOK_ID_BYTES = 128
_MAX_HOOK_LINES = 4
_MAX_HOOK_LINE_BYTES = 256
_MAX_HOOK_FAILURE_BYTES = 4_096
_HOOK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SAFE_PATH = re.compile(r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
_READABILITY_LINE = re.compile(
    r"^(?P<path>[^:\s]+): grade (?P<grade>[0-9]+(?:\.[0-9]+)?)$"
)
_RUFF_LINE = re.compile(
    r"^(?P<path>[^:\s]+):(?P<line>[0-9]+):(?P<column>[0-9]+): "
    r"(?P<code>[A-Z][0-9]{1,5})(?: .*)?$"
)
_TY_ERROR = re.compile(r"^error\[(?P<code>[a-z][a-z0-9-]*)\]:.*$")
_TY_LOCATION = re.compile(
    r"^--> (?P<path>[^:\s]+):(?P<line>[0-9]+):(?P<column>[0-9]+)$"
)
_CANONICAL_HOOK_LINE = re.compile(
    r"^(?:No safe structured diagnostic available\.|"
    r"JUnit (?:(?:py311 failures=[0-9]+ errors=[0-9]+)(?:; py314 failures=[0-9]+ errors=[0-9]+)?|py314 failures=[0-9]+ errors=[0-9]+)|"
    r"(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+: grade [0-9]+(?:\.[0-9]+)?|"
    r"(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+:[0-9]+:[0-9]+: (?:[A-Z][0-9]{1,5}|[a-z][a-z0-9-]*))$"
)
_FALLBACK_HOOK_LINE = "No safe structured diagnostic available."


def _safe_path(value: str) -> bool:
    return bool(
        _SAFE_PATH.fullmatch(value)
        and not value.startswith("/")
        and all(part not in {".", ".."} for part in value.split("/"))
    )


def _safe_hook_id(value: str) -> bool:
    return bool(
        value.isascii()
        and _HOOK_ID.fullmatch(value)
        and len(value.encode("ascii")) <= _MAX_HOOK_ID_BYTES
    )


@dataclass(frozen=True)
class HookFailure:
    """One bounded, allowlisted failure detail from the aggregate hook run."""

    hook_id: str
    diagnostic_lines: tuple[str, ...]


class _PersistedHookFailure(BaseModel):
    """Strict persisted counterpart to :class:`HookFailure`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    hook_id: str = Field(strict=True)
    diagnostic_lines: tuple[str, ...]

    def model_post_init(self, _context: object) -> None:
        if (
            not _safe_hook_id(self.hook_id)
            or not self.diagnostic_lines
            or len(self.diagnostic_lines) > _MAX_HOOK_LINES
            or any(
                not line.isprintable()
                or len(line.encode("utf-8")) > _MAX_HOOK_LINE_BYTES
                or _CANONICAL_HOOK_LINE.fullmatch(line) is None
                for line in self.diagnostic_lines
            )
        ):
            raise ValueError("invalid hook failure evidence")


class JudgeValidationError(RuntimeError):
    """A validation candidate or its durable record is invalid."""


class _Record(BaseModel):
    """Bounded persisted state for a single staged tree."""

    model_config = ConfigDict(extra="forbid")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: Literal["running", "terminal-success", "terminal-failure"]
    returncode: int | None = Field(default=None, strict=True)
    diagnostic: str | None = None
    hook_failures: tuple[_PersistedHookFailure, ...] = ()

    def model_post_init(self, _context: object) -> None:
        terminal = self.state in {"terminal-success", "terminal-failure"}
        if terminal != (self.returncode is not None):
            raise ValueError("invalid validation lifecycle")
        if self.state == "terminal-success" and self.returncode != 0:
            raise ValueError("success requires zero exit")
        if self.state == "terminal-failure" and self.returncode == 0:
            raise ValueError("failure requires nonzero exit")
        if self.diagnostic is not None:
            match = _DIAGNOSTIC.fullmatch(self.diagnostic)
            numeric_values = re.findall(
                r"(?:failures=|errors=)([0-9]+)", self.diagnostic
            )
            if (
                self.state != "terminal-failure"
                or match is None
                or not self.diagnostic.isprintable()
                or len(self.diagnostic.encode("utf-8")) > _MAX_DIAGNOSTIC_BYTES
                or int(match.group("returncode")) != self.returncode
                or str(int(match.group("returncode"))) != match.group("returncode")
                or int(match.group("returncode")) == 0
                or abs(int(match.group("returncode"))) > _MAX_COUNTER
                or any(
                    str(int(value)) != value or int(value) > _MAX_COUNTER
                    for value in numeric_values
                )
            ):
                raise ValueError("invalid validation diagnostic")
        if (
            self.hook_failures
            and self.state != "terminal-failure"
            or len(self.hook_failures) > _MAX_HOOK_FAILURES
            or len(
                json.dumps(
                    [item.model_dump(mode="json") for item in self.hook_failures],
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            > _MAX_HOOK_FAILURE_BYTES
        ):
            raise ValueError("invalid hook failure evidence")


@dataclass(frozen=True)
class JudgeValidationReport:
    """Bounded command result with no hook output."""

    candidate_tree: str
    applied: bool
    action: Literal["preview", "ran", "existing", "status"]
    state: Literal["preview", "running", "terminal-success", "terminal-failure"]
    terminal: bool
    returncode: int | None
    diagnostic: str | None
    hook_failures: tuple[HookFailure, ...] = ()

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
    read_artifact: Callable[[Path], bytes | None] = lambda _path: None


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


def _bounded_counter(value: str | None) -> int | None:
    if value is None or _NONNEGATIVE_INTEGER.fullmatch(value) is None:
        return None
    parsed = int(value)
    return parsed if str(parsed) == value and parsed <= _MAX_COUNTER else None


def _suite_counts(element: ElementTree.Element) -> tuple[int, int] | None:
    failures = _bounded_counter(element.get("failures"))
    errors = _bounded_counter(element.get("errors"))
    if failures is None or errors is None:
        return None
    return failures, errors


def _junit_failure_counts(artifact: bytes | None) -> tuple[int, int] | None:
    """Return bounded aggregate failure counts from one fixed JUnit artifact."""
    if artifact is None or len(artifact) > _MAX_ARTIFACT_BYTES:
        return None
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return None
    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name == "testsuite":
        counts = _suite_counts(root)
    elif root_name == "testsuites":
        counts = _suite_counts(root)
        if counts is None:
            children = tuple(
                _suite_counts(child)
                for child in root
                if child.tag.rsplit("}", 1)[-1] == "testsuite"
            )
            if not children or any(child is None for child in children):
                return None
            failures = sum(child[0] for child in children if child is not None)
            errors = sum(child[1] for child in children if child is not None)
            counts = (
                (failures, errors)
                if failures <= _MAX_COUNTER and errors <= _MAX_COUNTER
                else None
            )
    else:
        return None
    if counts is None or sum(counts) == 0:
        return None
    return counts


def _junit_summaries(read_artifact: Callable[[Path], bytes | None]) -> tuple[str, ...]:
    """Read each fixed artifact once and return safe aggregate summaries."""
    summaries: list[str] = []
    for label, path in _JUNIT_ARTIFACTS:
        try:
            counts = _junit_failure_counts(read_artifact(path))
        except OSError:
            counts = None
        if counts is not None:
            summaries.append(f"{label} failures={counts[0]} errors={counts[1]}")
    return tuple(summaries)


def _failure_diagnostic(returncode: int, summaries: tuple[str, ...]) -> str:
    if returncode == 0 or abs(returncode) > _MAX_COUNTER:
        raise JudgeValidationError("validation hook returned an invalid exit code")
    suffix = (
        f"JUnit {'; '.join(summaries)}"
        if summaries
        else "no failing JUnit summary available"
    )
    return f"git hook run pre-commit failed with exit code {returncode}; {suffix}"


def _canonical_readability(lines: tuple[str, ...]) -> tuple[str, ...]:
    details: list[tuple[float, str, str]] = []
    for line in lines:
        match = _READABILITY_LINE.fullmatch(line)
        if match is not None and _safe_path(match.group("path")):
            details.append(
                (
                    float(match.group("grade")),
                    match.group("path"),
                    f"{match.group('path')}: grade {match.group('grade')}",
                )
            )
    return tuple(
        line
        for _grade, _path, line in sorted(
            details, key=lambda item: (-item[0], item[1])
        )[:_MAX_HOOK_LINES]
    )


def _canonical_ruff(lines: tuple[str, ...]) -> tuple[str, ...]:
    details: list[str] = []
    for line in lines:
        match = _RUFF_LINE.fullmatch(line)
        if match is not None and _safe_path(match.group("path")):
            details.append(
                f"{match.group('path')}:{match.group('line')}:{match.group('column')}: "
                f"{match.group('code')}"
            )
    return tuple(details[:_MAX_HOOK_LINES])


def _canonical_ty(lines: tuple[str, ...]) -> tuple[str, ...]:
    details: list[str] = []
    pending_code: str | None = None
    for line in lines:
        error = _TY_ERROR.fullmatch(line)
        if error is not None:
            pending_code = error.group("code")
            continue
        location = _TY_LOCATION.fullmatch(line)
        if (
            location is not None
            and pending_code is not None
            and _safe_path(location.group("path"))
        ):
            details.append(
                f"{location.group('path')}:{location.group('line')}:"
                f"{location.group('column')}: {pending_code}"
            )
            pending_code = None
    return tuple(details[:_MAX_HOOK_LINES])


def _safe_canonical_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Keep only a bounded canonical detail set safe for persistence."""
    if not lines or any(
        not line.isprintable()
        or len(line.encode("utf-8")) > _MAX_HOOK_LINE_BYTES
        or _CANONICAL_HOOK_LINE.fullmatch(line) is None
        for line in lines
    ):
        return ()
    return lines


def _hook_frames(stdout: str, stderr: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Extract deduplicated standard pre-commit failed-hook frames in order."""
    lines = tuple((stdout + "\n" + stderr).splitlines())
    frames: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    index = 0
    while index + 1 < len(lines):
        if not re.fullmatch(r".+\.{2,}Failed", lines[index]):
            index += 1
            continue
        match = re.fullmatch(r"- hook id: (?P<hook_id>.+)", lines[index + 1])
        if match is None:
            index += 1
            continue
        hook_id = match.group("hook_id")
        end = index + 2
        while end < len(lines) and not re.fullmatch(r".+\.{2,}Failed", lines[end]):
            end += 1
        if _safe_hook_id(hook_id) and hook_id not in seen:
            frames.append((hook_id, lines[index + 2 : end]))
            seen.add(hook_id)
        index = end
    return tuple(frames[:_MAX_HOOK_FAILURES])


def _canonical_hook_failures(
    stdout: str, stderr: str, summaries: tuple[str, ...]
) -> tuple[HookFailure, ...]:
    """Return bounded typed diagnostics without retaining unrecognized output."""
    candidates: list[HookFailure] = []
    junit_line = f"JUnit {'; '.join(summaries)}" if summaries else None
    for hook_id, lines in _hook_frames(stdout, stderr):
        details = _safe_canonical_lines(
            _canonical_readability(lines)
            if hook_id == "readability-check"
            else _canonical_ruff(lines)
            if hook_id == "ruff"
            else _canonical_ty(lines)
            if hook_id == "ty"
            else (junit_line,)
            if hook_id == "tox" and junit_line is not None
            else ()
        )
        candidates.append(HookFailure(hook_id, details or (_FALLBACK_HOOK_LINE,)))

    packed = [HookFailure(item.hook_id, (_FALLBACK_HOOK_LINE,)) for item in candidates]
    for index, candidate in enumerate(candidates):
        details: list[str] = []
        for line in candidate.diagnostic_lines:
            proposed = [*packed]
            proposed[index] = HookFailure(candidate.hook_id, (*details, line))
            serialized = json.dumps(
                [
                    {
                        "hook_id": item.hook_id,
                        "diagnostic_lines": list(item.diagnostic_lines),
                    }
                    for item in proposed
                ],
                separators=(",", ":"),
            ).encode("utf-8")
            if len(serialized) > _MAX_HOOK_FAILURE_BYTES:
                break
            details.append(line)
            packed = proposed
    return tuple(packed)


def _report(
    candidate_tree: str,
    *,
    applied: bool,
    action: Literal["ran", "existing", "status"],
    record: _Record,
) -> JudgeValidationReport:
    terminal = record.state in {"terminal-success", "terminal-failure"}
    return JudgeValidationReport(
        candidate_tree,
        applied,
        action,
        record.state,
        terminal,
        record.returncode,
        record.diagnostic,
        tuple(
            HookFailure(item.hook_id, item.diagnostic_lines)
            for item in record.hook_failures
        )
        if record.state == "terminal-failure"
        else (),
    )


def run_judge_validation(
    *, candidate_tree: str, apply: bool, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Preview or run exactly one hook for the current, locked staged tree."""
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    if not apply:
        return JudgeValidationReport(
            candidate_tree, False, "preview", "preview", False, None, None
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
            candidate_tree,
            running.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        )
        result = dependencies.run_command(("git", "hook", "run", "pre-commit"))
        state: Literal["terminal-success", "terminal-failure"] = (
            "terminal-success" if result.returncode == 0 else "terminal-failure"
        )
        summaries = (
            _junit_summaries(dependencies.read_artifact)
            if state == "terminal-failure"
            else ()
        )
        hook_failures = (
            _canonical_hook_failures(result.stdout, result.stderr, summaries)
            if state == "terminal-failure"
            else ()
        )
        terminal = _Record(
            candidate_tree=candidate_tree,
            state=state,
            returncode=result.returncode,
            diagnostic=(
                _failure_diagnostic(result.returncode, summaries)
                if state == "terminal-failure"
                else None
            ),
            hook_failures=tuple(
                _PersistedHookFailure(
                    hook_id=item.hook_id, diagnostic_lines=item.diagnostic_lines
                )
                for item in hook_failures
            ),
        )
        dependencies.atomic_write_record(
            candidate_tree,
            terminal.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
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
