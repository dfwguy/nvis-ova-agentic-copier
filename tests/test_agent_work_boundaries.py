"""Red boundary contracts for the serial GitLab-native runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.adapters import (
    AdapterError,
    CodexInvoker,
    CommandResult,
    GitLabGateway,
    LockBusy,
    ProjectLock,
    ServiceCheckout,
    build_default_dependencies,
    load_environment_config,
)
from agent_work.cli import build_parser, main
from agent_work.models import AgentWorkConfig, GitLabIssue, RunReport
from agent_work.serial import RunNextDependencies


class _Commands:
    """Record subprocess calls and return scripted safe results."""

    def __init__(self, results: tuple[CommandResult, ...]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append((command, kwargs))
        return next(self.results)


def _config(tmp_path: Path) -> AgentWorkConfig:
    return load_environment_config(
        {
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
            "AGENT_WORK_CHECKOUT": str(tmp_path / "checkout"),
        }
    )


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("GITLAB_PROJECT_ID", "project-path"),
        ("AGENT_WORK_CHECKOUT", "relative/checkout"),
    ),
)
def test_environment_requires_numeric_project_and_absolute_local_paths(
    tmp_path: Path, key: str, value: str
) -> None:
    environment = {
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
        "AGENT_WORK_CHECKOUT": str(tmp_path / "checkout"),
        key: value,
    }

    with pytest.raises(ValueError):
        load_environment_config(environment)


def test_environment_derives_one_fixed_lock_file_from_the_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"

    config = load_environment_config(
        {
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
            "AGENT_WORK_CHECKOUT": str(checkout),
            "AGENT_WORK_LOCK_FILE": str(tmp_path / "ignored-custom.lock"),
        }
    )

    assert config.lock_file == checkout / ".agent-work.lock"


def test_default_dependencies_discover_checkout_and_project_without_environment(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    project_payload = json.dumps(
        {
            "id": 310967,
            "path_with_namespace": "group/project",
            "default_branch": "main",
            "web_url": "https://gitlab.example.com/group/project",
            "http_url_to_repo": "https://gitlab.example.com/group/project.git",
        }
    )
    commands = _Commands(
        (
            CommandResult(0, str(checkout), ""),
            CommandResult(0, "", ""),
            CommandResult(0, "https://gitlab.example.com/group/project.git\n", ""),
            CommandResult(0, json.dumps([json.loads(project_payload)]), ""),
            CommandResult(0, project_payload, ""),
        )
    )

    dependencies = build_default_dependencies(
        environment={}, run_command=commands, current_directory=tmp_path
    )

    assert dependencies.checkout.path == checkout
    assert commands.calls == [
        (("git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"), {}),
        (("git", "-C", str(checkout), "pull", "--ff-only"), {}),
        (("git", "-C", str(checkout), "remote", "get-url", "origin"), {}),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "GET",
                "projects?search=project&simple=true&per_page=100",
            ),
            {},
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "GET",
                "projects/310967",
            ),
            {},
        ),
    ]


@pytest.mark.parametrize(
    ("failure_at", "message"),
    (
        (1, "git pull --ff-only failed"),
        (3, "GitLab project lookup failed"),
    ),
)
def test_default_dependencies_name_the_failed_confirmation(
    tmp_path: Path, failure_at: int, message: str
) -> None:
    results = [
        CommandResult(0, str(tmp_path), ""),
        CommandResult(0, "", ""),
        CommandResult(0, "https://gitlab.example.com/group/project.git\n", ""),
        CommandResult(
            0,
            json.dumps(
                [
                    {
                        "id": 310967,
                        "path_with_namespace": "group/project",
                        "default_branch": "main",
                        "web_url": "https://gitlab.example.com/group/project",
                    }
                ]
            ),
            "",
        ),
        CommandResult(0, "{}", ""),
    ]
    results[failure_at] = CommandResult(1, "", "connection failed")

    with pytest.raises(AdapterError, match=message):
        build_default_dependencies(
            environment={},
            run_command=_Commands(tuple(results)),
            current_directory=tmp_path,
        )


def test_gitlab_selection_uses_only_the_numeric_project_endpoint(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "iid": 3,
                "title": "Oldest public issue",
                "description": None,
                "labels": ["agent::ready"],
                "state": "opened",
                "confidential": False,
                "created_at": "2026-08-01T00:00:00Z",
            }
        ]
    )
    commands = _Commands((CommandResult(0, payload, ""),))

    issues = GitLabGateway(_config(tmp_path), commands).list_ready()

    assert [issue.iid for issue in issues] == [3]
    command, _ = commands.calls[0]
    assert "projects/310967/issues" in command
    assert "labels=agent::ready" in command
    assert "state=opened" in command
    assert "confidential=false" in command
    assert "not[labels]=agent::running" in command
    assert "per_page=1" in command
    assert "order_by=created_at" in command
    assert "sort=asc" in command
    assert not any("group/project" in argument for argument in command)


def test_gitlab_ready_selection_filters_eligibility_in_one_bounded_query(
    tmp_path: Path,
) -> None:
    eligible = {
        "iid": 101,
        "title": "Eligible issue",
        "description": "",
        "labels": ["agent::ready"],
        "state": "opened",
        "confidential": False,
    }
    commands = _Commands((CommandResult(0, json.dumps([eligible]), ""),))

    issues = GitLabGateway(_config(tmp_path), commands).list_ready()

    assert [issue.iid for issue in issues] == [101]
    assert len(commands.calls) == 1
    command = commands.calls[0][0]
    assert "confidential=false" in command
    assert "not[labels]=agent::running" in command
    assert "per_page=1" in command


def test_gitlab_running_selection_includes_closed_issues(tmp_path: Path) -> None:
    closed_running = {
        "iid": 3,
        "title": "Merged while transition was interrupted",
        "description": "",
        "labels": ["agent::running"],
        "state": "closed",
        "confidential": False,
    }
    commands = _Commands((CommandResult(0, json.dumps([closed_running]), ""),))

    issues = GitLabGateway(_config(tmp_path), commands).list_running()

    assert [issue.iid for issue in issues] == [3]
    assert "state=all" in commands.calls[0][0]
    assert "per_page=2" in commands.calls[0][0]


def test_gitlab_rejects_malformed_issue_text_as_a_safe_adapter_error(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "iid": 3,
                "title": "Issue",
                "description": 42,
                "labels": ["agent::ready"],
                "state": "opened",
                "confidential": False,
            }
        ]
    )
    commands = _Commands((CommandResult(0, payload, ""),))

    with pytest.raises(AdapterError, match="invalid issue data"):
        GitLabGateway(_config(tmp_path), commands).list_ready()


def test_gitlab_label_transition_uses_explicit_add_remove_sets_and_refetches(
    tmp_path: Path,
) -> None:
    issue_payload = json.dumps(
        {
            "iid": 3,
            "title": "Issue",
            "description": "",
            "labels": ["agent::running"],
            "state": "opened",
            "confidential": False,
        }
    )
    commands = _Commands(
        (CommandResult(0, issue_payload, ""), CommandResult(0, issue_payload, ""))
    )
    issue = GitLabIssue(
        iid=3,
        title="Issue",
        labels=("agent::ready",),
        state="opened",
        confidential=False,
    )

    GitLabGateway(_config(tmp_path), commands).mark_running(issue)

    update, _ = commands.calls[0]
    assert "projects/310967/issues/3" in update
    assert "add_labels=agent::running" in update
    assert "remove_labels=agent::ready,agent::awaiting-human,agent::failed" in update
    fetch, _ = commands.calls[1]
    assert "projects/310967/issues/3" in fetch


def test_codex_is_invoked_once_noninteractively_with_prompt_on_stdin(
    tmp_path: Path,
) -> None:
    commands = _Commands((CommandResult(7, "ignored", "ignored"),))
    checkout = tmp_path / "checkout"

    returncode = CodexInvoker(commands).invoke(checkout, "untrusted prompt", 45)

    assert returncode == 7
    assert commands.calls == [
        (
            (
                "codex",
                "exec",
                "-C",
                str(checkout),
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=true",
                "--ephemeral",
                "--color",
                "never",
                "-",
            ),
            {"stdin": "untrusted prompt", "timeout_seconds": 45},
        )
    ]


def test_checkout_refuses_an_existing_deterministic_local_branch(
    tmp_path: Path,
) -> None:
    commands = _Commands(
        (
            CommandResult(0, "a" * 40, ""),
            CommandResult(2, "", ""),
        )
    )

    with pytest.raises(AdapterError, match="already exists"):
        ServiceCheckout(_config(tmp_path), commands).ensure_branch_available(
            "agent/issue-12"
        )


def test_project_lock_is_nonblocking(tmp_path: Path) -> None:
    first = ProjectLock(tmp_path / "project.lock")
    second = ProjectLock(tmp_path / "project.lock")

    with first, pytest.raises(LockBusy):
        second.__enter__()


class _UnusedDependency:
    """Satisfy dependency construction for a CLI report fake."""


def test_cli_has_one_safe_by_default_command_and_safe_json_output() -> None:
    reports: list[dict[str, Any]] = []

    def run(_dependencies: object, *, apply: bool, timeout_seconds: int) -> RunReport:
        assert not apply
        assert timeout_seconds == 1800
        return RunReport(outcome="preview_ready", issue_iid=12, branch="agent/issue-12")

    dependencies = cast(RunNextDependencies, _UnusedDependency())

    exit_code = main(
        ["run-next", "--json"],
        dependencies_factory=lambda: dependencies,
        run=run,
        write_stdout=lambda value: reports.append(json.loads(value)),
    )

    assert exit_code == 0
    assert reports == [
        {
            "branch": "agent/issue-12",
            "issue_iid": 12,
            "merge_request_url": None,
            "outcome": "preview_ready",
        }
    ]


def test_cli_help_exposes_only_the_serial_run_next_command() -> None:
    help_text = build_parser().format_help()

    assert "{run-next}" in help_text
    for removed_command in (
        "run-issue",
        "run-file",
        "run-once",
        "ingest-file",
        "reconcile",
        "status",
    ):
        assert removed_command not in help_text


def test_cli_returns_temporary_failure_when_the_project_lock_is_busy() -> None:
    def run(_dependencies: object, *, apply: bool, timeout_seconds: int) -> RunReport:
        del apply, timeout_seconds
        raise LockBusy("another run owns the project lock")

    errors: list[str] = []

    exit_code = main(
        ["run-next", "--apply"],
        dependencies_factory=lambda: cast(RunNextDependencies, _UnusedDependency()),
        run=run,
        write_stderr=errors.append,
    )

    assert exit_code == 75
    assert errors == ["another run owns the project lock"]
