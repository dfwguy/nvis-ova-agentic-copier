# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bounded worker projection of candidate timing evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from agent_work.repository import CommandResult, GitRepository, RepositoryError
from tests.test_agent_work_boundaries import _config


@pytest.mark.parametrize("action", ("validate", "status"))
def test_serial_projection_accepts_explicit_unknown_timing_without_proof(
    tmp_path: Path, action: str
) -> None:
    tree = "b" * 40
    report: dict[str, object] = {
        "candidate_tree": tree,
        "state": "terminal-success",
        "exit_code": 0,
        "timing_status": "unknown",
        "timing_unknown": {"environment": "py314", "reason": "os_error"},
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "junit_timings": [],
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0, tree if command[-1] == "write-tree" else json.dumps(report), ""
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("tests/test_speed.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    repository = ObservedRepository(_config(tmp_path), run)
    observed = repository.judge_candidate(
        tmp_path,
        tree,
        base_sha="a" * 40,
        changed_paths=("tests/test_speed.py",),
        action=action,
        timeout_seconds=30,
        timing_review_required=True,
    )

    assert observed["state"] == "terminal-success"
    assert observed["timing_status"] == "unknown"
    assert observed["timing_unknown"] == report["timing_unknown"]
    assert observed["junit_artifacts"] == observed["junit_timings"] == []
    assert observed["timing_review"] is None
    assert observed["evidence_id"]

    for invalid in (
        {"reason": "os_error", "environment": "py399"},
        {"reason": "invalid_xml", "environment": "py314"},
        {"reason": "os_error", "environment": "py314", "path": "/secret"},
    ):
        report["timing_unknown"] = invalid
        with pytest.raises(RepositoryError):
            repository.judge_candidate(
                tmp_path,
                tree,
                base_sha="a" * 40,
                changed_paths=("tests/test_speed.py",),
                action=action,
                timeout_seconds=30,
                timing_review_required=True,
            )

    report["timing_unknown"] = {"environment": "py314", "reason": "os_error"}
    for field, item in (
        (
            "junit_artifacts",
            {
                "environment": "py311",
                "path": str(tmp_path / "private-py311.xml"),
                "sha256": "c" * 64,
                "test_count": 1,
            },
        ),
        (
            "junit_timings",
            {
                "environment": "py311",
                "total_seconds": 0.1,
                "slowest_cases": [],
                "timing_complete": True,
                "over_100ms_cases": [],
            },
        ),
    ):
        report[field] = [item]
        with pytest.raises(
            RepositoryError, match="candidate timing evidence is invalid"
        ):
            repository.judge_candidate(
                tmp_path,
                tree,
                base_sha="a" * 40,
                changed_paths=("tests/test_speed.py",),
                action=action,
                timeout_seconds=30,
                timing_review_required=True,
            )
        report[field] = []


@pytest.mark.parametrize(
    ("state", "returncode", "artifact_reason"),
    (
        ("terminal-success", 0, None),
        ("terminal-failure", 1, None),
        ("terminal-artifact-failure", 1, "focused_budget_exhausted"),
        ("terminal-artifact-failure", 1, "finding_budget_exhausted"),
    ),
)
@pytest.mark.parametrize("action", ("validate", "status"))
def test_trusted_candidate_judge_response_strips_state_paths(
    tmp_path: Path,
    state: str,
    returncode: int,
    artifact_reason: str | None,
    action: str,
) -> None:
    """The controller returns bounded evidence and keeps snapshot paths private."""
    tree = "b" * 40
    hidden = str(tmp_path / "controller-state" / "junit.xml")
    report: dict[str, object] = {
        "candidate_tree": tree,
        "state": state,
        "terminal": True,
        "exit_code": returncode,
        "diagnostic": "JUnit py311 failures=1 errors=0" if returncode else None,
        "hook_failures": [],
        "junit_artifacts": (
            []
            if returncode
            else [
                {
                    "environment": environment,
                    "path": hidden,
                    "sha256": "c" * 64,
                    "test_count": 3,
                }
                for environment in ("py311", "py314")
            ]
        ),
        "failed_tests": ["tests.test_module::test_failed"] if returncode else [],
        "failed_junit_digests": ["d" * 64] if returncode else [],
        "junit_timings": (
            []
            if returncode
            else [
                {
                    "environment": "py311",
                    "total_seconds": 1.25,
                    "slowest_cases": [{"name": "test_known", "seconds": 1.25}],
                    "timing_complete": True,
                    "over_100ms_cases": [
                        {"name": "test_known", "seconds": 1.25},
                        {"name": "test_fourth", "seconds": 0.100001},
                    ],
                }
            ]
        ),
    }
    if artifact_reason is not None:
        report["artifact_failure"] = {
            "stage": "timing_review",
            "reason": artifact_reason,
            "environment": None,
            "path": None,
        }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-1] == "write-tree":
            return CommandResult(0, tree, "")
        assert "validation" in command
        return CommandResult(returncode, json.dumps(report), "")

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return "a" * 40

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("src/example.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return tree

    observed = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path / "worktree",
        tree,
        base_sha="a" * 40,
        changed_paths=("src/example.py",),
        action=action,
        timeout_seconds=30,
    )

    assert observed["state"] == state
    assert observed["failed_tests"] == report["failed_tests"]
    assert hidden not in json.dumps(observed)
    assert observed["evidence_id"]
    if artifact_reason is not None:
        assert observed["artifact_failure"] == {
            "stage": "timing_review",
            "reason": artifact_reason,
        }
        cast(dict[str, object], report["artifact_failure"])["reason"] = "invalid_timing"
        different_reason = ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path / "worktree",
            tree,
            base_sha="a" * 40,
            changed_paths=("src/example.py",),
            action=action,
            timeout_seconds=30,
        )
        assert different_reason["evidence_id"] != observed["evidence_id"]
        for invalid in (
            {"stage": "timing_review", "reason": "arbitrary", "path": None},
            {"stage": "arbitrary", "reason": "invalid_timing", "path": None},
            {"stage": "immutable_snapshot", "reason": "focused_budget_exhausted"},
        ):
            report["artifact_failure"] = invalid
            with pytest.raises(RepositoryError, match="artifact failure"):
                ObservedRepository(_config(tmp_path), run).judge_candidate(
                    tmp_path / "worktree",
                    tree,
                    base_sha="a" * 40,
                    changed_paths=("src/example.py",),
                    action=action,
                    timeout_seconds=30,
                )
    else:
        report["artifact_failure"] = {
            "stage": "timing_review",
            "reason": "invalid_timing",
            "environment": None,
            "path": None,
        }
        with pytest.raises(RepositoryError, match="artifact failure"):
            ObservedRepository(_config(tmp_path), run).judge_candidate(
                tmp_path / "worktree",
                tree,
                base_sha="a" * 40,
                changed_paths=("src/example.py",),
                action=action,
                timeout_seconds=30,
            )
    if returncode == 0:
        assert observed["junit_timings"] == report["junit_timings"]


