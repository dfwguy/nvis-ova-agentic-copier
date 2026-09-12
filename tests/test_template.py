# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
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
    "license-check",
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
    assert "validation (9 gates)" not in configuration
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
        assert "validation (9 gates)" in configuration
        assert not (_VALIDATION_JOBS & configuration.keys())
        assert configuration["validation (9 gates)"]["script"] == [
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
    assert content.endswith('"""My Test Project."""\n')
    assert "SPDX-License-Identifier: LicenseRef-NvidiaProprietary" in content


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
        yaml_blocks = re.findall(
            r"^[ \t]*```yaml[ \t]*\n(?P<yaml>.*?)\n^[ \t]*```[ \t]*$",
            project_context_path.read_text(),
            flags=re.DOTALL | re.MULTILINE,
        )
        budgets = [
            parsed
            for block in yaml_blocks
            if isinstance((parsed := yaml.safe_load(block)), dict)
            and set(expected) <= parsed.keys()
        ]

        assert len(budgets) == 1
        budget = budgets[0]
        assert budget == expected
        assert all(type(value) is int for value in budget.values())


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
    """Every workflow role has structured skill metadata and a role file."""
    relative_skill = Path(".agents") / "skills" / skill_name / "SKILL.md"
    skill_path = project_root / relative_skill
    generated_skill = generated_project / relative_skill

    assert skill_path.exists()
    assert generated_skill.exists()
    front_matter = yaml.safe_load(skill_path.read_text().split("---", 2)[1])
    assert front_matter["name"] == skill_name
    assert front_matter["metadata"]["model-tier"] == model_tier
    assert (project_root / ".agents" / "agents" / f"{skill_name}.md").exists()
    assert (generated_project / ".agents" / "agents" / f"{skill_name}.md").exists()


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


def test_agent_loop_wrapper_leaves_recovery_to_the_python_state_machine(
    generated_project: Path,
) -> None:
    """The systemd wrapper only validates configuration and execs one segment."""
    wrapper = (generated_project / "scripts" / "agent-work-systemd-run.sh").read_text()

    assert 'exec "${runner[@]}" run agent-work loop run --apply --json' in wrapper
    assert "logger" not in wrapper
    assert "git stash" not in wrapper
    assert "git switch" not in wrapper
    assert "git pull" not in wrapper


def test_agent_loop_wrapper_requires_apply_for_mutation(
    project_root: Path, tmp_path: Path
) -> None:
    """Bare wrapper execution is a harmless preview."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    marker = tmp_path / "marker"
    _write_command(commands / "uv", 'printf \'%s\\n\' "$*" > "$MARKER"')
    _write_command(commands / "agent", "exit 0")

    environment = os.environ | {
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "MARKER": str(marker),
        "PATH": f"{commands}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "agent-work-systemd-run.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0
    assert marker.read_text() == "run agent-work loop run --json\n"


def test_contributing_md_absent_in_generated_project(generated_project: Path) -> None:
    """Test that CONTRIBUTING.md is NOT generated — its absence triggers setup."""
    assert not (generated_project / "CONTRIBUTING.md").exists()


def test_claude_md_is_a_minimal_agents_md_entry_point(generated_project: Path) -> None:
    """Claude receives shared instructions without a second configuration tree."""
    content = (generated_project / "CLAUDE.md").read_text()
    assert content.endswith("# CLAUDE.md\n\n@AGENTS.md\n")
    assert "SPDX-License-Identifier: LicenseRef-NvidiaProprietary" in content


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


@pytest.mark.parametrize(
    "relative_path",
    (
        "src/agent_work/__init__.py",
        "src/agent_work/classification.py",
        "src/agent_work/domain.py",
        "src/agent_work/gitlab.py",
        "src/agent_work/ledger.py",
        "src/agent_work/migration.py",
        "src/agent_work/models.py",
        "src/agent_work/ports.py",
        "src/agent_work/repository.py",
        "src/agent_work/runtime.py",
        "src/agent_work/serial.py",
        "src/agent_work/state_machine.py",
        "src/agent_work/worker.py",
        "src/agent_work/cli.py",
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


def _bash_block_containing(document: str, required_fragments: tuple[str, ...]) -> str:
    """Return the unique Bash fence containing stable executable signatures."""
    blocks = _bash_blocks(document)
    matches = [
        block
        for block in blocks
        if all(fragment in block for fragment in required_fragments)
    ]
    assert len(matches) == 1, required_fragments
    return matches[0]


def _bash_blocks(document: str) -> tuple[str, ...]:
    """Return the executable Bash fences from one Markdown document."""
    return tuple(
        re.findall(
            r"^[ \t]*```bash[ \t]*\n(?P<script>.*?)\n^[ \t]*```[ \t]*$",
            document,
            flags=re.DOTALL | re.MULTILINE,
        )
    )


def _loads_configured_base_branch(script: str) -> bool:
    """Return whether one runnable block loads the configured service base."""
    exports_canonical_base = bool(
        re.search(
            r"^export AGENT_WORK_BASE_BRANCH=(?:['\"])?agent/work-base(?:['\"])?$",
            script,
            flags=re.MULTILINE,
        )
    )
    sources_service_environment = "set -a" in script and bool(
        re.search(r"^source .*agent-work\.env(?:['\"])?$", script, flags=re.MULTILINE)
    )
    return exports_canonical_base or sources_service_environment


def test_manual_loop_run_examples_load_configured_base_branch(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Every manual loop activation runs with an explicit service base."""
    documents = (
        project_root / "README.md",
        template_dir / "README.md.jinja",
        generated_project / "README.md",
        project_root / "docs/how-to/setup-serial-gitlab-agent-loop.md",
        template_dir / "docs/how-to/setup-serial-gitlab-agent-loop.md",
    )

    for document in documents:
        manual_blocks = tuple(
            block
            for block in _bash_blocks(document.read_text())
            if "uv run agent-work loop run" in block
        )
        assert manual_blocks, document
        assert all(_loads_configured_base_branch(block) for block in manual_blocks)


def test_loop_reference_defines_required_base_before_claiming_work(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """The reference states the base invariant and its pre-claim boundary."""
    relative_path = Path("docs/reference/serial-gitlab-agent-loop.md")
    root_reference = (project_root / relative_path).read_text()

    assert root_reference == (template_dir / relative_path).read_text()
    assert root_reference == (generated_project / relative_path).read_text()
    matching_paragraphs = tuple(
        paragraph
        for paragraph in root_reference.split("\n\n")
        if "AGENT_WORK_BASE_BRANCH" in paragraph
    )
    assert matching_paragraphs
    requirement = " ".join(matching_paragraphs).lower()
    assert "required" in requirement
    assert "agent/work-base" in requirement
    assert "before" in requirement
    assert "claim" in requirement


_READINESS_FENCE_SIGNATURE = (
    "project_json=",
    "default_branch_uri=",
    "approval_rules_json=",
)
_READINESS_SURFACES = (
    pytest.param(".agents/skills/setup-agent-loop/SKILL.md", id="setup-skill"),
    pytest.param("docs/how-to/setup-serial-gitlab-agent-loop.md", id="how-to"),
)


def _readiness_snippet(
    generated_project: Path,
    project_root: Path,
    relative_path: str,
) -> str:
    """Load one parity-checked, executable readiness fence from the product."""
    document = (project_root / relative_path).read_text()

    assert document == (project_root / "template" / relative_path).read_text()
    assert document == (generated_project / relative_path).read_text()
    return _bash_block_containing(document, _READINESS_FENCE_SIGNATURE)


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
    snippet = _bash_block_containing(skill, _READINESS_FENCE_SIGNATURE)

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
@pytest.mark.parametrize("relative_path", _READINESS_SURFACES)
def test_readiness_snippet_allows_only_effective_developer_access(
    generated_project: Path,
    project_root: Path,
    project_access: int | None,
    group_access: int | None,
    relative_path: str,
) -> None:
    """Every published readiness fence permits only effective Developer access."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
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
@pytest.mark.parametrize("relative_path", _READINESS_SURFACES)
def test_readiness_snippet_fails_closed_for_every_other_level(
    generated_project: Path,
    project_root: Path,
    project_access: object,
    group_access: object,
    relative_path: str,
) -> None:
    """Every published fence blocks unknown, low, and privileged access."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
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
        _bash_block_containing(how_to, _READINESS_FENCE_SIGNATURE),
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
            _bash_block_containing(how_to, _READINESS_FENCE_SIGNATURE),
        ),
        approval_rules,
    )
    skill_result = _approval_predicate_succeeds(
        _approval_predicate_from(
            _bash_block_containing(skill, _READINESS_FENCE_SIGNATURE)
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
        _bash_block_containing(how_to, _READINESS_FENCE_SIGNATURE),
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
        _bash_block_containing(how_to, _READINESS_FENCE_SIGNATURE),
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
        _bash_block_containing(how_to, _READINESS_FENCE_SIGNATURE),
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
@pytest.mark.parametrize("relative_path", _READINESS_SURFACES)
def test_readiness_snippet_accepts_complete_evidence(
    generated_project: Path,
    project_root: Path,
    effective_branch: str,
    approval_rules: str,
    relative_path: str,
) -> None:
    """Complete evidence passes through every Developer-readable preflight."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
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
@pytest.mark.parametrize("relative_path", _READINESS_SURFACES)
def test_readiness_snippet_reports_and_blocks_bad_evidence(
    generated_project: Path,
    project_root: Path,
    effective_branch: str,
    approval_rules: str,
    relative_path: str,
) -> None:
    """Every bad safeguard is reported and blocked by every readiness fence."""
    readiness_snippet = _readiness_snippet(
        generated_project,
        project_root,
        relative_path,
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


def test_claude_settings_prohibit_approval_and_merge_in_every_project_copy(
    generated_project: Path, project_root: Path
) -> None:
    """Every Claude frontend denies approval and merge operations."""
    expected_denials = [
        "Bash(glab api*merge_requests*approve*)",
        "Bash(glab api*merge_requests*merge*)",
        "Bash(glab mr approve*)",
        "Bash(glab mr revoke*)",
    ]

    for root in (project_root, project_root / "template", generated_project):
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        assert settings == {"permissions": {"deny": expected_denials}}


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


def test_serial_runner_ships_completion_relative_user_systemd_units(
    generated_project: Path, project_root: Path
) -> None:
    """Each repository receives a distinct completion-relative timer."""
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
    assert 'exec "${runner[@]}" run agent-work loop run --apply --json' in wrapper
    assert "AGENT_WORK_DIRENV is not executable" in wrapper
    assert "AGENT_WORK_AGENT_PATH is not set" in wrapper
    assert "AGENT_WORK_AGENT_PATH is not executable" in wrapper
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
    assert "TimeoutStartSec=90min" in root_service
    assert "TimeoutStartSec=90min" in generated_service
    assert "OnUnitInactiveSec=20min" in generated_timer
    assert "RandomizedDelaySec=60s" in generated_timer
    assert "FixedRandomDelay=true" in generated_timer
    assert "logger" not in wrapper


def test_runtime_has_no_approval_or_merge_operation(project_root: Path) -> None:
    """The serial wrapper can verify MRs but cannot approve or merge them."""
    runtime = "\n".join(
        path.read_text() for path in (project_root / "src" / "agent_work").glob("*.py")
    )
    assert "mr approve" not in runtime
    assert "merge_requests/{mr_iid}/approve" not in runtime
    assert "merge_requests/{mr_iid}/merge" not in runtime


def test_portable_markdown_mirrors_match_generated_output(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Portable workflow mirrors and Copier-rendered Markdown stay synchronized."""
    documentation_paths = {
        path.relative_to(template_dir) for path in (template_dir / "docs").rglob("*.md")
    }
    mirrored_paths = {
        Path("AGENTS.md"),
        Path("CLAUDE.md"),
        *(
            path.relative_to(template_dir)
            for path in (template_dir / ".agents").rglob("*.md")
        ),
        *(
            path.relative_to(template_dir)
            for path in (template_dir / ".gitlab" / "issue_templates").glob("*.md")
        ),
        *(path for path in documentation_paths if path.name != "index.md"),
    }
    copied_paths = {
        *mirrored_paths,
        Path("DIATAXIS.md"),
        *documentation_paths,
    }

    for relative_path in sorted(mirrored_paths):
        root_content = (project_root / relative_path).read_bytes()
        assert root_content == (template_dir / relative_path).read_bytes()

    for relative_path in sorted(copied_paths):
        template_content = (template_dir / relative_path).read_bytes()
        assert template_content == (generated_project / relative_path).read_bytes()
