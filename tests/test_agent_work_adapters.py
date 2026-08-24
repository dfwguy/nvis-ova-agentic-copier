"""Concrete-boundary contracts for the bounded Phase 1 runner."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import pytest
from pydantic import ValidationError

from agent_work.adapters import (
    AdapterFailure,
    CodexInvoker,
    CommandResult,
    GitLabGateway,
    LocalRunStore,
    WorktreeGateway,
    load_environment_config,
)
from agent_work.models import GitLabConfig
from agent_work.runner import CodexRequest


@dataclass
class Commands:
    """Record commands through the adapter's injected process boundary."""

    responses: list[CommandResult]
    calls: list[tuple[tuple[str, ...], str | None, int | None]]

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        self.calls.append((command, stdin, timeout_seconds))
        return self.responses.pop(0)


class ProjectMetadata(Protocol):
    """The immutable configured-project values consumed by orchestration."""

    id: int
    default_branch: str


class MergeRequestItem(Protocol):
    """The immutable retained-publication proof consumed by preflight."""

    sha: str


def test_environment_config_requires_host_and_positive_numeric_project_id() -> None:
    """The environment edge rejects absent or malformed non-secret configuration."""
    for environment in (
        {},
        {"GITLAB_HOST": "gitlab.example.com"},
        {"GITLAB_HOST": "gitlab.example.com", "GITLAB_PROJECT_ID": "zero"},
        {"GITLAB_HOST": "gitlab.example.com", "GITLAB_PROJECT_ID": "0"},
    ):
        with pytest.raises(AdapterFailure) as error:
            load_environment_config(environment)
        assert "GITLAB_TOKEN" not in str(error.value)

    assert load_environment_config(
        {"GITLAB_HOST": "gitlab.example.com", "GITLAB_PROJECT_ID": "310967"}
    ) == GitLabConfig(host="gitlab.example.com", project_id=310967)


def test_gitlab_gateway_rejects_a_response_for_another_issue_iid() -> None:
    """The numeric issue endpoint cannot substitute a different validated issue."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0,
            json.dumps(
                {
                    "iid": 999,
                    "title": "Substituted issue",
                    "state": "opened",
                    "confidential": False,
                    "labels": ["agent::ready"],
                }
            ),
            "",
        ),
    )

    with pytest.raises(AdapterFailure):
        gateway.fetch_issue(123)


@pytest.mark.parametrize(
    "response",
    (
        {"iid": 123, "title": "", "labels": ["agent::ready"]},
        {"iid": 123, "title": "Bounded", "labels": [42]},
    ),
)
def test_gitlab_gateway_converts_malformed_issue_payloads_to_safe_adapter_failures(
    response: dict[str, object],
) -> None:
    """Invalid GitLab issue JSON never leaks a Pydantic boundary exception."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(0, json.dumps(response), ""),
    )

    with pytest.raises(AdapterFailure):
        gateway.fetch_issue(123)


@pytest.mark.parametrize(
    "response",
    (
        {
            "iid": 123,
            "title": "Bounded work",
            "labels": ["agent::ready"],
            "confidential": False,
        },
        {
            "iid": 123,
            "title": "Bounded work",
            "labels": ["agent::ready"],
            "state": "opened",
        },
        {
            "iid": 123,
            "title": "Bounded work",
            "labels": ["agent::ready"],
            "state": "opened",
            "confidential": "false",
        },
    ),
)
def test_gitlab_gateway_requires_explicit_strict_issue_safety_fields(
    response: dict[str, object],
) -> None:
    """Safety-critical issue state never receives a benign default or coercion."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(0, json.dumps(response), ""),
    )

    with pytest.raises(AdapterFailure):
        gateway.fetch_issue(123)


@pytest.mark.parametrize(
    ("description", "normalized"),
    ((None, ""), (42, None), ([], None), ({}, None)),
)
def test_gitlab_gateway_normalizes_only_a_null_issue_description(
    description: object, normalized: str | None
) -> None:
    """GitLab's nullable description is safe, while other non-text values are not."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0,
            json.dumps(
                {
                    "iid": 123,
                    "title": "Bounded work",
                    "description": description,
                    "labels": ["agent::ready"],
                    "state": "opened",
                    "confidential": False,
                }
            ),
            "",
        ),
    )

    if normalized is None:
        with pytest.raises(AdapterFailure):
            gateway.fetch_issue(123)
    else:
        assert gateway.fetch_issue(123).description == normalized


