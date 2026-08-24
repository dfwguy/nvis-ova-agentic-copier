"""Remaining Phase 1 safety contracts not covered by the first runner slice."""

import json
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from agent_work.adapters import (
    AdapterFailure,
    CodexInvoker,
    CommandResult,
    LocalRunStore,
    WorktreeGateway,
)
from agent_work.cli import build_default_dependencies
from agent_work.models import GitLabConfig, GitLabIssue, WorkItem
from agent_work.result import AgentResult
from agent_work.runner import CodexRequest, RunDependencies, RunFailure, run_issue


def _result() -> dict[str, object]:
    """Return a valid published Phase 1 handoff result."""
    return {
        "schema_version": 1,
        "work_item_id": "gitlab:310967:123",
        "run_id": "run-123",
        "mode": "publish",
        "outcome": "awaiting_human",
        "summary": "Published.",
        "branch": "agent/issue-123",
        "branch_tip": "0123456789abcdef0123456789abcdef01234567",
        "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
        "tests": [],
        "findings": [],
        "next_action": "Human review",
        "error": None,
    }


def _work_item(config: GitLabConfig) -> WorkItem:
    """Build the sole normalized input accepted by apply-side callbacks."""
    return WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(iid=123, title="Bounded work", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )


@pytest.fixture(scope="module")
def result_schema(project_root: Path) -> Mapping[str, Any]:
    """Read trusted schema explicitly at the test filesystem boundary."""
    return json.loads(
        (project_root / ".agents/contracts/work-item-result-v1.schema.json").read_text()
    )


@pytest.mark.parametrize(
    ("ls_remote_status", "fresh_issue"), ((2, True), (1, False), (128, False))
)
def test_preflight_accepts_only_git_ls_remote_no_match_status_for_a_fresh_issue(
    tmp_path: Path,
    ls_remote_status: int,
    fresh_issue: bool,
) -> None:
    """Only Git's documented no-match result permits a fresh issue branch."""
    repository = tmp_path / "repository"
    nested = repository / "nested" / "directory"
    nested.mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if "projects/310967/pipelines?ref=main" in joined:
            return CommandResult(0, '[{"status":"success"}]', "")
        if "projects/310967/merge_requests?state=opened" in joined:
            return CommandResult(0, "[]", "")
        if "projects/310967" in joined:
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main",'
                '"http_url_to_repo":"https://gitlab.example.com/group/project.git",'
                '"ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        if "ls-remote" in joined:
            return CommandResult(ls_remote_status, "", "")
        return CommandResult(0, "", "")

    dependencies = build_default_dependencies(
        repository=nested,
        run_command=command,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    config = dependencies.load_config()
    if fresh_issue:
        dependencies.preflight_repository(config, _work_item(config))
    else:
        with pytest.raises(AdapterFailure):
            dependencies.preflight_repository(config, _work_item(config))

    if fresh_issue:
        assert (
            "git",
            "-C",
            str(nested),
            "rev-parse",
            "--show-toplevel",
        ) in calls
        assert (
            "git",
            "-C",
            str(repository),
            "fetch",
            "origin",
            "refs/heads/main:refs/remotes/origin/main",
        ) in calls
        assert ("uv", "run", "pytest") in calls
        assert any(
            "projects/310967/pipelines?ref=main&per_page=1" in call for call in calls
        )
        assert any("source_branch=agent/issue-123" in call for call in calls)


@pytest.mark.parametrize("pipeline_status", ("failed", "canceled"))
def test_preflight_rejects_unhealthy_main_pipeline_before_claim(
    tmp_path: Path, pipeline_status: str
) -> None:
    """A current default-branch failure blocks work before the remote claim boundary."""
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
    }

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if "pipelines?ref=main" in joined:
            return CommandResult(0, json.dumps([{"status": pipeline_status}]), "")
        if "projects/310967" in joined:
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main","http_url_to_repo":"https://gitlab.example.com/group/project.git","ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        return CommandResult(0, "[]" if "merge_requests" in joined else "", "")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment=environment,
    )
    config = dependencies.load_config()

    with pytest.raises(AdapterFailure):
        dependencies.preflight_repository(config, _work_item(config))


