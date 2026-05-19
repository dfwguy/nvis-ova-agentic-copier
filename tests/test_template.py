"""Tests for the copier template."""

import json
from pathlib import Path


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
    assert (generated_project / "tests" / "helpers" / "__init__.py").exists()


def test_tests_init_rendered(generated_project: Path) -> None:
    """Test that tests/__init__.py has no unrendered template variables."""
    content = (generated_project / "tests" / "__init__.py").read_text()
    assert "{{" not in content
    assert "}}" not in content
    assert "My Test Project" in content


def test_tests_helpers_init_rendered(generated_project: Path) -> None:
    """Test that tests/helpers/__init__.py has no unrendered template variables."""
    content = (generated_project / "tests" / "helpers" / "__init__.py").read_text()
    assert "{{" not in content
    assert "}}" not in content
    assert "My Test Project" in content


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


def test_claude_git_flow_present(generated_project: Path) -> None:
    """Test that the git-flow lifecycle doc is included so the orchestrator loads it."""
    git_flow = generated_project / ".claude" / "git-flow.md"
    assert git_flow.exists()
    content = git_flow.read_text()
    assert "glab mr create" in content
    assert "git checkout -b" in content
    assert "git checkout main" in content


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


def test_copier_answers_file_generated(generated_project: Path) -> None:
    """Test that .copier-answers.yml is generated so `copier update`/`recopy` work."""
    answers_file = generated_project / ".copier-answers.yml"
    assert answers_file.exists()
    content = answers_file.read_text()
    assert "_src_path:" in content
    assert "project_name: My Test Project" in content
    assert "project_slug: my_test_project" in content


def test_setup_repo_command_present(generated_project: Path) -> None:
    """Test that the /setup-repo command is included so new projects can lock down GitLab."""
    setup_repo = generated_project / ".claude" / "commands" / "setup-repo.md"
    assert setup_repo.exists()
    content = setup_repo.read_text()
    assert "protected_branches" in content
    assert "approval_rules" in content
    assert "/approvals" in content, (
        "self-approval block must target /projects/<id>/approvals, not the bare project endpoint"
    )


def test_contributing_md_absent_in_generated_project(generated_project: Path) -> None:
    """Test that CONTRIBUTING.md is NOT generated — its absence is the gate trigger in CLAUDE.md."""
    assert not (generated_project / "CONTRIBUTING.md").exists()


def test_generated_claude_md_has_lockdown_gate(generated_project: Path) -> None:
    """Test that the generated CLAUDE.md tells Claude to refuse work when CONTRIBUTING.md is missing."""
    content = (generated_project / "CLAUDE.md").read_text()
    assert "Repo lock-down gate" in content
    assert "CONTRIBUTING.md" in content
    assert "/setup-repo" in content
    assert "glab mr approve" in content


def test_generated_settings_denies_mr_approval(generated_project: Path) -> None:
    """Test that the generated .claude/settings.json denies AI-driven MR approval."""
    settings = json.loads((generated_project / ".claude" / "settings.json").read_text())
    deny = settings.get("permissions", {}).get("deny", [])
    assert any("glab mr approve" in entry for entry in deny)
    assert any("glab mr revoke" in entry for entry in deny)


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
