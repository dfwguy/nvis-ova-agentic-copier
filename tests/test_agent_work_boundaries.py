"""Behavior tests for GitLab, Git, and worker process boundaries."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_subprocess import FakeProcess

from agent_work.cli import main
from agent_work.domain import LIFECYCLE_LABELS
from agent_work.gitlab import GitLabError, GitLabGateway
from agent_work.models import AgentWorkConfig, CommandReport, MergeRequestDraft
from agent_work.repository import (
    CommandResult,
    GitRepository,
    RepositoryError,
    _origin_identity,
    discover_config,
    run_subprocess,
)
from agent_work.runtime import _build_local_logger
from agent_work.worker import AgentInvoker, WorkerProtocolError, _agent_command


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
    )


def _issue_payload(iid: int = 12, labels: list[str] | None = None) -> dict[str, object]:
    return {
        "iid": iid,
        "title": "Work",
        "description": None,
        "labels": labels or [],
        "state": "opened",
        "confidential": False,
        "created_at": "2026-01-01T00:00:00Z",
        "author": {"username": "ignored"},
    }


def _mr_payload(description: str = "") -> dict[str, object]:
    return {
        "iid": 88,
        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/88",
        "state": "opened",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "source_branch": "agent/issue-12",
        "target_branch": "main",
        "sha": "a" * 40,
        "has_conflicts": False,
        "description": description,
        "references": {"short": "!88"},
    }


class ApiStub:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.issue = _issue_payload(labels=["agent::ready"])

    def __call__(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        self.commands.append(command)
        endpoint = command[command.index("-X") + 2]
        method = command[command.index("-X") + 1]
        if endpoint == "projects/310967":
            payload: object = {
                "id": 310967,
                "host": "ignored",
                "default_branch": "main",
                "web_url": "https://gitlab.example.com/group/project",
                "namespace": {"name": "ignored"},
            }
        elif endpoint.endswith("/issues"):
            payload = [self.issue, _issue_payload(13, ["agent::running"])]
        elif endpoint.endswith("/issues/12"):
            if method == "PUT":
                additions = [
                    part.removeprefix("add_labels=")
                    for part in command
                    if part.startswith("add_labels=")
                ]
                self.issue["labels"] = additions
            payload = self.issue
        elif endpoint.endswith("/merge_requests"):
            payload = _mr_payload() if method == "POST" else [_mr_payload()]
        elif endpoint.endswith("/merge_requests/88"):
            payload = _mr_payload("Closes #12")
        elif endpoint.endswith("/closes_issues"):
            payload = [{"iid": 12, "project_id": 310967}]
        elif endpoint.endswith("/pipelines"):
            payload = [
                {
                    "id": 3,
                    "status": "success",
                    "sha": "a" * 40,
                    "web_url": "https://pipeline/3",
                    "ref": "ignored",
                },
                {
                    "id": 2,
                    "status": "failed",
                    "sha": "b" * 40,
                    "web_url": "https://pipeline/2",
                },
            ]
        elif endpoint.endswith("/notes"):
            payload = {"id": 1}
        else:
            raise AssertionError(endpoint)
        return CommandResult(0, json.dumps(payload), "")


@pytest.mark.parametrize("target", [*LIFECYCLE_LABELS, None])
def test_gitlab_lifecycle_transition_never_requests_target_removal(
    tmp_path: Path, target: str | None
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    gateway.transition_labels(12, target)

    update = next(command for command in api.commands if "PUT" in command)
    remove_field = next(part for part in update if part.startswith("remove_labels="))
    removed = set(remove_field.removeprefix("remove_labels=").split(","))
    assert removed == set(LIFECYCLE_LABELS).difference({target})
    if target is not None:
        assert f"add_labels={target}" in update


def test_gitlab_gateway_enforces_ready_selection_and_exact_evidence(
    tmp_path: Path,
) -> None:
    api = ApiStub()
    gateway = GitLabGateway(_config(tmp_path), api)

    assert gateway.verify_project().id == 310967
    assert [issue.iid for issue in gateway.list_ready()] == [12]
    assert {issue.iid for issue in gateway.list_projected()} == {12, 13}
    assert gateway.fetch_issue(12).description == ""
    assert gateway.transition_labels(12, "agent::running").labels == ("agent::running",)
    assert gateway.fetch_merge_request(88).iid == 88
    assert gateway.find_merge_request_by_branch("agent/issue-12") is not None
    draft = MergeRequestDraft(title="Change", description="Details")
    assert (
        gateway.create_merge_request(
            branch="agent/issue-12", target="main", issue_iid=12, draft=draft
        ).iid
        == 88
    )
    mr = gateway.ensure_closing_issue_reference(gateway.fetch_merge_request(88), 12)
    assert mr.description == "Closes #12"
    assert gateway.mr_closes_issue(88, 12)
    pipeline = gateway.latest_pipeline_for_sha(88, "a" * 40)
    assert pipeline is not None and pipeline.status == "success"
    assert gateway.latest_pipeline_for_sha(88, "c" * 40) is None
    gateway.comment_outcome(
        12, error_code="blocked", summary="Safe", next_action="Review"
    )
    assert all(
        "projects/310967" in command[command.index("-X") + 2]
        for command in api.commands
    )


def test_gitlab_projected_inventory_reads_every_issue_page(tmp_path: Path) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        page_field = next(
            (part for part in command if part.startswith("page=")), "page=1"
        )
        page = int(page_field.removeprefix("page="))
        payload = (
            [_issue_payload(iid, ["agent::running"]) for iid in range(1, 101)]
            if page == 1
            else [_issue_payload(101, ["agent::running"])]
        )
        return CommandResult(0, json.dumps(payload), "")

    issues = GitLabGateway(config, fake).list_projected()

    assert len(issues) == 101
    assert issues[-1].iid == 101


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (CommandResult(1, "secret", "secret"), "request failed"),
        (CommandResult(0, "not-json", ""), "invalid JSON"),
    ],
)
def test_gitlab_gateway_redacts_failed_or_invalid_responses(
    tmp_path: Path, result: CommandResult, message: str
) -> None:
    gateway = GitLabGateway(_config(tmp_path), lambda *_args, **_kwargs: result)
    with pytest.raises(GitLabError, match=message):
        gateway.verify_project()


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            "https://gitlab.example.com/group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
        (
            "git@gitlab.example.com:group/project.git",
            ("gitlab.example.com", "group/project"),
        ),
    ],
)
def test_origin_identity_accepts_https_and_scp(
    origin: str, expected: tuple[str, str]
) -> None:
    assert _origin_identity(origin) == expected


def test_origin_identity_rejects_an_unusable_remote() -> None:
    with pytest.raises(RepositoryError):
        _origin_identity("local-path")


def test_discover_config_is_read_only_and_confirms_numeric_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if "rev-parse" in command:
            return CommandResult(0, str(checkout), "")
        if "get-url" in command:
            return CommandResult(0, "https://gitlab.example.com/group/project.git", "")
        return CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "main",
                    "path_with_namespace": "group/project",
                }
            ),
            "",
        )

    config = discover_config(
        current_directory=tmp_path,
        environment={
            "XDG_STATE_HOME": str(tmp_path / "xdg"),
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
        run_command=fake,
    )
    assert config.project_id == 310967
    assert config.state_root.parent.name == "projects"
    assert not any("pull" in command for command in calls)


def test_run_subprocess_captures_output_and_wraps_timeouts(fp: FakeProcess) -> None:
    fp.register(["printf", "ok"], stdout="ok")
    assert run_subprocess(("printf", "ok")).stdout == "ok"

    def timeout(_process: object) -> None:
        raise subprocess.TimeoutExpired(("slow-command",), 1)

    fp.register(["slow-command"], callback=timeout)
    with pytest.raises(RepositoryError, match="required command"):
        run_subprocess(("slow-command",), timeout_seconds=0.001)


def test_repository_prepares_publishes_and_inventories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()
    responses: dict[tuple[str, ...], CommandResult] = {}
    calls: list[tuple[str, ...]] = []

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        args = command[3:]
        if args in responses:
            return responses[args]
        if args == ("branch", "--show-current"):
            branch = "main" if Path(command[2]) == config.checkout else "agent/issue-12"
            return CommandResult(0, f"{branch}\n", "")
        if args == ("status", "--porcelain"):
            return CommandResult(0, "M file\n", "")
        if args == ("rev-parse", "HEAD") or args == (
            "rev-parse",
            "origin/agent/issue-12",
        ):
            return CommandResult(0, "a" * 40, "")
        if args[:2] == ("for-each-ref", "--format=%(refname:short)"):
            return CommandResult(0, "agent/issue-12\n", "")
        if args[:2] == ("worktree", "list"):
            return CommandResult(0, f"worktree {config.checkout}\n", "")
        if args[:2] == ("stash", "list"):
            return CommandResult(0, "agent-work recovery issue-12\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)
    with pytest.raises(RepositoryError, match="clean"):
        repository.refresh_base()
    responses[("status", "--porcelain")] = CommandResult(0, "", "")
    repository.refresh_base()
    worktree = repository.create_issue_worktree(12, "agent/issue-12", "main")
    assert worktree == config.state_root / "worktrees" / "12"
    responses[("status", "--porcelain")] = CommandResult(0, "M file\n", "")
    assert (
        repository.commit_and_push(
            worktree, "agent/issue-12", 12, expected_head="a" * 40
        )
        == "a" * 40
    )
    branches, worktrees, stashes = repository.inventory()
    assert branches == ("agent/issue-12",)
    assert worktrees == (str(config.checkout),)
    assert stashes == ("agent-work recovery issue-12",)
    assert any("pull" in command for command in calls)


def test_existing_mr_worktree_requires_the_recorded_remote_sha(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkout.mkdir()

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--list", "review/existing"):
            return CommandResult(0, "review/existing\n", "")
        if arguments == ("rev-parse", "origin/review/existing"):
            return CommandResult(0, "b" * 40, "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)

    with pytest.raises(RepositoryError, match="recorded merge request SHA"):
        repository.worktree_for_existing_branch(12, "review/existing", "a" * 40)


@pytest.mark.parametrize(
    ("branch", "head", "status"),
    [
        ("other", "a" * 40, "M src/example.py\n"),
        ("agent/issue-12", "b" * 40, "M src/example.py\n"),
        ("agent/issue-12", "a" * 40, ""),
    ],
)
def test_publication_rejects_wrong_branch_worker_commits_and_empty_changes(
    tmp_path: Path, branch: str, head: str, status: str
) -> None:
    config = _config(tmp_path)

    def fake(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        arguments = command[3:]
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, branch, "")
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(0, head, "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(0, status, "")
        return CommandResult(0, "", "")

    repository = GitRepository(config, fake)

    with pytest.raises(RepositoryError):
        repository.commit_and_push(
            tmp_path / "worktree",
            "agent/issue-12",
            12,
            expected_head="a" * 40,
        )


def test_agent_profiles_are_argv_only_and_invocation_redacts_secrets(
    tmp_path: Path,
) -> None:
    for name in ("codex", "claude", "pi"):
        command, stdin = _agent_command(name, f"/{name}", tmp_path, "prompt")
        assert command[0] == f"/{name}"
        assert (stdin == "prompt") is (name == "codex")
    with pytest.raises(WorkerProtocolError):
        _agent_command("other", "/other", tmp_path, "prompt")

    invoker = AgentInvoker(
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="token-value", stderr="token-value"
        ),
        environment={"AGENT_WORK_AGENT": "codex", "SECRET_TOKEN": "token-value"},
    )
    result = invoker.invoke(tmp_path, "prompt", 10)
    assert result.stdout == result.stderr == "[REDACTED]"


def test_local_operational_log_keeps_info_and_debug_at_requested_verbosity(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    ordinary = _build_local_logger(config, debug=False)
    ordinary.info("ordinary lifecycle")
    ordinary.debug("hidden diagnostic")
    for handler in ordinary.handlers:
        handler.flush()
    log_path = config.state_root / "agent-work.log"
    assert log_path.read_text() == "ordinary lifecycle\n"

    debug = _build_local_logger(config, debug=True)
    debug.debug("visible diagnostic")
    for handler in debug.handlers:
        handler.flush()
    assert "visible diagnostic" in log_path.read_text()


def test_local_operational_log_records_command_outcomes_and_failures(
    tmp_path: Path,
) -> None:
    logger = _build_local_logger(_config(tmp_path), debug=False)

    assert (
        main(
            ["loop", "status"],
            dispatch=lambda *_args, **_kwargs: CommandReport(
                command="loop.status", applied=False, outcome="status"
            ),
        )
        == 0
    )

    def fail(*_args: object, **_kwargs: object) -> CommandReport:
        raise RuntimeError("ledger unavailable")

    assert main(["loop", "status"], dispatch=fail) == 1
    for handler in logger.handlers:
        handler.flush()
    contents = (_config(tmp_path).state_root / "agent-work.log").read_text()
    assert "outcome=status command=loop.status" in contents
    assert "failure=ledger unavailable" in contents
