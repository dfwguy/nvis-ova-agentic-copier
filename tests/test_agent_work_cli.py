"""Public CLI and package/template contract tests for ``agent-work``."""

import json
import tomllib
from pathlib import Path

import pytest

from agent_work.adapters import AdapterFailure, CommandResult, GitLabGateway
from agent_work.cli import build_default_dependencies, main
from agent_work.models import GitLabConfig
from agent_work.runner import RunFailure


class FakeRunner:
    """Small injected CLI boundary that returns a serializable report."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.failure = failure

    def __call__(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return {"preview": not kwargs["apply"], "branch": "agent/issue-123"}


def test_run_issue_defaults_to_dry_run_and_emits_one_json_document() -> None:
    """Automation gets a single JSON document without logging mixed into stdout."""
    runner = FakeRunner()
    stdout: list[str] = []

    exit_code = main(
        ["run-issue", "123", "--mode", "publish", "--json"],
        run_issue=runner,
        write_stdout=stdout.append,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert runner.calls == [
        {"issue_iid": 123, "mode": "publish", "apply": False, "timeout_seconds": 900}
    ]
    assert len(stdout) == 1
    assert json.loads(stdout[0]) == {"preview": True, "branch": "agent/issue-123"}


def test_run_issue_apply_and_timeout_are_forwarded_without_changing_stdout_contract() -> (
    None
):
    """Apply is explicit, and timeout is a bounded caller-controlled input."""
    runner = FakeRunner()
    stdout: list[str] = []

    exit_code = main(
        [
            "run-issue",
            "123",
            "--mode",
            "publish",
            "--apply",
            "--timeout-seconds",
            "90",
            "--json",
        ],
        run_issue=runner,
        write_stdout=stdout.append,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert runner.calls[0]["apply"] is True
    assert runner.calls[0]["timeout_seconds"] == 90
    assert len(stdout) == 1


@pytest.mark.parametrize("arguments", (["--help"], ["run-issue", "--help"]))
def test_help_is_successful_stdout_only_without_a_usage_error(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both public help surfaces are normal successful command output."""
    exit_code = main(arguments)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "usage: agent-work" in captured.out
    assert captured.err == ""


def test_human_default_and_json_runtime_failure_keep_output_streams_separate() -> None:
    """Humans get a concise report; JSON failures never contaminate stdout."""
    human_stdout: list[str] = []
    human_stderr: list[str] = []
    error_stdout: list[str] = []
    error_stderr: list[str] = []

    human_exit = main(
        ["run-issue", "123", "--mode", "publish"],
        run_issue=FakeRunner(),
        write_stdout=human_stdout.append,
        write_stderr=human_stderr.append,
    )
    error_exit = main(
        ["run-issue", "123", "--mode", "publish", "--json"],
        run_issue=FakeRunner(failure=RunFailure("safe blocker")),
        write_stdout=error_stdout.append,
        write_stderr=error_stderr.append,
    )

    assert human_exit == 0
    assert human_stdout == ["Run report: agent/issue-123"]
    assert human_stderr == []
    assert error_exit == 1
    assert error_stdout == []
    assert error_stderr == ["safe blocker"]


