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

import hashlib
import json
import math
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
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
MAX_TIMING_CASES = 1_024
MAX_TIMING_CASE_NAME_BYTES = 256
MAX_TIMING_CASE_LIST_BYTES = 262_144
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
_SAFE_TEST_NAME = re.compile(
    r"^(?:[A-Za-z0-9_./:-]+::)?test_[A-Za-z0-9_./:-]{1,150}"
    r"(?:\[[A-Za-z0-9_-][A-Za-z0-9_.-]{0,149}\])?$"
)
_SECONDS = re.compile(r"^[0-9]+(?:\.[0-9]{1,6})?$")


def _safe_path(value: str) -> bool:
    return bool(
        _SAFE_PATH.fullmatch(value)
        and not value.startswith("/")
        and all(part not in {".", ".."} for part in value.split("/"))
    )


def is_safe_junit_test_name(value: str) -> bool:
    """Allow only relative test identifiers, never private filesystem paths."""
    if _SAFE_TEST_NAME.fullmatch(value) is None:
        return False
    classname, separator, test_name = value.rpartition("::")
    if separator and not _safe_path(classname):
        return False
    return not test_name.startswith("/") and all(
        part not in {".", ".."} for part in test_name.split("/")
    )


def _safe_timing_case_name(classname: str, test_name: str) -> str | None:
    """Retain a safe base name and hide arbitrary parameter values in timing proof."""
    full_name = f"{classname}::{test_name}" if classname else test_name
    if is_safe_junit_test_name(full_name):
        return full_name
    base_name, separator, _parameter = test_name.partition("[")
    if not separator or not test_name.endswith("]"):
        return None
    safe_base = f"{classname}::{base_name}" if classname else base_name
    if not is_safe_junit_test_name(safe_base):
        return None
    digest = hashlib.sha256(full_name.encode("utf-8")).hexdigest()
    return f"{safe_base}[param_{digest}]"


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


class JUnitSnapshotError(JudgeValidationError):
    """An I/O failure while saving one known fixed JUnit artifact."""

    def __init__(self, environment: Literal["py311", "py314"], path: Path) -> None:
        super().__init__("fixed JUnit artifact snapshot failed")
        self.environment = environment
        self.path = path


@dataclass(frozen=True)
class JUnitArtifact:
    """Immutable, candidate-bound evidence retained after a successful hook."""

    environment: Literal["py311", "py314"]
    path: Path
    sha256: str
    test_count: int


