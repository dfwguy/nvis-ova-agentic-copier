"""Red state-machine tests for source-neutral normalized filesystem work."""

import hashlib
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_work.models import FilesystemEnvelope, GitLabConfig, GitLabIssue, WorkItem
from agent_work.result import AgentResult
from agent_work.runner import CodexRequest, RunDependencies, RunFailure, run_normalized

_LEGAL_TRANSITIONS = {
    ("discovered", "validated"),
    ("discovered", "dead_letter"),
    ("validated", "queued"),
    ("queued", "claimed"),
    ("claimed", "running"),
    ("claimed", "dead_letter"),
    ("running", "awaiting_human"),
    ("running", "retry_wait"),
    ("running", "dead_letter"),
}


def _work_item() -> WorkItem:
    envelope = FilesystemEnvelope.model_validate(
        {
            "schema_version": 1,
            "source_id": "nightly",
            "repository": "copier",
            "title": "Run through shared workflow",
            "body": "",
            "acceptance_criteria": ["Publish an MR."],
        }
    )
    digest = hashlib.sha256(b"exact producer bytes").hexdigest()
    return WorkItem.from_file(
        envelope=envelope,
        content_sha256=digest,
        run_id="run-file-123",
        mode="publish",
    )


def _dependencies(
    *,
    transitions: list[Mapping[str, object]],
    preflight: Callable[[GitLabConfig, WorkItem], None],
    worktree: Callable[[WorkItem], Path],
) -> RunDependencies:
    """Construct only public runner seams needed before Codex is reached."""
    return RunDependencies(
        load_config=lambda: GitLabConfig(host="gitlab.example.com", project_id=310967),
        fetch_issue=lambda _config, _iid: GitLabIssue(iid=1, title="unused"),
        repository_lock=lambda _work_item_id: nullcontext(),
        preflight_repository=preflight,
        create_worktree=worktree,
        invoke_codex=lambda _request: {},
        verify_publication=lambda _result, _worktree: None,
        append_journal=transitions.append,
        project_issue=lambda _result: None,
        new_run_id=lambda: "unused",
        load_result_schema=dict,
        claim_issue=lambda _work_item: None,
        write_canonical_result=lambda _run_id, _document: None,
        write_recovery_record=lambda _run_id, _document: None,
        schema_path=lambda: Path("/schema.json"),
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "unused",
        read_worktree_head=lambda _worktree: "unused",
    )


def _pairs(transitions: list[Mapping[str, object]]) -> list[tuple[str, str]]:
    return [
        (str(transition["from_status"]), str(transition["to_status"]))
        for transition in transitions
    ]


def _published_document(work_item: WorkItem) -> dict[str, object]:
    """Return one complete published handoff bound to the supplied file identity."""
    return {
        "schema_version": 1,
        "work_item_id": work_item.work_item_id,
        "run_id": work_item.run_id,
        "mode": "publish",
        "outcome": "awaiting_human",
        "summary": "Published the requested bounded change.",
        "branch": work_item.branch,
        "branch_tip": "0123456789abcdef0123456789abcdef01234567",
        "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/1",
        "tests": [],
        "findings": [],
        "next_action": "Human review and merge.",
        "error": None,
    }


