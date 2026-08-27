"""Tests for the copier template."""

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml


def test_generated_project_structure(generated_project: Path) -> None:
    """Test that the generated project has the expected top-level structure."""
    assert (generated_project / "pyproject.toml").exists()
    assert (generated_project / "README.md").exists()
    assert (generated_project / "CLAUDE.md").exists()
    assert (generated_project / ".gitignore").exists()
    assert (generated_project / ".pre-commit-config.yaml").exists()
    assert (generated_project / ".gitlab-ci.yml").exists()


def test_generated_agent_instructions_define_portable_model_tiers(
    generated_project: Path,
) -> None:
    """Generated instructions support the declared agent frontends and tiers."""
    instructions = (generated_project / "AGENTS.md").read_text()

    for frontend in ("Claude Code", "Codex", "Pi"):
        assert frontend in instructions
    for tier in ("Most capable", "Balanced", "Fast", "Frontier"):
        assert tier in instructions
    assert "Fable" in instructions
    assert "must not select" in instructions


def test_generated_agent_instructions_include_a_safe_session_checklist(
    generated_project: Path,
) -> None:
    """Generated agents receive the operational context needed to start safely."""
    instructions = (generated_project / "AGENTS.md").read_text()

    for requirement in (
        "## Agent session checklist",
        "CONTRIBUTING.md",
        "untrusted task data",
        "Never approve or merge",
        "Git and GitLab API access independently",
    ):
        assert requirement in instructions


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


def test_pytest_ci_installs_jq_for_executable_preflight_tests(
    generated_project: Path,
    project_root: Path,
) -> None:
    """Root and generated pytest jobs provide the jq executable tests require."""
    for ci_path in (
        project_root / ".gitlab-ci.yml",
        project_root / "template" / ".gitlab-ci.yml",
        generated_project / ".gitlab-ci.yml",
    ):
        ci_config = ci_path.read_text()
        pytest_job = re.search(
            r"^pytest:\n(?P<body>(?:^[ \t].*\n?)*)",
            ci_config,
            flags=re.MULTILINE,
        )

        assert pytest_job is not None
        assert "apt-get install -y --no-install-recommends git jq" in pytest_job.group(
            "body"
        )


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
    assert 'loginctl show-user "$USER" --property=Linger' in content


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


def test_claude_commands_are_absent(generated_project: Path) -> None:
    """Generated projects use agent roles without Claude command wrappers."""
    commands_dir = generated_project / ".claude" / "commands"
    assert not commands_dir.exists()


def test_lint_and_pytest_skill_is_generated(
    generated_project: Path, project_root: Path
) -> None:
    """Generated projects ship the portable Python validation skill."""
    skill = generated_project / ".agents" / "skills" / "lint-and-pytest" / "SKILL.md"
    source_skill = project_root / ".agents" / "skills" / "lint-and-pytest" / "SKILL.md"

    assert skill.exists()
    assert skill.read_text() == source_skill.read_text()
    content = skill.read_text()
    for requirement in (
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run ty check",
        "uv run pytest",
        "uv run tox",
        "Do not apply automatic fixes",
    ):
        assert requirement in content


@pytest.mark.parametrize(
    ("skill_name", "model_tier"),
    (
        ("architect", "most-capable"),
        ("cli-syntax-judge", "fast"),
        ("diataxis-judge", "fast"),
        ("diataxis-writer", "balanced"),
        ("filesystem-structure-judge", "fast"),
        ("judge-cicd", "balanced"),
        ("judge-quality", "most-capable"),
        ("judge-test", "balanced"),
        ("nonfunctional-review", "fast"),
        ("remediate", "most-capable"),
        ("tdd-document", "balanced"),
        ("tdd-finalizer", "fast"),
        ("tdd-green", "balanced"),
        ("tdd-judge", "balanced"),
        ("tdd-red", "balanced"),
    ),
)
def test_workflow_roles_are_portable_directory_skills(
    generated_project: Path, project_root: Path, skill_name: str, model_tier: str
) -> None:
    """Every workflow role is a shared Agent Skills directory with a tier."""
    relative_path = f".claude/skills/{skill_name}/SKILL.md"
    root_skill = project_root / relative_path
    generated_skill = generated_project / relative_path

    assert root_skill.exists()
    assert root_skill.read_text() == generated_skill.read_text()
    content = root_skill.read_text()
    assert f"name: {skill_name}" in content
    assert f"model-tier: {model_tier}" in content
    assert f"agents/{skill_name}.md" in content
    assert (
        "\nmodel:"
        not in (
            generated_project / ".claude" / "agents" / f"{skill_name}.md"
        ).read_text()
    )


def test_pi_loads_the_shared_project_skill_directory(
    generated_project: Path, project_root: Path
) -> None:
    """Pi uses the same project skill directories as the other frontends."""
    relative_path = ".pi/settings.json"
    root_settings = project_root / relative_path
    generated_settings = generated_project / relative_path

    assert root_settings.read_text() == generated_settings.read_text()
    assert json.loads(root_settings.read_text()) == {"skills": ["../.claude/skills"]}


def test_nonfunctional_review_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects review shared timed test results before finalization."""
    agent = generated_project / ".claude" / "agents" / "nonfunctional-review.md"
    workflow = (generated_project / "CLAUDE.md").read_text()

    assert agent.exists()
    assert "test-results.xml" in agent.read_text()
    assert "time" in agent.read_text()
    assert "performance" in agent.read_text().lower()
    assert "Non-functional Reviewer" in workflow
    assert workflow.index("Non-functional Reviewer") < workflow.index("Finalizer")
    assert (
        "nonfunctional-review"
        in (generated_project / ".claude" / "agents" / "tdd-finalizer.md").read_text()
    )


def test_diataxis_documentation_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the Diataxis documentation gates."""
    docs_process = generated_project / "docs" / "diataxis.md"
    writer = generated_project / ".claude" / "agents" / "diataxis-writer.md"
    judge = generated_project / ".claude" / "agents" / "diataxis-judge.md"

    assert docs_process.exists()
    assert "Tutorials" in docs_process.read_text()
    assert "How-to guides" in docs_process.read_text()
    assert "Reference" in docs_process.read_text()
    assert "Explanation" in docs_process.read_text()
    assert "include a Mermaid flowchart" in docs_process.read_text()
    assert writer.exists()
    assert judge.exists()
    assert "Test Writer" in docs_process.read_text()
    assert "Implementation Judge" in judge.read_text()
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
    documentation_process = generated_project / "docs" / "diataxis.md"
    documentation_index = generated_project / "docs" / "README.md"

    assert remediation_agent.exists()
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
        "explanation/agentic-workflow.md",
        "diataxis.md",
    ):
        assert page in index