@pytest.mark.parametrize(
    "response",
    (
        {
            "id": "310967",
            "default_branch": "main",
            "http_url_to_repo": "https://gitlab.example.com/group/project.git",
            "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
        },
        {
            "id": 310967,
            "default_branch": ["main"],
            "http_url_to_repo": "https://gitlab.example.com/group/project.git",
            "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
        },
    ),
)
def test_gitlab_gateway_rejects_malformed_project_metadata(
    response: dict[str, object],
) -> None:
    """Configured project metadata is a fail-closed external boundary."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(0, json.dumps(response), ""),
    )

    with pytest.raises(AdapterFailure):
        gateway.verify_project()


def test_gitlab_gateway_exposes_frozen_validated_project_metadata() -> None:
    """The project boundary gives orchestration immutable typed configured fields."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0,
            json.dumps(
                {
                    "id": 310967,
                    "default_branch": "release/2026",
                    "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                    "ssh_url_to_repo": "git@gitlab.example.com:group/project.git",
                }
            ),
            "",
        ),
    )

    project = cast(ProjectMetadata, gateway.verify_project())

    assert project.id == 310967
    assert project.default_branch == "release/2026"
    with pytest.raises(ValidationError):
        project.default_branch = "main"


def test_gitlab_gateway_uses_only_numeric_project_endpoints_and_parses_ready_issue() -> (
    None
):
    """All GitLab reads and projections use configured host/project, never origin."""
    commands = Commands(
        responses=[
            CommandResult(
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
            ),
            CommandResult(
                0,
                json.dumps(
                    {
                        "iid": 123,
                        "title": "Do bounded work",
                        "description": "Task data",
                        "state": "opened",
                        "confidential": False,
                        "labels": ["agent::ready"],
                        "updated_at": "2026-08-24T12:00:00Z",
                    }
                ),
                "",
            ),
            CommandResult(0, "{}", ""),
            CommandResult(0, "{}", ""),
            CommandResult(0, "{}", ""),
            CommandResult(0, "{}", ""),
            CommandResult(
                0,
                '{"web_url":"https://gitlab.example.com/group/project/-/merge_requests/42",'
                '"source_branch":"agent/issue-123","target_branch":"main",'
                '"source_project_id":310967,"target_project_id":310967,'
                '"sha":"0123456789abcdef0123456789abcdef01234567","state":"opened"}',
                "",
            ),
        ],
        calls=[],
    )
    config = GitLabConfig(host="gitlab.example.com", project_id=310967)
    gateway = GitLabGateway(config=config, run_command=commands)

    gateway.verify_project()
    issue = gateway.fetch_issue(123)
    gateway.claim_running(issue_iid=123, run_id="run-123")
    gateway.project_awaiting_human(issue_iid=123, run_id="run-123")
    gateway.verify_merge_request(
        branch="agent/issue-123",
        branch_tip="0123456789abcdef0123456789abcdef01234567",
        merge_request_url="https://gitlab.example.com/group/project/-/merge_requests/42",
        target_branch="main",
        source_project_id=310967,
        target_project_id=310967,
    )

    assert issue.iid == 123
    assert issue.labels == ("agent::ready",)
    assert commands.calls == [
        (
            ("glab", "api", "--hostname", "gitlab.example.com", "projects/310967"),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "projects/310967/issues/123",
            ),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "PUT",
                "projects/310967/issues/123",
                "-f",
                "add_labels=agent::running",
                "-f",
                "remove_labels=agent::ready",
            ),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "POST",
                "projects/310967/issues/123/notes",
                "-f",
                "body=Run run-123 started.",
            ),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "PUT",
                "projects/310967/issues/123",
                "-f",
                "add_labels=agent::awaiting-human",
                "-f",
                "remove_labels=agent::running,agent::ready",
            ),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "POST",
                "projects/310967/issues/123/notes",
                "-f",
                "body=Run run-123 reached awaiting_human.",
            ),
            None,
            None,
        ),
        (
            (
                "glab",
                "api",
                "--hostname",
                "gitlab.example.com",
                "-X",
                "GET",
                "projects/310967/merge_requests?state=opened",
                "-f",
                "source_branch=agent/issue-123",
            ),
            None,
            None,
        ),
    ]
    assert all("origin" not in part for call, _, _ in commands.calls for part in call)


