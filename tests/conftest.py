# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pytest configuration and fixtures for the copier template tests."""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register opt-in collection sharding for concurrent CI pytest runs."""
    group = parser.getgroup("ci sharding")
    group.addoption(
        "--ci-shard-count",
        type=int,
        default=None,
        help="Number of deterministic CI collection shards.",
    )
    group.addoption(
        "--ci-shard-index",
        type=int,
        default=None,
        help="One-based deterministic CI collection shard index.",
    )


def ci_shard_for_nodeid(nodeid: str, shard_count: int) -> int:
    """Return the one-based stable CI shard for a pytest node ID."""
    digest = hashlib.sha256(nodeid.encode()).digest()
    return int.from_bytes(digest, byteorder="big") % shard_count + 1


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Deselect collected tests outside the requested deterministic CI shard."""
    shard_count = config.getoption("ci_shard_count")
    shard_index = config.getoption("ci_shard_index")
    if shard_count is None and shard_index is None:
        return
    if shard_count is None or shard_index is None:
        message = "--ci-shard-count and --ci-shard-index must be supplied together"
        raise pytest.UsageError(message)
    if shard_count < 1:
        raise pytest.UsageError("--ci-shard-count must be at least 1")
    if not 1 <= shard_index <= shard_count:
        message = f"--ci-shard-index must be between 1 and {shard_count}"
        raise pytest.UsageError(message)
    selected = [
        item
        for item in items
        if ci_shard_for_nodeid(item.nodeid, shard_count) == shard_index
    ]
    selected_nodeids = {item.nodeid for item in selected}
    deselected = [item for item in items if item.nodeid not in selected_nodeids]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.fixture(scope="session")
def template_dir() -> Path:
    """Provide the template directory path."""
    return Path(__file__).parent.parent / "template"


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Provide the project root directory path."""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def template_source(
    tmp_path_factory: pytest.TempPathFactory, project_root: Path
) -> Path:
    """Stage template/ and copier.yaml in a non-git tmp dir.

    Pointing copier at the project root triggers its git-clone-via-`git add -A`
    path, which enumerates the entire work-tree and fails on any non-regular
    files there (e.g. sandbox bind-mounts masking home dotfiles). Staging
    decouples the test from the surrounding repo state.
    """
    staged = tmp_path_factory.mktemp("template_source")
    shutil.copytree(project_root / "template", staged / "template", symlinks=True)
    shutil.copy(project_root / "copier.yaml", staged / "copier.yaml")
    return staged


@pytest.fixture(scope="session")
def generated_project(
    tmp_path_factory: pytest.TempPathFactory,
    template_source: Path,
    project_root: Path,
) -> Path:
    """Generate a project through Copier's real trusted-task orchestration."""
    output_dir = tmp_path_factory.mktemp("generated") / "my_project"
    commands = tmp_path_factory.mktemp("copier_commands")
    for command in ("glab", "uv"):
        _write_noop_command(commands / command)
    uv_command = shutil.which("uv")
    assert uv_command is not None
    environment = os.environ | {"PATH": f"{commands}:{os.environ['PATH']}"}

    subprocess.run(
        [
            uv_command,
            "run",
            "copier",
            "copy",
            "--trust",
            "--force",
            "--quiet",
            "--data",
            "project_name=My Test Project",
            "--data",
            "project_slug=my_test_project",
            "--data",
            "description=A test project",
            str(template_source),
            str(output_dir),
        ],
        check=True,
        capture_output=True,
        cwd=project_root,
        env=environment,
        text=True,
    )
    return output_dir


def _write_noop_command(path: Path) -> None:
    """Create a successful command stub for an environment-dependent task."""
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
