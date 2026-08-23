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


def test_generated_pre_commit_uses_only_local_hooks(generated_project: Path) -> None:
    """Test that generated projects do not fetch remote hook repositories."""
    config = (generated_project / ".pre-commit-config.yaml").read_text()
    pyproject = (generated_project / "pyproject.toml").read_text()

    assert "repo: local" in config
    assert "repo: https://" not in config
    assert "pre-commit>=4.6.2" in pyproject
    assert "trailing-whitespace" in config
    assert "check-toml" in config
    assert "Kingfisher secret scan" in config
    assert "kingfisher-bin" in pyproject


def test_generated_pre_commit_lints_shell_scripts(generated_project: Path) -> None:
    """Test that generated projects syntax-check and lint shell scripts."""
    config = (generated_project / ".pre-commit-config.yaml").read_text()

    assert "id: bash-syntax" in config
    assert "entry: bash -n" in config
    assert "id: shellcheck" in config
    assert "entry: shellcheck" in config


def test_generated_ci_runs_kingfisher(generated_project: Path) -> None:
    """Test that generated projects scan committed content for secrets in CI."""
    ci_config = (generated_project / ".gitlab-ci.yml").read_text()

    assert "kingfisher:" in ci_config
    assert "--git-history none" in ci_config
    assert "--no-validate" in ci_config
    assert "--exclude .git" in ci_config


def test_generated_ci_runs_shellcheck(generated_project: Path) -> None:
    """Test that generated projects lint Bash and shell-script shebangs in CI."""
    ci_config = (generated_project / ".gitlab-ci.yml").read_text()

    assert "shellcheck:" in ci_config
    assert "apt-get install -y --no-install-recommends shellcheck" in ci_config


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


def test_package_init_rendered(generated_project: Path) -> None:
    """Test that the package __init__.py has a rendered module docstring."""
    content = (generated_project / "my_test_project" / "__init__.py").read_text()
    assert "{{" not in content
    assert "}}" not in content
    assert content.strip() == '"""My Test Project."""'


def test_generated_starter_test_exists(generated_project: Path) -> None:
    """Test that a starter test ships so pytest collects >=1 item (no exit 5)."""
    assert (generated_project / "tests" / "test_my_test_project.py").exists()


def test_starter_test_rendered(generated_project: Path) -> None:
    """Test that the starter test renders and imports the generated package."""
    content = (generated_project / "tests" / "test_my_test_project.py").read_text()
    assert "{{" not in content
    assert "}}" not in content
    assert "import my_test_project" in content


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


def test_diataxis_documentation_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the Diataxis documentation gates."""
    docs_process = generated_project / "docs" / "diataxis.md"
    writer = generated_project / ".claude" / "agents" / "diataxis-writer.md"
    judge = generated_project / ".claude" / "agents" / "diataxis-judge.md"
    command = generated_project / ".claude" / "commands" / "diataxis-writer.md"

    assert docs_process.exists()
    assert "Tutorials" in docs_process.read_text()
    assert "How-to guides" in docs_process.read_text()
    assert "Reference" in docs_process.read_text()
    assert "Explanation" in docs_process.read_text()
    assert writer.exists()
    assert judge.exists()
    assert command.exists()
    assert "/tdd-red" in command.read_text()
    assert "/tdd-judge" in judge.read_text()
    assert (
        "README.md"
        in (generated_project / ".claude" / "agents" / "tdd-document.md").read_text()
    )
    assert "docs/" in writer.read_text()


def test_cli_syntax_judge_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the CLI syntax design gate."""
    agent = generated_project / ".claude" / "agents" / "cli-syntax-judge.md"
    command = generated_project / ".claude" / "commands" / "cli-syntax-judge.md"
    workflow = (generated_project / "CLAUDE.md").read_text()

    assert agent.exists()
    assert command.exists()
    assert "https://clig.dev" in agent.read_text()
    assert "--json" in agent.read_text()
    assert "--apply" in agent.read_text()
    assert "/cli-syntax-judge" in workflow
    assert "/architect" in workflow


def test_claude_git_flow_present(generated_project: Path) -> None:
    """Test that the git-flow lifecycle doc is included so the orchestrator loads it."""
    git_flow = generated_project / ".claude" / "git-flow.md"
    assert git_flow.exists()
    content = git_flow.read_text()
    assert "projects/$GITLAB_PROJECT_ID/merge_requests" in content
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


