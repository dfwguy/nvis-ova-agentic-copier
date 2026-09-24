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
import tomllib
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
_IMAGE_TARGETS = ("tooling", "pytest-py311", "pytest-py314")
_IMAGE_VARIABLES = {
    "tooling": "CI_TOOLING_IMAGE",
    "pytest-py311": "CI_PY311_IMAGE",
    "pytest-py314": "CI_PY314_IMAGE",
}
_TOOLING_JOBS = _VALIDATION_JOBS - {
    "coverage-py311",
    "coverage-py314",
    "pytest-py311",
    "pytest-py314",
    "type-check",
    "license-check",
}
_KINGFISHER_CACHE = "/home/validation/.cache/kingfisher/rule-cache"
_KINGFISHER_COMPILE = (
    "/opt/ci/py311/bin/kingfisher rules compile-cache --no-update-check"
)
_KINGFISHER_SCAN = (
    "uv run --no-sync kingfisher scan . --git-history none --no-validate "
    "--no-update-check --exclude .git --exclude .cache --exclude .venv "
    "--exclude .tox --exclude .pytest_cache --exclude .ruff_cache"
)
_ROOT_UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.9.30-python3.11-bookworm-slim@"
    "sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36"
)
_CANDIDATE_BUILD_OPTIONS = (
    "buildctl-daemonless.sh build "
    '--import-cache "type=registry,ref=$CI_REGISTRY_IMAGE/'
    'validation-$CI_IMAGE_TARGET-cache" '
    '--export-cache "type=registry,ref=$CI_REGISTRY_IMAGE/'
    'validation-$CI_IMAGE_TARGET-cache,mode=max" '
    '--output "type=image,name=$CI_REGISTRY_IMAGE/'
    'validation-$CI_IMAGE_TARGET:$CI_COMMIT_SHA-$CI_PIPELINE_ID,push=true"'
)


def _docker_target_body(dockerfile: str, target: str) -> str:
    """Return one named Docker target through the next stage boundary."""
    match = re.search(rf"^FROM .+ AS {re.escape(target)}$", dockerfile, re.MULTILINE)
    assert match is not None
    next_stage = re.search(r"^FROM ", dockerfile[match.end() :], re.MULTILINE)
    end = (
        match.end() + next_stage.start() if next_stage is not None else len(dockerfile)
    )
    return dockerfile[match.start() : end]


