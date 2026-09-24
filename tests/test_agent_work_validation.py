# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for durable Judge validation sessions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import ascii_letters, digits

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work import validation
from agent_work.ledger import state_home
from agent_work.repository import CommandResult
from agent_work.validation import (
    JudgeValidationDependencies,
    JudgeValidationError,
    observe_judge_validation,
    run_judge_validation,
)

_CANDIDATE = "a" * 40
_OTHER_CANDIDATE = "b" * 40
_PARAM_ID_FIRST = ascii_letters + digits + "_-"
_PARAM_ID_REST = _PARAM_ID_FIRST + "."


@pytest.mark.parametrize("environment", ({}, {"XDG_STATE_HOME": ""}))
def test_empty_or_unset_xdg_state_home_uses_the_safe_default(
    environment: dict[str, str],
) -> None:
    assert state_home(environment=environment) == Path.home() / ".local" / "state"


@dataclass
class ValidationStateFake:
    """Injected bounded durable-state and candidate-lock boundary."""

    records: dict[str, dict[str, object]] = field(default_factory=dict)
    writes: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)

    def read(self, candidate_tree: str) -> dict[str, object] | None:
        return self.records.get(candidate_tree)

    def atomic_write(self, candidate_tree: str, record: dict[str, object]) -> None:
        self.records[candidate_tree] = record
        self.writes.append((candidate_tree, record))

    @contextmanager
    def candidate_lock(self, candidate_tree: str) -> Iterator[None]:
        self.locks.append(candidate_tree)
        yield


def _dependencies(
    state: ValidationStateFake,
    *,
    current_tree: str = _CANDIDATE,
    returncode: int = 0,
    monotonic_clock: Callable[[], float] = lambda: 0.0,
    sleeper: Callable[[float], None] = lambda _seconds: None,
) -> tuple[JudgeValidationDependencies, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(returncode, "unbounded stdout must not persist", "stderr")

    def capture(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in artifacts
        )

    return (
        JudgeValidationDependencies(
            current_staged_tree=lambda: current_tree,
            read_record=state.read,
            atomic_write_record=state.atomic_write,
            candidate_lock=state.candidate_lock,
            run_command=run,
            monotonic_clock=monotonic_clock,
            sleeper=sleeper,
            read_artifact=lambda _path: (
                b"<testsuite tests='1' failures='0' errors='0'/>"
            ),
            capture_junit_artifacts=capture,
        ),
        calls,
    )


def _diagnostic_dependencies(
    state: ValidationStateFake,
    *,
    artifacts: Mapping[Path, bytes | OSError],
    returncode: int = 1,
    stdout: str = "sensitive hook stdout",
    stderr: str = "sensitive hook stderr",
) -> tuple[JudgeValidationDependencies, list[tuple[str, ...]], list[Path]]:
    calls: list[tuple[str, ...]] = []
    artifact_reads: list[Path] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(returncode, stdout, stderr)

    def read_artifact(path: Path) -> bytes | None:
        artifact_reads.append(path)
        value = artifacts.get(path)
        if isinstance(value, OSError):
            raise value
        return value

    def capture(
        candidate_tree: str, captured: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        return tuple(
            Path("/snapshots") / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _count in captured
        )

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        capture_junit_artifacts=capture,
    )
    return dependencies, calls, artifact_reads


def test_preview_never_runs_hook_or_mutates_durable_session() -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=dependencies
    )

    assert report.state == "preview"
    assert calls == []
    assert state.writes == []
    assert state.locks == []


def test_terminal_failure_persists_only_aggregate_supported_junit_counts() -> None:
    state = ValidationStateFake()
    dependencies, calls, artifact_reads = _diagnostic_dependencies(
        state,
        returncode=7,
        stdout="stdout API_TOKEN=hook-secret",
        stderr="stderr password=hook-secret",
        artifacts={
            Path("test-results-py311.xml"): b"""
                <testsuites>
                  <testsuite name="private-suite" failures="1" errors="1">
                    <testcase name="secret-test-name">
                      <failure message="API_TOKEN=xml-secret">xml payload secret</failure>
                    </testcase>
                  </testsuite>
                  <testsuite name="second-private-suite" failures="1" errors="0" />
                </testsuites>
            """,
            Path("test-results-py314.xml"): b"""
                <testsuite tests="9" failures="4" errors="3">
                  <testcase name="another-secret-name" />
                </testsuite>
            """,
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        "git hook run pre-commit failed with exit code 7; "
        "JUnit py311 failures=2 errors=1; py314 failures=4 errors=3"
    )
    assert calls == [("git", "hook", "run", "pre-commit")]
    assert artifact_reads == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert report.diagnostic == expected
    assert state.records[_CANDIDATE]["diagnostic"] == expected
    persisted = str(state.records[_CANDIDATE])
    assert "hook-secret" not in persisted
    assert "xml-secret" not in persisted
    assert "secret-test-name" not in persisted


def test_failed_junit_names_and_digests_survive_source_artifact_cleanup() -> None:
    """A later status read retains bounded test identity without source XML."""
    state = ValidationStateFake()
    artifact = (
        b"<testsuite failures='1' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_known_failure'>"
        b"<failure message='secret detail'/></testcase></testsuite>"
    )
    artifacts: dict[Path, bytes | OSError] = {Path("test-results-py311.xml"): artifact}
    dependencies, calls, _reads = _diagnostic_dependencies(
        state, artifacts=artifacts, returncode=7
    )

    run_report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    artifacts.clear()
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        run_report.failed_tests
        == status.failed_tests
        == ("tests.test_module::test_known_failure",)
    )
    assert status.failed_junit_digests == (hashlib.sha256(artifact).hexdigest(),)
    assert "secret detail" not in str(state.records[_CANDIDATE])


