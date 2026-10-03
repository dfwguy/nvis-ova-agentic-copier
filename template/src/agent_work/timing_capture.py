# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Trusted private capture of serial baseline and candidate timing evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .ports import CommandRunner
from .test_timing import (
    MAX_JUNIT_BYTES,
    FindingBudgetExhausted,
    FocusedTimingBudgetExhausted,
    is_safe_timing_case_id,
    parse_full_junit_records,
    review_test_timings,
    validate_before_timing_records,
)
from .timing_admission import TimingAdmissionBusy

_ENVIRONMENTS = ("py311", "py314")
_MAX_BYTES = 1_048_576
_MAX_TRANSCRIPT_BYTES = 4_194_304
_TREE = re.compile(r"^[0-9a-f]{40}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_PLUGIN = """import json
import os
from pathlib import Path

_reports = {}

def pytest_runtest_logreport(report):
    if report.when in ("setup", "call", "teardown"):
        _reports.setdefault(report.nodeid, {})[report.when] = report.duration

def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["AGENT_TIMING_OUTPUT"]).write_text(json.dumps(_reports))
"""
_COLLECT_PLUGIN = """import json
import os
from pathlib import Path

def pytest_collection_finish(session):
    items = {item.nodeid: sorted(set(item.fixturenames)) for item in session.items}
    Path(os.environ["AGENT_TIMING_OUTPUT"]).write_text(json.dumps(items))
"""
_DISTRIBUTIONS = (
    "import importlib.metadata as m, json, sys, sysconfig; "
    "print(json.dumps({'interpreter': "
    "(sys.version, sys.implementation.cache_tag, sysconfig.get_platform()), "
    "'distributions': sorted((d.metadata['Name'].lower(), d.version) "
    "for d in m.distributions())}, sort_keys=True))"
)


def _write_text(path: Path, value: str) -> None:
    path.write_text(value)


class TimingCaptureError(RuntimeError):
    """A source, environment, inventory, or cleanup proof is incomplete."""

    code = "capture_failed"

    def __init__(
        self,
        message: str,
        *,
        code: Literal[
            "baseline_hook_failed", "focused_timing_command_failed", "capture_failed"
        ]
        | None = None,
    ) -> None:
        super().__init__(message)
        selected = code if code is not None else self.__class__.code
        if selected not in {
            "baseline_hook_failed",
            "focused_timing_command_failed",
            "capture_failed",
        }:
            raise ValueError("invalid timing failure code")
        self.code = selected


class BaselineJUnitUnavailableError(TimingCaptureError):
    """The baseline hook did not produce a required JUnit artifact."""

    def __init__(self) -> None:
        super().__init__("baseline JUnit artifact is unavailable")


class FocusedTimingCaptureError(TimingCaptureError):
    """Exact-source focused comparisons exhausted their bounded budget."""


class FindingBudgetCaptureError(TimingCaptureError):
    """The timing review cannot retain every critical finding."""


class _PrivateTranscript(BaseModel):
    """Validate private replay data before status trusts its source claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    baseline_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    seed: str = Field(pattern=r"^[0-9]{1,10}$")
    outcome: Literal["clear", "report_only", "correction_required"]
    runs: dict[str, object]
    findings: list[dict[str, object]] = Field(max_length=128)
    omitted_findings: int = Field(default=0, ge=0, le=32_768)
    full_inventories: dict[str, object]

    def model_post_init(self, _context: object) -> None:
        if (
            self.baseline_tree == self.candidate_tree
            or set(self.runs) != set(_ENVIRONMENTS)
            or set(self.full_inventories) != {"baseline", "candidate"}
        ):
            raise ValueError("timing transcript source pair is incomplete")
        for environment in _ENVIRONMENTS:
            raw_pair = self.runs[environment]
            if not isinstance(raw_pair, dict) or set(raw_pair) != {
                "baseline",
                "candidate",
            }:
                raise ValueError("timing transcript JUnit pair is incomplete")
            pair = cast(dict[str, object], raw_pair)
            for source in ("baseline", "candidate"):
                raw_run = pair[source]
                raw_inventory = self.full_inventories[source]
                if not isinstance(raw_run, dict) or not isinstance(raw_inventory, dict):
                    raise TypeError("timing transcript inventory is invalid")
                run = cast(dict[str, object], raw_run)
                inventory = cast(dict[str, object], raw_inventory)
                raw_full = inventory.get(environment)
                if not isinstance(raw_full, dict):
                    raise TypeError("timing transcript inventory is invalid")
                full = cast(dict[str, object], raw_full)
                cases = full.get("cases")
                if (
                    full.get("source_tree")
                    != (
                        self.baseline_tree
                        if source == "baseline"
                        else self.candidate_tree
                    )
                    or full.get("environment") != environment
                    or full.get("seed") != self.seed
                    or full.get("junit_sha256") != run.get("sha256")
                    or full.get("test_count") != run.get("test_count")
                    or full.get("timing_complete") is not True
                    or run.get("timing_complete") is not True
                    or not isinstance(cases, dict)
                    or len(cases) != run.get("test_count")
                ):
                    raise ValueError("timing transcript inventory differs from summary")