class _PersistedJUnitArtifact(BaseModel):
    """Strict persisted counterpart to :class:`JUnitArtifact`."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    environment: Literal["py311", "py314"]
    path: str = Field(strict=True)
    sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    test_count: int = Field(strict=True, ge=0, le=_MAX_COUNTER)


@dataclass(frozen=True)
class SlowTestCase:
    """One safe test name and its observed JUnit duration."""

    name: str
    seconds: float


@dataclass(frozen=True)
class JUnitTiming:
    """Bounded timing summary from one supported JUnit artifact."""

    environment: Literal["py311", "py314"]
    total_seconds: float
    slowest_cases: tuple[SlowTestCase, ...]
    timing_complete: bool
    over_100ms_cases: tuple[SlowTestCase, ...]


class _PersistedSlowCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    seconds: float = Field(ge=0, le=1_000_000)

    def model_post_init(self, _context: object) -> None:
        if not is_safe_junit_test_name(self.name):
            raise ValueError("invalid slow test name")


class _PersistedJUnitTiming(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    environment: Literal["py311", "py314"]
    total_seconds: float = Field(ge=0, le=1_000_000)
    slowest_cases: tuple[_PersistedSlowCase, ...] = Field(max_length=3)
    timing_complete: bool = Field(default=False, strict=True)
    over_100ms_cases: tuple[_PersistedSlowCase, ...] = Field(
        default=(), max_length=MAX_TIMING_CASES
    )

    def model_post_init(self, _context: object) -> None:
        encoded = json.dumps(
            [case.model_dump(mode="json") for case in self.over_100ms_cases],
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_TIMING_CASE_LIST_BYTES or any(
            case.seconds <= 0.100 for case in self.over_100ms_cases
        ):
            raise ValueError("JUnit timing case list exceeds bound")


@dataclass(frozen=True)
class ArtifactFailure:
    """Bounded reason for unusable evidence at one fixed artifact path."""

    stage: Literal[
        "pre_hook_cleanup",
        "post_hook_failed_evidence_read",
        "post_hook_failed_cleanup",
        "post_hook_success_evidence_read",
        "post_hook_success_cleanup",
        "immutable_snapshot",
    ]
    reason: Literal[
        "os_error",
        "missing",
        "invalid_xml",
        "oversized",
        "invalid_count",
        "invalid_snapshot",
    ]
    environment: Literal["py311", "py314"] | None
    path: Path | None


class _PersistedArtifactFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal[
        "pre_hook_cleanup",
        "post_hook_failed_evidence_read",
        "post_hook_failed_cleanup",
        "post_hook_success_evidence_read",
        "post_hook_success_cleanup",
        "immutable_snapshot",
    ]
    reason: Literal[
        "os_error",
        "missing",
        "invalid_xml",
        "oversized",
        "invalid_count",
        "invalid_snapshot",
    ]
    environment: Literal["py311", "py314"] | None = None
    path: str | None = None

    def model_post_init(self, _context: object) -> None:
        if (self.environment is None) != (self.path is None):
            raise ValueError("invalid fixed artifact pair")
        if (
            self.environment is not None
            and self.path != f"test-results-{self.environment}.xml"
        ):
            raise ValueError("invalid fixed artifact path")
        if self.environment is None and self.stage != "immutable_snapshot":
            raise ValueError("missing fixed artifact path")
        if self.reason == "invalid_snapshot" and (
            self.stage != "immutable_snapshot" or self.environment is not None
        ):
            raise ValueError("invalid snapshot reason")


class _Record(BaseModel):
    """Bounded persisted state for a single staged tree."""

    model_config = ConfigDict(extra="forbid")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    schema_version: Literal[2] | None = None
    state: Literal[
        "running", "terminal-success", "terminal-failure", "terminal-artifact-failure"
    ]
    returncode: int | None = Field(default=None, strict=True)
    diagnostic: str | None = None
    hook_failures: tuple[_PersistedHookFailure, ...] = ()
    junit_artifacts: tuple[_PersistedJUnitArtifact, ...] = ()
    failed_tests: tuple[str, ...] = ()
    failed_junit_digests: tuple[str, ...] = ()
    elapsed_seconds: float | None = Field(default=None, ge=0, le=10_800)
    junit_timings: tuple[_PersistedJUnitTiming, ...] = Field(default=(), max_length=2)
    artifact_failure: _PersistedArtifactFailure | None = None

    def model_post_init(self, _context: object) -> None:
        terminal = self.state in {
            "terminal-success",
            "terminal-failure",
            "terminal-artifact-failure",
        }
        if self.state == "terminal-artifact-failure":
            code_is_valid = (
                self.returncode is None or abs(self.returncode) <= _MAX_COUNTER
            )
        else:
            code_is_valid = terminal == (self.returncode is not None)
        if not code_is_valid:
            raise ValueError("invalid validation lifecycle")
        if (
            self.artifact_failure is not None
            and self.state != "terminal-artifact-failure"
        ):
            raise ValueError("invalid artifact failure evidence")
        if (
            self.returncode is None
            and self.artifact_failure is not None
            and self.artifact_failure.stage != "pre_hook_cleanup"
        ):
            raise ValueError("post-hook artifact failure requires a hook exit")
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
                self.state not in {"terminal-failure", "terminal-artifact-failure"}
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
        legacy = (
            self.schema_version is None
            and "junit_artifacts" not in self.model_fields_set
        )
        if self.schema_version is None and not legacy:
            raise ValueError("invalid validation record version")
        if (
            self.schema_version == 2
            and self.state == "terminal-success"
            and not _valid_junit_artifacts(self.candidate_tree, self.junit_artifacts)
        ):
            raise ValueError("invalid JUnit artifact evidence")
        if self.state != "terminal-success" and self.junit_artifacts:
            raise ValueError("invalid JUnit artifact evidence")
        if self.state == "running" and self.elapsed_seconds is not None:
            raise ValueError("running validation has no terminal timing")
        if (
            self.state != "terminal-success"
            and self.junit_timings
            or tuple(item.environment for item in self.junit_timings)
            != tuple(sorted({item.environment for item in self.junit_timings}))
        ):
            raise ValueError("invalid JUnit timing evidence")
        if (
            self.state != "terminal-failure"
            and (self.failed_tests or self.failed_junit_digests)
            or len(self.failed_tests) > 16
            or len(self.failed_junit_digests) > 2
            or any(not is_safe_junit_test_name(name) for name in self.failed_tests)
            or any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.failed_junit_digests
            )
        ):
            raise ValueError("invalid failed JUnit evidence")


@dataclass(frozen=True)
class JudgeValidationReport:
    """Bounded command result with no hook output."""

    candidate_tree: str
    applied: bool
    action: Literal[
        "preview",
        "ran",
        "existing",
        "status",
        "retry_preview",
        "retried",
        "existing_retry",
    ]
    state: Literal[
        "preview",
        "running",
        "terminal-success",
        "terminal-failure",
        "terminal-artifact-failure",
    ]
    terminal: bool
    returncode: int | None
    diagnostic: str | None
    hook_failures: tuple[HookFailure, ...] = ()
    junit_artifacts: tuple[JUnitArtifact, ...] = ()
    failed_tests: tuple[str, ...] = ()
    failed_junit_digests: tuple[str, ...] = ()
    elapsed_seconds: float | None = None
    junit_timings: tuple[JUnitTiming, ...] = ()
    artifact_failure: ArtifactFailure | None = None

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
    remove_junit_artifact: Callable[[Path], None] = lambda _path: None
    capture_junit_artifacts: Callable[
        [str, tuple[tuple[str, bytes, int], ...]], tuple[Path, ...] | None
    ] = lambda _tree, _artifacts: None
    read_retry_record: Callable[[str], object | None] = lambda _tree: None
    atomic_write_retry_record: Callable[[str, dict[str, object]], None] = (
        lambda _tree, _record: None
    )


def _valid_tree(value: str) -> bool:
    return bool(_TREE.fullmatch(value))


def _valid_junit_artifacts(
    candidate_tree: str, artifacts: tuple[_PersistedJUnitArtifact, ...]
) -> bool:
    """Validate the complete ordered candidate-bound JUnit evidence set."""
    if len(artifacts) != len(_JUNIT_ARTIFACTS):
        return False
    for artifact, (environment, basename) in zip(artifacts, _JUNIT_ARTIFACTS):
        path = Path(artifact.path)
        if (
            artifact.environment != environment
            or not path.is_absolute()
            or path.parent.name != candidate_tree
            or path.name != basename.name
        ):
            return False
    return True


def _write_snapshot_bytes(path: Path, contents: bytes) -> None:
    """Write one snapshot payload through the filesystem boundary."""
    path.write_bytes(contents)


@dataclass(frozen=True)
class FilesystemJUnitSnapshotStore:
    """Atomically publish read-only JUnit snapshots beneath one state root."""

    root: Path
    write_bytes: Callable[[Path, bytes], None] = _write_snapshot_bytes

    def __post_init__(self) -> None:
        """Resolve the state root once so persisted evidence paths are absolute."""
        object.__setattr__(self, "root", self.root.resolve())

    def capture(
        self, *, candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        """Publish one complete candidate snapshot without overwriting prior evidence."""
        if not _valid_tree(candidate_tree) or len(artifacts) != len(_JUNIT_ARTIFACTS):
            raise JudgeValidationError("invalid JUnit artifact snapshot")
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / candidate_tree
        if destination.exists():
            raise JudgeValidationError("JUnit artifact snapshot already exists")
        temporary = Path(tempfile.mkdtemp(prefix=".junit-", dir=self.root))
        try:
            paths: list[Path] = []
            for (environment, basename), (actual_environment, contents, count) in zip(
                _JUNIT_ARTIFACTS, artifacts
            ):
                if actual_environment != environment or not isinstance(contents, bytes):
                    raise JudgeValidationError("invalid JUnit artifact snapshot")
                if count < 0 or count > _MAX_COUNTER:
                    raise JudgeValidationError("invalid JUnit artifact snapshot")
                path = temporary / basename
                try:
                    self.write_bytes(path, contents)
                    path.chmod(0o444)
                except OSError as error:
                    raise JUnitSnapshotError(environment, basename) from error
                paths.append(destination / basename)
            temporary.chmod(0o555)
            temporary.replace(destination)
            return tuple(paths)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


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


def _declared_junit_count(root: ElementTree.Element) -> int | None:
    """Read a bounded declared count from an already parsed JUnit root."""
    root_name = root.tag.rsplit("}", 1)[-1]
    if root_name == "testsuite":
        return _bounded_counter(root.get("tests"))
    if root_name != "testsuites":
        return None
    direct_count = _bounded_counter(root.get("tests"))
    if direct_count is not None:
        return direct_count
    child_counts = tuple(
        _bounded_counter(child.get("tests"))
        for child in root
        if child.tag.rsplit("}", 1)[-1] == "testsuite"
    )
    if not child_counts or any(count is None for count in child_counts):
        return None
    total = sum(count for count in child_counts if count is not None)
    return total if total <= _MAX_COUNTER else None


def _junit_test_count(artifact: bytes | None) -> int | None:
    """Return a bounded test count from one well-formed successful JUnit artifact."""
    if artifact is None or len(artifact) > _MAX_ARTIFACT_BYTES:
        return None
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return None
    return _declared_junit_count(root)


def _unusable_artifact_reason(artifact: bytes | None) -> str:
    if artifact is None:
        return "missing"
    if len(artifact) > _MAX_ARTIFACT_BYTES:
        return "oversized"
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return "invalid_xml"
    if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
        return "invalid_xml"
    return "invalid_count"


def _junit_timing_summary(
    environment: Literal["py311", "py314"], artifact: bytes
) -> _PersistedJUnitTiming | None:
    """Summarize safe per-test time from one already bounded JUnit artifact."""
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return None
    total = 0.0
    seen = 0
    slowest: list[_PersistedSlowCase] = []
    over_100ms: list[_PersistedSlowCase] = []
    over_100ms_bytes = 2
    timing_complete = True
    seen_names: set[str] = set()
    for case in root.iter():
        if case.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        seen += 1
        raw_seconds = case.get("time", "")
        if _SECONDS.fullmatch(raw_seconds) is None:
            timing_complete = False
            continue
        seconds = float(raw_seconds)
        if not math.isfinite(seconds) or seconds > 1_000_000:
            timing_complete = False
            continue
        total += seconds
        if total > 1_000_000:
            return None
        raw_name = case.get("name", "")
        raw_class = case.get("classname", "")
        name = _safe_timing_case_name(raw_class, raw_name)
        if name is None or len(name.encode("utf-8")) > MAX_TIMING_CASE_NAME_BYTES:
            timing_complete = False
            continue
        if name in seen_names:
            timing_complete = False
        seen_names.add(name)
        slowest.append(_PersistedSlowCase(name=name, seconds=round(seconds, 3)))
        if seconds > 0.100:
            candidate = _PersistedSlowCase(name=name, seconds=seconds)
            case_bytes = len(
                json.dumps(
                    candidate.model_dump(mode="json"), separators=(",", ":")
                ).encode("utf-8")
            )
            separator_bytes = 1 if over_100ms else 0
            if (
                len(over_100ms) >= MAX_TIMING_CASES
                or over_100ms_bytes + case_bytes + separator_bytes
                > MAX_TIMING_CASE_LIST_BYTES
            ):
                timing_complete = False
            else:
                over_100ms.append(candidate)
                over_100ms_bytes += case_bytes + separator_bytes
    if not seen:
        return None
    if seen != _declared_junit_count(root):
        timing_complete = False
    return _PersistedJUnitTiming(
        environment=environment,
        total_seconds=round(total, 3),
        slowest_cases=tuple(
            sorted(slowest, key=lambda item: (-item.seconds, item.name))[:3]
        ),
        timing_complete=timing_complete,
        over_100ms_cases=tuple(over_100ms),
    )


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


def _failed_junit_evidence(
    read_artifact: Callable[[Path], bytes | None],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return bounded counts, names, and digests before failed artifacts are removed."""
    summaries: list[str] = []
    names: list[str] = []
    digests: list[str] = []
    for label, path in _JUNIT_ARTIFACTS:
        artifact = read_artifact(path)
        if artifact is None or len(artifact) > _MAX_ARTIFACT_BYTES:
            continue
        counts = _junit_failure_counts(artifact)
        if counts is None:
            continue
        summaries.append(f"{label} failures={counts[0]} errors={counts[1]}")
        digests.append(hashlib.sha256(artifact).hexdigest())
        try:
            root = ElementTree.fromstring(artifact)
        except ElementTree.ParseError:
            continue
        for case in root.iter():
            if case.tag.rsplit("}", 1)[-1] != "testcase" or not any(
                child.tag.rsplit("}", 1)[-1] in {"failure", "error"} for child in case
            ):
                continue
            raw_name = case.get("name", "")
            raw_class = case.get("classname", "")
            name = f"{raw_class}::{raw_name}" if raw_class else raw_name
            if is_safe_junit_test_name(name) and name not in names:
                names.append(name)
            if len(names) >= 16:
                break
    return tuple(summaries), tuple(names), tuple(digests)


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


