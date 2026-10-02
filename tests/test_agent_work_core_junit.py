# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Public shared-core JUnit interpretation and dependency contracts."""

import ast
import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from xml.etree import ElementTree

import pytest
from hypothesis import given
from hypothesis import strategies as st


@given(
    counts=st.lists(st.integers(min_value=0, max_value=10000), min_size=1, max_size=8),
    namespace=st.sampled_from(("", "urn:junit")),
)
def test_core_junit_generated_aggregate_counts_and_namespace(
    counts: list[int], namespace: str
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    suites = "".join(
        f'<testsuite tests="{count}" failures="1" errors="0"/>' for count in counts
    )
    artifact = f'<testsuites xmlns="{namespace}">{suites}</testsuites>'.encode()
    parses: list[bytes] = []

    def parse_xml(raw: bytes) -> ElementTree.Element:
        parses.append(raw)
        return ElementTree.fromstring(raw)

    evidence = parse_junit_evidence(artifact, serial=False, parse_xml=parse_xml)
    assert evidence.test_count == sum(counts)
    assert evidence.failure_counts == (len(counts), 0)
    assert evidence.sha256 == hashlib.sha256(artifact).hexdigest()
    assert evidence.unusable_reason is None
    assert parses == [artifact]
    assert evidence.timing is None


@pytest.mark.parametrize(
    ("artifact", "count", "failures", "reason"),
    (
        (b'<testsuite tests="0" failures="0" errors="0"/>', 0, None, None),
        (
            b'<testsuites tests="4" failures="2" errors="1"><testsuite tests="9" failures="7" errors="8"/></testsuites>',
            4,
            (2, 1),
            None,
        ),
        (b'<testsuite failures="1" errors="0"/>', None, (1, 0), "invalid_count"),
        (b'<testsuite tests="1" failures="01" errors="0"/>', 1, None, None),
        (
            b'<testsuite tests="999999999" failures="999999999" errors="0"/>',
            999999999,
            (999999999, 0),
            None,
        ),
        (
            b'<testsuites><testsuite tests="999999999" failures="999999999" errors="0"/><testsuite tests="1" failures="1" errors="0"/></testsuites>',
            None,
            None,
            "invalid_count",
        ),
        (b'<other tests="1" failures="1" errors="0"/>', None, None, "invalid_xml"),
        (b"<testsuite>", None, None, "invalid_xml"),
        (None, None, None, "missing"),
        (b"x" * 33554433, None, None, "oversized"),
    ),
    ids=(
        "zero",
        "direct-precedence",
        "failure-without-count",
        "invalid-failure-count",
        "counter-boundary",
        "aggregate-overflow",
        "wrong-root",
        "malformed",
        "missing",
        "oversized",
    ),
)
def test_core_junit_bounded_count_failure_and_unusable_rules(
    artifact: bytes | None,
    count: int | None,
    failures: tuple[int, int] | None,
    reason: str | None,
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    calls: list[bytes] = []

    def parse_xml(raw: bytes) -> ElementTree.Element:
        calls.append(raw)
        return ElementTree.fromstring(raw)

    evidence = parse_junit_evidence(artifact, serial=False, parse_xml=parse_xml)
    assert (evidence.test_count, evidence.failure_counts, evidence.unusable_reason) == (
        count,
        failures,
        reason,
    )
    assert len(calls) == (0 if reason in {"missing", "oversized"} else 1)
    assert evidence.failed_tests == ()


@given(
    value=st.sampled_from(
        ("-1", "+1", "01", "1.0", " 1", "1000000000", "NaN", "9" * 600)
    )
)
def test_core_junit_generated_invalid_numeric_counts(value: str) -> None:
    from agent_work.core.junit import parse_junit_evidence

    artifact = f'<testsuite tests="{value}" failures="{value}" errors="1"/>'.encode()
    evidence = parse_junit_evidence(
        artifact, serial=False, parse_xml=ElementTree.fromstring
    )
    assert evidence.test_count is None
    assert evidence.failure_counts is None
    assert evidence.unusable_reason == "invalid_count"


@given(count=st.integers(min_value=1, max_value=40))
def test_core_junit_generated_failed_names_are_unique_safe_and_bounded(
    count: int,
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    cases = "".join(
        f'<testcase classname="tests.test_speed" name="test_case{index}"><failure>PRIVATE_TOKEN</failure></testcase>'
        for index in range(count)
    )
    cases += (
        '<testcase classname="/private/path" name="test_secret"><error/></testcase>'
    )
    cases += (
        '<testcase classname="tests.test_speed" name="test_case0"><error/></testcase>'
    )
    artifact = f'<testsuite failures="{count}" errors="2">{cases}</testsuite>'.encode()
    evidence = parse_junit_evidence(
        artifact, serial=False, parse_xml=ElementTree.fromstring
    )
    assert evidence.failed_tests == tuple(
        f"tests.test_speed::test_case{index}" for index in range(min(count, 16))
    )
    assert evidence.failure_counts == (count, 2)
    assert "/private" not in str(evidence)
    assert "PRIVATE_TOKEN" not in str(evidence)


@given(
    milliseconds=st.lists(
        st.integers(min_value=0, max_value=1000), min_size=1, max_size=30
    ),
    serial=st.booleans(),
)
def test_core_junit_generated_timing_threshold_totals_sorting_and_identity(
    milliseconds: list[int], *, serial: bool
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    cases = "".join(
        f'<testcase classname="tests.test_speed" name="test_case{index}" time="{duration / 1000:.3f}"/>'
        for index, duration in enumerate(milliseconds)
    )
    artifact = f'<testsuite tests="{len(milliseconds)}" failures="0" errors="0">{cases}</testsuite>'.encode()
    evidence = parse_junit_evidence(
        artifact, serial=serial, parse_xml=ElementTree.fromstring
    )
    timing = evidence.timing
    assert timing is not None and timing.timing_complete
    prefix = "tests/test_speed.py::" if serial else "tests.test_speed::"
    expected = [
        (f"{prefix}test_case{index}", duration / 1000)
        for index, duration in enumerate(milliseconds)
    ]
    assert timing.total_seconds == pytest.approx(round(sum(milliseconds) / 1000, 3))
    assert [(case.name, case.seconds) for case in timing.over_100ms_cases] == [
        case for case in expected if case[1] > 0.1
    ]
    assert [(case.name, case.seconds) for case in timing.slowest_cases] == sorted(
        expected, key=lambda case: (-case[1], case[0])
    )[:3]
    for instance, field, value in (
        (evidence, "test_count", 99),
        (timing, "total_seconds", 99.0),
        (timing.slowest_cases[0], "seconds", 99.0),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, field, value)


@pytest.mark.parametrize(
    "seconds", ("", "-1", "NaN", "inf", "1e3", "0.1234567", "1000001")
)
def test_core_junit_invalid_time_is_incomplete_without_private_names(
    seconds: str,
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    artifact = f'<testsuite tests="1"><testcase classname="tests.test_speed" name="test_case" time="{seconds}"/></testsuite>'.encode()
    evidence = parse_junit_evidence(
        artifact, serial=False, parse_xml=ElementTree.fromstring
    )
    assert evidence.timing is not None
    assert not evidence.timing.timing_complete
    assert evidence.timing.total_seconds == 0
    assert evidence.timing.over_100ms_cases == ()


def test_core_junit_total_time_overflow_has_no_timing_summary() -> None:
    from agent_work.core.junit import parse_junit_evidence

    raw = b'<testsuite tests="2"><testcase name="test_one" time="1000000"/><testcase name="test_two" time="1"/></testsuite>'
    evidence = parse_junit_evidence(raw, serial=False, parse_xml=ElementTree.fromstring)
    assert evidence.test_count == 2
    assert evidence.timing is None


def _core_dependency_violations(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    pure_modules = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "hashlib",
        "json",
        "math",
        "re",
        "typing",
    }
    pure_helpers = {
        "test_timing": {
            "MAX_JUNIT_BYTES",
            "is_safe_timing_case_id",
            "safe_timing_case_id",
        },
        "agent_work.test_timing": {
            "MAX_JUNIT_BYTES",
            "is_safe_timing_case_id",
            "safe_timing_case_id",
        },
        "junit": {
            "JUnitEvidence",
            "JUnitTimingFacts",
            "JUnitCaseTiming",
            "parse_junit_evidence",
        },
        "agent_work.core.junit": {
            "JUnitEvidence",
            "JUnitTimingFacts",
            "JUnitCaseTiming",
            "parse_junit_evidence",
        },
        "xml.etree.ElementTree": {"Element", "ParseError"},
    }
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name for alias in node.names if alias.name not in pure_modules
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module not in pure_modules:
                allowed = pure_helpers.get(module, set())
                violations.extend(
                    f"{module}.{alias.name}"
                    for alias in node.names
                    if alias.name not in allowed
                )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"open", "__import__", "eval", "exec"}
        ):
            violations.append(node.func.id)
    return tuple(violations)


@pytest.mark.parametrize(
    "relative_path", ("src/agent_work/core/__init__.py", "src/agent_work/core/junit.py")
)
def test_core_junit_architecture_has_only_pure_dependencies(
    project_root: Path, relative_path: str
) -> None:
    assert _core_dependency_violations((project_root / relative_path).read_text()) == ()


@pytest.mark.parametrize(
    "source",
    (
        'from xml.etree import ElementTree as ET\nET.parse("/private/path")',
        'from xml.etree import ElementTree as ET\nET.fromstring(b"<testsuite/>")',
        'from xml.etree.ElementTree import fromstring as decode\ndecode(b"<testsuite/>")',
        'import xml.etree.ElementTree as ET\ndecode = ET.fromstring\ndecode(b"<testsuite/>")',
        "from xml.etree import ElementTree as ET\ndef parse(raw, parse_xml=ET.fromstring): pass",
        'from pathlib import Path\nPath("/private/path").read_bytes()',
        "from agent_work import validation",
        "import agent_work.cli as cli",
        "from xml.etree.ElementTree import fromstring as decode\ndef concrete(raw): return decode(raw)\ndef injected(raw, parse_xml):\n    decode = parse_xml\n    return decode(raw)",
    ),
)
def test_core_junit_dependency_guard_rejects_concrete_parser_and_io(
    source: str,
) -> None:
    assert _core_dependency_violations(source)


@pytest.mark.parametrize(
    "source",
    (
        "from xml.etree.ElementTree import Element, ParseError\ndef parse(raw, parse_xml): return parse_xml(raw)",
    ),
)
def test_core_junit_dependency_guard_allows_types_and_injected_parser(
    source: str,
) -> None:
    assert _core_dependency_violations(source) == ()


@pytest.mark.parametrize("serial", (False, True))
def test_core_junit_interpretation_uses_injected_tree_for_all_facts(
    *, serial: bool
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    raw = b'<testsuite tests="99" failures="0" errors="0"/>'
    returned = ElementTree.fromstring(
        b'<testsuite tests="1" failures="2" errors="3"><testcase classname="tests.test_speed" name="test_injected" time="0.2"><failure/></testcase></testsuite>'
    )
    parses: list[bytes] = []

    def parse_xml(contents: bytes) -> ElementTree.Element:
        parses.append(contents)
        return returned

    evidence = parse_junit_evidence(raw, serial=serial, parse_xml=parse_xml)
    assert parses == [raw]
    assert evidence.test_count == 1
    assert evidence.failure_counts == (2, 3)
    assert evidence.failed_tests == ("tests.test_speed::test_injected",)
    assert evidence.sha256 == hashlib.sha256(raw).hexdigest()
    assert evidence.unusable_reason is None
    assert evidence.timing is not None and evidence.timing.timing_complete
    assert evidence.timing.total_seconds == 0.2
    expected_name = (
        "tests/test_speed.py::test_injected"
        if serial
        else "tests.test_speed::test_injected"
    )
    assert [(case.name, case.seconds) for case in evidence.timing.slowest_cases] == [
        (expected_name, 0.2)
    ]
    assert evidence.timing.over_100ms_cases == evidence.timing.slowest_cases


@given(
    serial=st.booleans(),
    parameter=st.text(alphabet=" abcXYZ/{}", min_size=1, max_size=30),
)
def test_core_junit_generated_private_parameters_are_opaque_and_stable(
    *, serial: bool, parameter: str
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    name = f"test_case[private {parameter}]"
    root = ElementTree.Element("testsuite", tests="1", failures="1", errors="0")
    case = ElementTree.SubElement(
        root, "testcase", classname="tests.test_speed", name=name, time="0.2"
    )
    ElementTree.SubElement(case, "failure").text = "/private/path PRIVATE_TOKEN"
    raw = ElementTree.tostring(root)
    first = parse_junit_evidence(raw, serial=serial, parse_xml=ElementTree.fromstring)
    second = parse_junit_evidence(raw, serial=serial, parse_xml=ElementTree.fromstring)
    assert first == second
    assert first.failed_tests == ()
    assert first.timing is not None and first.timing.timing_complete
    prefix = "tests/test_speed.py::" if serial else "tests.test_speed::"
    timing_name = first.timing.over_100ms_cases[0].name
    assert timing_name.startswith(f"{prefix}test_case[param_")
    assert "private " not in str(first)
    assert "/private/path" not in str(first)
    assert "PRIVATE_TOKEN" not in str(first)


@given(declared=st.integers(min_value=0, max_value=8))
def test_core_junit_generated_completeness_tracks_declared_and_duplicate_cases(
    declared: int,
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    raw = f'<testsuite tests="{declared}"><testcase name="test_case" time="0.2"/></testsuite>'.encode()
    evidence = parse_junit_evidence(raw, serial=False, parse_xml=ElementTree.fromstring)
    assert evidence.timing is not None
    assert evidence.timing.timing_complete is (declared == 1)
    duplicate = raw.replace(
        b"</testsuite>", b'<testcase name="test_case" time="0.3"/></testsuite>'
    )
    evidence = parse_junit_evidence(
        duplicate, serial=False, parse_xml=ElementTree.fromstring
    )
    assert evidence.timing is not None and not evidence.timing.timing_complete


@given(count=st.integers(min_value=0, max_value=20))
def test_core_junit_generated_artifact_changes_are_interpreted_without_cache(
    count: int,
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    artifacts = [
        f'<testsuite tests="{value}"/>'.encode() for value in (count, count + 1, count)
    ]
    parses: list[bytes] = []

    def parse_xml(raw: bytes) -> ElementTree.Element:
        parses.append(raw)
        return ElementTree.fromstring(raw)

    evidence = [
        parse_junit_evidence(raw, serial=False, parse_xml=parse_xml)
        for raw in artifacts
    ]
    assert [item.test_count for item in evidence] == [count, count + 1, count]
    assert parses == artifacts
    assert evidence[0] == evidence[2]


@pytest.mark.parametrize("field", ("tests", "failures", "errors"))
@pytest.mark.parametrize(
    ("value", "accepted"),
    (
        ("0", 0),
        ("999999998", 999999998),
        ("999999999", 999999999),
        ("1000000000", None),
        ("9" * 4301, None),
        ("9" * 5000, None),
        ("0" * 4301, None),
    ),
    ids=(
        "zero",
        "below-cap",
        "cap",
        "over-cap",
        "4301-digits",
        "5000-digits",
        "noncanonical-long-zero",
    ),
)
@pytest.mark.parametrize("nested", (False, True))
def test_core_junit_counter_domain_preserves_independent_fields(
    field: str, value: str, accepted: int | None, *, nested: bool
) -> None:
    from agent_work.core.junit import parse_junit_evidence

    counters = {"tests": "1", "failures": "1", "errors": "1"} | {field: value}
    attributes = " ".join(f'{name}="{counter}"' for name, counter in counters.items())
    suite = f'<testsuite {attributes}><testcase classname="tests.test_speed" name="test_failed" time="0.2"><failure>PRIVATE_TOKEN</failure></testcase></testsuite>'
    raw = (f"<testsuites>{suite}</testsuites>" if nested else suite).encode()
    parses: list[bytes] = []

    def parse_xml(contents: bytes) -> ElementTree.Element:
        parses.append(contents)
        return ElementTree.fromstring(contents)

    evidence = parse_junit_evidence(raw, serial=False, parse_xml=parse_xml)
    assert evidence.test_count == (accepted if field == "tests" else 1)
    expected_failures = (
        None
        if accepted is None and field != "tests"
        else (
            accepted if field == "failures" else 1,
            accepted if field == "errors" else 1,
        )
    )
    assert evidence.failure_counts == expected_failures
    assert evidence.unusable_reason == (
        "invalid_count" if field == "tests" and accepted is None else None
    )
    assert evidence.failed_tests == (
        () if expected_failures is None else ("tests.test_speed::test_failed",)
    )
    assert evidence.sha256 == hashlib.sha256(raw).hexdigest()
    assert parses == [raw]
    assert "PRIVATE_TOKEN" not in str(evidence)