def test_explanations_use_mermaid_for_relationship_diagrams(
    project_root: Path,
) -> None:
    """Test that explanation pages use Mermaid for relationship diagrams."""
    template_explanation = (
        project_root / "docs" / "explanation" / "template-and-generated-project.md"
    ).read_text()
    workflow_explanation = (
        project_root / "docs" / "explanation" / "agentic-workflow.md"
    ).read_text()

    assert "```mermaid\nflowchart" in template_explanation
    assert "```mermaid\nflowchart" in workflow_explanation
    assert "Filesystem Judge" in workflow_explanation


def test_cli_syntax_judge_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the CLI syntax design gate."""
    agent = generated_project / ".claude" / "agents" / "cli-syntax-judge.md"
    workflow = (generated_project / "CLAUDE.md").read_text()

    assert agent.exists()
    assert "https://clig.dev" in agent.read_text()
    assert "--json" in agent.read_text()
    assert "--apply" in agent.read_text()
    assert "CLI Judge" in workflow
    assert "Architect" in workflow


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


def test_repository_setup_procedure_present(generated_project: Path) -> None:
    """New projects retain a portable procedure for GitLab lock-down."""
    setup_repo = generated_project / ".claude" / "repository-setup.md"
    assert setup_repo.exists()
    content = setup_repo.read_text()
    assert "protected_branches" in content
    assert "approval_rules" in content
    assert "/approvals" in content, (
        "self-approval block must target /projects/<id>/approvals, not the bare project endpoint"
    )


def test_setup_repo_publishes_its_bootstrap_change(generated_project: Path) -> None:
    """Test that lock-down setup cannot strand CONTRIBUTING.md on local main."""
    content = (generated_project / ".claude" / "repository-setup.md").read_text()
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


def test_agent_loop_wrapper_returns_dedicated_checkout_to_main(
    generated_project: Path,
) -> None:
    """The systemd pass wrapper resets its dedicated checkout after a pass."""
    content = (
        generated_project / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
    ).read_text()
    wrapper = (generated_project / "scripts" / "agent-work-systemd-run.sh").read_text()

    assert "Every bounded pass attempts to leave the dedicated checkout" in content
    assert "git switch main" in wrapper
    assert "git pull --ff-only" in wrapper
    assert "pass failed (exit $pass_status); attempting checkout recovery" in wrapper
    assert wrapper.index("pass_status=0") < wrapper.rindex("return_checkout_to_main")


def test_agent_work_worker_exception_overrides_generic_publication_rules(
    generated_project: Path,
) -> None:
    """Generated workers do not inherit the ordinary-agent publication duty."""
    required = "Serial agent-work worker exception"

    for relative_path in ("AGENTS.md", "CLAUDE.md"):
        assert required in (generated_project / relative_path).read_text()


def test_repository_setup_interviews_self_approval(generated_project: Path) -> None:
    """The setup procedure asks about self-approval for single-user projects."""
    content = (generated_project / ".claude" / "repository-setup.md").read_text()
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
    assert ".claude/repository-setup.md" in content
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
    workflow = (generated_project / "CLAUDE.md").read_text()
    finalizer = (
        generated_project / ".claude" / "agents" / "tdd-finalizer.md"
    ).read_text()

    assert agent.exists()
    assert "src/" in agent.read_text()
    assert "bin/" in agent.read_text()
    assert "Filesystem structure deviations" in agent.read_text()
    assert "Filesystem Judge" in workflow
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

        assert agent.exists()
        assert "Filesystem Judge" in workflow
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
    content = (project_root / root / ".claude" / "agents" / "architect.md").read_text()

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
def test_review_agents_file_minor_findings_as_gitlab_bug_issues(
    project_root: Path, command_name: str
) -> None:
    """Test that approving reviews hand off Bug issues in a machine-readable form."""
    for root in (project_root, project_root / "template"):
        content = (root / ".claude" / "agents" / command_name).read_text()

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
        project_root / "CLAUDE.md",
        project_root / "template" / "CLAUDE.md",
    ):
        content = workflow_path.read_text().lower()
        assert not any(gate in content for gate in forbidden_gates), workflow_path
        assert "awaiting_human" in content
        assert "without waiting for merge" in content

    readme_content = (project_root / "README.md").read_text()
    assert "docs/how-to/setup-serial-gitlab-agent-loop.md" in readme_content


@pytest.mark.parametrize(
    "relative_path",
    (
        ".claude/git-flow.md",
        ".claude/repository-setup.md",
        ".claude/agents/architect.md",
        ".claude/agents/judge-quality.md",
        ".claude/agents/judge-test.md",
        ".claude/agents/tdd-finalizer.md",
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


@pytest.mark.parametrize(
    "relative_path",
    (
        "src/agent_work/__init__.py",
        "src/agent_work/models.py",
        "src/agent_work/serial.py",
        "src/agent_work/adapters.py",
        "src/agent_work/cli.py",
        ".agents/skills/work-item-runner/SKILL.md",
        ".agents/skills/setup-agent-loop/SKILL.md",
    ),
)
def test_serial_runner_assets_have_exact_root_template_and_generated_parity(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """The Copier source and output ship one identical serial runner contract."""
    root_file = project_root / relative_path
    template_file = project_root / "template" / relative_path
    generated_file = generated_project / relative_path

    assert root_file.read_bytes() == template_file.read_bytes()
    assert root_file.read_bytes() == generated_file.read_bytes()


def test_session_boundary_workflow_preserves_agentic_handoffs(
    generated_project: Path, project_root: Path
) -> None:
    """Agentic workers and user sessions have distinct main-branch exits."""
    relative_path = ".claude/git-flow.md"
    content = (project_root / relative_path).read_text()

    assert content == (project_root / "template" / relative_path).read_text()
    assert content == (generated_project / relative_path).read_text()
    assert "### Session boundary" in content
    assert "agent/issue-<IID>" in content
    assert "If the MR is still open or is not merged" in content


def test_setup_loop_skill_classifies_effective_developer_access_without_attestation(
    generated_project: Path, project_root: Path
) -> None:
    """The setup skill has one constrained-Developer fast path in every output."""
    relative_path = ".agents/skills/setup-agent-loop/SKILL.md"
    root_skill = (project_root / relative_path).read_text()

    assert root_skill == (project_root / "template" / relative_path).read_text()
    assert root_skill == (generated_project / relative_path).read_text()

    assert re.search(
        r'project_json\s*=\s*"?\$\(\s*glab api --hostname "\$GITLAB_HOST" '
        r'"projects/\$GITLAB_PROJECT_ID"\s*\)"?',
        root_skill,
    )
    assert "jq" in root_skill
    assert ".permissions.project_access.access_level" in root_skill
    assert ".permissions.group_access.access_level" in root_skill
    assert "max" in root_skill

    for requirement in (
        "exactly 30",
        "Developer",
        "less than 30",
        "unknown",
        "block `--apply`",
        "40 or greater",
        "constrained Developer credential",
    ):
        assert requirement in root_skill

    assert "Developer `403`" not in root_skill
    assert "human Maintainer attestation" not in root_skill
    assert "members/all" not in root_skill
    assert "--jq" not in root_skill
    assert "privileged path" not in root_skill


def test_setup_loop_skill_preflights_developer_readable_repository_safeguards(
    project_root: Path,
) -> None:
    """Developer readiness proves observable safeguards before applied work."""
    skill = (project_root / ".agents/skills/setup-agent-loop/SKILL.md").read_text()

    effective_access_marker = "exactly 30"
    assert effective_access_marker in skill

    for requirement in (
        "jq -e",
        "default_branch",
        "default_branch_uri",
        "projects/$GITLAB_PROJECT_ID/repository/branches/$default_branch_uri",
        "developers_can_push",
        "developers_can_merge",
        "can_push",
        "protected",
        "approval_rules",
        "at least one",
        "projects/$GITLAB_PROJECT_ID/approval_rules",
        "applies_to_all_protected_branches",
        "no protected branches",
        "/approvals",
        "recurring Maintainer attestation",
        "actionable",
        "missing evidence",
        "block `--apply`",
    ):
        assert requirement in skill

    assert re.search(
        r"skip only.*?/approvals.*?recurring Maintainer attestation",
        skill,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert skill.index(effective_access_marker) < skill.index("repository/branches")
    assert skill.index(effective_access_marker) < skill.index("approval_rules")
    assert "repository/files/CONTRIBUTING.md/raw" not in skill
    assert "author/committers cannot self-approve" not in skill


def test_agents_define_the_developer_readable_preflight_exception(
    generated_project: Path, project_root: Path
) -> None:
    """Every project copy preserves the narrow exception to Maintainer-only nag."""
    root_agents = (project_root / "AGENTS.md").read_text()

    assert root_agents == (project_root / "template" / "AGENTS.md").read_text()
    assert root_agents == (generated_project / "AGENTS.md").read_text()

    for requirement in (
        "Developer-readable",
        "exactly `30`",
        "branch protection",
        "approval_rules",
        "default branch",
        "exception",
        "Maintainer-only",
        "/approvals",
        "recurring Maintainer attestation",
        "Developer role alone does not prove",
        "self-approval",
        "override",
    ):
        assert requirement in root_agents


@pytest.mark.parametrize(
    "relative_path",
    (
        ".agents/skills/setup-agent-loop/SKILL.md",
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
    ),
)
def test_setup_preflight_url_encodes_default_branch_before_api_paths(
    generated_project: Path,
    project_root: Path,
    relative_path: str,
) -> None:
    """A default branch name cannot change a numeric-project API path or query."""
    root_document = (project_root / relative_path).read_text()

    assert root_document == (project_root / "template" / relative_path).read_text()
    assert root_document == (generated_project / relative_path).read_text()

    effective_branch_path_variables = re.findall(
        r"repository/branches/\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?",
        root_document,
    )
    assert "ref=$default_branch" not in root_document
    assert effective_branch_path_variables

    encoded_branch = re.search(
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:uri|encoded)[A-Za-z0-9_]*)"
        r"\s*=\s*\"?\$\(.*?jq.*?@uri.*?\)\"?",
        root_document,
        flags=re.DOTALL,
    )
    assert encoded_branch is not None

    encoded_name = encoded_branch.group("name")
    assert encoded_branch.start() < root_document.index("repository/branches/")
    assert all(name == encoded_name for name in effective_branch_path_variables)


def _bash_block_after(skill: str, anchor: str) -> str:
    """Extract one Bash product snippet after its stable setup-skill anchor."""
    block = re.search(
        rf"{re.escape(anchor)}.*?^[ \t]*```bash\n(?P<script>.*?)\n^[ \t]*```",
        skill,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert block is not None, anchor
    return block.group("script")


_READINESS_SURFACES = (
    pytest.param(
        ".agents/skills/setup-agent-loop/SKILL.md",
        "1. Establish and validate a bare `GITLAB_HOST`",
        id="setup-skill",
    ),
    pytest.param(
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
        "## Verify Developer-readable safeguards",
        id="how-to",
    ),
)


def _readiness_snippet(
    generated_project: Path,
    project_root: Path,
    relative_path: str,
    anchor: str,
) -> str:
    """Load one parity-checked, executable readiness fence from the product."""
    document = (project_root / relative_path).read_text()

    assert document == (project_root / "template" / relative_path).read_text()
    assert document == (generated_project / relative_path).read_text()
    return _bash_block_after(document, anchor)


def _run_setup_skill_snippet(
    snippet: str,
    *,
    project_json: str,
    gitlab_host: str | None = "gitlab.example.test",
    gitlab_project_id: str | None = "123",
    effective_branch_json: str = (
        '{"name":"release/a","default":true,"protected":true,'
        '"developers_can_push":false,"developers_can_merge":false,'
        '"can_push":false}'
    ),
    approval_rules_json: str = '[{"approvals_required":1}]',
) -> subprocess.CompletedProcess[str]:
    """Execute a documented snippet with hermetic GitLab and Git boundaries."""
    runner = f"""
