"""Pytest configuration and fixtures for the copier template tests."""

import shutil
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
    shutil.copytree(project_root / "template", staged / "template")
    shutil.copy(project_root / "copier.yaml", staged / "copier.yaml")
    return staged


@pytest.fixture(scope="session")
def generated_project(
    tmp_path_factory: pytest.TempPathFactory, template_source: Path
) -> Path:
    """Generate a project from the template once per test session."""
    output_dir = tmp_path_factory.mktemp("generated") / "my_project"
    copier.run_copy(
        str(template_source),
        str(output_dir),
        data={
            "project_name": "My Test Project",
            "project_slug": "my_test_project",
            "description": "A test project",
        },
        defaults=True,
        overwrite=True,
        unsafe=True,
        skip_tasks=True,
    )
    return output_dir
