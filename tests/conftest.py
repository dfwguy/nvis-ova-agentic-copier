"""Pytest configuration and fixtures for the copier template tests."""

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
def generated_project(
    tmp_path_factory: pytest.TempPathFactory, template_dir: Path
) -> Path:
    """Generate a project from the template once per test session."""
    output_dir = tmp_path_factory.mktemp("generated") / "my_project"
    copier.run_copy(
        str(template_dir.parent),
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
