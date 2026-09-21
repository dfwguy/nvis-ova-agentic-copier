# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Contract tests for the project-only validation image."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

_VALIDATION_JOBS = {
    "kingfisher",
    "license-check",
    "readability-check",
    "coverage-py311",
    "coverage-py314",
    "pytest-py311",
    "pytest-py314",
    "ruff-check",
    "ruff-format",
    "shellcheck",
    "type-check",
    "yamllint",
}
_CANDIDATE_JOB = "ci-image:candidate"
_CANDIDATE_VALIDATION_IMAGE = (
    "$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID"
)
_KINGFISHER_CACHE = "/home/validation/.cache/kingfisher/rule-cache"
_KINGFISHER_COMPILE = (
    "/opt/ci/py311/bin/kingfisher rules compile-cache --no-update-check"
)
_KINGFISHER_SCAN = (
    "uv run --no-sync kingfisher scan . --git-history none --no-validate "
    "--no-update-check --exclude .git --exclude .cache --exclude .venv "
    "--exclude .tox --exclude .pytest_cache --exclude .ruff_cache"
)


def test_validation_image_contains_the_locked_non_root_toolchain(
    project_root: Path,
) -> None:
    """The project image freezes every repeated validation prerequisite."""
    dockerfile = (project_root / "Dockerfile").read_text()

    assert re.search(
        r"^FROM ghcr\.io/astral-sh/uv:0\.9\.30-python3\.11-bookworm-slim@"
        r"sha256:[0-9a-f]{64}$",
        dockerfile,
        flags=re.MULTILINE,
    )
    for package, version in {
        "git": "1:2.39.5-0+deb12u3",
        "jq": "1.6-2.1+deb12u2",
        "shellcheck": "0.9.0-1",
    }.items():
        assert f"{package}={version}" in dockerfile
    assert "uv python install 3.14.3 --managed-python" in dockerfile
    assert dockerfile.count("uv sync --locked --all-groups") == 2
    assert dockerfile.count("--no-install-project") == 2
    assert "--mount=type=secret,id=network-ca" in dockerfile
    assert (
        "cat /etc/ssl/certs/ca-certificates.crt /run/secrets/network-ca" in dockerfile
    )
    assert "SSL_CERT_FILE=/tmp/network-ca-bundle" in dockerfile
    assert "COPY --chown=10001:10001 pyproject.toml uv.lock ./" in dockerfile
    assert "COPY ." not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert dockerfile.index("USER 10001:10001") < dockerfile.index(
        "uv python install 3.14.3"
    )
    for executable in (
        "/opt/ci/py311/bin/python",
        "/opt/ci/py314/bin/python",
        "/opt/ci/py311/bin/pytest",
        "/opt/ci/py311/bin/ruff",
        "/opt/ci/py311/bin/yamllint",
        "/opt/ci/py311/bin/kingfisher",
        "/opt/ci/py311/bin/ty",
        "/opt/ci/py311/bin/uv",
        "git --version",
        "jq --version",
        "shellcheck --version",
    ):
        assert executable in dockerfile


def test_validation_image_compiles_kingfisher_rules_once_after_locked_installs(
    project_root: Path,
) -> None:
    """The non-root image build prepares rules before validating its tools."""
    dockerfile = (project_root / "Dockerfile").read_text()

    assert dockerfile.count(_KINGFISHER_COMPILE) == 1
    first_sync = dockerfile.index("uv sync --locked --all-groups")
    second_sync = dockerfile.index("uv sync --locked --all-groups", first_sync + 1)
    compile_rules = dockerfile.index(_KINGFISHER_COMPILE)
    verify_tools = dockerfile.index("RUN /opt/ci/py311/bin/python --version")
    final_workdir = dockerfile.rindex("WORKDIR /workspace")
    users_before_compile = re.findall(
        r"^USER\s+(\S+)", dockerfile[:compile_rules], flags=re.MULTILINE
    )

    assert users_before_compile[-1] == "10001:10001"
    assert first_sync < second_sync < compile_rules
    assert compile_rules < verify_tools < final_workdir