def test_success_timing_is_durable_and_status_does_not_rerun_hook() -> None:
    """A yielded Judge handle can recover the same controller timing evidence."""
    state = ValidationStateFake()
    readings = iter((10.0, 11.25))
    dependencies, calls = _dependencies(state, monotonic_clock=lambda: next(readings))

    ran = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert ran.elapsed_seconds == status.elapsed_seconds == 1.25
    assert calls == [("git", "hook", "run", "pre-commit")]


def test_success_junit_timing_summary_is_bounded_and_durable() -> None:
    """Nonfunctional review can read total and slowest tests after source cleanup."""
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    artifact = (
        b"<testsuite tests='3' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_fast' time='0.5'/>"
        b"<testcase classname='tests.test_module' name='test_slow' time='3.0'/>"
        b"<testcase name='private-secret' time='0.25'/></testsuite>"
    )
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert [item.total_seconds for item in status.junit_timings] == [3.75, 3.75]
    assert status.junit_timings[0].slowest_cases[0].name == (
        "tests.test_module::test_slow"
    )
    assert "private-secret" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    ("safe_name", "unsafe_name"),
    [
        ("test_case[linux-x86_64]", "test_case[/home/private]"),
        ("tests.test_module::test_case[py3.11]", "test_case[../private]"),
        ("tests/test_module.py::test_case[case_2]", "test_case[foo/../bar]"),
        ("test_case[42]", "test_case[.hidden]"),
        ("test_case[alpha]", "test_case[foo bar]"),
        ("test_case[alpha]", "test_case[foo\nbar]"),
        ("test_case[alpha]", "test_case[foo\\bar]"),
        ("test_case[alpha]", "test_case[foo]suffix"),
        ("test_case[alpha]", "test_case[[nested]]"),
        ("test_case[alpha]", "test_case[unclosed"),
        ("test_case[alpha]", "test_case[]"),
        ("test_case[alpha]", "test_case[" + "x" * 256 + "]"),
        ("test_case[alpha]", "tests/../private::test_case[alpha]"),
        ("test_case[alpha]", "/private::test_case[alpha]"),
    ],
)
def test_parameterized_junit_name_accepts_safe_id_and_rejects_unsafe_variant(
    safe_name: str, unsafe_name: str
) -> None:
    assert validation.is_safe_junit_test_name(safe_name)
    assert not validation.is_safe_junit_test_name(unsafe_name)


@given(
    first=st.sampled_from(tuple(_PARAM_ID_FIRST)),
    rest=st.text(alphabet=_PARAM_ID_REST, max_size=148),
    unsafe=st.sampled_from(("/", "\\", " ", "\t", "\n", "[", "]")),
)
def test_generated_parameter_id_safety_invariant(
    first: str, rest: str, unsafe: str
) -> None:
    safe_id = first + rest
    assert validation.is_safe_junit_test_name(f"test_case[{safe_id}]")
    assert not validation.is_safe_junit_test_name(f"test_case[{first}{unsafe}{rest}]")
    assert not validation.is_safe_junit_test_name(f"test_case[.{safe_id}]")


def test_parameterized_junit_failure_evidence_retains_only_safe_names() -> None:
    artifact = (
        b"<testsuite tests='4' failures='4' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_case[linux-x86_64]'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_case[../private]'>"
        b"<failure/></testcase>"
        b"<testcase classname='/private' name='test_case[alpha]'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_case[unclosed'>"
        b"<failure/></testcase></testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls, _reads = _diagnostic_dependencies(
        state, artifacts={Path("test-results-py311.xml"): artifact}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.failed_tests == ("tests.test_module::test_case[linux-x86_64]",)
    assert state.records[_CANDIDATE]["failed_tests"] == [
        "tests.test_module::test_case[linux-x86_64]"
    ]


def test_parameterized_junit_timing_evidence_retains_only_safe_names() -> None:
    artifact = (
        b"<testsuite tests='4' failures='0' errors='0'>"
        b"<testcase classname='tests.test_module' name='test_case[linux-x86_64]' time='4'/>"
        b"<testcase classname='tests.test_module' name='test_case[../private]' time='3'/>"
        b"<testcase classname='/private' name='test_case[alpha]' time='2'/>"
        b"<testcase classname='tests.test_module' name='test_case[unclosed' time='1'/>"
        b"</testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls = _dependencies(state)
    dependencies = replace(dependencies, read_artifact=lambda _path: artifact)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.junit_timings[0].total_seconds == 10.0
    assert [case.name for case in report.junit_timings[0].slowest_cases] == [
        "tests.test_module::test_case[linux-x86_64]"
    ]


def test_junit_evidence_drops_absolute_and_traversal_classnames() -> None:
    """Raw JUnit classnames cannot reveal a controller-private path."""
    artifact = (
        b"<testsuite tests='3' failures='3' errors='0'>"
        b"<testcase classname='/home/private/state' name='test_secret' time='3'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests/../private' name='test_secret' time='2'>"
        b"<failure/></testcase>"
        b"<testcase classname='tests.test_module' name='test_safe' time='1'>"
        b"<failure/></testcase></testsuite>"
    )
    state = ValidationStateFake()
    dependencies, _calls, _reads = _diagnostic_dependencies(
        state,
        artifacts={Path("test-results-py311.xml"): artifact},
        returncode=1,
    )
    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    assert report.failed_tests == ("tests.test_module::test_safe",)
    assert "/home/private" not in str(state.records[_CANDIDATE])

    success_state = ValidationStateFake()
    success_dependencies, _success_calls = _dependencies(success_state)
    success_dependencies = replace(
        success_dependencies, read_artifact=lambda _path: artifact
    )
    success = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=success_dependencies
    )
    assert success.junit_timings[0].total_seconds == 6.0
    assert [case.name for case in success.junit_timings[0].slowest_cases] == [
        "tests.test_module::test_safe"
    ]


def test_terminal_failure_without_usable_artifacts_persists_fixed_fallback() -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, artifacts={}, returncode=2
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        "git hook run pre-commit failed with exit code 2; "
        "no failing JUnit summary available"
    )
    assert report.diagnostic == expected
    assert state.records[_CANDIDATE]["diagnostic"] == expected
    assert report.hook_failures == ()