def test_preflight_binds_origin_refreshes_remote_tracking_main_and_captures_config(
    tmp_path: Path,
) -> None:
    """The configured numeric GitLab project binds Git origin before any claim."""
    repository = tmp_path / "repository"
    repository.mkdir()
    environment = {
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
    }
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if "projects/310967/merge_requests?state=opened" in joined:
            return CommandResult(0, "[]", "")
        if "projects/310967" in joined and "pipelines" not in joined:
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main","http_url_to_repo":"https://gitlab.example.com/group/project.git","ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        if "pipelines?ref=main" in joined:
            return CommandResult(0, '[{"status":"success"}]', "")
        return CommandResult(0, "", "")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment=environment,
    )
    config = dependencies.load_config()
    environment["GITLAB_PROJECT_ID"] = "999"
    dependencies.preflight_repository(config, _work_item(config))

    assert dependencies.load_config().project_id == 310967
    assert (
        "git",
        "-C",
        str(repository),
        "fetch",
        "origin",
        "refs/heads/main:refs/remotes/origin/main",
    ) in calls
    assert ("uv", "run", "pytest") in calls


def test_preflight_rejects_origin_mismatch_before_claim(tmp_path: Path) -> None:
    """A project ID alone cannot authorize an unrelated Git remote."""
    repository = tmp_path / "repository"
    repository.mkdir()

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.example.com/other/project.git\n", ""
            )
        if "projects/310967" in joined:
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main","http_url_to_repo":"https://gitlab.example.com/group/project.git","ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        return CommandResult(0, "", "")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )

    with pytest.raises(AdapterFailure):
        config = dependencies.load_config()
        dependencies.preflight_repository(config, _work_item(config))


def test_preflight_rejects_existing_remote_branch_without_an_open_merge_request(
    tmp_path: Path,
) -> None:
    """A remote issue branch without its handoff MR is a conflict, before claiming."""
    repository = tmp_path / "repository"
    repository.mkdir()
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if "projects/310967/pipelines?ref=main" in joined:
            return CommandResult(0, '[{"status":"success"}]', "")
        if "projects/310967" in joined:
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main",'
                '"http_url_to_repo":"https://gitlab.example.com/group/project.git",'
                '"ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        if "ls-remote" in joined:
            return CommandResult(
                0,
                "0123456789abcdef0123456789abcdef01234567\trefs/heads/agent/issue-123\n",
                "",
            )
        if "merge_requests?" in joined:
            return CommandResult(0, "[]", "")
        return CommandResult(0, "", "")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )

    with pytest.raises(AdapterFailure):
        config = dependencies.load_config()
        dependencies.preflight_repository(config, _work_item(config))

    assert not any("worktree add" in " ".join(call) for call in calls)


def test_default_repository_lock_requests_nonblocking_lock_and_keeps_config(
    tmp_path: Path,
) -> None:
    """CLI wiring keeps its first validated config and never waits on its local lock."""
    repository = tmp_path / "repository"
    repository.mkdir()
    lock_requests: list[bool] = []
    environment = {
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
    }

    class Store:
        root = tmp_path / "git-common" / "agent-work"

        @contextmanager
        def lock(self, *, blocking: bool) -> Any:
            lock_requests.append(blocking)
            yield

        def append_transition(self, _transition: Mapping[str, object]) -> None:
            return None

        def write_canonical_result(
            self, _run_id: str, _result: Mapping[str, object]
        ) -> Path:
            return self.root / "results" / "unused.json"

        def write_recovery_record(
            self, _run_id: str, _record: Mapping[str, object]
        ) -> Path:
            return self.root / "recovery" / "unused.json"

        def write_workspace_provenance(
            self, _work_item_id: str, _record: Mapping[str, object]
        ) -> Path:
            return self.root / "workspaces" / "unused.json"

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return None

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        return CommandResult(0, "", "")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment=environment,
        run_store=Store(),
    )
    captured = dependencies.load_config()
    environment["GITLAB_PROJECT_ID"] = "999"

    with dependencies.repository_lock("gitlab:310967:123"):
        pass

    assert lock_requests == [False]
    assert dependencies.load_config() == captured


