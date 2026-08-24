"""Regression contracts for Phase 1 safety findings from quality review."""

import json
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_work.adapters import (
    AdapterFailure,
    CommandResult,
    GitLabGateway,
    LocalRunStore,
    WorktreeGateway,
)
from agent_work.cli import build_default_dependencies
from agent_work.models import GitLabConfig, GitLabIssue, WorkItem
from agent_work.result import AgentResult, validate_agent_result
from agent_work.runner import CodexRequest, RunDependencies, RunFailure, run_issue


@pytest.fixture(scope="module")
def canonical_result_schema(project_root: Path) -> Mapping[str, Any]:
    """Load the canonical result contract only at the test boundary."""
    return json.loads(
        (project_root / ".agents/contracts/work-item-result-v1.schema.json").read_text()
    )


def _published_result(
    *, issue_iid: int = 123, run_id: str = "run-123"
) -> dict[str, object]:
    """Return one complete verified publish handoff document."""
    return {
        "schema_version": 1,
        "work_item_id": f"gitlab:310967:{issue_iid}",
        "run_id": run_id,
        "mode": "publish",
        "outcome": "awaiting_human",
        "summary": "Published a merge request.",
        "branch": f"agent/issue-{issue_iid}",
        "branch_tip": "0123456789abcdef0123456789abcdef01234567",
        "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
        "tests": [],
        "findings": [],
        "next_action": "Human review and merge",
        "error": None,
    }


def _create_independent_workspace(
    *, repository: Path, origin_url: str
) -> tuple[Path, list[tuple[str, ...]]]:
    """Create one retained clone through an injected Git command transcript."""
    calls: list[tuple[str, ...]] = []
    (repository / ".git").mkdir(parents=True)

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "remote get-url origin" in joined:
            return CommandResult(0, f"{origin_url}\n", "")
        if len(command) >= 2 and command[:2] == ("git", "clone"):
            (Path(command[-1]) / ".git").mkdir(parents=True)
            return CommandResult(0, "", "")
        if "checkout" in command:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    workspace = WorktreeGateway(
        repository=repository, run_command=command
    ).create_or_reuse(issue_iid=123)
    return workspace, calls


def test_same_issue_iid_uses_project_unique_independent_workspaces(
    tmp_path: Path,
) -> None:
    """Two project checkouts cannot share an IID workspace or its Git directory."""
    first_repository = tmp_path / "first-project"
    second_repository = tmp_path / "second-project"
    first_repository.mkdir()
    second_repository.mkdir()

    first, first_calls = _create_independent_workspace(
        repository=first_repository,
        origin_url="https://gitlab.one.example/group/first.git",
    )
    second, second_calls = _create_independent_workspace(
        repository=second_repository,
        origin_url="https://gitlab.two.example/group/second.git",
    )

    assert first != second
    assert (first / ".git").is_dir()
    assert (second / ".git").is_dir()
    assert (first / ".git").resolve() != (first_repository / ".git").resolve()
    assert (second / ".git").resolve() != (second_repository / ".git").resolve()
    assert all(
        any(call[:2] == ("git", "clone") for call in calls)
        and not any("worktree add" in " ".join(call) for call in calls)
        for calls in (first_calls, second_calls)
    )
    assert any(
        "https://gitlab.one.example/group/first.git" in call for call in first_calls
    )
    assert any(
        "https://gitlab.two.example/group/second.git" in call for call in second_calls
    )
    for workspace, calls in ((first, first_calls), (second, second_calls)):
        assert (
            "git",
            "-C",
            str(workspace),
            "checkout",
            "-b",
            "agent/issue-123",
            "origin/main",
        ) in calls


