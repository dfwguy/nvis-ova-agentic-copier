# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the interactive, read-only local timing comparison."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import pytest

from agent_work.local_test_timing import _junit_cases

BASE_TREE = "a" * 40
CANDIDATE_TREE = "b" * 40
SLOW = "tests/test_template.py::test_rendered_project[py-3.11]"
OTHER = "tests/test_agent_work.py::test_other"
NEW = "tests/test_agent_work.py::test_new[variant-A]"
PARITY = (
    "tests/test_template.py::"
    "test_serial_runner_assets_have_exact_root_template_and_generated_parity"
    "[src/agent_work/template_update_publication.py]"
)


def test_junit_class_case_matches_pytest_selector() -> None:
    artifact = (
        b"<testsuite tests='1' failures='0' errors='0'>"
        b"<testcase classname='tests.test_speed.TestCase' "
        b"name='test_case' time='0.08'/>"
        b"</testsuite>"
    )

    assert _junit_cases(artifact, 1) == {
        "tests/test_speed.py::TestCase::test_case": 0.08
    }


def _xml(times: Mapping[str, float]) -> bytes:
    cases = "".join(
        f'<testcase classname="{escape(node.split("::")[0].removesuffix(".py").replace("/", "."))}" '
        f'name="{escape("::".join(node.split("::")[1:]))}" time="{seconds:.3f}" />'
        for node, seconds in times.items()
    )
    return f'<testsuite tests="{len(times)}" failures="0" errors="0">{cases}</testsuite>'.encode()


def _phase_log(phases: Mapping[str, tuple[float, float, float]]) -> bytes:
    lines = [
        "============================= slowest durations ============================="
    ]
    for node, (setup, call, teardown) in phases.items():
        lines.extend(
            f"{seconds:.3f}s {phase} {node}"
            for phase, seconds in (
                ("setup", setup),
                ("call", call),
                ("teardown", teardown),
            )
        )
    return ("\n".join(lines) + "\n").encode()


def _artifact(
    files: dict[Path, bytes], path: str, body: bytes, *, count: int | None = None
) -> dict[str, object]:
    location = Path(path)
    files[location] = body
    artifact: dict[str, object] = {
        "path": str(location),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if count is not None:
        artifact["test_count"] = count
    return artifact


def _bundle(
    *,
    environment: str = "py311",
    baseline: tuple[dict[str, tuple[float, float, float]], ...] | None = None,
    candidate: tuple[dict[str, tuple[float, float, float]], ...] | None = None,
    full_times: dict[str, float] | None = None,
    other_full_times: dict[str, float] | None = None,
    changed: tuple[str, ...] = (),
) -> tuple[dict[str, Any], dict[Path, bytes]]:
    baseline = baseline or (
        {SLOW: (0.010, 0.240, 0.010)},
        {SLOW: (0.010, 0.230, 0.010)},
    )
    candidate = candidate or (
        {SLOW: (0.010, 0.180, 0.010)},
        {SLOW: (0.010, 0.170, 0.010)},
    )
    full_times = full_times or {SLOW: 0.190, OTHER: 0.100}
    files: dict[Path, bytes] = {}
    interpreter = f".tox/{environment}/bin/python"
    argv_prefix = (
        "uv",
        "run",
        "--no-project",
        "--python",
        interpreter,
        "python",
        "-m",
        "pytest",
        "-q",
        "--no-cov",
        "--durations=0",
        "--randomly-seed=262",
    )
    runs: dict[str, list[dict[str, object]]] = {"baseline": [], "candidate": []}
    for side, tree, repeats in (
        ("baseline", BASE_TREE, baseline),
        ("candidate", CANDIDATE_TREE, candidate),
    ):
        for index, phases in enumerate(repeats, start=1):
            times = {node: sum(values) for node, values in phases.items()}
            prefix = f"/evidence/{environment}-{side}-{index}"
            runs[side].append(
                {
                    "tree": tree,
                    "environment": environment,
                    "interpreter": interpreter,
                    "command": (
                        *argv_prefix,
                        f"--junitxml={prefix}.xml",
                        *sorted(set().union(*(set(x) for x in repeats))),
                    ),
                    "seed": 262,
                    "junit": _artifact(
                        files, f"{prefix}.xml", _xml(times), count=len(times)
                    ),
                    "phase_log": _artifact(files, f"{prefix}.log", _phase_log(phases)),
                }
            )
    manifest: dict[str, object] = {
        "baseline_tree": BASE_TREE,
        "candidate_tree": CANDIDATE_TREE,
        "changed_test_ids": changed,
        "environments": {
            environment: {
                "candidate_full_junit": _artifact(
                    files,
                    f"/evidence/{environment}-full.xml",
                    _xml(full_times),
                    count=len(full_times),
                ),
                "baseline_runs": runs["baseline"],
                "candidate_runs": runs["candidate"],
            }
        },
    }
    if environment == "py311":
        other, other_files = _bundle(
            environment="py314",
            baseline=baseline,
            candidate=candidate,
            full_times=other_full_times if other_full_times is not None else full_times,
            changed=changed,
        )
        manifest["environments"].update(other["environments"])
        files.update(other_files)
    return manifest, files


def _compare(
    manifest: dict[str, object],
    files: dict[Path, bytes],
    *,
    baseline_tree: str = BASE_TREE,
    candidate_tree: str = CANDIDATE_TREE,
) -> Any:
    from agent_work.local_test_timing import compare_local_test_timings

    def read_bytes(path: Path) -> bytes:
        return files[path]

    return compare_local_test_timings(
        manifest,
        expected_baseline_tree=baseline_tree,
        expected_candidate_tree=candidate_tree,
        read_bytes=read_bytes,
    )


def _case(result: Any, name: str) -> Any:
    return next(case for case in result.compared_cases if case.name == name)


def test_inherited_slow_case_is_visible_without_automatic_rejection_when_faster() -> (
    None
):
    manifest, files = _bundle()

    result = _compare(manifest, files)

    case = _case(result, SLOW)
    assert result.status == "clear"
    assert case.classification == "inherited"
    assert case.baseline_seconds == pytest.approx((0.260, 0.250))
    assert case.candidate_seconds == pytest.approx((0.200, 0.190))
    assert case.signed_delta_seconds == pytest.approx((-0.060, -0.060))
    assert case.dominant_phase == "call"
    assert result.over_100ms_cases["py311"] == ((SLOW, 0.190),)


def test_modified_existing_slow_case_that_improves_remains_visible_without_rejection() -> (
    None
):
    manifest, files = _bundle(changed=(SLOW,))

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert _case(result, SLOW).classification == "inherited"
    assert _case(result, SLOW).changed is True


def test_focused_candidate_case_missing_from_full_junit_is_inconclusive() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010), OTHER: (0.010, 0.180, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), OTHER: (0.010, 0.080, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190},
        changed=(),
    )

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons and "full JUnit" in result.reasons[0]