def parse_installed_precommit_failures(
    stdout: str, stderr: str
) -> tuple[HookFailure, ...]:
    """Return source-proven failures from standard installed pre-commit output."""
    return _canonical_hook_failures(stdout, stderr, ())


def _report(
    candidate_tree: str,
    *,
    applied: bool,
    action: Literal["ran", "existing", "status", "retried", "existing_retry"],
    record: _Record,
) -> JudgeValidationReport:
    terminal = record.state in {
        "terminal-success",
        "terminal-failure",
        "terminal-artifact-failure",
    }
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
        tuple(
            JUnitArtifact(
                item.environment,
                Path(item.path),
                item.sha256,
                item.test_count,
            )
            for item in record.junit_artifacts
        )
        if record.state == "terminal-success"
        else (),
        record.failed_tests if record.state == "terminal-failure" else (),
        record.failed_junit_digests if record.state == "terminal-failure" else (),
        record.elapsed_seconds,
        tuple(
            JUnitTiming(
                item.environment,
                item.total_seconds,
                tuple(
                    SlowTestCase(case.name, case.seconds) for case in item.slowest_cases
                ),
                item.timing_complete,
                tuple(
                    SlowTestCase(case.name, case.seconds)
                    for case in item.over_100ms_cases
                ),
            )
            for item in record.junit_timings
        )
        if record.state == "terminal-success"
        else (),
        ArtifactFailure(
            record.artifact_failure.stage,
            record.artifact_failure.reason,
            record.artifact_failure.environment,
            Path(record.artifact_failure.path)
            if record.artifact_failure.path is not None
            else None,
        )
        if record.artifact_failure is not None
        else None,
    )


