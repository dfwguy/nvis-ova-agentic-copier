"""Pytest configuration and fixtures for the copier template tests."""

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def template_dir() -> Path:
    """Provide the template directory path."""
    return Path(__file__).parent.parent / "template"


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Provide the project root directory path."""
    return Path(__file__).parent.parent
