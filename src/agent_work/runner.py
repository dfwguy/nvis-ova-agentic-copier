"""Shared orchestration for one bounded, manually selected work-item run."""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .models import GitLabConfig, GitLabIssue, WorkItem
from .prompt import render_issue_prompt
from .result import AgentResult, ResultValidationError, validate_agent_result


class RunFailure(RuntimeError):
    """A safe, user-facing blocker preventing a bounded run."""


@dataclass(frozen=True)
class CodexRequest:
    """One noninteractive, workspace-write Codex invocation."""

    worktree: Path
    prompt: str
    schema_path: Path
    timeout_seconds: int

    @property
    def command(self) -> tuple[str, ...]:
        """Return the exact safe Codex process command."""
        return (
            "codex",
            "exec",
            "-C",
            str(self.worktree),
            "-s",
            "workspace-write",
            "-c",
            "sandbox_workspace_write.network_access=true",
            "--output-schema",
            str(self.schema_path),
            "--json",
            "--ephemeral",
            "-",
        )

    @property
    def stdin(self) -> str:
        """Return the task prompt sent through standard input."""
        return self.prompt


@dataclass(frozen=True)
class RunDependencies:
    """All impure boundaries required by a normalized work-item run."""

    load_config: Callable[[], GitLabConfig]
    fetch_issue: Callable[[GitLabConfig, int], GitLabIssue]
    repository_lock: Callable[[str], AbstractContextManager[None]]
    preflight_repository: Callable[[GitLabConfig, WorkItem], None]
    create_worktree: Callable[[WorkItem], Path]
    invoke_codex: Callable[[CodexRequest], object]
    verify_publication: Callable[[AgentResult, Path], None]
    append_journal: Callable[[Mapping[str, object]], None]
    project_issue: Callable[[AgentResult], None]
    new_run_id: Callable[[], str]
    load_result_schema: Callable[[], Mapping[str, object]]
    claim_issue: Callable[[WorkItem], None]
    write_canonical_result: Callable[[str, Mapping[str, object]], None]
    write_recovery_record: Callable[[str, Mapping[str, object]], None]
    schema_path: Callable[[], Path]
    now: Callable[[], str]
    result_digest: Callable[[Mapping[str, object]], str]
    read_worktree_head: Callable[[Path], str]


@dataclass(frozen=True)
class RunReport:
    """A compact serializable report returned by a runner invocation."""

    preview: bool
    branch: str
    result: AgentResult | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize the report for the CLI's single JSON document."""
        report: dict[str, object] = {"preview": self.preview, "branch": self.branch}
        if self.result is not None:
            report["result"] = dict(self.result.document)
        return report


def _eligible(issue: GitLabIssue) -> bool:
    """Return whether an issue has the explicit Phase 1 opt-in signal."""
    return (
        issue.state == "opened"
        and not issue.confidential
        and "agent::ready" in issue.labels
    )


def _audit_transition(
    *,
    dependencies: RunDependencies,
    work_item: WorkItem,
    from_status: str,
    to_status: str,
    reason: str,
    result: AgentResult | None = None,
) -> None:
    """Append a full legal transition audit record through the injected journal."""
    dependencies.append_journal(
        {
            "work_item_id": work_item.work_item_id,
            "run_id": work_item.run_id,
            "from_status": from_status,
            "to_status": to_status,
            "occurred_at": dependencies.now(),
            "actor_type": "runner",
            "actor_id": "agent-work",
            "reason": reason,
            "attempt_count": work_item.attempt_count,
            "lease_owner": None,
            "lease_expires_at": None,
            "branch": result.branch if result is not None else work_item.branch,
            "branch_tip": result.branch_tip if result is not None else None,
            "merge_request_url": (
                result.merge_request_url if result is not None else None
            ),
            "result_digest": (
                dependencies.result_digest(result.document)
                if result is not None
                else None
            ),
        }
    )


def _failure_result(work_item: WorkItem, *, terminal: bool) -> AgentResult:
    """Create a safe local failure projection for the observable issue boundary."""
    return AgentResult(
        outcome="dead_letter" if terminal else "retry",
        branch=work_item.branch,
        branch_tip=None,
        merge_request_url=None,
        document={
            "schema_version": 1,
            "work_item_id": work_item.work_item_id,
            "run_id": work_item.run_id,
            "mode": work_item.mode,
            "outcome": "dead_letter" if terminal else "retry",
            "summary": "The bounded run did not reach a verified handoff.",
            "branch": work_item.branch,
            "branch_tip": None,
            "merge_request_url": None,
            "tests": [],
            "findings": [],
            "next_action": "Human review is required before another run.",
            "error": {
                "code": "run_failed",
                "message": "A bounded runner safety boundary failed.",
                "retryable": not terminal,
            },
        },
    )


