"""Red boundary contracts for the serial GitLab-native runner."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.adapters import (
    AdapterError,
    AgentInvoker,
    CommandResult,
    GitLabGateway,
    LockBusy,
    ProjectLock,
    ServiceCheckout,
    build_default_dependencies,
    discover_checkout_config,
)
from agent_work.cli import build_parser, main
from agent_work.models import (
    AgentInvocation,
    AgentWorkConfig,
    GitLabIssue,
    GitLabProject,
    MergeRequestDraft,
    PipelineEvidence,
    RunReport,
)
from agent_work.serial import BranchConflict, RunNextDependencies


class _Commands:
    """Record subprocess calls and return scripted safe results."""

    def __init__(self, results: tuple[CommandResult, ...]) -> None:
        self.results = iter(results)
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.calls.append((command, kwargs))
        return next(self.results)


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
    )


def test_config_derives_one_fixed_lock_file_from_the_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    config = AgentWorkConfig(
        host="gitlab.example.com", project_id=310967, checkout=checkout
    )

    assert config.lock_file == checkout / ".agent-work.lock"


def test_trusted_publish_normalizes_an_unexpected_worker_commit(
    tmp_path: Path,
) -> None:
    """The runner reauthors one direct worker commit before publication."""
    baseline = "a" * 40
    worker_commit = "b" * 40
    commands = _Commands(
        (
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, worker_commit, ""),
            CommandResult(0, f"{worker_commit} {baseline}\n", ""),
            CommandResult(0, "", ""),
            CommandResult(0, "", ""),
        )
    )

    ServiceCheckout(_config(tmp_path), commands).publish(
        "agent/issue-12",
        MergeRequestDraft(title="Worker change", description="Validated."),
        baseline,
    )

    assert [call[0] for call in commands.calls] == [
        ("git", "-C", str(tmp_path / "checkout"), "rev-parse", "--abbrev-ref", "HEAD"),
        ("git", "-C", str(tmp_path / "checkout"), "rev-parse", "HEAD"),
        (
            "git",
            "-C",
            str(tmp_path / "checkout"),
            "rev-list",
            "--parents",
            "-n",
            "1",
            "HEAD",
        ),
        (
            "git",
            "-C",
            str(tmp_path / "checkout"),
            "commit",
            "--amend",
            "--reset-author",
            "-m",
            "Worker change",
        ),
        (
            "git",
            "-C",
            str(tmp_path / "checkout"),
            "push",
            "--set-upstream",
            "origin",
            "agent/issue-12",
        ),
    ]


def test_trusted_publish_preserves_worker_merge_parents(tmp_path: Path) -> None:
    """A conflict-resolution merge is reauthored without flattening it."""
    baseline = "a" * 40
    merge_commit = "b" * 40
    other_parent = "c" * 40
    commands = _Commands(
        (
            CommandResult(0, "agent/issue-12\n", ""),
            CommandResult(0, merge_commit, ""),
            CommandResult(0, f"{merge_commit} {baseline} {other_parent}\n", ""),
            CommandResult(0, "", ""),
            CommandResult(0, "", ""),
        )
    )

    ServiceCheckout(_config(tmp_path), commands).publish(
        "agent/issue-12",
        MergeRequestDraft(title="Resolve conflict", description="Validated."),
        baseline,
    )

    assert (
        "git",
        "-C",
        str(tmp_path / "checkout"),
        "reset",
        "--mixed",
        baseline,
    ) not in [call[0] for call in commands.calls]
    assert (
        "git",
        "-C",
        str(tmp_path / "checkout"),
        "commit",
        "--amend",
        "--reset-author",
        "-m",
        "Resolve conflict",
    ) in [call[0] for call in commands.calls]


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
            CommandResult(0, "https://gitlab.example.com/group/project.git\n", ""),
            CommandResult(0, json.dumps([json.loads(project_payload)]), ""),
            CommandResult(0, project_payload, ""),
            CommandResult(0, "main\n", ""),
            CommandResult(0, "", ""),
        )
    )

    dependencies = build_default_dependencies(
        run_command=commands, current_directory=tmp_path
    )

    assert dependencies.checkout.path == checkout
    assert commands.calls == [
        (("git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"), {}),
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
        (("git", "-C", str(checkout), "branch", "--show-current"), {}),
        (("git", "-C", str(checkout), "pull", "--ff-only"), {}),
    ]


def test_checkout_derivation_uses_the_current_directory(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    project = {
        "id": 310967,
        "path_with_namespace": "group/project",
        "default_branch": "main",
        "web_url": "https://gitlab.example.com/group/project",
    }
    commands = _Commands(
        (
            CommandResult(0, str(checkout), ""),
            CommandResult(0, "https://gitlab.example.com/group/project.git\n", ""),
            CommandResult(0, json.dumps([project]), ""),
            CommandResult(0, json.dumps(project), ""),
            CommandResult(0, "main\n", ""),
            CommandResult(0, "", ""),
        )
    )

    config = discover_checkout_config(
        current_directory=tmp_path,
        environment={"AGENT_WORK_LOG_TAG": "project-agent-work"},
        run_command=commands,
    )

    assert config.checkout == checkout
    assert config.journal_tag == "project-agent-work"


def test_checkout_derivation_refuses_to_launch_from_a_feature_branch(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    project = {
        "id": 310967,
        "path_with_namespace": "group/project",
        "default_branch": "main",
        "web_url": "https://gitlab.example.com/group/project",
    }
    commands = _Commands(
        (
            CommandResult(0, str(checkout), ""),
            CommandResult(0, "https://gitlab.example.com/group/project.git\n", ""),
            CommandResult(0, json.dumps([project]), ""),
            CommandResult(0, json.dumps(project), ""),
            CommandResult(0, "agent/issue-12\n", ""),
        )
    )

    with pytest.raises(
        AdapterError,
        match=(
            "agent-work must start on the default branch 'main'; current branch is "
            "'agent/issue-12'. Switch to 'main', pull --ff-only, then launch "
            "agent-work again"
        ),
    ):
        discover_checkout_config(current_directory=tmp_path, run_command=commands)

    assert all("pull" not in command for command, _ in commands.calls)


@pytest.mark.parametrize(
    ("failure_at", "message"),
    (
        (5, "git pull --ff-only failed"),
        (2, "GitLab project lookup failed"),
    ),
)
def test_default_dependencies_name_the_failed_confirmation(
    tmp_path: Path, failure_at: int, message: str
) -> None:
    results = [
        CommandResult(0, str(tmp_path), ""),
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
        CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "main",
                    "web_url": "https://gitlab.example.com/group/project",
                }
            ),
            "",
        ),
        CommandResult(0, "main\n", ""),
        CommandResult(0, "", ""),
    ]
    results[failure_at] = CommandResult(1, "", "connection failed")

    with pytest.raises(AdapterError, match=message):
        build_default_dependencies(
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
                "labels": [],
                "state": "opened",
                "confidential": False,
                "created_at": "2026-08-01T00:00:00Z",
            }
        ]
    )
    commands = _Commands((CommandResult(0, payload, ""),))

    issues = GitLabGateway(_config(tmp_path), commands).list_eligible()

    assert [issue.iid for issue in issues] == [3]
    command, _ = commands.calls[0]
    assert "projects/310967/issues" in command
    assert "state=opened" in command
    assert "confidential=false" in command
    assert "not[labels]=agent::running,agent::awaiting-human,agent::failed" in command
    assert "per_page=1" in command
    assert "order_by=created_at" in command
    assert "sort=asc" in command
    assert not any("group/project" in argument for argument in command)


def test_gitlab_open_selection_filters_eligibility_in_one_bounded_query(
    tmp_path: Path,
) -> None:
    eligible = {
        "iid": 101,
        "title": "Eligible issue",
        "description": "",
        "labels": [],
        "state": "opened",
        "confidential": False,
    }
    commands = _Commands((CommandResult(0, json.dumps([eligible]), ""),))

    issues = GitLabGateway(_config(tmp_path), commands).list_eligible()

    assert [issue.iid for issue in issues] == [101]
    assert len(commands.calls) == 1
    command = commands.calls[0][0]
    assert "confidential=false" in command
    assert "not[labels]=agent::running,agent::awaiting-human,agent::failed" in command
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


def test_gitlab_reads_the_latest_default_branch_pipeline_by_numeric_project_id(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "id": 42,
                "status": "failed",
                "sha": "a" * 40,
                "web_url": "https://gitlab.example.com/group/project/-/pipelines/42",
            }
        ]
    )
    commands = _Commands((CommandResult(0, payload, ""),))
    pipeline = GitLabGateway(_config(tmp_path), commands).latest_default_pipeline(
        GitLabProject(
            id=310967,
            host="gitlab.example.com",
            default_branch="main",
            web_url="https://gitlab.example.com/group/project",
        )
    )

    assert pipeline == PipelineEvidence(
        id=42,
        status="failed",
        sha="a" * 40,
        web_url="https://gitlab.example.com/group/project/-/pipelines/42",
    )
    command = commands.calls[0][0]
    assert "projects/310967/pipelines" in command
    assert "ref=main" in command
    assert "per_page=1" in command


def test_gitlab_rejects_malformed_issue_text_as_a_safe_adapter_error(
    tmp_path: Path,
) -> None:
    payload = json.dumps(
        [
            {
                "iid": 3,
                "title": "Issue",
                "description": 42,
                "labels": [],
                "state": "opened",
                "confidential": False,
            }
        ]
    )
    commands = _Commands((CommandResult(0, payload, ""),))

    with pytest.raises(AdapterError, match="invalid issue data"):
        GitLabGateway(_config(tmp_path), commands).list_eligible()


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
        labels=(),
        state="opened",
        confidential=False,
    )

    GitLabGateway(_config(tmp_path), commands).mark_running(issue)

    update, _ = commands.calls[0]
    assert "projects/310967/issues/3" in update
    assert "add_labels=agent::running" in update
    assert "remove_labels=agent::awaiting-human,agent::failed" in update
    fetch, _ = commands.calls[1]
    assert "projects/310967/issues/3" in fetch


def test_gitlab_failure_comment_uses_the_numeric_issue_notes_endpoint(
    tmp_path: Path,
) -> None:
    commands = _Commands((CommandResult(0, "{}", ""),))
    issue = GitLabIssue(
        iid=3,
        title="Issue",
        labels=(),
        state="opened",
        confidential=False,
    )

    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        journal_tag="project-agent-work",
    )

    GitLabGateway(config, commands).comment_failed(
        issue, "Codex exited with status 7; no exact published MR evidence"
    )

    command, _ = commands.calls[0]
    assert command[:6] == (
        "glab",
        "api",
        "--hostname",
        "gitlab.example.com",
        "-X",
        "POST",
    )
    assert "projects/310967/issues/3/notes" in command
    assert any("agent::failed" in argument for argument in command)
    assert any(
        "journalctl --user -t project-agent-work --since today" in argument
        for argument in command
    )


@pytest.mark.parametrize(
    ("agent", "command", "stdin"),
    (
        pytest.param(
            "codex",
            (
                "codex",
                "exec",
                "-C",
                "{checkout}",
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=true",
                "--ephemeral",
                "--color",
                "never",
                "-",
            ),
            "untrusted prompt",
            id="codex",
        ),
        pytest.param(
            "claude",
            (
                "claude",
                "--print",
                "--output-format",
                "text",
                "--no-session-persistence",
                "--permission-mode",
                "acceptEdits",
                "--",
                "untrusted prompt",
            ),
            None,
            id="claude",
        ),
        pytest.param(
            "pi",
            (
                "pi",
                "--print",
                "--mode",
                "text",
                "--no-session",
                "untrusted prompt",
            ),
            None,
            id="pi",
        ),
    ),
)
def test_supported_agent_is_invoked_once_noninteractively(
    tmp_path: Path, agent: str, command: tuple[str, ...], stdin: str | None
) -> None:
    commands = _Commands((CommandResult(7, "ignored", "ignored"),))
    checkout = tmp_path / "checkout"

    invocation = AgentInvoker(commands, environment={"AGENT_WORK_AGENT": agent}).invoke(
        checkout, "untrusted prompt", 45
    )

    assert invocation == AgentInvocation(
        returncode=7, stdout="ignored", stderr="ignored"
    )
    assert commands.calls == [
        (
            tuple(part.replace("{checkout}", str(checkout)) for part in command),
            {"stdin": stdin, "timeout_seconds": 45},
        )
    ]


def test_agent_invocation_redacts_known_secret_values_from_diagnostics(
    tmp_path: Path,
) -> None:
    commands = _Commands(
        (CommandResult(1, "token=top-secret-value", "fatal: top-secret-value"),)
    )

    invocation = AgentInvoker(
        commands, environment={"GITLAB_TOKEN": "top-secret-value"}
    ).invoke(tmp_path, "prompt", 45)

    assert invocation.diagnostic == "token=[REDACTED]\nfatal: [REDACTED]"


def test_agent_invocation_rejects_an_unknown_agent(tmp_path: Path) -> None:
    commands = _Commands(())

    with pytest.raises(AdapterError, match="AGENT_WORK_AGENT"):
        AgentInvoker(commands, environment={"AGENT_WORK_AGENT": "unsupported"}).invoke(
            tmp_path, "prompt", 45
        )


def test_agent_invocation_uses_the_configured_absolute_executable(
    tmp_path: Path,
) -> None:
    commands = _Commands((CommandResult(0, "", ""),))

    AgentInvoker(
        commands,
        environment={
            "AGENT_WORK_AGENT": "claude",
            "AGENT_WORK_AGENT_PATH": "/opt/claude/bin/claude",
        },
    ).invoke(tmp_path, "prompt", 45)

    assert commands.calls[0][0][0] == "/opt/claude/bin/claude"


def test_worker_merge_request_draft_requires_one_valid_delimited_json_object() -> None:
    invocation = AgentInvocation(
        returncode=0,
        stdout=(
            "work complete\n<agent-work-merge-request>\n"
            '{"title":"fix: publish safely","description":"Validated."}\n'
            "</agent-work-merge-request>"
        ),
    )

    assert invocation.merge_request_draft() == MergeRequestDraft(
        title="fix: publish safely", description="Validated."
    )


def test_checkout_refuses_an_existing_deterministic_local_branch(
    tmp_path: Path,
) -> None:
    commands = _Commands(
        (
            CommandResult(0, "a" * 40, ""),
            CommandResult(2, "", ""),
            CommandResult(1, "", ""),
        )
    )

    with pytest.raises(BranchConflict, match="already exists"):
        ServiceCheckout(_config(tmp_path), commands).ensure_branch_available(
            "agent/issue-12", "main"
        )


def test_checkout_suggests_deleting_a_stale_merged_local_issue_branch(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout with space"
    config = AgentWorkConfig(
        host="gitlab.example.com", project_id=310967, checkout=checkout
    )
    commands = _Commands(
        (
            CommandResult(0, "a" * 40, ""),
            CommandResult(2, "", ""),
            CommandResult(0, "", ""),
        )
    )

    with pytest.raises(
        BranchConflict,
        match=(
            rf"already merged and absent from origin.*git -C '{checkout}' branch -d "
            r"agent/issue-12"
        ),
    ):
        ServiceCheckout(config, commands).ensure_branch_available(
            "agent/issue-12", "main"
        )


def test_checkout_stops_on_a_merge_base_validation_failure(tmp_path: Path) -> None:
    commands = _Commands(
        (
            CommandResult(0, "a" * 40, ""),
            CommandResult(2, "", ""),
            CommandResult(128, "", "fatal: bad revision"),
        )
    )

    with pytest.raises(AdapterError, match="checkout validation failed"):
        ServiceCheckout(_config(tmp_path), commands).ensure_branch_available(
            "agent/issue-12", "main"
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
    progress: list[str] = []

    def run(
        _dependencies: object,
        *,
        apply: bool,
        timeout_seconds: int,
        report_progress: Callable[[str], None],
    ) -> RunReport:
        assert not apply
        assert timeout_seconds == 1800
        report_progress("Issue #12 is ready on agent/issue-12.")
        return RunReport(outcome="preview_ready", issue_iid=12, branch="agent/issue-12")

    dependencies = cast(RunNextDependencies, _UnusedDependency())

    exit_code = main(
        ["run-next", "--json"],
        dependencies_factory=lambda: dependencies,
        run=run,
        write_stdout=lambda value: reports.append(json.loads(value)),
        write_stderr=progress.append,
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
    assert progress == [
        "Checking for next work item...",
        "Issue #12 is ready on agent/issue-12.",
    ]


def test_cli_reports_progress_before_starting_a_non_json_run() -> None:
    events: list[str] = []
    dependencies = cast(RunNextDependencies, _UnusedDependency())

    def dependencies_factory() -> RunNextDependencies:
        events.append("dependencies")
        return dependencies

    def run(
        _dependencies: object,
        *,
        apply: bool,
        timeout_seconds: int,
        report_progress: Callable[[str], None],
    ) -> RunReport:
        del apply, timeout_seconds
        report_progress("No eligible work items found.")
        events.append("run")
        return RunReport(outcome="idle")

    exit_code = main(
        ["run-next"],
        dependencies_factory=dependencies_factory,
        run=run,
        write_stdout=lambda message: events.append(f"stdout:{message}"),
        write_stderr=lambda message: events.append(f"stderr:{message}"),
    )

    assert exit_code == 0
    assert events == [
        "stderr:Checking for next work item...",
        "dependencies",
        "stderr:No eligible work items found.",
        "run",
        "stdout:idle",
    ]


def test_cli_prints_failed_agent_diagnostics_without_putting_them_in_json() -> None:
    errors: list[str] = []
    reports: list[dict[str, object]] = []
    report = RunReport(
        outcome="failed",
        issue_iid=3,
        branch="agent/issue-3",
        failure_reason="Agent exited with status 7; no exact published MR evidence",
        agent_diagnostic="Agent could not publish the branch",
    )

    exit_code = main(
        ["run-next", "--apply", "--json"],
        dependencies_factory=lambda: cast(RunNextDependencies, _UnusedDependency()),
        run=lambda _dependencies, **_kwargs: report,
        write_stdout=lambda value: reports.append(json.loads(value)),
        write_stderr=errors.append,
    )

    assert exit_code == 1
    assert errors == [
        "Checking for next work item...",
        "Failure: Agent exited with status 7; no exact published MR evidence",
        "Agent diagnostic (redacted):\nAgent could not publish the branch",
    ]
    assert reports == [
        {
            "branch": "agent/issue-3",
            "issue_iid": 3,
            "merge_request_url": None,
            "outcome": "failed",
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
    def run(
        _dependencies: object,
        *,
        apply: bool,
        timeout_seconds: int,
        report_progress: Callable[[str], None],
    ) -> RunReport:
        del apply, timeout_seconds, report_progress
        raise LockBusy("another run owns the project lock")

    errors: list[str] = []

    exit_code = main(
        ["run-next", "--apply"],
        dependencies_factory=lambda: cast(RunNextDependencies, _UnusedDependency()),
        run=run,
        write_stderr=errors.append,
    )

    assert exit_code == 75
    assert errors == [
        "Checking for next work item...",
        "another run owns the project lock",
    ]