glab() {{
  case "${{1:-}}" in
    auth)
      test "${{2:-}}" = status
      ;;
    api)
      request="${{@: -1}}"
      case "$request" in
        user)
          printf '%s\\n' '{{"username":"developer"}}'
          ;;
        */approvals*)
          printf '%s\\n' 'unexpected Maintainer-only /approvals request' >&2
          return 91
          ;;
        projects/123/repository/branches/release%2Fa)
          printf '%s\\n' "$EFFECTIVE_BRANCH_JSON"
          ;;
        */repository/branches/*)
          printf 'unexpected effective default-branch endpoint: %s\\n' "$request" >&2
          return 95
          ;;
        */approval_rules)
          printf '%s\\n' "$APPROVAL_RULES_JSON"
          ;;
        projects/*)
          printf '%s\\n' "$PROJECT_JSON"
          ;;
        *)
          printf 'unexpected GitLab endpoint: %s\\n' "$request" >&2
          return 92
          ;;
      esac
      ;;
    *)
      printf 'unexpected glab command: %s\\n' "$*" >&2
      return 93
      ;;
  esac
}}

codex() {{ :; }}

git() {{ return 94; }}

project_json="$PROJECT_JSON"
{snippet}
printf 'effective_access=%s\\n' "${{effective_access:-not-computed}}"
"""
    environment = os.environ.copy()
    environment.update(
        {
            "APPROVAL_RULES_JSON": approval_rules_json,
            "PROJECT_JSON": project_json,
            "EFFECTIVE_BRANCH_JSON": effective_branch_json,
        },
    )
    if gitlab_host is None:
        environment.pop("GITLAB_HOST", None)
    else:
        environment["GITLAB_HOST"] = gitlab_host
    if gitlab_project_id is None:
        environment.pop("GITLAB_PROJECT_ID", None)
    else:
        environment["GITLAB_PROJECT_ID"] = gitlab_project_id
    return subprocess.run(
        ["bash", "-Eeuo", "pipefail", "-c", runner],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def _project_access_json(project_access: object, group_access: object) -> str:
    """Build the project response supplied to the extracted access snippet."""
    return json.dumps(
        {
            "default_branch": "release/a",
            "permissions": {
                "group_access": {"access_level": group_access},
                "project_access": {"access_level": project_access},
            },
        },
    )


def test_setup_skill_bash_fence_is_explicitly_fail_closed(
    project_root: Path,
) -> None:
    """The complete executable readiness fence owns strict failure handling."""
    skill = (project_root / ".agents/skills/setup-agent-loop/SKILL.md").read_text()
    snippet = _bash_block_after(skill, "1. Establish and validate a bare `GITLAB_HOST`")

    assert snippet.lstrip().startswith("set -Eeuo pipefail") or (
        "if !" in snippet and "exit 1" in snippet
    )
    assert "missing evidence" in snippet
    assert "block --apply" in snippet


@pytest.mark.parametrize(
    ("gitlab_host", "gitlab_project_id", "expected"),
    (
        (None, "123", "GITLAB_HOST must be a bare hostname"),
        ("gitlab.example.test", None, "GITLAB_PROJECT_ID must be numeric"),
    ),
)
def test_setup_skill_preflight_reports_unset_target_variables(
    generated_project: Path,
    project_root: Path,
    gitlab_host: str | None,
    gitlab_project_id: str | None,
    expected: str,
) -> None:
    """Strict mode reports actionable missing evidence for an unset target."""
    snippet = _readiness_snippet(
        generated_project,
        project_root,
        ".agents/skills/setup-agent-loop/SKILL.md",
        "1. Establish and validate a bare `GITLAB_HOST`",
    )

    result = _run_setup_skill_snippet(
        snippet,
        project_json=_developer_project_json(),
        gitlab_host=gitlab_host,
        gitlab_project_id=gitlab_project_id,
    )

    assert result.returncode != 0
    assert expected in result.stderr
    assert "missing evidence" in result.stderr
    assert "block --apply" in result.stderr


@pytest.mark.parametrize(
    ("project_access", "group_access"),
    ((30, 30), (30, 20), (20, 30), (None, 30), (30, None)),
)
@pytest.mark.parametrize(("relative_path", "anchor"), _READINESS_SURFACES)
def test_readiness_snippet_allows_only_effective_developer_access(
    generated_project: Path,
    project_root: Path,
    project_access: int | None,
    group_access: int | None,
    relative_path: str,
    anchor: str,
) -> None:
    """Every published readiness fence permits only effective Developer access."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
        anchor,
    )

    result = _run_setup_skill_snippet(
        readiness_snippet,
        project_json=_project_access_json(project_access, group_access),
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("project_access", "group_access"),
    (
        (20, 20),
        (None, None),
        ("30", None),
        (None, "30"),
        ("30", "30"),
        (31, 30),
        (30, 31),
        (40, 30),
        (30, 40),
        (50, 30),
        (30, 50),
    ),
)
@pytest.mark.parametrize(("relative_path", "anchor"), _READINESS_SURFACES)
def test_readiness_snippet_fails_closed_for_every_other_level(
    generated_project: Path,
    project_root: Path,
    project_access: object,
    group_access: object,
    relative_path: str,
    anchor: str,
) -> None:
    """Every published fence blocks unknown, low, and privileged access."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
        anchor,
    )

    result = _run_setup_skill_snippet(
        readiness_snippet,
        project_json=_project_access_json(project_access, group_access),
    )

    assert result.returncode != 0
    assert "block --apply" in f"{result.stdout}\n{result.stderr}".lower()


def _effective_branch_json(
    *,
    omit: tuple[str, ...] = (),
    field_values: dict[str, object] | None = None,
    **overrides: object,
) -> str:
    """Build one authoritative effective-default-branch API response."""
    branch: dict[str, object] = {
        "name": "release/a",
        "default": True,
        "protected": True,
        "developers_can_push": False,
        "developers_can_merge": False,
        "can_push": False,
    }
    branch.update(field_values or {})
    branch.update(overrides)
    for field in omit:
        branch.pop(field)
    return json.dumps(branch)


def _approval_rules_json(*rules: dict[str, object]) -> str:
    """Build the approval-rules response used by the preflight snippet."""
    return json.dumps(list(rules))


def _developer_project_json() -> str:
    """Build a project response that selects the Developer readiness path."""
    return json.dumps(
        {
            "default_branch": "release/a",
            "permissions": {
                "group_access": {"access_level": 20},
                "project_access": {"access_level": 30},
            },
        },
    )


def test_how_to_approval_predicate_explicitly_requires_a_numeric_count(
    generated_project: Path, project_root: Path
) -> None:
    """The rendered jq guard must not rely on comparison type errors for safety."""
    relative_path = "docs/how-to/setup-serial-gitlab-agent-loop.md"
    how_to = (project_root / relative_path).read_text()

    assert how_to == (project_root / "template" / relative_path).read_text()
    assert how_to == (generated_project / relative_path).read_text()
    assert re.search(
        r"approvals_required[^\n]*?type\s*==\s*\"number\"",
        _bash_block_after(how_to, "## Verify Developer-readable safeguards"),
    )


def _approval_predicate_from(readiness_block: str) -> str:
    """Extract the approval-rule jq program from a guarded readiness block."""
    approval_check = readiness_block[readiness_block.index("approval_rules_json") :]
    predicate = re.search(
        r"jq -e(?: --arg default_branch \"\$default_branch\")? '\n"
        r"(?P<predicate>.*?)\n\s*'",
        approval_check,
        flags=re.DOTALL,
    )
    assert predicate is not None
    return predicate.group("predicate")


def _approval_predicate_succeeds(predicate: str, approval_rules: str) -> bool:
    """Evaluate an extracted jq approval predicate without any GitLab request."""
    result = subprocess.run(
        ["jq", "-e", "--arg", "default_branch", "release/a", predicate],
        capture_output=True,
        check=False,
        input=approval_rules,
        text=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    ("approval_rules", "expected"),
    (
        pytest.param(
            _approval_rules_json({"approvals_required": 1}),
            True,
            id="omitted-scope-is-global",
        ),
        pytest.param(
            _approval_rules_json({"approvals_required": 1, "protected_branches": []}),
            True,
            id="empty-scope-is-global",
        ),
        pytest.param(
            _approval_rules_json(
                {
                    "applies_to_all_protected_branches": True,
                    "approvals_required": 1,
                },
            ),
            True,
            id="applies-to-all-is-global",
        ),
        pytest.param(
            _approval_rules_json({"approvals_required": 1, "protected_branches": None}),
            False,
            id="null-scope-is-not-global",
        ),
        pytest.param(
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": "not-an-array"},
            ),
            False,
            id="string-scope-is-not-global",
        ),
        pytest.param(
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": {"name": "release/a"}},
            ),
            False,
            id="object-scope-is-not-global",
        ),
        pytest.param(
            _approval_rules_json({"approvals_required": 1, "protected_branches": 0}),
            False,
            id="numeric-scope-is-not-global",
        ),
        pytest.param(
            _approval_rules_json(
                {
                    "approvals_required": 1,
                    "protected_branches": [{"name": "release/a"}],
                },
            ),
            False,
            id="default-branch-scoped-rule-is-not-global",
        ),
    ),
)
def test_how_to_approval_predicate_matches_skill_scope_semantics(
    generated_project: Path,
    project_root: Path,
    approval_rules: str,
    expected: bool,
) -> None:
    """The how-to and skill accept only unambiguous global approval rules."""
    how_to_path = "docs/how-to/setup-serial-gitlab-agent-loop.md"
    skill_path = ".agents/skills/setup-agent-loop/SKILL.md"
    how_to = (project_root / how_to_path).read_text()
    skill = (project_root / skill_path).read_text()

    assert how_to == (project_root / "template" / how_to_path).read_text()
    assert how_to == (generated_project / how_to_path).read_text()
    assert skill == (project_root / "template" / skill_path).read_text()
    assert skill == (generated_project / skill_path).read_text()

    how_to_result = _approval_predicate_succeeds(
        _approval_predicate_from(
            _bash_block_after(how_to, "## Verify Developer-readable safeguards"),
        ),
        approval_rules,
    )
    skill_result = _approval_predicate_succeeds(
        _approval_predicate_from(
            _bash_block_after(skill, "1. Establish and validate a bare `GITLAB_HOST`")
        ),
        approval_rules,
    )

    assert how_to_result is expected
    assert skill_result is expected


@pytest.mark.parametrize(
    "approval_rules",
    (
        _approval_rules_json({"approvals_required": 1}),
        _approval_rules_json(
            {"approvals_required": 1, "protected_branches": []},
        ),
        _approval_rules_json(
            {
                "applies_to_all_protected_branches": True,
                "approvals_required": 1,
            },
        ),
    ),
)
def test_how_to_preflight_accepts_numeric_approval_counts(
    generated_project: Path, project_root: Path, approval_rules: str
) -> None:
    """The how-to permits a numeric approval count for global rules only."""
    relative_path = "docs/how-to/setup-serial-gitlab-agent-loop.md"
    how_to = (project_root / relative_path).read_text()

    assert how_to == (project_root / "template" / relative_path).read_text()
    assert how_to == (generated_project / relative_path).read_text()

    result = _run_setup_skill_snippet(
        _bash_block_after(how_to, "## Verify Developer-readable safeguards"),
        approval_rules_json=approval_rules,
        project_json=_developer_project_json(),
        effective_branch_json=_effective_branch_json(),
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected Maintainer-only /approvals request" not in result.stderr
    assert (
        "Effective API access: Developer (30); Maintainer-only approval audit "
        "and attestation not required for this readiness policy." in result.stdout
    )


@pytest.mark.parametrize(
    "protected_branches_scope",
    (None, "not-an-array", {"name": "release/a"}, 0, [{"name": "release/a"}]),
)
def test_how_to_preflight_blocks_non_global_approval_rule_scopes(
    generated_project: Path, project_root: Path, protected_branches_scope: object
) -> None:
    """Malformed or branch-scoped approval rules never prove global coverage."""
    relative_path = "docs/how-to/setup-serial-gitlab-agent-loop.md"
    how_to = (project_root / relative_path).read_text()

    assert how_to == (project_root / "template" / relative_path).read_text()
    assert how_to == (generated_project / relative_path).read_text()

    result = _run_setup_skill_snippet(
        _bash_block_after(how_to, "## Verify Developer-readable safeguards"),
        approval_rules_json=_approval_rules_json(
            {
                "approvals_required": 1,
                "protected_branches": protected_branches_scope,
            },
        ),
        project_json=_developer_project_json(),
        effective_branch_json=_effective_branch_json(),
    )

    assert result.returncode not in (0, 91), result.stderr
    assert "missing evidence" in result.stderr
    assert "BLOCK --apply" in result.stderr


@pytest.mark.parametrize(
    "approval_rules",
    tuple(
        _approval_rules_json(
            {"approvals_required": value, "protected_branches": scope},
        )
        for value in ("0", "1", "not-a-number")
        for scope in ([],)
    ),
)
def test_how_to_preflight_blocks_string_approval_counts(
    generated_project: Path, project_root: Path, approval_rules: str
) -> None:
    """The how-to approval predicate rejects string values regardless of scope."""
    relative_path = "docs/how-to/setup-serial-gitlab-agent-loop.md"
    how_to = (project_root / relative_path).read_text()

    assert how_to == (project_root / "template" / relative_path).read_text()
    assert how_to == (generated_project / relative_path).read_text()

    result = _run_setup_skill_snippet(
        _bash_block_after(how_to, "## Verify Developer-readable safeguards"),
        approval_rules_json=approval_rules,
        project_json=_developer_project_json(),
        effective_branch_json=_effective_branch_json(),
    )

    assert result.returncode not in (0, 91), result.stderr
    assert "missing evidence" in result.stderr
    assert "BLOCK --apply" in result.stderr


@pytest.mark.parametrize(
    ("effective_branch", "approval_rules"),
    (
        pytest.param(
            _effective_branch_json(),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="global-rule-without-protected-branches",
        ),
        pytest.param(
            _effective_branch_json(),
            _approval_rules_json({"approvals_required": 1}),
            id="global-rule-omits-protected-branches",
        ),
        pytest.param(
            _effective_branch_json(),
            _approval_rules_json(
                {
                    "applies_to_all_protected_branches": True,
                    "approvals_required": 1,
                },
            ),
            id="rule-applies-to-all-protected-branches",
        ),
    ),
)
@pytest.mark.parametrize(("relative_path", "anchor"), _READINESS_SURFACES)
def test_readiness_snippet_accepts_complete_evidence(
    generated_project: Path,
    project_root: Path,
    effective_branch: str,
    approval_rules: str,
    relative_path: str,
    anchor: str,
) -> None:
    """Complete evidence passes through every Developer-readable preflight."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
        anchor,
    )

    result = _run_setup_skill_snippet(
        readiness_snippet,
        approval_rules_json=approval_rules,
        project_json=_developer_project_json(),
        effective_branch_json=effective_branch,
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected Maintainer-only /approvals request" not in result.stderr
    assert (
        "Effective API access: Developer (30); Maintainer-only approval audit "
        "and attestation not required for this readiness policy." in result.stdout
    )


@pytest.mark.parametrize(
    ("effective_branch", "approval_rules"),
    (
        pytest.param(
            _effective_branch_json(name="main"),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-branch-name-wrong",
        ),
        pytest.param(
            _effective_branch_json(omit=("name",)),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-branch-name-missing",
        ),
        pytest.param(
            _effective_branch_json(name=30),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-branch-name-malformed",
        ),
        pytest.param(
            _effective_branch_json(default=False),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-signal-wrong",
        ),
        pytest.param(
            _effective_branch_json(omit=("default",)),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-signal-missing",
        ),
        pytest.param(
            _effective_branch_json(default="true"),
            _approval_rules_json(
                {"approvals_required": 1, "protected_branches": []},
            ),
            id="default-signal-malformed",
        ),
        *(
            pytest.param(
                _effective_branch_json(field_values={field: wrong}),
                _approval_rules_json(
                    {"approvals_required": 1, "protected_branches": []},
                ),
                id=f"{field}-wrong",
            )
            for field, wrong in (
                ("protected", False),
                ("developers_can_push", True),
                ("developers_can_merge", True),
                ("can_push", True),
            )
        ),
        *(
            pytest.param(
                _effective_branch_json(omit=(field,)),
                _approval_rules_json(
                    {"approvals_required": 1, "protected_branches": []},
                ),
                id=f"{field}-missing",
            )
            for field in (
                "protected",
                "developers_can_push",
                "developers_can_merge",
                "can_push",
            )
        ),
        *(
            pytest.param(
                _effective_branch_json(field_values={field: "false"}),
                _approval_rules_json(
                    {"approvals_required": 1, "protected_branches": []},
                ),
                id=f"{field}-malformed",
            )
            for field in (
                "protected",
                "developers_can_push",
                "developers_can_merge",
                "can_push",
            )
        ),
        pytest.param(
            _effective_branch_json(),
            _approval_rules_json({"approvals_required": 0, "protected_branches": []}),
            id="no-approval-rule",
        ),
        pytest.param(
            _effective_branch_json(),
            _approval_rules_json(
                {
                    "approvals_required": 1,
                    "protected_branches": [{"name": "release/a"}],
                },
            ),
            id="rule-scoped-to-default-branch-is-not-global",
        ),
    ),
)
@pytest.mark.parametrize(("relative_path", "anchor"), _READINESS_SURFACES)
def test_readiness_snippet_reports_and_blocks_bad_evidence(
    generated_project: Path,
    project_root: Path,
    effective_branch: str,
    approval_rules: str,
    relative_path: str,
    anchor: str,
) -> None:
    """Every bad safeguard is reported and blocked by every readiness fence."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
        anchor,
    )

    result = _run_setup_skill_snippet(
        readiness_snippet,
        approval_rules_json=approval_rules,
        project_json=_developer_project_json(),
        effective_branch_json=effective_branch,
    )

    assert result.returncode not in (0, 91), result.stderr
    outcome = f"{result.stdout}\n{result.stderr}".lower()
    assert "missing evidence" in outcome
    assert "block --apply" in outcome


@pytest.mark.parametrize(
    "relative_path",
    (
        ".agents/skills/setup-agent-loop/SKILL.md",
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
        "docs/reference/serial-gitlab-agent-loop.md",
        "docs/explanation/serial-gitlab-agent-loop.md",
    ),
)
def test_developer_readiness_docs_state_the_unverifiable_self_approval_boundary(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """The Developer-readable policy does not pretend to prove hidden controls."""
    root_document = (project_root / relative_path).read_text()

    assert root_document == (project_root / "template" / relative_path).read_text()
    assert root_document == (generated_project / relative_path).read_text()

    for requirement in ("Developer", "self-approval", "override"):
        assert requirement in root_document
    assert re.search(
        r"Developer.*?(?:cannot|does not).*?prove",
        root_document,
        flags=re.DOTALL,
    )
    assert "does not check" in root_document or "skips" in root_document
    assert "CONTRIBUTING.md" not in root_document


def test_agent_controls_prohibit_approval_and_merge_in_every_project_copy(
    generated_project: Path, project_root: Path
) -> None:
    """Policy controls and runtime both exclude approval and merge operations."""
    expected_denials = {
        "Bash(glab mr approve*)",
        "Bash(glab mr revoke*)",
        "Bash(glab api*merge_requests*approve*)",
        "Bash(glab api*merge_requests*merge*)",
    }

    for root in (project_root, project_root / "template", generated_project):
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        denials = set(settings["permissions"]["deny"])
        runtime = "\n".join(
            path.read_text() for path in (root / "src" / "agent_work").glob("*.py")
        )

        assert expected_denials.issubset(denials)
        assert "mr approve" not in runtime
        assert "merge_requests/{mr_iid}/approve" not in runtime
        assert "merge_requests/{mr_iid}/merge" not in runtime


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
        "docs/reference/serial-gitlab-agent-loop.md",
        "docs/explanation/serial-gitlab-agent-loop.md",
    ),
)
def test_setup_loop_docs_have_root_template_and_generated_parity(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """Every rendered copy retains its category's operator documentation."""
    root_document = (project_root / relative_path).read_text()

    assert root_document == (project_root / "template" / relative_path).read_text()
    assert root_document == (generated_project / relative_path).read_text()

    assert "awaiting" in root_document
    assert re.search(r"never\s+approves\s+or\s+merges", root_document)

    assert "Developer `403`" not in root_document
    assert "human Maintainer attestation" not in root_document
    assert "members/all" not in root_document
    assert "--jq" not in root_document


def test_setup_loop_how_to_extracts_effective_access_from_the_project_response(
    project_root: Path,
) -> None:
    """The how-to gives operators an executable constrained-Developer check."""
    how_to = (
        project_root / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
    ).read_text()

    for requirement in (
        "project_json",
        ".permissions.project_access.access_level",
        ".permissions.group_access.access_level",
        "max",
        "unknown",
        "constrained Developer credential",
        "40 or greater",
    ):
        assert requirement in how_to
    assert "exactly" in how_to and "`30`" in how_to
    assert "less than" in how_to and "BLOCK --apply" in how_to


def test_setup_loop_reference_normatively_classifies_access_levels(
    project_root: Path,
) -> None:
    """The reference distinguishes the Developer fast path from privileged use."""
    reference = (
        project_root / "docs" / "reference" / "serial-gitlab-agent-loop.md"
    ).read_text()

    assert "unknown" in reference
    assert re.search(r"constrained Developer\s+credential", reference)
    assert re.search(r"`?40`?\s+or greater", reference)
    assert "exactly" in reference and "`30`" in reference
    assert "less than" in reference and "`--apply`" in reference
    assert "`40`" in reference
    assert "Developer `403`" not in reference
    assert "human Maintainer attestation" not in reference


def test_setup_loop_explanation_preserves_developer_access_caveats(
    project_root: Path,
) -> None:
    """The explanation makes clear that access level is not policy proof."""
    explanation = (
        project_root / "docs" / "explanation" / "serial-gitlab-agent-loop.md"
    ).read_text()

    assert "Developers may" in explanation
    assert "approve" in explanation
    assert "project" in explanation
    assert "self-approv" in explanation
    assert "constrained Developer credential" in explanation
    assert "40 or greater" in explanation
    assert "block `--apply`" in explanation


@pytest.mark.parametrize(
    "relative_path",
    (
        "AGENTS.md",
        ".agents/skills/setup-agent-loop/SKILL.md",
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
        "docs/reference/serial-gitlab-agent-loop.md",
        "docs/explanation/serial-gitlab-agent-loop.md",
    ),
)
def test_developer_readiness_never_treats_privileged_access_as_an_alternative_path(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """Policy and docs block 40+ and require the constrained identity instead."""
    root_document = (project_root / relative_path).read_text()

    assert root_document == (project_root / "template" / relative_path).read_text()
    assert root_document == (generated_project / relative_path).read_text()
    assert "privileged path" not in root_document

    if relative_path != "AGENTS.md":
        assert re.search(r"`?40`?\s+or greater", root_document)
        assert re.search(r"constrained Developer\s+credential", root_document)
        assert re.search(r"(?:block `--apply`|BLOCK --apply)", root_document)


def test_readmes_describe_developer_access_as_a_caveat_not_proof(
    generated_project: Path, project_root: Path
) -> None:
    """Landing-page guidance does not mistake Developer access for a safeguard."""
    root_readme = (project_root / "README.md").read_text()
    assert "docs/how-to/setup-serial-gitlab-agent-loop.md" in root_readme

    readmes = (
        (project_root / "template" / "README.md.jinja").read_text(),
        (generated_project / "README.md").read_text(),
    )

    for readme in readmes:
        for requirement in (
            "awaiting_human",
            "never approves or merges",
        ):
            assert requirement in readme
        assert "Developer" in readme
        assert "approv" in readme
        assert "project" in readme
        assert "self-approv" in readme
        assert "Developer `403`" not in readme
        assert "Maintainer has verified" not in readme


def test_legacy_local_queue_contracts_and_commands_are_absent(
    generated_project: Path, project_root: Path
) -> None:
    """The simplified runner does not retain a second durable work system."""
    forbidden = (
        ".agents/contracts/work-item-contract.md",
        ".agents/contracts/work-item-result-v1.schema.json",
        ".agents/contracts/work-item-filesystem-v1.schema.json",
        ".claude/work-item.md",
        "src/agent_work/filesystem.py",
        "src/agent_work/prompt.py",
        "src/agent_work/result.py",
        "src/agent_work/runner.py",
    )

    for root in (project_root, project_root / "template", generated_project):
        assert all(not (root / path).exists() for path in forbidden)


def test_serial_runner_ships_user_systemd_units_with_journal_logging(
    generated_project: Path, project_root: Path
) -> None:
    """Each repository receives a distinct, jittered timer and journal tag."""
    root_service = (
        project_root / "systemd" / "agentic-copier-agent-work.service"
    ).read_text()
    root_timer = (
        project_root / "systemd" / "agentic-copier-agent-work.timer"
    ).read_text()
    template_service = (
        project_root
        / "template"
        / "systemd"
        / "{{ project_slug }}-agent-work.service.jinja"
    ).read_text()
    template_timer = (
        project_root
        / "template"
        / "systemd"
        / "{{ project_slug }}-agent-work.timer.jinja"
    ).read_text()
    generated_service = (
        generated_project / "systemd" / "my_test_project-agent-work.service"
    ).read_text()
    generated_timer = (
        generated_project / "systemd" / "my_test_project-agent-work.timer"
    ).read_text()
    wrapper = (generated_project / "scripts" / "agent-work-systemd-run.sh").read_text()

    assert "SyslogIdentifier=agentic-copier-agent-work" in root_service
    assert (
        "EnvironmentFile=%h/.config/agentic-copier-agent-work/agent-work.env"
        in root_service
    )
    assert 'agent_name="${AGENT_WORK_AGENT:-codex}"' in wrapper
    assert '"${runner[@]}" run agent-work run-next --apply --json' in wrapper
    assert "AGENT_WORK_DIRENV is not executable" in wrapper
    assert "AGENT_WORK_AGENT_PATH is not set" in wrapper
    assert "AGENT_WORK_AGENT_PATH is not executable" in wrapper
    assert (
        "AGENT_WORK_UV=%q"
        in (
            project_root / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
        ).read_text()
    )
    how_to = (
        project_root / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
    ).read_text()
    assert 'agent_name="${AGENT_WORK_AGENT:-codex}"' in how_to
    assert 'agent_path="$(command -v "$agent_name")"' in how_to
    assert "agent must resolve to an executable absolute path" in how_to
    assert "AGENT_WORK_AGENT=%q" in how_to
    assert "AGENT_WORK_AGENT_PATH=%q" in how_to
    assert "AGENT_WORK_DIRENV=%q" in how_to
    assert "glab auth status` reports" in how_to
    assert "Repair unavailable user-journal access" in how_to
    assert "systemd-journal" in how_to
    assert (
        "Failed to connect to bus"
        in (
            project_root / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
        ).read_text()
    )
    for git_flow in (
        project_root / ".claude" / "git-flow.md",
        project_root / "template" / ".claude" / "git-flow.md",
    ):
        assert "--jq" not in git_flow.read_text()
        assert "jq -er" in git_flow.read_text()
    assert "Unit=agentic-copier-agent-work.service" in root_timer
    assert (
        "Environment=AGENT_WORK_LOG_TAG={{ project_slug }}-agent-work"
        in template_service
    )
    assert "Unit={{ project_slug }}-agent-work.service" in template_timer
    assert "SyslogIdentifier=my_test_project-agent-work" in generated_service
    assert (
        "EnvironmentFile=%h/.config/my_test_project-agent-work/agent-work.env"
        in generated_service
    )
    assert "Unit=my_test_project-agent-work.service" in generated_timer
    assert "OnUnitActiveSec=5min" in generated_timer
    assert "RandomizedDelaySec=60s" in generated_timer
    assert "FixedRandomDelay=true" in generated_timer
    assert 'log_tag="${AGENT_WORK_LOG_TAG:-agent-work}"' in wrapper
    assert 'logger -s -t "$log_tag"' in wrapper


def test_shared_workflow_uses_documented_recovery_before_reporting_blockers(
    project_root: Path,
) -> None:
    """Known environment failures use their documented recovery path first."""
    for workflow in (
        project_root / "CLAUDE.md",
        project_root / "template" / "CLAUDE.md",
    ):
        content = workflow.read_text()

        assert "documented recovery" in content
        assert "bounded recovery path is exhausted" in content
        assert "stop\nthat work item" not in content


def test_runtime_has_no_approval_or_merge_operation(project_root: Path) -> None:
    """The serial wrapper can verify MRs but cannot approve or merge them."""
    runtime = "\n".join(
        path.read_text() for path in (project_root / "src" / "agent_work").glob("*.py")
    )
    assert "mr approve" not in runtime
    assert "merge_requests/{mr_iid}/approve" not in runtime
    assert "merge_requests/{mr_iid}/merge" not in runtime