def _published_schema() -> Mapping[str, object]:
    """Return an injected boundary schema sufficient for a complete handoff."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "work_item_id",
            "run_id",
            "mode",
            "outcome",
            "summary",
            "branch",
            "branch_tip",
            "merge_request_url",
            "tests",
            "findings",
            "next_action",
            "error",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "work_item_id": {"type": "string", "minLength": 1},
            "run_id": {"type": "string", "minLength": 1},
            "mode": {"const": "publish"},
            "outcome": {"const": "awaiting_human"},
            "summary": {"type": "string", "minLength": 1},
            "branch": {"type": "string", "minLength": 1},
            "branch_tip": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            "merge_request_url": {"type": "string", "format": "uri"},
            "tests": {"type": "array"},
            "findings": {"type": "array"},
            "next_action": {"type": "string", "minLength": 1},
            "error": {"const": None},
        },
    }


def _successful_dependencies(
    *,
    work_item: WorkItem,
    document: dict[str, object],
    events: list[str],
    transitions: list[Mapping[str, object]],
    canonical_documents: list[Mapping[str, object]],
) -> RunDependencies:
    """Construct every normalized-run boundary as a fully observable fake."""
    workspace = Path("/trusted/worktree")

    def journal(transition: Mapping[str, object]) -> None:
        events.append(f"audit:{transition['from_status']}->{transition['to_status']}")
        transitions.append(transition)

    def create_worktree(item: WorkItem) -> Path:
        events.append("worktree")
        assert item is work_item
        return workspace

    def invoke_codex(request: CodexRequest) -> object:
        events.append("codex")
        assert request.worktree == workspace
        assert request.schema_path == Path("/trusted/result.schema.json")
        assert request.timeout_seconds == 47
        return document

    def verify_publication(_result: AgentResult, checked_worktree: Path) -> None:
        events.append("verify-publication")
        assert checked_worktree == workspace

    def write_canonical_result(
        run_id: str, persisted_document: Mapping[str, object]
    ) -> None:
        events.append("write-canonical-result")
        assert run_id == work_item.run_id
        canonical_documents.append(persisted_document)

    def preflight(_config: GitLabConfig, item: WorkItem) -> None:
        events.append("preflight")
        assert item is work_item

    def load_schema() -> Mapping[str, object]:
        events.append("schema")
        return _published_schema()

    def read_head(checked_worktree: Path) -> str:
        events.append("read-head")
        assert checked_worktree == workspace
        return "0123456789abcdef0123456789abcdef01234567"

    return RunDependencies(
        load_config=lambda: (
            events.append("config")
            or GitLabConfig(host="gitlab.example.com", project_id=310967)
        ),
        fetch_issue=lambda _config, _iid: GitLabIssue(iid=1, title="unused"),
        repository_lock=lambda _work_item_id: nullcontext(),
        preflight_repository=preflight,
        create_worktree=create_worktree,
        invoke_codex=invoke_codex,
        verify_publication=verify_publication,
        append_journal=journal,
        project_issue=lambda _result: None,
        new_run_id=lambda: "unused",
        load_result_schema=load_schema,
        claim_issue=lambda _work_item: None,
        write_canonical_result=write_canonical_result,
        write_recovery_record=lambda _run_id, _document: None,
        schema_path=lambda: Path("/trusted/result.schema.json"),
        now=lambda: "2026-08-24T00:00:00+00:00",
        result_digest=lambda _document: "digest",
        read_worktree_head=read_head,
    )


def test_preflight_failure_records_discovered_to_dead_letter_not_a_running_failure() -> (
    None
):
    """An item that never starts must not be audited as a running workflow failure."""
    transitions: list[Mapping[str, object]] = []

    def preflight(_config: GitLabConfig, _item: WorkItem) -> None:
        raise OSError("preflight failed")

    with pytest.raises(OSError):
        run_normalized(
            work_item=_work_item(),
            timeout_seconds=90,
            dependencies=_dependencies(
                transitions=transitions,
                preflight=preflight,
                worktree=lambda _item: Path("/unused"),
            ),
        )

    assert _pairs(transitions) == [("discovered", "dead_letter")]
    assert all(pair in _LEGAL_TRANSITIONS for pair in _pairs(transitions))


def test_started_workflow_failure_records_running_to_dead_letter_not_failed() -> None:
    """Phase 2 has no `failed` state and must preserve the actual last status."""
    transitions: list[Mapping[str, object]] = []

    def worktree(_item: WorkItem) -> Path:
        raise OSError("worktree unavailable")

    with pytest.raises(OSError):
        run_normalized(
            work_item=_work_item(),
            timeout_seconds=90,
            dependencies=_dependencies(
                transitions=transitions,
                preflight=lambda _config, _item: None,
                worktree=worktree,
            ),
        )

    assert _pairs(transitions)[-1] == ("running", "dead_letter")
    assert all(pair in _LEGAL_TRANSITIONS for pair in _pairs(transitions))


def test_run_normalized_routes_one_file_work_item_through_the_complete_shared_handoff() -> (
    None
):
    """Filesystem work follows the same audited Codex and publication checks as issues."""
    work_item = _work_item()
    events: list[str] = []
    transitions: list[Mapping[str, object]] = []
    canonical_documents: list[Mapping[str, object]] = []
    document = _published_document(work_item)

    returned = run_normalized(
        work_item=work_item,
        timeout_seconds=47,
        dependencies=_successful_dependencies(
            work_item=work_item,
            document=document,
            events=events,
            transitions=transitions,
            canonical_documents=canonical_documents,
        ),
    )

    assert _pairs(transitions) == [
        ("discovered", "validated"),
        ("validated", "queued"),
        ("queued", "claimed"),
        ("claimed", "running"),
        ("running", "awaiting_human"),
    ]
    assert events == [
        "config",
        "preflight",
        "audit:discovered->validated",
        "audit:validated->queued",
        "audit:queued->claimed",
        "audit:claimed->running",
        "worktree",
        "codex",
        "schema",
        "read-head",
        "verify-publication",
        "write-canonical-result",
        "audit:running->awaiting_human",
    ]
    assert canonical_documents == [document]
    assert returned == document


def test_run_normalized_rejects_a_published_result_for_a_different_file_branch() -> (
    None
):
    """A schema-valid result cannot redirect one file request to another branch."""
    work_item = _work_item()
    events: list[str] = []
    transitions: list[Mapping[str, object]] = []
    canonical_documents: list[Mapping[str, object]] = []
    document = _published_document(work_item) | {"branch": "agent/file-unrelated"}

    with pytest.raises(RunFailure):
        run_normalized(
            work_item=work_item,
            timeout_seconds=47,
            dependencies=_successful_dependencies(
                work_item=work_item,
                document=document,
                events=events,
                transitions=transitions,
                canonical_documents=canonical_documents,
            ),
        )

    assert _pairs(transitions) == [
        ("discovered", "validated"),
        ("validated", "queued"),
        ("queued", "claimed"),
        ("claimed", "running"),
        ("running", "dead_letter"),
    ]
    assert "read-head" not in events
    assert "verify-publication" not in events
    assert canonical_documents == []
