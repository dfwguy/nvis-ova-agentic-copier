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
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .ports import CommandResult, CommandRunner
from .test_executor import TestExecutionPolicy
from .test_timing import MAX_JUNIT_BYTES, is_safe_timing_case_id, safe_timing_case_id
from .timing_admission import TimingAdmissionLease
from .timing_capture import (
    BaselineJUnitUnavailableError,
    FindingBudgetCaptureError,
    FocusedTimingCaptureError,
    TimingCaptureError,
)

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
_CHEAP_GATE_ID = re.compile(
    r"^(pre-commit-hook|yamllint|shellcheck|readability|ty|kingfisher|ruff-check|ruff-format) failed(?:$|:)"
)
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
_LICENSE_MESSAGE = re.compile(
    r"^(?:missing NVIDIA license header|misplaced NVIDIA license header|"
    r"incomplete NVIDIA license header|unsupported file type|"
    r"external license annotation required(?: for symlink)?)$"
)
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


class _LicenseViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(strict=True)
    message: str = Field(strict=True)


class _LicenseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    outcome: Literal["compliant", "violations"]
    violations: tuple[_LicenseViolation, ...] = Field(max_length=16)


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
        if not (
            is_safe_junit_test_name(self.name) or is_safe_timing_case_id(self.name)
        ):
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
        "timing_review",
    ]
    reason: Literal[
        "os_error",
        "missing",
        "invalid_xml",
        "oversized",
        "invalid_count",
        "invalid_snapshot",
        "invalid_timing",
        "finding_budget_exhausted",
        "focused_budget_exhausted",
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
        "timing_review",
    ]
    reason: Literal[
        "os_error",
        "missing",
        "invalid_xml",
        "oversized",
        "invalid_count",
        "invalid_snapshot",
        "invalid_timing",
        "finding_budget_exhausted",
        "focused_budget_exhausted",
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
        if self.environment is None and self.stage not in {
            "immutable_snapshot",
            "timing_review",
        }:
            raise ValueError("missing fixed artifact path")
        if self.reason == "invalid_snapshot" and (
            self.stage != "immutable_snapshot" or self.environment is not None
        ):
            raise ValueError("invalid snapshot reason")
        if self.reason in {
            "invalid_timing",
            "finding_budget_exhausted",
            "focused_budget_exhausted",
        } and (self.stage != "timing_review" or self.environment is not None):
            raise ValueError("invalid timing review reason")


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
    timing_status: Literal["complete", "unknown"] | None = None
    timing_unknown: dict[str, str] | None = None
    timing_comparison_status: Literal["unknown"] | None = None
    timing_comparison_reason: Literal["baseline_unavailable"] | None = None
    artifact_failure: _PersistedArtifactFailure | None = None
    requested_baseline_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    requested_baseline_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    timing_baseline_tree: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    timing_transcript_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    timing_failure_code: (
        Literal[
            "baseline_hook_failed", "focused_timing_command_failed", "capture_failed"
        ]
        | None
    ) = None

    def model_post_init(self, _context: object) -> None:
        if self.timing_status == "unknown":
            if (
                self.state != "terminal-success"
                or self.returncode != 0
                or self.timing_unknown is None
                or set(self.timing_unknown) != {"environment", "reason"}
                or self.timing_unknown["environment"] not in {"py311", "py314"}
                or self.timing_unknown["reason"] not in {"os_error", "missing"}
                or self.junit_artifacts
                or self.junit_timings
                or self.timing_baseline_tree is not None
                or self.timing_transcript_sha256 is not None
            ):
                raise ValueError("invalid unknown timing evidence")
        elif self.timing_unknown is not None or (
            self.timing_status == "complete" and self.state != "terminal-success"
        ):
            raise ValueError("invalid timing status")
        if self.timing_comparison_status == "unknown":
            if (
                self.timing_comparison_reason != "baseline_unavailable"
                or self.state != "terminal-success"
                or self.returncode != 0
                or self.timing_status != "complete"
                or self.timing_unknown is not None
                or not _valid_junit_artifacts(self.candidate_tree, self.junit_artifacts)
                or tuple(item.environment for item in self.junit_timings)
                != ("py311", "py314")
                or not all(item.timing_complete for item in self.junit_timings)
                or self.timing_baseline_tree is not None
                or self.timing_transcript_sha256 is not None
            ):
                raise ValueError("invalid timing comparison evidence")
        elif self.timing_comparison_reason is not None:
            raise ValueError("invalid timing comparison evidence")
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
        if self.timing_failure_code is not None and not (
            self.state == "terminal-artifact-failure"
            and self.returncode == 0
            and self.artifact_failure is not None
            and self.artifact_failure.stage == "timing_review"
            and _valid_junit_artifacts(self.candidate_tree, self.junit_artifacts)
        ):
            raise ValueError("invalid timing failure code")
        if (self.timing_baseline_tree is None) != (
            self.timing_transcript_sha256 is None
        ) or (
            self.state != "terminal-success" and self.timing_baseline_tree is not None
        ):
            raise ValueError("invalid timing proof identity")
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
            and self.timing_status != "unknown"
            and not _valid_junit_artifacts(self.candidate_tree, self.junit_artifacts)
        ):
            raise ValueError("invalid JUnit artifact evidence")
        if (
            self.state != "terminal-success"
            and self.junit_artifacts
            and not (
                self.state == "terminal-artifact-failure"
                and self.returncode == 0
                and self.artifact_failure is not None
                and self.artifact_failure.stage == "timing_review"
                and _valid_junit_artifacts(self.candidate_tree, self.junit_artifacts)
            )
        ):
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
        "preflight-failure",
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
    timing_failure_code: str | None = None
    timing_status: Literal["complete", "unknown"] | None = None
    timing_unknown: dict[str, str] | None = None
    timing_comparison_status: Literal["unknown"] | None = None
    timing_comparison_reason: Literal["baseline_unavailable"] | None = None

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
    run_license_preflight: Callable[[str], object]
    monotonic_clock: Callable[[], float]
    sleeper: Callable[[float], None]
    test_execution_policy: TestExecutionPolicy | None = None
    run_hook: (
        Callable[[tuple[str, ...], TimingAdmissionLease | None], CommandResult] | None
    ) = None
    project_admission: Callable[[], AbstractContextManager[object]] = nullcontext
    read_artifact: Callable[[Path], bytes | None] = lambda _path: None
    remove_junit_artifact: Callable[[Path], None] = lambda _path: None
    capture_junit_artifacts: Callable[
        [str, tuple[tuple[str, bytes, int], ...]], tuple[Path, ...] | None
    ] = lambda _tree, _artifacts: None
    read_retry_record: Callable[[str], object | None] = lambda _tree: None
    atomic_write_retry_record: Callable[[str, dict[str, object]], None] = (
        lambda _tree, _record: None
    )
    timing_review_required: bool = False
    expected_baseline_tree: str | None = None
    expected_baseline_sha: str | None = None
    capture_timing_review: Callable[
        [str, tuple[tuple[str, bytes, int], ...], str], Mapping[str, object] | None
    ] = lambda _tree, _artifacts, _output: None
    read_timing_transcript: Callable[[str], Mapping[str, object] | None] = (
        lambda _tree: None
    )
    hook_environment: Mapping[str, str] | None = None
    verify_candidate_source: Callable[[str], bool] = lambda _tree: True
    expected_snapshot_path: Callable[[str, str], Path] = lambda tree, environment: (
        Path("/snapshots") / tree / f"test-results-{environment}.xml"
    )
    read_saved_snapshot: Callable[[Path], bytes | None] | None = None
    is_snapshot_symlink: Callable[[Path], bool] = Path.is_symlink
    check_candidate_gate: Callable[[str], CommandResult] | None = None