def test_default_wiring_scopes_same_issue_worktree_by_gitlab_host_and_project(
    tmp_path: Path,
) -> None:
    """Configured project identity, not an IID alone, owns a retained clone."""
    repository = tmp_path / "repository"
    repository.mkdir()
    created_paths: list[Path] = []
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if command[:2] == ("glab", "api"):
            project_id = int(command[-1].rsplit("/", maxsplit=1)[1])
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": project_id,
                        "default_branch": "main",
                        "http_url_to_repo": (
                            f"https://{command[3]}/group/project-{project_id}.git"
                        ),
                        "ssh_url_to_repo": (
                            f"git@{command[3]}:group/project-{project_id}.git"
                        ),
                    }
                ),
                "",
            )
        if "remote get-url origin" in joined:
            return CommandResult(
                0, "https://gitlab.one.example/group/project-310967.git\n", ""
            )
        if command[:2] == ("git", "clone"):
            created_paths.append(Path(command[-1]))
            (Path(command[-1]) / ".git").mkdir(parents=True)
            return CommandResult(0, "", "")
        if "checkout" in command:
            return CommandResult(0, "", "")
        if "worktree add" in joined:
            raise AssertionError(
                "independent workspace creation must not use git worktree"
            )
        raise AssertionError(f"unexpected command: {command}")

    identities = (
        ("gitlab.one.example", "310967"),
        ("gitlab.one.example", "310968"),
        ("gitlab.two.example", "310967"),
    )
    for host, project_id in identities:
        dependencies = build_default_dependencies(
            repository=repository,
            run_command=command,
            environment={"GITLAB_HOST": host, "GITLAB_PROJECT_ID": project_id},
        )
        config = dependencies.load_config()
        dependencies.create_worktree(
            WorkItem.from_issue(
                config=config,
                issue=GitLabIssue(
                    iid=123, title="Bounded work", labels=("agent::ready",)
                ),
                run_id="run-123",
                mode="publish",
            )
        )

    assert len(set(created_paths)) == len(identities)
    assert all((path / ".git").is_dir() for path in created_paths)
    verified_origins = {
        f"https://{host}/group/project-{project_id}.git"
        for host, project_id in identities
    }
    clone_sources = {
        value
        for call in calls
        if call[:2] == ("git", "clone")
        for value in call
        if value in verified_origins
    }
    assert clone_sources == verified_origins
    for path in created_paths:
        assert (
            "git",
            "-C",
            str(path),
            "checkout",
            "-b",
            "agent/issue-123",
            "origin/main",
        ) in calls


def test_injected_run_store_exposes_atomic_result_recovery_and_provenance(
    tmp_path: Path,
) -> None:
    """CLI wiring uses injected durable operations, never a concrete-store check."""
    canonical_writes: list[tuple[str, Mapping[str, object]]] = []
    recovery_writes: list[tuple[str, Mapping[str, object]]] = []
    provenance_writes: list[tuple[str, Mapping[str, object]]] = []

    class Store:
        root = tmp_path / "git-common" / "agent-work"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
            yield

        def append_transition(self, _transition: Mapping[str, object]) -> None:
            return None

        def write_canonical_result(
            self, run_id: str, result: Mapping[str, object]
        ) -> Path:
            canonical_writes.append((run_id, dict(result)))
            return self.root / "results" / f"{run_id}.json"

        def write_recovery_record(
            self, run_id: str, record: Mapping[str, object]
        ) -> Path:
            recovery_writes.append((run_id, dict(record)))
            return self.root / "recovery" / f"{run_id}.json"

        def write_workspace_provenance(
            self, work_item_id: str, provenance: Mapping[str, object]
        ) -> Path:
            provenance_writes.append((work_item_id, dict(provenance)))
            return self.root / "workspaces" / f"{work_item_id}.json"

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return None

    store = Store()
    dependencies = build_default_dependencies(
        repository=tmp_path / "source",
        run_store=store,
    )

    dependencies.write_canonical_result("run-123", _published_result())

    assert canonical_writes == [("run-123", _published_result())]
    assert recovery_writes == []
    assert provenance_writes == []