def _uv_sync_invocations(docker_target: str) -> tuple[tuple[str, ...], ...]:
    """Return every shell-tokenized uv sync command from one Docker target."""
    normalized = re.sub(r"\\\n\s*", " ", docker_target)
    lexer = shlex.shlex(normalized, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = tuple(lexer)
    starts = tuple(
        index
        for index, token in enumerate(tokens[:-1])
        if token == "uv" and tokens[index + 1] == "sync"
    )
    boundaries = {"&&", "||", ";", "&", "|"}
    return tuple(
        tokens[
            start : next(
                (
                    index
                    for index in range(start + 2, len(tokens))
                    if tokens[index] in boundaries
                ),
                len(tokens),
            )
        ]
        for start in starts
    )


def _option_values(tokens: tuple[str, ...], option: str) -> tuple[str, ...]:
    """Return values supplied through either supported long-option spelling."""
    values: list[str] = []
    for index, token in enumerate(tokens):
        if token == option:
            values.append(tokens[index + 1])
        elif token.startswith(f"{option}="):
            values.append(token.partition("=")[2])
    return tuple(values)


def _candidate_build_contract_is_exact(build_command: str, target: str) -> bool:
    """Return whether one target has exact cache and pushed-output options."""
    normalized = re.sub(r"\\\n\s*", " ", build_command)
    tokens = tuple(
        token.replace("$CI_IMAGE_TARGET", target) for token in shlex.split(normalized)
    )
    cache = f"type=registry,ref=$CI_REGISTRY_IMAGE/validation-{target}-cache"
    output = (
        f"type=image,name=$CI_REGISTRY_IMAGE/validation-{target}:"
        "$CI_COMMIT_SHA-$CI_PIPELINE_ID,push=true"
    )
    return (
        _option_values(tokens, "--import-cache") == (cache,)
        and _option_values(tokens, "--export-cache") == (f"{cache},mode=max",)
        and _option_values(tokens, "--output") == (output,)
    )


def _final_env(docker_target: str) -> dict[str, str]:
    """Return the assignments from a target's final ENV instruction."""
    normalized = re.sub(r"\\\n\s*", " ", docker_target)
    env_instructions = re.findall(r"^ENV\s+(.+)$", normalized, flags=re.MULTILINE)
    return dict(
        token.split("=", maxsplit=1) for token in shlex.split(env_instructions[-1])
    )


def _network_ca_contract_is_exact(
    docker_target: str,
    *,
    tooling: bool,
) -> bool:
    """Return whether installs use the optional CA as the effective non-root user."""
    normalized = re.sub(r"\\\n\s*", " ", docker_target)
    normalized = re.sub(r"\s+", " ", normalized)
    mount = (
        "--mount=type=secret,id=network-ca,target=/run/secrets/network-ca,"
        "uid=10001,required=false"
    )
    condition = "if [ -f /run/secrets/network-ca ]; then"
    concatenate = (
        "cat /etc/ssl/certs/ca-certificates.crt /run/secrets/network-ca "
        "> /tmp/network-ca-bundle;"
    )
    tls_environment = "export UV_NATIVE_TLS=true SSL_CERT_FILE=/tmp/network-ca-bundle;"
    cleanup = "rm -f /tmp/network-ca-bundle"
    required = (mount, condition, concatenate, tls_environment, cleanup)
    if any(value not in normalized for value in required):
        return False
    sync = normalized.index("uv sync")
    if not (
        normalized.index(mount)
        < normalized.index(condition)
        < normalized.index(concatenate)
        < normalized.index(tls_environment)
        < sync
        < normalized.index(cleanup)
    ):
        return False
    users = tuple(re.findall(r"^USER\s+(\S+)", docker_target, flags=re.MULTILINE))
    if tooling:
        return users == ("root", "10001:10001") and docker_target.rindex(
            "USER 10001:10001"
        ) < docker_target.index("uv sync")
    return not users


def _has_exact_pytest_sync_scope(docker_target: str, *, typed: bool = False) -> bool:
    """Return whether one sync installs defaults and its required groups."""
    invocations = _uv_sync_invocations(docker_target)
    if len(invocations) != 1:
        return False
    sync_tokens = invocations[0]
    groups = _option_values(sync_tokens, "--group")
    only_groups = _option_values(sync_tokens, "--only-group")
    return (
        sync_tokens[:2] == ("uv", "sync")
        and "--locked" in sync_tokens
        and "--no-dev" in sync_tokens
        and groups == (("test", "typing") if typed else ("test",))
        and "--no-install-project" in sync_tokens
        and not only_groups
        and "--dev" not in sync_tokens
        and "--only-dev" not in sync_tokens
        and "--all-groups" not in sync_tokens
    )


def _has_exact_tooling_sync_scope(docker_target: str) -> bool:
    """Return whether the tooling image installs only its locked CI group."""
    invocations = _uv_sync_invocations(docker_target)
    if len(invocations) != 1:
        return False
    sync_tokens = invocations[0]
    return (
        sync_tokens[:2] == ("uv", "sync")
        and "--locked" in sync_tokens
        and _option_values(sync_tokens, "--only-group") == ("tooling",)
        and not _option_values(sync_tokens, "--group")
        and "--no-install-project" in sync_tokens
        and "--dev" not in sync_tokens
        and "--only-dev" not in sync_tokens
        and "--all-groups" not in sync_tokens
    )


def test_ci_tooling_group_excludes_test_dependencies_and_keeps_local_dev_tools(
    project_root: Path,
) -> None:
    """The CI image installs tools while local dev still includes tests and hooks."""
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    groups = project["dependency-groups"]
    tooling = {
        re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0]
        for requirement in groups["tooling"]
    }
    dev = groups["dev"]

    assert tooling == {"kingfisher-bin", "ruff", "textstat", "yamllint"}
    assert set(groups["typing"]) == {"ty>=0.0.1a11", "textstat>=0.7.4"}
    assert {"include-group": "test"} in dev
    assert {"include-group": "tooling"} in dev
    assert {"include-group": "typing"} in dev
    assert {
        re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0]
        for requirement in dev
        if isinstance(requirement, str)
    } == {"pre-commit", "tox", "tox-uv"}