def _valid_tree(value: str) -> bool:
    return bool(_TREE.fullmatch(value))


def _preflight_failure(
    candidate_tree: str, result: object
) -> JudgeValidationReport | None:
    """Accept only a bounded, consistent checker success envelope."""
    details: tuple[str, ...] = (_FALLBACK_HOOK_LINE,)
    stdout = getattr(result, "stdout", None)
    stderr = getattr(result, "stderr", None)
    returncode = getattr(result, "returncode", None)
    if (
        isinstance(stdout, str)
        and isinstance(stderr, str)
        and len(stdout.encode("utf-8")) <= 16_384
    ):
        try:

            def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
                value: dict[str, object] = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate checker field")
                    value[key] = item
                return value

            payload = _LicenseResult.model_validate(
                json.loads(stdout, object_pairs_hook=unique_pairs)
            )
        except (ValueError, ValidationError):
            payload = None
        if payload is not None:
            if (
                returncode == 0
                and payload.outcome == "compliant"
                and not payload.violations
                and not stderr
            ):
                return None
            if (
                returncode == 1
                and payload.outcome == "violations"
                and payload.violations
                and all(
                    _safe_path(item.path)
                    and len(item.path.encode("utf-8")) <= 200
                    and _LICENSE_MESSAGE.fullmatch(item.message)
                    for item in payload.violations
                )
            ):
                details = tuple(
                    f"{item.path}: {item.message}"
                    for item in payload.violations[:_MAX_HOOK_LINES]
                )
    return JudgeValidationReport(
        candidate_tree,
        True,
        "ran",
        "preflight-failure",
        False,
        None,
        None,
        (HookFailure("license-check", details),),
    )


