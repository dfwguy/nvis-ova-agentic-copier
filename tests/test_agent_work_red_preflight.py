# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for structured Ty diagnostics in Red preflight."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

TEST_PATH = "tests/test_widget.py"
WORKTREE = Path("/workspace/candidate")


def _diagnostic(
    check_name: str,
    *,
    description: str | None = None,
    severity: str = "major",
    path: str = TEST_PATH,
) -> dict[str, Any]:
    return {
        "check_name": check_name,
        "description": description or f"{check_name}: test diagnostic",
        "severity": severity,
        "fingerprint": "0123456789abcdef",
        "location": {
            "path": path,
            "positions": {
                "begin": {"line": 4, "column": 5},
                "end": {"line": 4, "column": 12},
            },
        },
    }


def _classify(diagnostics: object, *, returncode: int = 1) -> Any:
    preflight = importlib.import_module("agent_work.red_preflight")
    return preflight.classify_ty_diagnostics(
        json.dumps(diagnostics),
        returncode=returncode,
        test_paths=(TEST_PATH,),
        worktree=WORKTREE,
        is_production_module=_owns_production_module,
    )


def _owns_production_module(module: str) -> bool:
    return module in {"agent_work", "agent_work.red_checkpoint"}


def test_red_preflight_imports_after_models_in_fresh_interpreter() -> None:
    result = subprocess.run(
        (
            sys.executable,
            "-c",
            "import agent_work.models, agent_work.red_checkpoint, agent_work.red_preflight",
        ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rule", ("invalid-return-type", "invalid-assignment"))
def test_red_preflight_rejects_intrinsic_test_type_errors(rule: str) -> None:
    description = {
        "invalid-return-type": (
            "invalid-return-type: Return type does not match returned value: "
            'expected `int`, found `Literal["x"]`'
        ),
        "invalid-assignment": (
            "invalid-assignment: Object of type `list[int]` is not assignable "
            "to `list[object]`"
        ),
    }[rule]
    result = _classify([_diagnostic(rule, description=description)])

    assert not result.allowed
    assert result.residual_diagnostics == ()


def test_red_preflight_allows_and_enumerates_missing_production_member() -> None:
    result = _classify(
        [
            _diagnostic(
                "unresolved-attribute",
                description=(
                    "unresolved-attribute: Module `agent_work.red_checkpoint` has no member "
                    "`planned_api`"
                ),
                path=str(WORKTREE / TEST_PATH),
            )
        ]
    )

    assert result.allowed
    assert result.residual_diagnostics == ("unresolved-attribute",)
    assert result.residual_details == (
        "tests/test_widget.py:4:agent_work.red_checkpoint.planned_api",
    )


@pytest.mark.parametrize(
    ("rule", "description"),
    (
        (
            "unresolved-attribute",
            "unresolved-attribute: Module `agent_work` has no member `planned_api`",
        ),
        (
            "unresolved-import",
            "unresolved-import: Module `agent_work` has no member `planned_new_module`",
        ),
        (
            "unresolved-import",
            "unresolved-import: Cannot resolve imported module `agent_work.planned_new_module`",
        ),
    ),
)
def test_red_preflight_allows_missing_production_package_api(
    rule: str, description: str
) -> None:
    result = _classify([_diagnostic(rule, description=description)])

    assert result.allowed
    assert result.residual_diagnostics == (rule,)
    assert "agent_work" in result.residual_details[0]


@pytest.mark.parametrize(
    ("module", "allowed"),
    (("agent_work.newpkg.widget", True), ("other_package.newpkg.widget", False)),
)
def test_red_preflight_classifies_nested_new_module_from_owned_root(
    module: str, allowed: bool
) -> None:
    result = _classify(
        [
            _diagnostic(
                "unresolved-import",
                description=f"unresolved-import: Cannot resolve imported module `{module}`",
            )
        ]
    )

    assert result.allowed is allowed


def test_red_preflight_ownership_lookup_fails_closed_without_worktree(
    tmp_path: Path,
) -> None:
    preflight = importlib.import_module("agent_work.red_preflight")

    assert not preflight.production_module_exists(
        "agent_work.red_checkpoint", tmp_path / "missing"
    )


@pytest.mark.parametrize(
    "description",
    (
        "unresolved-attribute: Object of type `Fake` has no member `oops`",
        "unresolved-attribute: Module `other_package.widget` has no member `planned_api`",
    ),
)
def test_red_preflight_rejects_unrelated_missing_member(description: str) -> None:
    result = _classify(
        [
            _diagnostic(
                "unresolved-attribute",
                description=description,
            )
        ]
    )

    assert not result.allowed


def test_red_preflight_allows_baseline_warning() -> None:
    result = _classify([_diagnostic("possibly-unresolved-reference", severity="minor")])

    assert result.allowed
    assert result.residual_diagnostics == ()


@pytest.mark.parametrize(
    ("payload", "returncode"),
    (
        ({"not": "a diagnostic array"}, 1),
        ([{"check_name": "invalid-return-type"}], 1),
        ([_diagnostic("unresolved-attribute", path="/outside/test_widget.py")], 1),
        ([], 2),
    ),
)
def test_red_preflight_rejects_malformed_or_tool_failure(
    payload: object, returncode: int
) -> None:
    result = _classify(payload, returncode=returncode)

    assert not result.allowed