def test_ci_tooling_group_is_locked_with_exact_members(project_root: Path) -> None:
    """The lock records exact tooling and typing members in both group tables."""
    lock = tomllib.loads((project_root / "uv.lock").read_text())
    package = next(
        item for item in lock["package"] if item["name"] == "ova-agentic-flow-copier"
    )
    for group_name, expected in (
        ("tooling", {"kingfisher-bin", "ruff", "textstat", "yamllint"}),
        ("typing", {"ty", "textstat"}),
    ):
        for table in (package["dev-dependencies"], package["metadata"]["requires-dev"]):
            assert {dependency["name"] for dependency in table[group_name]} == expected


@pytest.mark.parametrize(
    "docker_target",
    (
        (
            "RUN uv sync --locked --no-dev --group test --no-install-project "
            "&& uv sync --locked --no-dev --group test --no-install-project"
        ),
        (
            "RUN uv sync --locked --no-dev --group test --group lint "
            "--no-install-project"
        ),
    ),
)
def test_pytest_sync_scope_rejects_extra_invocations_and_groups(
    docker_target: str,
) -> None:
    """Scope validation detects appended syncs and extra non-dev groups."""
    assert not _has_exact_pytest_sync_scope(docker_target)


@pytest.mark.parametrize(
    ("old", "new"),
    (
        (
            (
                '--import-cache "type=registry,ref=$CI_REGISTRY_IMAGE/'
                'validation-$CI_IMAGE_TARGET-cache" '
            ),
            "",
        ),
        (
            (
                '--export-cache "type=registry,ref=$CI_REGISTRY_IMAGE/'
                'validation-$CI_IMAGE_TARGET-cache,mode=max" '
            ),
            "",
        ),
        (",mode=max", ""),
        (
            "$CI_COMMIT_SHA-$CI_PIPELINE_ID",
            "$CI_PIPELINE_ID-$CI_COMMIT_SHA",
        ),
        ("-$CI_PIPELINE_ID", ""),
        (",push=true", ",push=false"),
        (",push=true", ""),
    ),
)
def test_candidate_build_contract_rejects_incomplete_cache_and_output_options(
    old: str,
    new: str,
) -> None:
    """Cache, revision suffix, and push mutations invalidate the build contract."""
    assert old in _CANDIDATE_BUILD_OPTIONS
    assert not _candidate_build_contract_is_exact(
        _CANDIDATE_BUILD_OPTIONS.replace(old, new),
        "tooling",
    )


