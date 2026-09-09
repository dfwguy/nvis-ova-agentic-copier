"""Contract tests for the project-only validation image."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_IMAGE_INPUTS = ("Dockerfile", "pyproject.toml", "uv.lock")


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