def test_default_workspace_creation_rejects_a_symlinked_project_namespace_before_clone(
    tmp_path: Path,
) -> None:
    """The project-scoped runner state root is verified before clone mutation."""
    repository = tmp_path / "repository"
    common_git_dir = repository / ".git"
    common_git_dir.mkdir(parents=True)
    namespace = common_git_dir / "agent-work" / "gitlab.example.com-310967"
    namespace.parent.mkdir()
    target = tmp_path / "outside-state"
    target.mkdir()
    namespace.symlink_to(target, target_is_directory=True)
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if "--show-toplevel" in command:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in command:
            return CommandResult(0, f"{common_git_dir}\n", "")
        if "projects/310967" in " ".join(command):
            return CommandResult(
                0,
                '{"id":310967,"default_branch":"main",'
                '"http_url_to_repo":"https://gitlab.example.com/group/project.git",'
                '"ssh_url_to_repo":"git@gitlab.example.com:group/project.git"}',
                "",
            )
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(
                0, "https://gitlab.example.com/group/project.git\n", ""
            )
        if command[:2] == ("git", "clone"):
            raise AssertionError("a symlinked state root must block before clone")
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    config = dependencies.load_config()

    with pytest.raises(AdapterFailure):
        dependencies.create_worktree(_work_item(config))

    assert not any(command[:2] == ("git", "clone") for command in calls)


def test_codex_request_uses_no_runner_state_bridge_and_keeps_publish_network() -> None:
    """Codex can publish from its working copy without runner-state write access."""
    request = CodexRequest(
        worktree=Path("/linked/worktree"),
        prompt="prompt",
        schema_path=Path("/schema.json"),
        timeout_seconds=90,
    )

    assert request.command == (
        "codex",
        "exec",
        "-C",
        "/linked/worktree",
        "-s",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--output-schema",
        "/schema.json",
        "--json",
        "--ephemeral",
        "-",
    )
    assert "--add-dir" not in request.command
    assert "-o" not in request.command
    assert "dangerously-bypass-approvals-and-sandbox" not in request.command


def test_absolute_repository_dot_git_places_independent_workspace_outside_repository(
    tmp_path: Path,
) -> None:
    """The source common Git directory is never shared with the retained clone."""
    repository = tmp_path / "repository"
    repository.mkdir()
    source_git = repository / ".git"
    source_git.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace_root = tmp_path / "agent-workspaces" / "gitlab-310967"
    expected = workspace_root / "issue-123"
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command[:2] == ("git", "clone"):
            (expected / ".git").mkdir(parents=True)
            return CommandResult(0, "", "")
        if "checkout" in command:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    assert (
        WorktreeGateway(repository=repository, run_command=command).create_or_reuse(
            issue_iid=123, origin_url=origin, workspace_root=workspace_root
        )
        == expected
    )
    assert repository not in expected.parents
    assert (expected / ".git").resolve() != source_git.resolve()
    assert not any("worktree" in part for call in calls for part in call)


@pytest.mark.parametrize("child", ("lock", "journal.jsonl", "results"))
def test_local_store_rejects_symlinked_state_children(
    tmp_path: Path, child: str
) -> None:
    """No state-file or state-directory operation follows attacker-controlled links."""
    root = tmp_path / "state"
    root.mkdir()
    target = tmp_path / "target"
    if child == "results":
        target.mkdir()
    else:
        target.write_text("do not follow", encoding="utf-8")
    (root / child).symlink_to(target, target_is_directory=child == "results")
    store = LocalRunStore(root=root)

    with pytest.raises(AdapterFailure):
        if child == "lock":
            with store.lock(blocking=False):
                pass
        elif child == "journal.jsonl":
            store.append_transition({"from_status": "queued", "to_status": "claimed"})
        else:
            store.write_canonical_result("run-123", {"outcome": "awaiting_human"})


