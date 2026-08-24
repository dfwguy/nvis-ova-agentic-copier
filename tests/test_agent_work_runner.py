"""Orchestration contracts for one bounded Phase 1 issue run."""

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_work.adapters import CommandResult, GitLabGateway
from agent_work.models import GitLabConfig, GitLabIssue, WorkItem
from agent_work.result import AgentResult
from agent_work.runner import CodexRequest, RunDependencies, RunFailure, run_issue


@dataclass
class Recorder:
    """Dependency fake recording externally observable boundary calls."""

    events: list[str]
    result: dict[str, object]
    schema: Mapping[str, Any]
    fail_at: str | None = None
    transitions: list[Mapping[str, object]] | None = None
    projected: list[AgentResult] | None = None

    def config(self) -> GitLabConfig:
        self.events.append("config")
        return GitLabConfig(host="gitlab.example.com", project_id=310967)

    def issue(self, _config: GitLabConfig, iid: int) -> GitLabIssue:
        self.events.append(f"issue:{iid}")
        return GitLabIssue.model_validate(
            {
                "iid": iid,
                "title": "Bounded runner",
                "description": "Do the requested work.",
                "labels": ["agent::ready"],
                "confidential": False,
                "updated_at": "2026-08-24T12:00:00Z",
            }
        )

    @contextmanager
    def lock(self, work_item_id: str) -> Iterator[None]:
        self.events.append(f"lock:{work_item_id}")
        yield
        self.events.append("unlock")

    def worktree(self, work_item: WorkItem) -> Path:
        self.events.append(f"worktree:{work_item.branch}")
        return Path("/tmp/agent-work/issue-123")

    def codex(self, request: CodexRequest) -> dict[str, object]:
        self.events.append("codex")
        assert request.worktree == Path("/tmp/agent-work/issue-123")
        assert request.timeout_seconds == 90
        return self.result

    def result_schema(self) -> Mapping[str, Any]:
        """Return the injected schema without consulting the filesystem."""
        self.events.append("schema")
        return self.schema

    def claim(self, _work_item: object) -> None:
        """Record the one explicit GitLab claim boundary."""
        self.events.append("claim")
        if self.fail_at == "claim":
            raise OSError("claim unavailable")

    def preflight(self, _config: GitLabConfig, _work_item: WorkItem) -> None:
        """Record the injected Git and GitLab repository preflight."""
        self.events.append("preflight")

    def write_canonical_result(
        self, _run_id: str, _result: Mapping[str, object]
    ) -> None:
        """Record one private persistence of the final validated result."""
        self.events.append("write_canonical_result")

    def verify(self, result: object, worktree: Path) -> None:
        self.events.append("verify")
        assert worktree == Path("/tmp/agent-work/issue-123")
        if self.fail_at == "verify":
            raise OSError("merge request cannot be verified")

    def read_worktree_head(self, worktree: Path) -> str:
        """Return the retained local tip through the explicit Git boundary."""
        self.events.append("head")
        assert worktree == Path("/tmp/agent-work/issue-123")
        return "0123456789abcdef0123456789abcdef01234567"

    def journal(self, transition: Mapping[str, object]) -> None:
        name = f"journal:{transition['from_status']}->{transition['to_status']}"
        self.events.append(name)
        if self.transitions is not None:
            self.transitions.append(transition)
        if self.fail_at == name:
            raise OSError("journal unavailable")

    def project(self, result: AgentResult) -> None:
        self.events.append("project")
        if self.projected is not None:
            self.projected.append(result)
        if self.fail_at == "project":
            raise OSError("issue projection unavailable")


def _published_result() -> dict[str, object]:
    """Return the result emitted by a successful Codex run."""
    return {
        "schema_version": 1,
        "work_item_id": "gitlab:310967:123",
        "run_id": "run-123",
        "mode": "publish",
        "outcome": "awaiting_human",
        "summary": "Published a merge request.",
        "branch": "agent/issue-123",
        "branch_tip": "0123456789abcdef0123456789abcdef01234567",
        "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
        "tests": [],
        "findings": [],
        "next_action": "Human review and merge",
        "error": None,
    }


