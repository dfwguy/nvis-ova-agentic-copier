"""Tests for the copier template."""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker


def test_generated_project_structure(generated_project: Path) -> None:
    """Test that the generated project has the expected top-level structure."""
    assert (generated_project / "pyproject.toml").exists()
    assert (generated_project / "README.md").exists()
    assert (generated_project / "CLAUDE.md").exists()
    assert (generated_project / ".gitignore").exists()
    assert (generated_project / ".pre-commit-config.yaml").exists()
    assert (generated_project / ".gitlab-ci.yml").exists()


def test_generated_project_includes_structured_gitlab_issue_templates(
    generated_project: Path,
) -> None:
    """Test that generated projects track work with GitLab issue templates."""
    templates = generated_project / ".gitlab" / "issue_templates"

    bug = (templates / "Bug.md").read_text()
    feature = (templates / "Feature.md").read_text()

    for template in (bug, feature):
        assert "## Priority" in template
        assert "P0" in template
        assert "P3" in template
        assert "## Severity" in template
        assert "S1" in template
        assert "S4" in template

    assert "## Steps to reproduce" in bug
    assert "## Expected behavior" in bug
    assert "## Actual behavior" in bug
    assert "## Problem" in feature
    assert "## Acceptance criteria" in feature


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


def test_root_ci_shellcheck_excludes_generated_environments(project_root: Path) -> None:
    """Test that the root CI ShellCheck job skips generated tool environments."""
    ci_config = (project_root / ".gitlab-ci.yml").read_text()

    assert "-not -path './.venv/*'" in ci_config
    assert "-not -path './.tox/*'" in ci_config


def test_root_tox_generates_enforced_coverage_artifacts(project_root: Path) -> None:
    """Test that root tox produces the coverage artifacts uploaded by CI."""
    config = tomllib.loads((project_root / "pyproject.toml").read_text())
    expected_arguments = {
        "--cov-fail-under=85",
        "--cov-report=xml:coverage.xml",
        "--cov-report=html",
        "--junitxml=test-results.xml",
    }

    for environment in ("py311", "py314"):
        command = config["tool"]["tox"]["env"][environment]["commands"][0]
        assert expected_arguments.issubset(command)


def _unlabeled_markdown_fences(content: str) -> list[str]:
    """Return unlabeled Markdown fence openings in content."""
    fence = re.compile(r"^(`{3,}|~{3,})([^`~\s]+)?\s*$")
    open_fence: str | None = None
    unlabeled_fences: list[str] = []

    for line in content.splitlines():
        match = fence.fullmatch(line)
        if match is None:
            continue
        marker, language = match.groups()
        if marker == open_fence and language is None:
            open_fence = None
        elif open_fence is None:
            if language is None:
                unlabeled_fences.append(marker)
            open_fence = marker

    return unlabeled_fences


def test_markdown_fences_have_a_language(project_root: Path) -> None:
    """Test that maintained Markdown fences declare a language."""
    ignored_paths = {".git", ".venv", ".tox", ".pytest_cache", ".ruff_cache"}

    markdown_paths = (*project_root.rglob("*.md"), *project_root.rglob("*.md.jinja"))
    for markdown_path in markdown_paths:
        if ignored_paths.intersection(markdown_path.relative_to(project_root).parts):
            continue
        assert not _unlabeled_markdown_fences(markdown_path.read_text()), markdown_path


def test_markdown_fence_guard_rejects_unlabeled_tilde_fences() -> None:
    """Test that the Markdown guard handles all standard fence marker types."""
    content = "~~~\nexample\n~~~\n\n```text\nexample\n```\n"

    assert _unlabeled_markdown_fences(content) == ["~~~"]


def test_generated_package_directory(generated_project: Path) -> None:
    """Test that Python packages use the standard isolated ``src/`` layout."""
    package = generated_project / "src" / "my_test_project"

    assert package.is_dir()
    assert (package / "__init__.py").exists()
    assert not (generated_project / "my_test_project").exists()


def test_generated_tests_directory(generated_project: Path) -> None:
    """Test that the tests directory is generated correctly."""
    assert (generated_project / "tests").is_dir()
    assert (generated_project / "tests" / "__init__.py").exists()
    assert (generated_project / "tests" / "conftest.py").exists()
    assert (generated_project / "tests" / "helpers" / "__init__.py").exists()


def test_package_init_rendered(generated_project: Path) -> None:
    """Test that the package __init__.py has a rendered module docstring."""
    content = (
        generated_project / "src" / "my_test_project" / "__init__.py"
    ).read_text()
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