def test_terminal_failure_projects_only_allowlisted_hook_evidence_in_command_order() -> (
    None
):
    state = ValidationStateFake()
    dependencies, calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
readability-check.....................................................Failed
- hook id: readability-check
docs/guide.md: grade 9.2
ruff..................................................................Failed
- hook id: ruff
src/agent_work/example.py:7:3: F401 unused import
ty....................................................................Failed
- hook id: ty
error[invalid-assignment]: private source text
--> src/agent_work/example.py:8:2
tox...................................................................Failed
- hook id: tox
""",
        artifacts={
            Path("test-results-py311.xml"): b"<testsuite failures='2' errors='1'/>",
            Path("test-results-py314.xml"): b"<testsuite failures='4' errors='3'/>",
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    expected = (
        {
            "hook_id": "readability-check",
            "diagnostic_lines": ["docs/guide.md: grade 9.2"],
        },
        {
            "hook_id": "ruff",
            "diagnostic_lines": ["src/agent_work/example.py:7:3: F401"],
        },
        {
            "hook_id": "ty",
            "diagnostic_lines": ["src/agent_work/example.py:8:2: invalid-assignment"],
        },
        {
            "hook_id": "tox",
            "diagnostic_lines": [
                "JUnit py311 failures=2 errors=1; py314 failures=4 errors=3"
            ],
        },
    )
    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        tuple(
            {"hook_id": item.hook_id, "diagnostic_lines": list(item.diagnostic_lines)}
            for item in report.hook_failures
        )
        == expected
    )
    assert state.records[_CANDIDATE]["hook_failures"] == list(expected)


def test_readability_hook_projects_highest_safe_grades_before_lower_rows() -> None:
    """Readability evidence makes the highest safe grades visible first."""
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
readability-check.....................................................Failed
- hook id: readability-check
docs/lower.md: grade 7.8
docs/higher.md: grade 8.1
docs/tie-b.md: grade 8.0
docs/tie-a.md: grade 8.0
""",
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.hook_failures[0].diagnostic_lines == (
        "docs/higher.md: grade 8.1",
        "docs/tie-a.md: grade 8.0",
        "docs/tie-b.md: grade 8.0",
        "docs/lower.md: grade 7.8",
    )


def test_terminal_failure_drops_hostile_output_and_uses_fixed_generic_hook_detail() -> (
    None
):
    state = ValidationStateFake()
    hostile = (
        "\x1b[31mAPI_TOKEN=do-not-store\x1b[0m\n"
        "PASSWORD=do-not-store\n"
        "enter credential now> \n"
        "/absolute/private.py:1:1: F401 source text\n"
        "../escape.py:1:1: F401 source text\n"
        "ruff..................................................................Failed\n"
        "- hook id: ruff\n"
        "private_source.py:1:1: F401 arbitrary source and secret-like text\n"
        "shellcheck............................................................Failed\n"
        "- hook id: shellcheck\n"
        "free-form message with private source context and a caret ^^^\n"
        "spoof.................................................................Failed\n"
        "- hook id: ../../not-safe\n"
        + "oversized..............................................................Failed\n"
        + "- hook id: "
        + ("x" * 129)
        + "\n"
    )
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, stdout=hostile, stderr=hostile, artifacts={}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert tuple(
        {"hook_id": item.hook_id, "diagnostic_lines": list(item.diagnostic_lines)}
        for item in report.hook_failures
    ) == (
        {
            "hook_id": "ruff",
            "diagnostic_lines": ["private_source.py:1:1: F401"],
        },
        {
            "hook_id": "shellcheck",
            "diagnostic_lines": ["No safe structured diagnostic available."],
        },
    )
    persisted = str(state.records[_CANDIDATE])
    for forbidden in (
        "API_TOKEN",
        "PASSWORD",
        "credential",
        "do-not-store",
        "private source",
        "free-form message",
        "\x1b",
        "../../not-safe",
    ):
        assert forbidden not in persisted


