# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Measure root coverage ownership through the actual pytest-cov producer."""

from __future__ import annotations

import importlib
import os
import sys
import tomllib
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from runpy import run_path
from xml.etree import ElementTree

import coverage
import pytest
import pytest_cov.plugin
from pytest_subprocess import FakeProcess

import agent_work
from agent_work.core.junit import parse_junit_evidence


@dataclass(frozen=True)
class _Measurement:
    exit_code: int
    files: frozenset[Path]
    statements: dict[Path, frozenset[int]]
    hits: dict[Path, frozenset[int]]
    passed: int


class _PassedCases:
    def __init__(self) -> None:
        self.passed = 0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" and report.passed:
            self.passed += 1


def _measure(
    directory: Path,
    config: Path,
    recipe: list[str],
    *,
    pytest_entrypoint: Callable[..., int],
) -> _Measurement:
    """Invoke the real producer with temporary output paths and no autoload."""
    pytest_config = directory / "pytest.ini"
    pytest_config.write_text("[pytest]\n", encoding="utf-8")
    cases = _PassedCases()
    argv = [
        *recipe,
        f"--cov-config={config}",
        f"--cov-report=xml:{directory / 'coverage.xml'}",
        f"--junitxml={directory / 'junit.xml'}",
        "-c",
        str(pytest_config),
        "--confcutdir",
        str(directory),
        "-p",
        "no:cacheprovider",
        str(directory / "test_probe.py"),
    ]
    with pytest.MonkeyPatch.context() as environment:
        environment.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
        environment.delenv("PYTEST_ADDOPTS", raising=False)
        environment.setenv("COVERAGE_FILE", str(directory / ".coverage"))
        environment.syspath_prepend(str(directory))
        exit_code = pytest_entrypoint(argv, plugins=[pytest_cov.plugin, cases])
    measured = coverage.Coverage(
        data_file=str(directory / ".coverage"), config_file=str(config)
    )
    measured.load()
    data = measured.get_data()
    files = frozenset(Path(name).resolve() for name in data.measured_files())
    xml_report = ElementTree.parse(directory / "coverage.xml").getroot()
    sources = tuple(Path(source.text or "") for source in xml_report.iter("source"))
    statements = {}
    for item in xml_report.iter("class"):
        filename = item.attrib["filename"]
        candidates = {(source / filename).resolve() for source in sources} & files
        assert len(candidates) == 1
        path = candidates.pop()
        statements[path] = frozenset(
            int(line.attrib["number"]) for line in item.iter("line")
        )
    assert statements.keys() == files
    assert sum(len(lines) for lines in statements.values()) == int(
        xml_report.attrib["lines-valid"]
    )
    hits = {path: frozenset(data.lines(str(path)) or ()) for path in files}
    report = ElementTree.parse(directory / "junit.xml").getroot()
    assert all(not list(case) for case in report.iter("testcase"))
    return _Measurement(exit_code, files, statements, hits, cases.passed)


def _coverage_recipe(project_root: Path) -> list[str]:
    """Use the common effective tox recipe, preserving its coverage gate."""
    config = tomllib.loads((project_root / "pyproject.toml").read_text())
    environments = config["tool"]["tox"]["env"]
    recipes = []
    for name in config["tool"]["tox"]["env_list"]:
        command = environments[name]["commands"][0]
        assert command[0] == "pytest"
        recipes.append(
            [
                arg
                for arg in command[1:]
                if not arg.startswith(("--cov-report=", "--junitxml="))
                and arg != f"junit_suite_name={name}"
                and arg != "-o"
            ]
        )
    assert all(recipe == recipes[0] for recipe in recipes)
    assert "--cov" in recipes[0]
    assert "--cov-fail-under=85" in recipes[0]
    return recipes[0]