def test_validation_image_retains_the_stable_kingfisher_rule_cache(
    project_root: Path,
) -> None:
    """Only uv's download cache is removed after the rule cache is compiled."""
    dockerfile = (project_root / "Dockerfile").read_text()
    normalized = re.sub(r"\\\n\s*", " ", dockerfile)
    non_root_build = dockerfile[dockerfile.index("USER 10001:10001") :]
    normalized_non_root_build = re.sub(r"\\\n\s*", " ", non_root_build)
    cache_removals = tuple(
        tuple(shlex.split(operands))
        for operands in re.findall(r"rm -rf\s+([^&\n]+)", normalized_non_root_build)
    )

    assert re.search(
        rf"\bKF_RULE_CACHE_DIR={re.escape(_KINGFISHER_CACHE)}(?:\s|$)",
        normalized,
    )
    assert _KINGFISHER_COMPILE in normalized
    assert normalized.index(
        f"KF_RULE_CACHE_DIR={_KINGFISHER_CACHE}"
    ) < normalized.index(_KINGFISHER_COMPILE)
    assert cache_removals == (("/home/validation/.cache/uv",),)


def test_precompiled_rules_preserve_the_root_kingfisher_scan_contract(
    project_root: Path,
) -> None:
    """The image optimization keeps the established 1,055-rule scan behavior."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    template_ci = (project_root / "template" / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))

    assert configuration["kingfisher"] == {
        "extends": ".base_uv",
        "script": [_KINGFISHER_SCAN],
    }
    assert _KINGFISHER_COMPILE not in root_ci
    assert "KF_RULE_CACHE_DIR" not in root_ci
    assert _KINGFISHER_COMPILE not in template_ci
    assert "KF_RULE_CACHE_DIR" not in template_ci
    assert (project_root / "Dockerfile").read_text().count(_KINGFISHER_COMPILE) == 1


def test_candidate_image_builds_and_pushes_current_checkout_for_every_admitted_pipeline(
    project_root: Path,
) -> None:
    """Every admitted revision gets the exact validation image it needs."""
    ci_text = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(ci_text.replace("!reference ", ""))
    builder = configuration[".ci_image_builder"]
    candidate = configuration[_CANDIDATE_JOB]

    assert builder["stage"] == "ci-image"
    assert (builder | candidate)["stage"] == "ci-image"
    assert configuration["stages"].index("ci-image") < configuration["stages"].index(
        "validate"
    )
    assert re.fullmatch(
        r"docker\.io/moby/buildkit:v0\.33\.0-rootless@sha256:[0-9a-f]{64}",
        builder["image"]["name"],
    )
    assert builder["image"]["entrypoint"] == [""]
    assert builder["variables"] == {
        "BUILDKITD_FLAGS": "--oci-worker-no-process-sandbox"
    }
    assert builder["before_script"] == [
        "mkdir -p /tmp/ci-image-context",
        "cp Dockerfile pyproject.toml uv.lock /tmp/ci-image-context/",
    ]
    assert candidate["extends"] == ".ci_image_builder"
    assert candidate["tags"] == ["size/medium"]
    assert "rules" not in candidate
    assert candidate["before_script"][0] == [
        ".ci_image_builder",
        "before_script",
    ]
    candidate_setup = "\n".join(map(str, candidate["before_script"][1:]))
    assert 'mkdir -p "$HOME/.docker"' in candidate_setup
    assert "$CI_REGISTRY_USER" in candidate_setup
    assert "$CI_REGISTRY_PASSWORD" in candidate_setup
    assert '"$HOME/.docker/config.json"' in candidate_setup
    assert "echo" not in candidate_setup
    assert "set -x" not in candidate_setup
    build_command, digest_command = candidate["script"]
    for expected in (
        "--local context=/tmp/ci-image-context",
        "--local dockerfile=/tmp/ci-image-context",
        '--secret "id=network-ca,src=$CI_SERVER_TLS_CA_FILE"',
        "--metadata-file build-metadata.json",
        '"type=registry,ref=$CI_REGISTRY_IMAGE/validation-cache"',
        '"type=registry,ref=$CI_REGISTRY_IMAGE/validation-cache,mode=max"',
        "name=$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID,push=true",
    ):
        assert expected in build_command
    assert '"containerimage.digest"' in digest_command
    assert "^sha256:[0-9a-f]{64}$" in digest_command
    assert "CI_IMAGE=%s/validation@%s" in digest_command
    assert candidate["artifacts"] == {
        "reports": {"dotenv": "ci-image.env"},
        "paths": ["build-metadata.json", "ci-image.env"],
        "when": "always",
    }


def test_project_image_contract_does_not_leak_into_generated_ci(
    project_root: Path,
) -> None:
    """Generated projects remain independent of this project registry."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    template_ci = (project_root / "template" / ".gitlab-ci.yml").read_text()

    assert _CANDIDATE_JOB in root_ci
    assert "ci-image:validate" not in root_ci
    assert "ci-image:publish" not in root_ci
    assert "CI_REGISTRY_IMAGE" not in template_ci
    assert "moby/buildkit" not in template_ci


