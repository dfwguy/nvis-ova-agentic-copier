# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure, bounded interpretation of injected JUnit XML trees."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

from ..test_timing import MAX_JUNIT_BYTES, safe_timing_case_id

MAX_TIMING_CASES = 1_024
MAX_TIMING_CASE_NAME_BYTES = 256
MAX_TIMING_CASE_LIST_BYTES = 262_144
_MAX_COUNTER = 999_999_999
_NONNEGATIVE_INTEGER = re.compile(r"^[0-9]+$")
_SAFE_PATH = re.compile(r"^(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")
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


@dataclass(frozen=True)
class JUnitCaseTiming:
    """One privacy-safe case identity and duration."""

    name: str
    seconds: float


@dataclass(frozen=True)
class JUnitTimingFacts:
    """Bounded case timing without an environment or transport schema."""

    total_seconds: float
    slowest_cases: tuple[JUnitCaseTiming, ...]
    timing_complete: bool
    over_100ms_cases: tuple[JUnitCaseTiming, ...]


@dataclass(frozen=True)
class JUnitEvidence:
    """Immutable semantic facts and the original artifact digest."""

    test_count: int | None
    failure_counts: tuple[int, int] | None
    failed_tests: tuple[str, ...]
    sha256: str | None
    timing: JUnitTimingFacts | None
    unusable_reason: str | None


def _bounded_counter(value: str | None) -> int | None:
    if (
        value is None
        or len(value) > len(str(_MAX_COUNTER))
        or _NONNEGATIVE_INTEGER.fullmatch(value) is None
    ):
        return None
    parsed = int(value)
    return parsed if str(parsed) == value and parsed <= _MAX_COUNTER else None


def _suite_counts(element: Element) -> tuple[int, int] | None:
    failures = _bounded_counter(element.get("failures"))
    errors = _bounded_counter(element.get("errors"))
    if failures is None or errors is None:
        return None
    return failures, errors


def _failure_counts(root: Element) -> tuple[int, int] | None:
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


def _declared_junit_count(root: Element) -> int | None:
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


def _timing_summary(root: Element, *, serial: bool) -> JUnitTimingFacts | None:
    total = 0.0
    seen = 0
    slowest: list[JUnitCaseTiming] = []
    over_100ms: list[JUnitCaseTiming] = []
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
        slowest.append(JUnitCaseTiming(name=name, seconds=round(seconds, 3)))
        if seconds > 0.100:
            candidate = JUnitCaseTiming(name=name, seconds=seconds)
            case_bytes = len(
                json.dumps(
                    {"name": candidate.name, "seconds": candidate.seconds},
                    separators=(",", ":"),
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
    return JUnitTimingFacts(
        total_seconds=round(total, 3),
        slowest_cases=tuple(
            sorted(slowest, key=lambda item: (-item.seconds, item.name))[:3]
        ),
        timing_complete=timing_complete,
        over_100ms_cases=tuple(over_100ms),
    )


def _failed_names(root: Element) -> tuple[str, ...]:
    names: list[str] = []
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
    return tuple(names)


def parse_junit_evidence(
    artifact: bytes | None,
    *,
    serial: bool,
    parse_xml: Callable[[bytes], Element],
) -> JUnitEvidence:
    """Interpret one bounded artifact once, keeping raw-byte identity separate."""
    if artifact is None:
        return JUnitEvidence(None, None, (), None, None, "missing")
    if len(artifact) > MAX_JUNIT_BYTES:
        return JUnitEvidence(None, None, (), None, None, "oversized")
    digest = hashlib.sha256(artifact).hexdigest()
    try:
        root = parse_xml(artifact)
    except ParseError:
        return JUnitEvidence(None, None, (), digest, None, "invalid_xml")
    if root.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
        return JUnitEvidence(None, None, (), digest, None, "invalid_xml")
    count = _declared_junit_count(root)
    failures = _failure_counts(root)
    return JUnitEvidence(
        count,
        failures,
        _failed_names(root) if failures is not None else (),
        digest,
        _timing_summary(root, serial=serial),
        "invalid_count" if count is None else None,
    )