@pytest.mark.parametrize(
    "arguments",
    (
        ["run-issue", "zero", "--mode", "publish"],
        ["run-issue", "0", "--mode", "publish"],
        ["run-issue", "123", "--mode", "inspect"],
        ["run-issue", "123", "--mode", "publish", "--timeout-seconds", "0"],
    ),
)
def test_cli_reports_usage_errors_with_exit_code_two(arguments: list[str]) -> None:
    """Invalid CLI input is distinguishable from a runtime blocker."""
    stderr: list[str] = []

    exit_code = main(
        arguments,
        run_issue=FakeRunner(),
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 2
    assert stderr


def test_cli_reports_runtime_blockers_with_exit_code_one() -> None:
    """A runner failure never masquerades as a successful preview or publish."""
    stderr: list[str] = []

    exit_code = main(
        ["run-issue", "123", "--mode", "publish", "--json"],
        run_issue=FakeRunner(failure=RunFailure("GitLab rejected the claim")),
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stderr == ["GitLab rejected the claim"]


@pytest.mark.parametrize(
    "failure", (AdapterFailure("raw stderr should stay private"), OSError("raw stderr"))
)
def test_cli_converts_every_adapter_failure_to_safe_stderr_exit_one(
    failure: Exception,
) -> None:
    """No subprocess or adapter exception can escape as a traceback from the CLI."""
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = main(
        ["run-issue", "123", "--mode", "publish", "--json"],
        run_issue=FakeRunner(failure=failure),
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["agent-work run failed"]


@pytest.mark.parametrize(
    "response",
    (
        '{"iid":123,"title":"Bounded","labels":["agent::ready"],"confidential":false}',
        '{"iid":123,"title":"Bounded","labels":["agent::ready"],"state":"opened"}',
    ),
)
def test_cli_converts_missing_issue_safety_fields_to_a_safe_runtime_exit(
    response: str,
) -> None:
    """Missing external state or confidentiality fields cannot enter runner work."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(0, response, ""),
    )
    stdout: list[str] = []
    stderr: list[str] = []

    def runner(**_kwargs: object) -> object:
        gateway.fetch_issue(123)
        raise AssertionError("an unsafe issue payload must stop at the gateway")

    exit_code = main(
        ["run-issue", "123", "--mode", "publish", "--json"],
        run_issue=runner,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["agent-work run failed"]


def test_default_cli_wiring_builds_real_dependencies_without_removing_injection() -> (
    None
):
    """The installed command can construct its concrete boundary adapters."""
    dependencies = build_default_dependencies(repository=Path("/work/source"))

    assert dependencies.load_config is not None
    assert dependencies.fetch_issue is not None
    assert dependencies.invoke_codex is not None
    assert dependencies.preflight_repository is not None


def test_root_and_rendered_template_declare_the_same_agent_work_cli(
    generated_project: Path, project_root: Path
) -> None:
    """The copier source and generated projects expose the Phase 1 command."""
    root_config = tomllib.loads((project_root / "pyproject.toml").read_text())
    generated_config = tomllib.loads((generated_project / "pyproject.toml").read_text())

    assert root_config["project"]["scripts"]["agent-work"] == "agent_work.cli:main"
    assert generated_config["project"]["scripts"]["agent-work"] == "agent_work.cli:main"
    template_config = (project_root / "template" / "pyproject.toml.jinja").read_text()
    assert 'packages = ["src/{{ project_slug }}", "src/agent_work"]' in template_config
    assert "force-include" not in template_config
    assert generated_config["tool"]["hatch"]["build"]["targets"]["wheel"][
        "packages"
    ] == ["src/my_test_project", "src/agent_work"]
    for name in (
        "__init__.py",
        "adapters.py",
        "cli.py",
        "filesystem.py",
        "models.py",
        "prompt.py",
        "result.py",
        "runner.py",
    ):
        root_module = project_root / "src" / "agent_work" / name
        template_module = project_root / "template" / "src" / "agent_work" / name
        generated_module = generated_project / "src" / "agent_work" / name
        assert root_module.read_bytes() == template_module.read_bytes()
        assert root_module.read_bytes() == generated_module.read_bytes()


def test_work_item_runner_artifacts_and_agent_branch_policy_are_mirrored(
    generated_project: Path, project_root: Path
) -> None:
    """Root, template, and generated projects retain the same runner instructions."""
    mirrored_paths = (
        ".agents/skills/work-item-runner/SKILL.md",
        ".claude/commands/work-item.md",
        ".claude/git-flow.md",
    )

    for relative_path in mirrored_paths:
        root = project_root / relative_path
        template = project_root / "template" / relative_path
        generated = generated_project / relative_path
        assert root.read_bytes() == template.read_bytes() == generated.read_bytes()

    skill = (project_root / mirrored_paths[0]).read_text()
    git_flow = (project_root / mirrored_paths[2]).read_text()
    assert "agent/issue-<iid>" in skill
    assert "untrusted" in skill
    assert "awaiting_human" in skill
    assert "agent/issue-<iid>" in git_flow
    assert "work-item-runner" in git_flow
    assert "agent-work run-issue" in git_flow
    assert (
        "git rev-parse --abbrev-ref HEAD | grep -E '^agent/issue-[1-9][0-9]*$'"
        in git_flow
    )
    assert 'test -d "$(git rev-parse --show-toplevel)/.git"' in git_flow
    assert 'test -d "$(git rev-parse --git-dir)"' not in git_flow
    assert "stop on ambiguity" not in git_flow

    runner_route_start = git_flow.index(
        "- **On a mechanically verified runner `agent/issue-<iid>` branch**"
    )
    ordinary_route_start = git_flow.index("- **Already on a `claude/<slug>`")
    runner_route = git_flow[runner_route_start:ordinary_route_start]
    ordinary_route = git_flow[ordinary_route_start:]
    assert "continue" in runner_route and "workflow" in runner_route
    assert "agent-work run-issue" in runner_route
    assert "configured default branch" in runner_route
    assert "origin/main" not in runner_route
    assert "On any other branch" in ordinary_route
    assert "stop" in ordinary_route