def test_ruff_uses_version_016_defaults(
    generated_project: Path, project_root: Path
) -> None:
    """Test that the template relies on Ruff 0.16's expanded default rules."""
    root_config = tomllib.loads((project_root / "pyproject.toml").read_text())
    generated_config = tomllib.loads(
        (generated_project / "pyproject.toml").read_text(),
    )

    for config in (root_config, generated_config):
        lint_config = config["tool"]["ruff"]["lint"]
        assert "select" not in lint_config
        assert lint_config["ignore"] == [
            "E501",
            "G004",
            "D203",
            "D213",
            "COM812",
            "S603",
        ]
        assert "ruff>=0.16.4" in config["dependency-groups"]["dev"]


def test_generated_hatch_configuration_builds_both_src_packages(
    generated_project: Path,
) -> None:
    """Test that Hatch builds project and bounded-runner packages from ``src/``."""
    content = (generated_project / "pyproject.toml").read_text()

    assert "[tool.hatch.build.targets.wheel]" in content
    assert 'packages = ["src/my_test_project", "src/agent_work"]' in content
    assert "force-include" not in content
    assert "[project.scripts]" in content


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


def test_generated_project_documents_filesystem_structure_defaults(
    generated_project: Path,
) -> None:
    """Test that a generated project records its required layout and deviations."""
    content = (generated_project / ".claude" / "project.md").read_text()

    assert "src/{{ project_slug }}/" not in content
    assert "src/my_test_project/" in content
    assert "Filesystem structure deviations: None" in content
    for field in ("**Language:**", "**Rule:**", "**Actual paths:**", "**Rationale:**"):
        assert field in content


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
    assert "include a Mermaid flowchart" in docs_process.read_text()
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


def test_generated_remediation_workflow_and_documentation_index_rule(
    generated_project: Path,
) -> None:
    """Test that projects can remediate themselves and keep documentation navigable."""
    remediation_agent = generated_project / ".claude" / "agents" / "remediate.md"
    remediation_command = generated_project / ".claude" / "commands" / "remediate.md"
    documentation_process = generated_project / "docs" / "diataxis.md"
    documentation_index = generated_project / "docs" / "README.md"

    assert remediation_agent.exists()
    assert remediation_command.exists()
    assert "Make this repo meet our standards" in remediation_agent.read_text()
    assert "baseline" in remediation_agent.read_text()
    assert "Documentation index" in documentation_process.read_text()
    assert "Diátaxis page" in documentation_process.read_text()
    assert documentation_index.exists()
    assert "diataxis.md" in documentation_index.read_text()


def test_documentation_index_links_every_diataxis_page(project_root: Path) -> None:
    """Test that the source documentation index exposes every Diataxis page."""
    index = (project_root / "docs" / "README.md").read_text()

    for page in (
        "tutorials/create-your-first-project.md",
        "how-to/create-a-project-non-interactively.md",
        "how-to/customize-a-generated-project.md",
        "how-to/update-a-generated-project.md",
        "how-to/validate-template-changes.md",
        "reference/template-contract.md",
        "explanation/template-and-generated-project.md",
        "explanation/why-diataxis.md",
        "explanation/coding-standards.md",
        "diataxis.md",
    ):
        assert page in index


def test_explanations_and_readme_use_mermaid_for_relationship_diagrams(
    project_root: Path,
) -> None:
    """Test that documentation uses Mermaid for relationship and workflow diagrams."""
    readme = (project_root / "README.md").read_text()
    explanation = (
        project_root / "docs" / "explanation" / "template-and-generated-project.md"
    ).read_text()

    assert "```mermaid\nflowchart" in readme
    assert 'filesystem["/filesystem-structure-judge"]' in readme
    assert "```mermaid\nflowchart" in explanation


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


def test_claude_git_flow_verifies_git_and_gitlab_access(
    generated_project: Path,
) -> None:
    """Test that the generated workflow verifies both remote access paths."""
    content = (generated_project / ".claude" / "git-flow.md").read_text()

    assert "## Git and GitLab access preflight" in content
    assert "git pull --ff-only" in content
    assert 'glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"' in content
    assert "Do not change a working remote or credential configuration" in content


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