def test_serial_judge_passes_exact_baseline_and_rejects_missing_timing_proof(
    tmp_path: Path,
) -> None:
    baseline_sha = "a" * 40
    candidate_tree = "b" * 40
    observed_commands: list[tuple[str, ...]] = []
    report: dict[str, object] = {
        "candidate_tree": candidate_tree,
        "state": "terminal-success",
        "exit_code": 0,
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "junit_timings": [],
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        observed_commands.append(command)
        return CommandResult(
            0,
            candidate_tree if command[-1] == "write-tree" else json.dumps(report),
            "",
        )

    class ObservedRepository(GitRepository):
        def head(self, worktree: Path) -> str:
            del worktree
            return baseline_sha

        def changed_paths(self, worktree: Path) -> tuple[str, ...]:
            del worktree
            return ("tests/test_speed.py",)

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            del worktree, base_sha, paths
            return candidate_tree

    with pytest.raises(RepositoryError, match="timing proof"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            candidate_tree,
            base_sha=baseline_sha,
            changed_paths=("tests/test_speed.py",),
            action="validate",
            timeout_seconds=30,
            timing_review_required=True,
        )

    validation_command = next(
        command for command in observed_commands if "validation" in command
    )
    assert validation_command[validation_command.index("--baseline-sha") + 1] == (
        baseline_sha
    )

    class_case = "tests/test_speed.py::TestWidget::test_existing"
    hashed_class_case = f"{class_case}[param_{'a' * 64}]"
    report["timing_review"] = {
        "baseline_sha": baseline_sha,
        "baseline_tree": "c" * 40,
        "candidate_tree": candidate_tree,
        "outcome": "correction_required",
        "transcript_sha256": "d" * 64,
        "runs": {
            environment: {
                source: {
                    "sha256": "e" * 64,
                    "test_count": 2,
                    "timing_complete": True,
                }
                for source in ("baseline", "candidate")
            }
            for environment in ("py311", "py314")
        },
        "findings": [
            {
                "environment": "py311",
                "name": "tests/test_speed.py::test_existing",
                "reason": "corroborated_regression",
                "phase_deltas": {"setup": 0.0, "call": 0.8, "teardown": 0.0},
            },
            {
                "environment": "py314",
                "name": hashed_class_case,
                "reason": "inherited_slow",
                "phase_deltas": None,
            },
        ],
        "omitted_findings": 44,
    }
    report["junit_artifacts"] = [
        {
            "environment": environment,
            "path": str(tmp_path / f"private-{environment}.xml"),
            "sha256": "e" * 64,
            "test_count": 2,
        }
        for environment in ("py311", "py314")
    ]
    report["junit_timings"] = [
        {
            "environment": "py314",
            "total_seconds": 1.1,
            "slowest_cases": [
                {"name": class_case, "seconds": 0.7},
                {"name": hashed_class_case, "seconds": 0.4},
            ],
            "timing_complete": True,
            "over_100ms_cases": [
                {"name": class_case, "seconds": 0.7},
                {"name": hashed_class_case, "seconds": 0.4},
            ],
        }
    ]
    observed = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        candidate_tree,
        base_sha=baseline_sha,
        changed_paths=("tests/test_speed.py",),
        action="validate",
        timeout_seconds=30,
        timing_review_required=True,
    )

    assert observed["timing_review"] == report["timing_review"]
    timing = cast(dict[str, object], report["timing_review"])
    retained_findings = timing["findings"]
    timing["findings"] = [
        {
            "environment": "py311",
            "name": "tests/test_speed.py::test_existing",
            "reason": "inherited_slow",
            "phase_deltas": None,
        }
    ] * 128
    timing["omitted_findings"] = 1
    with pytest.raises(RepositoryError, match="timing proof"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            candidate_tree,
            base_sha=baseline_sha,
            changed_paths=("tests/test_speed.py",),
            action="validate",
            timeout_seconds=30,
            timing_review_required=True,
        )
    timing["findings"] = retained_findings
    timing["omitted_findings"] = 44
    assert (
        cast(list[dict[str, object]], observed["junit_timings"])[0]["over_100ms_cases"]
        == cast(list[dict[str, object]], report["junit_timings"])[0]["over_100ms_cases"]
    )

    report["junit_timings"] = [
        {
            "environment": "py314",
            "total_seconds": 0.4,
            "slowest_cases": [
                {"name": "tests.test_speed::test_existing", "seconds": 0.4}
            ],
            "timing_complete": True,
            "over_100ms_cases": [
                {"name": "tests.test_speed::test_existing", "seconds": 0.4}
            ],
        }
    ]
    interactive = ObservedRepository(_config(tmp_path), run).judge_candidate(
        tmp_path,
        candidate_tree,
        base_sha=baseline_sha,
        changed_paths=("tests/test_speed.py",),
        action="validate",
        timeout_seconds=30,
    )
    assert (
        cast(list[dict[str, object]], interactive["junit_timings"])[0][
            "over_100ms_cases"
        ]
        == cast(list[dict[str, object]], report["junit_timings"])[0]["over_100ms_cases"]
    )

    timing = cast(dict[str, object], report["timing_review"])
    runs = cast(dict[str, object], timing["runs"])
    del runs["py314"]
    with pytest.raises(RepositoryError, match="timing proof"):
        ObservedRepository(_config(tmp_path), run).judge_candidate(
            tmp_path,
            candidate_tree,
            base_sha=baseline_sha,
            changed_paths=("tests/test_speed.py",),
            action="validate",
            timeout_seconds=30,
            timing_review_required=True,
        )