@pytest.mark.parametrize(
    "change",
    (
        {"target_project_id": 999},
        {"source_project_id": 999},
        {"target_branch": "release"},
        {"sha": "fedcba9876543210fedcba9876543210fedcba98"},
        {"web_url": "https://evil.example/merge_requests/42"},
    ),
)
def test_merge_request_verification_binds_every_publication_artifact_to_project(
    change: dict[str, object],
) -> None:
    """A matching branch alone cannot prove publication to the configured project."""
    expected_url = "https://gitlab.example.com/group/project/-/merge_requests/42"
    candidate = {
        "web_url": expected_url,
        "source_branch": "agent/issue-123",
        "target_branch": "main",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "state": "opened",
    } | change
    commands = Commands(
        responses=[CommandResult(0, json.dumps(candidate), "")], calls=[]
    )

    with pytest.raises(AdapterFailure):
        GitLabGateway(
            config=GitLabConfig(host="gitlab.example.com", project_id=310967),
            run_command=commands,
        ).verify_merge_request(
            branch="agent/issue-123",
            branch_tip="0123456789abcdef0123456789abcdef01234567",
            merge_request_url=expected_url,
            target_branch="main",
            source_project_id=310967,
            target_project_id=310967,
        )


@pytest.mark.parametrize(
    "missing_field",
    ("target_branch", "source_project_id", "target_project_id"),
)
def test_merge_request_verification_rejects_missing_project_binding_proof(
    missing_field: str,
) -> None:
    """A GitLab MR response must explicitly prove each project and target binding."""
    expected_url = "https://gitlab.example.com/group/project/-/merge_requests/42"
    candidate: dict[str, object] = {
        "web_url": expected_url,
        "source_branch": "agent/issue-123",
        "target_branch": "main",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "state": "opened",
    }
    del candidate[missing_field]
    commands = Commands(
        responses=[CommandResult(0, json.dumps(candidate), "")], calls=[]
    )

    with pytest.raises(AdapterFailure):
        GitLabGateway(
            config=GitLabConfig(host="gitlab.example.com", project_id=310967),
            run_command=commands,
        ).verify_merge_request(
            branch="agent/issue-123",
            branch_tip="0123456789abcdef0123456789abcdef01234567",
            merge_request_url=expected_url,
            target_branch="main",
            source_project_id=310967,
            target_project_id=310967,
        )