@dataclass(frozen=True)
class TimingCaptureDependencies:
    """Injected source and process boundaries for one serial timing capture."""

    run_command: CommandRunner
    monotonic_clock: Callable[[], float]
    candidate_worktree: Path
    state_root: Path
    environment: Mapping[str, str]
    atomic_write: Callable[[Path, bytes], None]
    read_bytes: Callable[[Path], bytes] = Path.read_bytes
    read_text: Callable[[Path], str] = Path.read_text
    write_text: Callable[[Path, str], None] = _write_text
    exists: Callable[[Path], bool] = Path.exists
    size: Callable[[Path], int] = lambda path: path.stat().st_size
    make_directory: Callable[[Path], None] = lambda path: path.mkdir(
        parents=True, exist_ok=True
    )
    make_temporary: Callable[[Path], Path] = lambda root: Path(
        tempfile.mkdtemp(prefix=".timing-", dir=root)
    )
    remove_owned_tree: Callable[[Path], None] = shutil.rmtree
    read_bounded_bytes: Callable[[Path, int], bytes] | None = None


def _run(
    dependencies: TimingCaptureDependencies,
    argv: tuple[str, ...],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: float = 120,
    failure_code: Literal[
        "baseline_hook_failed", "focused_timing_command_failed", "capture_failed"
    ] = "capture_failed",
) -> str:
    result = dependencies.run_command(
        argv,
        current_directory=cwd,
        environment=environment,
        timeout_seconds=timeout,
    )
    if result.returncode != 0:
        raise TimingCaptureError("timing command failed", code=failure_code)
    if len(result.stdout.encode("utf-8")) > _MAX_BYTES:
        raise TimingCaptureError("timing command output exceeds bound")
    return result.stdout.strip()


def _source_tree(
    dependencies: TimingCaptureDependencies, path: Path, *, clean: bool
) -> str:
    tree = _run(dependencies, ("git", "write-tree"), cwd=path)
    if _TREE.fullmatch(tree) is None:
        raise TimingCaptureError("timing source tree is invalid")
    unstaged = dependencies.run_command(
        ("git", "diff", "--quiet"), current_directory=path, timeout_seconds=30
    )
    if unstaged.returncode != 0:
        raise TimingCaptureError("timing source has unstaged tracked changes")
    untracked = _run(
        dependencies,
        ("git", "ls-files", "--others", "--exclude-standard"),
        cwd=path,
    )
    if untracked:
        raise TimingCaptureError("timing source has untracked files")
    if clean and (
        _run(dependencies, ("git", "rev-parse", "HEAD^{tree}"), cwd=path) != tree
        or _run(dependencies, ("git", "status", "--porcelain"), cwd=path)
    ):
        raise TimingCaptureError("baseline source is not clean")
    return tree


def candidate_source_matches(
    dependencies: TimingCaptureDependencies, candidate_tree: str
) -> bool:
    """Check that hook-visible tracked and importable files match the staged tree."""
    try:
        return (
            _source_tree(dependencies, dependencies.candidate_worktree, clean=False)
            == candidate_tree
        )
    except TimingCaptureError:
        return False


def _read_bounded_bytes(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit + 1)


def _artifact(
    path: Path, dependencies: TimingCaptureDependencies, *, max_bytes: int = _MAX_BYTES
) -> bytes:
    try:
        if dependencies.size(path) > max_bytes:
            raise TimingCaptureError("full JUnit artifact exceeds bound")
        if dependencies.read_bounded_bytes is not None:
            artifact = dependencies.read_bounded_bytes(path, max_bytes)
        elif dependencies.read_bytes is Path.read_bytes:
            artifact = _read_bounded_bytes(path, max_bytes)
        else:
            artifact = dependencies.read_bytes(path)
        if not isinstance(artifact, bytes) or len(artifact) > max_bytes:
            raise TimingCaptureError("full JUnit artifact exceeds bound")
        return artifact
    except OSError as error:
        raise TimingCaptureError("full JUnit artifact is unavailable") from error


def _prepared_fingerprint(
    dependencies: TimingCaptureDependencies,
    source: Path,
    environment: str,
    *,
    strict: bool = False,
) -> str:
    interpreter = source / ".tox" / environment / "bin" / "python"
    output = _run(
        dependencies,
        (str(interpreter), "-c", _DISTRIBUTIONS),
        cwd=source,
        environment=(dict(dependencies.environment) | {"PYTHONDONTWRITEBYTECODE": "1"})
        if strict
        else None,
        timeout=30,
    )
    try:
        observed = _unique_json(output) if strict else json.loads(output)
    except json.JSONDecodeError as error:
        raise TimingCaptureError("prepared environment is malformed") from error
    values = cast(dict[str, object], observed) if isinstance(observed, dict) else {}
    interpreter_identity = values.get("interpreter")
    distributions = values.get("distributions")
    if (
        not isinstance(interpreter_identity, list)
        or len(interpreter_identity) != 3
        or any(not isinstance(item, str) or not item for item in interpreter_identity)
        or not isinstance(distributions, list)
        or not distributions
        or any(
            not isinstance(item, list)
            or len(item) != 2
            or any(not isinstance(part, str) or not part for part in item)
            for item in distributions
        )
        or distributions != sorted(distributions)
    ):
        raise TimingCaptureError("prepared environment is incomplete")
    if strict and not cast(str, interpreter_identity[0]).startswith(
        {"py311": "3.11", "py314": "3.14"}[environment]
    ):
        raise TimingCaptureError("prepared interpreter is unsupported")
    return hashlib.sha256(output.encode()).hexdigest()