def test_new_case_above_100ms_is_advisory_without_baseline_case() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.140, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190, NEW: 0.160},
        changed=(NEW,),
    )

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert _case(result, NEW).classification == "introduced"
    assert _case(result, NEW).baseline_seconds == ()
    assert _case(result, NEW).candidate_seconds == pytest.approx((0.160, 0.160))


def test_new_case_at_exactly_one_second_is_allowed() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.140, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190, NEW: 1.000},
        changed=(NEW,),
    )

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert _case(result, NEW).classification == "introduced"
    assert result.at_least_1s_cases["py311"] == ((NEW, 1.000),)


def test_new_case_above_one_second_requires_review_from_full_junit() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.050, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190, NEW: 1.001},
        changed=(NEW,),
    )

    result = _compare(manifest, files)

    assert result.status == "review"
    assert _case(result, NEW).classification == "introduced"
    assert _case(result, NEW).candidate_seconds == pytest.approx((0.070, 0.070))


def test_modified_case_above_one_second_requires_review_even_if_focused_run_improves() -> (
    None
):
    manifest, files = _bundle(full_times={SLOW: 1.001, OTHER: 0.100}, changed=(SLOW,))

    result = _compare(manifest, files)

    assert result.status == "review"
    assert _case(result, SLOW).changed is True
    assert _case(result, SLOW).classification == "inherited"


def test_unmodified_case_above_one_second_does_not_gate_new_advisory_case() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.140, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 1.001, NEW: 0.160},
        changed=(NEW,),
    )

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert result.at_least_1s_cases["py311"] == ((SLOW, 1.001),)


@pytest.mark.parametrize("missing_side", ["baseline_runs", "candidate_runs"])
def test_absent_paired_proof_is_unknown_with_valid_candidate_inventory(
    missing_side: str,
) -> None:
    manifest, files = _bundle(full_times={SLOW: 0.190, NEW: 1.250}, changed=(SLOW,))
    for evidence in manifest["environments"].values():
        del evidence[missing_side]

    result = _compare(manifest, files)

    assert result.status == "unknown"
    assert result.over_100ms_cases == {
        "py311": ((SLOW, 0.190), (NEW, 1.250)),
        "py314": ((SLOW, 0.190), (NEW, 1.250)),
    }
    assert result.at_least_1s_cases == {
        "py311": ((NEW, 1.250),),
        "py314": ((NEW, 1.250),),
    }