def test_root_validation_consumes_its_pipeline_candidate_without_bootstrap(
    project_root: Path,
) -> None:
    """Root validation waits for and uses its pipeline-specific candidate image."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))
    base = configuration[".base_uv"]
    before_script = "\n".join(base["before_script"])
    validation_contract = "\n".join(
        str(configuration[name])
        for name in (".pytest", ".pytest_coverage", *sorted(_VALIDATION_JOBS))
    )

    assert base["image"]["name"] == _CANDIDATE_VALIDATION_IMAGE
    expected_need = {"job": _CANDIDATE_JOB, "artifacts": False}
    assert base["needs"] == [expected_need]
    assert base["before_script"] == [
        'cd "$CI_PROJECT_DIR"',
        'git config --global --add safe.directory "$CI_PROJECT_DIR"',
    ]
    for runtime_setup in ("apt-get", "uv python install", "uv sync"):
        assert runtime_setup not in before_script
        assert runtime_setup not in validation_contract
    pytest_template = configuration[".base_uv"] | configuration[".pytest"]
    coverage_template = configuration[".base_uv"] | configuration[".pytest_coverage"]
    assert configuration[".pytest"]["extends"] == ".base_uv"
    assert configuration[".pytest_coverage"]["extends"] == ".base_uv"
    for job_name in _VALIDATION_JOBS:
        inherited_job = (
            pytest_template
            if job_name.startswith("pytest-")
            else coverage_template
            if job_name.startswith("coverage-")
            else configuration[".base_uv"]
        )
        effective_job = inherited_job | configuration[job_name]
        expected_needs = [expected_need]
        if job_name.startswith("coverage-"):
            expected_needs.append(
                {
                    "job": f"pytest-{job_name.removeprefix('coverage-')}",
                    "artifacts": True,
                }
            )
        assert effective_job["needs"] == expected_needs
        assert effective_job["image"]["name"] == _CANDIDATE_VALIDATION_IMAGE
    assert 'UV_PROJECT_ENVIRONMENT="$python_environment"' in str(
        configuration[".pytest"]
    )
    assert configuration["pytest-py311"]["variables"] == {
        "TEST_ENVIRONMENT": "py311",
    }
    assert configuration["pytest-py314"]["variables"] == {
        "TEST_ENVIRONMENT": "py314",
    }
    pytest_script = "\n".join(configuration[".pytest"]["script"])
    assert 'py311) python_environment="$CI_PY311_ENVIRONMENT"' in pytest_script
    assert 'py314) python_environment="$CI_PY314_ENVIRONMENT"' in pytest_script
    assert 'PYTHONPATH="$CI_PROJECT_DIR:$CI_PROJECT_DIR/src"' in str(
        configuration[".pytest"]
    )


def test_root_ci_exposes_each_gate_as_a_native_merge_request_job(
    project_root: Path,
) -> None:
    """Every validation result is an independently visible GitLab job."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))
    visible_validation_jobs = {
        name
        for name, value in configuration.items()
        if name in _VALIDATION_JOBS and isinstance(value, dict)
    }

    assert visible_validation_jobs == _VALIDATION_JOBS
    assert "validation (9 gates)" not in configuration
    assert configuration["workflow"]["rules"] == [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
        {"if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH"},
        {"when": "never"},
    ]
    for name in _VALIDATION_JOBS:
        expected_base = (
            ".pytest"
            if name.startswith("pytest-")
            else ".pytest_coverage"
            if name.startswith("coverage-")
            else ".base_uv"
        )
        assert configuration[name]["extends"] == expected_base
        effective_job = configuration[expected_base] | configuration[name]
        assert effective_job["script"]
        assert "run-ci-checks.sh" not in str(effective_job["script"])