def _fixture_exposed_name(decorator: ast.expr, function_name: str) -> str | None:
    """Return a static fixture name and reject aliases with unknown values."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    is_fixture = (
        isinstance(target, ast.Attribute)
        and target.attr == "fixture"
        or isinstance(target, ast.Name)
        and target.id == "fixture"
    )
    if not is_fixture:
        return None
    if not isinstance(decorator, ast.Call):
        return function_name
    if decorator.args or any(keyword.arg is None for keyword in decorator.keywords):
        raise TimingCaptureError("changed helper impact map is unknown")
    alias = next(
        (keyword.value for keyword in decorator.keywords if keyword.arg == "name"),
        None,
    )
    if alias is None:
        return function_name
    if (
        not isinstance(alias, ast.Constant)
        or not isinstance(alias.value, str)
        or not alias.value
    ):
        raise TimingCaptureError("changed helper impact map is unknown")
    return alias.value


def _changed_fixture_names(
    base: Path,
    candidate: Path,
    paths: tuple[str, ...],
    dependencies: TimingCaptureDependencies,
) -> tuple[str, ...]:
    """Conservatively identify changed fixtures; reject unknown helper impact."""
    changed: set[str] = set()
    for path in paths:
        if not path.endswith("/conftest.py"):
            continue
        versions: list[dict[str, str]] = []
        module_statements: list[tuple[str, ...]] = []
        for root in (base, candidate):
            file = root / path
            if not dependencies.exists(file):
                versions.append({})
                module_statements.append(())
                continue
            try:
                source = dependencies.read_text(file)
                module = ast.parse(source)
            except (OSError, SyntaxError) as error:
                raise TimingCaptureError("fixture impact map is unavailable") from error
            definitions: dict[str, str] = {}
            module_statements.append(
                tuple(
                    ast.dump(node, include_attributes=False)
                    for node in module.body
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
            )
            for node in module.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    definitions[node.name] = ast.dump(node, include_attributes=False)
            versions.append(definitions)
        before, after = versions
        if module_statements[0] != module_statements[1]:
            raise TimingCaptureError("conftest module impact map is unknown")
        modified = {
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        }
        fixtures: dict[str, set[str]] = {}
        for root in (base, candidate):
            if not dependencies.exists(root / path):
                continue
            for node in ast.parse(dependencies.read_text(root / path)).body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.name not in modified:
                    continue
                exposed = {
                    name
                    for decorator in node.decorator_list
                    if (name := _fixture_exposed_name(decorator, node.name)) is not None
                }
                if exposed:
                    fixtures.setdefault(node.name, set()).update(exposed)
        if modified - fixtures.keys():
            raise TimingCaptureError("changed helper impact map is unknown")
        changed.update(name for function in modified for name in fixtures[function])
    return tuple(sorted(changed))


def _collect_fixture_consumers(
    dependencies: TimingCaptureDependencies,
    candidate: Path,
    private: Path,
    changed_fixtures: tuple[str, ...],
    selectors: Mapping[str, str],
    environment: Mapping[str, str],
    environment_name: str = "py311",
) -> tuple[str, ...]:
    if not changed_fixtures:
        return ()
    plugin = private / "agent_work_timing_collect.py"
    dependencies.write_text(plugin, _COLLECT_PLUGIN)
    output = private / f"collection-{environment_name}.json"
    plugin_env = dict(environment) | {
        "PYTHONPATH": str(private),
        "AGENT_TIMING_OUTPUT": str(output),
    }
    _run(
        dependencies,
        (
            str(candidate / ".tox" / environment_name / "bin" / "pytest"),
            "--collect-only",
            "-q",
            "-p",
            "agent_work_timing_collect",
        ),
        cwd=candidate,
        environment=plugin_env,
        timeout=120,
    )
    try:
        observed = json.loads(
            _artifact(output, dependencies, max_bytes=_MAX_TRANSCRIPT_BYTES)
        )
    except json.JSONDecodeError as error:
        raise TimingCaptureError("fixture collection is malformed") from error
    if not isinstance(observed, dict) or set(observed) != set(selectors.values()):
        raise TimingCaptureError("fixture collection is incomplete")
    if any(
        not isinstance(fixtures, list)
        or len(fixtures) > 128
        or any(not isinstance(item, str) for item in fixtures)
        for fixtures in observed.values()
    ):
        raise TimingCaptureError("fixture collection is malformed")
    safe_by_raw = {raw: safe for safe, raw in selectors.items()}
    consumers = [
        safe_by_raw[name]
        for name, fixtures in observed.items()
        if any(item in fixtures for item in changed_fixtures)
    ]
    return tuple(sorted(consumers))


def _focused_runner(
    dependencies: TimingCaptureDependencies,
    *,
    base: Path,
    private: Path,
    baseline_tree: str,
    candidate_tree: str,
    environment: Mapping[str, str],
    selectors: Mapping[str, Mapping[str, Mapping[str, str]]],
) -> Callable[..., dict[str, object]]:
    plugin = private / "agent_work_timing_phases.py"
    dependencies.write_text(plugin, _PLUGIN)
    deadline = dependencies.monotonic_clock() + 900
    counter = 0

    def focused(**request: object) -> dict[str, object]:
        nonlocal counter
        source_tree = request["source_tree"]
        source = (
            base if source_tree == baseline_tree else dependencies.candidate_worktree
        )
        if source_tree not in {baseline_tree, candidate_tree}:
            raise TimingCaptureError("focused source tree is unknown")
        remaining = deadline - dependencies.monotonic_clock()
        if remaining <= 0:
            raise FocusedTimingCaptureError("focused timing budget exhausted")
        case_names = request["case_names"]
        if (
            not isinstance(case_names, tuple)
            or not 1 <= len(case_names) <= 32
            or any(not isinstance(name, str) for name in case_names)
            or len(set(case_names)) != len(case_names)
        ):
            raise TimingCaptureError("focused selector is invalid")
        try:
            source_selectors = selectors[cast(str, source_tree)][
                cast(str, request["environment"])
            ]
            raw_selectors = tuple(
                source_selectors[name] for name in cast(tuple[str, ...], case_names)
            )
        except KeyError as error:
            raise TimingCaptureError("focused selector is unavailable") from error
        if len(set(raw_selectors)) != len(raw_selectors):
            raise TimingCaptureError("focused selector is ambiguous")
        counter += 1
        output = private / f"focused-{counter}.json"
        plugin_env = dict(environment) | {
            "PYTHONPATH": str(private),
            "AGENT_TIMING_OUTPUT": str(output),
        }
        try:
            _run(
                dependencies,
                (
                    str(
                        source
                        / ".tox"
                        / cast(str, request["environment"])
                        / "bin"
                        / "pytest"
                    ),
                    "-q",
                    "-p",
                    "agent_work_timing_phases",
                    *raw_selectors,
                ),
                cwd=source,
                environment=plugin_env,
                timeout=min(120, remaining),
                failure_code="focused_timing_command_failed",
            )
        except TimingCaptureError as error:
            if dependencies.monotonic_clock() >= deadline:
                raise FocusedTimingCaptureError(
                    "focused timing budget exhausted"
                ) from error
            raise
        try:
            raw_cases = json.loads(_artifact(output, dependencies))
        except json.JSONDecodeError as error:
            raise TimingCaptureError("focused phase evidence is malformed") from error
        if not isinstance(raw_cases, dict) or set(raw_cases) != set(raw_selectors):
            raise TimingCaptureError("focused phase evidence is incomplete")
        if _source_tree(dependencies, source, clean=source == base) != source_tree:
            raise TimingCaptureError("focused source changed")
        return {
            key: request[key]
            for key in (
                "source_tree",
                "environment",
                "prepared_environment",
                "argv",
                "seed",
            )
        } | {
            "cases": {
                name: raw_cases[selector]
                for name, selector in zip(case_names, raw_selectors, strict=True)
            }
        }

    return focused


def capture_serial_timing_review(
    *,
    baseline_sha: str,
    candidate_tree: str,
    candidate_artifacts: tuple[tuple[str, bytes, int], ...],
    changed_paths: tuple[str, ...],
    dependencies: TimingCaptureDependencies,
) -> dict[str, object]:
    """Run the private baseline and focused comparison for an exact serial pair."""
    if _SHA.fullmatch(baseline_sha) is None or _TREE.fullmatch(candidate_tree) is None:
        raise TimingCaptureError("serial timing source identity is invalid")
    if any(
        path.startswith("tests/")
        and path.endswith(".py")
        and not Path(path).name.startswith("test_")
        and not path.endswith("/conftest.py")
        or Path(path).name == "conftest.py"
        and not path.startswith("tests/")
        for path in changed_paths
    ):
        raise TimingCaptureError("changed test helper impact map is unknown")
    candidate = dependencies.candidate_worktree
    if _source_tree(dependencies, candidate, clean=False) != candidate_tree:
        raise TimingCaptureError("candidate source changed before timing capture")
    baseline_tree = _run(
        dependencies, ("git", "rev-parse", f"{baseline_sha}^{{tree}}"), cwd=candidate
    )
    if _TREE.fullmatch(baseline_tree) is None or baseline_tree == candidate_tree:
        raise TimingCaptureError("baseline source tree is invalid")
    actual_paths = tuple(
        sorted(
            set(
                _run(
                    dependencies,
                    (
                        "git",
                        "diff",
                        "--name-only",
                        "--no-renames",
                        baseline_tree,
                        candidate_tree,
                    ),
                    cwd=candidate,
                ).splitlines()
            )
        )
    )
    if actual_paths != changed_paths:
        raise TimingCaptureError("changed timing source paths differ")
    seed = str(int(candidate_tree[:8], 16))
    if dependencies.environment.get("PYTEST_ADDOPTS"):
        raise TimingCaptureError("ambient pytest options prevent seed binding")
    seeded = dict(dependencies.environment) | {
        "PYTEST_ADDOPTS": f"--randomly-seed={seed}"
    }
    dependencies.make_directory(dependencies.state_root)
    private = dependencies.make_temporary(dependencies.state_root)
    base = private / "baseline"
    added = False
    captured: dict[str, object] | None = None
    try:
        added = True
        _run(
            dependencies,
            ("git", "worktree", "add", "--detach", str(base), baseline_sha),
            cwd=candidate,
            timeout=120,
        )
        if (
            _run(dependencies, ("git", "rev-parse", "HEAD"), cwd=base) != baseline_sha
            or _source_tree(dependencies, base, clean=True) != baseline_tree
        ):
            raise TimingCaptureError("baseline checkout differs from exact source")
        _run(
            dependencies,
            ("git", "hook", "run", "pre-commit"),
            cwd=base,
            environment=seeded,
            timeout=1_800,
            failure_code="baseline_hook_failed",
        )
        candidate_by_env = {
            env: (contents, count) for env, contents, count in candidate_artifacts
        }
        if len(candidate_artifacts) != len(_ENVIRONMENTS) or set(
            candidate_by_env
        ) != set(_ENVIRONMENTS):
            raise TimingCaptureError("candidate full JUnit pair is incomplete")
        baseline_runs: dict[str, dict[str, object]] = {}
        candidate_runs: dict[str, dict[str, object]] = {}
        selector_map: dict[str, dict[str, dict[str, str]]] = {
            baseline_tree: {},
            candidate_tree: {},
        }
        for env in _ENVIRONMENTS:
            try:
                baseline_xml = _artifact(
                    base / f"test-results-{env}.xml",
                    dependencies,
                    max_bytes=MAX_JUNIT_BYTES,
                )
            except TimingCaptureError as error:
                if isinstance(error.__cause__, FileNotFoundError):
                    raise BaselineJUnitUnavailableError() from error
                raise
            candidate_xml, candidate_count = candidate_by_env[env]
            baseline_cases, baseline_selectors = parse_full_junit_records(baseline_xml)
            candidate_cases, candidate_selectors = parse_full_junit_records(
                candidate_xml
            )
            selector_map[baseline_tree][env] = baseline_selectors
            selector_map[candidate_tree][env] = candidate_selectors
            if candidate_count != len(candidate_cases):
                raise TimingCaptureError("candidate JUnit count differs from inventory")
            base_prepared = _prepared_fingerprint(dependencies, base, env)
            prepared = _prepared_fingerprint(dependencies, candidate, env)
            if base_prepared != prepared:
                raise TimingCaptureError("prepared environments differ")
            for target, tree, xml, cases in (
                (baseline_runs, baseline_tree, baseline_xml, baseline_cases),
                (candidate_runs, candidate_tree, candidate_xml, candidate_cases),
            ):
                target[env] = {
                    "source_tree": tree,
                    "environment": env,
                    "prepared_environment": prepared,
                    "argv": ("git", "hook", "run", "pre-commit"),
                    "seed": seed,
                    "junit_sha256": hashlib.sha256(xml).hexdigest(),
                    "test_count": len(cases),
                    "timing_complete": True,
                    "cases": cases,
                }
        changed_fixtures = _changed_fixture_names(
            base, candidate, changed_paths, dependencies
        )
        affected = tuple(
            sorted(
                {
                    case
                    for env in _ENVIRONMENTS
                    for case in _collect_fixture_consumers(
                        dependencies,
                        candidate,
                        private,
                        changed_fixtures,
                        selector_map[candidate_tree][env],
                        seeded,
                        environment_name=env,
                    )
                }
            )
        )
        try:
            review = review_test_timings(
                baseline_tree=baseline_tree,
                candidate_tree=candidate_tree,
                baseline_runs=baseline_runs,
                candidate_runs=candidate_runs,
                changed_tests=tuple(
                    path
                    for path in changed_paths
                    if path.startswith("tests/") and not path.endswith("/conftest.py")
                ),
                affected_by_helper=affected,
                run_focused=_focused_runner(
                    dependencies,
                    base=base,
                    private=private,
                    baseline_tree=baseline_tree,
                    candidate_tree=candidate_tree,
                    environment=seeded,
                    selectors=selector_map,
                ),
            )
        except FindingBudgetExhausted as error:
            raise FindingBudgetCaptureError("finding budget exhausted") from error
        except FocusedTimingBudgetExhausted as error:
            raise FocusedTimingCaptureError(
                "focused timing budget exhausted"
            ) from error
        if (
            _run(dependencies, ("git", "rev-parse", "HEAD"), cwd=base) != baseline_sha
            or _source_tree(dependencies, base, clean=True) != baseline_tree
            or _source_tree(dependencies, candidate, clean=False) != candidate_tree
        ):
            raise TimingCaptureError("source changed during timing review")
        runs = {
            env: {
                source: {
                    "sha256": cast(str, values[env]["junit_sha256"]),
                    "test_count": cast(int, values[env]["test_count"]),
                    "timing_complete": True,
                }
                for source, values in (
                    ("baseline", baseline_runs),
                    ("candidate", candidate_runs),
                )
            }
            for env in _ENVIRONMENTS
        }
        findings = [
            {
                "environment": item.environment,
                "name": item.name,
                "reason": item.reason,
                "phase_deltas": item.phase_deltas,
            }
            for item in review.findings
        ]
        captured = {
            "baseline_sha": baseline_sha,
            "baseline_tree": baseline_tree,
            "candidate_tree": candidate_tree,
            "seed": seed,
            "outcome": review.outcome,
            "runs": runs,
            "findings": findings,
            "omitted_findings": review.omitted_findings,
            "full_inventories": {
                "baseline": baseline_runs,
                "candidate": candidate_runs,
            },
        }
        try:
            _PrivateTranscript.model_validate(captured)
        except ValidationError as error:
            raise TimingCaptureError("timing transcript is incomplete") from error
        payload = json.dumps(
            captured, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(payload) > _MAX_TRANSCRIPT_BYTES:
            raise TimingCaptureError("timing transcript exceeds bound")
    finally:
        cleanup_failed = False
        if added:
            cleanup = dependencies.run_command(
                ("git", "worktree", "remove", "--force", str(base)),
                current_directory=candidate,
                timeout_seconds=120,
            )
            cleanup_failed = cleanup.returncode != 0
        if not cleanup_failed:
            dependencies.remove_owned_tree(private)
        if cleanup_failed:
            raise TimingCaptureError("private baseline checkout cleanup failed")
    if captured is None:
        raise TimingCaptureError("timing transcript was not produced")
    target = dependencies.state_root / f"{candidate_tree}.timing.json"
    if dependencies.exists(target):
        raise TimingCaptureError("timing transcript already exists")
    dependencies.atomic_write(target, payload)
    return captured


def read_serial_timing_transcript(
    *, candidate_tree: str, dependencies: TimingCaptureDependencies
) -> dict[str, object] | None:
    """Read one bounded private transcript for source-bound status replay."""
    path = dependencies.state_root / f"{candidate_tree}.timing.json"
    if not dependencies.exists(path):
        return None
    try:
        if dependencies.size(path) > _MAX_TRANSCRIPT_BYTES:
            raise TimingCaptureError("timing transcript exceeds bound")
        payload = dependencies.read_bytes(path)
        value = _PrivateTranscript.model_validate_json(payload)
    except (OSError, ValidationError) as error:
        raise TimingCaptureError("timing transcript is unavailable") from error
    return value.model_dump(mode="json", exclude_unset=True)


class BeforeTimingRequest(BaseModel):
    """Declare exact existing tests without granting permission to measure them."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    project_id: int = Field(gt=0)
    issue_iid: int = Field(gt=0)
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    invariant: str = Field(min_length=1, max_length=4096)
    test_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    repair_paths: tuple[str, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _validate_selection(self) -> BeforeTimingRequest:
        if (
            not self.invariant.strip()
            or len(set(self.test_ids)) != len(self.test_ids)
            or not all(is_safe_timing_case_id(name) for name in self.test_ids)
            or len(set(self.repair_paths)) != len(self.repair_paths)
            or any(not _before_test_path(path) for path in self.repair_paths)
        ):
            raise ValueError("before timing selection is invalid")
        return self


@dataclass(frozen=True)
class BeforeTimingScope:
    """Immutable permission supplied separately by the native controller."""

    project_id: int
    issue_iid: int
    source_sha: str
    source_tree: str
    invariant: str
    test_ids: tuple[str, ...]
    repair_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        BeforeTimingRequest.model_validate(asdict(self))


@dataclass(frozen=True)
class BeforeTimingDependencies:
    """Explicit private capture, admission, immutable publication and read ports."""

    timing: TimingCaptureDependencies
    publish_snapshot: Callable[[Path, Path], None]
    project_admission: Callable[[], AbstractContextManager[object]]
    read_regular_file: Callable[[Path, int], bytes]


class _BeforeArtifact(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _BeforeRun(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    environment: Literal["py311", "py314"]
    repeat: int = Field(ge=0, le=1)
    seed: str
    prepared_environment: str = Field(pattern=r"^[0-9a-f]{64}$")
    argv: tuple[str, ...]
    junit: _BeforeArtifact
    phases: _BeforeArtifact


class _BeforeSnapshot(BeforeTimingRequest):
    schema_version: Literal["before-timing-v1"]
    source_root: str
    capture_root: str
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: dict[str, str]
    runs: tuple[_BeforeRun, ...] = Field(min_length=4, max_length=4)


def _before_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return (
        len(path.encode()) <= 256
        and len(parts) >= 2
        and parts[0] == "tests"
        and PurePosixPath(path).as_posix() == path
        and all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts)
        and ".." not in parts
        and (parts[-1] == "conftest.py" or parts[-1].startswith("test_"))
        and path.endswith(".py")
    )


def _unique_json(payload: str | bytes) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate timing JSON key")
            result[key] = value
        return result

    def invalid_constant(_value: str) -> object:
        raise ValueError("nonfinite timing JSON value")

    return json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)


def _before_scope(
    request: BeforeTimingRequest, scope: BeforeTimingScope | None
) -> None:
    if (
        not isinstance(request, BeforeTimingRequest)
        or not isinstance(scope, BeforeTimingScope)
        or request.model_dump() != asdict(scope)
    ):
        raise TimingCaptureError("native before timing scope is unavailable or differs")


def _before_read(
    path: Path, dependencies: BeforeTimingDependencies, limit: int = _MAX_BYTES
) -> bytes:
    payload = dependencies.read_regular_file(path, limit)
    if not isinstance(payload, bytes) or len(payload) > limit:
        raise TimingCaptureError("before timing artifact is oversized or invalid")
    return payload


def _before_source(
    request: BeforeTimingRequest, dependencies: BeforeTimingDependencies
) -> dict[str, str]:
    timing = dependencies.timing
    source = timing.candidate_worktree
    environment = dict(timing.environment) | {"GIT_OPTIONAL_LOCKS": "0"}

    def git(*arguments: str) -> str:
        return _run(timing, ("git", *arguments), cwd=source, environment=environment)

    if (
        git("rev-parse", "HEAD") != request.source_sha
        or git("rev-parse", "HEAD^{tree}") != request.source_tree
        or git("status", "--porcelain")
        or git("ls-files", "--others", "--exclude-standard")
    ):
        raise TimingCaptureError("before timing source changed")
    paths = git("ls-files").splitlines()
    required = (
        set(request.repair_paths)
        | {selector.split("::", 1)[0] for selector in request.test_ids}
        | {"pyproject.toml", "uv.lock"}
    )
    if not paths or len(set(paths)) != len(paths) or not required.issubset(paths):
        raise TimingCaptureError("before timing source inventory is incomplete")
    if any(
        not path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or PurePosixPath(path).as_posix() != path
        for path in paths
    ):
        raise TimingCaptureError("before timing source path is invalid")
    return {
        path: hashlib.sha256(_before_read(source / path, dependencies)).hexdigest()
        for path in sorted(required)
    }


def _before_environment(timing: TimingCaptureDependencies) -> str:
    if timing.environment.get("PYTEST_ADDOPTS"):
        raise TimingCaptureError("ambient pytest options prevent seed binding")
    return hashlib.sha256(
        json.dumps(dict(timing.environment), sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _before_command(
    request: BeforeTimingRequest,
    source: Path,
    private: Path,
    environment: str,
    repeat: int,
) -> tuple[str, ...]:
    return (
        str(source / ".tox" / environment / "bin" / "pytest"),
        "-q",
        "-p",
        "agent_work_timing_phases",
        f"--randomly-seed={int(request.source_tree[:8], 16)}",
        f"--junitxml={private / f'focused-{environment}-{repeat}.xml'}",
        *request.test_ids,
    )


def _before_destination(request: BeforeTimingRequest, root: Path) -> Path:
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    return root / f"{request.source_tree}.before-{digest}"


def _before_validate(
    snapshot: _BeforeSnapshot,
    request: BeforeTimingRequest,
    destination: Path,
    dependencies: BeforeTimingDependencies,
) -> None:
    timing = dependencies.timing
    source = timing.candidate_worktree
    private = Path(snapshot.capture_root)
    if (
        snapshot.model_dump(include=set(BeforeTimingRequest.model_fields))
        != request.model_dump()
        or snapshot.source_root != str(source)
        or destination != _before_destination(request, timing.state_root)
        or private.parent != timing.state_root
        or not private.name.startswith(".timing-")
        or snapshot.environment_sha256 != _before_environment(timing)
        or snapshot.provenance != _before_source(request, dependencies)
        or {(run.environment, run.repeat) for run in snapshot.runs}
        != {(env, repeat) for env in _ENVIRONMENTS for repeat in (0, 1)}
    ):
        raise TimingCaptureError("before timing proof bindings differ")
    fingerprints = {
        env: _prepared_fingerprint(timing, source, env, strict=True)
        for env in _ENVIRONMENTS
    }
    for run in snapshot.runs:
        stem = f"focused-{run.environment}-{run.repeat}"
        if (
            run.seed != str(int(request.source_tree[:8], 16))
            or run.prepared_environment != fingerprints[run.environment]
            or run.argv
            != _before_command(request, source, private, run.environment, run.repeat)
            or run.junit.path != str(destination / f"{stem}.xml")
            or run.phases.path != str(destination / f"{stem}.json")
        ):
            raise TimingCaptureError("before timing run bindings differ")
        artifacts = []
        for artifact, limit in ((run.junit, MAX_JUNIT_BYTES), (run.phases, _MAX_BYTES)):
            payload = _before_read(Path(artifact.path), dependencies, limit)
            if hashlib.sha256(payload).hexdigest() != artifact.sha256:
                raise TimingCaptureError("before timing artifact changed")
            artifacts.append(payload)
        phases = _unique_json(artifacts[1])
        if not isinstance(phases, dict):
            raise TimingCaptureError("before timing phases are invalid")
        validate_before_timing_records(
            artifacts[0], cast(dict[str, object], phases), request.test_ids
        )


def reload_before_timing(
    manifest_path: Path,
    *,
    request: BeforeTimingRequest,
    approved_scope: BeforeTimingScope | None,
    dependencies: BeforeTimingDependencies,
) -> dict[str, object]:
    """Read a complete immutable snapshot without running tests or writing files."""
    # Repository imports validation, which imports this module.
    from .repository import RepositoryError

    _before_scope(request, approved_scope)
    try:
        if manifest_path.name != "manifest.json":
            raise TimingCaptureError("before timing manifest path is invalid")
        payload = _before_read(manifest_path, dependencies, _MAX_TRANSCRIPT_BYTES)
        _unique_json(payload)
        snapshot = _BeforeSnapshot.model_validate_json(payload)
        _before_validate(snapshot, request, manifest_path.parent, dependencies)
        return {"manifest_path": str(manifest_path)}
    except (OSError, ValueError, TimingCaptureError, RepositoryError):
        raise TimingCaptureError("before timing evidence is unavailable") from None


def capture_before_timing(
    request: BeforeTimingRequest,
    *,
    approved_scope: BeforeTimingScope | None,
    dependencies: BeforeTimingDependencies,
) -> dict[str, object]:
    """Measure approved existing tests, publish exclusively, then reload proof."""
    # Delay this import until the repository/validation modules have loaded.
    from .repository import RepositoryError

    _before_scope(request, approved_scope)
    timing = dependencies.timing
    private: Path | None = None
    destination = _before_destination(request, timing.state_root)
    try:
        with dependencies.project_admission():
            environment_digest = _before_environment(timing)
            provenance = _before_source(request, dependencies)
            if timing.exists(destination):
                raise TimingCaptureError("before timing snapshot already exists")
            fingerprints = {
                env: _prepared_fingerprint(
                    timing, timing.candidate_worktree, env, strict=True
                )
                for env in _ENVIRONMENTS
            }
            timing.make_directory(timing.state_root)
            private = timing.make_temporary(timing.state_root)
            # Duplicate reports/collection must remain visible, not overwritten.
            phase_plugin = _PLUGIN.replace(
                "_reports.setdefault(report.nodeid, {})[report.when] = report.duration",
                "phases = _reports.setdefault(report.nodeid, {})\n"
                "        if report.when in phases:\n"
                '            phases["duplicate_report"] = True\n'
                "        phases[report.when] = report.duration",
            )
            collect_plugin = _COLLECT_PLUGIN.replace(
                'Path(os.environ["AGENT_TIMING_OUTPUT"]).write_text(json.dumps(items))',
                "if len(items) != len(session.items):\n"
                '        items["duplicate_collection"] = []\n'
                '    Path(os.environ["AGENT_TIMING_OUTPUT"]).write_text(json.dumps(items))',
            )
            timing.write_text(private / "agent_work_timing_phases.py", phase_plugin)
            timing.write_text(private / "agent_work_timing_collect.py", collect_plugin)
            runs: list[dict[str, object]] = []
            deadline = timing.monotonic_clock() + 900
            for env in _ENVIRONMENTS:
                collection = private / f"collection-{env}.json"
                plugin_env = dict(timing.environment) | {
                    "PYTHONPATH": str(private),
                    "AGENT_TIMING_OUTPUT": str(collection),
                }
                _run(
                    timing,
                    (
                        str(
                            timing.candidate_worktree / ".tox" / env / "bin" / "pytest"
                        ),
                        "--collect-only",
                        "-q",
                        "-p",
                        "agent_work_timing_collect",
                        *request.test_ids,
                    ),
                    cwd=timing.candidate_worktree,
                    environment=plugin_env,
                )
                collected = _unique_json(_before_read(collection, dependencies))
                if not isinstance(collected, dict) or set(collected) != set(
                    request.test_ids
                ):
                    raise TimingCaptureError("before timing collection differs")
                for repeat in (0, 1):
                    remaining = deadline - timing.monotonic_clock()
                    if remaining <= 0:
                        raise TimingCaptureError("before timing budget exhausted")
                    stem = f"focused-{env}-{repeat}"
                    phases_path = private / f"{stem}.json"
                    argv = _before_command(
                        request, timing.candidate_worktree, private, env, repeat
                    )
                    _run(
                        timing,
                        argv,
                        cwd=timing.candidate_worktree,
                        environment=plugin_env
                        | {"AGENT_TIMING_OUTPUT": str(phases_path)},
                        timeout=min(120, remaining),
                    )
                    junit = _before_read(
                        private / f"{stem}.xml", dependencies, MAX_JUNIT_BYTES
                    )
                    phases_payload = _before_read(phases_path, dependencies)
                    phases = _unique_json(phases_payload)
                    if not isinstance(phases, dict):
                        raise TimingCaptureError("before timing phases are invalid")
                    validate_before_timing_records(
                        junit, cast(dict[str, object], phases), request.test_ids
                    )
                    runs.append(
                        {
                            "environment": env,
                            "repeat": repeat,
                            "seed": str(int(request.source_tree[:8], 16)),
                            "prepared_environment": fingerprints[env],
                            "argv": argv,
                            "junit": {
                                "path": str(destination / f"{stem}.xml"),
                                "sha256": hashlib.sha256(junit).hexdigest(),
                            },
                            "phases": {
                                "path": str(destination / f"{stem}.json"),
                                "sha256": hashlib.sha256(phases_payload).hexdigest(),
                            },
                        }
                    )
            if (
                provenance != _before_source(request, dependencies)
                or environment_digest != _before_environment(timing)
                or any(
                    fingerprints[env]
                    != _prepared_fingerprint(
                        timing, timing.candidate_worktree, env, strict=True
                    )
                    for env in _ENVIRONMENTS
                )
            ):
                raise TimingCaptureError("before timing provenance changed")
            snapshot = _BeforeSnapshot.model_validate(
                request.model_dump()
                | {
                    "schema_version": "before-timing-v1",
                    "source_root": str(timing.candidate_worktree),
                    "capture_root": str(private),
                    "environment_sha256": environment_digest,
                    "provenance": provenance,
                    "runs": tuple(runs),
                }
            )
            timing.atomic_write(
                private / "manifest.json", snapshot.model_dump_json().encode()
            )
            dependencies.publish_snapshot(private, destination)
            return reload_before_timing(
                destination / "manifest.json",
                request=request,
                approved_scope=approved_scope,
                dependencies=dependencies,
            )
    except (
        OSError,
        ValueError,
        TimingCaptureError,
        TimingAdmissionBusy,
        RepositoryError,
    ):
        # Publication may have happened before an error; never remove its target.
        raise TimingCaptureError("before timing capture is unavailable") from None
    finally:
        if private is not None:
            try:
                timing.remove_owned_tree(private)
            except OSError:
                raise TimingCaptureError(
                    "before timing temporary cleanup failed"
                ) from None
