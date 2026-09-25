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

import os
import shutil
import subprocess
from pathlib import Path

import copier
import pytest


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
) -> Path:
    """Render project files without running setup tasks."""
    output_dir = tmp_path_factory.mktemp("generated") / "my_project"
    copier.run_copy(
        str(template_source),
        output_dir,
        data={
            "project_name": "My Test Project",
            "project_slug": "my_test_project",
            "description": "A test project",
        },
        defaults=True,
        overwrite=True,
        quiet=True,
        skip_tasks=True,
    )
    return output_dir


@pytest.fixture(scope="session")
def trusted_generated_project(
    tmp_path_factory: pytest.TempPathFactory,
    template_source: Path,
    project_root: Path,
) -> Path:
    """Generate a project through Copier's real trusted-task orchestration."""
    output_dir = tmp_path_factory.mktemp("trusted_generated") / "my_project"
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
