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
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from jinja2 import StrictUndefined, Template

from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult
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
        b"<testcase classname='tests.test_speed' name='test_new' time='0.2'/>"
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


def test_clear_capture_binds_both_full_versions_and_replays_private_transcript(
    tmp_path: Path,
) -> None:
    artifact = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed' name='test_existing' time='0.08'/>"
        b"</testsuite>"
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