@pytest.mark.parametrize("missing_side", ["baseline_runs", "candidate_runs"])
def test_changed_case_above_one_second_requires_review_when_paired_proof_absent(
    missing_side: str,
) -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.050, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190, NEW: 1.001},
        changed=(NEW,),
    )
    for evidence in manifest["environments"].values():
        del evidence[missing_side]

    result = _compare(manifest, files)

    assert result.status == "review"
    assert result.over_100ms_cases == {
        "py311": ((SLOW, 0.190), (NEW, 1.001)),
        "py314": ((SLOW, 0.190), (NEW, 1.001)),
    }
    assert result.at_least_1s_cases == {
        "py311": ((NEW, 1.001),),
        "py314": ((NEW, 1.001),),
    }


def test_unchanged_case_with_consistent_candidate_slowdown_is_advisory() -> None:
    baseline = ({SLOW: (0.010, 0.090, 0.010)}, {SLOW: (0.010, 0.100, 0.010)})
    candidate = ({SLOW: (0.010, 0.190, 0.010)}, {SLOW: (0.010, 0.200, 0.010)})
    manifest, files = _bundle(baseline=baseline, candidate=candidate)

    result = _compare(manifest, files)

    case = _case(result, SLOW)
    assert result.status == "clear"
    assert case.classification == "consistent_slowdown"
    assert case.changed is False
    assert case.signed_delta_seconds == pytest.approx((0.100, 0.100))
    assert case.dominant_phase == "call"


def test_setup_increase_is_attributed_to_setup_without_claiming_body_increase() -> None:
    baseline = ({SLOW: (0.020, 0.070, 0.010)},) * 2
    candidate = ({SLOW: (0.220, 0.070, 0.010)},) * 2
    manifest, files = _bundle(baseline=baseline, candidate=candidate)

    result = _compare(manifest, files)

    case = _case(result, SLOW)
    assert case.classification == "consistent_slowdown"
    assert case.dominant_phase == "setup"
    assert case.phase_seconds["call"] == ((0.070, 0.070), (0.070, 0.070))
    assert case.phase_seconds["setup"] == ((0.020, 0.020), (0.220, 0.220))


def test_noisy_repeats_are_inconclusive_instead_of_a_regression() -> None:
    baseline = ({SLOW: (0.010, 0.120, 0.010)}, {SLOW: (0.010, 0.280, 0.010)})
    candidate = ({SLOW: (0.010, 0.240, 0.010)}, {SLOW: (0.010, 0.130, 0.010)})
    manifest, files = _bundle(baseline=baseline, candidate=candidate)

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert _case(result, SLOW).classification == "inconclusive"


def test_unequal_repeat_counts_cannot_hide_a_mixed_direction() -> None:
    baseline = ({SLOW: (0.010, 0.080, 0.010)},) * 2
    candidate = (
        {SLOW: (0.010, 0.280, 0.010)},
        {SLOW: (0.010, 0.280, 0.010)},
        {SLOW: (0.010, 0.030, 0.010)},
    )
    manifest, files = _bundle(baseline=baseline, candidate=candidate)

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons and "equal" in result.reasons[0]


@pytest.mark.parametrize("missing_side", ["baseline_runs", "candidate_runs"])
def test_fewer_than_two_repeats_is_inconclusive(missing_side: str) -> None:
    manifest, files = _bundle()
    environment = manifest["environments"]["py311"]
    environment[missing_side] = environment[missing_side][:1]

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons


@pytest.mark.parametrize(
    "fault",
    ["digest", "tree", "interpreter", "seed", "count", "command", "missing_file"],
)
def test_mismatched_or_missing_artifact_proof_is_inconclusive(fault: str) -> None:
    manifest, files = _bundle()
    run = manifest["environments"]["py311"]["candidate_runs"][0]
    if fault == "digest":
        run["phase_log"]["sha256"] = "0" * 64
    elif fault == "tree":
        run["tree"] = "c" * 40
    elif fault == "interpreter":
        run["interpreter"] = ".tox/py314/bin/python"
    elif fault == "seed":
        run["seed"] = 999
    elif fault == "count":
        run["junit"]["test_count"] = 99
    elif fault == "command":
        run["command"] = ("pytest", "-q")
    else:
        del files[Path(run["junit"]["path"])]

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons


