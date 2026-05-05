"""Tests for the copier template."""

from pathlib import Path

import pytest


@pytest.fixture
def generated_project(tmp_path: Path, template_dir: Path) -> Path:
    """Generate a project from the template and return its path."""
    import copier

    output_dir = tmp_path / "my_project"
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
    )
    return output_dir


def test_generated_project_structure(generated_project: Path) -> None:
    """Test that the generated project has the expected top-level structure."""
    assert (generated_project / "pyproject.toml").exists()
    assert (generated_project / "README.md").exists()
    assert (generated_project / "CLAUDE.md").exists()
    assert (generated_project / ".gitignore").exists()
    assert (generated_project / ".pre-commit-config.yaml").exists()
    assert (generated_project / ".gitlab-ci.yml").exists()


def test_generated_package_directory(generated_project: Path) -> None:
    """Test that the package directory is created with the correct name."""
    assert (generated_project / "my_test_project").is_dir()
    assert (generated_project / "my_test_project" / "__init__.py").exists()


def test_generated_tests_directory(generated_project: Path) -> None:
    """Test that the tests directory is generated correctly."""
    assert (generated_project / "tests").is_dir()
    assert (generated_project / "tests" / "__init__.py").exists()
    assert (generated_project / "tests" / "conftest.py").exists()


def test_pyproject_toml_rendered(generated_project: Path) -> None:
    """Test that pyproject.toml was rendered with the correct variable values."""
    content = (generated_project / "pyproject.toml").read_text()
    assert 'name = "my_test_project"' in content
    assert 'description = "A test project"' in content
    assert "{{ project_slug }}" not in content
    assert "{{ description }}" not in content


def test_readme_rendered(generated_project: Path) -> None:
    """Test that README.md was rendered with the correct variable values."""
    content = (generated_project / "README.md").read_text()
    assert "My Test Project" in content
    assert "A test project" in content
    assert "{{ project_name }}" not in content
    assert "{{ description }}" not in content


def test_claude_project_md_rendered(generated_project: Path) -> None:
    """Test that .claude/project.md was rendered with the correct variable values."""
    project_md = generated_project / ".claude" / "project.md"
    assert project_md.exists()
    content = project_md.read_text()
    assert "My Test Project" in content
    assert "A test project" in content
    assert "{{ project_name }}" not in content


def test_no_jinja_extension_in_output(generated_project: Path) -> None:
    """Test that no .jinja files remain in the generated project."""
    jinja_files = list(generated_project.rglob("*.jinja"))
    assert jinja_files == [], f"Unexpected .jinja files: {jinja_files}"


def test_claude_agents_present(generated_project: Path) -> None:
    """Test that Claude agent definitions are included in the generated project."""
    agents_dir = generated_project / ".claude" / "agents"
    assert agents_dir.is_dir()
    agent_files = list(agents_dir.glob("*.md"))
    assert len(agent_files) > 0


def test_claude_commands_present(generated_project: Path) -> None:
    """Test that Claude command definitions are included in the generated project."""
    commands_dir = generated_project / ".claude" / "commands"
    assert commands_dir.is_dir()
    command_files = list(commands_dir.glob("*.md"))
    assert len(command_files) > 0


def test_yamllint_helper_present(generated_project: Path) -> None:
    """Test that the yamllint helper script is included."""
    helper = generated_project / "tests" / "helpers" / "yamllint_to_json.py"
    assert helper.exists()


def test_template_pathlib_usage(template_dir: Path) -> None:
    """Test that template Python files use pathlib instead of os.path."""
    yamllint_helper = template_dir / "tests" / "helpers" / "yamllint_to_json.py"
    content = yamllint_helper.read_text()
    assert "import os" not in content


def test_template_python_type_annotations(template_dir: Path) -> None:
    """Test that template Python files use type annotations."""
    yamllint_helper = template_dir / "tests" / "helpers" / "yamllint_to_json.py"
    content = yamllint_helper.read_text()
    assert "->" in content


def test_copier_yaml_exists(project_root: Path) -> None:
    """Test that copier.yaml exists and defines the expected variables."""
    copier_yaml = project_root / "copier.yaml"
    assert copier_yaml.exists()
    content = copier_yaml.read_text()
    assert "project_name" in content
    assert "project_slug" in content
    assert "description" in content
    assert "_subdirectory" in content
    assert "_tasks" in content