@pytest.mark.parametrize("callback_name", ("preflight_repository", "create_worktree"))
def test_default_apply_callbacks_reject_branch_strings_before_command_access(
    callback_name: str, tmp_path: Path
) -> None:
    """Only a normalized WorkItem may select or create an independent workspace."""
    commands: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        raise AssertionError("string compatibility must fail before subprocess access")

    dependencies = build_default_dependencies(
        repository=tmp_path / "source",
        run_command=command,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    config = dependencies.load_config()
    callback = getattr(dependencies, callback_name)

    with pytest.raises(TypeError):
        callback(
            config, "agent/issue-123"
        ) if callback_name == "preflight_repository" else callback("agent/issue-123")

    assert commands == []


def test_codex_child_receives_a_self_contained_working_copy_only() -> None:
    """Codex receives no runner-state path but retains constrained publish networking."""
    request = CodexRequest(
        worktree=Path("/worktrees/issue-123"),
        prompt="prompt",
        schema_path=Path("/schema.json"),
        timeout_seconds=90,
    )

    assert "sandbox_workspace_write.network_access=true" in request.command
    assert "--add-dir" not in request.command
    assert "-o" not in request.command
    assert "--output-schema" in request.command
    assert "--json" in request.command


def test_validated_canonical_result_cannot_be_mutated_after_validation(
    canonical_result_schema: Mapping[str, Any],
) -> None:
    """Recovery handling cannot alter the canonical document it has already verified."""
    result = validate_agent_result(
        _published_result(),
        work_item_id="gitlab:310967:123",
        run_id="run-123",
        mode="publish",
        schema=canonical_result_schema,
    )

    with pytest.raises(TypeError):
        result.document["summary"] = "rewritten during recovery"  # type: ignore[index]
    with pytest.raises(AttributeError):
        result.document["tests"].append({"command": "mutated"})  # type: ignore[union-attr]


def test_post_handoff_recovery_keeps_canonical_result_and_verified_artifacts(
    canonical_result_schema: Mapping[str, Any], tmp_path: Path
) -> None:
    """Projection observability failure cannot demote an already verified handoff."""
    canonical = _published_result()
    writes: list[Mapping[str, object]] = []
    recovery_writes: list[Mapping[str, object]] = []
    transitions: list[Mapping[str, object]] = []
    projected: list[AgentResult] = []

    @contextmanager
    def lock() -> Iterator[None]:
        yield

    def project(result: AgentResult) -> None:
        projected.append(result)
        if result.outcome == "awaiting_human":
            raise OSError("final projection unavailable")

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=lambda _config, iid: GitLabIssue(
            iid=iid, title="Bounded", labels=("agent::ready",)
        ),
        repository_lock=lambda _work_item_id: lock(),
        preflight_repository=lambda _config, _work_item: None,
        create_worktree=lambda _work_item: tmp_path / "retained-worktree",
        invoke_codex=lambda _request: canonical,
        verify_publication=lambda _result, _worktree: None,
        append_journal=transitions.append,
        project_issue=project,
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: canonical_result_schema,
        claim_issue=lambda _work_item: None,
        write_canonical_result=lambda _run_id, document: writes.append(dict(document)),
        write_recovery_record=lambda _run_id, document: recovery_writes.append(
            dict(document)
        ),
        schema_path=lambda: tmp_path / "schema.json",
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: str(canonical["branch_tip"]),
    )

    report = run_issue(
        issue_iid=123,
        mode="publish",
        apply=True,
        timeout_seconds=90,
        dependencies=dependencies,
    )

    recovery_transition = transitions[-1]
    assert report.result is not None
    assert report.result.outcome == "awaiting_human"
    assert writes == [canonical]
    assert len(recovery_writes) == 1
    recovery = recovery_writes[0]
    assert recovery["stage"] == "project_issue"
    assert recovery["canonical_result_digest"] == "digest"
    assert recovery["outcome"] == "awaiting_human"
    assert recovery["branch"] == canonical["branch"]
    assert recovery["branch_tip"] == canonical["branch_tip"]
    assert recovery["merge_request_url"] == canonical["merge_request_url"]
    assert recovery["occurred_at"] == "2026-08-24T00:00:00+00:00"
    assert (
        recovery["status_reason"]
        == "issue projection failed after verified publication"
    )
    assert "final projection unavailable" not in json.dumps(recovery)
    assert [result.outcome for result in projected] == ["awaiting_human"]
    assert recovery_transition["from_status"] == "running"
    assert recovery_transition["to_status"] == "awaiting_human"
    assert recovery_transition["branch"] == canonical["branch"]
    assert recovery_transition["branch_tip"] == canonical["branch_tip"]
    assert recovery_transition["merge_request_url"] == canonical["merge_request_url"]