def test_expected_tree_identity_mismatch_is_inconclusive() -> None:
    manifest, files = _bundle()

    result = _compare(manifest, files, candidate_tree="c" * 40)

    assert result.status == "inconclusive"
    assert result.reasons


def test_inventory_is_complete_for_both_supported_versions_and_preserves_parameter_ids() -> (
    None
):
    first, first_files = _bundle(
        full_times={SLOW: 0.101, NEW: 1.000, OTHER: 0.100},
    )
    second, second_files = _bundle(
        environment="py314",
        full_times={SLOW: 0.102, NEW: 1.250, OTHER: 0.100},
    )
    first["environments"].update(second["environments"])
    first_files.update(second_files)

    result = _compare(first, first_files)

    assert result.over_100ms_cases == {
        "py311": ((SLOW, 0.101), (NEW, 1.000)),
        "py314": ((SLOW, 0.102), (NEW, 1.250)),
    }
    assert result.at_least_1s_cases == {
        "py311": ((NEW, 1.000),),
        "py314": ((NEW, 1.250),),
    }


def test_full_inventory_can_exceed_focused_run_case_bound() -> None:
    full_times = {SLOW: 0.190} | {
        f"tests/test_scale.py::test_case_{index}": 0.101 for index in range(1024)
    }
    manifest, files = _bundle(full_times=full_times, other_full_times={SLOW: 0.190})

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert len(result.over_100ms_cases["py311"]) == 1025


def test_real_pytest_phase_padding_and_rounding_remain_comparable() -> None:
    manifest, files = _bundle()
    run = manifest["environments"]["py311"]["baseline_runs"][0]
    artifact = run["phase_log"]
    body = (
        "============================== slowest durations ===============================\n"
        f"0.01s setup    {SLOW}\n"
        f"0.23s call     {SLOW}\n"
        f"0.01s teardown {SLOW}\n"
    ).encode()
    files[Path(artifact["path"])] = body
    artifact["sha256"] = hashlib.sha256(body).hexdigest()

    result = _compare(manifest, files)

    assert result.status == "clear"


def test_changed_slow_case_missing_from_focused_runs_is_inconclusive() -> None:
    manifest, files = _bundle(full_times={SLOW: 0.190, NEW: 0.160}, changed=(NEW,))

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons


def test_introduced_case_under_one_second_is_advisory_if_change_list_omits_it() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.140, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline, candidate=candidate, full_times={SLOW: 0.190, NEW: 0.160}
    )

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert _case(result, NEW).classification == "introduced"


@pytest.mark.parametrize("mismatch", [False, True])
def test_seed_is_bound_across_repeats_and_command(mismatch: bool) -> None:
    manifest, files = _bundle()
    environment = manifest["environments"]["py311"]
    for side in ("baseline_runs", "candidate_runs"):
        for run in environment[side]:
            run["seed"] = 314
            run["command"] = tuple(
                "--randomly-seed=314" if arg == "--randomly-seed=262" else arg
                for arg in run["command"]
            )
    if mismatch:
        environment["candidate_runs"][1]["seed"] = 315

    result = _compare(manifest, files)

    assert result.status == ("inconclusive" if mismatch else "clear")


def test_failing_junit_is_inconclusive_even_with_valid_digest() -> None:
    manifest, files = _bundle()
    artifact = manifest["environments"]["py311"]["candidate_runs"][0]["junit"]
    path = Path(artifact["path"])
    body = files[path].replace(b'failures="0"', b'failures="1"')
    files[path] = body
    artifact["sha256"] = hashlib.sha256(body).hexdigest()

    result = _compare(manifest, files)

    assert result.status == "inconclusive"