def test_validation_image_targets_install_only_their_locked_dependency_scope(
    project_root: Path,
) -> None:
    """Each final image contains only the locked tools needed by its job lane."""
    dockerfile = (project_root / "Dockerfile").read_text()

    from_instructions = re.findall(
        r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$",
        dockerfile,
        flags=re.MULTILINE,
    )
    assert from_instructions == [
        (_ROOT_UV_IMAGE, "nonroot-base"),
        ("nonroot-base", "tooling"),
        ("nonroot-base", "pytest-py311"),
        ("nonroot-base", "pytest-py314"),
    ]
    targets = {
        target: _docker_target_body(dockerfile, target) for target in _IMAGE_TARGETS
    }
    shared_base = _docker_target_body(dockerfile, "nonroot-base")
    tooling = targets["tooling"]
    py311 = targets["pytest-py311"]
    py314 = targets["pytest-py314"]

    for package, version in {
        "git": "1:2.39.5-0+deb12u3",
        "jq": "1.6-2.1+deb12u2",
    }.items():
        assert f"{package}={version}" in shared_base
        assert f"{package}={version}" not in py311
        assert f"{package}={version}" not in py314
    assert "shellcheck=0.9.0-1" in tooling
    assert "shellcheck=0.9.0-1" not in shared_base
    assert "shellcheck=0.9.0-1" not in py311
    assert "shellcheck=0.9.0-1" not in py314
    assert _has_exact_tooling_sync_scope(tooling)
    for pytest_target, typed in ((py311, True), (py314, False)):
        sync_invocations = _uv_sync_invocations(pytest_target)
        assert len(sync_invocations) == 1
        sync_tokens = sync_invocations[0]
        groups = _option_values(sync_tokens, "--group")
        only_groups = _option_values(sync_tokens, "--only-group")

        assert _has_exact_pytest_sync_scope(pytest_target, typed=typed)
        assert sync_tokens[:2] == ("uv", "sync")
        assert "--locked" in sync_tokens
        assert "--no-dev" in sync_tokens
        assert groups == (("test", "typing") if typed else ("test",))
        assert "--no-install-project" in sync_tokens
        assert "dev" not in groups
        assert "dev" not in only_groups
        assert not only_groups
        assert "--dev" not in sync_tokens
        assert "--only-dev" not in sync_tokens
        assert "--all-groups" not in sync_tokens
    assert "--python 3.11" in py311
    assert "--python 3.14.3" in py314
    assert "kingfisher" in tooling
    assert "kingfisher" not in py311
    assert "kingfisher" not in py314
    assert "pytest --version" not in tooling
    assert "pytest --version" in py311
    assert "pytest --version" in py314
    assert "--mount=type=secret,id=network-ca" in dockerfile
    assert "COPY --chown=10001:10001 pyproject.toml uv.lock ./" in dockerfile
    assert "COPY ." not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "WORKDIR /workspace" in dockerfile


@pytest.mark.parametrize(
    ("target", "tooling", "expected_environment"),
    (
        (
            "tooling",
            True,
            {
                "PATH": "/opt/ci/py311/bin:${PATH}",
                "UV_PROJECT_ENVIRONMENT": "/opt/ci/py311",
            },
        ),
        (
            "pytest-py311",
            False,
            {
                "PATH": "/opt/ci/py311/bin:${PATH}",
                "UV_PROJECT_ENVIRONMENT": "/opt/ci/py311",
                "CI_PY311_ENVIRONMENT": "/opt/ci/py311",
            },
        ),
        (
            "pytest-py314",
            False,
            {
                "PATH": "/opt/ci/py314/bin:${PATH}",
                "UV_PROJECT_ENVIRONMENT": "/opt/ci/py314",
                "CI_PY314_ENVIRONMENT": "/opt/ci/py314",
            },
        ),
    ),
)
def test_validation_targets_use_optional_ca_nonroot_installs_and_exact_environments(
    project_root: Path,
    target: str,
    *,
    tooling: bool,
    expected_environment: dict[str, str],
) -> None:
    """Each lane installs with optional TLS as non-root and exports only its paths."""
    dockerfile = (project_root / "Dockerfile").read_text()
    docker_target = _docker_target_body(dockerfile, target)
    shared_base = _docker_target_body(dockerfile, "nonroot-base")

    assert tuple(re.findall(r"^USER\s+(\S+)", shared_base, flags=re.MULTILINE)) == (
        "10001:10001",
    )
    assert _network_ca_contract_is_exact(docker_target, tooling=tooling)
    assert _final_env(docker_target) == expected_environment