def test_unsupported_hook_never_projects_ruff_shaped_output() -> None:
    """Unsupported hooks retain only their ID and the fixed fallback."""
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout="""
unknown-hook..........................................................Failed
- hook id: unknown-hook
safe/path.py:1:2: E101 private message
""",
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.hook_failures[0].hook_id == "unknown-hook"
    assert report.hook_failures[0].diagnostic_lines == (
        "No safe structured diagnostic available.",
    )


def test_overlong_recognized_hook_line_falls_back_without_stranding_record() -> None:
    """An overlong canonical detail cannot leave a durable session running."""
    state = ValidationStateFake()
    long_path = f"pkg/{'a' * 250}.py"
    dependencies, calls, _artifact_reads = _diagnostic_dependencies(
        state,
        stdout=(
            "ruff..................................................................Failed\n"
            "- hook id: ruff\n"
            f"{long_path}:1:2: E101 private message\n"
        ),
        artifacts={},
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert report.state == "terminal-failure"
    assert report.hook_failures[0].diagnostic_lines == (
        "No safe structured diagnostic available.",
    )
    assert state.records[_CANDIDATE]["state"] == "terminal-failure"


def test_hook_failure_evidence_keeps_only_the_earliest_bounded_safe_entries() -> None:
    state = ValidationStateFake()
    safe_path = f"pkg/{'a' * 231}.py"
    ruff_frame = "\n".join(
        (
            "ruff..................................................................Failed",
            "- hook id: ruff",
            *(
                f"{safe_path}:{line}:2: E{100 + line} ignored source text"
                for line in range(1, 7)
            ),
        )
    )
    unsupported_frames = "\n".join(
        f"hook-{index:02d}.......................................................Failed\n"
        f"- hook id: hook-{index:02d}\n"
        f"{safe_path}:1:2: E101 private source text\n"
        "free-form private message"
        for index in range(17)
    )
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state, stdout=f"{ruff_frame}\n{unsupported_frames}", artifacts={}
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert len(report.hook_failures) == 16
    assert [item.hook_id for item in report.hook_failures] == [
        "ruff",
        *(f"hook-{index:02d}" for index in range(15)),
    ]
    assert len(report.hook_failures[0].diagnostic_lines) == 4
    assert all(
        item.diagnostic_lines == ("No safe structured diagnostic available.",)
        for item in report.hook_failures[1:]
    )
    assert all(
        item.hook_id.isascii() and len(item.hook_id.encode()) <= 128
        for item in report.hook_failures
    )
    assert all(
        line.isprintable() and len(line.encode("utf-8")) <= 256
        for item in report.hook_failures
        for line in item.diagnostic_lines
    )
    serialized = json.dumps(
        state.records[_CANDIDATE]["hook_failures"], separators=(",", ":")
    ).encode("utf-8")
    assert len(serialized) <= 4096
    assert b"private source" not in serialized
    assert b"free-form" not in serialized


@pytest.mark.parametrize(
    "artifact",
    (
        b"<testsuite failures='not-a-number' errors='1'/>",
        b"<testsuite failures='-1' errors='1'/>",
        b"<testsuite failures='0' errors='0'/>",
        (b"<testsuite failures='" + (b"9" * 600) + b"' errors='1'/>"),
        b"<testsuite><broken></testsuite>",
        b"\xff\xfe\xfd",
        b"x" * 1_048_577,
        PermissionError("artifact secret path is unreadable"),
    ),
    ids=(
        "nonnumeric-count",
        "negative-count",
        "no-failures",
        "unbounded-count",
        "malformed-xml",
        "invalid-utf8",
        "oversized-xml",
        "unreadable-xml",
    ),
)
def test_unusable_junit_artifact_fails_safely_to_generic_evidence(
    artifact: bytes | OSError,
) -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): artifact,
            Path("untrusted-result.xml"): (
                b"<testsuite failures='99' errors='99' name='secret'/>"
            ),
        },
        returncode=9,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic == (
        "git hook run pre-commit failed with exit code 9; "
        "no failing JUnit summary available"
    )
    assert "99" not in report.diagnostic
    assert "secret" not in report.diagnostic
    assert Path("untrusted-result.xml") not in _artifact_reads


@pytest.mark.parametrize("returncode", (-15, 1, 255))
def test_diagnostic_is_one_safe_line_with_a_strict_utf8_byte_bound(
    returncode: int,
) -> None:
    state = ValidationStateFake()
    dependencies, _calls, _artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): (b"<testsuite failures='4' errors='1'/>")
        },
        returncode=returncode,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic is not None
    assert len(report.diagnostic.encode("utf-8")) <= 512
    assert report.diagnostic.isprintable()
    assert report.diagnostic.splitlines() == [report.diagnostic]


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    ((0, "terminal-success"), (1, "terminal-failure"), (-15, "terminal-failure")),
)
def test_apply_runs_one_fixed_hook_and_persists_its_terminal_code(
    returncode: int, expected_state: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state, returncode=returncode)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert state.locks == [_CANDIDATE]
    assert report.state == expected_state
    assert report.returncode == returncode
    expected_record: dict[str, object] = {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": expected_state,
        "returncode": returncode,
        "elapsed_seconds": 0.0,
    }
    if returncode != 0:
        expected_record["diagnostic"] = (
            f"git hook run pre-commit failed with exit code {returncode}; "
            "no failing JUnit summary available"
        )
    else:
        expected_record["junit_artifacts"] = [
            {
                "environment": environment,
                "path": f"/snapshots/{_CANDIDATE}/test-results-{environment}.xml",
                "sha256": hashlib.sha256(
                    b"<testsuite tests='1' failures='0' errors='0'/>"
                ).hexdigest(),
                "test_count": 1,
            }
            for environment in ("py311", "py314")
        ]
    assert state.records[_CANDIDATE] == expected_record


