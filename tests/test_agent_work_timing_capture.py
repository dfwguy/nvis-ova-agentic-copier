# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for trusted serial timing capture boundaries."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jinja2 import StrictUndefined, Template
from pytest_subprocess import FakeProcess

from agent_work import timing_capture
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult, run_subprocess
from agent_work.timing_capture import (
    FocusedTimingCaptureError,
    TimingCaptureDependencies,
    TimingCaptureError,
    _changed_fixture_names,
    _collect_fixture_consumers,
    _prepared_fingerprint,
    capture_serial_timing_review,
    read_serial_timing_transcript,
)

BASE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
BASE_TREE = "c" * 40


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _dependencies(tmp_path: Path, run: CommandRunner) -> TimingCaptureDependencies:
    return TimingCaptureDependencies(
        run_command=run,
        monotonic_clock=lambda: 0.0,
        candidate_worktree=tmp_path / "candidate",
        state_root=tmp_path / "state",
        environment={},
        atomic_write=_write_bytes,
    )


def test_capture_rejects_omitted_canonical_changed_test_path(tmp_path: Path) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command == ("git", "write-tree"):
            return CommandResult(0, CANDIDATE_TREE, "")
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "tests/test_speed.py\n", "")
        return CommandResult(0, "", "")

    with pytest.raises(TimingCaptureError, match="paths differ"):
        capture_serial_timing_review(
            baseline_sha=BASE_SHA,
            candidate_tree=CANDIDATE_TREE,
            candidate_artifacts=(),
            changed_paths=(),
            dependencies=_dependencies(tmp_path, run),
        )


@pytest.mark.parametrize(
    ("failed_command", "expected_code"),
    (
        ("baseline_hook", "baseline_hook_failed"),
        ("focused_pytest", "focused_timing_command_failed"),
        ("generic_command", "capture_failed"),
    ),
)
def test_capture_classifies_command_failure_by_trusted_stage(
    tmp_path: Path, failed_command: str, expected_code: str
) -> None:
    baseline_artifact = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_existing' time='0.01'/>"
        b"</testsuite>"
    )
    candidate_artifact = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_new' time='1.2'/>"
        b"</testsuite>"
    )
    observed: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        observed.append(command)
        cwd = cast(Path, kwargs["current_directory"])
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "tests/test_speed.py\n", "")
        if command[:4] == ("git", "worktree", "add", "--detach"):
            Path(command[4]).mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(
                0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
            )
        if command == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, BASE_SHA, "")
        if command == ("git", "rev-parse", "HEAD^{tree}"):
            return CommandResult(0, BASE_TREE, "")
        if command == ("git", "hook", "run", "pre-commit"):
            if failed_command == "baseline_hook":
                return CommandResult(1, "", "PRIVATE_TOKEN=secret /private/hook")
            for environment in ("py311", "py314"):
                (cwd / f"test-results-{environment}.xml").write_bytes(baseline_artifact)
            return CommandResult(0, "", "")
        if command[0].endswith("/pytest"):
            return CommandResult(1, "", "PRIVATE_TOKEN=secret /private/pytest")
        if command[1:2] == ("-c",):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                        "distributions": [["pytest", "9.0"]],
                    }
                ),
                "",
            )
        if command == ("git", "status", "--porcelain"):
            return CommandResult(0, "", "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
        } or command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    if failed_command == "generic_command":
        original_run = run

        def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
            if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
                return CommandResult(1, "", "PRIVATE_TOKEN=secret /private/git")
            return original_run(command, **kwargs)

    dependencies = _dependencies(tmp_path, run)
    with pytest.raises(TimingCaptureError) as failure:
        capture_serial_timing_review(
            baseline_sha=BASE_SHA,
            candidate_tree=CANDIDATE_TREE,
            candidate_artifacts=(
                ("py311", candidate_artifact, 1),
                ("py314", candidate_artifact, 1),
            ),
            changed_paths=("tests/test_speed.py",),
            dependencies=dependencies,
        )

    if failed_command == "focused_pytest":
        assert any(command[0].endswith("/pytest") for command in observed)
    if failed_command == "baseline_hook":
        assert ("git", "hook", "run", "pre-commit") in observed
    assert getattr(failure.value, "code", None) == expected_code
    assert "PRIVATE_TOKEN" not in str(failure.value)
    assert "/private/" not in str(failure.value)


def test_prepared_fingerprint_compares_installed_versions_without_source_path(
    tmp_path: Path,
) -> None:
    package_version = "1.0"

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert command[1] == "-c"
        return CommandResult(
            0,
            json.dumps(
                {
                    "interpreter": ["3.11.9", "cpython-311", "linux-x86_64"],
                    "distributions": [["pytest", package_version]],
                },
                sort_keys=True,
            ),
            "",
        )

    dependencies = _dependencies(tmp_path, run)
    first = _prepared_fingerprint(dependencies, tmp_path / "baseline", "py311")
    second = _prepared_fingerprint(dependencies, tmp_path / "candidate", "py311")
    assert first == second


@pytest.mark.parametrize(
    "xml_size", [0, 1_048_577, 4_194_305, 6_291_456, 33_554_432, 33_554_433]
)
def test_clear_capture_binds_both_full_versions_and_replays_private_transcript(
    tmp_path: Path,
    xml_size: int,
) -> None:
    artifact = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_existing' time='0.08'/>"
        b"</testsuite>"
    )
    if xml_size:
        artifact += b" " * (xml_size - len(artifact))
    observed_commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        observed_commands.append(command)
        cwd = cast(Path, kwargs.get("current_directory"))
        if len(command) == 6 and command[:4] == (
            "git",
            "worktree",
            "add",
            "--detach",
        ):
            baseline = Path(command[4])
            baseline.mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            assert cwd.name == "baseline"
            seeded = cast(Mapping[str, str], kwargs["environment"])
            assert seeded["PYTEST_ADDOPTS"] == (
                f"--randomly-seed={int(CANDIDATE_TREE[:8], 16)}"
            )
            for environment in ("py311", "py314"):
                (cwd / f"test-results-{environment}.xml").write_bytes(artifact)
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(
                0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
            )
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command == ("git", "rev-parse", "HEAD"):
            return CommandResult(0, BASE_SHA, "")
        if command == ("git", "rev-parse", "HEAD^{tree}"):
            return CommandResult(0, BASE_TREE, "")
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "tests/test_speed.py\n", "")
        if command == ("git", "diff", "--quiet"):
            return CommandResult(0, "", "")
        if command == ("git", "ls-files", "--others", "--exclude-standard"):
            return CommandResult(0, "", "")
        if command == ("git", "status", "--porcelain"):
            return CommandResult(0, "", "")
        if command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        if len(command) >= 3 and command[1] == "-c":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                        "distributions": [["pytest", "9.0"]],
                    },
                    sort_keys=True,
                ),
                "",
            )
        raise AssertionError(f"unexpected timing capture command: {command}")

    dependencies = _dependencies(tmp_path, run)
    if xml_size > 33_554_432:
        with pytest.raises(TimingCaptureError):
            capture_serial_timing_review(
                baseline_sha=BASE_SHA,
                candidate_tree=CANDIDATE_TREE,
                candidate_artifacts=(("py311", artifact, 1), ("py314", artifact, 1)),
                changed_paths=("tests/test_speed.py",),
                dependencies=dependencies,
            )
        assert not (dependencies.state_root / f"{CANDIDATE_TREE}.timing.json").exists()
        return
    captured = capture_serial_timing_review(
        baseline_sha=BASE_SHA,
        candidate_tree=CANDIDATE_TREE,
        candidate_artifacts=(("py311", artifact, 1), ("py314", artifact, 1)),
        changed_paths=("tests/test_speed.py",),
        dependencies=dependencies,
    )
    replayed = read_serial_timing_transcript(
        candidate_tree=CANDIDATE_TREE, dependencies=dependencies
    )

    assert captured["outcome"] == "clear"
    assert replayed is not None and replayed["runs"] == captured["runs"]
    assert replayed["omitted_findings"] == 0
    legacy = dict(captured)
    del legacy["omitted_findings"]
    (dependencies.state_root / f"{CANDIDATE_TREE}.timing.json").write_text(
        json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    )
    legacy_replay = read_serial_timing_transcript(
        candidate_tree=CANDIDATE_TREE, dependencies=dependencies
    )
    assert legacy_replay == json.loads(json.dumps(legacy))
    assert observed_commands.count(("git", "hook", "run", "pre-commit")) == 1
    assert any(
        command[:3] == ("git", "worktree", "remove") for command in observed_commands
    )