def test_local_store_never_overwrites_a_canonical_result_for_the_same_run(
    tmp_path: Path,
) -> None:
    """A recovery record cannot replace a prior verified result document."""
    store = LocalRunStore(root=tmp_path / "state")
    canonical = _published_result()
    result_path = store.write_canonical_result("run-123", canonical)
    first_contents = result_path.read_text(encoding="utf-8")

    with pytest.raises(AdapterFailure):
        store.write_canonical_result(
            "run-123",
            {
                **canonical,
                "outcome": "retry",
                "summary": "Overwrote verified handoff",
                "error": {
                    "code": "recovery",
                    "message": "must be separately stored",
                    "retryable": True,
                },
            },
        )

    assert result_path.read_text(encoding="utf-8") == first_contents


def test_run_store_uses_an_injected_atomic_writer_for_result_and_journal(
    tmp_path: Path,
) -> None:
    """Result and audit persistence share one injected durable atomic write boundary."""
    writes: list[tuple[Path, str, int]] = []

    def atomic_write(path: Path, content: str, mode: int) -> None:
        writes.append((path, content, mode))

    store = LocalRunStore(root=tmp_path / "state", atomic_write=atomic_write)

    store.write_canonical_result("run-123", {"outcome": "awaiting_human"})
    store.append_transition({"from_status": "queued", "to_status": "claimed"})

    assert writes[0][0] == tmp_path / "state" / "results" / "run-123.json"
    assert writes[0][2] == 0o600
    assert writes[1][0] == tmp_path / "state" / "journal.jsonl"
    assert writes[1][1].endswith("\n")


def test_run_issue_rejects_a_fetched_issue_with_the_wrong_iid(tmp_path: Path) -> None:
    """A GitLab response cannot substitute another issue for the requested IID."""
    events: list[str] = []

    @contextmanager
    def lock() -> Iterator[None]:
        events.append("lock")
        yield

    dependencies = RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=lambda _config, _iid: GitLabIssue(
            iid=999, title="Other issue", labels=("agent::ready",)
        ),
        repository_lock=lambda _work_item_id: lock(),
        preflight_repository=lambda _config, _work_item: events.append("preflight"),
        create_worktree=lambda _work_item: tmp_path / "worktree",
        invoke_codex=lambda _request: {},
        verify_publication=lambda _result, _worktree: None,
        append_journal=lambda _transition: None,
        project_issue=lambda _result: None,
        new_run_id=lambda: "run-123",
        load_result_schema=lambda: {"type": "object"},
        claim_issue=lambda _work_item: events.append("claim"),
        write_canonical_result=lambda _run_id, _document: None,
        write_recovery_record=lambda _run_id, _record: None,
        schema_path=lambda: tmp_path / "schema.json",
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=lambda _worktree: "0123456",
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=False,
            timeout_seconds=90,
            dependencies=dependencies,
        )

    assert events == []