@pytest.mark.parametrize(
    ("state_name", "returncode"),
    (("terminal-success", 0), ("terminal-failure", 1)),
)
def test_repeat_apply_for_terminal_candidate_does_not_duplicate_the_hook(
    state_name: str, returncode: int
) -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": state_name,
                "returncode": returncode,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == state_name
    assert calls == []
    assert state.writes == []


def test_repeat_failure_returns_stored_diagnostic_without_hook_or_rewrite() -> None:
    diagnostic = (
        "git hook run pre-commit failed with exit code 1; "
        "JUnit py311 failures=2 errors=0"
    )
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-failure",
                "returncode": 1,
                "diagnostic": diagnostic,
            }
        }
    )
    dependencies, calls, artifact_reads = _diagnostic_dependencies(
        state,
        artifacts={
            Path("test-results-py311.xml"): (b"<testsuite failures='99' errors='99'/>")
        },
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.action == "existing"
    assert report.diagnostic == diagnostic
    assert calls == []
    assert artifact_reads == []
    assert state.writes == []


def test_legacy_terminal_failure_without_diagnostic_remains_readable() -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-failure",
                "returncode": 1,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert report.state == "terminal-failure"
    assert report.diagnostic is None
    assert report.hook_failures == ()
    assert report.junit_artifacts == ()
    assert calls == []
    assert state.writes == []


def test_successful_apply_captures_exact_supported_junit_bytes_and_status_reuses_evidence(
    tmp_path: Path,
) -> None:
    """A completed candidate retains immutable evidence without rereading checkout XML."""
    state = ValidationStateFake()
    artifact_bytes = {
        Path(
            "test-results-py311.xml"
        ): b"<testsuites><testsuite tests='3' failures='0' errors='0'/></testsuites>",
        Path(
            "test-results-py314.xml"
        ): b"<testsuite tests='5' failures='0' errors='0'/>",
    }
    artifact_reads: list[Path] = []
    captures: list[tuple[str, tuple[tuple[str, bytes, int], ...]]] = []

    def read_artifact(path: Path) -> bytes:
        artifact_reads.append(path)
        return artifact_bytes[path]

    def capture_junit_artifacts(
        candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        captures.append((candidate_tree, artifacts))
        return tuple(
            tmp_path / candidate_tree / f"test-results-{environment}.xml"
            for environment, _contents, _test_count in artifacts
        )

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: CommandResult(0, "", ""),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        capture_junit_artifacts=capture_junit_artifacts,
    )

    applied = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert artifact_reads == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert captures == [
        (
            _CANDIDATE,
            (
                ("py311", artifact_bytes[Path("test-results-py311.xml")], 3),
                ("py314", artifact_bytes[Path("test-results-py314.xml")], 5),
            ),
        )
    ]
    assert tuple(
        (item.environment, item.path, item.sha256, item.test_count)
        for item in applied.junit_artifacts
    ) == (
        (
            "py311",
            tmp_path / _CANDIDATE / "test-results-py311.xml",
            hashlib.sha256(artifact_bytes[Path("test-results-py311.xml")]).hexdigest(),
            3,
        ),
        (
            "py314",
            tmp_path / _CANDIDATE / "test-results-py314.xml",
            hashlib.sha256(artifact_bytes[Path("test-results-py314.xml")]).hexdigest(),
            5,
        ),
    )

    status_dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: pytest.fail("status must not run hook"),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: pytest.fail("status must not read mutable XML"),
        capture_junit_artifacts=lambda *_args: pytest.fail("status must not capture"),
    )
    status = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=status_dependencies
    )

    assert status.junit_artifacts == applied.junit_artifacts


@pytest.mark.parametrize(
    "junit_artifacts",
    (
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            }
        ],
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py311",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py312",
                "path": f"/snapshots/{_CANDIDATE}/py312.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py311",
                "path": "relative/py311.xml",
                "sha256": "a" * 64,
                "test_count": 1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
        [
            {
                "environment": "py311",
                "path": f"/snapshots/{_OTHER_CANDIDATE}/py311.xml",
                "sha256": "A" * 64,
                "test_count": -1,
            },
            {
                "environment": "py314",
                "path": f"/snapshots/{_CANDIDATE}/py314.xml",
                "sha256": "b" * 64,
                "test_count": 1,
            },
        ],
    ),
    ids=("partial", "duplicate", "wrong-environment", "relative-path", "unsafe"),
)
def test_observation_rejects_partial_duplicate_or_unsafe_junit_evidence(
    junit_artifacts: list[dict[str, object]],
) -> None:
    state = ValidationStateFake(
        records={
            _CANDIDATE: {
                "candidate_tree": _CANDIDATE,
                "state": "terminal-success",
                "returncode": 0,
                "junit_artifacts": junit_artifacts,
            }
        }
    )
    dependencies, calls = _dependencies(state)

    with pytest.raises(JudgeValidationError, match="validation record is malformed"):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)

    assert calls == []
    assert state.writes == []