def _recover_post_claim_failure(
    *,
    dependencies: RunDependencies,
    work_item: WorkItem,
    from_status: str,
) -> None:
    """Best-effort durable recovery that never masks the original unsafe run."""
    terminal = from_status == "awaiting_human"
    failure_result = _failure_result(work_item, terminal=terminal)
    try:
        dependencies.write_recovery_record(work_item.run_id, failure_result.document)
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        _audit_transition(
            dependencies=dependencies,
            work_item=work_item,
            from_status=from_status,
            to_status="dead_letter" if terminal else "retry_wait",
            reason="post-claim safety boundary failed",
            result=failure_result,
        )
    except (OSError, RuntimeError, ValueError):
        pass
    try:
        dependencies.project_issue(failure_result)
    except (OSError, RuntimeError, ValueError):
        pass


def run_issue(
    *,
    issue_iid: int,
    mode: Literal["publish"],
    apply: bool,
    timeout_seconds: int,
    dependencies: RunDependencies,
) -> RunReport:
    """Preview or execute exactly one selected, opted-in GitLab work item."""
    config = dependencies.load_config()
    selected_issue = dependencies.fetch_issue(config, issue_iid)
    if selected_issue.iid != issue_iid:
        raise RunFailure("GitLab returned a different issue than requested")
    if not _eligible(selected_issue):
        raise RunFailure(
            "issue must be open, non-confidential, and labelled agent::ready"
        )
    work_item = WorkItem.from_issue(
        config=config,
        issue=selected_issue,
        run_id=dependencies.new_run_id(),
        mode=mode,
    )
    if not apply:
        return RunReport(preview=True, branch=work_item.branch)

    failure: BaseException | None = None
    result: AgentResult | None = None
    post_claim_status: str | None = None
    with dependencies.repository_lock(work_item.work_item_id):
        try:
            dependencies.preflight_repository(config, work_item)
            current_issue = dependencies.fetch_issue(config, issue_iid)
            if current_issue.iid != issue_iid:
                raise RunFailure("GitLab returned a different issue than requested")
            if not _eligible(current_issue):
                raise RunFailure(
                    "issue must remain open, non-confidential, and labelled agent::ready"
                )
            work_item = WorkItem.from_issue(
                config=config,
                issue=current_issue,
                run_id=work_item.run_id,
                mode=mode,
            )
            _audit_transition(
                dependencies=dependencies,
                work_item=work_item,
                from_status="discovered",
                to_status="validated",
                reason="issue eligibility confirmed",
            )
            _audit_transition(
                dependencies=dependencies,
                work_item=work_item,
                from_status="validated",
                to_status="queued",
                reason="bounded run selected",
            )
            _audit_transition(
                dependencies=dependencies,
                work_item=work_item,
                from_status="queued",
                to_status="claimed",
                reason="local lock acquired",
            )
            try:
                dependencies.claim_issue(work_item)
            except (OSError, RuntimeError) as error:
                failure = error
                post_claim_status = "claimed"
            else:
                post_claim_status = "claimed"
                _audit_transition(
                    dependencies=dependencies,
                    work_item=work_item,
                    from_status="claimed",
                    to_status="running",
                    reason="claim projected",
                )
                post_claim_status = "running"
                result = _run_claimed_work_item(
                    work_item=work_item,
                    timeout_seconds=timeout_seconds,
                    dependencies=dependencies,
                    on_terminal=lambda value: _audit_transition(
                        dependencies=dependencies,
                        work_item=work_item,
                        from_status="running",
                        to_status="awaiting_human",
                        reason="publication verified",
                        result=value,
                    ),
                )
                post_claim_status = "awaiting_human"
                try:
                    dependencies.project_issue(result)
                except (OSError, RuntimeError, ValueError):
                    dependencies.write_recovery_record(
                        work_item.run_id,
                        {
                            **_mutable_document(result.document),
                            "stage": "project_issue",
                            "canonical_result_digest": dependencies.result_digest(
                                result.document
                            ),
                            "occurred_at": dependencies.now(),
                            "status_reason": "issue projection failed after verified publication",
                        },
                    )
        except (OSError, RuntimeError, ResultValidationError, ValueError) as error:
            failure = error
        if failure is not None and post_claim_status is not None:
            _recover_post_claim_failure(
                dependencies=dependencies,
                work_item=work_item,
                from_status=post_claim_status,
            )
    if failure is not None:
        if isinstance(failure, RunFailure):
            raise failure
        raise RunFailure("agent-work run failed") from failure
    if result is None:
        raise RunFailure("agent-work run failed")
    return RunReport(preview=False, branch=work_item.branch, result=result)


