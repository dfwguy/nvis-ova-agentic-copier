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
import subprocess
from pathlib import Path

import pytest
import yaml

_IMAGE_INPUTS = ("Dockerfile", "pyproject.toml", "uv.lock")
_VALIDATION_JOBS = {
    "kingfisher",
    "license-check",
    "pytest-py311",
    "pytest-py314",
    "ruff-check",
    "ruff-format",
    "shellcheck",
    "type-check",
    "yamllint",
}
_PUBLISHED_VALIDATION_IMAGE = (
    "gitlab-master.nvidia.com:5005/nvis-ova/nvis-ova-agentic-copier/validation@"
    "sha256:adf3aa8e0f7a1996766e80d576c45f1e7677626f863829cec8114fabf8747521"
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


def test_ci_image_validation_builds_without_registry_credentials_or_push(
    project_root: Path,
) -> None:
    """Merge requests prove the image build without publishing it."""
    ci_text = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(ci_text.replace("!reference ", ""))
    builder = configuration[".ci_image_builder"]
    validation = configuration["ci-image:validate"]

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
    assert validation["extends"] == ".ci_image_builder"
    assert validation["tags"] == ["size/medium"]
    assert validation["rules"] == [
        {
            "if": '$CI_PIPELINE_SOURCE == "merge_request_event"',
            "changes": {"paths": list(_IMAGE_INPUTS)},
        },
        {"when": "never"},
    ]
    assert validation["script"][0].splitlines() == [
        "buildctl-daemonless.sh build \\",
        "  --frontend dockerfile.v0 \\",
        "  --local context=/tmp/ci-image-context \\",
        "  --local dockerfile=/tmp/ci-image-context \\",
        '  --secret "id=network-ca,src=$CI_SERVER_TLS_CA_FILE"',
    ]
    effective_validation_text = str((builder, validation))
    assert "push=true" not in effective_validation_text
    assert "CI_REGISTRY_PASSWORD" not in effective_validation_text
    assert "CI_REGISTRY_USER" not in effective_validation_text


def test_ci_image_publication_is_default_branch_only_and_emits_digest(
    project_root: Path,
) -> None:
    """Only reviewed toolchain changes publish an immutable image receipt."""
    ci_text = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(ci_text.replace("!reference ", ""))
    publication = configuration["ci-image:publish"]
    job_text = str(publication)

    assert publication["extends"] == ".ci_image_builder"
    assert publication["tags"] == ["size/medium"]
    assert publication["rules"] == [
        {
            "if": "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH",
            "changes": {"paths": list(_IMAGE_INPUTS)},
        },
        {"when": "never"},
    ]
    assert "CI_REGISTRY_USER" in job_text
    assert "CI_REGISTRY_PASSWORD" in job_text
    assert publication["before_script"][0] == [
        ".ci_image_builder",
        "before_script",
    ]
    publisher_setup = "\n".join(map(str, publication["before_script"][1:]))
    assert 'mkdir -p "$HOME/.docker"' in publisher_setup
    assert "$CI_REGISTRY_USER" in publisher_setup
    assert "$CI_REGISTRY_PASSWORD" in publisher_setup
    assert '"$HOME/.docker/config.json"' in publisher_setup
    build_command, digest_command = publication["script"]
    assert build_command.splitlines() == [
        "buildctl-daemonless.sh build \\",
        "  --frontend dockerfile.v0 \\",
        "  --local context=/tmp/ci-image-context \\",
        "  --local dockerfile=/tmp/ci-image-context \\",
        '  --secret "id=network-ca,src=$CI_SERVER_TLS_CA_FILE" \\',
        "  --metadata-file build-metadata.json \\",
        (
            '  --output "type=image,name=$CI_REGISTRY_IMAGE/validation:'
            '$CI_COMMIT_SHA,push=true"'
        ),
    ]
    assert '"containerimage.digest"' in digest_command
    assert "^sha256:[0-9a-f]{64}$" in digest_command
    assert "CI_IMAGE=%s/validation@%s" in digest_command
    assert publication["artifacts"] == {
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

    assert "ci-image:validate" in root_ci
    assert "ci-image:publish" in root_ci
    assert "CI_REGISTRY_IMAGE" not in template_ci
    assert "moby/buildkit" not in template_ci


def test_root_validation_consumes_the_published_image_without_bootstrap(
    project_root: Path,
) -> None:
    """Root validation reuses the reviewed image without downloading tools."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))
    base = configuration[".base_uv"]
    before_script = "\n".join(base["before_script"])
    validation_contract = "\n".join(
        str(configuration[name]) for name in (".pytest", *sorted(_VALIDATION_JOBS))
    )

    assert base["image"]["name"] == _PUBLISHED_VALIDATION_IMAGE
    assert base["needs"] == []
    assert base["before_script"] == ['cd "$CI_PROJECT_DIR"']
    for runtime_setup in ("apt-get", "uv python install", "uv sync"):
        assert runtime_setup not in before_script
        assert runtime_setup not in validation_contract
    assert configuration[".pytest"]["extends"] == ".base_uv"
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
    assert 'PYTHONPATH="$CI_PROJECT_DIR/src"' in str(configuration[".pytest"])


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
        expected_base = ".pytest" if name.startswith("pytest-") else ".base_uv"
        assert configuration[name]["extends"] == expected_base
        effective_job = configuration[expected_base] | configuration[name]
        assert effective_job["script"]
        assert "run-ci-checks.sh" not in str(effective_job["script"])


@pytest.mark.parametrize(
    ("test_environment", "expected_environment", "expected_status"),
    (
        ("py311", "/opt/ci/py311", 0),
        ("py314", "/opt/ci/py314", 0),
        ("unknown", None, 2),
    ),
)
def test_root_pytest_job_selects_the_image_environment_at_runtime(
    project_root: Path,
    tmp_path: Path,
    test_environment: str,
    expected_environment: str | None,
    expected_status: int,
) -> None:
    """The shared pytest shell maps labels to image environments safely."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(root_ci.replace("!reference ", ""))
    script = "\n".join(configuration[".pytest"]["script"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    observed = tmp_path / "observed-environment"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$UV_PROJECT_ENVIRONMENT" > "$OBSERVED"\n'
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
        assert "Unknown test environment" in result.stderr
    else:
        assert observed.read_text().strip() == expected_environment


def test_root_pytest_job_runs_balanced_shards_and_combines_stable_source_coverage(
    project_root: Path,
    tmp_path: Path,
) -> None:
    """Each image environment separates costly groups and combines all coverage."""
    configuration = yaml.safe_load(
        (project_root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )
    pytest_template = configuration[".pytest"]
    script = "\n".join(pytest_template["script"])

    assert pytest_template["artifacts"]["reports"]["junit"] == [
        "test-results-$TEST_ENVIRONMENT-shard-1.xml",
        "test-results-$TEST_ENVIRONMENT-shard-2.xml",
        "test-results-$TEST_ENVIRONMENT-shard-3.xml",
    ]
    assert "tests/test_agent_work_serial_runtime.py" in script
    assert "tests/test_template.py" in script
    assert "--ignore=tests/test_agent_work_serial_runtime.py" in script
    assert "--ignore=tests/test_template.py" in script
    assert "--cov=src/agent_work" in script
    assert "coverage combine \\" in script
    assert '".coverage.$TEST_ENVIRONMENT-shard-1"' in script
    assert '".coverage.$TEST_ENVIRONMENT-shard-2"' in script
    assert '".coverage.$TEST_ENVIRONMENT-shard-3"' in script
    assert "coverage report --fail-under=85" in script
    assert 'coverage xml -o "coverage-$TEST_ENVIRONMENT.xml"' in script
    assert script.index("coverage report --fail-under=85") < script.index(
        "coverage xml"
    )

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invocations = tmp_path / "uv-invocations"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s|%s|%s\\n\' "$PYTHONPATH" "$COVERAGE_FILE" "$*" '
        '>> "$INVOCATIONS"\n'
        'case "$*" in\n'
        "    *'--ignore=tests/test_agent_work_serial_runtime.py'*) shard=remaining ;;\n"
        "    *'tests/test_agent_work_serial_runtime.py'*) shard=agent-work ;;\n"
        "    *'tests/test_template.py'*) shard=template ;;\n"
        "    *) shard=other ;;\n"
        "esac\n"
        'if [[ "${FAIL_SHARD:-}" = "$shard" ]]; then\n'
        "    exit 7\n"
        "fi\n"
        "if [[ \"$*\" == *'coverage report --fail-under=85'* ]] && "
        '[[ "${FAIL_COVERAGE_REPORT:-0}" = 1 ]]; then\n'
        "    exit 9\n"
        "fi\n"
        "if [[ \"$*\" == *'coverage combine'* ]] && "
        '[[ "${FAIL_COVERAGE_COMBINE:-0}" = 1 ]]; then\n'
        "    exit 8\n"
        "fi\n"
    )
    fake_uv.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "CI_PROJECT_DIR": str(tmp_path),
            "CI_PY311_ENVIRONMENT": "/opt/ci/py311",
            "CI_PY314_ENVIRONMENT": "/opt/ci/py314",
            "INVOCATIONS": str(invocations),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "TEST_ENVIRONMENT": "py311",
        }
    )

    succeeded = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert succeeded.returncode == 0
    calls = invocations.read_text().splitlines()
    assert len(calls) == 6
    assert {call.split("|", maxsplit=2)[1] for call in calls[:3]} == {
        ".coverage.py311-shard-1",
        ".coverage.py311-shard-2",
        ".coverage.py311-shard-3",
    }
    shard_calls = calls[:3]
    assert all(call.startswith(f"{tmp_path / 'src'}|") for call in shard_calls)
    assert all("--cov=src/agent_work" in call for call in shard_calls)
    serial_runtime_call = next(
        call
        for call in shard_calls
        if "tests/test_agent_work_serial_runtime.py" in call
        and "--ignore=tests/test_agent_work_serial_runtime.py" not in call
    )
    template_call = next(
        call
        for call in shard_calls
        if "tests/test_template.py" in call
        and "--ignore=tests/test_template.py" not in call
    )
    assert serial_runtime_call != template_call
    remaining_call = next(
        call
        for call in shard_calls
        if "--ignore=tests/test_agent_work_serial_runtime.py" in call
    )
    assert "--ignore=tests/test_template.py" in remaining_call

    for failed_shard in ("agent-work", "template", "remaining"):
        failure_start = len(invocations.read_text().splitlines())
        failed = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env=environment | {"FAIL_SHARD": failed_shard},
            text=True,
        )

        assert failed.returncode != 0
        failure_calls = invocations.read_text().splitlines()[failure_start:]
        assert len(failure_calls) == 3
        assert all("coverage combine" not in call for call in failure_calls)
        assert all("coverage report" not in call for call in failure_calls)
        assert all("coverage xml" not in call for call in failure_calls)

    coverage_failed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment | {"FAIL_COVERAGE_REPORT": "1"},
        text=True,
    )

    assert coverage_failed.returncode == 9
    coverage_failure_calls = invocations.read_text().splitlines()[-6:]
    report_index = next(
        index
        for index, call in enumerate(coverage_failure_calls)
        if "coverage report --fail-under=85" in call
    )
    xml_index = next(
        index
        for index, call in enumerate(coverage_failure_calls)
        if "coverage xml" in call
    )
    assert report_index < xml_index

    combine_failed = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment | {"FAIL_COVERAGE_COMBINE": "1"},
        text=True,
    )

    assert combine_failed.returncode == 8
    combine_failure_calls = invocations.read_text().splitlines()[-4:]
    assert any("coverage combine" in call for call in combine_failure_calls)
    assert all("coverage report" not in call for call in combine_failure_calls)
    assert all("coverage xml" not in call for call in combine_failure_calls)


def test_portable_template_keeps_its_independent_validation_bootstrap(
    project_root: Path,
) -> None:
    """Generated repositories do not depend on this project's private image."""
    template_ci = (project_root / "template" / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(template_ci)

    assert _PUBLISHED_VALIDATION_IMAGE not in template_ci
    assert "gitlab-master.nvidia.com:5005/nvis-ova" not in template_ci
    assert "apt-get install -y --no-install-recommends git jq shellcheck" in template_ci
    assert "uv python install 3.14.3 --managed-python" in template_ci
    assert "UV_PROJECT_ENVIRONMENT=/opt/ci" not in template_ci
    assert "validation (9 gates)" in configuration
    assert not (_VALIDATION_JOBS & configuration.keys())