def test_candidate_only_test_id_has_realistic_side_specific_selection() -> None:
    baseline = ({SLOW: (0.010, 0.240, 0.010)},) * 2
    candidate = ({SLOW: (0.010, 0.170, 0.010), NEW: (0.010, 0.140, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={SLOW: 0.190, NEW: 0.160},
        changed=(NEW,),
    )
    for run in manifest["environments"]["py311"]["baseline_runs"]:
        run["command"] = tuple(arg for arg in run["command"] if arg != NEW)

    result = _compare(manifest, files)

    assert result.status == "clear"
    assert _case(result, NEW).classification == "introduced"


def test_verbose_pytest_phase_control_preserves_comparable_runs() -> None:
    manifest, files = _bundle()
    environment = manifest["environments"]["py311"]
    for side in ("baseline_runs", "candidate_runs"):
        for run in environment[side]:
            run["command"] = tuple(
                "-vv" if arg == "-q" else arg for arg in run["command"]
            )
            artifact = run["phase_log"]
            path = Path(artifact["path"])
            body = (
                files[path]
                .replace(b"s setup ", b"s setup    ")
                .replace(b"s call ", b"s call     ")
            )
            files[path] = body
            artifact["sha256"] = hashlib.sha256(body).hexdigest()

    result = _compare(manifest, files)

    assert result.status == "clear"


def test_documented_verbose_command_without_quiet_flag_is_accepted() -> None:
    manifest, files = _bundle()
    environment = manifest["environments"]["py311"]
    for side in ("baseline_runs", "candidate_runs"):
        for run in environment[side]:
            run["command"] = tuple(
                "-vv" if arg == "-q" else arg for arg in run["command"]
            )

    result = _compare(manifest, files)

    assert result.status == "clear"


def test_mixed_quiet_and_verbose_flags_are_inconclusive() -> None:
    manifest, files = _bundle()
    environment = manifest["environments"]["py311"]
    for side in ("baseline_runs", "candidate_runs"):
        for run in environment[side]:
            run["command"] = (*run["command"], "-vv")

    result = _compare(manifest, files)

    assert result.status == "inconclusive"


def test_slash_parameter_is_hashed_consistently_and_kept_in_full_inventory() -> None:
    baseline = ({PARITY: (0.010, 0.200, 0.010)},) * 2
    candidate = ({PARITY: (0.010, 0.150, 0.010)},) * 2
    manifest, files = _bundle(
        baseline=baseline,
        candidate=candidate,
        full_times={PARITY: 0.170, SLOW: 0.190},
    )

    result = _compare(manifest, files)

    safe = PARITY.partition("[")[0]
    hashed = f"{safe}[param_{hashlib.sha256(PARITY.encode()).hexdigest()}]"
    assert result.status == "clear"
    assert _case(result, hashed).classification == "inherited"
    assert result.over_100ms_cases["py311"] == ((hashed, 0.170), (SLOW, 0.190))


def test_long_parameter_is_hashed_within_bounded_full_junit_artifact() -> None:
    long_id = f"tests/test_template.py::test_long[{'x' * 2048}]"
    manifest, files = _bundle(full_times={SLOW: 0.190, long_id: 0.120})

    result = _compare(manifest, files)

    safe = long_id.partition("[")[0]
    hashed = f"{safe}[param_{hashlib.sha256(long_id.encode()).hexdigest()}]"
    assert result.status == "clear"
    assert result.over_100ms_cases["py311"] == ((SLOW, 0.190), (hashed, 0.120))


def test_compared_rows_identify_their_python_environment() -> None:
    manifest, files = _bundle()

    result = _compare(manifest, files)

    assert {(case.environment, case.name) for case in result.compared_cases} == {
        ("py311", SLOW),
        ("py314", SLOW),
    }


@pytest.mark.parametrize("fault", ["reused_artifacts", "unbound_junit_command"])
def test_focused_runs_require_distinct_command_bound_artifacts(fault: str) -> None:
    manifest, files = _bundle()
    runs = manifest["environments"]["py311"]["candidate_runs"]
    if fault == "reused_artifacts":
        runs[1]["junit"] = runs[0]["junit"]
        runs[1]["phase_log"] = runs[0]["phase_log"]
    else:
        runs[0]["command"] = tuple(
            "--junitxml=/evidence/wrong.xml" if arg.startswith("--junitxml=") else arg
            for arg in runs[0]["command"]
        )

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons


def test_single_environment_cannot_return_clear() -> None:
    manifest, files = _bundle()
    del manifest["environments"]["py314"]

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons


def test_alias_path_cannot_reuse_one_run_as_two_repeats() -> None:
    manifest, files = _bundle()
    runs = manifest["environments"]["py311"]["candidate_runs"]
    first, second = runs
    for kind in ("junit", "phase_log"):
        original = first[kind]
        path = Path(original["path"])
        second[kind] = {
            **original,
            "path": f"{path.parent}/./{path.name}",
        }
    second["command"] = tuple(
        f"--junitxml={second['junit']['path']}"
        if arg.startswith("--junitxml=")
        else arg
        for arg in second["command"]
    )

    result = _compare(manifest, files)

    assert result.status == "inconclusive"
    assert result.reasons