@pytest.mark.parametrize(
    "production_path",
    (
        "src/agent_work/test_timing.py",
        "template/src/agent_work/test_timing.py",
        "scripts/check_licenses.py",
        "template/scripts/check_licenses.py",
    ),
)
def test_production_change_reports_existing_case_without_focused_repeats(
    tmp_path: Path, production_path: str
) -> None:
    crossing = "tests/test_speed.py::test_crossing"
    inherited = "tests/test_speed.py::test_inherited"
    baseline_artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_crossing' time='0.22'/>"
        b"<testcase classname='tests.test_speed' name='test_inherited' time='1.22'/>"
        b"</testsuite>"
    )
    candidate_artifact = (
        b"<testsuite tests='2' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_crossing' time='1.22'/>"
        b"<testcase classname='tests.test_speed' name='test_inherited' time='1.22'/>"
        b"</testsuite>"
    )
    focused_calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = cast(Path, kwargs["current_directory"])
        if command[:4] == ("git", "worktree", "add", "--detach"):
            Path(command[4]).mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            for environment in ("py311", "py314"):
                (cwd / f"test-results-{environment}.xml").write_bytes(baseline_artifact)
            return CommandResult(0, "", "")
        if command[0].endswith("/pytest"):
            focused_calls.append(command)
            environment = cast(Mapping[str, str], kwargs["environment"])
            assert command[-1] == crossing
            call_seconds = 0.22 if cwd.name == "baseline" else 1.22
            Path(environment["AGENT_TIMING_OUTPUT"]).write_text(
                json.dumps(
                    {
                        crossing: {
                            "setup": 0.0,
                            "call": call_seconds,
                            "teardown": 0.0,
                        }
                    }
                )
            )
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(
                0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
            )
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command in {
            ("git", "rev-parse", "HEAD"),
            ("git", "rev-parse", "HEAD^{tree}"),
        }:
            return CommandResult(
                0, BASE_SHA if command[-1] == "HEAD" else BASE_TREE, ""
            )
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, f"{production_path}\n", "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
            ("git", "status", "--porcelain"),
        } or command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        if len(command) >= 3 and command[1] == "-c":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                        "distributions": [["pytest", "9.0"]],
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected timing capture command: {command}")

    captured = capture_serial_timing_review(
        baseline_sha=BASE_SHA,
        candidate_tree=CANDIDATE_TREE,
        candidate_artifacts=(
            ("py311", candidate_artifact, 2),
            ("py314", candidate_artifact, 2),
        ),
        changed_paths=(production_path,),
        dependencies=_dependencies(tmp_path, run),
    )

    assert focused_calls == []
    assert captured["outcome"] == "report_only"
    findings = cast(list[dict[str, object]], captured["findings"])
    assert {(finding["name"], finding["reason"]) for finding in findings} == {
        (crossing, "inherited_slow"),
        (inherited, "inherited_slow"),
    }
    assert set(cast(dict[str, object], captured["runs"])) == {"py311", "py314"}


def test_changed_file_with_many_existing_slow_cases_retains_complete_junit_proof(
    tmp_path: Path,
) -> None:
    artifact = (
        b"<testsuite tests='86' failures='0' errors='0'>"
        + b"".join(
            f"<testcase classname='tests.test_speed' name='test_case_{index}' time='0.101'/>".encode()
            for index in range(86)
        )
        + b"</testsuite>"
    )

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = cast(Path, kwargs.get("current_directory"))
        if len(command) == 6 and command[:4] == (
            "git",
            "worktree",
            "add",
            "--detach",
        ):
            Path(command[4]).mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            assert cwd.name == "baseline"
            for environment in ("py311", "py314"):
                (cwd / f"test-results-{environment}.xml").write_bytes(artifact)
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(
                0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
            )
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command in {
            ("git", "rev-parse", "HEAD"),
            ("git", "rev-parse", "HEAD^{tree}"),
        }:
            return CommandResult(
                0, BASE_SHA if command[-1] == "HEAD" else BASE_TREE, ""
            )
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "tests/test_speed.py\n", "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
            ("git", "status", "--porcelain"),
        } or command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        if len(command) >= 3 and command[1] == "-c":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                        "distributions": [["pytest", "9.0"]],
                    },
                    sort_keys=True,
                ),
                "",
            )
        raise AssertionError(f"unexpected timing capture command: {command}")

    dependencies = _dependencies(tmp_path, run)
    captured = capture_serial_timing_review(
        baseline_sha=BASE_SHA,
        candidate_tree=CANDIDATE_TREE,
        candidate_artifacts=(("py311", artifact, 86), ("py314", artifact, 86)),
        changed_paths=("tests/test_speed.py",),
        dependencies=dependencies,
    )
    replayed = read_serial_timing_transcript(
        candidate_tree=CANDIDATE_TREE, dependencies=dependencies
    )

    assert captured["outcome"] == "report_only"
    assert len(cast(list[object], captured["findings"])) == 128
    assert captured["omitted_findings"] == 44
    assert all(
        finding["reason"] == "touched_slow"
        for finding in cast(list[dict[str, object]], captured["findings"])
    )
    assert replayed is not None
    for field in (
        "baseline_sha",
        "baseline_tree",
        "candidate_tree",
        "outcome",
        "runs",
        "findings",
        "omitted_findings",
    ):
        assert replayed[field] == captured[field]
    runs = cast(dict[str, dict[str, dict[str, object]]], captured["runs"])
    inventories = cast(
        dict[str, dict[str, dict[str, object]]], replayed["full_inventories"]
    )
    for environment in ("py311", "py314"):
        for source, tree in (("baseline", BASE_TREE), ("candidate", CANDIDATE_TREE)):
            assert runs[environment][source] == {
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "test_count": 86,
                "timing_complete": True,
            }
            assert inventories[source][environment]["source_tree"] == tree
            assert inventories[source][environment]["test_count"] == 86
            assert inventories[source][environment]["timing_complete"] is True


