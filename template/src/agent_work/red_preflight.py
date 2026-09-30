# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only lint and type preflight for a candidate Red test tree."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .ports import CommandRunner
    from .repository import CommandResult

_MODULE_MEMBER = re.compile(
    r"^(?:unresolved-attribute|unresolved-import): Module `([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)` "
    r"has no member `([A-Za-z_][\w]*)`"
)
_IMPORTED_MODULE = re.compile(
    r"^unresolved-import: Cannot resolve imported module `([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`"
)
_RESIDUAL_DETAIL = re.compile(
    r"^(?:tests|template/tests)/[^:\n]+\.py:[1-9][0-9]*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
)
_MAX_OUTPUT_BYTES = 262_144
_MAX_DIAGNOSTICS = 128


@dataclass(frozen=True)
class TyClassification:
    """Decision and bounded list of expected missing production APIs."""

    allowed: bool
    residual_diagnostics: tuple[str, ...] = ()
    residual_details: tuple[str, ...] = ()


def _test_path(path: str, worktree: Path) -> str | None:
    """Normalize only a declared test path inside the checked worktree."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(worktree)
        except ValueError:
            return None
    normalized = candidate.as_posix()
    pure = PurePosixPath(normalized)
    if pure.as_posix() != normalized or ".." in pure.parts or pure.is_absolute():
        return None
    return normalized


def production_module_exists(module: str, worktree: Path) -> bool:
    """Require the missing member to belong to an existing src module."""
    parts = module.split(".")
    if not parts or not all(part.isidentifier() for part in parts):
        return False
    if not worktree.is_dir():
        return False
    source = worktree / "src" / Path(*parts)
    return source.with_suffix(".py").is_file() or (source / "__init__.py").is_file()


def parse_red_preflight_success(output: str) -> tuple[str, ...] | None:
    """Parse bounded successful preflight evidence without trusting model text."""
    if len(output.encode()) > 32_768:
        return None
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "residual_diagnostics",
        "residual_details",
    }:
        return None
    rules = payload["residual_diagnostics"]
    details = payload["residual_details"]
    if (
        not isinstance(rules, list)
        or not isinstance(details, list)
        or len(rules) != len(details)
        or len(rules) > _MAX_DIAGNOSTICS
        or any(
            not isinstance(rule, str)
            or rule not in {"unresolved-attribute", "unresolved-import"}
            for rule in rules
        )
        or any(
            not isinstance(detail, str)
            or len(detail.encode()) > 512
            or _RESIDUAL_DETAIL.fullmatch(detail) is None
            for detail in details
        )
    ):
        return None
    return tuple(details)


def classify_ty_diagnostics(
    output: str,
    *,
    returncode: int,
    test_paths: tuple[str, ...],
    worktree: Path,
    is_production_module: Callable[[str], bool],
) -> TyClassification:
    """Allow only warnings and missing members of src production modules."""
    rejected = TyClassification(False)
    if returncode not in {0, 1} or len(output.encode()) > _MAX_OUTPUT_BYTES:
        return rejected
    try:
        diagnostics = json.loads(output)
    except (ValueError, TypeError):
        return rejected
    if not isinstance(diagnostics, list) or len(diagnostics) > _MAX_DIAGNOSTICS:
        return rejected
    allowed_paths = set(test_paths)
    residual: list[str] = []
    details: list[str] = []
    for item in diagnostics:
        if not isinstance(item, dict):
            return rejected
        name = item.get("check_name")
        description = item.get("description")
        severity = item.get("severity")
        location = item.get("location")
        positions = location.get("positions") if isinstance(location, Mapping) else None
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not isinstance(item.get("fingerprint"), str)
            or severity not in {"major", "minor", "info"}
            or not isinstance(location, Mapping)
            or not isinstance(location.get("path"), str)
            or _test_path(location["path"], worktree) not in allowed_paths
            or not isinstance(positions, Mapping)
            or not isinstance(positions.get("begin"), Mapping)
            or not isinstance(positions.get("end"), Mapping)
            or any(
                not isinstance(point.get(key), int) or point[key] < 1
                for point in (positions["begin"], positions["end"])
                for key in ("line", "column")
            )
        ):
            return rejected
        if severity != "major":
            continue
        if not description.startswith(f"{name}: "):
            return rejected
        match = _MODULE_MEMBER.fullmatch(description)
        imported = _IMPORTED_MODULE.fullmatch(description)
        if name in {"unresolved-attribute", "unresolved-import"} and match:
            module = match.group(1)
            if not is_production_module(module):
                return rejected
            missing = f"{module}.{match.group(2)}"
        elif name == "unresolved-import" and imported:
            missing = imported.group(1)
            owner = missing.rpartition(".")[0]
            while owner and not is_production_module(owner):
                owner = owner.rpartition(".")[0]
            if not owner:
                return rejected
        else:
            return rejected
        residual.append(name)
        detail = f"{_test_path(location['path'], worktree)}:{positions['begin']['line']}:{missing}"
        if len(detail.encode()) > 512:
            return rejected
        details.append(detail)
    if returncode == 0 and residual:
        return rejected
    if returncode == 1 and not diagnostics:
        return rejected
    return TyClassification(True, tuple(residual), tuple(details))


def run_red_preflight(
    worktree: Path,
    test_paths: tuple[str, ...],
    candidate_tree: str,
    *,
    run_command: CommandRunner,
    is_production_module: Callable[[str], bool],
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    """Check exact candidate test paths without modifying their files."""
    from .repository import CommandResult as Result
    from .repository import RepositoryError

    if not test_paths or any(
        _test_path(path, worktree) != path
        or not path.startswith(("tests/", "template/tests/"))
        or not path.endswith(".py")
        for path in test_paths
    ):
        return Result(1, "", "invalid Red preflight test paths")
    del candidate_tree  # The caller verifies its staged or snapshot tree identity.
    try:
        for argv in (
            ("uv", "run", "ruff", "check", "--no-fix", *test_paths),
            ("uv", "run", "ruff", "format", "--check", *test_paths),
        ):
            result = run_command(
                argv,
                timeout_seconds=120,
                current_directory=worktree,
                environment=environment,
            )
            if result.returncode != 0:
                return Result(1, "", "Red preflight Ruff check failed")
        ty = run_command(
            ("uv", "run", "ty", "check", "--output-format", "gitlab", *test_paths),
            timeout_seconds=120,
            current_directory=worktree,
            environment=environment,
        )
    except (RepositoryError, OSError):
        return Result(1, "", "Red preflight tool could not be completed")
    classification = classify_ty_diagnostics(
        ty.stdout,
        returncode=ty.returncode,
        test_paths=test_paths,
        worktree=worktree,
        is_production_module=is_production_module,
    )
    if ty.stderr or not classification.allowed:
        return Result(1, "", "Red preflight Ty check failed")
    return Result(
        0,
        json.dumps(
            {
                "residual_diagnostics": classification.residual_diagnostics,
                "residual_details": classification.residual_details,
            }
        ),
        "",
    )