def test_setup_repo_publishes_its_bootstrap_change(generated_project: Path) -> None:
    """Test that lock-down setup cannot strand CONTRIBUTING.md on local main."""
    content = (generated_project / ".claude" / "commands" / "setup-repo.md").read_text()
    mr_lookup = (
        'glab api --hostname "$GITLAB_HOST" '
        '"projects/$GITLAB_PROJECT_ID/merge_requests?state=opened&'
        'source_branch=claude/setup-repo"'
    )
    mr_create = (
        'glab api --hostname "$GITLAB_HOST" -X POST '
        '"projects/$GITLAB_PROJECT_ID/merge_requests"'
    )
    required_handoff = (
        'setup_branch="claude/setup-repo"',
        "clean default branch",
        "create or reuse",
        "git add CONTRIBUTING.md",
        "git commit",
        "git push --set-upstream origin HEAD",
        "projects/$GITLAB_PROJECT_ID/merge_requests",
        "source_branch",
        "branch tip",
        "awaiting_human",
        "without waiting for merge",
        "Create only when the lookup has no open MR",
        "refs/heads/$setup_branch",
        "refs/remotes/origin/$setup_branch",
        "Historical merged or closed setup MRs",
        "settings-only result",
        "release the clean checkout",
    )

    for requirement in required_handoff:
        assert requirement in content
    assert content.index('setup_branch="claude/setup-repo"') < content.index(
        "Write `CONTRIBUTING.md`"
    )
    assert content.index("git add CONTRIBUTING.md") < content.index(
        "git push --set-upstream origin HEAD"
    )
    assert content.index("git push --set-upstream origin HEAD") < content.index(
        mr_lookup
    )
    assert content.index(mr_lookup) < content.index(mr_create)
    assert content.index("refs/heads/$setup_branch") < content.index(
        "Protect the default branch"
    )
    assert content.index("refs/remotes/origin/$setup_branch") < content.index(
        "Protect the default branch"
    )
    assert content.index("git push --set-upstream origin HEAD") < content.index(
        "git checkout <default_branch>"
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
    assert "sole bounded mutation" in content
    assert "publishes its own merge request" in content


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


def test_generated_language_guidance_requires_a_separate_code_directory(
    generated_project: Path,
) -> None:
    """Test that generated Python and Bash guidance sets layout boundaries."""
    python_guidance = (generated_project / ".claude" / "lang" / "python.md").read_text()
    bash_guidance = (generated_project / ".claude" / "lang" / "bash.md").read_text()

    assert "src/" in python_guidance
    assert "src/<package>/" in python_guidance
    assert "tests/" in python_guidance
    assert "outside the production" in python_guidance.lower()
    assert "[project.scripts]" in python_guidance
    assert "exactly one user-facing" in bash_guidance
    assert "bin/<tool>" in bash_guidance
    assert "implementation" in bash_guidance.lower()
    assert "`src/`" in bash_guidance
    assert "`lib/`" in bash_guidance
    assert "tests outside production" in bash_guidance.lower()


def test_source_and_template_language_guidance_define_the_same_structure_gate(
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that Copier source and generated guidance both specify the gate."""
    for guidance_dir in (
        project_root / ".claude" / "lang",
        template_dir / ".claude" / "lang",
    ):
        python_guidance = (guidance_dir / "python.md").read_text()
        bash_guidance = (guidance_dir / "bash.md").read_text()

        assert "src/" in python_guidance
        assert "src/<package>/" in python_guidance
        assert "tests/" in python_guidance
        assert "outside the production" in python_guidance.lower()
        assert "[project.scripts]" in python_guidance
        assert "exactly one user-facing" in bash_guidance
        assert "bin/<tool>" in bash_guidance
        assert "implementation" in bash_guidance.lower()
        assert "`src/`" in bash_guidance
        assert "`lib/`" in bash_guidance
        assert "tests outside production" in bash_guidance.lower()


def test_filesystem_structure_judge_is_generated_and_wired_into_final_gates(
    generated_project: Path,
) -> None:
    """Test that generated projects review layout before finalization."""
    agent = generated_project / ".claude" / "agents" / "filesystem-structure-judge.md"
    command = (
        generated_project / ".claude" / "commands" / "filesystem-structure-judge.md"
    )
    workflow = (generated_project / "CLAUDE.md").read_text()
    finalizer = (
        generated_project / ".claude" / "agents" / "tdd-finalizer.md"
    ).read_text()

    assert agent.exists()
    assert command.exists()
    assert "src/" in agent.read_text()
    assert "bin/" in agent.read_text()
    assert "Filesystem structure deviations" in agent.read_text()
    assert "subagent_type: filesystem-structure-judge" in command.read_text()
    assert "/filesystem-structure-judge" in workflow
    assert "`filesystem-structure-judge` approves" in finalizer


def test_source_and_template_workflows_wire_the_filesystem_structure_judge(
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that the source workflow and Copier output retain the same gate."""
    for root in (project_root, template_dir):
        workflow = (root / "CLAUDE.md").read_text()
        finalizer = (root / ".claude" / "agents" / "tdd-finalizer.md").read_text()
        agent = root / ".claude" / "agents" / "filesystem-structure-judge.md"
        command = root / ".claude" / "commands" / "filesystem-structure-judge.md"

        assert agent.exists()
        assert command.exists()
        assert "subagent_type: filesystem-structure-judge" in command.read_text()
        assert "/filesystem-structure-judge" in workflow
        assert "`filesystem-structure-judge` approves" in finalizer


def test_filesystem_structure_judge_defines_concrete_layout_and_deviation_rules(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that every shipped filesystem judge enforces the full contract."""
    judge_paths = (
        generated_project / ".claude" / "agents" / "filesystem-structure-judge.md",
        project_root / ".claude" / "agents" / "filesystem-structure-judge.md",
        template_dir / ".claude" / "agents" / "filesystem-structure-judge.md",
    )

    for judge_path in judge_paths:
        content = judge_path.read_text()

        assert "src/<package>" in content
        assert "tests" in content.lower()
        assert "[project.scripts]" in content
        assert "exactly one user-facing" in content
        assert "bin/<tool>" in content
        assert "`src/`" in content
        assert "`lib/`" in content
        assert "language" in content.lower()
        assert "rule" in content.lower()
        assert "actual paths" in content.lower()
        assert "rationale" in content.lower()


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


def test_copier_skip_paths_preserve_project_specific_metadata(
    project_root: Path,
) -> None:
    """Test that Copier preserves source and project-specific metadata."""
    configuration = yaml.safe_load((project_root / "copier.yaml").read_text())

    assert "src/{{ project_slug }}/__init__.py" in configuration["_skip_if_exists"]
    assert "{{ project_slug }}/__init__.py" not in configuration["_skip_if_exists"]
    assert "pyproject.toml" in configuration["_skip_if_exists"]
    assert ".claude/project.md" in configuration["_skip_if_exists"]


def test_copier_update_warns_legacy_flat_packages_to_stop_and_migrate(
    project_root: Path,
) -> None:
    """Test that Copier does not silently update a legacy flat package layout."""
    configuration = yaml.safe_load((project_root / "copier.yaml").read_text())

    message = configuration["message_before_update"].lower()
    assert "legacy flat" in message
    assert "<package>/" in message
    assert "src/<package>/" in message
    assert "stop" in message
    assert "migrate" in message


def test_filesystem_structure_judge_rejects_split_legacy_python_layouts(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that the layout gate blocks a flat and src package split."""
    judge_paths = (
        generated_project / ".claude" / "agents" / "filesystem-structure-judge.md",
        project_root / ".claude" / "agents" / "filesystem-structure-judge.md",
        template_dir / ".claude" / "agents" / "filesystem-structure-judge.md",
    )

    for judge_path in judge_paths:
        content = judge_path.read_text().lower()

        assert "<package>/" in content
        assert "src/<package>/" in content
        assert "split" in content
        assert "overall: reject" in content
        assert "migrate" in content


def test_update_how_to_gives_safe_legacy_flat_layout_migration_steps(
    project_root: Path,
) -> None:
    """Test that old-layout projects get backup, move, and verification guidance."""
    content = (
        (project_root / "docs" / "how-to" / "update-a-generated-project.md")
        .read_text()
        .lower()
    )

    assert "legacy flat" in content
    assert "backup" in content
    assert "clean working tree" in content
    assert "move" in content
    assert "<package>/" in content
    assert "src/<package>/" in content
    assert "uv run pytest" in content
    assert "uv run tox" in content


def test_root_and_template_project_notes_declare_structure_compliance(
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that every project records deviations from the standard layout."""
    root_notes = (project_root / ".claude" / "project.md").read_text()
    template_notes = (template_dir / ".claude" / "project.md.jinja").read_text()

    assert "Filesystem structure deviations" in root_notes
    for field in ("**Language:**", "**Rule:**", "**Actual paths:**", "**Rationale:**"):
        assert field in root_notes
    assert "src/{{ project_slug }}/" in template_notes
    assert "Filesystem structure deviations: None" in template_notes
    for field in ("**Language:**", "**Rule:**", "**Actual paths:**", "**Rationale:**"):
        assert field in template_notes


def test_copier_preflight_requires_only_git_uv_and_glab_before_git_init(
    project_root: Path,
) -> None:
    """Test that only commands needed by Copier gate initial setup."""
    content = (project_root / "copier.yaml").read_text()
    tasks = content.split("_tasks:\n", maxsplit=1)[1].split(
        "\n\nproject_name:", maxsplit=1
    )[0]
    preflight, git_init, _ = tasks.partition("  - git init")

    assert git_init, "git init must remain a Copier task"
    assert preflight, "a preflight task must run before git init"
    for prerequisite in ("command -v git", "command -v uv", "command -v glab"):
        assert prerequisite in preflight
    for non_prerequisite in (
        "command -v bash",
        "command -v shellcheck",
        "git config --global --get",
        "git var GIT_AUTHOR_IDENT",
        "glab auth status",
        "glab api",
    ):
        assert non_prerequisite not in preflight


def test_copier_checks_scoped_git_identity_and_gitlab_access_after_git_init(
    project_root: Path,
) -> None:
    """Test that post-init diagnostics support direnv-scoped identity and tokens."""
    tasks = _copier_tasks(project_root)
    git_init_index = tasks.index("git init")
    post_init_tasks = tasks[git_init_index + 1 :]

    assert any("git var GIT_AUTHOR_IDENT" in task for task in post_init_tasks)
    assert any(
        'gitlab_host="${GITLAB_HOST:-gitlab-master.nvidia.com}"' in task
        for task in post_init_tasks
    )
    assert any(
        'glab api --hostname "$gitlab_host" user' in task for task in post_init_tasks
    )
    assert all("git config --global --get" not in task for task in tasks)
    assert all("glab auth status" not in task for task in tasks)


def _copier_tasks(project_root: Path) -> list[str]:
    """Load Copier tasks from the template configuration."""
    configuration = yaml.safe_load((project_root / "copier.yaml").read_text())
    return configuration["_tasks"]


def _write_command(path: Path, body: str) -> None:
    """Create an executable POSIX-shell command stub."""
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run_copier_tasks(tasks: list[str], environment: dict[str, str]) -> list[str]:
    """Run Copier tasks in order, stopping after the first failed task."""
    results = []
    for task in tasks:
        result = subprocess.run(
            ["/bin/sh", "-c", task],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
        results.append(result.stdout + result.stderr)
        if result.returncode != 0:
            break
    return results


def test_copier_preflight_stops_setup_when_required_glab_is_missing(
    project_root: Path, tmp_path: Path
) -> None:
    """Test that a missing required command prevents every setup task."""
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "setup-ran"
    _write_command(
        commands / "git",
        """case "$1" in
  init) printf 'git init\\n' >> "$MARKER" ;;
esac""",
    )
    _write_command(commands / "uv", 'printf \'uv %s\\n\' "$1" >> "$MARKER"')
    environment = os.environ | {"MARKER": str(marker), "PATH": str(commands)}

    results = _run_copier_tasks(_copier_tasks(project_root), environment)

    assert len(results) == 1
    assert "Missing required command: glab" in results[0]
    assert not marker.exists()


def test_copier_preflight_allows_setup_on_ready_workstation(
    project_root: Path, tmp_path: Path
) -> None:
    """Test that a passing preflight permits setup tasks in their declared order."""
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "setup-ran"
    _write_command(
        commands / "git",
        """case "$1" in
  init) printf 'git init\\n' >> "$MARKER" ;;
  var) printf 'Template User <template@example.com> 1 +0000\\n' ;;
esac""",
    )
    _write_command(commands / "uv", 'printf \'uv %s\\n\' "$1" >> "$MARKER"')
    _write_command(
        commands / "glab",
        """case "$1:$2:$3" in
  api:--hostname:gitlab-master.nvidia.com) [ "$4" = user ] ;;