def test_gitlab_gateway_rejects_a_malformed_pipeline_item_after_a_successful_one() -> (
    None
):
    """Pipeline health cannot ignore malformed later records from the external list."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0, json.dumps([{"status": "success"}, ["not-a-pipeline"]]), ""
        ),
    )

    with pytest.raises(AdapterFailure):
        gateway.latest_pipeline_is_healthy(target_branch="main")


@pytest.mark.parametrize("operation", ("find", "verify"))
def test_gitlab_gateway_rejects_malformed_merge_request_items_anywhere_in_a_list(
    operation: str,
) -> None:
    """One valid MR never permits the gateway to silently skip malformed siblings."""
    url = "https://gitlab.example.com/group/project/-/merge_requests/42"
    branch_tip = "0123456789abcdef0123456789abcdef01234567"
    candidate = {
        "web_url": url,
        "source_branch": "agent/issue-123",
        "target_branch": "main",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "sha": branch_tip,
        "state": "opened",
    }
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0, json.dumps([candidate, ["not-a-merge-request"]]), ""
        ),
    )

    with pytest.raises(AdapterFailure):
        if operation == "find":
            gateway.find_open_merge_request(
                branch="agent/issue-123", target_branch="main"
            )
        else:
            gateway.verify_merge_request(
                branch="agent/issue-123",
                branch_tip=branch_tip,
                merge_request_url=url,
                target_branch="main",
                source_project_id=310967,
                target_project_id=310967,
            )


def test_gitlab_gateway_exposes_frozen_validated_merge_request_items() -> None:
    """Preflight receives an immutable, typed MR value rather than raw JSON."""
    branch_tip = "0123456789abcdef0123456789abcdef01234567"
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0,
            json.dumps(
                [
                    {
                        "web_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
                        "source_branch": "agent/issue-123",
                        "target_branch": "main",
                        "source_project_id": 310967,
                        "target_project_id": 310967,
                        "sha": branch_tip,
                        "state": "opened",
                    }
                ]
            ),
            "",
        ),
    )

    merge_request = cast(
        MergeRequestItem,
        gateway.find_open_merge_request(branch="agent/issue-123", target_branch="main"),
    )

    assert merge_request.sha == branch_tip
    with pytest.raises(ValidationError):
        merge_request.sha = "f" * 40


def test_worktree_gateway_creates_an_independent_clone_from_the_verified_origin(
    tmp_path: Path,
) -> None:
    """An issue workspace owns its Git directory and starts its exact branch at origin/main."""
    repository = tmp_path / "source"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace_root = tmp_path / "agent-workspaces" / "gitlab-310967"
    expected = workspace_root / "issue-123"
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command[:2] == ("git", "clone"):
            assert command == ("git", "clone", "--no-checkout", origin, str(expected))
            (expected / ".git").mkdir(parents=True)
            return CommandResult(0, "", "")
        if command[:4] == ("git", "-C", str(expected), "checkout"):
            assert command[-3:] == ("-b", "agent/issue-123", "origin/main")
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected command: {command}")

    gateway = WorktreeGateway(repository=repository, run_command=command)

    workspace = gateway.create_or_reuse(
        issue_iid=123, origin_url=origin, workspace_root=workspace_root
    )

    assert workspace == expected
    assert (workspace / ".git").is_dir()
    assert not any("worktree" in call for call in calls)


def test_worktree_gateway_stops_before_clone_when_the_source_origin_is_unavailable(
    tmp_path: Path,
) -> None:
    """Workspace creation cannot infer an origin or mutate the source checkout."""
    repository = tmp_path / "source"
    repository.mkdir()
    commands = Commands(
        responses=[CommandResult(1, "", "unavailable")],
        calls=[],
    )

    with pytest.raises(AdapterFailure):
        WorktreeGateway(repository=repository, run_command=commands).create_or_reuse(
            issue_iid=123
        )

    assert len(commands.calls) == 1
    assert all("clone" not in call for call, _, _ in commands.calls)


def test_worktree_gateway_rejects_a_symlinked_retained_git_dir_before_git_commands(
    tmp_path: Path,
) -> None:
    """A retained workspace cannot turn a linked `.git` path into a trusted clone."""
    repository = tmp_path / "source"
    repository.mkdir()
    workspace_root = tmp_path / "retained"
    workspace = workspace_root / "issue-123"
    workspace.mkdir(parents=True)
    foreign_git = tmp_path / "foreign-git"
    foreign_git.mkdir()
    (workspace / ".git").symlink_to(foreign_git, target_is_directory=True)
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise AssertionError("a symlinked retained Git directory must block first")

    with pytest.raises(AdapterFailure):
        WorktreeGateway(repository=repository, run_command=command).create_or_reuse(
            issue_iid=123,
            origin_url="https://gitlab.example.com/group/project.git",
            workspace_root=workspace_root,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("workspace_origin", "branch", "head"),
    (
        ("https://evil.example/group/project.git", "agent/issue-123", "a" * 40),
        ("https://gitlab.example.com/group/project.git", "agent/issue-999", "a" * 40),
        ("https://gitlab.example.com/group/project.git", "agent/issue-123", ""),
    ),
)
def test_worktree_gateway_rejects_independent_workspace_without_matching_origin_branch_and_head(
    tmp_path: Path,
    workspace_origin: str,
    branch: str,
    head: str,
) -> None:
    """A retained clone must prove its origin, deterministic branch, and readable HEAD."""
    repository = tmp_path / "source"
    repository.mkdir()
    origin = "https://gitlab.example.com/group/project.git"
    workspace_root = tmp_path / "agent-workspaces" / "gitlab-310967"
    expected = workspace_root / "issue-123"
    (expected / ".git").mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    def command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command == ("git", "-C", str(repository), "remote", "get-url", "origin"):
            return CommandResult(0, f"{origin}\n", "")
        if command == ("git", "-C", str(expected), "remote", "get-url", "origin"):
            return CommandResult(0, f"{workspace_origin}\n", "")
        if command == ("git", "-C", str(expected), "branch", "--show-current"):
            return CommandResult(0, f"{branch}\n", "")
        if command == ("git", "-C", str(expected), "rev-parse", "HEAD"):
            return CommandResult(0, f"{head}\n", "")
        raise AssertionError(f"unexpected command: {command}")

    with pytest.raises(AdapterFailure):
        WorktreeGateway(repository=repository, run_command=command).create_or_reuse(
            issue_iid=123, origin_url=origin, workspace_root=workspace_root
        )

    assert not any("clone" in call for call in calls)


def test_local_store_uses_common_git_state_and_rejects_symlinked_paths(
    tmp_path: Path,
) -> None:
    """Runner state lives under the common Git directory and never follows a link."""
    common_dir = tmp_path / "source" / ".git"
    expected_state = common_dir / "agent-work"

    store = LocalRunStore.for_git_common_dir(common_dir)
    assert store.root == expected_state

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "linked-state"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(AdapterFailure), LocalRunStore(root=symlink).lock():
        pass


def test_local_store_reports_lock_contention_without_waiting(tmp_path: Path) -> None:
    """A second local runner gets a safe contention failure rather than blocking."""
    store = LocalRunStore(root=tmp_path / "agent-work")

    with store.lock(), pytest.raises(AdapterFailure), store.lock(blocking=False):
        pass


def test_local_store_rejects_a_symlinked_workspace_manifest_directory(
    tmp_path: Path,
) -> None:
    """Provenance creation never follows a workspace-manifest directory link."""
    root = tmp_path / "state"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (root / "workspaces").symlink_to(target, target_is_directory=True)

    with pytest.raises(AdapterFailure):
        LocalRunStore(root=root).write_workspace_provenance(
            "gitlab:310967:123", {"workspace": "/retained/issue-123"}
        )

    assert list(target.iterdir()) == []


def test_local_store_is_append_only_and_keeps_result_private(tmp_path: Path) -> None:
    """The local lock, journal, and result file keep durable evidence safely local."""
    store = LocalRunStore(root=tmp_path)

    with store.lock():
        store.append_transition({"from_status": "queued", "to_status": "claimed"})
        store.append_transition({"from_status": "claimed", "to_status": "running"})
        result_path = store.write_canonical_result(
            "run-123", {"outcome": "awaiting_human"}
        )

    journal = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl").read_text().splitlines()
    ]
    assert [record["to_status"] for record in journal] == ["claimed", "running"]
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600
    assert json.loads(result_path.read_text()) == {"outcome": "awaiting_human"}


def test_local_store_never_replaces_a_canonical_result_created_during_atomic_publish(
    tmp_path: Path,
) -> None:
    """A competing final result wins safely even if it appears after the first check."""
    root = tmp_path / "state"
    raced_result = root / "results" / "run-123.json"
    writes: list[Path] = []

    def atomic_write(path: Path, content: str, _mode: int) -> None:
        writes.append(path)
        if path == raced_result:
            path.write_text('{"outcome":"racer"}', encoding="utf-8")
            raise FileExistsError("racing canonical create")
        path.write_text(content, encoding="utf-8")

    store = LocalRunStore(root=root, atomic_write=atomic_write)

    with pytest.raises(AdapterFailure):
        store.write_canonical_result("run-123", {"outcome": "awaiting_human"})

    store.append_transition({"from_status": "queued", "to_status": "claimed"})

    assert raced_result.read_text(encoding="utf-8") == '{"outcome":"racer"}'
    assert writes == [raced_result, root / "journal.jsonl"]


@pytest.mark.parametrize("record_kind", ("canonical", "recovery", "provenance"))
def test_default_store_never_replaces_a_record_created_during_atomic_commit(
    tmp_path: Path, record_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each default-writer record preserves a competitor at its create syscall."""
    root = tmp_path / "state"
    store = LocalRunStore(root=root)
    run_id = "run-123"
    if record_kind == "canonical":
        target = root / "results" / f"{run_id}.json"
        publish = lambda document: store.write_canonical_result(run_id, document)
    elif record_kind == "recovery":
        target = root / "recovery" / f"{run_id}.json"
        publish = lambda document: store.write_recovery_record(run_id, document)
    else:
        work_item_id = "gitlab:310967:123"
        target = (
            root
            / "workspaces"
            / (hashlib.sha256(work_item_id.encode()).hexdigest() + ".json")
        )
        publish = lambda document: store.write_workspace_provenance(
            work_item_id, document
        )

    racer_document = '{"owner":"concurrent-creator"}'

    def create_competing_record(
        _temporary: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | os.PathLike[str],
        *_args: object,
        **_kwargs: object,
    ) -> None:
        assert Path(destination) == target
        target.write_text(racer_document, encoding="utf-8")
        raise FileExistsError("competing create won")

    monkeypatch.setattr(os, "link", create_competing_record)

    with pytest.raises(AdapterFailure):
        publish({"payload": "published"})

    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(racer_document)