def run_normalized(
    *, work_item: WorkItem, timeout_seconds: int, dependencies: RunDependencies
) -> dict[str, object]:
    """Run one already-claimed source-neutral item through the shared engine."""
    config = dependencies.load_config()
    current_status = "discovered"
    auditing = False
    audit_failed = False

    def transition(
        to_status: str, reason: str, result: AgentResult | None = None
    ) -> None:
        """Record one legal transition only after its source status is known durable."""
        nonlocal audit_failed, auditing, current_status
        auditing = True
        try:
            _audit_transition(
                dependencies=dependencies,
                work_item=work_item,
                from_status=current_status,
                to_status=to_status,
                reason=reason,
                result=result,
            )
        except (OSError, RuntimeError, ValueError):
            audit_failed = True
            raise
        finally:
            auditing = False
        current_status = to_status

    try:
        dependencies.preflight_repository(config, work_item)
        transition("validated", "filesystem item validated")
        transition("queued", "bounded run selected")
        transition("claimed", "filesystem claim acquired")
        transition("running", "normalized runner started")
        result = _run_claimed_work_item(
            work_item=work_item,
            timeout_seconds=timeout_seconds,
            dependencies=dependencies,
            on_terminal=lambda value: transition(
                "awaiting_human", "publication verified", value
            ),
        )
        return _mutable_document(result.document)
    except (OSError, RuntimeError, ResultValidationError, ValueError):
        if not auditing and not audit_failed:
            try:
                transition("dead_letter", "normalized runner failed")
            except (OSError, RuntimeError, ValueError):
                pass
        raise


def _run_claimed_work_item(
    *,
    work_item: WorkItem,
    timeout_seconds: int,
    dependencies: RunDependencies,
    on_terminal: Callable[[AgentResult], None],
) -> AgentResult:
    """Run the shared Codex, verification, persistence, and terminal-audit path."""
    worktree = dependencies.create_worktree(work_item)
    request = CodexRequest(
        worktree=worktree,
        prompt=render_issue_prompt(
            work_item=work_item, schema_name=dependencies.schema_path().name
        ),
        schema_path=dependencies.schema_path(),
        timeout_seconds=timeout_seconds,
    )
    raw_document = dependencies.invoke_codex(request)
    if not isinstance(raw_document, dict) or not all(
        isinstance(key, str) for key in raw_document
    ):
        raise RunFailure("Codex did not return an object result")
    result = validate_agent_result(
        cast(dict[str, object], raw_document),
        work_item_id=work_item.work_item_id,
        run_id=work_item.run_id,
        mode=work_item.mode,
        schema=dependencies.load_result_schema(),
    )
    if result.outcome != "awaiting_human" or result.branch != work_item.branch:
        raise RunFailure("a validated awaiting_human handoff is required")
    if result.branch_tip is None or result.merge_request_url is None:
        raise RunFailure("published handoff artifacts are invalid")
    if dependencies.read_worktree_head(worktree) != result.branch_tip:
        raise RunFailure("retained worktree head does not match result")
    dependencies.verify_publication(result, worktree)
    dependencies.write_canonical_result(
        work_item.run_id, _mutable_document(result.document)
    )
    on_terminal(result)
    return result


def _mutable_document(document: Mapping[str, object]) -> dict[str, object]:
    """Copy a frozen validated document for an outbound persistence boundary."""
    return {key: _mutable_value(value) for key, value in document.items()}


def _mutable_value(value: object) -> object:
    """Recursively thaw one JSON-compatible result value."""
    if isinstance(value, Mapping):
        return {key: _mutable_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_value(item) for item in value]
    return value
