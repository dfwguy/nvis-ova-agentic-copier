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


def test_root_envrc_is_ignored_in_source_template_and_rendered_project(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """The checkout credential source remains local and untracked everywhere."""
    for root in (project_root, template_dir, generated_project):
        assert "/.envrc" in (root / ".gitignore").read_text().splitlines()


def test_agent_loop_policy_and_guidance_remain_team_portable(
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Portable policy and guidance avoid source identity and alternate modes."""
    source_specific_path = Path("gitlab", "nvis-ova", "token").as_posix()
    alternate_mode_terms = tuple(
        separator.join(words)
        for words in (
            ("convenience", "mode"),
            ("selected", "mode"),
            ("gated", "alternative", "mode"),
        )
        for separator in (" ", "-")
    )
    relevant_paths = tuple(
        path
        for root in (project_root, template_dir, generated_project)
        for directory in (root / ".agents", root / "docs")
        for path in directory.rglob("*.md")
    ) + (
        project_root / "AGENTS.md",
        project_root / "README.md",
        template_dir / "AGENTS.md",
        template_dir / "README.md.jinja",
        generated_project / "AGENTS.md",
        generated_project / "README.md",
    )
    content_by_path = {path: path.read_text() for path in relevant_paths}
    violations = {
        "source-specific password-store path": [
            str(path)
            for path, content in content_by_path.items()
            if source_specific_path in content
        ],
        "alternate credential mode framing": [
            str(path)
            for path, content in content_by_path.items()
            if any(term in content.casefold() for term in alternate_mode_terms)
        ],
    }

    assert violations == {
        "source-specific password-store path": [],
        "alternate credential mode framing": [],
    }
    for root in (project_root, template_dir, generated_project):
        policy = " ".join(content_by_path[root / "AGENTS.md"].split())
        assert (
            "deterministic controller, AI workflow, and required nested agents"
            in policy
        )
        assert "same scoped Developer GitLab credential" in policy
        assert (
            "Humans provide product and policy inputs and alone approve or merge."
            in policy
        )
    guidance = " ".join(content_by_path.values()).replace("\n", " ")
    assert "locally configured `GITLAB_TOKEN_PASS_PATH`" in guidance
    assert 'GITLAB_TOKEN="$(pass show "$GITLAB_TOKEN_PASS_PATH")"' in guidance


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
        "junit": [
            "test-results-$TEST_ENVIRONMENT-shard-1.xml",
            "test-results-$TEST_ENVIRONMENT-shard-2.xml",
        ],
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
    """The systemd wrapper keeps recovery in Python while coordinating activations."""
    wrapper = (generated_project / "scripts" / "agent-work-systemd-run.sh").read_text()

    assert (
        'authenticated "$AGENT_WORK_UV" run agent-work loop run --apply --json'
        in wrapper
    )
    assert (
        'exec authenticated "$AGENT_WORK_UV" run agent-work loop run --apply --json'
        not in wrapper
    )
    assert "logger" not in wrapper
    assert "git stash" not in wrapper
    assert "git switch" not in wrapper
    assert "git pull" not in wrapper


def test_agent_loop_wrapper_requires_apply_for_mutation(
    project_root: Path, tmp_path: Path
) -> None:
    """Bare wrapper execution is a harmless preview."""
    checkout, environment, marker, glab_marker = _wrapper_checkout_environment(tmp_path)
    before_head = _test_git_output(checkout, "rev-parse", "HEAD")

    result = _run_wrapper(project_root, environment)

    assert result.returncode == 0, result.stderr
    assert _test_git_output(checkout, "rev-parse", "HEAD") == before_head
    assert marker.read_text() == "one\nrun agent-work loop run --json\n"
    assert json.loads(glab_marker.read_text()) == {
        "host": "gitlab.example.test",
        "project_id": "123",
    }


@pytest.mark.parametrize(
    ("configured_direnv", "expected_error"),
    (
        (None, "AGENT_WORK_DIRENV must be an absolute executable path"),
        ("direnv", "AGENT_WORK_DIRENV must be an absolute executable path"),
        (
            "absolute-nonexecutable",
            "AGENT_WORK_DIRENV must be an absolute executable path",
        ),
    ),
)
def test_agent_loop_wrapper_requires_an_absolute_executable_direnv(
    project_root: Path,
    tmp_path: Path,
    configured_direnv: str | None,
    expected_error: str,
) -> None:
    """The service cannot run authenticated boundaries outside direnv."""
    _, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    if configured_direnv is None:
        environment.pop("AGENT_WORK_DIRENV")
    elif configured_direnv == "direnv":
        environment["AGENT_WORK_DIRENV"] = configured_direnv
    elif configured_direnv == "absolute-nonexecutable":
        candidate = tmp_path / "commands" / "direnv-disabled"
        candidate.write_text("#!/bin/sh\nexit 0\n")
        environment["AGENT_WORK_DIRENV"] = str(candidate)

    result = _run_wrapper(project_root, environment)

    assert result.returncode == 2
    assert expected_error in result.stderr
    assert not marker.exists()


def test_agent_loop_wrapper_requires_an_absolute_timeout_executable(
    project_root: Path, tmp_path: Path
) -> None:
    """The wrapper fails safely before any worker work without its timeout seam."""
    _, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    environment.pop("AGENT_WORK_TIMEOUT", None)

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 2
    assert "AGENT_WORK_TIMEOUT must be an absolute executable path" in result.stderr
    assert not marker.exists()


def test_agent_loop_wrapper_bounds_housekeeping_without_capping_applied_worker(
    project_root: Path, tmp_path: Path
) -> None:
    """Only short control-plane work uses the injected TERM-to-KILL timeout."""
    _, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    timeout_marker = tmp_path / "timeout-calls"
    timeout = tmp_path / "commands" / "timeout"
    _write_command(
        timeout,
        'printf "%s\\n" "$*" >> "$TIMEOUT_MARKER"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --signal=TERM|--kill-after=30s) shift ;;\n"
        "    120s) shift; break ;;\n"
        "    *) exit 97 ;;\n"
        "  esac\n"
        "done\n"
        'exec "$@"',
    )
    environment |= {
        "AGENT_WORK_TIMEOUT": str(timeout),
        "TIMEOUT_MARKER": str(timeout_marker),
    }

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    timeout_calls = timeout_marker.read_text().splitlines()
    assert any(
        call.startswith(
            f"--signal=TERM --kill-after=30s 120s {environment['AGENT_WORK_GIT']}"
        )
        for call in timeout_calls
    )
    assert any(
        call.startswith(
            f"--signal=TERM --kill-after=30s 120s {environment['AGENT_WORK_DIRENV']}"
        )
        for call in timeout_calls
    )
    assert any(environment["AGENT_WORK_GLAB"] in call for call in timeout_calls)
    assert all(environment["AGENT_WORK_UV"] not in call for call in timeout_calls)
    assert marker.read_text() == "one\nrun agent-work loop run --apply --json\n"


@pytest.mark.parametrize("timeout_exit", (124, 137))
def test_agent_loop_wrapper_stops_on_housekeeping_timeout_before_worker(
    project_root: Path, tmp_path: Path, timeout_exit: int
) -> None:
    """A timeout exit is a bounded safe stop, never an attempted worker run."""
    _, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    timeout = tmp_path / "commands" / "timeout"
    _write_command(timeout, f"exit {timeout_exit}")
    environment["AGENT_WORK_TIMEOUT"] = str(timeout)

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 2
    assert "housekeeping command timed out" in result.stderr
    assert len(result.stderr) < 512
    assert not marker.exists()


@pytest.mark.parametrize("helper_succeeds", (True, False))
def test_agent_loop_wrapper_checks_nonmutating_https_publication_readiness(
    project_root: Path, tmp_path: Path, *, helper_succeeds: bool
) -> None:
    """The runner starts only after the scrubbed glab-helper dry-run succeeds."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    commands = tmp_path / "commands"
    git = commands / "git"
    push_marker = tmp_path / "push-dry-run.json"
    _run_test_git(
        checkout,
        "remote",
        "set-url",
        "origin",
        "https://gitlab.example.test/group/project.git",
    )
    project_json = json.dumps(
        {
            "id": 123,
            "default_branch": "main",
            "http_url_to_repo": "https://gitlab.example.test/group/project.git",
            "ssh_url_to_repo": "git@gitlab.example.test:group/project.git",
        }
    )
    _write_command(
        commands / "glab",
        'if [ "$1" = auth ]; then exit "${GLAB_HELPER_EXIT:-0}"; fi\n'
        f"printf '%s\\n' '{project_json}'",
    )
    _write_command(
        git,
        'case " $* " in\n'
        '  *" push --dry-run "*)\n'
        '    printf \'{"arguments":"%s","helper0":"%s","helper1":"%s","prompt":"%s"}\\n\' "$*" "$GIT_CONFIG_VALUE_0" "$GIT_CONFIG_VALUE_1" "$GIT_TERMINAL_PROMPT" > "$PUSH_MARKER"\n'
        "    set -- $GIT_CONFIG_VALUE_1\n"
        '    "$@"\n'
        "    ;;\n"
        '  *" ls-remote "*)\n'
        '    sha="$("$REAL_GIT" -C "$AGENT_WORK_CHECKOUT" rev-parse HEAD)"\n'
        '    printf "%s\\trefs/heads/main\\n" "$sha"\n'
        "    ;;\n"
        '  *) exec "$REAL_GIT" "$@" ;;\n'
        "esac",
    )
    environment |= {
        "AGENT_WORK_GIT": str(git),
        "PUSH_MARKER": str(push_marker),
        "REAL_GIT": str(shutil.which("git")),
        "GLAB_HELPER_EXIT": "0" if helper_succeeds else "23",
        "GIT_CONFIG_VALUE_0": "ambient-helper-must-be-cleared",
    }

    before_head = _test_git_output(checkout, "rev-parse", "HEAD")
    result = _run_wrapper(project_root, environment)

    assert push_marker.exists()
    assert _test_git_output(checkout, "rev-parse", "HEAD") == before_head
    readiness = json.loads(push_marker.read_text())
    assert "push --dry-run" in readiness["arguments"]
    assert readiness["helper0"] == ""
    assert readiness["helper1"] == f"{commands / 'glab'} auth git-credential"
    assert readiness["prompt"] == "0"
    if helper_succeeds:
        assert result.returncode == 0, result.stderr
        assert marker.exists()
    else:
        assert result.returncode != 0
        assert not marker.exists()


@pytest.mark.parametrize("arguments", ((), ("--apply",)))
@pytest.mark.parametrize("helper_succeeds", (True, False))
def test_agent_loop_wrapper_direnv_readiness_restores_scrubbed_controls(
    project_root: Path,
    tmp_path: Path,
    arguments: tuple[str, ...],
    *,
    helper_succeeds: bool,
) -> None:
    """Direnv cannot replace the target or Git credential controls at readiness."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    (checkout / ".envrc").write_text("# test boundary\n")
    commands = tmp_path / "commands"
    readiness = tmp_path / "readiness.json"
    _write_command(
        commands / "direnv",
        "export GITLAB_HOST=poison.example GITLAB_PROJECT_ID=999 GIT_CONFIG_VALUE_1=poison GIT_ASKPASS=poison GIT_CONFIG_PARAMETERS=credential.helper=poison AGENT_WORK_GIT=/poison/git AGENT_WORK_GLAB=/poison/glab\n"
        'shift 2\nexec "$@"',
    )
    _write_command(
        commands / "glab",
        'if [ "$1" = auth ]; then [ "${HELPER_OK:-0}" = 1 ] || { echo secret-helper-output >&2; exit 23; }; exit 0; fi\n'
        'printf "%s\\n" \'{"id":123,"default_branch":"main","http_url_to_repo":"https://gitlab.example.test/group/project.git"}\'',
    )
    _write_command(
        commands / "git",
        'case " $* " in *" push --dry-run "*) printf \'{"host":"%s","project":"%s","helper":"%s","askpass":"%s","parameters":"%s"}\\n\' "$GITLAB_HOST" "$GITLAB_PROJECT_ID" "$GIT_CONFIG_VALUE_1" "$GIT_ASKPASS" "${GIT_CONFIG_PARAMETERS:-}" > "$READINESS"; set -- $GIT_CONFIG_VALUE_1; "$@" ;; *" ls-remote "*) sha="$("$REAL_GIT" --git-dir "$LOCAL_ORIGIN" rev-parse refs/heads/main)"; printf "%s\\trefs/heads/main\\n" "$sha" ;; *fetch*) for part in "$@"; do refspec="$part"; done; exec "$REAL_GIT" -C "$AGENT_WORK_CHECKOUT" fetch --no-tags --no-recurse-submodules "$LOCAL_ORIGIN" "$refspec" ;; *) exec "$REAL_GIT" "$@" ;; esac',
    )
    _write_command(
        commands / "uv",
        'printf \'{"git":"%s","glab":"%s"}\\n\' "$AGENT_WORK_GIT" "$AGENT_WORK_GLAB" > "$UV_MARKER"\nprintf \'{"schema_version":1,"command":"loop.run","applied":true,"outcome":"idle","stop_reason":"idle"}\\n\'',
    )
    environment |= {
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "AGENT_WORK_GLAB": str(commands / "glab"),
        "AGENT_WORK_GIT": str(commands / "git"),
        "READINESS": str(readiness),
        "HELPER_OK": "1" if helper_succeeds else "0",
    }
    result = _run_wrapper(project_root, environment, *arguments)
    assert readiness.exists(), result.stderr
    observed = json.loads(readiness.read_text())
    assert observed == {
        "host": "gitlab.example.test",
        "project": "123",
        "helper": f"{commands / 'glab'} auth git-credential",
        "askpass": "/bin/false",
        "parameters": "",
    }
    if helper_succeeds:
        assert result.returncode == 0, result.stderr
        assert json.loads(marker.read_text()) == {
            "git": str(commands / "git"),
            "glab": str(commands / "glab"),
        }
    else:
        assert result.returncode != 0
        assert "secret-helper-output" not in result.stderr + result.stdout
        assert not marker.exists()


@pytest.mark.parametrize("arguments", ((), ("--apply",)))
def test_agent_loop_wrapper_uses_one_checkout_env_for_authenticated_boundaries(
    project_root: Path,
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    """API, HTTPS Git, readiness, and Python share one evaluated envrc."""
    checkout, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    configured_pass_path = "teams/example-project/agent-token"
    (checkout / ".envrc").write_text(
        "export GITLAB_HOST=gitlab.example.test\n"
        "export GITLAB_PROJECT_ID=123\n"
        f"export GITLAB_TOKEN_PASS_PATH={configured_pass_path}\n"
        'export GITLAB_TOKEN="$(pass show "$GITLAB_TOKEN_PASS_PATH")"\n'
    )
    commands = tmp_path / "commands"
    stage_marker = tmp_path / "authenticated-stages"
    _write_command(
        commands / "pass",
        '[ "$1" = show ] && [ "$2" = "$EXPECTED_PASS_PATH" ] || exit 21\n'
        'printf "%s" "$TEST_SENTINEL"',
    )
    _write_command(
        commands / "direnv",
        'test "$1" = exec || exit 22\n'
        'checkout="$2"\n'
        "shift 2\n"
        'PATH="$COMMANDS:$PATH"\n'
        "export PATH\n"
        'set -a; . "$checkout/.envrc"; set +a\n'
        "export GITLAB_HOST=poison.example.test GITLAB_PROJECT_ID=999\n"
        "export GIT_CONFIG_VALUE_1=poison GIT_ASKPASS=poison\n"
        "export GIT_CONFIG_PARAMETERS=credential.helper=poison\n"
        'exec "$@"',
    )
    project_json = json.dumps(
        {
            "id": 123,
            "default_branch": "main",
            "http_url_to_repo": "https://gitlab.example.test/group/project.git",
        }
    )
    _write_command(
        commands / "glab",
        '[ "$GITLAB_TOKEN" = "$TEST_SENTINEL" ] || exit 23\n'
        'case "$1" in\n'
        '  api) [ "$GITLAB_HOST:$GITLAB_PROJECT_ID" = "gitlab.example.test:123" ] || exit 24; printf "api:ok\\n" >> "$STAGE_MARKER"; '
        f"printf '%s\\n' '{project_json}' ;;\n"
        '  auth) printf "helper:ok\\n" >> "$STAGE_MARKER" ;;\n'
        "  *) exit 25 ;;\n"
        "esac",
    )
    _write_command(
        commands / "git",
        "check_boundary() {\n"
        '  [ "$GITLAB_TOKEN" = "$TEST_SENTINEL" ] &&\n'
        '  [ "$GITLAB_HOST:$GITLAB_PROJECT_ID" = "gitlab.example.test:123" ] &&\n'
        '  [ "$GIT_CONFIG_VALUE_0" = "" ] &&\n'
        '  [ "$GIT_CONFIG_VALUE_1" = "$AGENT_WORK_GLAB auth git-credential" ] &&\n'
        '  [ "$GIT_ASKPASS" = /bin/false ] &&\n'
        '  [ -z "${GIT_CONFIG_PARAMETERS:-}" ]\n'
        "}\n"
        'case " $* " in\n'
        '  *" ls-remote "*) check_boundary || exit 26; printf "ls-remote:ok\\n" >> "$STAGE_MARKER"; sha="$("$REAL_GIT" --git-dir "$LOCAL_ORIGIN" rev-parse refs/heads/main)"; printf "%s\\trefs/heads/main\\n" "$sha" ;;\n'
        '  *" fetch "*) check_boundary || exit 27; printf "fetch:ok\\n" >> "$STAGE_MARKER"; for part in "$@"; do refspec="$part"; done; exec "$REAL_GIT" -C "$AGENT_WORK_CHECKOUT" fetch --no-tags --no-recurse-submodules "$LOCAL_ORIGIN" "$refspec" ;;\n'
        '  *" push --dry-run "*) check_boundary || exit 28; printf "readiness:ok\\n" >> "$STAGE_MARKER"; set -- $GIT_CONFIG_VALUE_1; "$@" ;;\n'
        '  *) exec "$REAL_GIT" "$@" ;;\n'
        "esac",
    )
    _write_command(
        commands / "uv",
        '[ "$GITLAB_TOKEN" = "$TEST_SENTINEL" ] || exit 29\n'
        '[ "$GITLAB_HOST:$GITLAB_PROJECT_ID" = "gitlab.example.test:123" ] || exit 30\n'
        '[ "$GIT_CONFIG_VALUE_1" = "$AGENT_WORK_GLAB auth git-credential" ] || exit 31\n'
        '[ "$GIT_ASKPASS" = /bin/false ] || exit 32\n'
        '[ -z "${GIT_CONFIG_PARAMETERS:-}" ] || exit 33\n'
        'printf "runner:ok\\n" >> "$STAGE_MARKER"\n'
        'printf \'{"schema_version":1,"command":"loop.run","applied":true,"outcome":"idle","stop_reason":"idle"}\\n\'',
    )
    environment |= {
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "AGENT_WORK_GIT": str(commands / "git"),
        "AGENT_WORK_GLAB": str(commands / "glab"),
        "AGENT_WORK_UV": str(commands / "uv"),
        "COMMANDS": str(commands),
        "EXPECTED_PASS_PATH": configured_pass_path,
        "STAGE_MARKER": str(stage_marker),
        "TEST_SENTINEL": "test-only-shared-value",
    }

    result = _run_wrapper(project_root, environment, *arguments)

    assert result.returncode == 0, result.stderr
    stages = stage_marker.read_text().splitlines()
    expected_transport = "fetch:ok" if arguments else "ls-remote:ok"
    assert stages.count("api:ok") == 1
    assert expected_transport in stages
    assert "readiness:ok" in stages
    assert "helper:ok" in stages
    assert "runner:ok" in stages
    assert environment["TEST_SENTINEL"] not in stage_marker.read_text()
    assert environment["TEST_SENTINEL"] not in result.stdout + result.stderr


def test_agent_loop_wrapper_fast_forwards_before_applied_worker(
    project_root: Path, tmp_path: Path
) -> None:
    """An applied service run uses the newly fetched default-branch content."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    _advance_wrapper_default_branch(checkout, "two")
    hook_marker = tmp_path / "post-merge-ran"
    hook = checkout / ".git" / "hooks" / "post-merge"
    hook.parent.mkdir(parents=True, exist_ok=True)
    _write_command(hook, 'touch "$HOOK_MARKER"')
    environment["HOOK_MARKER"] = str(hook_marker)

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert (checkout / "version").read_text() == "two\n"
    assert marker.read_text() == "two\nrun agent-work loop run --apply --json\n"
    assert not hook_marker.exists()


def test_agent_loop_wrapper_drains_terminal_handoffs_while_work_remains_eligible(
    project_root: Path, tmp_path: Path
) -> None:
    """Terminal handoffs repeatedly preview and start distinct eligible activations."""
    checkout, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(
        tmp_path,
        environment,
        (
            _applied_report("complete"),
            _preview_report("eligible"),
            _applied_report("blocked"),
            _preview_report("eligible"),
            _applied_report("awaiting_human"),
            _preview_report("idle"),
        ),
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
    ]
    assert _test_git_output(checkout, "rev-parse", "--show-toplevel") == str(checkout)


def test_agent_loop_wrapper_reemits_each_validated_report_once(
    project_root: Path, tmp_path: Path
) -> None:
    """The service journal retains every validated applied and preview report."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    reports = (_applied_report("complete"), _preview_report("idle"))
    _scripted_wrapper_worker(tmp_path, environment, reports)

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert all(result.stdout.count(report) == 1 for report in reports)


@pytest.mark.parametrize(
    "stop_reason", ("awaiting_human", "blocked", "failed", "complete")
)
def test_agent_loop_wrapper_terminal_handoff_previews_once_then_yields_on_idle(
    project_root: Path, tmp_path: Path, stop_reason: str
) -> None:
    """Every terminal issue result makes one fresh authoritative eligibility check."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(
        tmp_path,
        environment,
        (_applied_report(stop_reason), _preview_report("idle")),
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
    ]


def test_agent_loop_wrapper_skips_open_dependent_work_after_terminal_handoff(
    project_root: Path, tmp_path: Path
) -> None:
    """An authoritative idle preview for open dependencies starts no second activation."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(
        tmp_path,
        environment,
        (_applied_report("complete"), _preview_report("idle")),
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
    ]


@pytest.mark.parametrize(
    "stop_reason",
    ("idle", "pipeline_wait", "retry_wait", "repair", "time_limit", "segment_limit"),
)
def test_agent_loop_wrapper_nonterminal_result_yields_without_eligibility_polling(
    project_root: Path, tmp_path: Path, stop_reason: str
) -> None:
    """Wait, recovery, and budget results return control to the timer without spinning."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(
        tmp_path, environment, (_applied_report(stop_reason),)
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json"
    ]