@pytest.mark.parametrize(
    "record",
    (
        {"candidate_tree": _CANDIDATE, "state": "terminal-success", "returncode": 0},
        {
            "candidate_tree": _CANDIDATE,
            "schema_version": 2,
            "state": "terminal-success",
            "returncode": 0,
        },
        {
            "candidate_tree": _CANDIDATE,
            "schema_version": 2,
            "state": "terminal-success",
            "returncode": 0,
            "junit_artifacts": [],
        },
    ),
    ids=("legacy-omitted", "current-missing", "current-empty"),
)
def test_success_evidence_version_discriminates_legacy_from_current(
    record: dict[str, object],
) -> None:
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, _calls = _dependencies(state)

    if "schema_version" not in record:
        report = observe_judge_validation(
            candidate_tree=_CANDIDATE, dependencies=dependencies
        )
        assert report.junit_artifacts == ()
    else:
        with pytest.raises(
            JudgeValidationError, match="validation record is malformed"
        ):
            observe_judge_validation(
                candidate_tree=_CANDIDATE, dependencies=dependencies
            )


def test_hook_failure_keeps_existing_diagnostics_without_junit_capture() -> None:
    state = ValidationStateFake()
    captures: list[object] = []
    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=lambda command, **_kwargs: CommandResult(7, "", ""),
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='1' errors='0'/>",
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.diagnostic == (
        "git hook run pre-commit failed with exit code 7; "
        "JUnit py311 failures=1 errors=0; py314 failures=1 errors=0"
    )
    assert report.junit_artifacts == ()
    assert "junit_artifacts" not in state.records[_CANDIDATE]
    assert captures == []


def test_artifact_capture_failure_is_terminal_without_rerunning_the_hook() -> None:
    """A successful hook cannot be reported as a usable validation without snapshots."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='0' errors='0'/>",
        capture_junit_artifacts=lambda *_args: (_ for _ in ()).throw(OSError("secret")),
    )

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    observed = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert (
        first.state == repeated.state == observed.state == "terminal-artifact-failure"
    )
    assert first.returncode == repeated.returncode == observed.returncode == 0
    assert (
        first.junit_artifacts
        == repeated.junit_artifacts
        == observed.junit_artifacts
        == ()
    )
    assert "secret" not in str(state.records[_CANDIDATE])


def test_apply_removes_stale_junit_sources_before_hook_and_in_finally_cleanup(
    tmp_path: Path,
) -> None:
    """Only XML produced by this one hook invocation can become durable evidence."""
    state = ValidationStateFake()
    events: list[str] = []

    def remove_junit_artifact(path: Path) -> None:
        events.append(f"remove:{path}")

    def read_artifact(path: Path) -> bytes:
        events.append(f"read:{path}")
        return b"<testsuite tests='1' failures='0' errors='0'/>"

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        events.append(f"hook:{' '.join(command)}")
        return CommandResult(0, "", "")

    def capture_junit_artifacts(
        _candidate_tree: str, artifacts: tuple[tuple[str, bytes, int], ...]
    ) -> tuple[Path, ...]:
        events.append("capture")
        return tuple(
            tmp_path / _CANDIDATE / f"test-results-{environment}.xml"
            for environment, *_rest in artifacts
        )

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=read_artifact,
        remove_junit_artifact=remove_junit_artifact,
        capture_junit_artifacts=capture_junit_artifacts,
    )

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    fixed_paths = (Path("test-results-py311.xml"), Path("test-results-py314.xml"))
    assert report.state == "terminal-success"
    assert events == [
        *(f"remove:{path}" for path in fixed_paths),
        "hook:git hook run pre-commit",
        *(f"read:{path}" for path in fixed_paths),
        *(f"remove:{path}" for path in fixed_paths),
        "capture",
    ]


def test_post_hook_junit_cleanup_failure_is_terminal_and_never_retries_hook(
    tmp_path: Path,
) -> None:
    """Cleanup errors fail closed after the one completed hook rather than rerunning it."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    removals = 0
    captures: list[object] = []

    def remove_junit_artifact(_path: Path) -> None:
        nonlocal removals
        removals += 1
        if removals == 4:
            raise OSError("private cleanup path")

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: b"<testsuite tests='1' failures='0' errors='0'/>",
        remove_junit_artifact=remove_junit_artifact,
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert first.state == repeated.state == "terminal-artifact-failure"
    assert first.junit_artifacts == repeated.junit_artifacts == ()
    assert captures == []
    assert "private cleanup path" not in str(state.records[_CANDIDATE])