def _dependencies(
    recorder: Recorder,
    *,
    result_path: Path,
    fetch_issue: Callable[[GitLabConfig, int], GitLabIssue] | None = None,
) -> RunDependencies:
    """Wire a recorder through every impure runner boundary."""

    return RunDependencies(
        load_config=recorder.config,
        fetch_issue=recorder.issue if fetch_issue is None else fetch_issue,
        repository_lock=recorder.lock,
        create_worktree=recorder.worktree,
        invoke_codex=recorder.codex,
        verify_publication=recorder.verify,
        append_journal=recorder.journal,
        project_issue=recorder.project,
        new_run_id=lambda: "run-123",
        load_result_schema=recorder.result_schema,
        claim_issue=recorder.claim,
        preflight_repository=recorder.preflight,
        write_canonical_result=recorder.write_canonical_result,
        write_recovery_record=lambda _run_id, _record: None,
        schema_path=lambda: result_path.parent / "work-item-result-v1.schema.json",
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=recorder.read_worktree_head,
    )


@pytest.fixture
def private_result_path(tmp_path: Path) -> Path:
    """Keep every runner result file in pytest-owned ephemeral state."""
    return tmp_path / "git-common" / "agent-work" / "results" / "run-123.json"


@pytest.fixture(scope="module")
def canonical_result_schema(project_root: Path) -> Mapping[str, Any]:
    """Load canonical JSON once at the test's explicit filesystem boundary."""
    return json.loads(
        (project_root / ".agents/contracts/work-item-result-v1.schema.json").read_text()
    )