@pytest.mark.parametrize(
    "response",
    (
        CommandResult(124, "", "sensitive stderr"),
        CommandResult(1, "", "sensitive stderr"),
        CommandResult(0, "not json", "sensitive stderr"),
        CommandResult(0, "[]\n", "sensitive stderr"),
        CommandResult(0, "1\n", "sensitive stderr"),
    ),
)
def test_codex_invoker_converts_process_failures_without_exposing_stderr(
    response: CommandResult,
) -> None:
    """Codex subprocess details remain private when the bounded invocation fails."""
    commands = Commands(responses=[response], calls=[])
    invoker = CodexInvoker(run_command=commands)
    request = CodexRequest(
        worktree=Path("/tmp/worktree"),
        prompt="untrusted prompt text",
        schema_path=Path("/tmp/schema.json"),
        timeout_seconds=90,
    )

    with pytest.raises(AdapterFailure) as error:
        invoker(request)

    assert "sensitive stderr" not in str(error.value)
    assert commands.calls == [(request.command, "untrusted prompt text", 90)]


def test_codex_invoker_parses_one_last_message_json() -> None:
    """The invoker returns the final structured Codex message after one process call."""
    commands = Commands(
        responses=[
            CommandResult(
                0,
                '{"type":"thread.started"}\n{"type":"item.completed","item":{"type":"agent_message","text":"{\\"outcome\\": \\"awaiting_human\\"}"}}\n',
                "",
            )
        ],
        calls=[],
    )
    request = CodexRequest(
        worktree=Path("/tmp/worktree"),
        prompt="prompt",
        schema_path=Path("/tmp/schema.json"),
        timeout_seconds=90,
    )

    assert CodexInvoker(run_command=commands)(request) == {"outcome": "awaiting_human"}
    assert len(commands.calls) == 1