def test_pre_hook_junit_cleanup_failure_is_terminal_without_a_hook_return_code() -> (
    None
):
    """A stale source that cannot be removed cannot authenticate a new candidate."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    captures: list[object] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: pytest.fail("pre-hook failure must not read XML"),
        remove_junit_artifact=lambda _path: (_ for _ in ()).throw(
            OSError("private stale source")
        ),
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    observed = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert calls == []
    assert captures == []
    assert (
        first.state == repeated.state == observed.state == "terminal-artifact-failure"
    )
    assert first.returncode == repeated.returncode == observed.returncode is None
    assert (
        first.junit_artifacts
        == repeated.junit_artifacts
        == observed.junit_artifacts
        == ()
    )
    assert "private stale source" not in str(state.records[_CANDIDATE])


@pytest.mark.parametrize(
    "artifact",
    (
        None,
        b"<testsuite tests='1' failures='0' errors='0'>",
        b"x" * 1_048_577,
    ),
    ids=("missing", "malformed", "oversized"),
)
def test_unusable_success_junit_xml_is_terminal_without_capture_or_hook_retry(
    artifact: bytes | None,
) -> None:
    """A zero hook exit is not success when its required produced XML is unusable."""
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []
    removals: list[Path] = []
    captures: list[object] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "", "")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
        read_artifact=lambda _path: artifact,
        remove_junit_artifact=removals.append,
        capture_junit_artifacts=lambda *_args: captures.append(_args),
    )

    first = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )
    repeated = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert captures == []
    assert removals == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert first.state == repeated.state == "terminal-artifact-failure"
    assert first.returncode == repeated.returncode == 0
    assert first.junit_artifacts == repeated.junit_artifacts == ()


def test_filesystem_junit_snapshot_store_publishes_readonly_candidate_evidence(
    tmp_path: Path,
) -> None:
    """The public store atomically retains the exact payloads under their candidate."""
    state_root = tmp_path / "agent-work-validation" / "repository"
    store = validation.FilesystemJUnitSnapshotStore(root=state_root)
    artifacts = (
        ("py311", b"<testsuite tests='3' failures='0' errors='0'/>", 3),
        ("py314", b"<testsuite tests='5' failures='0' errors='0'/>", 5),
    )

    paths = store.capture(candidate_tree=_CANDIDATE, artifacts=artifacts)

    assert paths == (
        state_root / _CANDIDATE / "test-results-py311.xml",
        state_root / _CANDIDATE / "test-results-py314.xml",
    )
    assert tuple(path.read_bytes() for path in paths) == tuple(
        contents for _environment, contents, _count in artifacts
    )
    assert all(path.is_absolute() for path in paths)
    assert all(path.stat().st_mode & 0o777 == 0o444 for path in paths)
    assert (state_root / _CANDIDATE).stat().st_mode & 0o777 == 0o555


def test_filesystem_junit_snapshot_store_removes_partial_temporary_state(
    tmp_path: Path,
) -> None:
    """A failed atomic publication leaves no candidate or temporary evidence behind."""
    state_root = tmp_path / "agent-work-validation" / "repository"
    writes = 0

    def write_bytes(path: Path, contents: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected write failure")
        path.write_bytes(contents)

    store = validation.FilesystemJUnitSnapshotStore(
        root=state_root, write_bytes=write_bytes
    )

    with pytest.raises((JudgeValidationError, OSError)):
        store.capture(
            candidate_tree=_CANDIDATE,
            artifacts=(
                ("py311", b"first", 1),
                ("py314", b"second", 1),
            ),
        )

    assert not (state_root / _CANDIDATE).exists()
    assert not state_root.exists() or not tuple(state_root.iterdir())


def test_preview_success_and_running_reports_never_expose_diagnostics() -> None:
    preview_state = ValidationStateFake()
    preview_dependencies, _preview_calls = _dependencies(preview_state)
    preview = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=False, dependencies=preview_dependencies
    )

    success_state = ValidationStateFake()
    success_dependencies, _success_calls, success_artifact_reads = (
        _diagnostic_dependencies(
            success_state,
            artifacts={
                Path("test-results-py311.xml"): (
                    b"<testsuite failures='4' errors='3'/>"
                )
            },
            returncode=0,
        )
    )
    success = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=success_dependencies
    )

    running_state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    running_dependencies, _running_calls = _dependencies(running_state)
    running = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=running_dependencies
    )

    assert preview.diagnostic is None
    assert success.diagnostic is None
    assert running.diagnostic is None
    assert preview.hook_failures == success.hook_failures == running.hook_failures == ()
    assert success_artifact_reads == [Path("test-results-py311.xml")]


def test_recovery_never_starts_a_second_hook_for_existing_running_candidate() -> None:
    record: dict[str, object] = {"candidate_tree": _CANDIDATE, "state": "running"}
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, calls = _dependencies(state)

    report = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert report.state == "running"
    assert calls == []
    assert state.writes == []


def test_interrupted_hook_preserves_running_record_for_nonduplicating_recovery() -> (
    None
):
    state = ValidationStateFake()
    calls: list[tuple[str, ...]] = []

    def interrupted_run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise RuntimeError("frontend handle lost")

    dependencies = JudgeValidationDependencies(
        current_staged_tree=lambda: _CANDIDATE,
        read_record=state.read,
        atomic_write_record=state.atomic_write,
        candidate_lock=state.candidate_lock,
        run_command=interrupted_run,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="frontend handle lost"):
        run_judge_validation(
            candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
        )
    recovered = run_judge_validation(
        candidate_tree=_CANDIDATE, apply=True, dependencies=dependencies
    )

    assert calls == [("git", "hook", "run", "pre-commit")]
    assert state.records[_CANDIDATE] == {
        "candidate_tree": _CANDIDATE,
        "schema_version": 2,
        "state": "running",
    }
    assert recovered.state == "running"


def test_observation_reports_running_then_terminal_without_hook_or_state_mutation() -> (
    None
):
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    dependencies, calls = _dependencies(state)

    running = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )
    state.records[_CANDIDATE] = {
        "candidate_tree": _CANDIDATE,
        "state": "terminal-failure",
        "returncode": 1,
    }
    terminal = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies
    )

    assert running.state == "running" and running.returncode is None
    assert terminal.state == "terminal-failure" and terminal.returncode == 1
    assert calls == []
    assert state.writes == []


@pytest.mark.parametrize(
    ("returncode", "expected_state"),
    ((0, "terminal-success"), (1, "terminal-failure")),
)
def test_observation_waits_for_terminal_record_without_running_a_hook(
    returncode: int, expected_state: str
) -> None:
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        state.records[_CANDIDATE] = {
            "candidate_tree": _CANDIDATE,
            "state": expected_state,
            "returncode": returncode,
        }

    dependencies, calls = _dependencies(
        state, monotonic_clock=lambda: now[0], sleeper=sleep
    )

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies, wait_seconds=3
    )

    assert report.state == expected_state
    assert report.returncode == returncode
    assert sleeps == [1.0]
    assert calls == []
    assert state.writes == []


def test_observation_wait_stops_at_its_injected_monotonic_deadline() -> None:
    state = ValidationStateFake(
        records={_CANDIDATE: {"candidate_tree": _CANDIDATE, "state": "running"}}
    )
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    dependencies, calls = _dependencies(
        state, monotonic_clock=lambda: now[0], sleeper=sleep
    )

    report = observe_judge_validation(
        candidate_tree=_CANDIDATE, dependencies=dependencies, wait_seconds=3
    )

    assert report.state == "running"
    assert sleeps == [1.0, 1.0, 1.0]
    assert calls == []


@pytest.mark.parametrize(
    ("candidate_tree", "current_tree"),
    (("not-a-tree", _CANDIDATE), (_CANDIDATE, _OTHER_CANDIDATE)),
    ids=("malformed", "stale"),
)
def test_apply_rejects_malformed_or_noncurrent_candidate_before_state_or_hook(
    candidate_tree: str, current_tree: str
) -> None:
    state = ValidationStateFake()
    dependencies, calls = _dependencies(state, current_tree=current_tree)

    with pytest.raises(JudgeValidationError):
        run_judge_validation(
            candidate_tree=candidate_tree, apply=True, dependencies=dependencies
        )

    assert calls == []
    assert state.writes == []
    assert state.locks == []


@pytest.mark.parametrize(
    "record",
    (
        {},
        {"candidate_tree": _OTHER_CANDIDATE, "state": "running"},
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": "1",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 1,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "stdout": "x" * 2_001,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "arbitrary persisted failure detail",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 1; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": -15,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 15; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code -5; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 5,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 0; "
                "no failing JUnit summary available"
            ),
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "git hook run pre-commit failed\nsecret",
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "diagnostic": "é" * 257,
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "diagnostic": (
                "git hook run pre-commit failed with exit code 1; "
                "no failing JUnit summary available"
            ),
        },
        [],
    ),
    ids=(
        "missing",
        "mismatched-tree",
        "noninteger-code",
        "inconsistent-terminal-code",
        "unbounded-output",
        "noncanonical-diagnostic",
        "mismatched-positive-code",
        "mismatched-negative-record-code",
        "mismatched-negative-diagnostic-code",
        "zero-diagnostic-code",
        "multiline-diagnostic",
        "utf8-oversized-diagnostic",
        "success-with-diagnostic",
        "non-object-record",
    ),
)
def test_observation_fails_closed_for_malformed_or_unbounded_record(
    record: dict[str, object],
) -> None:
    state = ValidationStateFake(records={_CANDIDATE: record})
    dependencies, calls = _dependencies(state)

    with pytest.raises(JudgeValidationError):
        observe_judge_validation(candidate_tree=_CANDIDATE, dependencies=dependencies)

    assert calls == []
    assert state.writes == []


def test_observation_fails_closed_for_malformed_hook_failure_evidence() -> None:
    valid_line = "pkg/example.py:1:2: E101"
    valid_entry = {"hook_id": "ruff", "diagnostic_lines": [valid_line]}
    records: tuple[dict[str, object], ...] = (
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [{"hook_id": "x" * 129, "diagnostic_lines": [valid_line]}],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "../../unsafe", "diagnostic_lines": [valid_line]}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "ruff", "diagnostic_lines": ["arbitrary secret text"]}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [{"hook_id": "ruff", "diagnostic_lines": ["x" * 257]}],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [valid_entry for _ in range(17)],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {"hook_id": "ruff", "diagnostic_lines": [valid_line] * 5}
            ],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-success",
            "returncode": 0,
            "hook_failures": [valid_entry],
        },
        {
            "candidate_tree": _CANDIDATE,
            "state": "terminal-failure",
            "returncode": 1,
            "hook_failures": [
                {
                    "hook_id": f"hook-{index}",
                    "diagnostic_lines": [
                        f"pkg/{'a' * 231}.py:1:2: E101" for _ in range(4)
                    ],
                }
                for index in range(16)
            ],
        },
    )

    for record in records:
        state = ValidationStateFake(records={_CANDIDATE: record})
        dependencies, calls = _dependencies(state)

        with pytest.raises(JudgeValidationError):
            observe_judge_validation(
                candidate_tree=_CANDIDATE, dependencies=dependencies
            )

        assert calls == []
        assert state.writes == []
