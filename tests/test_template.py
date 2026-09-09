"""Tests for the copier template."""

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import copier
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


def test_generated_project_requires_debuggable_local_application_logging(
    generated_project: Path,
) -> None:
    """Generated coding agents require debug and local log handlers."""
    guidance = (generated_project / ".agents" / "lang" / "python.md").read_text()

    assert "--debug" in guidance
    assert "logging.FileHandler" in guidance
    assert "local handler" in guidance
    assert "remote syslog" in guidance


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
        ".agents/project.md",
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


_VALIDATION_JOBS = {
    "kingfisher",
    "pytest-py311",
    "pytest-py314",
    "ruff-check",
    "ruff-format",
    "shellcheck",
    "type-check",
    "yamllint",
}


def _ci_configuration(root: Path) -> dict[str, Any]:
    """Load GitLab CI while treating its reference tag as plain YAML."""
    return yaml.safe_load(
        (root / ".gitlab-ci.yml").read_text().replace("!reference ", "")
    )


def _ci_job_script(configuration: dict[str, Any], job_name: str) -> list[str]:
    """Return a job script, following one hidden-template inheritance edge."""
    job = configuration[job_name]
    if "script" in job:
        return job["script"]
    return configuration[job["extends"]]["script"]


def test_ci_exposes_each_validation_gate_as_a_parallel_job(
    project_root: Path,
) -> None:
    """Every root gate has an honest, independently visible status."""
    configuration = _ci_configuration(project_root)
    visible_gates = {
        name
        for name, value in configuration.items()
        if name in _VALIDATION_JOBS and isinstance(value, dict)
    }

    assert visible_gates == _VALIDATION_JOBS
    assert "validation (8 gates)" not in configuration
    for job_name in _VALIDATION_JOBS:
        job = configuration[job_name]
        assert "needs" not in job
        assert "dependencies" not in job
        assert _ci_job_script(configuration, job_name)
        assert "run-ci-checks.sh" not in str(_ci_job_script(configuration, job_name))