def test_capture_replays_complete_large_inventories_with_bounded_transcript(
    tmp_path: Path,
) -> None:
    case_count = 1_600
    names = tuple(
        f"tests/test_speed.py::test_case_{index:04d}_{'x' * 200}"
        for index in range(case_count)
    )
    artifact = (
        f"<testsuite tests='{case_count}' failures='0' errors='0'>".encode()
        + b"".join(
            f"<testcase classname='tests.test_speed' name='{name.split('::')[1]}' time='0.01'/>".encode()
            for name in names
        )
        + b"</testsuite>"
    )
    assert len(artifact) <= 1_048_576
    assert case_count <= 8_192

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = cast(Path, kwargs.get("current_directory"))
        if command[:4] == ("git", "worktree", "add", "--detach"):
            Path(command[4]).mkdir()
            return CommandResult(0, "", "")
        if command == ("git", "hook", "run", "pre-commit"):
            for environment in ("py311", "py314"):
                (cwd / f"test-results-{environment}.xml").write_bytes(artifact)
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(
                0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
            )
        if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
            return CommandResult(0, BASE_TREE, "")
        if command in {
            ("git", "rev-parse", "HEAD"),
            ("git", "rev-parse", "HEAD^{tree}"),
        }:
            return CommandResult(
                0, BASE_SHA if command[-1] == "HEAD" else BASE_TREE, ""
            )
        if command[:3] == ("git", "diff", "--name-only"):
            return CommandResult(0, "src/agent_work/timing_capture.py\n", "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
            ("git", "status", "--porcelain"),
        } or command[:3] == ("git", "worktree", "remove"):
            return CommandResult(0, "", "")
        if len(command) >= 3 and command[1] == "-c":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                        "distributions": [["pytest", "9.0"]],
                    },
                    sort_keys=True,
                ),
                "",
            )
        raise AssertionError(f"unexpected timing capture command: {command}")

    dependencies = _dependencies(tmp_path, run)
    captured = capture_serial_timing_review(
        baseline_sha=BASE_SHA,
        candidate_tree=CANDIDATE_TREE,
        candidate_artifacts=(
            ("py311", artifact, case_count),
            ("py314", artifact, case_count),
        ),
        changed_paths=("src/agent_work/timing_capture.py",),
        dependencies=dependencies,
    )
    replayed = read_serial_timing_transcript(
        candidate_tree=CANDIDATE_TREE, dependencies=dependencies
    )

    assert replayed is not None
    for field in ("outcome", "findings", "omitted_findings"):
        assert replayed[field] == captured[field]
    assert replayed["baseline_sha"] == BASE_SHA
    assert replayed["baseline_tree"] == BASE_TREE
    assert replayed["candidate_tree"] == CANDIDATE_TREE
    assert replayed["seed"] == str(int(CANDIDATE_TREE[:8], 16))
    runs = cast(dict[str, dict[str, dict[str, object]]], replayed["runs"])
    inventories = cast(
        dict[str, dict[str, dict[str, object]]], replayed["full_inventories"]
    )
    expected_cases = dict.fromkeys(names, 0.01)
    for environment in ("py311", "py314"):
        for source, tree in (("baseline", BASE_TREE), ("candidate", CANDIDATE_TREE)):
            full = inventories[source][environment]
            assert "selectors" not in full
            assert full["cases"] == expected_cases
            assert full["source_tree"] == tree
            assert full["environment"] == environment
            assert full["seed"] == replayed["seed"]
            assert full["junit_sha256"] == hashlib.sha256(artifact).hexdigest()
            assert full["test_count"] == case_count
            assert full["timing_complete"] is True
            assert runs[environment][source] == {
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "test_count": case_count,
                "timing_complete": True,
            }
    payload = (dependencies.state_root / f"{CANDIDATE_TREE}.timing.json").read_bytes()
    assert len(payload) <= 4_194_304
    selector_bytes = len(
        json.dumps(dict(zip(names, names, strict=True)), separators=(",", ":")).encode()
    )
    assert len(payload) + 4 * (len(',"selectors":') + selector_bytes) > 4_194_304


def test_focused_runner_binds_multiple_selectors_to_one_exact_source(
    tmp_path: Path,
) -> None:
    from agent_work.timing_capture import _focused_runner

    names = ("tests/test_speed.py::test_first", "tests/test_speed.py::test_second")
    observed: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        cwd = cast(Path, kwargs["current_directory"])
        if command[0].endswith("/pytest"):
            observed.append(command)
            environment = cast(dict[str, str], kwargs["environment"])
            Path(environment["AGENT_TIMING_OUTPUT"]).write_text(
                json.dumps(
                    {
                        name: {"setup": 0.01, "call": 0.2, "teardown": 0.01}
                        for name in names
                    }
                )
            )
            return CommandResult(0, "", "")
        if command == ("git", "write-tree"):
            return CommandResult(0, CANDIDATE_TREE, "")
        if command in {
            ("git", "diff", "--quiet"),
            ("git", "ls-files", "--others", "--exclude-standard"),
        }:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command} in {cwd}")

    dependencies = _dependencies(tmp_path, run)
    private = tmp_path / "private"
    private.mkdir()
    focused = _focused_runner(
        dependencies,
        base=tmp_path / "baseline",
        private=private,
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        environment={},
        selectors={
            CANDIDATE_TREE: {"py311": {name: name for name in names}},
            BASE_TREE: {"py311": {}},
        },
    )

    result = focused(
        source_tree=CANDIDATE_TREE,
        environment="py311",
        prepared_environment="fingerprint",
        argv=("hook",),
        seed="123",
        case_names=names,
    )

    assert len(observed) == 1
    assert observed[0][-2:] == names
    assert set(cast(dict[str, object], result["cases"])) == set(names)


@pytest.mark.parametrize(
    ("elapsed", "expected_error"),
    (
        (901.0, FocusedTimingCaptureError),
        (1.0, TimingCaptureError),
    ),
)
def test_focused_runner_classifies_failed_final_pytest_call(
    tmp_path: Path, elapsed: float, expected_error: type[TimingCaptureError]
) -> None:
    from agent_work.timing_capture import _focused_runner

    now = [0.0]
    case = "tests/test_speed.py::test_slow"

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[0].endswith("/pytest"):
            now[0] = elapsed
            return CommandResult(1, "", "timeout")
        raise AssertionError(f"unexpected command: {command}")

    private = tmp_path / "private"
    private.mkdir()
    focused = _focused_runner(
        replace(_dependencies(tmp_path, run), monotonic_clock=lambda: now[0]),
        base=tmp_path / "baseline",
        private=private,
        baseline_tree=BASE_TREE,
        candidate_tree=CANDIDATE_TREE,
        environment={},
        selectors={
            CANDIDATE_TREE: {"py311": {case: case}},
            BASE_TREE: {"py311": {}},
        },
    )

    with pytest.raises(expected_error) as failure:
        focused(
            source_tree=CANDIDATE_TREE,
            environment="py311",
            prepared_environment="fingerprint",
            argv=("hook",),
            seed="123",
            case_names=(case,),
        )
    assert type(failure.value) is expected_error


def test_tox_matrix_passes_explicit_serial_pytest_seed_into_both_versions(
    project_root: Path,
) -> None:
    template = Template(
        (project_root / "template" / "pyproject.toml.jinja").read_text(),
        undefined=StrictUndefined,
    )
    for contents in (
        (project_root / "pyproject.toml").read_text(),
        template.render(project_slug="my_test_project", description="A test project"),
    ):
        config = tomllib.loads(contents)
        for environment in ("py311", "py314"):
            assert (
                "PYTEST_ADDOPTS"
                in config["tool"]["tox"]["env"][environment]["pass_env"]
            )