def _calibrate(directory: Path, recipe: list[str], *, missed: bool) -> _Measurement:
    """Calibrate the unchanged 85 percent gate with a tiny independent owner."""
    directory.mkdir()
    package = directory / "coverage_calibration_owner432"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "hit.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    if missed:
        (package / "miss.py").write_text(
            "def untouched():\n    first = 1\n    second = 2\n    return first + second\n",
            encoding="utf-8",
        )
    (directory / "test_probe.py").write_text(
        "from coverage_calibration_owner432.hit import answer\n"
        "def test_calibration():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    config = directory / "pyproject.toml"
    config.write_text(
        "[tool.coverage.run]\nsource_pkgs = ['coverage_calibration_owner432']\n",
        encoding="utf-8",
    )
    try:
        return _measure(directory, config, recipe, pytest_entrypoint=pytest.main)
    finally:
        for name in tuple(sys.modules):
            if name.startswith("coverage_calibration_owner432") or name == "test_probe":
                del sys.modules[name]
        importlib.invalidate_caches()


def _measure_root(config: Path, foreign: Path) -> coverage.Coverage:
    """Measure canonical production and foreign execution without reporting."""
    measured = coverage.Coverage(config_file=str(config), data_file=None)
    measured.start()
    try:
        facts = parse_junit_evidence(
            b'<testsuite tests="1" failures="0" errors="0"><testcase name="test_one" time="0.01"/></testsuite>',
            serial=False,
            parse_xml=ElementTree.fromstring,
        )
        assert facts.test_count == 1
        assert run_path(str(foreign))["value"] == 42
    finally:
        measured.stop()
    return measured


@pytest.fixture
def coverage_isolation(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Pause enclosing coverage and restore per-case import and environment state."""
    enclosing = request.config.pluginmanager.getplugin("_cov")
    controller = enclosing.cov_controller if enclosing is not None else None
    original_modules = dict(sys.modules)
    original_path = list(sys.path)
    original_caches = dict(sys.path_importer_cache)
    original_environment = dict(os.environ)
    if controller is not None:
        controller.pause()
    try:
        yield
    finally:
        try:
            for name in tuple(sys.modules):
                if name not in original_modules:
                    del sys.modules[name]
            sys.modules.update(original_modules)
            sys.path[:] = original_path
            os.environ.clear()
            os.environ.update(original_environment)
            importlib.invalidate_caches()
            sys.path_importer_cache.clear()
            sys.path_importer_cache.update(original_caches)
        finally:
            if controller is not None:
                controller.resume()


@pytest.mark.usefixtures("coverage_isolation")
@pytest.mark.parametrize("missed", [False, True], ids=["complete", "incomplete"])
def test_effective_tox_floor_accepts_complete_and_rejects_incomplete_coverage(
    project_root: Path,
    tmp_path: Path,
    *,
    missed: bool,
) -> None:
    recipe = _coverage_recipe(project_root)
    measurement = _calibrate(tmp_path / "calibration", recipe, missed=missed)
    assert measurement.passed == 1
    total = sum(len(lines) for lines in measurement.statements.values())
    covered = sum(
        len(lines & measurement.hits[path])
        for path, lines in measurement.statements.items()
    )
    if missed:
        assert measurement.exit_code == pytest.ExitCode.TESTS_FAILED
        assert covered / total == pytest.approx(1 / 3)
        untouched = next(path for path in measurement.files if path.name == "miss.py")
        assert measurement.statements[untouched]
        assert not measurement.hits[untouched]
    else:
        assert measurement.exit_code == pytest.ExitCode.OK
        assert covered == total


@pytest.mark.usefixtures("coverage_isolation")
def test_root_tox_coverage_measures_all_production_and_excludes_tests(
    project_root: Path,
    tmp_path: Path,
    fp: FakeProcess,
) -> None:
    _coverage_recipe(project_root)
    foreign = tmp_path / "template_alias.py"
    foreign.write_text("value = 42\n", encoding="utf-8")
    measured = _measure_root(project_root / "pyproject.toml", foreign)
    data = measured.get_data()
    files = frozenset(Path(filename).resolve() for filename in data.measured_files())
    owner = Path(agent_work.__file__).resolve().parent
    inventory = frozenset(path.resolve() for path in owner.rglob("*.py"))
    assert not fp.calls
    assert files == inventory, (
        "root coverage does not exclusively own complete production package"
    )
    junit = owner / "core" / "junit.py"
    assert data.lines(str(junit))
    assert foreign not in files
    assert Path(__file__).resolve() not in files
    unexecuted = next(
        path
        for path in sorted(inventory)
        if not data.lines(str(path)) and path.name != "__init__.py"
    )
    assert measured.analysis2(str(unexecuted))[1]