@pytest.mark.parametrize(
    "pipeline_response",
    (
        "{}",
        "[]",
        "[{}]",
        '[{"status":"unknown"}]',
        '[{"status":"running"}]',
        '[{"status":null}]',
        '["not-a-pipeline"]',
    ),
)
def test_pipeline_preflight_fails_closed_for_missing_or_malformed_latest_status(
    pipeline_response: str,
) -> None:
    """Only an explicit successful latest default-branch pipeline may pass preflight."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(0, pipeline_response, ""),
    )

    with pytest.raises(AdapterFailure):
        gateway.latest_pipeline_is_healthy(target_branch="main")


def test_work_item_task_content_digest_is_stable_and_content_sensitive() -> None:
    """Runner provenance binds reuse to the task data, not a mutable issue timestamp."""
    config = GitLabConfig(host="gitlab.example.com", project_id=310967)
    first = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(
            iid=123,
            title="Implement bounded runner",
            description="Keep the source repository isolated.",
            labels=("agent::ready", "priority::high"),
            updated_at="2026-08-24T10:00:00Z",
        ),
        run_id="run-1",
        mode="publish",
    )
    same_task_new_revision = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(
            iid=123,
            title="Implement bounded runner",
            description="Keep the source repository isolated.",
            labels=("agent::running", "priority::high"),
            updated_at="2026-08-25T10:00:00Z",
        ),
        run_id="run-2",
        mode="publish",
    )
    altered_task = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(
            iid=123,
            title="Implement bounded runner",
            description="Keep the source repository isolated.",
            labels=("agent::awaiting-human", "priority::low"),
            updated_at="2026-08-24T10:00:00Z",
        ),
        run_id="run-3",
        mode="publish",
    )

    assert first.task_content_digest == same_task_new_revision.task_content_digest
    assert first.task_content_digest != altered_task.task_content_digest


@pytest.mark.parametrize(
    ("field", "unexpected"),
    (
        ("work_item_id", "gitlab:310967:999"),
        ("task_content_digest", "wrong-digest"),
        ("branch", "agent/issue-999"),
        ("workspace", "/another/retained/workspace"),
        ("origin_url", "https://evil.example/group/project.git"),
    ),
)
def test_reuse_rejects_each_mismatched_runner_manifest_field(
    tmp_path: Path, field: str, unexpected: str
) -> None:
    """A retained clone is usable only through its exact runner-authored manifest."""
    repository = tmp_path / "repository"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace = (
        tmp_path / "state" / "workspaces" / "gitlab.example.com-310967" / "issue-123"
    )
    (workspace / ".git").mkdir(parents=True)
    config = GitLabConfig(host="gitlab.example.com", project_id=310967)
    work_item = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )
    provenance: dict[str, object] = {
        "work_item_id": work_item.work_item_id,
        "task_content_digest": work_item.task_content_digest,
        "branch": work_item.branch,
        "workspace": str(workspace),
        "origin_url": origin,
    }
    provenance[field] = unexpected
    calls: list[tuple[str, ...]] = []

    class Store:
        root = tmp_path / "state"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
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
            raise AssertionError("a retained manifest must never be replaced")

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return provenance

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "projects/310967" in joined:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "default_branch": "main",
                        "http_url_to_repo": origin,
                        "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                    }
                ),
                "",
            )
        if command == ("git", "-C", str(repository), "remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(workspace), "remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(workspace), "branch", "--show-current"):
            return CommandResult(0, f"{work_item.branch}\n", "")
        if command == ("git", "-C", str(workspace), "rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40 + "\n", "")
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        run_store=Store(),
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )

    with pytest.raises(AdapterFailure):
        dependencies.create_worktree(work_item)

    assert not any(str(workspace) in command for command in calls)


def test_fresh_work_item_preflights_then_creates_a_clone_and_provenance(
    tmp_path: Path,
) -> None:
    """A new selected task has no manifest until its runner-owned clone is created."""
    repository = tmp_path / "repository"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace = (
        tmp_path / "state" / "workspaces" / "gitlab.example.com-310967" / "issue-123"
    )
    provenance_writes: list[tuple[str, Mapping[str, object]]] = []
    calls: list[tuple[str, ...]] = []

    class Store:
        root = tmp_path / "state"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
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
            self, work_item_id: str, record: Mapping[str, object]
        ) -> Path:
            provenance_writes.append((work_item_id, dict(record)))
            return self.root / "workspaces" / "manifest.json"

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return None

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        joined = " ".join(command)
        if "--show-toplevel" in joined:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in joined:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "status --porcelain" in joined or "fetch origin" in joined:
            return CommandResult(0, "", "")
        if command == ("uv", "run", "pytest"):
            return CommandResult(0, "", "")
        if "projects/310967/pipelines?ref=main" in joined:
            return CommandResult(0, '[{"status":"success"}]', "")
        if "projects/310967/merge_requests?state=opened" in joined:
            return CommandResult(0, "[]", "")
        if "projects/310967" in joined:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "default_branch": "main",
                        "http_url_to_repo": origin,
                        "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                    }
                ),
                "",
            )
        if "remote get-url origin" in joined:
            return CommandResult(0, f"{origin}\n", "")
        if "ls-remote" in joined:
            return CommandResult(2, "", "")
        if command[:2] == ("git", "clone"):
            assert workspace.parent.is_dir()
            assert stat.S_IMODE(workspace.parent.stat().st_mode) == 0o700
            (workspace / ".git").mkdir(parents=True)
            return CommandResult(0, "", "")
        if "checkout" in command:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        run_store=Store(),
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    work_item = WorkItem.from_issue(
        config=dependencies.load_config(),
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )

    dependencies.preflight_repository(dependencies.load_config(), work_item)
    assert dependencies.create_worktree(work_item) == workspace

    assert (workspace / ".git").is_dir()
    assert provenance_writes == [
        (
            work_item.work_item_id,
            {
                "work_item_id": work_item.work_item_id,
                "task_content_digest": work_item.task_content_digest,
                "branch": work_item.branch,
                "workspace": str(workspace),
                "origin_url": origin,
            },
        )
    ]


def test_fresh_work_item_rejects_an_unmanifested_deterministic_workspace(
    tmp_path: Path,
) -> None:
    """A pre-existing clone is never adopted merely because its path is deterministic."""
    repository = tmp_path / "repository"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace = (
        tmp_path / "state" / "workspaces" / "gitlab.example.com-310967" / "issue-123"
    )
    (workspace / ".git").mkdir(parents=True)
    provenance_writes: list[Mapping[str, object]] = []
    calls: list[tuple[str, ...]] = []

    class Store:
        root = tmp_path / "state"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
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
            self, _work_item_id: str, record: Mapping[str, object]
        ) -> Path:
            provenance_writes.append(dict(record))
            return self.root / "workspaces" / "manifest.json"

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return None

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if "--show-toplevel" in command:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in command:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "projects/310967" in " ".join(command):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "default_branch": "main",
                        "http_url_to_repo": origin,
                        "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                    }
                ),
                "",
            )
        if command == ("git", "-C", str(repository), "remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(workspace), "remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(workspace), "branch", "--show-current"):
            return CommandResult(0, "agent/issue-123\n", "")
        if command == ("git", "-C", str(workspace), "rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40 + "\n", "")
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        run_store=Store(),
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    config = dependencies.load_config()
    work_item = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )

    with pytest.raises(AdapterFailure):
        dependencies.create_worktree(work_item)

    assert provenance_writes == []
    assert not any(str(workspace) in command for command in calls)


def test_matching_retained_manifest_reuses_workspace_without_recreating_it(
    tmp_path: Path,
) -> None:
    """A verified retained clone is usable on a later run without another create-only manifest."""
    repository = tmp_path / "repository"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace = (
        tmp_path / "state" / "workspaces" / "gitlab.example.com-310967" / "issue-123"
    )
    (workspace / ".git").mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    config = GitLabConfig(host="gitlab.example.com", project_id=310967)
    work_item = WorkItem.from_issue(
        config=config,
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )

    class Store:
        root = tmp_path / "state"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
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
            _self, _work_item_id: str, _record: Mapping[str, object]
        ) -> Path:
            raise AssertionError("retained workspace provenance must not be recreated")

        def read_workspace_provenance(
            self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return {
                "work_item_id": work_item.work_item_id,
                "task_content_digest": work_item.task_content_digest,
                "branch": work_item.branch,
                "workspace": str(workspace),
                "origin_url": origin,
            }

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(workspace), "branch", "--show-current"):
            return CommandResult(0, "agent/issue-123\n", "")
        if command == ("git", "-C", str(workspace), "rev-parse", "HEAD"):
            return CommandResult(0, "a" * 40 + "\n", "")
        if "--show-toplevel" in command:
            return CommandResult(0, f"{repository}\n", "")
        if "--git-common-dir" in command:
            return CommandResult(0, f"{repository / '.git'}\n", "")
        if "projects/310967" in " ".join(command):
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "default_branch": "main",
                        "http_url_to_repo": origin,
                        "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                    }
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        run_store=Store(),
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )

    assert dependencies.create_worktree(work_item) == workspace
    assert not any("clone" in call for call in calls)


@pytest.mark.parametrize(
    ("local_tip", "remote_tip", "merge_request_tip"),
    (("a" * 40, "b" * 40, "b" * 40), ("a" * 40, "a" * 40, "b" * 40)),
)
def test_preflight_rejects_retained_clone_when_local_remote_and_mr_tips_diverge(
    tmp_path: Path,
    local_tip: str,
    remote_tip: str,
    merge_request_tip: str,
) -> None:
    """Published reuse requires local HEAD, remote branch, and open-MR SHA equality."""
    repository = tmp_path / "repository"
    workspace = tmp_path / "state" / "workspaces" / "gitlab-310967-123"
    (repository / ".git").mkdir(parents=True)
    (workspace / ".git").mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    class Store:
        root = tmp_path / "state"

        @contextmanager
        def lock(self, *, blocking: bool) -> Iterator[None]:
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
            _self, _work_item_id: str
        ) -> Mapping[str, object] | None:
            return {
                "work_item_id": "gitlab:310967:123",
                "task_content_digest": "stable-task-digest",
                "branch": "agent/issue-123",
                "workspace": str(workspace),
                "origin_url": "https://gitlab.example.com/group/project.git",
            }

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
        if "rev-parse HEAD" in joined and str(workspace) in joined:
            return CommandResult(0, f"{local_tip}\n", "")
        if "projects/310967/pipelines?ref=main" in joined:
            return CommandResult(0, '[{"status":"success"}]', "")
        if "projects/310967/merge_requests?state=opened" in joined:
            return CommandResult(
                0,
                json.dumps(
                    [
                        {
                            "state": "opened",
                            "source_branch": "agent/issue-123",
                            "target_branch": "main",
                            "source_project_id": 310967,
                            "target_project_id": 310967,
                            "sha": merge_request_tip,
                        }
                    ]
                ),
                "",
            )
        if "projects/310967" in joined:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "id": 310967,
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                        "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                    }
                ),
                "",
            )
        if "ls-remote" in joined:
            return CommandResult(0, f"{remote_tip}\trefs/heads/agent/issue-123\n", "")
        if "status --porcelain" in joined or "fetch origin" in joined:
            return CommandResult(0, "", "")
        if command == ("uv", "run", "pytest"):
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    dependencies = build_default_dependencies(
        repository=repository,
        run_command=command,
        run_store=Store(),
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": "310967",
        },
    )
    work_item = WorkItem.from_issue(
        config=dependencies.load_config(),
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )

    with pytest.raises(AdapterFailure):
        dependencies.preflight_repository(dependencies.load_config(), work_item)

    assert any("rev-parse HEAD" in " ".join(call) for call in calls)
    assert not any("worktree" in " ".join(call) for call in calls)