def test_agent_loop_wrapper_refreshes_advanced_default_branch_before_next_activation(
    project_root: Path, tmp_path: Path
) -> None:
    """A continuation verifies and fast-forwards the configured base before applying."""
    checkout, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    writer = checkout.parent / "future-writer"
    subprocess.run(
        ["git", "clone", str(checkout.parent / "origin.git"), str(writer)],
        check=True,
        capture_output=True,
        text=True,
    )
    (writer / "version").write_text("two\n")
    _commit_repository_state(writer, "prepare future default branch")
    _run_test_git(writer, "push", "origin", "HEAD:future")
    advance = tmp_path / "advance-default-branch"
    _write_command(
        advance,
        'sha="$("$REAL_GIT" --git-dir "$LOCAL_ORIGIN" rev-parse refs/heads/future)"\n'
        'exec "$REAL_GIT" --git-dir "$LOCAL_ORIGIN" update-ref refs/heads/main "$sha"',
    )
    environment["UV_ADVANCE"] = str(advance)
    calls = _scripted_wrapper_worker(
        tmp_path,
        environment,
        (
            _applied_report("complete"),
            _preview_report("eligible"),
            _applied_report("complete"),
            _preview_report("idle"),
        ),
        advance_after_first_apply=True,
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode == 0, result.stderr
    assert (
        calls.read_text().splitlines()[2]
        == "two|run agent-work loop run --apply --json"
    )
    assert (checkout / "version").read_text() == "two\n"


@pytest.mark.parametrize(
    "report",
    (
        "not-json",
        '{"schema_version":1,"command":"loop.run","applied":true,"outcome":"activation_complete","stop_reason":"unknown"}',
        '{"schema_version":1,"command":"loop.run","applied":true,"outcome":"idle","stop_reason":"complete"}',
    ),
)
def test_agent_loop_wrapper_fails_closed_for_malformed_or_unexpected_reports(
    project_root: Path, tmp_path: Path, report: str
) -> None:
    """The coordinator does not continue after an untrusted worker report."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(tmp_path, environment, (report,))

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json"
    ]


@pytest.mark.parametrize(
    "report",
    (
        "not-json",
        '{"schema_version":1,"command":"loop.run","applied":true,"outcome":"eligible"}',
        '{"schema_version":1,"command":"loop.run","applied":false,"outcome":"blocked"}',
    ),
)
def test_agent_loop_wrapper_fails_closed_for_invalid_eligibility_preview(
    project_root: Path, tmp_path: Path, report: str
) -> None:
    """A terminal handoff never continues through an untrusted preview."""
    _, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    calls = _scripted_wrapper_worker(
        tmp_path, environment, (_applied_report("complete"), report)
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert calls.read_text().splitlines() == [
        "one|run agent-work loop run --apply --json",
        "one|run agent-work loop run --json",
    ]


@pytest.mark.parametrize("state", ("dirty", "diverged"))
def test_agent_loop_wrapper_blocks_unrefreshable_checkout_before_worker(
    project_root: Path, tmp_path: Path, state: str
) -> None:
    """Dirty or diverged dedicated checkouts fail closed without invoking UV."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    if state == "dirty":
        (checkout / "untracked").write_text("dirty\n")
    else:
        (checkout / "version").write_text("local\n")
        _commit_repository_state(checkout, "local divergence")

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()


def test_agent_loop_wrapper_rejects_configured_base_branch_mismatch(
    project_root: Path, tmp_path: Path
) -> None:
    """The service may only refresh the explicitly configured agent branch."""
    _, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    environment["AGENT_WORK_BASE_BRANCH"] = "agent/another-base"

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    (
        "reported_project_id",
        "reported_default_branch",
        "reported_origin",
        "upstream_override",
    ),
    (
        ("124", "main", None, None),
        ("123", "missing", None, None),
        ("123", "main", "https://wrong.example.test/project.git", None),
        ("123", "main", None, "origin/not-main"),
    ),
)
def test_agent_loop_wrapper_rejects_untrusted_gitlab_or_checkout_identity(
    project_root: Path,
    tmp_path: Path,
    reported_project_id: str,
    reported_default_branch: str,
    reported_origin: str | None,
    upstream_override: str | None,
) -> None:
    """Project, default, origin, and upstream evidence must all agree."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(
        tmp_path,
        reported_project_id=reported_project_id,
        reported_default_branch=reported_default_branch,
        reported_origin=reported_origin,
        upstream_override=upstream_override,
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()
    assert checkout.exists()


def test_agent_loop_wrapper_preview_blocks_behind_checkout_without_mutation(
    project_root: Path, tmp_path: Path
) -> None:
    """Preview verifies remote freshness but never fetches, merges, or runs stale work."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    _advance_wrapper_default_branch(checkout, "two")
    before_head = _test_git_output(checkout, "rev-parse", "HEAD")
    before_remote_ref = _test_git_output(checkout, "rev-parse", "origin/main")
    before_status = _test_git_output(
        checkout, "status", "--porcelain", "--untracked-files=all"
    )

    result = _run_wrapper(project_root, environment)

    assert result.returncode != 0
    assert _test_git_output(checkout, "rev-parse", "HEAD") == before_head
    assert _test_git_output(checkout, "rev-parse", "origin/main") == before_remote_ref
    assert (
        _test_git_output(checkout, "status", "--porcelain", "--untracked-files=all")
        == before_status
    )
    assert (checkout / "version").read_text() == "one\n"
    assert not marker.exists()


