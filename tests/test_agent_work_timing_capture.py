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

import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from jinja2 import StrictUndefined, Template

from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult
from agent_work.timing_capture import (
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
    assert observed_commands.count(("git", "hook", "run", "pre-commit")) == 1
    assert any(
        command[:3] == ("git", "worktree", "remove") for command in observed_commands
    )


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
