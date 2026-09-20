# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Render fixed CI gate results as a GitLab-compatible JUnit suite."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import BaseModel, ConfigDict, Field, ValidationError

EXPECTED_GATES = (
    "ruff-check",
    "ruff-format",
    "pytest-py311",
    "pytest-py314",
    "yamllint",
    "kingfisher",
    "type-check",
    "license-check",
    "readability-check",
    "shellcheck",
)


class GateResultInput(BaseModel):
    """Validated TSV fields at the CI result-file boundary."""

    model_config = ConfigDict(frozen=True)

    name: Literal[
        "ruff-check",
        "ruff-format",
        "pytest-py311",
        "pytest-py314",
        "yamllint",
        "kingfisher",
        "type-check",
        "license-check",
        "readability-check",
        "shellcheck",
    ]
    status: int = Field(ge=0, le=255)
    duration: float = Field(ge=0, allow_inf_nan=False)


@dataclass(frozen=True)
class GateResult:
    """One completed CI gate and its measured result."""

    name: str
    status: int
    duration: float


def parse_results(lines: Iterable[str]) -> tuple[GateResult, ...]:
    """Parse and validate the complete fixed gate-result manifest."""
    parsed: dict[str, GateResult] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        fields = raw_line.rstrip("\n").split("\t")
        if len(fields) != 3:
            msg = f"line {line_number} must contain name, status, and duration"
            raise ValueError(msg)
        raw_name, raw_status, raw_duration = fields
        try:
            boundary = GateResultInput.model_validate(
                {
                    "name": raw_name,
                    "status": raw_status,
                    "duration": raw_duration,
                }
            )
        except ValidationError as error:
            msg = f"line {line_number} has an invalid gate result"
            raise ValueError(msg) from error
        if boundary.name in parsed:
            msg = f"line {line_number} duplicates gate {boundary.name!r}"
            raise ValueError(msg)
        parsed[boundary.name] = GateResult(
            name=boundary.name,
            status=boundary.status,
            duration=boundary.duration,
        )

    missing = [name for name in EXPECTED_GATES if name not in parsed]
    if missing:
        msg = f"missing CI gate results: {', '.join(missing)}"
        raise ValueError(msg)
    return tuple(parsed[name] for name in EXPECTED_GATES)


def render_junit(results: Sequence[GateResult]) -> str:
    """Render validated gate results as one JUnit test suite."""
    if tuple(result.name for result in results) != EXPECTED_GATES:
        msg = "CI gate results must use the complete canonical order"
        raise ValueError(msg)
    failures = sum(result.status != 0 for result in results)
    displayed_durations = tuple(f"{result.duration:.3f}" for result in results)
    total_duration = sum(
        (Decimal(duration) for duration in displayed_durations),
        start=Decimal(),
    )
    suite = Element(
        "testsuite",
        {
            "name": "CI gates",
            "tests": str(len(results)),
            "failures": str(failures),
            "errors": "0",
            "time": f"{total_duration:.3f}",
        },
    )
    for result, duration in zip(results, displayed_durations, strict=True):
        case = SubElement(
            suite,
            "testcase",
            {
                "classname": "ci.gates",
                "name": result.name,
                "time": duration,
            },
        )
        if result.status != 0:
            failure = SubElement(
                case,
                "failure",
                {"message": f"exit status {result.status}"},
            )
            failure.text = f"See ci-logs/{result.name}.log"
    return tostring(suite, encoding="unicode")


def main() -> int:
    """Read a manifest from stdin and write its JUnit representation."""
    try:
        results = parse_results(sys.stdin)
        report = render_junit(results)
    except ValueError as error:
        sys.stderr.write(f"Invalid CI gate results: {error}\n")
        return 2
    sys.stdout.write(f"{report}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