esac""",
    )
    environment = os.environ | {"MARKER": str(marker), "PATH": str(commands)}

    results = _run_copier_tasks(_copier_tasks(project_root), environment)

    assert len(results) == len(_copier_tasks(project_root))
    assert marker.read_text().splitlines() == ["git init", "uv sync", "uv run"]


def test_copier_post_init_readiness_diagnostics_do_not_stop_setup(
    project_root: Path, tmp_path: Path
) -> None:
    """Test that scoped identity and GitLab-access diagnostics are non-blocking."""
    commands = tmp_path / "bin"
    commands.mkdir()
    marker = tmp_path / "setup-ran"
    _write_command(
        commands / "git",
        """case "$1" in
  init) printf 'git init\\n' >> "$MARKER" ;;
  var) exit 1 ;;
esac""",
    )
    _write_command(commands / "uv", 'printf \'uv %s\\n\' "$1" >> "$MARKER"')
    _write_command(
        commands / "glab",
        """case "$1" in
  api) exit 1 ;;
esac""",
    )
    environment = os.environ | {"MARKER": str(marker), "PATH": str(commands)}

    results = _run_copier_tasks(_copier_tasks(project_root), environment)

    assert len(results) == len(_copier_tasks(project_root))
    assert marker.read_text().splitlines() == ["git init", "uv sync", "uv run"]


def _workflow_paths(root: Path) -> tuple[Path, ...]:
    """Return the maintained workflow instructions for a project root."""
    candidates = (
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "README.md",
        *(root / ".claude").rglob("*.md"),
        *(root / ".agents").rglob("*.md"),
    )
    return tuple(path for path in candidates if path.exists())


def test_root_and_generated_workflows_do_not_defer_work_to_markdown_backlogs(
    project_root: Path, generated_project: Path
) -> None:
    """Test that workflows use GitLab issues instead of TODO/FIXME files."""
    forbidden_reference = re.compile(r"(?i)(?:todo|fixme)\.md")

    for root in (project_root, project_root / "template", generated_project):
        for backlog_name in ("TODO.md", "FIXME.md", "todo.md", "fixme.md"):
            assert not (root / backlog_name).exists()
        for workflow_path in _workflow_paths(root):
            content = workflow_path.read_text()
            assert forbidden_reference.search(content) is None, workflow_path


def test_repository_changes_require_publication_and_treat_failures_as_blockers(
    generated_project: Path, project_root: Path
) -> None:
    """Test that no workflow permits local-only completion of repository work."""
    forbidden_completion_policies = (
        "local-change",
        "local-only work",
        "defer publishing",
        "skip step 3 *for now*",
    )
    required_policy = (
        "every repository change must publish",
        "a publication failure is a blocker, not completion",
    )

    for root in (project_root, project_root / "template", generated_project):
        for workflow_path in _workflow_paths(root):
            content = workflow_path.read_text().lower()
            assert not any(
                policy in content for policy in forbidden_completion_policies
            ), workflow_path

        for relative_path in ("AGENTS.md", "CLAUDE.md", ".claude/git-flow.md"):
            content = (root / relative_path).read_text().lower()
            for policy in required_policy:
                assert policy in content, relative_path


@pytest.mark.parametrize(
    "root",
    (".", "template"),
)
def test_architect_records_deferred_scope_with_gitlab_feature_issues(
    project_root: Path, root: str
) -> None:
    """Test that deferred design scope is captured as GitLab Feature work."""
    command = project_root / root / ".claude" / "commands" / "architect.md"
    content = command.read_text()

    for requirement in (
        "GitLab Feature issue",
        "Problem",
        "Proposed outcome",
        "Acceptance criteria",
        "Priority",
        "Severity",
        "Scope and dependencies",
        "numeric project ID",
        "unfiled item",
        "never claim it was persisted",
    ):
        assert requirement in content


@pytest.mark.parametrize(
    "command_name",
    ("judge-quality.md", "judge-test.md"),
)
def test_review_commands_file_minor_findings_as_gitlab_bug_issues(
    project_root: Path, command_name: str
) -> None:
    """Test that approving reviews hand off Bug issues in a machine-readable form."""
    for root in (project_root, project_root / "template"):
        content = (root / ".claude" / "commands" / command_name).read_text()

        for requirement in (
            "Bug template",
            "GitLab issue",
            "S4",
            "type::quality",
            "numeric project ID",
            "unfiled",
            "Never claim it was persisted",
            "Markdown backlog file as a fallback",
            "Created <N> GitLab issues",
            "No new issues",
        ):
            assert requirement in content


def test_git_flow_allows_unrelated_open_mrs_without_removing_safety_gates(
    project_root: Path,
) -> None:
    """Test that work is blocked only by conflicts, not unrelated open MRs."""
    forbidden_gates = (
        "any open mr in the repo",
        "mrs are outstanding",
        "stay on the branch until",
        "remain on the feature branch",
        "workflow is paused pending",
        "wait for the user to come back",
        "do not return to `main` as soon",
    )

    for root in (project_root, project_root / "template"):
        content = (root / ".claude" / "git-flow.md").read_text().lower()

        assert not any(gate in content for gate in forbidden_gates)
        assert "unrelated open merge requests" in content
        assert "non-blocking" in content
        assert "same branch" in content
        assert "dependency" in content
        assert "resource conflict" in content
        assert "clean working tree" in content
        assert "git pull --ff-only" in content
        assert "latest pipeline" in content
        assert "no direct-to-`main` path" in content
        assert "ai must never approve or merge" in content
        assert "report the result as `awaiting_human` and end the active run" in content
        assert "release the active checkout" in content
        assert "branch-specific continuation from `main`" in content
        assert "source_branch=$intended_branch" in content
        assert 'git checkout "$intended_branch"' in content
        assert "source_branch=$(git rev-parse --abbrev-ref head)" in content
        assert "only once the mr is merged and `main` is green" in content
        assert content.index("only once the mr is merged and `main` is green") < (
            content.index("git branch -d claude/<slug>")
        )
        assert content.index("source_branch=$intended_branch") < content.index(
            'git checkout -b "$intended_branch"'
        )

    for workflow_path in (
        project_root / "README.md",
        project_root / "CLAUDE.md",
        project_root / "template" / "CLAUDE.md",
    ):
        content = workflow_path.read_text().lower()
        assert not any(gate in content for gate in forbidden_gates), workflow_path
        assert "awaiting_human" in content
        assert "without waiting for merge" in content


@pytest.mark.parametrize(
    "relative_path",
    (
        ".claude/git-flow.md",
        ".claude/commands/setup-repo.md",
        ".claude/commands/architect.md",
        ".claude/commands/judge-quality.md",
        ".claude/commands/judge-test.md",
        ".claude/commands/tdd-finalizer.md",
    ),
)
def test_root_and_template_share_the_same_phase_zero_workflow_contract(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """Test that source and generated projects retain identical workflow policy."""
    assert (project_root / relative_path).read_bytes() == (
        project_root / "template" / relative_path
    ).read_bytes()
    assert (project_root / relative_path).read_bytes() == (
        generated_project / relative_path
    ).read_bytes()


def _outcome_rule(schema: dict[str, Any], outcome: str) -> dict[str, Any]:
    """Return the conditional schema branch for one result outcome."""
    for rule in schema["allOf"]:
        outcome_condition = rule["if"]["properties"].get("outcome", {})
        if outcome_condition.get("const") == outcome:
            return rule
    raise AssertionError(f"Missing conditional result rule for {outcome!r}")


def test_work_item_contract_and_result_schema_are_canonical_and_enforce_invariants(
    generated_project: Path, project_root: Path
) -> None:
    """Test source and rendered state/result contracts are complete and synchronized."""
    contract_path = ".agents/contracts/work-item-contract.md"
    schema_path = ".agents/contracts/work-item-result-v1.schema.json"

    for path in (contract_path, schema_path):
        assert (project_root / path).exists()
        assert (project_root / "template" / path).exists()
        assert (project_root / path).read_bytes() == (
            project_root / "template" / path
        ).read_bytes()
        assert (project_root / path).read_bytes() == (
            generated_project / path
        ).read_bytes()

    contract = (project_root / contract_path).read_text()
    states = (
        "discovered",
        "validated",
        "queued",
        "claimed",
        "running",
        "completed",
        "awaiting_human",
        "retry_wait",
        "dead_letter",
    )
    for state in states:
        assert state in contract

    state_section = contract.split("## State machine", maxsplit=1)[1].split(
        "## Transition audit", maxsplit=1
    )[0]
    transition_lines = re.findall(r"^\w+ -> .+$", state_section, flags=re.MULTILINE)
    allowed_transitions = {
        source: {target.strip() for target in targets.split("|")}
        for source, targets in (line.split(" -> ") for line in transition_lines)
    }
    assert allowed_transitions == {
        "discovered": {"validated", "dead_letter"},
        "validated": {"queued"},
        "queued": {"claimed"},
        "claimed": {"running", "retry_wait", "dead_letter"},
        "running": {"completed", "awaiting_human", "retry_wait", "dead_letter"},
        "awaiting_human": {"queued", "completed", "dead_letter"},
        "retry_wait": {"queued", "dead_letter"},
    }
    assert "Only `completed` and `dead_letter` are terminal" in contract
    assert "parked and releases its claim and lease" in contract
    for audit_field in (
        "from_status",
        "to_status",
        "run_id",
        "attempt_count",
        "occurred_at",
        "reason",
    ):
        assert f"`{audit_field}`" in contract
    for outcome, state in (
        ("completed", "completed"),
        ("awaiting_human", "awaiting_human"),
        ("retry", "retry_wait"),
        ("dead_letter", "dead_letter"),
    ):
        assert f"| `{outcome}` | `{state}` |" in contract

    schema = json.loads((project_root / schema_path).read_text())
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "work_item_id",
        "run_id",
        "mode",
        "outcome",
        "summary",
        "tests",
        "findings",
        "next_action",
        "error",
    }
    assert schema["properties"]["schema_version"] == {"const": 1}
    assert schema["properties"]["mode"]["enum"] == ["inspect", "publish"]
    assert schema["properties"]["outcome"]["enum"] == [
        "completed",
        "awaiting_human",
        "retry",
        "dead_letter",
    ]
    assert "retryable" not in schema["properties"]
    assert schema["properties"]["error"]["type"] == ["object", "null"]
    for field in ("branch", "branch_tip", "merge_request_url"):
        assert field not in schema["required"]
        assert schema["properties"][field]["type"] == ["string", "null"]
    assert schema["properties"]["tests"]["type"] == "array"
    assert schema["properties"]["findings"]["type"] == "array"

    retry_rule = _outcome_rule(schema, "retry")["then"]
    assert set(retry_rule["required"]) == {"error", "next_action"}
    assert retry_rule["properties"]["error"]["properties"]["retryable"] == {
        "const": True
    }
    assert retry_rule["properties"]["next_action"]["minLength"] == 1

    dead_letter_rule = _outcome_rule(schema, "dead_letter")["then"]
    assert set(dead_letter_rule["required"]) == {"error", "next_action"}
    assert dead_letter_rule["properties"]["error"]["properties"]["retryable"] == {
        "const": False
    }
    assert dead_letter_rule["properties"]["next_action"]["minLength"] == 1

    awaiting_human_rule = _outcome_rule(schema, "awaiting_human")["then"]
    assert awaiting_human_rule["required"] == ["next_action"]
    assert awaiting_human_rule["properties"]["next_action"]["minLength"] == 1

    merge_request_rule = next(
        rule
        for rule in schema["allOf"]
        if "merge_request_url" in rule["if"]["properties"]
    )
    assert set(merge_request_rule["then"]["required"]) == {
        "branch",
        "branch_tip",
    }

    _validate_representative_work_item_results(schema)
    generated_schema = json.loads((generated_project / schema_path).read_text())
    _validate_representative_work_item_results(generated_schema)


def _base_work_item_result() -> dict[str, Any]:
    """Return a minimal valid completed inspection result."""
    return {
        "schema_version": 1,
        "work_item_id": "gitlab:310967:123",
        "run_id": "run-123",
        "mode": "inspect",
        "outcome": "completed",
        "summary": "Completed an inspection.",
        "tests": [],
        "findings": [],
        "next_action": "",
        "error": None,
    }


def _validate_representative_work_item_results(schema: dict[str, Any]) -> None:
    """Validate representative accepted and rejected result envelopes."""
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    base = _base_work_item_result()
    published_artifacts = {
        "branch": "agent/issue-123",
        "branch_tip": "0123456789abcdef0123456789abcdef01234567",
        "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
    }
    human_blocker = {
        "code": "human_input_required",
        "message": "The acceptance criteria are ambiguous.",
        "retryable": False,
    }
    valid_results = (
        base,
        base
        | {
            "outcome": "awaiting_human",
            "error": human_blocker,
            "next_action": "Clarify the acceptance criteria.",
        },
        base
        | {
            "mode": "publish",
            "outcome": "awaiting_human",
            "error": human_blocker,
            "next_action": "Clarify the acceptance criteria.",
        },
        base
        | {
            "mode": "publish",
            "outcome": "awaiting_human",
            **published_artifacts,
            "next_action": "Human review and merge",
        },
        base | {"mode": "publish", **published_artifacts},
        base
        | {
            "mode": "publish",
            "outcome": "retry",
            "next_action": "Retry after backoff",
            "error": {"code": "timeout", "message": "Timed out", "retryable": True},
        },
        base
        | {
            "mode": "publish",
            "outcome": "dead_letter",
            "next_action": "Inspect invalid input",
            "error": {
                "code": "invalid_input",
                "message": "Input is invalid",
                "retryable": False,
            },
        },
    )
    for result in valid_results:
        validator.validate(result)

    invalid_case_names = (
        "extra property",
        "local change mode",
        "inspect branch",
        "tip without branch",
        "completed error",
        "retry without error",
        "retry marked fatal",
        "dead letter marked retryable",
        "awaiting human without action",
        "inspect awaiting human without blocker",
        "publish completed without artifacts",
        "publish completed with partial artifacts",
        "publish awaiting human without blocker or artifacts",
        "publish awaiting human with partial artifacts",
        "publish awaiting human with artifacts and blocker",
        "invalid branch tip",
        "invalid merge request URI",
    )
    invalid_results = (
        base | {"unexpected": True},
        base | {"mode": "local-change"},
        base | {"mode": "inspect", "branch": "agent/forbidden"},
        base | {"branch_tip": "0123456789abcdef0123456789abcdef01234567"},
        base
        | {"error": {"code": "unexpected", "message": "Unexpected", "retryable": True}},
        base | {"outcome": "retry", "next_action": "", "error": None},
        base
        | {
            "outcome": "retry",
            "next_action": "Retry later",
            "error": {"code": "fatal", "message": "Fatal", "retryable": False},
        },
        base
        | {
            "outcome": "dead_letter",
            "next_action": "Inspect",
            "error": {"code": "timeout", "message": "Timed out", "retryable": True},
        },
        base | {"outcome": "awaiting_human", "next_action": ""},
        base
        | {
            "outcome": "awaiting_human",
            "next_action": "Clarify the acceptance criteria.",
        },
        base
        | {
            "mode": "publish",
        },
        base | {"mode": "publish", "branch": "agent/issue-123"},
        base
        | {
            "mode": "publish",
            "outcome": "awaiting_human",
            "next_action": "Human review and merge",
        },
        base
        | {
            "mode": "publish",
            "outcome": "awaiting_human",
            "branch": "agent/issue-123",
            "next_action": "Human review and merge",
        },
        base
        | {
            "mode": "publish",
            "outcome": "awaiting_human",
            **published_artifacts,
            "error": human_blocker,
            "next_action": "Resolve the blocker.",
        },
        base
        | {
            "mode": "publish",
            "branch_tip": "not-a-sha",
            "merge_request_url": published_artifacts["merge_request_url"],
        },
        base
        | {
            "mode": "publish",
            "branch": published_artifacts["branch"],
            "branch_tip": published_artifacts["branch_tip"],
            "merge_request_url": "not a URI",
        },
    )
    for case, result in zip(invalid_case_names, invalid_results, strict=True):
        assert not validator.is_valid(result), case


def test_root_and_template_ship_identical_gitlab_issue_templates(
    project_root: Path,
) -> None:
    """Test that source and generated projects use the same issue forms."""
    for filename in ("Bug.md", "Feature.md"):
        root_template = project_root / ".gitlab" / "issue_templates" / filename
        generated_template = (
            project_root / "template" / ".gitlab" / "issue_templates" / filename
        )

        assert root_template.exists()
        assert generated_template.exists()
        assert root_template.read_bytes() == generated_template.read_bytes()