@pytest.mark.parametrize(
    (
        "test_environment",
        "node_index",
        "node_total",
        "expected_environment",
        "expected_status",
        "expected_message",
    ),
    (
        ("py311", "1", "6", "/opt/ci/py311", 0, None),
        ("py314", "6", "6", "/opt/ci/py314", 0, None),
        ("unknown", "1", "6", None, 2, "Unknown test environment"),
        ("py311", None, "6", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", None, None, 2, "CI_NODE_TOTAL"),
        ("py311", "one", "6", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", "six", None, 2, "CI_NODE_TOTAL"),
        ("py311", "0", "6", None, 2, "CI_NODE_INDEX"),
        ("py311", "7", "6", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", "5", None, 2, "CI_NODE_TOTAL"),
    ),
)
def test_root_pytest_job_validates_gitlab_parallel_metadata_and_runs_one_native_shard(
    project_root: Path,
    tmp_path: Path,
    test_environment: str,
    node_index: str | None,
    node_total: str | None,
    expected_environment: str | None,
    expected_status: int,
    expected_message: str | None,
) -> None:
    """Each native shard rejects bad metadata before it invokes uv."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))
    script = "\n".join(configuration[".pytest"]["script"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    observed = tmp_path / "observed-invocation"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s|%s|%s\\n\' "$UV_PROJECT_ENVIRONMENT" "$COVERAGE_FILE" "$*" '
        '> "$OBSERVED"\n'
    )
    fake_uv.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "CI_PROJECT_DIR": str(tmp_path),
            "CI_PY311_ENVIRONMENT": "/opt/ci/py311",
            "CI_PY314_ENVIRONMENT": "/opt/ci/py314",
            "OBSERVED": str(observed),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "TEST_ENVIRONMENT": test_environment,
        }
    )
    environment.pop("CI_NODE_INDEX", None)
    environment.pop("CI_NODE_TOTAL", None)
    if node_index is not None:
        environment["CI_NODE_INDEX"] = node_index
    if node_total is not None:
        environment["CI_NODE_TOTAL"] = node_total

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert result.returncode == expected_status
    if expected_environment is None:
        assert not observed.exists()
        assert expected_message is not None
        assert expected_message in result.stderr
    else:
        assert node_index is not None
        observed_environment, coverage_file, command = (
            observed.read_text().strip().split("|", maxsplit=2)
        )
        assert observed_environment == expected_environment
        assert coverage_file == f".coverage.{test_environment}-shard-{node_index}"
        assert command == (
            "run --no-sync env -u UV_PROJECT_ENVIRONMENT pytest "
            "-p tests.helpers.serial_runtime_shard "
            f"--serial-runtime-shard-index={int(node_index) - 1} "
            "--serial-runtime-shard-count=6 "
            "--cov=src/agent_work "
            f"--junitxml=test-results-{test_environment}-shard-{node_index}.xml "
            f"-o junit_suite_name={test_environment} "
            f"-o cache_dir=.cache/pytest/{test_environment}/shard-{node_index}"
        )


def test_root_pytest_jobs_use_six_gitlab_native_shards_with_unique_artifacts(
    project_root: Path,
) -> None:
    """GitLab schedules one complete-suite partition per job and interpreter."""
    configuration = yaml.safe_load(
        (project_root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )
    pytest_template = configuration[".pytest"]
    assert pytest_template["artifacts"] == {
        "reports": {"junit": "test-results-$TEST_ENVIRONMENT-shard-$CI_NODE_INDEX.xml"},
        "paths": [".coverage.$TEST_ENVIRONMENT-shard-$CI_NODE_INDEX"],
        "when": "always",
    }
    assert "coverage combine" not in pytest_template["script"][0]
    for environment in ("py311", "py314"):
        job = configuration[f"pytest-{environment}"]
        assert job["extends"] == ".pytest"
        assert job["parallel"] == 6
        assert job["variables"] == {"TEST_ENVIRONMENT": environment}


@pytest.mark.parametrize("environment", ("py311", "py314"))
def test_root_coverage_job_combines_matching_six_shard_artifacts(
    project_root: Path,
    tmp_path: Path,
    environment: str,
) -> None:
    """Coverage aggregation waits for matching native shards and propagates failures."""
    configuration = yaml.safe_load(
        (project_root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )
    coverage_template = configuration[".pytest_coverage"]
    coverage_job = configuration[f"coverage-{environment}"]
    script = "\n".join(coverage_template["script"])
    expected_raw_files = [
        f".coverage.{environment}-shard-{index}" for index in range(1, 7)
    ]

    assert coverage_job["extends"] == ".pytest_coverage"
    assert coverage_job["variables"] == {"TEST_ENVIRONMENT": environment}
    assert coverage_job["needs"] == [
        {"job": _CANDIDATE_JOB, "artifacts": False},
        {"job": f"pytest-{environment}", "artifacts": True},
    ]
    assert coverage_template["artifacts"] == {
        "reports": {
            "coverage_report": {
                "coverage_format": "cobertura",
                "path": "coverage-$TEST_ENVIRONMENT.xml",
            }
        },
        "paths": ["coverage-$TEST_ENVIRONMENT.xml"],
        "when": "always",
    }
    assert [
        raw_file for raw_file in expected_raw_files if raw_file in script
    ] == expected_raw_files
    assert (
        script.index("coverage combine")
        < script.index("coverage report --fail-under=85")
        < script.index("coverage xml")
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invocations = tmp_path / "uv-invocations"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$INVOCATIONS"\n'
        'case "$*" in\n'
        "  *'coverage combine'*) exit \"${FAIL_COMBINE:-0}\" ;;\n"
        "  *'coverage report'*) exit \"${FAIL_REPORT:-0}\" ;;\n"
        "  *'coverage xml'*) exit \"${FAIL_XML:-0}\" ;;\n"
        "esac\n"
    )
    fake_uv.chmod(0o755)
    environment_variables = os.environ.copy() | {
        "CI_PY311_ENVIRONMENT": "/opt/ci/py311",
        "CI_PY314_ENVIRONMENT": "/opt/ci/py314",
        "INVOCATIONS": str(invocations),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_ENVIRONMENT": environment,
    }

    for failure_name, expected_status, expected_calls in (
        (None, 0, 3),
        ("FAIL_COMBINE", 8, 1),
        ("FAIL_REPORT", 9, 3),
        ("FAIL_XML", 10, 3),
    ):
        failure_environment = environment_variables.copy()
        if failure_name is not None:
            failure_environment[failure_name] = str(expected_status)
        start = len(invocations.read_text().splitlines()) if invocations.exists() else 0
        result = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env=failure_environment,
            text=True,
        )
        calls = invocations.read_text().splitlines()[start:]
        assert result.returncode == expected_status
        assert len(calls) == expected_calls
        assert calls[0].endswith("coverage combine " + " ".join(expected_raw_files))
        if failure_name == "FAIL_COMBINE":
            assert all(
                "coverage report" not in call and "coverage xml" not in call
                for call in calls
            )
        else:
            assert calls[1].endswith("coverage report --fail-under=85")
            assert calls[2].endswith(f"coverage xml -o coverage-{environment}.xml")


def test_root_coverage_job_rejects_unknown_environment_before_invoking_uv(
    project_root: Path,
    tmp_path: Path,
) -> None:
    """Coverage aggregation fails closed before invoking its external runner."""
    configuration = yaml.safe_load(
        (project_root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )
    script = "\n".join(configuration[".pytest_coverage"]["script"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invoked = tmp_path / "uv-invoked"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text('#!/usr/bin/env bash\ntouch "$UV_INVOKED"\n')
    fake_uv.chmod(0o755)
    environment = os.environ.copy() | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TEST_ENVIRONMENT": "unknown",
        "UV_INVOKED": str(invoked),
    }

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert result.returncode == 2
    assert "Unknown test environment" in result.stderr
    assert not invoked.exists()


def test_portable_template_keeps_its_independent_validation_bootstrap(
    project_root: Path,
) -> None:
    """The generated template stays independent of the root image."""
    template_ci = (project_root / "template" / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(template_ci)

    assert _CANDIDATE_VALIDATION_IMAGE not in template_ci
    assert "gitlab-master.nvidia.com:5005/nvis-ova" not in template_ci
    assert "apt-get install -y --no-install-recommends git jq shellcheck" in template_ci
    assert "uv python install 3.14.3 --managed-python" in template_ci
    assert "UV_PROJECT_ENVIRONMENT=/opt/ci" not in template_ci
    assert "validation (10 gates)" in configuration
    assert not (_VALIDATION_JOBS & configuration.keys())