@pytest.mark.parametrize(
    ("old", "new"),
    (
        ("id=network-ca", "id=other-ca"),
        ("/etc/ssl/certs/ca-certificates.crt", "/tmp/other-certificates"),
        ("UV_NATIVE_TLS=true", "UV_NATIVE_TLS=false"),
        ("SSL_CERT_FILE=/tmp/network-ca-bundle", "SSL_CERT_FILE=/tmp/other"),
        ("rm -f /tmp/network-ca-bundle", "true"),
        (
            "FROM nonroot-base AS pytest-py311",
            "FROM nonroot-base AS pytest-py311\nUSER root",
        ),
    ),
)
def test_pytest_target_tls_contract_rejects_missing_setup_and_root_escalation(
    project_root: Path,
    old: str,
    new: str,
) -> None:
    """TLS omissions and pytest-stage privilege escalation invalidate the target."""
    dockerfile = (project_root / "Dockerfile").read_text()
    pytest_target = _docker_target_body(dockerfile, "pytest-py311")

    assert old in pytest_target
    assert not _network_ca_contract_is_exact(
        pytest_target.replace(old, new),
        tooling=False,
    )


def test_validation_image_compiles_kingfisher_rules_once_after_locked_installs(
    project_root: Path,
) -> None:
    """The non-root image build prepares rules before validating its tools."""
    dockerfile = (project_root / "Dockerfile").read_text()
    tooling = _docker_target_body(dockerfile, "tooling")

    assert dockerfile.count(_KINGFISHER_COMPILE) == 1
    locked_sync = tooling.index("uv sync --locked --only-group tooling")
    compile_rules = tooling.index(_KINGFISHER_COMPILE)
    verify_tools = tooling.index("kingfisher --version")
    verify_git = tooling.index("git --version")
    verify_jq = tooling.index("jq --version")
    final_workdir = tooling.rindex("WORKDIR /workspace")
    users_before_compile = re.findall(
        r"^USER\s+(\S+)", tooling[:compile_rules], flags=re.MULTILINE
    )

    assert users_before_compile[-1] == "10001:10001"
    assert locked_sync < compile_rules
    assert compile_rules < verify_tools < final_workdir
    assert compile_rules < verify_git < final_workdir
    assert compile_rules < verify_jq < final_workdir


def test_tooling_image_verifies_global_uv_from_base_image(project_root: Path) -> None:
    """The tooling image verifies the base image's uv, outside its synced venv."""
    dockerfile = (project_root / "Dockerfile").read_text()
    tooling = _docker_target_body(dockerfile, "tooling")
    lines = tooling.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUN /opt/ci/py311/bin/python --version")
    )
    verification_lines = []
    for line in lines[start:]:
        verification_lines.append(line)
        if not line.endswith("\\"):
            break
    verification_run = "\n".join(verification_lines)
    lexer = shlex.shlex(
        re.sub(r"\\\n\s*", " ", verification_run),
        posix=True,
        punctuation_chars=";&|",
    )
    lexer.whitespace_split = True
    lexer.commenters = "#"
    segments: list[tuple[str, ...]] = []
    segment: list[str] = []
    for token in lexer:
        if token == "&&":
            segments.append(tuple(segment))
            segment = []
        else:
            segment.append(token)
    segments.append(tuple(segment))

    assert ("uv", "--version") in segments
    assert "/opt/ci/py311/bin/uv --version" not in verification_run


def test_validation_image_retains_the_stable_kingfisher_rule_cache(
    project_root: Path,
) -> None:
    """Only uv's download cache is removed after the rule cache is compiled."""
    dockerfile = (project_root / "Dockerfile").read_text()
    tooling = _docker_target_body(dockerfile, "tooling")
    normalized = re.sub(r"\\\n\s*", " ", tooling)
    non_root_build = tooling[tooling.index("USER 10001:10001") :]
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