def test_local_store_rejects_a_symlink_at_the_exact_private_result_path(
    tmp_path: Path,
) -> None:
    """An attacker cannot redirect the already-known private result file."""
    root = tmp_path / "state"
    results = root / "results"
    results.mkdir(parents=True)
    target = tmp_path / "outside-result.json"
    target.write_text("do not overwrite", encoding="utf-8")
    (results / "run-123.json").symlink_to(target)

    with pytest.raises(AdapterFailure):
        LocalRunStore(root=root).write_canonical_result(
            "run-123", {"outcome": "awaiting_human"}
        )


def test_claimed_running_journal_failure_stops_before_worktree_or_codex(
    result_schema: Mapping[str, Any], tmp_path: Path
) -> None:
    """Losing durable running-state recording means no repository mutation may start."""
    events: list[str] = []

    def issue(_config: GitLabConfig, _iid: int) -> GitLabIssue:
        events.append("issue")
        return GitLabIssue(iid=123, title="Run", labels=("agent::ready",))

    def journal(transition: Mapping[str, object]) -> None:
        events.append(f"journal:{transition['from_status']}->{transition['to_status']}")
        if transition["to_status"] == "running":
            raise OSError("disk unavailable")

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=issue,
        repository_lock=lambda _work_item_id: _lock(),
        preflight_repository=lambda _config, _work_item: events.append("preflight"),
        create_worktree=lambda _work_item: _unexpected(events, "worktree"),
        invoke_codex=lambda _request: _unexpected(events, "codex"),
        verify_publication=lambda _result, _worktree: None,
        append_journal=journal,
        project_issue=lambda _result: events.append("project_failure"),
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: result_schema,
        claim_issue=lambda _item: events.append("claim"),
        schema_path=lambda: tmp_path / "schema.json",
        write_canonical_result=lambda _run_id, _result: None,
        write_recovery_record=lambda _run_id, _record: None,
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: "0123456789abcdef0123456789abcdef01234567",
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=dependencies,
        )

    assert "worktree" not in events
    assert "codex" not in events
    assert "project_failure" in events


@pytest.mark.parametrize("jsonl_event", ("[]\n", "1\n"))
def test_runner_recovers_after_claim_when_codex_jsonl_event_is_not_an_object(
    jsonl_event: str, result_schema: Mapping[str, Any], tmp_path: Path
) -> None:
    """A malformed child event becomes a safe post-claim retry rather than a traceback."""
    transitions: list[Mapping[str, object]] = []
    projected: list[AgentResult] = []

    @contextmanager
    def lock() -> Any:
        yield

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=lambda _config, _iid: GitLabIssue(
            iid=123, title="Run", labels=("agent::ready",)
        ),
        repository_lock=lambda _work_item_id: lock(),
        preflight_repository=lambda _config, _work_item: None,
        create_worktree=lambda _work_item: tmp_path / "retained-worktree",
        invoke_codex=CodexInvoker(
            run_command=lambda _command, **_kwargs: CommandResult(0, jsonl_event, "")
        ),
        verify_publication=lambda _result, _worktree: None,
        append_journal=transitions.append,
        project_issue=projected.append,
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: result_schema,
        claim_issue=lambda _work_item: None,
        schema_path=lambda: tmp_path / "schema.json",
        write_canonical_result=lambda _run_id, _document: None,
        write_recovery_record=lambda _run_id, _record: None,
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: "0123456789abcdef0123456789abcdef01234567",
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=dependencies,
        )

    assert transitions[-1]["from_status"] == "running"
    assert transitions[-1]["to_status"] in {"retry_wait", "dead_letter"}
    assert projected[-1].outcome in {"retry", "dead_letter"}