def test_agent_loop_wrapper_preview_does_not_require_local_tracking_ref(
    project_root: Path, tmp_path: Path
) -> None:
    """A current preview relies on ls-remote, not a local origin default ref."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    _run_test_git(checkout, "update-ref", "-d", "refs/remotes/origin/main")

    result = _run_wrapper(project_root, environment)

    assert result.returncode == 0, result.stderr
    assert marker.exists()
    assert not (checkout / ".git" / "refs" / "remotes" / "origin" / "main").exists()


@pytest.mark.parametrize("operation", ("fetch", "merge"))
def test_agent_loop_wrapper_stops_on_refresh_command_failure(
    project_root: Path, tmp_path: Path, operation: str
) -> None:
    """A failed fetch or fast-forward blocks the worker rather than recovering unsafely."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    if operation == "merge":
        _advance_wrapper_default_branch(checkout, "two")
    commands = tmp_path / "commands"
    _write_command(
        commands / "git-failure",
        f'for argument in "$@"; do\n'
        f'  if [ "$argument" = "{operation}" ]; then exit 72; fi\n'
        "done\n"
        'exec "$(command -v git)" "$@"',
    )
    environment["AGENT_WORK_GIT"] = str(commands / "git-failure")

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()


def test_agent_loop_wrapper_fails_closed_when_refresh_lock_is_held(
    project_root: Path, tmp_path: Path
) -> None:
    """Only one wrapper can refresh and invoke the worker for a checkout at once."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    git_dir = Path(
        _test_git_output(checkout, "rev-parse", "--path-format=absolute", "--git-dir")
    )
    lock_path = git_dir / "agent-work-systemd.lock"
    lock_holder = subprocess.Popen(
        ["flock", "-n", str(lock_path), "/bin/sh", "-c", "read ignored"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert lock_holder.poll() is None
        result = _run_wrapper(project_root, environment, "--apply")
    finally:
        assert lock_holder.stdin is not None
        lock_holder.stdin.close()
        lock_holder.wait(timeout=5)

    assert result.returncode != 0
    assert not marker.exists()


def test_agent_loop_wrapper_rejects_target_filter_attributes_before_refresh(
    project_root: Path, tmp_path: Path
) -> None:
    """A target filter attribute blocks refresh even without a local filter driver."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    writer = checkout.parent / "writer"
    subprocess.run(
        ["git", "clone", str(checkout.parent / "origin.git"), str(writer)],
        check=True,
        capture_output=True,
        text=True,
    )
    (writer / ".gitattributes").write_text("filtered filter=untrusted\n")
    (writer / "filtered").write_text("remote content\n")
    _commit_repository_state(writer, "add filtered content")
    _run_test_git(writer, "push", "origin", "main")
    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    "config_key",
    (
        "filter.untrusted.smudge",
        "core.askpass",
        "credential.helper",
        "extensions.worktreeConfig",
        "http.sslverify",
        "http.proxy",
        "remote.origin.proxy",
    ),
)
def test_agent_loop_wrapper_rejects_unsafe_local_config_before_refresh(
    project_root: Path, tmp_path: Path, config_key: str
) -> None:
    """Every non-allowlisted local Git setting blocks before any refresh."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    commands = tmp_path / "commands"
    _write_command(commands / "grep", "exit 99")
    environment["PATH"] = f"{commands}:{environment['PATH']}"
    filter_marker = tmp_path / "filter-ran"
    _run_test_git(
        checkout,
        "config",
        config_key,
        f"touch {filter_marker}",
    )

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not filter_marker.exists()
    assert not marker.exists()


def test_agent_loop_wrapper_rejects_local_info_attributes_before_refresh(
    project_root: Path, tmp_path: Path
) -> None:
    """Resolved local attributes cannot bind an untrusted filter driver."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    attributes_path = Path(
        _test_git_output(
            checkout,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/attributes",
        )
    )
    attributes_path.parent.mkdir(parents=True, exist_ok=True)
    attributes_path.write_text("* filter=untrusted\n")

    result = _run_wrapper(project_root, environment, "--apply")

    assert result.returncode != 0
    assert not marker.exists()