def test_setup_repo_interviews_self_approval(generated_project: Path) -> None:
    """Test that /setup-repo interviews the user before blocking self-approval (single-user opt-out)."""
    content = (generated_project / ".claude" / "commands" / "setup-repo.md").read_text()
    assert "AskUserQuestion" in content, (
        "setup-repo must interview the user about self-approval"
    )
    assert "Block self-approval" in content
    assert "Allow self-approval" in content
    assert "single-user" in content


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


def test_generated_claude_workflow_supports_bash(generated_project: Path) -> None:
    """Test that generated agent guidance selects Bash-specific validation."""
    workflow = (generated_project / "CLAUDE.md").read_text()
    bash_conventions = generated_project / ".claude" / "lang" / "bash.md"
    judge = generated_project / ".claude" / "agents" / "tdd-judge.md"
    finalizer = generated_project / ".claude" / "agents" / "tdd-finalizer.md"

    assert bash_conventions.exists()
    assert "ShellCheck" in bash_conventions.read_text()
    assert "supports Python and Bash projects" in workflow
    assert "bash -n" in judge.read_text()
    assert "shellcheck" in finalizer.read_text()


def test_generated_settings_denies_mr_approval(generated_project: Path) -> None:
    """Test that the generated .claude/settings.json denies AI-driven MR approval."""
    settings = json.loads((generated_project / ".claude" / "settings.json").read_text())
    deny = settings.get("permissions", {}).get("deny", [])
    assert any("glab mr approve" in entry for entry in deny)
    assert any("glab mr revoke" in entry for entry in deny)


def test_agents_md_present(generated_project: Path) -> None:
    """Test that AGENTS.md provides a native Codex workflow."""
    agents_md = generated_project / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert "CLAUDE.md" in content
    assert ".claude/lang/python.md" in content
    assert "gpt-5.6-sol" in content
    assert "gpt-5.6-terra" in content
    assert "gpt-5.6-luna" in content
    assert "GITLAB_PROJECT_ID" in content


def test_claude_lang_python_present(generated_project: Path) -> None:
    """Test that .claude/lang/python.md is generated with Python conventions."""
    python_md = generated_project / ".claude" / "lang" / "python.md"
    assert python_md.exists()
    content = python_md.read_text()
    assert "uv run pytest" in content
    assert "Dependency Inversion" in content
    assert "pytest.mark.parametrize" in content


def test_generated_claude_md_has_language_index(generated_project: Path) -> None:
    """Test that the generated CLAUDE.md references the language conventions file."""
    content = (generated_project / "CLAUDE.md").read_text()
    assert ".claude/lang/python.md" in content
    assert "Language-Specific Conventions" in content


def test_claude_lang_python_testing_present(generated_project: Path) -> None:
    """Test that the detailed testing reference is generated as a canonical language file."""
    testing_md = generated_project / ".claude" / "lang" / "python-testing.md"
    assert testing_md.exists()
    content = testing_md.read_text()
    assert "pytest-subprocess" in content
    assert "requests-mock" in content


def test_cursor_testing_rule_references_canonical_file(generated_project: Path) -> None:
    """Test the Cursor test rule is a thin @-import of the canonical file, not a duplicate."""
    rule = generated_project / "tests" / ".cursor" / "rules" / "testing.mdc"
    assert rule.exists()
    content = rule.read_text()
    assert "@.claude/lang/python-testing.md" in content
    # The old per-topic duplicated rules must be gone.
    rules_dir = generated_project / "tests" / ".cursor" / "rules"
    assert not (rules_dir / "subprocess.mdc").exists()
    assert not (rules_dir / "modify_tests.mdc").exists()


def test_claude_guidance_present(generated_project: Path) -> None:
    """Test that language-agnostic guidance files are generated in .claude/guidance/."""
    guidance = generated_project / ".claude" / "guidance"
    assert (guidance / "tdd.md").exists()
    assert (guidance / "markdown.md").exists()
    assert "Red" in (guidance / "tdd.md").read_text()


def test_general_cursor_rules_reference_canonical_files(
    generated_project: Path,
) -> None:
    """Test the general Cursor rules are thin @-imports of the canonical guidance files."""
    rules = generated_project / ".cursor" / "rules"
    assert "@.claude/guidance/tdd.md" in (rules / "tdd.mdc").read_text()
    assert "@.claude/guidance/markdown.md" in (rules / "markdown.mdc").read_text()


def test_generated_claude_md_has_general_guidance_index(
    generated_project: Path,
) -> None:
    """Test that the generated CLAUDE.md references the general guidance layer."""
    content = (generated_project / "CLAUDE.md").read_text()
    assert "General Guidance" in content
    assert ".claude/guidance/tdd.md" in content


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