def test_changed_conftest_module_constant_rejects_unknown_consumer_map(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root, value in ((baseline, "old"), (candidate, "new")):
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "conftest.py").write_text(f"VALUE = {value!r}\n")

    with pytest.raises(TimingCaptureError, match="module impact map"):
        _changed_fixture_names(
            baseline,
            candidate,
            ("tests/conftest.py",),
            _dependencies(
                tmp_path, lambda command, **_kwargs: CommandResult(0, "", "")
            ),
        )


def test_changed_called_session_fixture_has_known_consumer_map(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root, value in ((baseline, "old"), (candidate, "new")):
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "conftest.py").write_text(
            "import pytest\n"
            "@pytest.fixture(scope='session')\n"
            "def shared_fixture():\n"
            f"    return {value!r}\n"
        )

    assert _changed_fixture_names(
        baseline,
        candidate,
        ("tests/conftest.py",),
        _dependencies(tmp_path, lambda command, **_kwargs: CommandResult(0, "", "")),
    ) == ("shared_fixture",)


def test_changed_fixture_alias_uses_exposed_name(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root, value in ((baseline, "old"), (candidate, "new")):
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "conftest.py").write_text(
            "import pytest\n"
            "@pytest.fixture(name='public_fixture')\n"
            "def shared_fixture():\n"
            f"    return {value!r}\n"
        )

    assert _changed_fixture_names(
        baseline,
        candidate,
        ("tests/conftest.py",),
        _dependencies(tmp_path, lambda command, **_kwargs: CommandResult(0, "", "")),
    ) == ("public_fixture",)


def test_changed_fixture_dynamic_alias_fails_closed(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline"
    candidate = tmp_path / "candidate"
    for root, value in ((baseline, "old"), (candidate, "new")):
        (root / "tests").mkdir(parents=True)
        (root / "tests" / "conftest.py").write_text(
            "import pytest\n"
            "ALIAS = 'public_fixture'\n"
            "@pytest.fixture(name=ALIAS)\n"
            "def shared_fixture():\n"
            f"    return {value!r}\n"
        )

    with pytest.raises(TimingCaptureError, match="impact map is unknown"):
        _changed_fixture_names(
            baseline,
            candidate,
            ("tests/conftest.py",),
            _dependencies(
                tmp_path, lambda command, **_kwargs: CommandResult(0, "", "")
            ),
        )


@pytest.mark.parametrize("malformed", (False, True))
def test_fixture_collection_maps_only_consumers_and_rejects_unknown_lists(
    tmp_path: Path, *, malformed: bool
) -> None:
    first = "tests/test_speed.py::test_first"
    second = "tests/test_speed.py::test_second"
    other = "tests/test_speed.py::test_other"
    selectors = {name: name for name in (first, second, other)}

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        del command
        environment = cast(Mapping[str, str], kwargs["environment"])
        output = Path(environment["AGENT_TIMING_OUTPUT"])
        output.write_text(
            json.dumps(
                {
                    first: ["changed_fixture"],
                    second: ["dependent_fixture", "changed_fixture"],
                    other: ["other_fixture"] if not malformed else "unknown",
                }
            )
        )
        return CommandResult(0, "", "")

    dependencies = _dependencies(tmp_path, run)
    private = tmp_path / "private"
    private.mkdir()
    if malformed:
        with pytest.raises(TimingCaptureError, match="malformed"):
            _collect_fixture_consumers(
                dependencies,
                dependencies.candidate_worktree,
                private,
                ("changed_fixture",),
                selectors,
                {},
            )
    else:
        assert _collect_fixture_consumers(
            dependencies,
            dependencies.candidate_worktree,
            private,
            ("changed_fixture",),
            selectors,
            {},
        ) == (first, second)


@pytest.mark.parametrize(
    "reader_case",
    [
        "positive",
        "lying_stat",
        "overflow",
        "nonbytes",
        "stat_refusal",
        "phase_overflow",
        "stdout_overflow",
        "legacy_positive",
        "legacy_overflow",
        "legacy_nonbytes",
        "legacy_oserror",
    ],
)
@settings(max_examples=5)
@given(
    stat_hint=st.integers(min_value=0, max_value=128),
    padding=st.integers(min_value=0, max_value=32),
)
def test_capture_bounded_reader_checks_stat_then_actual_bytes_before_parsing(
    reader_case: str,
    stat_hint: int,
    padding: int,
) -> None:
    with TemporaryDirectory(prefix="junit-cap-") as directory:
        tmp_path = Path(directory)
        artifact = (
            b"<testsuite tests='1' failures='0' errors='0'>"
            b"<testcase classname='tests.test_speed' name='test_existing' time='0.08'/>"
            b"</testsuite>"
        )
        artifact += b" " * padding
        candidate_artifact = (
            artifact.replace(b"time='0.08'", b"time='1.28'")
            if reader_case == "phase_overflow"
            else artifact
        )
        observed_commands: list[tuple[str, ...]] = []

        def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
            observed_commands.append(command)
            cwd = cast(Path, kwargs.get("current_directory"))
            if len(command) == 6 and command[:4] == (
                "git",
                "worktree",
                "add",
                "--detach",
            ):
                baseline = Path(command[4])
                baseline.mkdir()
                return CommandResult(0, "", "")
            if command == ("git", "hook", "run", "pre-commit"):
                assert cwd.name == "baseline"
                seeded = cast(Mapping[str, str], kwargs["environment"])
                assert seeded["PYTEST_ADDOPTS"] == (
                    f"--randomly-seed={int(CANDIDATE_TREE[:8], 16)}"
                )
                for environment in ("py311", "py314"):
                    (cwd / f"test-results-{environment}.xml").write_bytes(artifact)
                return CommandResult(0, "", "")
            if command == ("git", "write-tree"):
                return CommandResult(
                    0, BASE_TREE if cwd.name == "baseline" else CANDIDATE_TREE, ""
                )
            if command == ("git", "rev-parse", f"{BASE_SHA}^{{tree}}"):
                return CommandResult(0, BASE_TREE, "")
            if command == ("git", "rev-parse", "HEAD"):
                return CommandResult(0, BASE_SHA, "")
            if command == ("git", "rev-parse", "HEAD^{tree}"):
                return CommandResult(0, BASE_TREE, "")
            if command[:3] == ("git", "diff", "--name-only"):
                return CommandResult(0, "tests/test_speed.py\n", "")
            if command == ("git", "diff", "--quiet"):
                return CommandResult(0, "", "")
            if command == ("git", "ls-files", "--others", "--exclude-standard"):
                return CommandResult(0, "", "")
            if command == ("git", "status", "--porcelain"):
                return CommandResult(0, "", "")
            if command[:3] == ("git", "worktree", "remove"):
                return CommandResult(0, "", "")
            if command[0].endswith("/pytest"):
                environment = cast(Mapping[str, str], kwargs["environment"])
                output = Path(environment["AGENT_TIMING_OUTPUT"])
                raw = json.dumps(
                    {
                        "tests/test_speed.py::test_existing": {
                            "setup": 0.0,
                            "call": 0.08,
                            "teardown": 0.0,
                        }
                    }
                ).encode()
                output.write_bytes(raw + b" " * (1_048_577 - len(raw)))
                return CommandResult(0, "", "")
            if len(command) >= 3 and command[1] == "-c":
                if reader_case == "stdout_overflow":
                    return CommandResult(0, " " * 1_048_577, "")
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "interpreter": ["3.11", "cpython-311", "linux-x86_64"],
                            "distributions": [["pytest", "9.0"]],
                        },
                        sort_keys=True,
                    ),
                    "",
                )
            raise AssertionError(f"unexpected timing capture command: {command}")

        reads: list[tuple[Path, int]] = []
        stats: list[Path] = []

        def size(path: Path) -> int:
            stats.append(path)
            if reader_case == "phase_overflow" and path.suffix == ".json":
                return stat_hint
            return (
                33_554_433
                if reader_case == "stat_refusal"
                else (stat_hint if reader_case == "lying_stat" else path.stat().st_size)
            )

        def read(path: Path, limit: int) -> bytes:
            assert path in stats
            assert limit == (1_048_576 if path.suffix == ".json" else 33_554_432)
            reads.append((path, limit))
            if reader_case == "overflow":
                path.write_bytes(artifact + b" " * (limit + 1 - len(artifact)))
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
            if reader_case == "nonbytes":
                return cast(bytes, "private-invalid-type")
            return raw

        legacy_reads: list[Path] = []

        def read_legacy(path: Path) -> bytes:
            if path.suffix != ".xml":
                return path.read_bytes()
            assert reader_case.startswith("legacy_")
            legacy_reads.append(path)
            if reader_case == "legacy_oserror":
                raise OSError("PRIVATE_TOKEN=legacy-reader")
            if reader_case == "legacy_nonbytes":
                return cast(bytes, "PRIVATE_TOKEN=invalid-type")
            if reader_case == "legacy_overflow":
                return artifact + b" " * (33_554_433 - len(artifact))
            return path.read_bytes()

        dependencies = cast(
            Callable[..., TimingCaptureDependencies], TimingCaptureDependencies
        )(
            run_command=run,
            monotonic_clock=lambda: 0.0,
            candidate_worktree=tmp_path / "candidate",
            state_root=tmp_path / "state",
            environment={},
            atomic_write=_write_bytes,
            size=size,
            read_bytes=read_legacy,
            read_bounded_bytes=None if reader_case.startswith("legacy_") else read,
        )

        if reader_case in {
            "overflow",
            "nonbytes",
            "stat_refusal",
            "phase_overflow",
            "stdout_overflow",
            "legacy_overflow",
            "legacy_nonbytes",
            "legacy_oserror",
        }:
            with pytest.raises(TimingCaptureError):
                capture_serial_timing_review(
                    baseline_sha=BASE_SHA,
                    candidate_tree=CANDIDATE_TREE,
                    candidate_artifacts=(
                        ("py311", candidate_artifact, 1),
                        ("py314", candidate_artifact, 1),
                    ),
                    changed_paths=("tests/test_speed.py",),
                    dependencies=dependencies,
                )
            assert (
                len(reads)
                == {
                    "stat_refusal": 0,
                    "overflow": 1,
                    "nonbytes": 1,
                    "phase_overflow": 3,
                    "stdout_overflow": 1,
                    "legacy_overflow": 0,
                    "legacy_nonbytes": 0,
                    "legacy_oserror": 0,
                }[reader_case]
            )
            assert len(legacy_reads) == (1 if reader_case.startswith("legacy_") else 0)
            assert not (
                dependencies.state_root / f"{CANDIDATE_TREE}.timing.json"
            ).exists()
            return
        captured = capture_serial_timing_review(
            baseline_sha=BASE_SHA,
            candidate_tree=CANDIDATE_TREE,
            candidate_artifacts=(
                ("py311", candidate_artifact, 1),
                ("py314", candidate_artifact, 1),
            ),
            changed_paths=("tests/test_speed.py",),
            dependencies=dependencies,
        )
        replayed = read_serial_timing_transcript(
            candidate_tree=CANDIDATE_TREE, dependencies=dependencies
        )

        assert len(reads) == (0 if reader_case == "legacy_positive" else 2)
        assert len(legacy_reads) == (2 if reader_case == "legacy_positive" else 0)
        assert captured["outcome"] == "clear"
        assert replayed is not None and replayed["runs"] == captured["runs"]
        assert replayed["omitted_findings"] == 0
        legacy = dict(captured)
        del legacy["omitted_findings"]
        (dependencies.state_root / f"{CANDIDATE_TREE}.timing.json").write_text(
            json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        )
        legacy_replay = read_serial_timing_transcript(
            candidate_tree=CANDIDATE_TREE, dependencies=dependencies
        )
        assert legacy_replay == json.loads(json.dumps(legacy))
        assert observed_commands.count(("git", "hook", "run", "pre-commit")) == 1
        assert any(
            command[:3] == ("git", "worktree", "remove")
            for command in observed_commands
        )