def test_three_candidate_images_build_and_push_the_exact_source_targets(
    project_root: Path,
) -> None:
    """Every admitted revision publishes exactly one image per validation lane."""
    ci_text = (project_root / ".gitlab-ci.yml").read_text()
    configuration = yaml.safe_load(ci_text.replace("!reference ", ""))
    builder = configuration[".ci_image_builder"]

    assert builder["stage"] == "ci-image"
    assert configuration["stages"].index("ci-image") < configuration["stages"].index(
        "validate"
    )
    assert re.fullmatch(
        r"docker\.io/moby/buildkit:v0\.33\.0-rootless@sha256:[0-9a-f]{64}",
        builder["image"]["name"],
    )
    assert builder["image"]["entrypoint"] == [""]
    assert builder["variables"] == {
        "BUILDKITD_FLAGS": "--oci-worker-no-process-sandbox",
    }
    assert builder["before_script"][:2] == [
        "mkdir -p /tmp/ci-image-context",
        "cp Dockerfile pyproject.toml uv.lock /tmp/ci-image-context/",
    ]
    assert builder["before_script"][2] == 'mkdir -p "$HOME/.docker"'
    assert len(builder["before_script"]) == 4
    auth_script = builder["before_script"][3]
    assert '> "$HOME/.docker/config.json"' in auth_script
    shared_setup = "\n".join(map(str, builder["before_script"][2:]))
    assert "$CI_REGISTRY_USER" in shared_setup
    assert "$CI_REGISTRY_PASSWORD" in shared_setup
    assert "echo" not in shared_setup
    assert "set -x" not in shared_setup
    build_command, digest_command = builder["script"]
    candidate_names = {name for name in configuration if name.startswith("ci-image:")}
    assert candidate_names == {f"ci-image:{target}" for target in _IMAGE_TARGETS}
    for target in _IMAGE_TARGETS:
        candidate = configuration[f"ci-image:{target}"]
        assert candidate["extends"] == ".ci_image_builder"
        assert candidate["tags"] == ["size/medium"]
        assert "rules" not in candidate
        assert (builder | candidate)["stage"] == "ci-image"
        assert candidate["before_script"] == [[".ci_image_builder", "before_script"]]
        assert candidate["variables"] == {
            "CI_IMAGE_TARGET": target,
            "CI_IMAGE_VARIABLE": _IMAGE_VARIABLES[target],
        }
        assert _candidate_build_contract_is_exact(build_command, target)
        build_tokens = tuple(
            token.replace("$CI_IMAGE_TARGET", target)
            for token in shlex.split(re.sub(r"\\\n\s*", " ", build_command))
        )
        cache = f"type=registry,ref=$CI_REGISTRY_IMAGE/validation-{target}-cache"
        assert _option_values(build_tokens, "--import-cache") == (cache,)
        assert _option_values(build_tokens, "--export-cache") == (f"{cache},mode=max",)
        assert _option_values(build_tokens, "--output") == (
            (
                f"type=image,name=$CI_REGISTRY_IMAGE/validation-{target}:"
                "$CI_COMMIT_SHA-$CI_PIPELINE_ID,push=true"
            ),
        )
        for expected in (
            "--local context=/tmp/ci-image-context",
            "--local dockerfile=/tmp/ci-image-context",
            '--secret "id=network-ca,src=$CI_SERVER_TLS_CA_FILE"',
            '--opt "target=$CI_IMAGE_TARGET"',
            '--metadata-file "build-metadata-$CI_IMAGE_TARGET.json"',
        ):
            assert expected in build_command
        assert '"containerimage.digest"' in digest_command
        assert "^sha256:[0-9a-f]{64}$" in digest_command
        assert "$CI_IMAGE_VARIABLE" in digest_command
    assert builder["artifacts"] == {
        "reports": {"dotenv": "ci-image-$CI_IMAGE_TARGET.env"},
        "paths": [
            "build-metadata-$CI_IMAGE_TARGET.json",
            "ci-image-$CI_IMAGE_TARGET.env",
        ],
        "when": "always",
    }