@pytest.mark.parametrize(
    "failure_stage",
    ("worktree", "codex", "validation", "verification", "persistence"),
)
def test_post_claim_failures_release_running_with_a_schema_valid_failure_result(
    failure_stage: str,
    result_schema: Mapping[str, Any],
    tmp_path: Path,
) -> None:
    """Every applied post-claim failure releases the remote claim and preserves recovery data."""
    transitions: list[Mapping[str, object]] = []
    projected: list[AgentResult] = []

    @contextmanager
    def lock() -> Any:
        yield

    def issue(_config: GitLabConfig, _iid: int) -> GitLabIssue:
        return GitLabIssue(iid=123, title="Run", labels=("agent::ready",))

    def worktree(_work_item: WorkItem) -> Path:
        if failure_stage == "worktree":
            raise OSError("worktree unavailable")
        return tmp_path / "retained-worktree"

    def codex(_request: CodexRequest) -> dict[str, object]:
        if failure_stage == "codex":
            raise OSError("Codex failed")
        return {} if failure_stage == "validation" else _result()

    def verify(_result_value: object, worktree_path: Path) -> None:
        assert worktree_path == tmp_path / "retained-worktree"
        if failure_stage == "verification":
            raise OSError("MR verification failed")

    def persist(_run_id: str, _document: Mapping[str, object]) -> None:
        if (
            failure_stage == "persistence"
            and _document.get("outcome") == "awaiting_human"
        ):
            raise OSError("result persistence failed")

    def project(result: AgentResult) -> None:
        projected.append(result)
        if failure_stage == "projection" and result.outcome == "awaiting_human":
            raise OSError("projection failed")

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=issue,
        repository_lock=lambda _work_item_id: lock(),
        preflight_repository=lambda _config, _work_item: None,
        create_worktree=worktree,
        invoke_codex=codex,
        verify_publication=verify,
        append_journal=transitions.append,
        project_issue=project,
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: result_schema,
        claim_issue=lambda _item: None,
        schema_path=lambda: tmp_path / "schema.json",
        write_canonical_result=persist,
        write_recovery_record=lambda _run_id, _record: None,
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: "0123456789abcdef0123456789abcdef01234567",
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=dependencies,
        )

    failure_result = projected[-1]
    assert failure_result.outcome in {"retry", "dead_letter"}
    Draft202012Validator(result_schema, format_checker=FormatChecker()).validate(
        failure_result.document
    )
    if failure_stage != "worktree":
        assert failure_result.branch == "agent/issue-123"
        assert failure_result.branch_tip is None
        assert failure_result.merge_request_url is None
    assert transitions[-1]["to_status"] in {"retry_wait", "dead_letter"}
    if failure_stage != "worktree":
        assert transitions[-1]["branch"] == "agent/issue-123"


def test_mismatched_retained_worktree_head_blocks_publication_projection(
    result_schema: Mapping[str, Any], tmp_path: Path
) -> None:
    """The reported SHA is not sufficient when the retained worktree points elsewhere."""
    projected: list[AgentResult] = []

    @contextmanager
    def lock() -> Any:
        yield

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=lambda _config, _iid: GitLabIssue(
            iid=123, title="Run", labels=("agent::ready",)
        ),
        repository_lock=lambda _work_item_id: lock(),
        preflight_repository=lambda _config, _work_item: None,
        create_worktree=lambda _work_item: tmp_path / "retained-worktree",
        invoke_codex=lambda _request: _result(),
        verify_publication=lambda _result_value, _worktree: None,
        append_journal=lambda _transition: None,
        project_issue=projected.append,
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: result_schema,
        claim_issue=lambda _item: None,
        schema_path=lambda: tmp_path / "schema.json",
        write_canonical_result=lambda _run_id, _document: None,
        write_recovery_record=lambda _run_id, _record: None,
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: "fedcba9876543210fedcba9876543210fedcba98",
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=dependencies,
        )

    assert projected
    assert projected[-1].outcome in {"retry", "dead_letter"}


def _unexpected(events: list[str], event: str) -> Path:
    """Record and reject a boundary that must not run in this scenario."""
    events.append(event)
    raise AssertionError(f"unexpected {event}")


class _lock:
    """Tiny injected context manager for hermetic runner tests."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> bool:
        return False