def test_ci_avoids_duplicate_push_and_merge_request_pipelines(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """An open merge request suppresses the duplicate branch pipeline."""
    expected_rules = [
        {"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'},
        {
            "if": "$CI_COMMIT_BRANCH && $CI_OPEN_MERGE_REQUESTS",
            "when": "never",
        },
        {"if": "$CI_COMMIT_BRANCH"},
    ]
    for root in (project_root, template_dir, generated_project):
        assert _ci_configuration(root)["workflow"]["rules"] == expected_rules


def test_generated_ci_uses_exact_stable_python_versions(
    generated_project: Path,
) -> None:
    """Each Python test job selects a reproducible stable interpreter."""
    configuration = _ci_configuration(generated_project)
    base = configuration[".base_uv"]

    assert base["image"]["name"] == (
        "ghcr.io/astral-sh/uv:0.9.30-python3.11-bookworm-slim@"
        "sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36"
    )
    before_script = "\n".join(base["before_script"])
    normalized_before_script = " ".join(before_script.replace("\\\n", "").split())
    assert (
        'UV_PROJECT_ENVIRONMENT="$CI_PROJECT_DIR/.venv" '
        "uv sync --locked --all-groups --python 3.11 --no-managed-python"
        in normalized_before_script
    )
    assert "uv python install 3.14.3 --managed-python" in before_script
    assert "uv python install 3.14\n" not in before_script
    assert (
        'UV_PROJECT_ENVIRONMENT="/tmp/ci-venvs/py314" '
        "uv sync --locked --all-groups --python 3.14.3 --managed-python"
        in normalized_before_script
    )


def test_portable_ci_is_mirrored_without_the_private_root_image(
    generated_project: Path,
    template_dir: Path,
) -> None:
    """Rendered projects keep the portable template CI byte-for-byte."""
    template_ci = (template_dir / ".gitlab-ci.yml").read_text()
    generated_ci = (generated_project / ".gitlab-ci.yml").read_text()

    assert template_ci == generated_ci
    assert "gitlab-master.nvidia.com:5005/nvis-ova" not in template_ci
    assert "moby/buildkit" not in template_ci


def test_portable_ci_keeps_one_shared_setup_job(
    generated_project: Path,
    template_dir: Path,
) -> None:
    """Generated CI stays portable without repeating setup across jobs."""
    for root in (template_dir, generated_project):
        configuration = _ci_configuration(root)
        assert "validation (8 gates)" in configuration
        assert not (_VALIDATION_JOBS & configuration.keys())
        assert configuration["validation (8 gates)"]["script"] == [
            "bash tests/helpers/run-ci-checks.sh"
        ]
        assert (root / "tests" / "helpers" / "run-ci-checks.sh").exists()


@pytest.mark.parametrize("shellcheck_fails", (False, True))
def test_ci_shellcheck_job_propagates_scan_failures(
    project_root: Path,
    tmp_path: Path,
    *,
    shellcheck_fails: bool,
) -> None:
    """A ShellCheck violation makes its independently visible job fail."""
    configuration = _ci_configuration(project_root)
    scan_script = next(
        command
        for command in configuration["shellcheck"]["script"]
        if "find ." in command
    )
    checked_file = tmp_path / "check.sh"
    checked_file.write_text("#!/usr/bin/env bash\nprintf 'checked\\n'\n")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invocations = tmp_path / "invocations.log"
    _write_command(
        fake_bin / "shellcheck",
        """printf 'shellcheck %s\\n' "$*" >> "$INVOCATIONS"
if [ "$FAIL_SHELLCHECK" = 1 ] && [ "$1" = ./check.sh ]; then exit 7; fi""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "FAIL_SHELLCHECK": str(int(shellcheck_fails)),
            "INVOCATIONS": str(invocations),
            "PATH": f"{fake_bin}:{environment['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", "-c", scan_script],
        check=False,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        text=True,
    )

    assert (result.returncode != 0) is shellcheck_fails
    assert "shellcheck ./check.sh" in invocations.read_text()


def test_ci_jobs_run_one_direct_gate_command(
    project_root: Path,
) -> None:
    """Each visible root job owns one validation command."""
    expected_commands = {
        "ruff-check": "uv run --no-sync ruff check",
        "ruff-format": "uv run --no-sync ruff format --check",
        "pytest-py311": "uv run --no-sync env -u UV_PROJECT_ENVIRONMENT pytest",
        "pytest-py314": "uv run --no-sync env -u UV_PROJECT_ENVIRONMENT pytest",
        "yamllint": "uv run --no-sync yamllint",
        "kingfisher": "uv run --no-sync kingfisher scan",
        "type-check": "uv run --no-sync ty check",
        "shellcheck": "shellcheck",
    }
    configuration = _ci_configuration(project_root)
    for job_name, command in expected_commands.items():
        assert command in "\n".join(_ci_job_script(configuration, job_name))
    assert "tox run" not in str(configuration)


def test_ci_jobs_publish_independent_reports(
    project_root: Path,
) -> None:
    """Each reporting root job owns only its matching GitLab evidence."""
    configuration = _ci_configuration(project_root)
    pytest_template = configuration[".pytest"]
    assert pytest_template["artifacts"]["reports"] == {
        "junit": "test-results-$TEST_ENVIRONMENT.xml",
        "coverage_report": {
            "coverage_format": "cobertura",
            "path": "coverage-$TEST_ENVIRONMENT.xml",
        },
    }
    assert pytest_template["artifacts"]["when"] == "always"
    for environment in ("py311", "py314"):
        assert configuration[f"pytest-{environment}"]["variables"] == {
            "TEST_ENVIRONMENT": environment,
        }
    assert configuration["ruff-check"]["artifacts"]["reports"] == {
        "codequality": "ruff-code-quality-report.json"
    }
    assert configuration["yamllint"]["artifacts"]["reports"] == {
        "codequality": "yamllint-code-quality-report.json"
    }
    assert configuration["ruff-format"]["artifacts"]["paths"] == ["format-changes.diff"]


def test_root_tox_enforces_coverage_for_each_python(project_root: Path) -> None:
    """Root tox retains its coverage floor for every Python."""
    config = tomllib.loads((project_root / "pyproject.toml").read_text())

    for environment in ("py311", "py314"):
        command = config["tool"]["tox"]["env"][environment]["commands"][0]
        assert "--cov" in command
        assert "--cov-fail-under=85" in command
        assert f"junit_suite_name={environment}" in command


def test_parallel_tox_environments_use_distinct_report_paths(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Parallel tox environments cannot overwrite one another's reports."""
    expected_paths = {
        "py311": {
            "coverage": "coverage-py311.xml",
            "junit": "test-results-py311.xml",
            "data": ".coverage.py311",
        },
        "py314": {
            "coverage": "coverage-py314.xml",
            "junit": "test-results-py314.xml",
            "data": ".coverage.py314",
        },
    }

    for root in (project_root, generated_project):
        config = tomllib.loads((root / "pyproject.toml").read_text())
        for environment, paths in expected_paths.items():
            tox_env = config["tool"]["tox"]["env"][environment]
            command = tox_env["commands"][0]

            assert tox_env["set_env"]["COVERAGE_FILE"] == paths["data"]
            assert f"--cov-report=xml:{paths['coverage']}" in command
            assert f"--junitxml={paths['junit']}" in command
            assert f"junit_suite_name={environment}" in command
            assert "--cov-fail-under=85" in command
            assert "--cov-report=html" not in command

    template_config = (template_dir / "pyproject.toml.jinja").read_text()
    for paths in expected_paths.values():
        for path in paths.values():
            assert path in template_config


def test_parallel_test_artifacts_are_ignored_everywhere(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Environment-specific reports stay out of source control."""
    for root in (project_root, template_dir, generated_project):
        ignore_lines = set((root / ".gitignore").read_text().splitlines())

        assert "coverage-*.xml" in ignore_lines
        assert "test-results-*.xml" in ignore_lines
        assert "format-changes.diff" in ignore_lines
        assert "*-code-quality-report.json" in ignore_lines

    root_ignore = set((project_root / ".gitignore").read_text().splitlines())
    assert "ci-gates.xml" not in root_ignore
    assert "ci-gates.tsv" not in root_ignore
    assert "ci-logs/" not in root_ignore
    assert "code-quality-report.json" not in root_ignore
    template_ignore = set((template_dir / ".gitignore").read_text().splitlines())
    assert "ci-gates.xml" in template_ignore
    assert "ci-gates.tsv" in template_ignore
    assert "ci-logs/" in template_ignore
    assert "code-quality-report.json" in template_ignore


def test_generated_dev_environment_includes_local_tox_matrix(
    generated_project: Path,
) -> None:
    """Generated projects retain tox as the local supported-matrix runner."""
    config = tomllib.loads((generated_project / "pyproject.toml").read_text())

    assert "tox-uv>=1.20" in config["dependency-groups"]["dev"]


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


def test_canonical_project_context_is_rendered(generated_project: Path) -> None:
    """Test that .agents/project.md was rendered with the correct variable values."""
    project_md = generated_project / ".agents" / "project.md"
    assert project_md.exists()
    content = project_md.read_text()
    assert "My Test Project" in content
    assert "A test project" in content
    assert "{{ project_name }}" not in content
    assert not (generated_project / ".claude" / "project.md").exists()


def test_project_context_defines_pipeline_execution_budget_defaults(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Each project owns explicit queue-free pipeline execution budgets."""
    expected = {
        "pipeline_execution_preferred_seconds": 60,
        "pipeline_execution_hard_limit_seconds": 120,
    }

    for root in (project_root, template_dir, generated_project):
        project_context_path = root / ".agents" / "project.md"
        if root == template_dir:
            project_context_path = project_context_path.with_suffix(".md.jinja")
        project_context = project_context_path.read_text()
        budget_block = re.search(
            r"^## Pipeline Performance Budget\n\n"
            r"```yaml\n(?P<budget>.*?)\n```$",
            project_context,
            re.MULTILINE | re.DOTALL,
        )

        assert budget_block is not None
        budget = yaml.safe_load(budget_block.group("budget"))
        assert budget == expected
        assert all(type(value) is int for value in budget.values())
        assert "longest individual job `duration`" in project_context
        assert "runner scheduling must not reject a change" in project_context
        assert "explicit human approval to change" in project_context


def test_generated_project_documents_filesystem_structure_defaults(
    generated_project: Path,
) -> None:
    """Test that a generated project records its required layout and deviations."""
    content = (generated_project / ".agents" / "project.md").read_text()

    assert "src/{{ project_slug }}/" not in content
    assert "src/my_test_project/" in content
    assert "scripts/agent-work-systemd-run.sh" in content
    for field in ("**Language:**", "**Rule:**", "**Actual paths:**", "**Rationale:**"):
        assert field in content


def test_no_jinja_extension_in_output(generated_project: Path) -> None:
    """Test that no .jinja files remain in the generated project."""
    jinja_files = list(generated_project.rglob("*.jinja"))
    assert jinja_files == [], f"Unexpected .jinja files: {jinja_files}"


def test_shared_agent_roles_are_present(generated_project: Path) -> None:
    """Test that portable agent roles are included in the generated project."""
    agents_dir = generated_project / ".agents" / "agents"
    assert agents_dir.is_dir()
    agent_files = list(agents_dir.glob("*.md"))
    assert len(agent_files) > 0


def test_shared_agent_commands_are_absent(generated_project: Path) -> None:
    """Generated projects use portable roles without command wrappers."""
    commands_dir = generated_project / ".agents" / "commands"
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
        ("agent-policy-judge", "fast"),
        ("cli-syntax-judge", "fast"),
        ("diataxis-judge", "fast"),
        ("diataxis-writer", "balanced"),
        ("documentation-finalizer", "fast"),
        ("documentation-writer", "balanced"),
        ("filesystem-structure-judge", "fast"),
        ("findings-triage", "balanced"),
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
    relative_path = f".agents/skills/{skill_name}/SKILL.md"
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
            generated_project / ".agents" / "agents" / f"{skill_name}.md"
        ).read_text()
    )


def test_all_frontends_use_the_shared_project_skill_directory(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Codex and Pi discover native skills; Claude uses one compatibility link."""
    for root in (project_root, template_dir, generated_project):
        assert (root / ".agents" / "skills").is_dir()
        claude_skills = root / ".claude" / "skills"
        assert claude_skills.is_symlink()
        assert claude_skills.resolve() == (root / ".agents" / "skills").resolve()
        assert not (root / ".pi" / "settings.json").exists()


@pytest.mark.parametrize("customized", (False, True))
def test_copier_links_only_an_unchanged_rendered_skill_tree(
    project_root: Path, tmp_path: Path, *, customized: bool
) -> None:
    """Copier links the shared skills without deleting customized legacy files."""
    configuration = yaml.safe_load((project_root / "copier.yaml").read_text())
    canonical = tmp_path / ".agents" / "skills"
    compatibility = tmp_path / ".claude" / "skills"
    shutil.copytree(project_root / ".agents" / "skills", canonical)
    shutil.copytree(canonical, compatibility)
    if customized:
        (compatibility / "local-skill.md").write_text("local\n")

    result = _run_shell_task(_copier_frontend_migration_task(project_root), tmp_path)

    assert "_preserve_symlinks" not in configuration
    assert result.returncode == int(customized)
    assert compatibility.is_symlink() is (not customized)
    if customized:
        assert (compatibility / "local-skill.md").read_text() == "local\n"
        assert "differs from .agents/skills" in result.stderr
    else:
        assert compatibility.readlink() == Path("../.agents/skills")


@pytest.mark.parametrize(
    ("target", "expected_returncode", "expected_message"),
    (
        (Path("../.agents/skills"), 0, None),
        (Path("../local/skills"), 1, "does not point to ../.agents/skills"),
    ),
)
def test_copier_link_task_handles_an_existing_compatibility_link(
    project_root: Path,
    tmp_path: Path,
    target: Path,
    expected_returncode: int,
    expected_message: str | None,
) -> None:
    """Repeating setup keeps the correct link and preserves an unexpected one."""
    canonical = tmp_path / ".agents" / "skills"
    canonical.mkdir(parents=True)
    compatibility = tmp_path / ".claude" / "skills"
    compatibility.parent.mkdir()
    compatibility.symlink_to(target)

    result = _run_shell_task(_copier_frontend_migration_task(project_root), tmp_path)

    assert result.returncode == expected_returncode
    assert compatibility.is_symlink()
    assert compatibility.readlink() == target
    if expected_message is not None:
        assert expected_message in result.stderr


def test_copier_frontend_migration_preserves_legacy_project_context(
    project_root: Path, tmp_path: Path
) -> None:
    """The migration reports customized project context without changing it."""
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    compatibility = tmp_path / ".claude" / "skills"
    compatibility.parent.mkdir()
    compatibility.symlink_to(Path("../.agents/skills"))
    legacy_context = tmp_path / ".claude" / "project.md"
    legacy_context.write_text("custom legacy context\n")

    result = _run_shell_task(_copier_frontend_migration_task(project_root), tmp_path)

    assert result.returncode == 0
    assert legacy_context.read_text() == "custom legacy context\n"
    assert "Reconcile it into .agents/project.md" in result.stderr


def test_copier_update_preserves_legacy_project_context(
    project_root: Path, tmp_path: Path
) -> None:
    """A real Copier update keeps customized context from the legacy frontend path."""
    source = tmp_path / "source"
    source.mkdir()
    _initialize_test_repository(source)

    legacy_configuration = yaml.safe_load((project_root / "copier.yaml").read_text())
    legacy_configuration["_skip_if_exists"] = [
        path
        for path in legacy_configuration["_skip_if_exists"]
        if path != ".agents/project.md"
    ]
    legacy_configuration["_tasks"] = []
    (source / "copier.yaml").write_text(
        yaml.safe_dump(legacy_configuration, sort_keys=False)
    )
    versioned_template = source / "template"
    legacy_template = versioned_template / ".claude"
    legacy_template.mkdir(parents=True)
    shutil.copy(
        project_root / "template" / "{{ _copier_conf.answers_file }}.jinja",
        versioned_template / "{{ _copier_conf.answers_file }}.jinja",
    )
    shutil.copy(
        project_root / "template" / ".agents" / "project.md.jinja",
        legacy_template / "project.md.jinja",
    )
    _commit_template_version(source, "v1")

    output = tmp_path / "existing-project"
    copier.run_copy(
        str(source),
        output,
        data={
            "project_name": "Existing Project",
            "project_slug": "existing_project",
            "description": "Existing project",
        },
        defaults=True,
        overwrite=True,
        unsafe=True,
        skip_tasks=True,
        vcs_ref="v1",
    )
    legacy_context = output / ".claude" / "project.md"
    legacy_context.write_text("custom legacy context\n")
    _initialize_test_repository(output)
    _commit_repository_state(output, "customized legacy project")

    shutil.rmtree(versioned_template)
    (versioned_template / ".agents").mkdir(parents=True)
    (versioned_template / ".claude").mkdir()
    for relative_path in (
        Path("{{ _copier_conf.answers_file }}.jinja"),
        Path("AGENTS.md"),
        Path(".agents/project.md.jinja"),
        Path(".claude/project.md"),
    ):
        destination = versioned_template / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(project_root / "template" / relative_path, destination)
    shutil.copy(project_root / "copier.yaml", source / "copier.yaml")
    _commit_template_version(source, "v2")

    copier.run_update(
        output,
        defaults=True,
        overwrite=True,
        skip_tasks=True,
        unsafe=True,
        vcs_ref="v2",
    )

    assert legacy_context.read_text() == "custom legacy context\n"
    assert (output / ".agents" / "project.md").exists()
    assert ".claude/project.md" not in (output / "AGENTS.md").read_text()


def test_canonical_workflow_does_not_reference_legacy_claude_paths(
    project_root: Path,
) -> None:
    """The canonical tree never routes agents through frontend compatibility files."""
    for root in (project_root, project_root / "template"):
        for path in (root / ".agents").rglob("*.md"):
            content = path.read_text()
            assert "CLAUDE.md" not in content, path
            assert ".claude/" not in content, path


def test_agent_routing_manifest_is_explicit_portable_and_identical(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Every project copy names the exact instructions and skills to activate."""
    root_content = (project_root / "AGENTS.md").read_text()

    assert root_content == (template_dir / "AGENTS.md").read_text()
    assert root_content == (generated_project / "AGENTS.md").read_text()
    assert "\n@.agents/" not in root_content

    for required_route in (
        "Always | `.agents/project.md`",
        "Missing contribution marker | `$repository-setup`",
        "Markdown files | `.agents/guidance/markdown.md`",
        "Coding workflow | `.agents/guidance/tdd.md`",
        "Documentation/policy workflow | `.agents/guidance/documentation.md`",
        "Python files | `.agents/lang/python.md`",
        "Bash files or Bash shebangs | `.agents/lang/bash.md`",
        "Python tests | `.agents/lang/python-testing.md`",
        "Any repository mutation | `$git-flow`",
    ):
        assert required_route in root_content

    assert "`@...` imports are not portable" in root_content
    assert (
        "Do not read conditional guidance when its condition is false" in root_content
    )


def test_shared_agent_instructions_do_not_use_nonportable_nested_imports(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Canonical instructions route conditional reads without recursive @ imports."""
    for root in (project_root, template_dir, generated_project):
        for instruction in (root / ".agents").rglob("*.md"):
            imports = [
                line
                for line in instruction.read_text().splitlines()
                if line.startswith("@.agents/")
            ]
            assert not imports, instruction


def test_generated_manifest_defines_disjoint_coding_and_documentation_workflows(
    generated_project: Path,
) -> None:
    """Generated projects route code and content through disjoint workflows."""
    content = (generated_project / "AGENTS.md").read_text()
    coding = content[
        content.index("## Coding workflow manifest") : content.index(
            "## Documentation and agent-policy workflow manifest"
        )
    ]
    documentation = content[
        content.index(
            "## Documentation and agent-policy workflow manifest"
        ) : content.index("## GitLab publication")
    ]

    for heading in (
        "Applies",
        "Owner",
        "Tier / Codex",
        "Requires",
        "Receipt; rejection route",
    ):
        assert heading in coding
        assert heading in documentation

    coding_skills = (
        "architect",
        "tdd-red",
        "diataxis-writer",
        "tdd-green",
        "nonfunctional-review",
        "tdd-judge",
        "diataxis-judge",
        "judge-quality",
        "judge-test",
        "judge-cicd",
        "tdd-document",
        "cli-syntax-judge",
        "filesystem-structure-judge",
        "tdd-finalizer",
        "pipeline-performance-judge",
        "findings-triage",
    )
    documentation_skills = (
        "git-flow",
        "architect",
        "documentation-writer",
        "diataxis-judge",
        "agent-policy-judge",
        "documentation-finalizer",
        "pipeline-performance-judge",
        "findings-triage",
    )
    for skill in coding_skills:
        assert f"`${skill}`" in coding
    for skill in documentation_skills:
        assert f"`${skill}`" in documentation
    for excluded in (
        "tdd-red",
        "tdd-green",
        "nonfunctional-review",
        "tdd-judge",
        "judge-quality",
        "judge-test",
        "judge-cicd",
        "tdd-finalizer",
        "cli-syntax-judge",
        "filesystem-structure-judge",
    ):
        assert f"`${excluded}`" not in documentation

    assert "Main; never delegate" in coding
    assert "Parallel with `$tdd-red`" in coding
    assert "`not applicable — <reason>`" in content
    assert "Do not route by display name" in content
    assert "Mixed or uncertain" in content
    assert "whole change" in content
    for phase in ("2a `$tdd-red`", "3 `$tdd-green`"):
        phase_row = next(line for line in coding.splitlines() if phase in line)
        assert "| Main; never delegate | Current enabled |" in phase_row


def test_documentation_finalizer_is_content_only_and_portably_mirrored(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """The content finalizer validates policy and prose without test gates."""
    relative_paths = (
        ".agents/agents/documentation-finalizer.md",
        ".agents/skills/documentation-finalizer/SKILL.md",
    )
    for relative_path in relative_paths:
        copies = tuple(
            (root / relative_path).read_text()
            for root in (project_root, template_dir, generated_project)
        )
        assert copies[0] == copies[1] == copies[2]

    finalizer = (project_root / relative_paths[0]).read_text().lower()
    for excluded in ("pytest", "tox", "junit", "coverage", "test-results"):
        assert excluded not in finalizer
    for requirement in (
        "reclassify",
        "applicability record",
        "mirror",
        "source of truth",
    ):
        assert requirement in finalizer


def test_main_red_green_skills_preserve_session_model_continuity(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Main-owned continuity phases never require an impossible model swap."""
    for skill_name in ("tdd-red", "tdd-green"):
        relative_skill = Path(".agents") / "skills" / skill_name / "SKILL.md"
        copies = tuple(
            (root / relative_skill).read_text()
            for root in (project_root, template_dir, generated_project)
        )
        assert copies[0] == copies[1] == copies[2]
        for skill in copies:
            assert "current enabled main-session model" in skill
            assert "do not switch models mid-session" in skill

    relative_role = Path(".agents") / "agents" / "tdd-red.md"
    role_copies = tuple(
        (root / relative_role).read_text()
        for root in (project_root, template_dir, generated_project)
    )
    assert role_copies[0] == role_copies[1] == role_copies[2]
    for red_role in role_copies:
        assert "same main agent" in red_role
        assert "`tdd-green` subagent" not in red_role


def test_scoped_judges_have_explicit_not_applicable_receipts(
    generated_project: Path,
) -> None:
    """Conditional final judges cannot disappear without a recorded decision."""
    workflow = (generated_project / "AGENTS.md").read_text()
    finalizer = (
        generated_project / ".agents" / "agents" / "tdd-finalizer.md"
    ).read_text()

    assert "CLI surface or contract changed" in workflow
    assert "Python/Bash layout or packaging changed" in workflow
    for skill_name in ("cli-syntax-judge", "filesystem-structure-judge"):
        receipt = f"`${skill_name}`: `not applicable — <reason>`"
        assert receipt in finalizer


def test_serial_worker_routes_by_exact_skill_ids(generated_project: Path) -> None:
    """The worker classifies the change before activating exact skill IDs."""
    content = (
        generated_project / ".agents" / "skills" / "work-item-runner" / "SKILL.md"
    ).read_text()

    assert "exact `$skill-id`" in content
    assert "Do not infer a skill from a display name" in content
    assert "Coding workflow manifest" in content
    assert "Documentation and agent-policy workflow manifest" in content
    assert "whole change" in content
    for skill_name in (
        "architect",
        "tdd-red",
        "diataxis-writer",
        "tdd-green",
        "tdd-finalizer",
        "documentation-writer",
        "agent-policy-judge",
        "documentation-finalizer",
    ):
        assert f"`${skill_name}`" in content


def test_nonfunctional_review_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects review shared timed test results before finalization."""
    agent = generated_project / ".agents" / "agents" / "nonfunctional-review.md"
    workflow = (generated_project / "AGENTS.md").read_text()

    assert agent.exists()
    assert "test-results.xml" in agent.read_text()
    assert "time" in agent.read_text()
    assert "performance" in agent.read_text().lower()
    assert "non-trivial changes" in workflow
    assert (
        "nonfunctional-review"
        in (generated_project / ".agents" / "agents" / "tdd-finalizer.md").read_text()
    )


def test_diataxis_documentation_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the Diataxis documentation gates."""
    docs_process = generated_project / "DIATAXIS.md"
    writer = generated_project / ".agents" / "agents" / "diataxis-writer.md"
    judge = generated_project / ".agents" / "agents" / "diataxis-judge.md"

    assert docs_process.exists()
    assert "Canonical layout" in docs_process.read_text()
    assert "Required front matter" in docs_process.read_text()
    assert "Landing pages" in docs_process.read_text()
    assert "Documentation change report" in docs_process.read_text()
    assert writer.exists()
    assert judge.exists()
    assert "Implementation Judge" in judge.read_text()
    assert (
        "README.md"
        in (generated_project / ".agents" / "agents" / "tdd-document.md").read_text()
    )
    assert "docs/" in writer.read_text()


def test_generated_remediation_workflow_and_documentation_index_rule(
    generated_project: Path,
) -> None:
    """Test that projects can remediate themselves and keep documentation navigable."""
    remediation_agent = generated_project / ".agents" / "agents" / "remediate.md"
    documentation_process = generated_project / "DIATAXIS.md"
    documentation_index = generated_project / "docs" / "index.md"

    assert remediation_agent.exists()
    assert "Make this repo meet our standards" in remediation_agent.read_text()
    assert "baseline" in remediation_agent.read_text()
    assert "Landing pages" in documentation_process.read_text()
    assert "Required front matter" in documentation_process.read_text()
    assert documentation_index.exists()
    assert "how-to/index.md" in documentation_index.read_text()


def test_cli_syntax_judge_workflow_is_generated(generated_project: Path) -> None:
    """Test that generated projects include the CLI syntax design gate."""
    agent = generated_project / ".agents" / "agents" / "cli-syntax-judge.md"
    workflow = (generated_project / "AGENTS.md").read_text()

    assert agent.exists()
    assert "https://clig.dev" in agent.read_text()
    assert "--json" in agent.read_text()
    assert "--apply" in agent.read_text()
    assert "non-trivial changes" in workflow


def test_portable_git_flow_is_present(generated_project: Path) -> None:
    """Test that the git-flow lifecycle doc is included so the orchestrator loads it."""
    git_flow = generated_project / ".agents" / "skills" / "git-flow" / "SKILL.md"
    assert git_flow.exists()
    content = git_flow.read_text()
    assert "projects/$GITLAB_PROJECT_ID/merge_requests" in content
    assert "git checkout -b" in content
    assert "git checkout main" in content


def test_portable_git_flow_verifies_git_and_gitlab_access(
    generated_project: Path,
) -> None:
    """Test that the generated workflow verifies both remote access paths."""
    content = (
        generated_project / ".agents" / "skills" / "git-flow" / "SKILL.md"
    ).read_text()

    assert "## Git and GitLab access preflight" in content
    assert "git pull --ff-only" in content
    assert 'glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"' in content
    assert "direnv exec . glab api" in content
    assert "path-based `404`" in content
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
    """New projects retain the portable repository lock-down skill."""
    setup_repo = (
        generated_project / ".agents" / "skills" / "repository-setup" / "SKILL.md"
    )
    assert setup_repo.exists()
    content = setup_repo.read_text()
    assert "name: repository-setup" in content
    assert "protected_branches" in content
    assert "approval_rules" in content
    assert "/approvals" in content, (
        "self-approval block must target /projects/<id>/approvals, not the bare project endpoint"
    )
    assert "direnv exec . glab api user" in content
    assert "non-empty `username`" in content


def test_setup_repo_publishes_its_bootstrap_change(generated_project: Path) -> None:
    """Test that lock-down setup cannot strand CONTRIBUTING.md on local main."""
    content = (
        generated_project / ".agents" / "skills" / "repository-setup" / "SKILL.md"
    ).read_text()
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


def test_agent_loop_wrapper_returns_dedicated_checkout_to_its_base_branch(
    generated_project: Path,
) -> None:
    """The systemd pass wrapper resets its dedicated checkout after a pass."""
    content = (
        generated_project / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
    ).read_text()
    wrapper = (generated_project / "scripts" / "agent-work-systemd-run.sh").read_text()

    assert (
        "Every bounded pass attempts to leave the dedicated checkout on its base branch"
        in content
    )
    assert "agent-work recovery" in content
    assert "git stash apply <SHA>" in content
    assert "AGENT_WORK_BASE_BRANCH" in content
    assert 'base_branch="${AGENT_WORK_BASE_BRANCH:-main}"' in wrapper
    assert 'git switch "$base_branch"' in wrapper
    assert "git pull --ff-only" in wrapper
    assert "pass failed (exit $pass_status); attempting checkout recovery" in wrapper
    assert "git stash push --include-untracked" in wrapper
    assert "preserved worker changes in recovery stash" in wrapper
    assert wrapper.index("pass_status=0") < wrapper.rindex(
        "return_checkout_to_base_branch"
    )


def test_agent_loop_wrapper_preserves_failed_worker_changes_before_recovery(
    project_root: Path, tmp_path: Path
) -> None:
    """A failed pass stashes worker output before resetting its checkout."""
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    commands = tmp_path / "commands"
    commands.mkdir()
    _write_command(commands / "uv", "exit 1")
    _write_command(commands / "agent", "exit 0")
    _write_command(commands / "logger", "cat")

    for command in (
        ["git", "init", "--bare", str(remote)],
        ["git", "init", "--initial-branch", "main", str(checkout)],
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    for command in (
        ["git", "-C", str(checkout), "config", "user.name", "Test User"],
        ["git", "-C", str(checkout), "config", "user.email", "test@example.com"],
        ["git", "-C", str(checkout), "remote", "add", "origin", str(remote)],
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    (checkout / "tracked.txt").write_text("baseline\n")
    subprocess.run(
        ["git", "-C", str(checkout), "add", "tracked.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "commit", "-m", "baseline"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "push", "-u", "origin", "main"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(checkout), "switch", "-c", "agent/issue-123"],
        check=True,
        capture_output=True,
        text=True,
    )
    (checkout / "tracked.txt").write_text("worker output\n")
    (checkout / "untracked.txt").write_text("worker output\n")

    environment = os.environ | {
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "AGENT_WORK_BASE_BRANCH": "main",
        "PATH": f"{commands}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "agent-work-systemd-run.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 1
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "main"
    )
    assert (
        subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        == ""
    )
    stash_files = subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "stash",
            "show",
            "--include-untracked",
            "--name-only",
            "stash@{0}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert {"tracked.txt", "untracked.txt"} <= set(stash_files.splitlines())


def test_agent_work_worker_exception_overrides_generic_publication_rules(
    generated_project: Path,
) -> None:
    """Generated workers do not inherit the ordinary-agent publication duty."""
    required = "Serial agent-work worker exception"

    assert required in (generated_project / "AGENTS.md").read_text()


def test_repository_setup_interviews_self_approval(generated_project: Path) -> None:
    """The setup procedure asks about self-approval for single-user projects."""
    content = (
        generated_project / ".agents" / "skills" / "repository-setup" / "SKILL.md"
    ).read_text()
    assert "AskUserQuestion" in content, (
        "setup-repo must interview the user about self-approval"
    )
    assert "Block self-approval" in content
    assert "Allow self-approval" in content
    assert "single-user" in content


def test_contributing_md_absent_in_generated_project(generated_project: Path) -> None:
    """Test that CONTRIBUTING.md is NOT generated — its absence triggers setup."""
    assert not (generated_project / "CONTRIBUTING.md").exists()


def test_generated_agents_md_loads_lockdown_skill_when_marker_is_missing(
    generated_project: Path,
) -> None:
    """The generated workflow routes a missing marker to the setup skill."""
    content = (generated_project / "AGENTS.md").read_text()
    assert (
        "grep -Fq 'All changes go through a merge request.' CONTRIBUTING.md" in content
    )
    assert ".agents/skills/repository-setup/SKILL.md" in content
    assert "protected_branches" not in content


def test_generated_agent_workflow_supports_bash(generated_project: Path) -> None:
    """Test that generated agent guidance selects Bash-specific validation."""
    workflow = (generated_project / "AGENTS.md").read_text()
    bash_conventions = generated_project / ".agents" / "lang" / "bash.md"
    judge = generated_project / ".agents" / "agents" / "tdd-judge.md"
    finalizer = generated_project / ".agents" / "agents" / "tdd-finalizer.md"

    assert bash_conventions.exists()
    assert "ShellCheck" in bash_conventions.read_text()
    assert ".agents/lang/bash.md" in workflow
    assert "bash -n" in judge.read_text()
    assert "shellcheck" in finalizer.read_text()


def test_generated_language_guidance_requires_a_separate_code_directory(
    generated_project: Path,
) -> None:
    """Test that generated Python and Bash guidance sets layout boundaries."""
    python_guidance = (generated_project / ".agents" / "lang" / "python.md").read_text()
    bash_guidance = (generated_project / ".agents" / "lang" / "bash.md").read_text()

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
        project_root / ".agents" / "lang",
        template_dir / ".agents" / "lang",
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
    agent = generated_project / ".agents" / "agents" / "filesystem-structure-judge.md"
    workflow = (generated_project / "AGENTS.md").read_text()
    finalizer = (
        generated_project / ".agents" / "agents" / "tdd-finalizer.md"
    ).read_text()

    assert agent.exists()
    assert "src/" in agent.read_text()
    assert "bin/" in agent.read_text()
    assert "Filesystem structure deviations" in agent.read_text()
    assert "non-trivial changes" in workflow
    assert "`filesystem-structure-judge` approves" in finalizer


def test_pipeline_performance_judge_is_the_single_live_pipeline_owner(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """One generated agent owns complete post-publication reconciliation."""
    relative_agent = ".agents/agents/pipeline-performance-judge.md"
    relative_skill = ".agents/skills/pipeline-performance-judge/SKILL.md"

    for root in (project_root, template_dir, generated_project):
        agent = root / relative_agent
        skill = root / relative_skill

        assert agent.exists()
        assert skill.exists()
        agent_content = agent.read_text()
        skill_content = skill.read_text()

        assert "projects/$GITLAB_PROJECT_ID/merge_requests" in agent_content
        assert "queued_duration" in agent_content
        assert "one complete bounded reconciliation invocation" in skill_content
        assert "The agent owns the entire live-pipeline lifecycle" in agent_content
        assert "Do not push, retry, cancel, approve, or merge" in agent_content
        assert "model-tier: balanced" in skill_content


def test_pipeline_agent_enforces_the_project_owned_execution_budget(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Only the live-pipeline owner interprets configurable execution limits."""
    preferred_field = "pipeline_execution_preferred_seconds"
    hard_field = "pipeline_execution_hard_limit_seconds"

    for root in (project_root, template_dir, generated_project):
        agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()
        normalized_agent = " ".join(agent.split())

        for requirement in (
            ".agents/project.md",
            "Pipeline Performance Budget",
            preferred_field,
            hard_field,
            "finite numeric",
            "booleans",
            "greater than zero",
            "preferred budget must be less than or equal to the hard limit",
            "longest executed terminal job `duration`",
            "Do not apply the budget to the pipeline wall-clock span",
            "Budgeted execution duration",
            "their absent durations do not invalidate measured executed jobs",
            "queue time is excluded",
            "Preferred target: met",
            "Hard limit: passed",
            "Hard limit: exceeded",
            "Direct feedback: Reject → `$tdd-green`; no issue required",
            "needs verification",
        ):
            assert requirement in normalized_agent

        assert re.search(
            r"longest job duration.*less than or equal to.*preferred", agent
        )
        assert re.search(r"longest job duration.*greater than.*hard limit", agent)
        assert not re.search(rf"{preferred_field}:\s*10\b", agent)
        assert not re.search(rf"{hard_field}:\s*60\b", agent)


def test_pipeline_budget_has_no_unrelated_workflow_plumbing(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Other roles remain unaware of the project-owned pipeline thresholds."""
    fields = (
        "pipeline_execution_preferred_seconds",
        "pipeline_execution_hard_limit_seconds",
    )
    unrelated_paths = (
        Path("AGENTS.md"),
        Path("README.md"),
        Path(".agents/agents/judge-cicd.md"),
        Path(".agents/agents/findings-triage.md"),
        Path(".agents/agents/tdd-finalizer.md"),
        Path(".agents/skills/git-flow/SKILL.md"),
    )

    for root in (project_root, template_dir, generated_project):
        for relative_path in unrelated_paths:
            path = root / relative_path
            if root == template_dir and relative_path == Path("README.md"):
                path = path.with_suffix(".md.jinja")
            content = path.read_text()
            for field in fields:
                assert field not in content, relative_path


def test_pipeline_agent_owns_bounded_interruptible_wait_until_terminal(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Pending progress stays inside the single bounded agent invocation."""
    for root in (project_root, template_dir, generated_project):
        agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()
        for requirement in (
            "15-minute default deadline",
            "30-second interval",
            "at least once every 60 seconds",
            "interruptible wait",
            "final snapshot",
        ):
            assert requirement in agent

        assert "Pending observations are progress" in agent
        assert "do not return control" in agent
        assert "wait deadline reached" in agent
        assert "while true" not in agent.lower()
        assert "sleep 30" not in agent.lower()


def test_pipeline_snapshot_classifies_statuses_and_uses_exact_source_sha(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Every snapshot selects the MR pipeline by exact source SHA and raw status."""
    for root in (project_root, template_dir, generated_project):
        agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()

        for status in (
            "created",
            "waiting_for_resource",
            "preparing",
            "pending",
            "running",
            "success",
            "failed",
            "canceled",
            "skipped",
            "manual",
            "scheduled",
            "unknown status",
        ):
            assert f"`{status}`" in agent

        for endpoint in (
            "merge_requests/<iid>",
            "merge_requests/<iid>/pipelines?per_page=100",
            "pipelines/<pipeline-id>",
            "pipelines/<pipeline-id>/jobs?include_retried=true&per_page=100",
            "jobs/<job-id>/trace",
        ):
            assert endpoint in agent

        assert (
            "newest pipeline whose SHA exactly matches the expected source SHA" in agent
        )
        assert "Never substitute the newest `main` pipeline" in agent


def test_pipeline_agent_returns_a_bounded_resumable_timeout(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """A bounded timeout is final for one complete reconciliation attempt."""
    for root in (project_root, template_dir, generated_project):
        pipeline_agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()
        assert "Reconciliation: nonterminal" in pipeline_agent
        assert (
            "Later action: start one new complete reconciliation invocation"
            in pipeline_agent
        )
        assert "wait deadline reached" in pipeline_agent
        assert "human action required" in pipeline_agent
        assert "stale source" in pipeline_agent
        assert "API failure" in pipeline_agent


def test_terminal_pipeline_receipt_reports_wall_elapsed_and_optimization(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Terminal handoffs report actual user wait time and improvement evidence."""
    for root in (project_root, template_dir, generated_project):
        pipeline_agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()
        for requirement in (
            "Pipeline elapsed (wall clock)",
            "Timestamp interval",
            "finished_at - created_at",
            "GitLab duration",
            "GitLab queued duration",
            "Optimization assessment",
            "Baseline comparison",
            "Pipeline elapsed: unavailable",
        ):
            assert requirement in pipeline_agent

        assert "Never add job `queued_duration` values" in pipeline_agent


def test_ci_judge_reviews_static_configuration_without_live_pipeline_access(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """The CI judge validates local config without inspecting live pipelines."""
    for root in (project_root, template_dir, generated_project):
        ci_judge = (root / ".agents" / "agents" / "judge-cicd.md").read_text()

        assert "repository configuration only" in ci_judge
        assert "Do not query GitLab pipelines" in ci_judge
        assert "Config sync" in ci_judge
        assert "Static shape" in ci_judge
        assert "glab api" not in ci_judge
        assert "queued_duration" not in ci_judge
        assert "Pipeline Performance Judge" not in ci_judge


def test_findings_triage_files_prepublication_minor_findings_after_publication(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Ordinary triage owns only prepublication judge findings."""
    relative_agent = ".agents/agents/findings-triage.md"
    relative_skill = ".agents/skills/findings-triage/SKILL.md"

    for root in (project_root, template_dir, generated_project):
        agent = root / relative_agent
        skill = root / relative_skill
        assert agent.exists()
        assert skill.exists()
        agent_content = agent.read_text()
        assert "projects/$GITLAB_PROJECT_ID/issues" in agent_content
        assert "Bug template" in agent_content
        assert "Phase 4–6" in agent_content
        assert "Minor" in agent_content
        assert "deduplicate" in agent_content.lower()
        assert "pipeline" not in agent_content.lower()
        assert "model-tier: balanced" in skill.read_text()

    for judge_name in (
        "judge-quality.md",
        "judge-test.md",
        "judge-cicd.md",
    ):
        content = (project_root / ".agents" / "agents" / judge_name).read_text()
        assert "Findings Triage" in content


def test_pipeline_agent_uses_stable_non_racing_loop_feedback(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Deferred CI findings use ordinary issues with a stable fingerprint."""
    for root in (project_root, template_dir, generated_project):
        agent = (
            root / ".agents" / "agents" / "pipeline-performance-judge.md"
        ).read_text()

        for requirement in (
            "projects/$GITLAB_PROJECT_ID/issues",
            "agent-pipeline-feedback",
            "fingerprint: <normalized-category>|<stable-scope>|<stable-summary>",
            "Bug template",
            "created",
            "reused",
            "unfiled",
            "loop-ready fallback",
            "No pipeline feedback issue required",
        ):
            assert requirement in agent

        assert "search open GitLab issues" in agent
        assert "Never hand pipeline findings to `$findings-triage`" in agent
        assert "runner/platform" in agent
        assert "Direct feedback: Reject → `$tdd-green`; no issue required" in agent
        assert "ordinary issue" in agent

        marker = re.search(
            r"<!-- agent-pipeline-feedback\n(?P<body>.*?)-->",
            agent,
            flags=re.DOTALL,
        )
        assert marker is not None
        marker_body = marker.group("body")
        assert "project_id: <numeric-project-id>" in marker_body
        assert "fingerprint:" in marker_body
        for unstable_identity in (
            "merge_request_iid",
            "source_branch",
            "source_sha",
            "pipeline-id",
            "job-id",
        ):
            assert unstable_identity not in marker_body

        assert "compare exact marker text" in agent
        assert "MR/source/pipeline/job evidence outside the marker" in agent
        assert agent.index("search open GitLab issues") < agent.index(
            "create one GitLab Bug template"
        )


def test_live_pipeline_internals_are_not_plumbed_through_other_roles(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Unrelated roles neither query nor interpret the live GitLab pipeline."""
    unrelated_paths = (
        ".agents/skills/git-flow/SKILL.md",
        ".agents/skills/findings-triage/SKILL.md",
        ".agents/skills/judge-cicd/SKILL.md",
        ".agents/skills/judge-quality/SKILL.md",
        ".agents/skills/judge-test/SKILL.md",
        ".agents/skills/tdd-finalizer/SKILL.md",
        ".agents/agents/findings-triage.md",
        ".agents/agents/judge-cicd.md",
        ".agents/agents/judge-quality.md",
        ".agents/agents/judge-test.md",
        ".agents/agents/tdd-finalizer.md",
    )
    forbidden_internals = (
        "queued_duration",
        "finished_at - created_at",
        "30-second interval",
        "wait deadline reached",
        "merge_requests/<iid>/pipelines",
    )

    for root in (project_root, template_dir, generated_project):
        for relative_path in unrelated_paths:
            content = (root / relative_path).read_text()
            for forbidden in forbidden_internals:
                assert forbidden not in content, relative_path

            if relative_path != ".agents/agents/judge-cicd.md":
                assert "pipeline" not in content.lower(), relative_path

        ci_judge = (root / ".agents" / "agents" / "judge-cicd.md").read_text()
        assert "Do not query GitLab pipelines" in ci_judge
        assert "repository configuration only" in ci_judge
        for live_ownership in (
            "projects/$GITLAB_PROJECT_ID/pipelines",
            "interruptible wait",
            "Pipeline elapsed",
            "Pipeline Performance Judge",
        ):
            assert live_ownership not in ci_judge

        workflow = (root / "AGENTS.md").read_text()
        assert "exactly one invocation owns the complete phase" in workflow
        assert "Phase 4–6 Minor findings" in workflow


def test_explanation_routes_postpublication_roles_from_the_published_mr(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """The explanation shows both independent postpublication handoffs."""
    for root in (project_root, template_dir, generated_project):
        explanation = (root / "docs/explanation/agentic-workflow.md").read_text()

        assert "mr --> pipeline[Pipeline Performance Judge]" in explanation
        assert "mr -->|Phase 4–6 Minor findings| triage[Findings Triage]" in explanation
        assert "pipeline --> human[Human approval and merge]" in explanation
        assert "triage --> human" in explanation
        assert "final_gates -. actionable Minor findings .-> triage" not in explanation


def test_mr_remediation_issues_resume_the_originating_merge_request(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that a judge finding can safely drive a later update to its open MR."""
    for root in (project_root, template_dir, generated_project):
        triage = (root / ".agents" / "agents" / "findings-triage.md").read_text()
        assert "agent-mr-remediation" in triage
        assert "source branch" in triage
        assert "Closes #<triage-issue>" in triage

    for root in (project_root, template_dir):
        runner = (
            root / ".agents" / "skills" / "work-item-runner" / "SKILL.md"
        ).read_text()

        assert "agent-mr-remediation" in runner
        assert "existing merge request" in runner
        assert "Do not create a second merge request" in runner


def test_source_and_template_workflows_wire_the_filesystem_structure_judge(
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that the source workflow and Copier output retain the same gate."""
    for root in (project_root, template_dir):
        workflow = (root / "AGENTS.md").read_text()
        finalizer = (root / ".agents" / "agents" / "tdd-finalizer.md").read_text()
        agent = root / ".agents" / "agents" / "filesystem-structure-judge.md"

        assert agent.exists()
        assert "non-trivial changes" in workflow
        assert "`filesystem-structure-judge` approves" in finalizer


def test_filesystem_structure_judge_defines_concrete_layout_and_deviation_rules(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that every shipped filesystem judge enforces the full contract."""
    judge_paths = (
        generated_project / ".agents" / "agents" / "filesystem-structure-judge.md",
        project_root / ".agents" / "agents" / "filesystem-structure-judge.md",
        template_dir / ".agents" / "agents" / "filesystem-structure-judge.md",
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


def test_claude_md_is_a_minimal_agents_md_entry_point(generated_project: Path) -> None:
    """Claude receives shared instructions without a second configuration tree."""
    assert (
        generated_project / "CLAUDE.md"
    ).read_text() == "# CLAUDE.md\n\n@AGENTS.md\n"


def test_agents_md_present(generated_project: Path) -> None:
    """Test that AGENTS.md provides a native Codex workflow."""
    agents_md = generated_project / "AGENTS.md"
    assert agents_md.exists()
    content = agents_md.read_text()
    assert ".claude/skills -> ../.agents/skills" in content
    assert ".agents/lang/python.md" in content
    assert "gpt-5.6-sol" in content
    assert "gpt-5.6-terra" in content
    assert "gpt-5.6-luna" in content
    assert "GITLAB_PROJECT_ID" in content


def test_canonical_python_guidance_is_present(generated_project: Path) -> None:
    """Test that .agents/lang/python.md is generated with Python conventions."""
    python_md = generated_project / ".agents" / "lang" / "python.md"
    assert python_md.exists()
    content = python_md.read_text()
    assert "uv run pytest" in content
    assert "Dependency Inversion" in content
    assert "pytest.mark.parametrize" in content


def test_generated_agents_md_has_language_index(generated_project: Path) -> None:
    """Test that AGENTS.md references the language conventions file."""
    content = (generated_project / "AGENTS.md").read_text()
    assert ".agents/lang/python.md" in content
    assert "files you expect to change" in content


def test_canonical_python_testing_guidance_is_present(generated_project: Path) -> None:
    """Test that the detailed testing reference is generated as a canonical language file."""
    testing_md = generated_project / ".agents" / "lang" / "python-testing.md"
    assert testing_md.exists()
    content = testing_md.read_text()
    assert "pytest-subprocess" in content
    assert "requests-mock" in content


def test_cursor_testing_rule_references_canonical_file(generated_project: Path) -> None:
    """Test the Cursor test rule is a thin @-import of the canonical file, not a duplicate."""
    rule = generated_project / "tests" / ".cursor" / "rules" / "testing.mdc"
    assert rule.exists()
    content = rule.read_text()
    assert "@.agents/lang/python-testing.md" in content
    # The old per-topic duplicated rules must be gone.
    rules_dir = generated_project / "tests" / ".cursor" / "rules"
    assert not (rules_dir / "subprocess.mdc").exists()
    assert not (rules_dir / "modify_tests.mdc").exists()


def test_shared_guidance_is_present(generated_project: Path) -> None:
    """Test that language-agnostic guidance files are generated in .agents/guidance/."""
    guidance = generated_project / ".agents" / "guidance"
    assert (guidance / "tdd.md").exists()
    assert (guidance / "documentation.md").exists()
    assert (guidance / "markdown.md").exists()
    assert "Red" in (guidance / "tdd.md").read_text()


def test_general_cursor_rules_reference_canonical_files(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Cursor imports canonical guidance only for files matching each rule."""
    for root in (project_root, template_dir, generated_project):
        rules = root / ".cursor" / "rules"
        assert "@.agents/guidance/tdd.md" in (rules / "tdd.mdc").read_text()
        markdown_rule = (rules / "markdown.mdc").read_text()
        assert "@.agents/guidance/markdown.md" in markdown_rule
        assert "alwaysApply: false" in markdown_rule


def test_generated_agents_md_has_general_guidance_index(
    generated_project: Path,
) -> None:
    """Test that shared guidance remains available from the canonical layout."""
    content = (generated_project / ".agents" / "guidance" / "tdd.md").read_text()
    assert "Red" in content


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
    assert ".agents/project.md" in configuration["_skip_if_exists"]
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
        generated_project / ".agents" / "agents" / "filesystem-structure-judge.md",
        project_root / ".agents" / "agents" / "filesystem-structure-judge.md",
        template_dir / ".agents" / "agents" / "filesystem-structure-judge.md",
    )

    for judge_path in judge_paths:
        content = judge_path.read_text().lower()

        assert "<package>/" in content
        assert "src/<package>/" in content
        assert "split" in content
        assert "overall: reject" in content
        assert "migrate" in content


def test_root_and_template_project_notes_declare_structure_compliance(
    project_root: Path,
    template_dir: Path,
) -> None:
    """Test that every project records deviations from the standard layout."""
    root_notes = (project_root / ".agents" / "project.md").read_text()
    template_notes = (template_dir / ".agents" / "project.md.jinja").read_text()

    assert "Filesystem structure deviations" in root_notes
    for field in ("**Language:**", "**Rule:**", "**Actual paths:**", "**Rationale:**"):
        assert field in root_notes
    assert "src/{{ project_slug }}/" in template_notes
    assert "scripts/agent-work-systemd-run.sh" in template_notes
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


def _copier_frontend_migration_task(project_root: Path) -> str:
    """Return the guarded Claude compatibility migration task."""
    return next(
        task for task in _copier_tasks(project_root) if ".claude/skills" in task
    )


def _run_shell_task(
    task: str, working_directory: Path
) -> subprocess.CompletedProcess[str]:
    """Run one Copier shell task in an isolated directory."""
    return subprocess.run(
        ["/bin/sh", "-c", task],
        check=False,
        capture_output=True,
        cwd=working_directory,
        text=True,
    )


def _write_command(path: Path, body: str) -> None:
    """Create an executable POSIX-shell command stub."""
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _commit_template_version(source: Path, tag: str) -> None:
    """Commit and tag one version of a temporary Copier source."""
    _commit_repository_state(source, tag)
    _run_test_git(source, "tag", tag)


def _initialize_test_repository(path: Path) -> None:
    """Initialize a temporary Git repository without user templates or hooks."""
    _run_test_git(path, "init", "--initial-branch", "main", "--template=")


def _commit_repository_state(path: Path, message: str) -> None:
    """Commit the complete state of a temporary Git repository."""
    for command in (("add", "--all"), ("commit", "-m", message)):
        _run_test_git(path, *command)


def _run_test_git(path: Path, *arguments: str) -> None:
    """Run Git with deterministic identity, signing, hooks, and config boundaries."""
    environment = os.environ | {
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        check=True,
        capture_output=True,
        cwd=path,
        env=environment,
        text=True,
    )


def _run_copier_tasks(
    tasks: list[str], environment: dict[str, str], working_directory: Path
) -> list[str]:
    """Run Copier tasks in order, stopping after the first failed task."""
    results = []
    for task in tasks:
        result = subprocess.run(
            ["/bin/sh", "-c", task],
            check=False,
            capture_output=True,
            cwd=working_directory,
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

    results = _run_copier_tasks(_copier_tasks(project_root), environment, tmp_path)

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
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    environment = os.environ | {"MARKER": str(marker), "PATH": str(commands)}

    results = _run_copier_tasks(_copier_tasks(project_root), environment, tmp_path)

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
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    environment = os.environ | {"MARKER": str(marker), "PATH": str(commands)}

    results = _run_copier_tasks(_copier_tasks(project_root), environment, tmp_path)

    assert len(results) == len(_copier_tasks(project_root))
    assert marker.read_text().splitlines() == ["git init", "uv sync", "uv run"]


def _workflow_paths(root: Path) -> tuple[Path, ...]:
    """Return the maintained workflow instructions for a project root."""
    candidates = (
        root / "AGENTS.md",
        root / "CLAUDE.md",
        root / "README.md",
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

        for relative_path in ("AGENTS.md", ".agents/skills/git-flow/SKILL.md"):
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
    content = (project_root / root / ".agents" / "agents" / "architect.md").read_text()

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
def test_review_agents_preserve_minor_findings_for_terminal_triage(
    project_root: Path, command_name: str
) -> None:
    """Test that approving reviews hand Minor records to Findings Triage."""
    for root in (project_root, project_root / "template"):
        content = (root / ".agents" / "agents" / command_name).read_text()

        for requirement in (
            "Findings Triage",
            "standard output format",
            "Bug-template",
            "GitLab issues",
            "Markdown backlog file as a fallback",
            "Do not claim an issue was created",
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
        content = (
            (root / ".agents" / "skills" / "git-flow" / "SKILL.md").read_text().lower()
        )

        assert not any(gate in content for gate in forbidden_gates)
        assert "unrelated open merge requests" in content
        assert "non-blocking" in content
        assert "same branch" in content
        assert "dependency" in content
        assert "resource conflict" in content
        assert "clean working tree" in content
        assert "git pull --ff-only" in content
        assert "latest pipeline" not in content
        assert "no direct-to-`main` path" in content
        assert "ai must never approve or merge" in content
        assert "report the result as `awaiting_human` and end the active run" in content
        assert "release the active checkout" in content
        assert "branch-specific continuation from `main`" in content
        assert "source_branch=$intended_branch" in content
        assert 'git checkout "$intended_branch"' in content
        assert "source_branch=$(git rev-parse --abbrev-ref head)" in content
        assert "only once the mr is merged" in content
        assert content.index("only once the mr is merged") < (
            content.index("git branch -d claude/<slug>")
        )
        assert content.index("source_branch=$intended_branch") < content.index(
            'git checkout -b "$intended_branch"'
        )

    readme_content = (project_root / "README.md").read_text()
    assert "docs/how-to/setup-serial-gitlab-agent-loop.md" in readme_content


@pytest.mark.parametrize(
    "relative_path",
    (
        ".agents/skills/git-flow/SKILL.md",
        ".agents/skills/repository-setup/SKILL.md",
        ".agents/agents/architect.md",
        ".agents/agents/judge-quality.md",
        ".agents/agents/judge-test.md",
        ".agents/agents/tdd-finalizer.md",
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
    relative_path = ".agents/skills/git-flow/SKILL.md"
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
    expected_denials = [
        "Bash(glab api*merge_requests*approve*)",
        "Bash(glab api*merge_requests*merge*)",
        "Bash(glab mr approve*)",
        "Bash(glab mr revoke*)",
    ]

    for root in (project_root, project_root / "template", generated_project):
        instructions = (root / "AGENTS.md").read_text()
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        runtime = "\n".join(
            path.read_text() for path in (root / "src" / "agent_work").glob("*.py")
        )

        assert "Never approve or merge" in instructions
        assert settings == {"permissions": {"deny": expected_denials}}
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


@pytest.mark.parametrize(
    "relative_path",
    (
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
    assert re.search(r"`?40`?\s+or greater", root_document)
    assert re.search(r"constrained Developer\s+credential", root_document)
    assert re.search(r"(?:block `--apply`|BLOCK --apply)", root_document)


def test_legacy_local_queue_contracts_and_commands_are_absent(
    generated_project: Path, project_root: Path
) -> None:
    """The simplified runner does not retain a second durable work system."""
    forbidden = (
        ".agents/contracts/work-item-contract.md",
        ".agents/contracts/work-item-result-v1.schema.json",
        ".agents/contracts/work-item-filesystem-v1.schema.json",
        ".agents/work-item.md",
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
        project_root / ".agents" / "skills" / "git-flow" / "SKILL.md",
        project_root / "template" / ".agents" / "skills" / "git-flow" / "SKILL.md",
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
    assert "OnUnitActiveSec=20min" in generated_timer
    assert "RandomizedDelaySec=60s" in generated_timer
    assert "FixedRandomDelay=true" in generated_timer
    assert 'log_tag="${AGENT_WORK_LOG_TAG:-agent-work}"' in wrapper
    assert 'logger -s -t "$log_tag"' in wrapper


def test_runtime_has_no_approval_or_merge_operation(project_root: Path) -> None:
    """The serial wrapper can verify MRs but cannot approve or merge them."""
    runtime = "\n".join(
        path.read_text() for path in (project_root / "src" / "agent_work").glob("*.py")
    )
    assert "mr approve" not in runtime
    assert "merge_requests/{mr_iid}/approve" not in runtime
    assert "merge_requests/{mr_iid}/merge" not in runtime