def test_agent_loop_wrapper_rejects_duplicate_origin_urls(
    project_root: Path, tmp_path: Path
) -> None:
    """Ambiguous local origin URLs block before remote or worker execution."""
    checkout, environment, marker, _ = _wrapper_checkout_environment(tmp_path)
    _run_test_git(
        checkout,
        "config",
        "--add",
        "remote.origin.url",
        "https://wrong.example.test/project.git",
    )

    result = _run_wrapper(project_root, environment)

    assert result.returncode != 0
    assert not marker.exists()


def test_agent_loop_wrapper_stops_before_direnv_without_checkout_local_envrc(
    project_root: Path, tmp_path: Path
) -> None:
    """A configured direnv route is valid only for a checkout-local envrc."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    direnv_marker = tmp_path / "direnv-marker"
    uv_marker = tmp_path / "uv-marker"
    _write_command(commands / "direnv", 'touch "$DIRENV_MARKER"')
    _write_command(commands / "uv", 'touch "$UV_MARKER"')
    _write_command(commands / "agent", "exit 0")

    environment = os.environ | {
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "123",
        "DIRENV_MARKER": str(direnv_marker),
        "UV_MARKER": str(uv_marker),
    }
    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "agent-work-systemd-run.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 2
    assert "AGENT_WORK_CHECKOUT/.envrc" in result.stderr
    assert not direnv_marker.exists()
    assert not uv_marker.exists()


@pytest.mark.parametrize(
    ("gitlab_host", "gitlab_project_id", "expected_error"),
    (
        ("", "123", "GITLAB_HOST must be a bare hostname"),
        ("https://gitlab.example.test", "123", "GITLAB_HOST must be a bare hostname"),
        ("gitlab.example.test/group", "123", "GITLAB_HOST must be a bare hostname"),
        ("gitlab.example.test", "", "GITLAB_PROJECT_ID must be a positive integer"),
        ("gitlab.example.test", "0", "GITLAB_PROJECT_ID must be a positive integer"),
        ("gitlab.example.test", "00", "GITLAB_PROJECT_ID must be a positive integer"),
        ("gitlab.example.test", "abc", "GITLAB_PROJECT_ID must be a positive integer"),
    ),
)
def test_agent_loop_wrapper_rejects_invalid_service_target_identity(
    project_root: Path,
    tmp_path: Path,
    gitlab_host: str,
    gitlab_project_id: str,
    expected_error: str,
) -> None:
    """The wrapper rejects malformed public GitLab target configuration."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    marker = tmp_path / "uv-marker"
    _write_command(commands / "uv", 'touch "$UV_MARKER"')
    _write_command(commands / "agent", "exit 0")

    environment = os.environ | {
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "GITLAB_HOST": gitlab_host,
        "GITLAB_PROJECT_ID": gitlab_project_id,
        "UV_MARKER": str(marker),
    }
    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "agent-work-systemd-run.sh")],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 2
    assert expected_error in result.stderr
    assert not marker.exists()