class BeforeTimingFixture:
    """Synthetic commands and immutable-publication ports for the real producer."""

    def __init__(self, root: Path, *, fault: str = "") -> None:
        self.source = root / "source"
        self.state = root / "state"
        self.source.mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "tests/test_speed.py").write_text(
            "def test_existing():\n    assert 1 == 1\n"
        )
        (self.source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
        (self.source / "uv.lock").write_text("version = 1\n")
        for environment in ("py311", "py314"):
            binary = self.source / ".tox" / environment / "bin"
            binary.mkdir(parents=True)
            for name in ("python", "pytest"):
                (binary / name).write_text("synthetic nonexecuted interpreter")
                (binary / name).chmod(0o700)
        self.fault = fault
        self.commands: list[tuple[str, ...]] = []
        self.command_environments: list[dict[str, str]] = []
        self.publications: list[Path] = []
        self.removals: list[Path] = []
        self.admissions = 0
        self.focused = 0
        self.duration = 0.04
        self.fingerprint_drift = ""
        self.reload_only = False
        self.head = BASE_SHA
        self.tree = BASE_TREE
        self.selectors = ("tests/test_speed.py::test_existing",)
        self.repair_paths = ("tests/test_speed.py",)
        self.tracked_bytes = {
            name: (self.source / name).read_bytes()
            for name in ("tests/test_speed.py", "pyproject.toml", "uv.lock")
        }

    def run(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.commands.append(command)
        environment = dict(cast(Mapping[str, str], kwargs.get("environment") or {}))
        self.command_environments.append(environment)
        if command[0] == "git":
            changed = [
                name
                for name, contents in self.tracked_bytes.items()
                if (self.source / name).read_bytes() != contents
            ]
            if self.reload_only:
                assert "write-tree" not in command
                assert not any(
                    part in command for part in ("worktree", "checkout", "add")
                )
                assert environment.get("GIT_OPTIONAL_LOCKS") == "0"
            if "rev-parse" in command:
                return CommandResult(
                    0, self.tree if command[-1].endswith("{tree}") else self.head, ""
                )
            if "write-tree" in command:
                return CommandResult(0, self.tree, "")
            if "diff" in command and "--quiet" in command:
                return CommandResult(int(bool(changed)), "", "")
            if "status" in command:
                return CommandResult(0, "".join(f" M {name}\n" for name in changed), "")
            if "ls-files" in command:
                return CommandResult(
                    0,
                    ""
                    if "--others" in command
                    else "tests/test_speed.py\npyproject.toml\nuv.lock\n",
                    "",
                )
            return CommandResult(0, "", "")
        if "-c" in command:
            version = "3.14" if "py314" in command[0] else "3.11"
            identity = [version, "cpython-" + version.replace(".", ""), "linux-x86_64"]
            distributions = [["pytest", "9.0"], ["pytest-randomly", "4.0"]]
            if self.fingerprint_drift == "interpreter":
                identity[1] = "PRIVATE_DIFFERENT_INTERPRETER"
            elif self.fingerprint_drift == "distributions":
                distributions[0][1] = "99.0"
            return CommandResult(
                0,
                json.dumps(
                    {"interpreter": identity, "distributions": distributions},
                    sort_keys=True,
                ),
                "",
            )
        assert not self.reload_only
        assert not any(part in command for part in ("hook", "tox"))
        assert all(selector in command for selector in self.selectors)
        output = Path(environment["AGENT_TIMING_OUTPUT"])
        if "--collect-only" in command:
            payload = json.dumps({selector: [] for selector in self.selectors})
            if self.fault == "duplicate_collection":
                payload = '{"tests/test_speed.py::test_existing":[],"tests/test_speed.py::test_existing":[]}'
            output.write_text(payload)
            return CommandResult(0, "", "")
        self.focused += 1
        if self.fault == "source_during":
            self.tree = "f" * 40
        elif self.fault == "dependency_during":
            (self.source / "uv.lock").write_text("version = 99\n")
        elif self.fault == "environment_during":
            self.fingerprint_drift = "distributions"
        if self.fault == "command_failed":
            return CommandResult(1, "PRIVATE_EXTERNAL_OUTPUT", "PRIVATE_ERROR")
        phases: dict[str, object] = {
            "setup": 0.01,
            "call": self.duration - 0.02,
            "teardown": 0.01,
        }
        if self.fault == "nonfinite":
            phases["call"] = float("nan")
        elif self.fault == "boolean_phase":
            phases["setup"] = True
        elif self.fault == "missing_phase":
            del phases["teardown"]
        payload = json.dumps({selector: phases for selector in self.selectors})
        if self.fault == "duplicate_report":
            payload = payload.replace('"setup": 0.01', '"setup": 0.01,"setup": 0.01')
        elif self.fault == "malformed_phase":
            payload = "{invalid"
        elif self.fault == "wrong_test":
            payload = payload.replace("test_existing", "test_foreign")
        output.write_text(payload)
        junit_args = [
            part.removeprefix("--junitxml=")
            for part in command
            if part.startswith("--junitxml=")
        ]
        if not junit_args and "--junitxml" in command:
            junit_args = [command[command.index("--junitxml") + 1]]
        assert len(junit_args) == 1
        duration = self.duration + (0.1 if self.fault == "disagreement" else 0)
        cases = "".join(
            f'<testcase classname="tests.test_speed" name="{selector.split("::")[-1]}" time="{duration:.6f}"/>'
            for selector in self.selectors
        )
        if self.fault == "duplicate_junit":
            cases += cases
        elif self.fault == "skipped":
            cases = cases.replace("/>", "><skipped/></testcase>")
        artifact = f'<testsuite tests="{len(self.selectors)}" failures="0" errors="0" skipped="{int(self.fault == "skipped")}">{cases}</testsuite>'
        Path(junit_args[0]).write_text(artifact)
        return CommandResult(0, "", "")

    @contextmanager
    def admission(self) -> Iterator[None]:
        from agent_work.timing_admission import TimingAdmissionBusy

        self.admissions += 1
        if self.fault == "busy":
            raise TimingAdmissionBusy("PRIVATE_BUSY")
        yield

    def publish(self, temporary: Path, destination: Path) -> None:
        if self.fault == "before_publish":
            raise OSError("PRIVATE_BEFORE_PUBLISH")
        assert not destination.exists()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive mkdir arbitrates collisions; no replacement writer can overwrite.
        destination.mkdir()
        for child in temporary.iterdir():
            if child.is_dir():
                shutil.copytree(child, destination / child.name)
            else:
                shutil.copy2(child, destination / child.name)
        self.publications.append(destination)
        if self.fault == "readback":
            (destination / "manifest.json").write_bytes(b"PRIVATE_CORRUPT_MANIFEST")
        if self.fault == "uncertain_publish":
            raise OSError("PRIVATE_AFTER_PUBLICATION")

    def remove(self, path: Path) -> None:
        self.removals.append(path)
        assert path not in self.publications
        shutil.rmtree(path)

    def read_regular(self, path: Path, limit: int) -> bytes:
        if not path.is_file() or path.is_symlink():
            raise OSError("not a regular file")
        with path.open("rb") as stream:
            return stream.read(limit + 1)

    def binding(self, **changes: object) -> dict[str, object]:
        """Return independent native scope and request values."""
        return {
            "project_id": 55,
            "issue_iid": 324,
            "source_sha": BASE_SHA,
            "source_tree": BASE_TREE,
            "invariant": "existing behavior remains covered",
            "test_ids": self.selectors,
            "repair_paths": self.repair_paths,
        } | changes

    def request(self, **changes: object) -> object:
        """Construct the real boundary request only when each test executes."""
        constructor = cast(Callable[..., object], timing_capture.BeforeTimingRequest)
        return constructor(**self.binding(**changes))

    def scope(self, **changes: object) -> object:
        """Supply a separate immutable Main-owned approval value."""
        constructor = cast(Callable[..., object], timing_capture.BeforeTimingScope)
        return constructor(**self.binding(**changes))

    def dependencies(self) -> object:
        """Compose only genuine external command/filesystem/admission ports."""
        timing = TimingCaptureDependencies(
            run_command=self.run,
            monotonic_clock=lambda: 0.0,
            candidate_worktree=self.source,
            state_root=self.state,
            environment={},
            atomic_write=_write_bytes,
            remove_owned_tree=self.remove,
        )
        constructor = cast(
            Callable[..., object], timing_capture.BeforeTimingDependencies
        )
        return constructor(
            timing=timing,
            publish_snapshot=self.publish,
            project_admission=self.admission,
            read_regular_file=self.read_regular,
        )

    def capture(
        self, *, request: object | None = None, scope: object | None = None
    ) -> Mapping[str, object]:
        """Call the genuine producer, never a fake frontend/result action."""
        producer = cast(
            Callable[..., Mapping[str, object]],
            timing_capture.capture_before_timing,
        )
        return producer(
            self.request() if request is None else request,
            approved_scope=self.scope() if scope is None else scope,
            dependencies=self.dependencies(),
        )

    def reload(
        self, path: Path, *, request: object | None = None, scope: object | None = None
    ) -> Mapping[str, object]:
        """Replay through the public read-only validator and same external ports."""
        producer = cast(
            Callable[..., Mapping[str, object]],
            timing_capture.reload_before_timing,
        )
        self.reload_only = True
        return producer(
            path,
            request=self.request() if request is None else request,
            approved_scope=self.scope() if scope is None else scope,
            dependencies=self.dependencies(),
        )


def test_before_timing_capture_publishes_complete_bound_evidence_then_reloads(
    tmp_path: Path,
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    captured = fixture.capture()
    manifest = Path(cast(str, captured["manifest_path"]))
    assert manifest.is_file() and manifest.parent in fixture.publications
    assert fixture.admissions == 1
    assert fixture.focused == 4
    evidence = cast(dict[str, object], json.loads(manifest.read_bytes()))
    assert evidence["source_sha"] == BASE_SHA
    assert evidence["source_tree"] == BASE_TREE
    assert evidence["test_ids"] == list(fixture.selectors)
    assert evidence["repair_paths"] == list(fixture.repair_paths)
    runs = cast(list[dict[str, object]], evidence["runs"])
    assert {(run["environment"], run["repeat"]) for run in runs} == {
        (environment, repeat) for environment in ("py311", "py314") for repeat in (0, 1)
    }
    for run in runs:
        assert run["seed"] == str(int(BASE_TREE[:8], 16))
        assert fixture.selectors[0] in cast(list[str], run["argv"])
        assert len(cast(str, run["prepared_environment"])) == 64
        for key in ("junit", "phases"):
            artifact = cast(dict[str, str], run[key])
            path = Path(artifact["path"])
            assert path.is_relative_to(manifest.parent) and path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]
    before = {
        path: path.read_bytes() for path in manifest.parent.iterdir() if path.is_file()
    }
    focused = fixture.focused
    publications = tuple(fixture.publications)
    assert fixture.reload(manifest) == captured
    assert fixture.focused == focused and tuple(fixture.publications) == publications
    assert before == {path: path.read_bytes() for path in before}
    assert fixture.admissions == 1


@pytest.mark.parametrize(
    "fault",
    [
        "command_failed",
        "skipped",
        "duplicate_collection",
        "duplicate_report",
        "duplicate_junit",
        "malformed_phase",
        "nonfinite",
        "boolean_phase",
        "missing_phase",
        "wrong_test",
        "disagreement",
        "before_publish",
        "busy",
    ],
)
def test_before_timing_public_producer_refuses_incomplete_evidence_after_control(
    tmp_path: Path, fault: str
) -> None:
    control = BeforeTimingFixture(tmp_path / "control")
    captured = control.capture()
    assert control.reload(Path(cast(str, captured["manifest_path"]))) == captured
    subject = BeforeTimingFixture(tmp_path / "subject", fault=fault)
    with pytest.raises((TimingCaptureError, ValueError, OSError)) as caught:
        subject.capture()
    assert "PRIVATE" not in str(caught.value)
    assert subject.publications == []
    assert not subject.state.exists() or not list(subject.state.iterdir())
    if fault == "busy":
        assert subject.focused == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", 56),
        ("issue_iid", 325),
        ("source_sha", "d" * 40),
        ("source_tree", "e" * 40),
        ("invariant", "different invariant"),
        ("test_ids", ("tests/test_speed.py::test_foreign",)),
        ("repair_paths", ("tests/test_foreign.py",)),
    ],
)
def test_before_timing_request_cannot_expand_separate_controller_scope(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    assert fixture.capture()
    before = tuple(fixture.commands)
    with pytest.raises((TimingCaptureError, ValueError)):
        fixture.capture(
            request=fixture.request(**{field: value}), scope=fixture.scope()
        )
    assert tuple(fixture.commands) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_ids", ()),
        ("test_ids", ("tests/test_speed.py::test_existing",) * 2),
        ("test_ids", ("--collect-only",)),
        ("test_ids", ("../test_speed.py::test_existing",)),
        ("repair_paths", ("src/agent_work/timing_capture.py",)),
        ("repair_paths", ("tests/../src/unsafe.py",)),
        ("repair_paths", ("/tmp/test_speed.py",)),
        ("project_id", True),
        ("issue_iid", "324"),
    ],
)
def test_before_timing_strict_request_rejects_unsafe_or_coerced_selection(
    tmp_path: Path, field: str, value: object
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    assert fixture.capture()
    with pytest.raises((TimingCaptureError, ValueError)):
        fixture.capture(request=fixture.request(**{field: value}))


@pytest.mark.parametrize(
    "drift",
    ["head", "tree", "interpreter", "distributions", "dependency", "source", "scope"],
)
def test_before_timing_reload_refuses_source_environment_and_scope_drift(
    tmp_path: Path, drift: str
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    captured = fixture.capture()
    path = Path(cast(str, captured["manifest_path"]))
    if drift == "head":
        fixture.head = "f" * 40
    elif drift == "tree":
        fixture.tree = "f" * 40
    elif drift in ("interpreter", "distributions"):
        fixture.fingerprint_drift = drift
    elif drift == "dependency":
        (fixture.source / "uv.lock").write_text("version = 99\n")
    elif drift == "source":
        (fixture.source / "tests/test_speed.py").write_text(
            "def test_foreign(): pass\n"
        )
    scope = fixture.scope(invariant="foreign") if drift == "scope" else fixture.scope()
    before = fixture.focused
    with pytest.raises((TimingCaptureError, ValueError)):
        fixture.reload(path, scope=scope)
    assert fixture.focused == before


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "partial",
        "altered",
        "duplicate_json",
        "command",
        "seed",
        "repeat",
        "observer",
        "unsupported",
        "symlink",
        "project",
        "issue",
        "invariant",
        "selectors",
        "paths",
        "source",
    ],
)
def test_before_timing_reload_rejects_altered_or_noncontroller_proof(
    tmp_path: Path, mutation: str
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    captured = fixture.capture()
    path = Path(cast(str, captured["manifest_path"]))
    evidence = cast(dict[str, object], json.loads(path.read_bytes()))
    runs = cast(list[dict[str, object]], evidence["runs"])
    if mutation in ("missing", "altered", "symlink"):
        artifact = Path(cast(dict[str, str], runs[0]["junit"])["path"])
        artifact.unlink()
        if mutation == "altered":
            artifact.write_bytes(b"PRIVATE_ALTERED")
        elif mutation == "symlink":
            artifact.symlink_to(path)
    elif mutation == "partial":
        path.write_bytes(b'{"source_tree":')
    elif mutation == "duplicate_json":
        path.write_bytes(
            b'{"source_tree":"' + BASE_TREE.encode() + b'",' + path.read_bytes()[1:]
        )
    else:
        if mutation == "command":
            runs[0]["argv"] = ["PRIVATE_WORKER_COMMAND"]
        elif mutation == "seed":
            runs[0]["seed"] = "314159"
        elif mutation == "repeat":
            runs.pop()
        elif mutation == "unsupported":
            runs[0]["environment"] = "py312"
        elif mutation == "project":
            evidence["project_id"] = 56
        elif mutation == "issue":
            evidence["issue_iid"] = 325
        elif mutation == "invariant":
            evidence["invariant"] = "different invariant"
        elif mutation == "selectors":
            evidence["test_ids"] = ["tests/test_speed.py::test_foreign"]
        elif mutation == "paths":
            evidence["repair_paths"] = ["src/agent_work/unsafe.py"]
        elif mutation == "source":
            evidence["source_sha"] = "f" * 40
        else:
            evidence = {
                "schema": "observer",
                "source_tree": BASE_TREE,
                "measurements": runs,
            }
        path.write_text(json.dumps(evidence))
    before = fixture.focused
    with pytest.raises((TimingCaptureError, ValueError, OSError)) as caught:
        fixture.reload(path)
    assert "PRIVATE" not in str(caught.value)
    assert fixture.focused == before


def test_before_timing_exclusive_publication_preserves_existing_and_uncertain_artifacts(
    tmp_path: Path,
) -> None:
    fixture = BeforeTimingFixture(tmp_path / "existing")
    captured = fixture.capture()
    path = Path(cast(str, captured["manifest_path"]))
    before = {
        item: item.read_bytes() for item in path.parent.iterdir() if item.is_file()
    }
    with pytest.raises((TimingCaptureError, OSError)):
        fixture.capture()
    assert before == {item: item.read_bytes() for item in before}
    uncertain = BeforeTimingFixture(tmp_path / "uncertain", fault="uncertain_publish")
    with pytest.raises((TimingCaptureError, OSError)):
        uncertain.capture()
    assert len(uncertain.publications) == 1
    retained = uncertain.publications[0]
    assert (retained / "manifest.json").is_file()
    assert retained not in uncertain.removals
    uncertain.fault = ""
    assert uncertain.reload(retained / "manifest.json")


@settings(max_examples=8)
@given(milliseconds=st.integers(min_value=20, max_value=999))
def test_before_timing_generated_duration_rules_keep_exact_producer_control(
    tmp_path_factory: pytest.TempPathFactory, milliseconds: int
) -> None:
    root = tmp_path_factory.mktemp("before-duration")
    control = BeforeTimingFixture(root / "control")
    control.duration = milliseconds / 1000
    captured = control.capture()
    assert control.reload(Path(cast(str, captured["manifest_path"]))) == captured
    subject = BeforeTimingFixture(root / "subject", fault="disagreement")
    subject.duration = milliseconds / 1000
    with pytest.raises((TimingCaptureError, ValueError)):
        subject.capture()


@pytest.mark.parametrize(
    "fault", ["source_during", "dependency_during", "environment_during"]
)
def test_before_timing_capture_refuses_provenance_change_before_publication(
    tmp_path: Path, fault: str
) -> None:
    control = BeforeTimingFixture(tmp_path / "control")
    assert control.capture()
    subject = BeforeTimingFixture(tmp_path / "subject", fault=fault)
    with pytest.raises((TimingCaptureError, ValueError)):
        subject.capture()
    assert subject.publications == []


def test_before_timing_capture_without_native_scope_has_no_execution_or_publication(
    tmp_path: Path,
) -> None:
    fixture = BeforeTimingFixture(tmp_path)
    control = fixture.capture()
    assert control
    before = tuple(fixture.commands)
    producer = cast(
        Callable[..., Mapping[str, object]], timing_capture.capture_before_timing
    )
    with pytest.raises((TimingCaptureError, ValueError)):
        producer(
            fixture.request(), approved_scope=None, dependencies=fixture.dependencies()
        )
    assert tuple(fixture.commands) == before


def test_before_timing_published_readback_failure_preserves_untrusted_snapshot(
    tmp_path: Path,
) -> None:
    control = BeforeTimingFixture(tmp_path / "control")
    assert control.capture()
    subject = BeforeTimingFixture(tmp_path / "subject", fault="readback")
    with pytest.raises((TimingCaptureError, ValueError)):
        subject.capture()
    assert len(subject.publications) == 1
    destination = subject.publications[0]
    assert destination.exists() and destination not in subject.removals
    with pytest.raises((TimingCaptureError, ValueError)):
        subject.reload(destination / "manifest.json")


@settings(max_examples=6)
@given(parameter=st.integers(min_value=0, max_value=99))
def test_before_timing_generated_exact_selector_identity_is_bound_at_reload(
    tmp_path_factory: pytest.TempPathFactory, parameter: int
) -> None:
    root = tmp_path_factory.mktemp("before-selector")
    fixture = BeforeTimingFixture(root)
    fixture.selectors = (f"tests/test_speed.py::test_existing[{parameter}]",)
    (fixture.source / "tests/test_speed.py").write_text(
        "import pytest\n"
        f"@pytest.mark.parametrize('value', [{parameter}])\n"
        "def test_existing(value):\n    assert value >= 0\n"
    )
    fixture.tracked_bytes["tests/test_speed.py"] = (
        fixture.source / "tests/test_speed.py"
    ).read_bytes()
    captured = fixture.capture()
    path = Path(cast(str, captured["manifest_path"]))
    assert fixture.reload(path) == captured
    with pytest.raises((TimingCaptureError, ValueError)):
        fixture.reload(
            path,
            request=fixture.request(
                test_ids=("tests/test_speed.py::test_existing[foreign]",)
            ),
        )


class NativeCommandFailureFixture(BeforeTimingFixture):
    """Exercise the native OS adapter through the public command port."""

    def __init__(
        self, root: Path, *, fp: FakeProcess, native_error: str, stage: str
    ) -> None:
        super().__init__(root)
        self.fp = fp
        self.fp.allow_unregistered(False)
        self.native_error = native_error
        self.stage = stage
        self.armed = False
        self.native_attempts = 0

    def run(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        focused = "pytest" in Path(command[0]).name and "--collect-only" not in command
        if self.armed and (
            (self.stage == "focused" and focused)
            or (self.stage == "published" and bool(self.publications))
            or self.stage in ("reload", "uncertain")
        ):
            self.native_attempts += 1

            def native_failure(_process: object) -> None:
                if self.native_error == "missing":
                    raise OSError("PRIVATE_NATIVE_COMMAND")
                raise subprocess.TimeoutExpired(
                    command,
                    1,
                    output="PRIVATE_NATIVE_OUTPUT",
                    stderr="PRIVATE_NATIVE_ERROR",
                )

            self.fp.register(command, callback=native_failure)
            try:
                return run_subprocess(command, timeout_seconds=1)
            finally:
                assert self.fp.call_count(command) == 1
        return super().run(command, **kwargs)


def before_timing_snapshot_bytes(root: Path) -> dict[Path, bytes]:
    """Read persisted bytes independently of the producer."""
    return {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("native_error", ["missing", "timeout"])
@pytest.mark.parametrize("stage", ["focused", "published", "reload", "uncertain"])
def test_before_timing_native_command_failure_is_safe_and_preserves_publication(
    tmp_path: Path, fp: FakeProcess, native_error: str, stage: str
) -> None:
    control = BeforeTimingFixture(tmp_path / "control")
    control_manifest = Path(str(control.capture()["manifest_path"]))
    assert control.reload(control_manifest)["manifest_path"] == str(control_manifest)
    assert control.focused == 4
    subject = NativeCommandFailureFixture(
        tmp_path / "subject", fp=fp, native_error=native_error, stage=stage
    )
    manifest = Path()
    before: dict[Path, bytes] = {}
    removals: tuple[Path, ...] = ()
    if stage in ("reload", "uncertain"):
        if stage == "uncertain":
            subject.fault = "uncertain_publish"
            with pytest.raises(TimingCaptureError):
                subject.capture()
            subject.fault = ""
            manifest = subject.publications[0] / "manifest.json"
            assert subject.reload(manifest)["manifest_path"] == str(manifest)
        else:
            manifest = Path(str(subject.capture()["manifest_path"]))
        before = before_timing_snapshot_bytes(subject.state)
        removals = tuple(subject.removals)
    subject.armed = True
    with pytest.raises(TimingCaptureError) as error:
        if stage in ("reload", "uncertain"):
            subject.reload(manifest)
        else:
            subject.capture()
    assert "PRIVATE" not in str(error.value)
    assert str(subject.source) not in str(error.value)
    assert subject.native_attempts == 1
    if stage == "focused":
        assert subject.publications == []
        assert len(subject.removals) == 1 and not subject.removals[0].exists()
        assert before_timing_snapshot_bytes(subject.state) == {}
    elif stage == "published":
        assert len(subject.publications) == 1 and subject.focused == 4
        assert len(subject.removals) == 1 and not subject.removals[0].exists()
        published = before_timing_snapshot_bytes(subject.state)
        subject.armed = False
        manifest = subject.publications[0] / "manifest.json"
        assert subject.reload(manifest)["manifest_path"] == str(manifest)
        assert published == before_timing_snapshot_bytes(subject.state)
    else:
        assert before == before_timing_snapshot_bytes(subject.state)
        assert tuple(subject.removals) == removals
        assert subject.focused == 4 and len(subject.publications) == 1