def test_dry_run_reads_the_selected_issue_but_has_no_side_effects(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """The default command is a useful preview and never claims or invokes."""
    recorder = Recorder(
        events=[], result=_published_result(), schema=canonical_result_schema
    )

    report = run_issue(
        issue_iid=123,
        mode="publish",
        apply=False,
        timeout_seconds=90,
        dependencies=_dependencies(recorder, result_path=private_result_path),
    )

    assert report.preview is True
    assert report.branch == "agent/issue-123"
    assert recorder.events == ["config", "issue:123"]


def test_preview_constructs_an_empty_task_body_from_a_gateway_null_description(
    canonical_result_schema: Mapping[str, Any], private_result_path: Path
) -> None:
    """A nullable GitLab body remains an explicit empty string through preview setup."""
    gateway = GitLabGateway(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        run_command=lambda _command: CommandResult(
            0,
            json.dumps(
                {
                    "iid": 123,
                    "title": "Bounded runner",
                    "description": None,
                    "labels": ["agent::ready"],
                    "state": "opened",
                    "confidential": False,
                }
            ),
            "",
        ),
    )
    issue = gateway.fetch_issue(123)
    work_item = WorkItem.from_issue(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        issue=issue,
        run_id="run-123",
        mode="publish",
    )
    recorder = Recorder(
        events=[], result=_published_result(), schema=canonical_result_schema
    )

    def fetch(_config: GitLabConfig, iid: int) -> GitLabIssue:
        recorder.events.append(f"issue:{iid}")
        return issue

    report = run_issue(
        issue_iid=123,
        mode="publish",
        apply=False,
        timeout_seconds=90,
        dependencies=_dependencies(
            recorder, result_path=private_result_path, fetch_issue=fetch
        ),
    )

    assert issue.description == ""
    assert work_item.body == ""
    assert report.preview is True
    assert recorder.events == ["config", "issue:123"]


def test_apply_runs_one_issue_in_auditable_order_and_stops_at_human_handoff(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """One apply makes one isolated run and projects status only after validation."""
    recorder = Recorder(
        events=[], result=_published_result(), schema=canonical_result_schema
    )

    report = run_issue(
        issue_iid=123,
        mode="publish",
        apply=True,
        timeout_seconds=90,
        dependencies=_dependencies(recorder, result_path=private_result_path),
    )

    assert report.preview is False
    assert report.result is not None
    assert report.result.outcome == "awaiting_human"
    assert recorder.events == [
        "config",
        "issue:123",
        "lock:gitlab:310967:123",
        "preflight",
        "issue:123",
        "journal:discovered->validated",
        "journal:validated->queued",
        "journal:queued->claimed",
        "claim",
        "journal:claimed->running",
        "worktree:agent/issue-123",
        "codex",
        "schema",
        "head",
        "verify",
        "write_canonical_result",
        "journal:running->awaiting_human",
        "project",
        "unlock",
    ]


def test_apply_does_not_project_a_success_when_publication_verification_fails(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """A missing or unverified MR is a blocker, never a completed run."""
    projected: list[AgentResult] = []
    recorder = Recorder(
        events=[],
        result=_published_result(),
        schema=canonical_result_schema,
        fail_at="verify",
        projected=projected,
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=_dependencies(recorder, result_path=private_result_path),
        )

    assert recorder.events == [
        "config",
        "issue:123",
        "lock:gitlab:310967:123",
        "preflight",
        "issue:123",
        "journal:discovered->validated",
        "journal:validated->queued",
        "journal:queued->claimed",
        "claim",
        "journal:claimed->running",
        "worktree:agent/issue-123",
        "codex",
        "schema",
        "head",
        "verify",
        "journal:running->retry_wait",
        "project",
        "unlock",
    ]
    assert [result.outcome for result in projected] == ["retry"]


@pytest.mark.parametrize(
    "issue_change",
    (
        {"state": "closed"},
        {"confidential": True},
        {"labels": ()},
    ),
)
def test_apply_rejects_issues_without_the_explicit_nonconfidential_ready_signal(
    issue_change: dict[str, object],
    private_result_path: Path,
) -> None:
    """The runner never claims a confidential or non-opted-in issue."""
    recorder = Recorder(events=[], result=_published_result(), schema={})

    def ineligible_issue(config: GitLabConfig, iid: int) -> GitLabIssue:
        return recorder.issue(config, iid).model_copy(update=issue_change)

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=_dependencies(
                recorder,
                result_path=private_result_path,
                fetch_issue=ineligible_issue,
            ),
        )

    assert recorder.events == ["config", "issue:123"]


def test_apply_records_every_canonical_transition_before_the_human_handoff(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """The journal is the immutable source of the full bounded state history."""
    transitions: list[Mapping[str, object]] = []
    recorder = Recorder(
        events=[],
        result=_published_result(),
        schema=canonical_result_schema,
        transitions=transitions,
    )

    run_issue(
        issue_iid=123,
        mode="publish",
        apply=True,
        timeout_seconds=90,
        dependencies=_dependencies(recorder, result_path=private_result_path),
    )

    assert [
        (transition["from_status"], transition["to_status"])
        for transition in transitions
    ] == [
        ("discovered", "validated"),
        ("validated", "queued"),
        ("queued", "claimed"),
        ("claimed", "running"),
        ("running", "awaiting_human"),
    ]
    required_audit_fields = {
        "work_item_id",
        "run_id",
        "from_status",
        "to_status",
        "occurred_at",
        "actor_type",
        "actor_id",
        "reason",
        "attempt_count",
        "lease_owner",
        "lease_expires_at",
        "branch",
        "branch_tip",
        "merge_request_url",
        "result_digest",
    }
    assert all(required_audit_fields.issubset(transition) for transition in transitions)


def test_apply_rejects_a_valid_result_that_reports_another_issue_branch(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """Publication proof cannot substitute an agent-reported branch for the work item."""
    projected: list[AgentResult] = []
    recorder = Recorder(
        events=[],
        result=_published_result() | {"branch": "agent/issue-999"},
        schema=canonical_result_schema,
        projected=projected,
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=_dependencies(recorder, result_path=private_result_path),
        )

    assert recorder.events == [
        "config",
        "issue:123",
        "lock:gitlab:310967:123",
        "preflight",
        "issue:123",
        "journal:discovered->validated",
        "journal:validated->queued",
        "journal:queued->claimed",
        "claim",
        "journal:claimed->running",
        "worktree:agent/issue-123",
        "codex",
        "schema",
        "journal:running->retry_wait",
        "project",
        "unlock",
    ]
    assert [result.outcome for result in projected] == ["retry"]


@pytest.mark.parametrize(
    "fail_at",
    ("journal:claimed->running", "journal:running->awaiting_human"),
)
def test_apply_never_returns_a_handoff_when_durable_recording_or_projection_fails(
    fail_at: str,
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """A failed journal or observable issue projection cannot be called success."""
    projected: list[AgentResult] = []
    recorder = Recorder(
        events=[],
        result=_published_result(),
        schema=canonical_result_schema,
        fail_at=fail_at,
        projected=projected,
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=_dependencies(recorder, result_path=private_result_path),
        )

    if fail_at == "journal:claimed->running":
        assert recorder.events == [
            "config",
            "issue:123",
            "lock:gitlab:310967:123",
            "preflight",
            "issue:123",
            "journal:discovered->validated",
            "journal:validated->queued",
            "journal:queued->claimed",
            "claim",
            "journal:claimed->running",
            "journal:claimed->retry_wait",
            "project",
            "unlock",
        ]
        assert [result.outcome for result in projected] == ["retry"]
        return

    if fail_at == "journal:running->awaiting_human":
        assert recorder.events[-6:] == [
            "verify",
            "write_canonical_result",
            "journal:running->awaiting_human",
            "journal:running->retry_wait",
            "project",
            "unlock",
        ]
        assert [result.outcome for result in projected] == ["retry"]
        return


def test_apply_recovers_claim_failure_as_a_legal_failed_transition(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """An externally visible claim failure is a retry/dead-letter path, never a traceback."""
    transitions: list[Mapping[str, object]] = []
    recorder = Recorder(
        events=[],
        result=_published_result(),
        schema=canonical_result_schema,
        fail_at="claim",
        transitions=transitions,
    )

    with pytest.raises(RunFailure):
        run_issue(
            issue_iid=123,
            mode="publish",
            apply=True,
            timeout_seconds=90,
            dependencies=_dependencies(recorder, result_path=private_result_path),
        )

    assert transitions[-1]["to_status"] in {"retry_wait", "dead_letter"}
    assert "project" in recorder.events


def test_apply_preflights_and_rechecks_eligibility_inside_the_lock_before_claim(
    canonical_result_schema: Mapping[str, Any],
    private_result_path: Path,
) -> None:
    """Git/repository safety gates and the second ready check precede a remote claim."""
    recorder = Recorder(
        events=[], result=_published_result(), schema=canonical_result_schema
    )
    dependencies = _dependencies(recorder, result_path=private_result_path)

    def preflight(config: GitLabConfig, work_item: WorkItem) -> None:
        assert config.project_id == 310967
        assert work_item.branch == "agent/issue-123"
        recorder.events.append("preflight")

    guarded_dependencies = RunDependencies(
        load_config=dependencies.load_config,
        fetch_issue=dependencies.fetch_issue,
        repository_lock=dependencies.repository_lock,
        create_worktree=dependencies.create_worktree,
        invoke_codex=dependencies.invoke_codex,
        verify_publication=dependencies.verify_publication,
        append_journal=dependencies.append_journal,
        project_issue=dependencies.project_issue,
        new_run_id=dependencies.new_run_id,
        load_result_schema=dependencies.load_result_schema,
        claim_issue=dependencies.claim_issue,
        schema_path=dependencies.schema_path,
        preflight_repository=preflight,
        write_canonical_result=dependencies.write_canonical_result,
        write_recovery_record=dependencies.write_recovery_record,
        now=dependencies.now,
        result_digest=dependencies.result_digest,
        read_worktree_head=dependencies.read_worktree_head,
    )

    run_issue(
        issue_iid=123,
        mode="publish",
        apply=True,
        timeout_seconds=90,
        dependencies=guarded_dependencies,
    )

    assert recorder.events.index("preflight") < recorder.events.index("claim")
    assert (
        recorder.events.index("lock:gitlab:310967:123")
        < recorder.events.index("issue:123", 2)
        < recorder.events.index("claim")
    )


def test_apply_keeps_runner_result_state_out_of_codex_and_persists_once(
    tmp_path: Path, canonical_result_schema: Mapping[str, Any]
) -> None:
    """The runner persists the final document without exposing its state path to Codex."""
    result_path = tmp_path / "git-common" / "agent-work" / "results" / "run-123.json"
    persisted: list[tuple[str, Mapping[str, object]]] = []
    recorder = Recorder(
        events=[], result=_published_result(), schema=canonical_result_schema
    )
    dependencies = _dependencies(recorder, result_path=result_path)

    def codex(request: CodexRequest) -> dict[str, object]:
        assert all(str(result_path) not in argument for argument in request.command)
        assert "-o" not in request.command
        return recorder.codex(request)

    durable_dependencies = RunDependencies(
        load_config=dependencies.load_config,
        fetch_issue=dependencies.fetch_issue,
        repository_lock=dependencies.repository_lock,
        create_worktree=dependencies.create_worktree,
        invoke_codex=codex,
        verify_publication=dependencies.verify_publication,
        append_journal=dependencies.append_journal,
        project_issue=dependencies.project_issue,
        new_run_id=dependencies.new_run_id,
        load_result_schema=dependencies.load_result_schema,
        claim_issue=dependencies.claim_issue,
        schema_path=dependencies.schema_path,
        preflight_repository=dependencies.preflight_repository,
        write_canonical_result=lambda run_id, result: persisted.append(
            (run_id, result)
        ),
        write_recovery_record=dependencies.write_recovery_record,
        now=dependencies.now,
        result_digest=dependencies.result_digest,
        read_worktree_head=dependencies.read_worktree_head,
    )

    run_issue(
        issue_iid=123,
        mode="publish",
        apply=True,
        timeout_seconds=90,
        dependencies=durable_dependencies,
    )

    assert persisted == [("run-123", _published_result())]


def test_work_item_from_issue_retains_immutable_source_and_task_identity() -> None:
    """Normalization derives the bounded-run identity from immutable issue data."""
    item = WorkItem.from_issue(
        config=GitLabConfig(host="gitlab.example.com", project_id=310967),
        issue=GitLabIssue(iid=123, title="Bounded", labels=("agent::ready",)),
        run_id="run-123",
        mode="publish",
    )

    assert item.work_item_id == "gitlab:310967:123"
    assert item.branch == "agent/issue-123"
    assert item.source_id == "123"
    assert item.mode == "publish"
    assert item.status == "discovered"
    assert item.deduplication_key == "gitlab:310967:123:"
    status_field = "status"
    title_field = "title"
    with pytest.raises(FrozenInstanceError):
        setattr(item, status_field, "completed")
    with pytest.raises(FrozenInstanceError):
        setattr(item.issue, title_field, "mutated")


def test_codex_request_uses_the_exact_bounded_workspace_write_command() -> None:
    """Codex receives a self-contained working copy, JSONL output, and no bypass."""
    request = CodexRequest(
        worktree=Path("/tmp/worktree"),
        prompt="bounded prompt",
        schema_path=Path("/tmp/work-item-result-v1.schema.json"),
        timeout_seconds=90,
    )

    assert request.command == (
        "codex",
        "exec",
        "-C",
        "/tmp/worktree",
        "-s",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--output-schema",
        "/tmp/work-item-result-v1.schema.json",
        "--json",
        "--ephemeral",
        "-",
    )
    assert "--add-dir" not in request.command
    assert "-o" not in request.command
    assert request.stdin == "bounded prompt"
    assert "dangerously-bypass-approvals-and-sandbox" not in request.command