def test_agent_loop_wrapper_stops_before_direnv_with_symlinked_envrc(
    project_root: Path, tmp_path: Path
) -> None:
    """A checkout-local authorization boundary cannot be a symlink."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    external_envrc = tmp_path / "external.envrc"
    external_envrc.write_text("# unauthorized external envrc\n")
    (checkout / ".envrc").symlink_to(external_envrc)
    commands = tmp_path / "commands"
    commands.mkdir()
    direnv_marker = tmp_path / "direnv-marker"
    _write_command(commands / "direnv", 'touch "$DIRENV_MARKER"')
    _write_command(commands / "uv", "exit 0")
    _write_command(commands / "agent", "exit 0")

    result = subprocess.run(
        ["bash", str(project_root / "scripts" / "agent-work-systemd-run.sh")],
        check=False,
        capture_output=True,
        env=os.environ
        | {
            "AGENT_WORK_CHECKOUT": str(checkout),
            "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
            "AGENT_WORK_UV": str(commands / "uv"),
            "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
            "AGENT_WORK_DIRENV": str(commands / "direnv"),
            "DIRENV_MARKER": str(direnv_marker),
            "GITLAB_HOST": "gitlab.example.test",
            "GITLAB_PROJECT_ID": "123",
        },
        text=True,
    )

    assert result.returncode == 2
    assert "AGENT_WORK_CHECKOUT/.envrc" in result.stderr
    assert not direnv_marker.exists()


@pytest.mark.parametrize("arguments", ((), ("--apply",)))
def test_agent_loop_wrapper_preserves_configured_identity_after_direnv(
    project_root: Path, tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    """Direnv credentials cannot replace the service's GitLab target identity."""
    checkout, environment, _, _ = _wrapper_checkout_environment(tmp_path)
    (checkout / ".envrc").write_text("# fake direnv authorization boundary\n")
    commands = tmp_path / "commands"
    marker = tmp_path / "runner-identity.json"
    _write_command(
        commands / "direnv",
        "export GITLAB_HOST=ancestor.example.test\n"
        "export GITLAB_PROJECT_ID=999\n"
        "shift 2\n"
        'exec "$@"',
    )
    _write_command(
        commands / "uv",
        'printf \'{"host":"%s","project_id":"%s","arguments":"%s"}\\n\' '
        '"$GITLAB_HOST" "$GITLAB_PROJECT_ID" "$*" > "$MARKER"\nprintf \'{"schema_version":1,"command":"loop.run","applied":true,"outcome":"idle","stop_reason":"idle"}\\n\'',
    )
    _write_command(commands / "agent", "exit 0")

    environment |= {
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "MARKER": str(marker),
    }
    result = subprocess.run(
        [
            "bash",
            str(project_root / "scripts" / "agent-work-systemd-run.sh"),
            *arguments,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(marker.read_text()) == {
        "host": "gitlab.example.test",
        "project_id": "123",
        "arguments": (
            "run agent-work loop run --apply --json"
            if arguments
            else "run agent-work loop run --json"
        ),
    }


@pytest.mark.parametrize(
    "relative_path",
    (
        ".agents/skills/setup-agent-loop/SKILL.md",
        "docs/how-to/setup-serial-gitlab-agent-loop.md",
    ),
)
def test_service_setup_persists_public_identity_and_previews_wrapper_before_timer(
    generated_project: Path, project_root: Path, relative_path: str
) -> None:
    """Published setup validates the isolated service route before enabling it."""
    document = (project_root / relative_path).read_text()
    assert document == (project_root / "template" / relative_path).read_text()
    assert document == (generated_project / relative_path).read_text()
    setup = _bash_block_containing(document, ("unit_name=", "agent-work.env"))

    assert "GITLAB_HOST=%q" in setup
    assert "GITLAB_PROJECT_ID=%q" in setup
    assert "AGENT_WORK_FACTORY_USERNAME=%q" in setup
    assert 'AGENT_WORK_FACTORY_USERNAME="$factory_username"' in setup
    assert "GITLAB_TOKEN" not in setup
    assert "env -i" in setup
    assert "agent-work-systemd-run.sh" in setup
    assert document.index("agent-work-systemd-run.sh") < document.index(
        "systemctl --user enable --now"
    )


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


def _test_git_output(path: Path, *arguments: str) -> str:
    """Return deterministic output from an isolated test repository."""
    environment = os.environ | {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ["git", *arguments],
        check=True,
        capture_output=True,
        cwd=path,
        env=environment,
        text=True,
    ).stdout.strip()


def _wrapper_checkout_environment(
    tmp_path: Path,
    *,
    reported_project_id: str = "123",
    reported_default_branch: str = "main",
    reported_origin: str | None = None,
    upstream_override: str | None = None,
) -> tuple[Path, dict[str, str], Path, Path]:
    """Create a dedicated clean checkout and command seams for wrapper tests."""
    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch", "main", remote],
        check=True,
        capture_output=True,
        text=True,
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _initialize_test_repository(checkout)
    (checkout / ".gitignore").write_text("/.envrc\n")
    (checkout / "version").write_text("one\n")
    _commit_repository_state(checkout, "initial")
    (checkout / ".envrc").write_text("# isolated authorization boundary\n")
    assert _test_git_output(checkout, "check-ignore", ".envrc") == ".envrc"
    assert ".envrc" not in _test_git_output(checkout, "ls-files").splitlines()
    _run_test_git(checkout, "remote", "add", "origin", str(remote))
    _run_test_git(checkout, "push", "-u", "origin", "main")
    _run_test_git(checkout, "switch", "-c", "agent/work-base")
    _run_test_git(
        checkout,
        "branch",
        "--set-upstream-to",
        "origin/main",
    )
    if upstream_override is not None:
        _run_test_git(
            checkout,
            "config",
            "branch.agent/work-base.merge",
            f"refs/heads/{upstream_override}",
        )

    commands = tmp_path / "commands"
    commands.mkdir()
    marker = tmp_path / "uv-marker"
    glab_marker = tmp_path / "glab-marker"
    checkout_url = "https://gitlab.example.test/group/project.git"
    project_url = reported_origin or checkout_url
    _run_test_git(checkout, "remote", "set-url", "origin", checkout_url)
    project_json = json.dumps(
        {
            "id": int(reported_project_id),
            "default_branch": reported_default_branch,
            "http_url_to_repo": project_url,
            "ssh_url_to_repo": f"git@example.test:{reported_project_id}/project.git",
        }
    )
    _write_command(
        commands / "glab",
        'printf \'{"host":"%s","project_id":"%s"}\\n\' '
        '"$GITLAB_HOST" "$GITLAB_PROJECT_ID" > "$GLAB_MARKER"\n'
        f"printf '%s\\n' '{project_json}'",
    )
    _write_command(
        commands / "git",
        'case " $* " in\n'
        '  *" ls-remote "*) sha="$("$REAL_GIT" --git-dir "$LOCAL_ORIGIN" rev-parse refs/heads/main)"; printf "%s\\trefs/heads/main\\n" "$sha" ;;\n'
        '  *" push --dry-run "*) "$AGENT_WORK_GLAB" auth git-credential >/dev/null 2>&1 ;;\n'
        '  *fetch*) for part in "$@"; do refspec="$part"; done; exec "$REAL_GIT" -C "$AGENT_WORK_CHECKOUT" fetch --no-tags --no-recurse-submodules "$LOCAL_ORIGIN" "$refspec" ;;\n'
        '  *) exec "$REAL_GIT" "$@" ;;\n'
        "esac",
    )
    _write_command(
        commands / "uv",
        'printf \'%s\\n\' "$(cat version)" > "$UV_MARKER"\n'
        'printf \'%s\\n\' "$*" >> "$UV_MARKER"\n'
        'printf \'{"schema_version":1,"command":"loop.run","applied":true,"outcome":"idle","stop_reason":"idle"}\\n\'',
    )
    _write_command(commands / "direnv", 'shift 2\nexec "$@"')
    _write_command(commands / "agent", "exit 0")
    command_paths = {
        "AGENT_WORK_GIT": str(commands / "git"),
        "AGENT_WORK_JQ": shutil.which("jq"),
        "AGENT_WORK_FLOCK": shutil.which("flock"),
        "AGENT_WORK_TIMEOUT": shutil.which("timeout"),
    }
    assert all(command_paths.values())
    environment = os.environ | {
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_BASE_BRANCH": "agent/work-base",
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_UV": str(commands / "uv"),
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "AGENT_WORK_GLAB": str(commands / "glab"),
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "123",
        "UV_MARKER": str(marker),
        "GLAB_MARKER": str(glab_marker),
        "REAL_GIT": str(shutil.which("git")),
        "LOCAL_ORIGIN": str(remote),
        **{key: str(value) for key, value in command_paths.items()},
    }
    return checkout, environment, marker, glab_marker


def _advance_wrapper_default_branch(checkout: Path, content: str) -> None:
    """Advance the tracked default branch without altering the service checkout."""
    writer = checkout.parent / "writer"
    subprocess.run(
        ["git", "clone", str(checkout.parent / "origin.git"), str(writer)],
        check=True,
        capture_output=True,
        text=True,
    )
    (writer / "version").write_text(f"{content}\n")
    _commit_repository_state(writer, f"advance to {content}")
    _run_test_git(writer, "push", "origin", "main")


def _run_wrapper(
    project_root: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run the wrapper under its injected command and checkout seams."""
    return subprocess.run(
        [
            "bash",
            str(project_root / "scripts" / "agent-work-systemd-run.sh"),
            *arguments,
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def _applied_report(stop_reason: str) -> str:
    """Return one minimal valid applied loop report for a wrapper worker fake."""
    return json.dumps(
        {
            "schema_version": 1,
            "command": "loop.run",
            "applied": True,
            "outcome": "idle" if stop_reason == "idle" else "activation_complete",
            "stop_reason": stop_reason,
        }
    )


def _preview_report(outcome: str) -> str:
    """Return one minimal valid read-only loop report for a wrapper worker fake."""
    return json.dumps(
        {
            "schema_version": 1,
            "command": "loop.run",
            "applied": False,
            "outcome": outcome,
        }
    )


def _scripted_wrapper_worker(
    tmp_path: Path,
    environment: dict[str, str],
    reports: tuple[str, ...],
    *,
    advance_after_first_apply: bool = False,
) -> Path:
    """Install a JSON-reporting UV seam and return its ordered invocation record."""
    reports_path = tmp_path / "worker-reports.jsonl"
    reports_path.write_text("\n".join(reports) + "\n")
    calls = tmp_path / "worker-calls"
    commands = tmp_path / "scripted-worker"
    _write_command(
        commands,
        'printf "%s|%s\\n" "$(cat version)" "$*" >> "$UV_CALLS"\n'
        'call_count="$(wc -l < "$UV_CALLS")"\n'
        'if [ "$*" = "run agent-work loop run --apply --json" ]; then\n'
        '  apply_count="$(grep -c -- "--apply --json" "$UV_CALLS")"\n'
        '  if [ "$apply_count" = 1 ] && [ -n "${UV_ADVANCE:-}" ]; then "$UV_ADVANCE"; fi\n'
        "fi\n"
        'sed -n "${call_count}p" "$UV_REPORTS"',
    )
    environment |= {
        "AGENT_WORK_UV": str(commands),
        "UV_CALLS": str(calls),
        "UV_REPORTS": str(reports_path),
    }
    if not advance_after_first_apply:
        environment.pop("UV_ADVANCE", None)
    return calls


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
        "src/agent_work/upstream_finding.py",
        "src/agent_work/upstream_gitlab.py",
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


def test_upstream_finding_docs_publish_the_read_only_preview_interface_and_navigation(
    project_root: Path, template_dir: Path
) -> None:
    """Reference syntax and a linked how-to remain mirrored template interfaces."""
    reference_path = Path("docs/reference/serial-gitlab-agent-loop.md")
    how_to_path = Path("docs/how-to/setup-serial-gitlab-agent-loop.md")
    root_reference = (project_root / reference_path).read_text()
    root_how_to = (project_root / how_to_path).read_text()
    assert root_reference == (template_dir / reference_path).read_text()
    assert root_how_to == (template_dir / how_to_path).read_text()
    command = (
        "agent-work upstream-finding preview --manifest-file PATH "
        "--upstream-host HOST --upstream-project-id POSITIVE_INT "
        "--upstream-credential-file PATH [--apply] [--json] [--debug]"
    )
    assert command in root_reference
    assert "../how-to/setup-serial-gitlab-agent-loop.md" in root_reference
    assert "upstream-finding preview --manifest-file" in root_how_to
    assert "dedicated credential file" in root_how_to.lower()
    assert "no issue is posted" in root_how_to.lower()
    assert (
        "serial-gitlab-agent-loop.md"
        in (project_root / "docs/reference/index.md").read_text()
    )


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
    """Manual loop activation guidance loads an explicit service base."""
    documents = (
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


@pytest.mark.parametrize(
    ("document", "validation_commands"),
    (
        ("root", ("uvx copier copy", "uv run pytest")),
        ("template", ("uv run pytest",)),
        ("rendered", ("uv run pytest",)),
    ),
)
def test_readmes_keep_one_validation_fence_and_delegate_loop_setup_to_codex(
    document: str,
    validation_commands: tuple[str, ...],
    generated_project: Path,
    project_root: Path,
    template_dir: Path,
) -> None:
    """Each README keeps validation executable and delegates loop management."""
    readmes = {
        "root": project_root / "README.md",
        "template": template_dir / "README.md.jinja",
        "rendered": generated_project / "README.md",
    }
    content = readmes[document].read_text()
    bash_blocks = _bash_blocks(content)
    text_blocks = tuple(
        re.findall(
            r"^[ \t]*```text[ \t]*\n(?P<prompt>.*?)\n^[ \t]*```[ \t]*$",
            content,
            flags=re.DOTALL | re.MULTILINE,
        )
    )

    assert len(bash_blocks) == 1
    assert all(command in bash_blocks[0] for command in validation_commands)
    assert "uv run agent-work loop run" not in content
    assert "AGENT_WORK_BASE_BRANCH" not in content
    assert "AGENT_WORK_FACTORY_USERNAME" not in content
    assert "ask codex" in content.casefold()
    assert "recoverable serial gitlab agent loop" in content.casefold()
    assert "(docs/how-to/setup-serial-gitlab-agent-loop.md)" in content
    assert len(text_blocks) == 1
    prompt = text_blocks[0].casefold()
    for fragment in (
        "follow",
        "repository's instructions",
        "loop setup guide",
        "prerequisites",
        "safeguards",
        "human decisions",
        "blockers",
    ):
        assert fragment in prompt


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
    assert (
        'authenticated "$AGENT_WORK_UV" run agent-work loop run --apply --json'
        in wrapper
    )
    assert (
        'exec authenticated "$AGENT_WORK_UV" run agent-work loop run --apply --json'
        not in wrapper
    )
    assert "require_path AGENT_WORK_DIRENV" in wrapper
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
    for service in (root_service, template_service, generated_service):
        assert "TimeoutStartSec=190min" in service
        assert "TimeoutStartSec=infinity" not in service
        assert "KillMode=control-group" in service
        assert "KillSignal=SIGTERM" in service
        assert "TimeoutStopSec=30s" in service
        assert "SendSIGKILL=yes" in service
    assert "require_path AGENT_WORK_TIMEOUT" in wrapper
    assert '"$AGENT_WORK_TIMEOUT" --signal=TERM --kill-after=30s 120s' in wrapper
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


def test_git_flow_publishes_and_verifies_literal_markdown_mr_descriptions(
    project_root: Path, template_dir: Path
) -> None:
    """MR instructions preserve a literal Markdown body through GitLab read-back."""
    skill_path = Path(".agents/skills/git-flow/SKILL.md")
    root_contract = (project_root / skill_path).read_text()
    template_contract = (template_dir / skill_path).read_text()

    assert root_contract == template_contract
    creation_contract = root_contract.split("3b. **MR exists", maxsplit=1)[0]
    existing_contract = root_contract.split("3b. **MR exists", maxsplit=1)[1]
    expected_description = """## Summary

- Publish an exact `Markdown` description.

## Test plan

- `uv run tox`"""
    expected_assignment = (
        "mr_description=$(cat <<'MR_DESCRIPTION'\n"
        f"{expected_description}\n"
        "MR_DESCRIPTION\n"
        ")"
    )

    # This equality protects the literal Markdown bytes, including the blank
    # lines and inline code, rather than merely checking that Markdown exists.
    assert expected_assignment in creation_contract
    assert '-f description="$mr_description"' in creation_contract
    creation_request = creation_contract.split("created_mr_json=", maxsplit=1)[1].split(
        "mr_iid=", maxsplit=1
    )[0]
    assert "%0A" not in creation_request
    assert not re.search(
        r"(?i)(?:url|uri|percent)[_-]?encod|(?:quote|escape)(?:_plus)?\(",
        creation_request,
    )
    assert re.search(
        r"(?ms)created_mr_json=.*?mr_iid=.*?\.iid.*?"
        r"read_back_mr_json=.*?projects/\$GITLAB_PROJECT_ID/merge_requests/\$mr_iid.*?"
        r"if ! jq -e --arg iid \"\$mr_iid\" .*?"
        r"--arg description \"\$mr_description\" .*?"
        r"<<<\"\$read_back_mr_json\" >/dev/null; then.*?exit 1",
        creation_contract,
    )
    expected_jq_arguments = {
        "iid": "mr_iid",
        "source_branch": "source_branch",
        "target_branch": "target_branch",
        "title": "title",
        "description": "mr_description",
    }
    expected_jq_predicates = {
        "source_branch": "source_branch",
        "target_branch": "target_branch",
        "title": "title",
        "description": "description",
    }
    for field, variable in expected_jq_arguments.items():
        assert f'--arg {field} "${variable}"' in creation_contract
    for field, variable in expected_jq_arguments.items():
        assert f"(.{field} | type)" in creation_contract
    for field, variable in expected_jq_predicates.items():
        assert f".{field} == ${variable}" in creation_contract
    assert "(.iid | tostring) == $iid" in creation_contract
    assert 'if (.iid | type) == "number" then .iid' in creation_contract
    assert re.search(
        r'(?ms)mr_iid=.*?\)" \|\| \{\s+printf.*?\s+exit 1\s+\}',
        creation_contract,
    )
    assert re.search(
        r"(?ms)selected_mr_json=.*?mr_iid=.*?\.iid.*?"
        r"git push.*?"
        r"read_back_mr_json=.*?projects/\$GITLAB_PROJECT_ID/merge_requests/\$mr_iid.*?"
        r"if ! jq -e --arg iid \"\$mr_iid\" --argjson intended \"\$selected_mr_json\" .*?"
        r"<<<\"\$read_back_mr_json\" >/dev/null; then.*?exit 1",
        existing_contract,
    )
    assert "(.iid | tostring) == $iid" in existing_contract
    for field in expected_jq_arguments:
        assert f"(.{field} | type)" in existing_contract
    assert ".source_branch == $intended.source_branch" in existing_contract
    assert ".target_branch == $intended.target_branch" in existing_contract
    assert ".title == $intended.title" in existing_contract
    assert ".description == $intended.description" in existing_contract
    assert (
        'jq -cer \'if length == 1 and (.[0].iid | type == "number")'
        in existing_contract
    )
    assert 'if (.iid | type) == "number" then .iid' in existing_contract
    assert re.search(
        r'(?ms)selected_mr_json=.*?\)" \|\| \{\s+printf.*?\s+exit 1\s+\}.*?'
        r'mr_iid=.*?\)" \|\| \{\s+printf.*?\s+exit 1\s+\}.*?git push',
        existing_contract,
    )
    assert existing_contract.index("selected_mr_json=") < existing_contract.index(
        "git push"
    )