def license_preflight_compliant(candidate_tree: str, result: object) -> bool:
    """Accept only the existing bounded license checker success envelope."""
    return _preflight_failure(candidate_tree, result) is None


def cheap_gate_failure_id(result: CommandResult | None) -> str:
    """Return a fixed check ID without exposing command output."""
    if result is None or not isinstance(result.stderr, str):
        return "cheap-gate"
    match = _CHEAP_GATE_ID.match(result.stderr)
    return match.group(1) if match is not None else "cheap-gate"


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
    if artifact is None or len(artifact) > MAX_JUNIT_BYTES:
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
    if artifact is None or len(artifact) > MAX_JUNIT_BYTES:
        return None
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return None
    return _declared_junit_count(root)


def _unusable_artifact_reason(artifact: bytes | None) -> str:
    if artifact is None:
        return "missing"
    if len(artifact) > MAX_JUNIT_BYTES:
        return "oversized"
    try:
        root = ElementTree.fromstring(artifact)
    except ElementTree.ParseError:
        return "invalid_xml"
    if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
        return "invalid_xml"
    return "invalid_count"


def _junit_timing_summary(
    environment: Literal["py311", "py314"], artifact: bytes, *, serial: bool = False
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
        try:
            name = (
                safe_timing_case_id(raw_class, raw_name)
                if serial
                else _safe_timing_case_name(raw_class, raw_name)
            )
        except ValueError:
            name = None
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


def _candidate_timing_summaries(
    captured: tuple[tuple[str, bytes, int], ...], *, serial: bool
) -> tuple[_PersistedJUnitTiming, ...]:
    return tuple(
        summary
        for environment, artifact, _count in captured
        if (
            summary := _junit_timing_summary(
                cast(Literal["py311", "py314"], environment), artifact, serial=serial
            )
        )
        is not None
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
        if artifact is None or len(artifact) > MAX_JUNIT_BYTES:
            continue
        counts = _junit_failure_counts(artifact)
        if counts is None:
            continue
        summaries.append(f"{label} failures={counts[0]} errors={counts[1]}")
        digests.append(hashlib.sha256(artifact).hexdigest())
        if len(names) >= 16:
            continue
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
        record.timing_failure_code,
        record.timing_status,
        record.timing_unknown,
        record.timing_comparison_status,
        record.timing_comparison_reason,
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
    junit_artifacts: tuple[_PersistedJUnitArtifact, ...] = (),
    timing_failure_code: Literal[
        "baseline_hook_failed", "focused_timing_command_failed", "capture_failed"
    ]
    | None = None,
) -> _Record:
    """Create a redacted terminal state for unusable or unpublishable evidence."""
    return _Record(
        candidate_tree=candidate_tree,
        schema_version=2,
        state="terminal-artifact-failure",
        returncode=returncode,
        elapsed_seconds=elapsed_seconds,
        diagnostic=diagnostic,
        junit_artifacts=junit_artifacts,
        timing_failure_code=timing_failure_code,
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
    if dependencies.timing_review_required:
        baseline_tree = dependencies.expected_baseline_tree
        if baseline_tree is None or not _valid_tree(baseline_tree):
            raise JudgeValidationError("timing baseline tree is invalid")
        record = _Record.model_validate(
            record.model_dump(exclude_none=True)
            | {
                "requested_baseline_tree": baseline_tree,
                **(
                    {"requested_baseline_sha": dependencies.expected_baseline_sha}
                    if dependencies.expected_baseline_sha is not None
                    else {}
                ),
            }
        )
    dependencies.atomic_write_record(
        candidate_tree,
        record.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
    )
    return _report(candidate_tree, applied=True, action="ran", record=record)


def _timing_transcript_digest(
    transcript: Mapping[str, object] | None,
    *,
    baseline_tree: str,
    candidate_tree: str,
    baseline_sha: str | None = None,
) -> str:
    """Validate a bounded source pair and bind its private transcript by digest."""
    if (
        transcript is None
        or transcript.get("baseline_tree") != baseline_tree
        or transcript.get("candidate_tree") != candidate_tree
        or baseline_sha is not None
        and transcript.get("baseline_sha") != baseline_sha
        or transcript.get("outcome")
        not in {
            "clear",
            "report_only",
            "correction_required",
        }
    ):
        raise JudgeValidationError("timing transcript source pair is invalid")
    try:
        payload = json.dumps(
            transcript, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise JudgeValidationError("timing transcript is malformed") from error
    if len(payload) > 4_194_304:
        raise JudgeValidationError("timing transcript exceeds bound")
    return hashlib.sha256(payload).hexdigest()


def _timing_artifacts_match(
    transcript: Mapping[str, object] | None,
    artifacts: tuple[_PersistedJUnitArtifact, ...],
) -> bool:
    """Bind private candidate full-run summaries to immutable JUnit snapshots."""
    if transcript is None or len(artifacts) != len(_JUNIT_ARTIFACTS):
        return False
    runs = transcript.get("runs")
    if not isinstance(runs, Mapping) or set(runs) != {"py311", "py314"}:
        return False
    runs = cast(Mapping[str, object], runs)
    for artifact in artifacts:
        pair = runs.get(artifact.environment)
        if not isinstance(pair, Mapping):
            return False
        pair = cast(Mapping[str, object], pair)
        candidate = pair.get("candidate")
        if not isinstance(candidate, Mapping):
            return False
        candidate = cast(Mapping[str, object], candidate)
        if (
            candidate.get("sha256") != artifact.sha256
            or candidate.get("test_count") != artifact.test_count
            or candidate.get("timing_complete") is not True
        ):
            return False
    return True


def _timing_failure_code(
    error: Exception,
) -> Literal["baseline_hook_failed", "focused_timing_command_failed", "capture_failed"]:
    """Classify timing failure without retaining private exception text."""
    if isinstance(error, TimingCaptureError):
        if error.code == "baseline_hook_failed":
            return "baseline_hook_failed"
        if error.code == "focused_timing_command_failed":
            return "focused_timing_command_failed"
    return "capture_failed"


def run_judge_validation(
    *, candidate_tree: str, apply: bool, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Preview or run exactly one hook for the current, locked staged tree."""
    if apply and dependencies.test_execution_policy is not None:
        dependencies.test_execution_policy.require_native_hook("judge")
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    if not apply:
        return JudgeValidationReport(
            candidate_tree, False, "preview", "preview", False, None, None
        )
    if dependencies.current_staged_tree() != candidate_tree:
        raise JudgeValidationError("candidate tree is not the current staged tree")
    if dependencies.timing_review_required and not dependencies.verify_candidate_source(
        candidate_tree
    ):
        raise JudgeValidationError("candidate source is not exact before hook")
    with (
        dependencies.project_admission() as lease,
        dependencies.candidate_lock(candidate_tree),
    ):
        record = _record(candidate_tree, dependencies.read_record(candidate_tree))
        if record is not None:
            _verify_required_timing(candidate_tree, record, dependencies)
            return _report(
                candidate_tree, applied=True, action="existing", record=record
            )
        try:
            preflight_result = dependencies.run_license_preflight(candidate_tree)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            preflight_result = None
        preflight = _preflight_failure(candidate_tree, preflight_result)
        if preflight is not None:
            return preflight
        try:
            gate_result = (
                dependencies.check_candidate_gate(candidate_tree)
                if dependencies.check_candidate_gate is not None
                else None
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            gate_result = None
            gate_failed = True
        else:
            gate_failed = gate_result is None or gate_result.returncode != 0
        if dependencies.current_staged_tree() != candidate_tree:
            raise JudgeValidationError("staged tree changed during cheap gate")
        if gate_failed:
            failure_id = cheap_gate_failure_id(gate_result)
            return JudgeValidationReport(
                candidate_tree,
                True,
                "ran",
                "preflight-failure",
                False,
                None,
                (
                    "pre-commit hook missing or not executable; run uv run pre-commit install"
                    if failure_id == "pre-commit-hook"
                    else "cheap candidate gate failed"
                ),
                (HookFailure(failure_id, (_FALLBACK_HOOK_LINE,)),),
            )
        if dependencies.current_staged_tree() != candidate_tree:
            raise JudgeValidationError("candidate tree is not the current staged tree")
        running = _Record(
            candidate_tree=candidate_tree,
            schema_version=2,
            state="running",
            requested_baseline_tree=(
                dependencies.expected_baseline_tree
                if dependencies.timing_review_required
                else None
            ),
            requested_baseline_sha=(
                dependencies.expected_baseline_sha
                if dependencies.timing_review_required
                else None
            ),
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
        hook_command = ("git", "hook", "run", "pre-commit")
        result = (
            dependencies.run_command(
                hook_command, environment=dependencies.hook_environment
            )
            if dependencies.timing_review_required and dependencies.run_hook is None
            else dependencies.run_command(hook_command)
            if dependencies.run_hook is None
            else dependencies.run_hook(
                hook_command,
                lease if isinstance(lease, TimingAdmissionLease) else None,
            )
        )
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
        for environment, path in _JUNIT_ARTIFACTS:
            try:
                artifact = dependencies.read_artifact(path)
            except OSError:
                if artifact_failure is None:
                    artifact_failure = (
                        "post_hook_success_evidence_read",
                        "os_error",
                        environment,
                        path,
                    )
                continue
            test_count = _junit_test_count(artifact)
            if artifact is None or test_count is None:
                reason = _unusable_artifact_reason(artifact)
                if reason != "missing" or artifact_failure is None:
                    artifact_failure = (
                        "post_hook_success_evidence_read",
                        reason,
                        environment,
                        path,
                    )
                if reason != "missing":
                    break
                continue
            captured.append((environment, artifact, test_count))
        cleanup_failure: tuple[str, str, str, Path] | None = None
        try:
            for environment, path in _JUNIT_ARTIFACTS:
                dependencies.remove_junit_artifact(path)
        except OSError:
            cleanup_failure = (
                "post_hook_success_cleanup",
                "os_error",
                environment,
                path,
            )
        if cleanup_failure is not None:
            artifact_failure = cleanup_failure
        if (
            artifact_failure is not None
            and artifact_failure[0] == "post_hook_success_evidence_read"
            and artifact_failure[1] in {"missing", "os_error"}
        ):
            unknown_timing = {
                "environment": artifact_failure[2],
                "reason": artifact_failure[1],
            }
            if (
                dependencies.timing_review_required
                and not dependencies.verify_candidate_source(candidate_tree)
            ):
                return _write_terminal(
                    candidate_tree,
                    _artifact_failure_record(
                        candidate_tree,
                        result.returncode,
                        elapsed,
                        stage="timing_review",
                        reason="invalid_timing",
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
                    elapsed_seconds=elapsed,
                    timing_status="unknown",
                    timing_unknown=unknown_timing,
                ),
                dependencies,
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
        if (
            dependencies.timing_review_required
            and not dependencies.verify_candidate_source(candidate_tree)
        ):
            return _write_terminal(
                candidate_tree,
                _artifact_failure_record(
                    candidate_tree,
                    result.returncode,
                    elapsed,
                    stage="timing_review",
                    reason="invalid_timing",
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
        timing_baseline_tree: str | None = None
        timing_transcript_sha256: str | None = None
        comparison_unknown = False
        summaries = _candidate_timing_summaries(
            tuple(captured), serial=dependencies.timing_review_required
        )
        if dependencies.timing_review_required:
            timing_baseline_tree = dependencies.expected_baseline_tree
            try:
                if (
                    timing_baseline_tree is None
                    or not _valid_tree(timing_baseline_tree)
                    or timing_baseline_tree == candidate_tree
                ):
                    raise JudgeValidationError("timing baseline tree is invalid")
                transcript = dependencies.capture_timing_review(
                    candidate_tree, tuple(captured), result.stdout
                )
                if transcript is None:
                    comparison_unknown = True
                else:
                    timing_transcript_sha256 = _timing_transcript_digest(
                        transcript,
                        baseline_tree=timing_baseline_tree,
                        candidate_tree=candidate_tree,
                        baseline_sha=dependencies.expected_baseline_sha,
                    )
                    if not _timing_artifacts_match(transcript, persisted):
                        raise JudgeValidationError(
                            "timing transcript JUnit pair differs"
                        )
            except (
                JudgeValidationError,
                OSError,
                ValueError,
                TypeError,
                RuntimeError,
            ) as error:
                if isinstance(error, BaselineJUnitUnavailableError) or (
                    isinstance(error, TimingCaptureError)
                    and error.code == "baseline_hook_failed"
                ):
                    comparison_unknown = True
                else:
                    return _write_terminal(
                        candidate_tree,
                        _artifact_failure_record(
                            candidate_tree,
                            result.returncode,
                            elapsed,
                            stage="timing_review",
                            reason=(
                                "finding_budget_exhausted"
                                if isinstance(error, FindingBudgetCaptureError)
                                else "focused_budget_exhausted"
                                if isinstance(error, FocusedTimingCaptureError)
                                else "invalid_timing"
                            ),
                            junit_artifacts=persisted,
                            timing_failure_code=_timing_failure_code(error),
                        ),
                        dependencies,
                    )
            if comparison_unknown:
                if len(summaries) != 2 or not all(
                    summary.timing_complete for summary in summaries
                ):
                    return _write_terminal(
                        candidate_tree,
                        _artifact_failure_record(
                            candidate_tree,
                            result.returncode,
                            elapsed,
                            stage="timing_review",
                            reason="invalid_timing",
                            junit_artifacts=persisted,
                        ),
                        dependencies,
                    )
                timing_baseline_tree = None
        return _write_terminal(
            candidate_tree,
            _Record(
                candidate_tree=candidate_tree,
                schema_version=2,
                state="terminal-success",
                returncode=0,
                junit_artifacts=persisted,
                elapsed_seconds=elapsed,
                timing_baseline_tree=timing_baseline_tree,
                timing_transcript_sha256=timing_transcript_sha256,
                junit_timings=summaries,
                timing_status="complete" if comparison_unknown else None,
                timing_comparison_status="unknown" if comparison_unknown else None,
                timing_comparison_reason=(
                    "baseline_unavailable" if comparison_unknown else None
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


def _saved_junit_pair(
    original: _Record, dependencies: JudgeValidationDependencies
) -> tuple[tuple[str, bytes, int], ...]:
    """Read and verify both immutable candidate JUnit snapshots."""
    if any(
        Path(artifact.path)
        != dependencies.expected_snapshot_path(
            original.candidate_tree, artifact.environment
        )
        for artifact in original.junit_artifacts
    ):
        raise JudgeValidationError("saved JUnit artifact path is outside state root")
    for artifact in original.junit_artifacts:
        path = Path(artifact.path)
        if dependencies.is_snapshot_symlink(path) or dependencies.is_snapshot_symlink(
            path.parent
        ):
            raise JudgeValidationError("saved JUnit snapshot is a symlink")
    read_saved_snapshot = dependencies.read_saved_snapshot or dependencies.read_artifact
    captured: list[tuple[str, bytes, int]] = []
    for artifact in original.junit_artifacts:
        try:
            contents = read_saved_snapshot(Path(artifact.path))
        except OSError as error:
            raise JudgeValidationError("saved JUnit artifact is unavailable") from error
        if (
            contents is None
            or hashlib.sha256(contents).hexdigest() != artifact.sha256
            or _junit_test_count(contents) != artifact.test_count
        ):
            raise JudgeValidationError("saved JUnit artifact changed before retry")
        captured.append((artifact.environment, contents, artifact.test_count))
    return tuple(captured)


def _retry_saved_timing(
    candidate_tree: str,
    original: _Record,
    original_sha256: str,
    dependencies: JudgeValidationDependencies,
) -> JudgeValidationReport:
    """Capture timing from the exact saved successful-hook JUnit pair."""
    captured = _saved_junit_pair(original, dependencies)

    baseline_tree = dependencies.expected_baseline_tree
    if baseline_tree is None or not _valid_tree(baseline_tree):
        raise JudgeValidationError("timing baseline tree is invalid")
    prior_transcript = dependencies.read_timing_transcript(candidate_tree)
    prior_digest: str | None = None
    if prior_transcript is not None:
        prior_digest = _timing_transcript_digest(
            prior_transcript,
            baseline_tree=baseline_tree,
            candidate_tree=candidate_tree,
            baseline_sha=dependencies.expected_baseline_sha,
        )
        if not _timing_artifacts_match(prior_transcript, original.junit_artifacts):
            raise JudgeValidationError("prior timing transcript JUnit pair differs")
    running = _Record(
        candidate_tree=candidate_tree,
        schema_version=2,
        state="running",
        requested_baseline_tree=original.requested_baseline_tree,
        requested_baseline_sha=original.requested_baseline_sha,
    )
    dependencies.atomic_write_retry_record(
        candidate_tree,
        running.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        | {"original_sha256": original_sha256},
    )
    try:
        transcript = dependencies.capture_timing_review(candidate_tree, captured, "")
        digest = _timing_transcript_digest(
            transcript,
            baseline_tree=baseline_tree,
            candidate_tree=candidate_tree,
            baseline_sha=dependencies.expected_baseline_sha,
        )
        if not _timing_artifacts_match(transcript, original.junit_artifacts):
            raise JudgeValidationError("timing transcript JUnit pair differs")
        if prior_digest is not None and prior_digest != digest:
            raise JudgeValidationError("prior timing transcript changed on retry")
    except (
        JudgeValidationError,
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
    ) as error:
        record = _artifact_failure_record(
            candidate_tree,
            0,
            original.elapsed_seconds,
            stage="timing_review",
            reason=(
                "finding_budget_exhausted"
                if isinstance(error, FindingBudgetCaptureError)
                else "focused_budget_exhausted"
                if isinstance(error, FocusedTimingCaptureError)
                else "invalid_timing"
            ),
            junit_artifacts=original.junit_artifacts,
            timing_failure_code=_timing_failure_code(error),
        )
    else:
        record = _Record(
            candidate_tree=candidate_tree,
            schema_version=2,
            state="terminal-success",
            returncode=0,
            junit_artifacts=original.junit_artifacts,
            elapsed_seconds=original.elapsed_seconds,
            timing_baseline_tree=baseline_tree,
            timing_transcript_sha256=digest,
            junit_timings=tuple(
                summary
                for environment, contents, _count in captured
                if (
                    summary := _junit_timing_summary(
                        cast(Literal["py311", "py314"], environment),
                        contents,
                        serial=True,
                    )
                )
                is not None
            ),
        )
    record = _Record.model_validate(
        record.model_dump(exclude_none=True)
        | {
            "requested_baseline_tree": original.requested_baseline_tree,
            "requested_baseline_sha": original.requested_baseline_sha,
        }
    )
    dependencies.atomic_write_retry_record(
        candidate_tree,
        record.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        | {"original_sha256": original_sha256},
    )
    return _report(candidate_tree, applied=True, action="retried", record=record)


def retry_judge_validation(
    *, candidate_tree: str, apply: bool, dependencies: JudgeValidationDependencies
) -> JudgeValidationReport:
    """Retry pre-hook cleanup or saved serial timing evidence once."""
    if not _valid_tree(candidate_tree):
        raise JudgeValidationError("candidate tree is malformed")
    original_raw = dependencies.read_record(candidate_tree)
    original = _record(candidate_tree, original_raw)
    if original is not None:
        _verify_required_timing(candidate_tree, original, dependencies)
    if (
        dependencies.timing_review_required
        and original is not None
        and (
            original.requested_baseline_tree != dependencies.expected_baseline_tree
            or original.requested_baseline_sha != dependencies.expected_baseline_sha
        )
    ):
        raise JudgeValidationError("timing baseline changed before retry")
    cleanup_retry = (
        original is not None
        and original.state == "terminal-artifact-failure"
        and original.returncode is None
        and original.artifact_failure is not None
        and original.artifact_failure.stage == "pre_hook_cleanup"
        and original.artifact_failure.reason == "os_error"
    )
    timing_retry = (
        original is not None
        and dependencies.timing_review_required
        and original.state == "terminal-artifact-failure"
        and original.returncode == 0
        and original.artifact_failure is not None
        and original.artifact_failure.stage == "timing_review"
        and _valid_junit_artifacts(candidate_tree, original.junit_artifacts)
    )
    if not (cleanup_retry or timing_retry):
        raise JudgeValidationError("validation retry is not permitted")
    if dependencies.current_staged_tree() != candidate_tree:
        raise JudgeValidationError("candidate tree is not the current staged tree")
    if timing_retry and not dependencies.verify_candidate_source(candidate_tree):
        raise JudgeValidationError("candidate source changed before timing retry")
    assert isinstance(original_raw, Mapping)
    original_sha256 = hashlib.sha256(
        json.dumps(original_raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if not apply:
        return JudgeValidationReport(
            candidate_tree, False, "retry_preview", "preview", False, None, None
        )
    with (
        dependencies.project_admission() as lease,
        dependencies.candidate_lock(candidate_tree),
    ):
        current = dependencies.read_record(candidate_tree)
        if current != original_raw:
            raise JudgeValidationError(
                "original validation record changed before retry"
            )
        if dependencies.current_staged_tree() != candidate_tree:
            raise JudgeValidationError("candidate tree changed before retry")
        if timing_retry and not dependencies.verify_candidate_source(candidate_tree):
            raise JudgeValidationError("candidate source changed before timing retry")
        existing = _retry_record(
            candidate_tree,
            original_sha256,
            dependencies.read_retry_record(candidate_tree),
        )
        if existing is not None:
            if timing_retry:
                assert original is not None
                _saved_junit_pair(original, dependencies)
                _verify_required_timing(candidate_tree, existing, dependencies)
            return _report(
                candidate_tree, applied=True, action="existing_retry", record=existing
            )
        if timing_retry:
            assert original is not None
            return _retry_saved_timing(
                candidate_tree, original, original_sha256, dependencies
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
            project_admission=lambda: nullcontext(lease),
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
        and record.artifact_failure.stage in {"pre_hook_cleanup", "timing_review"}
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
    _verify_required_timing(candidate_tree, record, dependencies)
    return _report(candidate_tree, applied=False, action="status", record=record)


def _verify_required_timing(
    candidate_tree: str,
    record: _Record,
    dependencies: JudgeValidationDependencies,
) -> None:
    """Reject changed baseline requests and missing or tampered success proof."""
    if (
        record.requested_baseline_tree is not None
        or record.requested_baseline_sha is not None
        or record.timing_transcript_sha256 is not None
        or record.timing_comparison_status is not None
    ) and not dependencies.timing_review_required:
        raise JudgeValidationError("required timing baseline was omitted")
    if not dependencies.timing_review_required:
        return
    if record.requested_baseline_tree != dependencies.expected_baseline_tree:
        raise JudgeValidationError("required timing baseline changed")
    if record.requested_baseline_sha != dependencies.expected_baseline_sha:
        raise JudgeValidationError("required timing baseline commit changed")
    if record.state == "terminal-success":
        if (
            record.timing_status == "unknown"
            or record.timing_comparison_status == "unknown"
        ):
            return
        baseline_tree = dependencies.expected_baseline_tree
        transcript = dependencies.read_timing_transcript(candidate_tree)
        if (
            baseline_tree is None
            or record.timing_baseline_tree != baseline_tree
            or record.timing_transcript_sha256 is None
            or not _timing_artifacts_match(transcript, record.junit_artifacts)
            or _timing_transcript_digest(
                transcript,
                baseline_tree=baseline_tree,
                candidate_tree=candidate_tree,
                baseline_sha=dependencies.expected_baseline_sha,
            )
            != record.timing_transcript_sha256
        ):
            raise JudgeValidationError("required timing transcript is unavailable")