@pytest.mark.parametrize(
    ("metadata", "expected_status", "expected_content"),
    (
        (
            '{"containerimage.digest":"sha256:' + "a" * 64 + '"}',
            0,
            "CI_PY311_IMAGE=registry.example/project/validation-pytest-py311@"
            "sha256:" + "a" * 64 + "\n",
        ),
        ("not valid build metadata", 2, None),
    ),
)
def test_candidate_digest_script_fails_closed_and_writes_exact_evidence(
    project_root: Path,
    tmp_path: Path,
    metadata: str,
    expected_status: int,
    expected_content: str | None,
) -> None:
    """The shared evidence script accepts one digest and rejects malformed metadata."""
    configuration = yaml.safe_load(
        (project_root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )
    script = configuration[".ci_image_builder"]["script"][1]
    metadata_path = tmp_path / "build-metadata-pytest-py311.json"
    evidence_path = tmp_path / "ci-image-pytest-py311.env"
    metadata_path.write_text(metadata)
    evidence_path.write_text("STALE_IMAGE=must-not-survive\n")
    environment = os.environ.copy() | {
        "CI_IMAGE_TARGET": "pytest-py311",
        "CI_IMAGE_VARIABLE": "CI_PY311_IMAGE",
        "CI_REGISTRY_IMAGE": "registry.example/project",
    }

    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert result.returncode == expected_status
    if expected_content is None:
        assert not evidence_path.exists()
    else:
        assert evidence_path.read_text() == expected_content


def test_project_image_contract_does_not_leak_into_generated_ci(
    project_root: Path,
) -> None:
    """Generated projects remain independent of this project registry."""
    root_ci = (project_root / ".gitlab-ci.yml").read_text()
    template_ci = (project_root / "template" / ".gitlab-ci.yml").read_text()

    for target in _IMAGE_TARGETS:
        assert f"ci-image:{target}" in root_ci
    assert "ci-image:validate" not in root_ci
    assert "ci-image:publish" not in root_ci
    assert "CI_REGISTRY_IMAGE" not in template_ci
    assert "moby/buildkit" not in template_ci


def test_root_validation_uses_its_lane_image_without_runtime_bootstrap(
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

    tooling_image = (
        "$CI_REGISTRY_IMAGE/validation-tooling:$CI_COMMIT_SHA-$CI_PIPELINE_ID"
    )
    assert base["image"]["name"] == tooling_image
    tooling_need = {"job": "ci-image:tooling", "artifacts": False}
    assert base["needs"] == [tooling_need]
    assert base["before_script"] == [
        'cd "$CI_PROJECT_DIR"',
        'git config --global --add safe.directory "$CI_PROJECT_DIR"',
    ]
    for runtime_setup in ("apt-get", "uv python install", "uv sync"):
        assert runtime_setup not in before_script
        assert runtime_setup not in validation_contract
    assert configuration[".pytest"]["extends"] == ".base_uv"
    assert configuration[".pytest_coverage"]["extends"] == ".base_uv"
    required_tooling_commands = {
        "ruff-check": "uv run --no-sync ruff check",
        "ruff-format": "uv run --no-sync ruff format --check",
        "yamllint": "uv run --no-sync yamllint",
        "kingfisher": _KINGFISHER_SCAN,
        "readability-check": (
            'PYTHONPATH="$CI_PROJECT_DIR/src" uv run --no-sync '
            "python -m agent_work.readability "
            "--root . --scope all"
        ),
        "shellcheck": "shellcheck",
    }
    for job_name in _TOOLING_JOBS:
        effective_job = base | configuration[job_name]
        assert effective_job["needs"] == [tooling_need]
        assert effective_job["image"]["name"] == tooling_image
        assert required_tooling_commands[job_name] in "\n".join(effective_job["script"])
    for job_name, expected_script in (
        ("type-check", "uv run --no-sync ty check"),
        ("license-check", "uv run --no-sync python scripts/check_licenses.py --root ."),
    ):
        job = configuration[job_name]
        assert job["image"]["name"] == (
            "$CI_REGISTRY_IMAGE/validation-pytest-py311:$CI_COMMIT_SHA-$CI_PIPELINE_ID"
        )
        assert job["needs"] == [{"job": "ci-image:pytest-py311", "artifacts": False}]
        assert job["script"] == [expected_script]
    for environment in ("py311", "py314"):
        target = f"pytest-{environment}"
        image = f"$CI_REGISTRY_IMAGE/validation-{target}:$CI_COMMIT_SHA-$CI_PIPELINE_ID"
        candidate_need = {"job": f"ci-image:{target}", "artifacts": False}
        pytest_job = configuration[f"pytest-{environment}"]
        coverage_job = configuration[f"coverage-{environment}"]
        assert pytest_job["image"]["name"] == image
        assert pytest_job["needs"] == [candidate_need]
        assert coverage_job["image"]["name"] == image
        assert coverage_job["needs"] == [
            candidate_need,
            {"job": f"pytest-{environment}", "artifacts": True},
        ]
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
        ("py311", "1", "17", "/opt/ci/py311", 0, None),
        ("py311", "7", "17", "/opt/ci/py311", 0, None),
        ("py311", "10", "17", "/opt/ci/py311", 0, None),
        ("py311", "16", "17", "/opt/ci/py311", 0, None),
        ("py311", "17", "17", "/opt/ci/py311", 0, None),
        ("py314", "1", "6", "/opt/ci/py314", 0, None),
        ("py314", "6", "6", "/opt/ci/py314", 0, None),
        ("unknown", "1", "17", None, 2, "Unknown test environment"),
        ("py311", None, "17", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", None, None, 2, "CI_NODE_TOTAL"),
        ("py311", "one", "17", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", "six", None, 2, "CI_NODE_TOTAL"),
        ("py311", "0", "17", None, 2, "CI_NODE_INDEX"),
        ("py311", "18", "17", None, 2, "CI_NODE_INDEX"),
        ("py311", "1", "6", None, 2, "CI_NODE_TOTAL"),
        ("py314", "7", "6", None, 2, "CI_NODE_INDEX"),
        ("py314", "1", "17", None, 2, "CI_NODE_TOTAL"),
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
            f"--serial-runtime-shard-count={node_total} "
            "--cov=src/agent_work "
            f"--junitxml=test-results-{test_environment}-shard-{node_index}.xml "
            f"-o junit_suite_name={test_environment} "
            f"-o cache_dir=.cache/pytest/{test_environment}/shard-{node_index}"
        )


def test_root_pytest_jobs_use_environment_specific_native_shards_with_unique_artifacts(
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
    for environment, shard_count in (("py311", 17), ("py314", 6)):
        job = configuration[f"pytest-{environment}"]
        assert job["extends"] == ".pytest"
        assert job["parallel"] == shard_count
        assert job["variables"] == {"TEST_ENVIRONMENT": environment}


@pytest.mark.parametrize("environment", ("py311", "py314"))
def test_root_coverage_job_combines_matching_native_shard_artifacts(
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
    shard_count = 17 if environment == "py311" else 6
    expected_raw_files = [
        f".coverage.{environment}-shard-{index}" for index in range(1, shard_count + 1)
    ]

    assert coverage_job["extends"] == ".pytest_coverage"
    assert coverage_job["variables"] == {"TEST_ENVIRONMENT": environment}
    assert coverage_job["needs"] == [
        {"job": f"ci-image:pytest-{environment}", "artifacts": False},
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

    assert "$CI_REGISTRY_IMAGE/validation-" not in template_ci
    assert "gitlab-master.nvidia.com:5005/nvis-ova" not in template_ci
    assert "apt-get install -y --no-install-recommends git jq shellcheck" in template_ci
    assert "uv python install 3.14.3 --managed-python" in template_ci
    assert "UV_PROJECT_ENVIRONMENT=/opt/ci" not in template_ci
    assert "validation (10 gates)" in configuration
    assert not (_VALIDATION_JOBS & configuration.keys())