def _artifact_failure_record(
    candidate_tree: str,
    returncode: int | None,
    elapsed_seconds: float | None = None,
    *,
    stage: str,
    reason: str,
    environment: str | None = None,
    path: Path | None = None,
    diagnostic: str | None = None,
) -> _Record:
    """Create a redacted terminal state for unusable or unpublishable evidence."""
    return _Record(
        candidate_tree=candidate_tree,
        schema_version=2,
        state="terminal-artifact-failure",
        returncode=returncode,
        elapsed_seconds=elapsed_seconds,
        diagnostic=diagnostic,
        artifact_failure=_PersistedArtifactFailure.model_validate(
            {
                "stage": stage,
                "reason": reason,
                "environment": environment,
                "path": str(path) if path is not None else None,
            }
        ),
    )


def _write_terminal(
    candidate_tree: str, record: _Record, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Persist a terminal record and project its bounded public report."""
    dependencies.atomic_write_record(
        candidate_tree,
        record.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
    )
    return _report(candidate_tree, applied=True, action="ran", record=record)


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
        running = _Record(
            candidate_tree=candidate_tree, schema_version=2, state="running"
        )
        dependencies.atomic_write_record(
            candidate_tree,
            running.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        )
        try:
            for environment, path in _JUNIT_ARTIFACTS:
                dependencies.remove_junit_artifact(path)
        except OSError:
            return _write_terminal(
                candidate_tree,
                _artifact_failure_record(
                    candidate_tree,
                    None,
                    stage="pre_hook_cleanup",
                    reason="os_error",
                    environment=environment,
                    path=path,
                ),
                dependencies,
            )
        started = dependencies.monotonic_clock()
        result = dependencies.run_command(("git", "hook", "run", "pre-commit"))
        elapsed = round(
            min(10_800.0, max(0.0, dependencies.monotonic_clock() - started)), 3
        )
        if result.returncode != 0:
            read_failure: tuple[str, Path] | None = None
            current_environment, current_path = _JUNIT_ARTIFACTS[0]

            def read_failed(path: Path) -> bytes | None:
                nonlocal current_environment, current_path
                current_path = path
                current_environment = next(
                    env for env, fixed in _JUNIT_ARTIFACTS if fixed == path
                )
                return dependencies.read_artifact(path)

            try:
                summaries, failed_tests, failed_digests = _failed_junit_evidence(
                    read_failed
                )
                hook_failures = _canonical_hook_failures(
                    result.stdout, result.stderr, summaries
                )
            except OSError:
                read_failure = (current_environment, current_path)
                summaries, failed_tests, failed_digests, hook_failures = (), (), (), ()

            cleanup_failure: tuple[str, Path] | None = None
            for environment, path in _JUNIT_ARTIFACTS:
                try:
                    dependencies.remove_junit_artifact(path)
                except OSError:
                    if cleanup_failure is None:
                        cleanup_failure = (environment, path)
            if cleanup_failure is not None or read_failure is not None:
                selected_failure = (
                    cleanup_failure if cleanup_failure is not None else read_failure
                )
                assert selected_failure is not None
                failed_environment, failed_path = selected_failure
                return _write_terminal(
                    candidate_tree,
                    _artifact_failure_record(
                        candidate_tree,
                        result.returncode,
                        elapsed,
                        stage=(
                            "post_hook_failed_cleanup"
                            if cleanup_failure is not None
                            else "post_hook_failed_evidence_read"
                        ),
                        reason="os_error",
                        environment=failed_environment,
                        path=failed_path,
                        diagnostic=_failure_diagnostic(result.returncode, ()),
                    ),
                    dependencies,
                )
            return _write_terminal(
                candidate_tree,
                _Record(
                    candidate_tree=candidate_tree,
                    schema_version=2,
                    state="terminal-failure",
                    returncode=result.returncode,
                    diagnostic=_failure_diagnostic(result.returncode, summaries),
                    failed_tests=failed_tests,
                    failed_junit_digests=failed_digests,
                    elapsed_seconds=elapsed,
                    hook_failures=tuple(
                        _PersistedHookFailure(
                            hook_id=item.hook_id, diagnostic_lines=item.diagnostic_lines
                        )
                        for item in hook_failures
                    ),
                ),
                dependencies,
            )

        captured: list[tuple[str, bytes, int]] = []
        artifact_failure: tuple[str, str, str, Path] | None = None
        try:
            for environment, path in _JUNIT_ARTIFACTS:
                artifact = dependencies.read_artifact(path)
                test_count = _junit_test_count(artifact)
                if artifact is None or test_count is None:
                    artifact_failure = (
                        "post_hook_success_evidence_read",
                        _unusable_artifact_reason(artifact),
                        environment,
                        path,
                    )
                    break
                captured.append((environment, artifact, test_count))
        except OSError:
            artifact_failure = (
                "post_hook_success_evidence_read",
                "os_error",
                environment,
                path,
            )
        try:
            for environment, path in _JUNIT_ARTIFACTS:
                dependencies.remove_junit_artifact(path)
        except OSError:
            if artifact_failure is None:
                artifact_failure = (
                    "post_hook_success_cleanup",
                    "os_error",
                    environment,
                    path,
                )
        if artifact_failure is not None:
            stage, reason, environment, path = artifact_failure
            return _write_terminal(
                candidate_tree,
                _artifact_failure_record(
                    candidate_tree,
                    result.returncode,
                    elapsed,
                    stage=stage,
                    reason=reason,
                    environment=environment,
                    path=path,
                ),
                dependencies,
            )
        try:
            paths = dependencies.capture_junit_artifacts(
                candidate_tree, tuple(captured)
            )
            if paths is None:
                raise JudgeValidationError("invalid JUnit artifact snapshot")
            persisted = tuple(
                _PersistedJUnitArtifact(
                    environment=cast(Literal["py311", "py314"], environment),
                    path=str(path),
                    sha256=hashlib.sha256(artifact).hexdigest(),
                    test_count=test_count,
                )
                for (environment, artifact, test_count), path in zip(captured, paths)
            )
            if not _valid_junit_artifacts(candidate_tree, persisted):
                raise JudgeValidationError("invalid JUnit artifact snapshot")
        except JUnitSnapshotError as error:
            return _write_terminal(
                candidate_tree,
                _artifact_failure_record(
                    candidate_tree,
                    result.returncode,
                    elapsed,
                    stage="immutable_snapshot",
                    reason="os_error",
                    environment=error.environment,
                    path=error.path,
                ),
                dependencies,
            )
        except (JudgeValidationError, OSError) as error:
            return _write_terminal(
                candidate_tree,
                _artifact_failure_record(
                    candidate_tree,
                    result.returncode,
                    elapsed,
                    stage="immutable_snapshot",
                    reason="invalid_snapshot"
                    if isinstance(error, JudgeValidationError)
                    else "os_error",
                ),
                dependencies,
            )
        return _write_terminal(
            candidate_tree,
            _Record(
                candidate_tree=candidate_tree,
                schema_version=2,
                state="terminal-success",
                returncode=0,
                junit_artifacts=persisted,
                elapsed_seconds=elapsed,
                junit_timings=tuple(
                    summary
                    for environment, artifact, _count in captured
                    if (
                        summary := _junit_timing_summary(
                            cast(Literal["py311", "py314"], environment), artifact
                        )
                    )
                    is not None
                ),
            ),
            dependencies,
        )


def _retry_record(
    candidate_tree: str, original_sha256: str, value: object | None
) -> _Record | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or cast(Mapping[str, object], value).get("original_sha256") != original_sha256
    ):
        raise JudgeValidationError("validation retry record is malformed")
    return _record(
        candidate_tree,
        {
            key: item
            for key, item in cast(Mapping[str, object], value).items()
            if key != "original_sha256"
        },
    )


def retry_judge_validation(
    *, candidate_tree: str, apply: bool, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Preview or run one append-only retry after pre-hook cleanup failed."""
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    original_raw = dependencies.read_record(candidate_tree)
    original = _record(candidate_tree, original_raw)
    if (
        original is None
        or original.state != "terminal-artifact-failure"
        or original.returncode is not None
        or original.artifact_failure is None
        or original.artifact_failure.stage != "pre_hook_cleanup"
        or original.artifact_failure.reason != "os_error"
    ):
        raise JudgeValidationError("validation retry is not permitted")
    if dependencies.current_staged_tree() != candidate_tree:
        raise JudgeValidationError("candidate tree is not the current staged tree")
    assert isinstance(original_raw, Mapping)
    original_sha256 = hashlib.sha256(
        json.dumps(original_raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not apply:
        return JudgeValidationReport(
            candidate_tree, False, "retry_preview", "preview", False, None, None
        )
    with dependencies.candidate_lock(candidate_tree):
        current = dependencies.read_record(candidate_tree)
        if current != original_raw:
            raise JudgeValidationError(
                "original validation record changed before retry"
            )
        existing = _retry_record(
            candidate_tree,
            original_sha256,
            dependencies.read_retry_record(candidate_tree),
        )
        if existing is not None:
            return _report(
                candidate_tree, applied=True, action="existing_retry", record=existing
            )

        def read_retry(tree: str) -> object | None:
            raw = dependencies.read_retry_record(tree)
            if raw is None:
                return None
            record = _retry_record(tree, original_sha256, raw)
            return (
                None
                if record is None
                else record.model_dump(
                    mode="json", exclude_none=True, exclude_defaults=True
                )
            )

        def write_retry(tree: str, record: dict[str, object]) -> None:
            dependencies.atomic_write_retry_record(
                tree, {**record, "original_sha256": original_sha256}
            )

        retry_dependencies = replace(
            dependencies,
            read_record=read_retry,
            atomic_write_record=write_retry,
            candidate_lock=lambda _tree: nullcontext(),
        )
        result = run_judge_validation(
            candidate_tree=candidate_tree, apply=True, dependencies=retry_dependencies
        )
        return replace(result, action="retried")


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
    retry_sha256: str | None = None
    if (
        record.state == "terminal-artifact-failure"
        and record.artifact_failure is not None
        and record.artifact_failure.stage == "pre_hook_cleanup"
    ):
        original_raw = dependencies.read_record(candidate_tree)
        assert isinstance(original_raw, Mapping)
        original_sha256 = hashlib.sha256(
            json.dumps(original_raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        retry = _retry_record(
            candidate_tree,
            original_sha256,
            dependencies.read_retry_record(candidate_tree),
        )
        if retry is not None:
            record = retry
            retry_sha256 = original_sha256
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
        record = (
            _record(candidate_tree, dependencies.read_record(candidate_tree))
            if retry_sha256 is None
            else _retry_record(
                candidate_tree,
                retry_sha256,
                dependencies.read_retry_record(candidate_tree),
            )
        )
        if record is None:
            raise JudgeValidationError("validation record is missing")
    return _report(candidate_tree, applied=False, action="status", record=record)
