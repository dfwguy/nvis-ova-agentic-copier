"""Red policy contracts for the deliberately small serial runner."""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Self

import pytest

from agent_work.adapters import AdapterError
from agent_work.models import (
    AgentInvocation,
    GitLabIssue,
    GitLabProject,
    GitPublicationEvidence,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
)
from agent_work.serial import (
    BranchConflict,
    RunNextDependencies,
    RunNextError,
    _prompt,
    run_next,
)


def _issue(*, iid: int, labels: tuple[str, ...] = ()) -> GitLabIssue:
    """Return one eligible issue without an HTTP boundary."""
    return GitLabIssue(
        iid=iid,
        title=f"Issue {iid}",
        description="Untrusted task text",
        labels=labels,
        state="opened",
        confidential=False,
    )


class _Lock(AbstractContextManager[None]):
    """A protocol-compatible lock fake that records one ownership attempt."""

    def __init__(self) -> None:
        self.entered = 0

    def __call__(self) -> Self:
        return self

    def __enter__(self) -> None:
        self.entered += 1

    def __exit__(self, *_args: object) -> bool:
        return False


class _GitLab:
    """Semantic GitLab boundary fake with observable state projections."""

    def __init__(
        self,
        *,
        eligible: tuple[GitLabIssue, ...] = (),
        running: tuple[GitLabIssue, ...] = (),
        awaiting_human: tuple[GitLabIssue, ...] = (),
        publication: MergeRequestEvidence | None = None,
        closes_issue: bool = True,
        pipeline: PipelineEvidence | None = None,
    ) -> None:
        self.eligible = eligible
        self.running = running
        self.awaiting_human = awaiting_human
        self._publication = publication
        self._closes_issue = closes_issue
        self._pipeline = pipeline
        self.events: list[tuple[str, int]] = []
        self.failure_comments: list[tuple[int, str]] = []

    def verify_project(self) -> GitLabProject:
        return GitLabProject(
            id=310967,
            host="gitlab.example.com",
            default_branch="main",
            web_url="https://gitlab.example.com/group/project",
        )

    def list_eligible(self) -> tuple[GitLabIssue, ...]:
        return self.eligible

    def list_running(self) -> tuple[GitLabIssue, ...]:
        return self.running

    def list_awaiting_human(self) -> tuple[GitLabIssue, ...]:
        return self.awaiting_human

    def latest_default_pipeline(
        self, project: GitLabProject
    ) -> PipelineEvidence | None:
        del project
        return self._pipeline

    def fetch_issue(self, iid: int) -> GitLabIssue:
        return next(
            issue for issue in (*self.eligible, *self.running) if issue.iid == iid
        )

    def mark_running(self, issue: GitLabIssue) -> None:
        self.events.append(("running", issue.iid))
        self.running = (issue,)

    def mark_awaiting_human(self, issue: GitLabIssue) -> None:
        self.events.append(("awaiting_human", issue.iid))

    def mark_failed(self, issue: GitLabIssue) -> None:
        self.events.append(("failed", issue.iid))

    def comment_failed(self, issue: GitLabIssue, reason: str) -> None:
        self.failure_comments.append((issue.iid, reason))

    def publication(self, branch: str) -> MergeRequestEvidence | None:
        del branch
        return self._publication

    def mr_closes_issue(self, mr_iid: int, issue_iid: int) -> bool:
        del mr_iid, issue_iid
        return self._closes_issue

    def create_merge_request(
        self, issue: GitLabIssue, project: GitLabProject, draft: MergeRequestDraft
    ) -> MergeRequestEvidence:
        del project, draft
        self.events.append(("create_merge_request", issue.iid))
        self._publication = _publication(issue)
        return self._publication


class _Checkout:
    """Semantic checkout boundary fake; it never touches a real repository."""

    def __init__(
        self,
        *,
        sha: str = "a" * 40,
        branch_error: RuntimeError | None = None,
        resume_error: RuntimeError | None = None,
        baseline_error: RuntimeError | None = None,
        publish_error: RuntimeError | None = None,
    ) -> None:
        self.path = Path("/trusted/checkout")
        self.events: list[str] = []
        self.sha = sha
        self.branch_error = branch_error
        self.resume_error = resume_error
        self.baseline_error = baseline_error
        self.publish_error = publish_error

    def preview_preflight(self, *_args: object) -> None:
        self.events.append("preview_preflight")

    def applied_preflight(self, project: GitLabProject) -> None:
        del project
        self.events.append("applied_preflight")

    def ensure_branch_available(self, branch: str, default_branch: str) -> None:
        del branch, default_branch
        self.events.append("ensure_branch_available")
        if self.branch_error is not None:
            raise self.branch_error

    def prepare_branch(self, branch: str, default_branch: str) -> Path:
        del branch, default_branch
        self.events.append("prepare_branch")
        return self.path

    def resume_branch(self, branch: str, default_branch: str) -> Path:
        del branch, default_branch
        self.events.append("resume_branch")
        if self.resume_error is not None:
            raise self.resume_error
        return self.path

    def publication_tip(self, branch: str) -> GitPublicationEvidence:
        del branch
        self.events.append("publication_tip")
        return GitPublicationEvidence(local_sha=self.sha, remote_sha=self.sha)

    def worker_baseline(self, branch: str) -> str:
        del branch
        self.events.append("worker_baseline")
        if self.baseline_error is not None:
            raise self.baseline_error
        return self.sha

    def publish(self, branch: str, draft: MergeRequestDraft, baseline: str) -> None:
        del branch, draft, baseline
        self.events.append("publish")
        if self.publish_error is not None:
            raise self.publish_error


class _Invoke:
    """Agent boundary fake that records whether work was actually invoked."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        error: RuntimeError | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.returncode = returncode
        self.error = error
        self.events = events
        self.calls: list[tuple[Path, str, int]] = []

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> AgentInvocation:
        assert prompt
        self.calls.append((checkout, prompt, timeout))
        if self.events is not None:
            self.events.append("invoke")
        if self.error is not None:
            raise self.error
        return AgentInvocation(
            returncode=self.returncode,
            stdout=(
                '<agent-work-merge-request>{"title":"Worker change",'
                '"description":"Validated change."}</agent-work-merge-request>'
            ),
        )


def _publication(issue: GitLabIssue) -> MergeRequestEvidence:
    return MergeRequestEvidence(
        iid=7,
        web_url="https://gitlab.example.com/group/project/-/merge_requests/7",
        state="opened",
        source_project_id=310967,
        target_project_id=310967,
        source_branch=f"agent/issue-{issue.iid}",
        target_branch="main",
        sha="a" * 40,
    )


def _failed_pipeline() -> PipelineEvidence:
    return PipelineEvidence(
        id=42,
        status="failed",
        sha="b" * 40,
        web_url="https://gitlab.example.com/group/project/-/pipelines/42",
    )


def _deps(
    gitlab: _GitLab, checkout: _Checkout, invoke: _Invoke, lock: _Lock
) -> RunNextDependencies:
    return RunNextDependencies(
        gitlab=gitlab, checkout=checkout, invoke=invoke, lock=lock
    )


def test_preview_is_read_only_and_reports_idle_without_ready_or_running_work() -> None:
    gitlab, checkout, invoke, lock = _GitLab(), _Checkout(), _Invoke(), _Lock()

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=False, timeout_seconds=30
    )

    assert report.outcome == "idle"
    assert gitlab.events == []
    assert checkout.events == []
    assert invoke.calls == []
    assert lock.entered == 0


def test_preview_reports_ready_work_without_claiming_or_invoking() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=False, timeout_seconds=30
    )

    assert report.outcome == "preview_ready"
    assert report.issue_iid == issue.iid
    assert gitlab.events == []
    assert checkout.events == []
    assert invoke.calls == []


def test_applied_claim_requires_the_selected_issue_to_be_the_only_gitlab_lock() -> None:
    issue = _issue(iid=123)

    class _ConflictingGitLab(_GitLab):
        def mark_running(self, issue: GitLabIssue) -> None:
            super().mark_running(issue)
            self.running = (issue, _issue(iid=124, labels=("agent::running",)))

    gitlab, checkout, invoke, lock = (
        _ConflictingGitLab(eligible=(issue,)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    with pytest.raises(RunNextError, match="lock is not uniquely held"):
        run_next(_deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30)

    assert invoke.calls == []


def test_preview_reports_running_work_without_recovery_or_invocation() -> None:
    issue = _issue(iid=123, labels=("agent::running",))
    gitlab, checkout, invoke, lock = (
        _GitLab(running=(issue,)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    progress: list[str] = []

    report = run_next(
        _deps(gitlab, checkout, invoke, lock),
        apply=False,
        timeout_seconds=30,
        report_progress=progress.append,
    )

    assert report.outcome == "preview_running"
    assert report.issue_iid == issue.iid
    assert gitlab.events == []
    assert checkout.events == []
    assert invoke.calls == []
    assert lock.entered == 0
    assert progress == ["Issue #123 is already running on agent/issue-123."]


def test_applied_run_selects_only_the_first_ready_issue() -> None:
    first, second = _issue(iid=12), _issue(iid=13)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(first, second), publication=_publication(first)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.issue_iid == first.iid
    assert gitlab.events == [("running", first.iid), ("awaiting_human", first.iid)]
    assert len(invoke.calls) == 1


def test_applied_run_directs_the_worker_to_repair_a_failed_default_pipeline() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(
            eligible=(issue,),
            publication=_publication(issue),
            pipeline=_failed_pipeline(),
        ),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    run_next(_deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30)

    assert "Pipeline remediation:" in invoke.calls[0][1]
    assert "pipeline 42" in invoke.calls[0][1]
    assert "failed" in invoke.calls[0][1]


def test_worker_prompt_defines_the_trusted_runner_worker_mode() -> None:
    """Worker edits stay uncommitted for trusted runner publication."""
    issue = _issue(iid=123)
    project = _GitLab().verify_project()

    prompt = _prompt(issue, project, "agent/issue-123")

    assert "worker exception takes precedence over" in prompt
    assert "leave only uncommitted working-tree" in prompt
    assert "and alone stages, commits, pushes, and opens the merge request" in prompt


def test_failed_applied_issue_reports_agent_exit_and_diagnostics() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(),
        _Invoke(returncode=7),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.failure_reason == (
        "Agent exited with status 7; no exact published MR evidence"
    )
    assert gitlab.failure_comments == [(issue.iid, report.failure_reason)]


def test_failed_publication_names_the_trusted_boundary() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(
            publish_error=AdapterError(
                "pushing trusted worker branch failed (exit 128)"
            )
        ),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.failure_reason == (
        "trusted branch publication failed: pushing trusted worker branch failed "
        "(exit 128); no exact published MR evidence"
    )
    assert gitlab.failure_comments == [(issue.iid, report.failure_reason)]


def test_applied_ready_issue_invokes_once_then_parks_on_exact_publication() -> None:
    issue = _issue(iid=123)
    progress: list[str] = []
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,), publication=_publication(issue)),
        _Checkout(),
        _Invoke(events=progress),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock),
        apply=True,
        timeout_seconds=45,
        report_progress=progress.append,
    )

    assert report.outcome == "awaiting_human"
    assert report.issue_iid == issue.iid
    assert report.merge_request_url == _publication(issue).web_url
    assert gitlab.events == [("running", issue.iid), ("awaiting_human", issue.iid)]
    assert len(invoke.calls) == 1
    assert invoke.calls[0][0] == checkout.path
    assert invoke.calls[0][2] == 45
    assert lock.entered == 1
    assert progress == [
        "Claiming issue #123 on agent/issue-123.",
        "Starting agent for issue #123 on agent/issue-123.",
        "invoke",
    ]


def test_applied_issue_fails_when_existing_publication_does_not_close_issue() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,), publication=_publication(issue), closes_issue=False),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "failed"
    assert gitlab.events == [("running", issue.iid), ("failed", issue.iid)]
    assert len(invoke.calls) == 1


def test_stranded_single_running_issue_is_failed_without_another_invocation() -> None:
    issue = _issue(iid=123, labels=("agent::running",))
    progress: list[str] = []
    gitlab, checkout, invoke, lock = (
        _GitLab(running=(issue,)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock),
        apply=True,
        timeout_seconds=30,
        report_progress=progress.append,
    )

    assert report.outcome == "failed"
    assert report.issue_iid == issue.iid
    assert gitlab.events == [("failed", issue.iid)]
    assert invoke.calls == []
    assert progress == ["Reconciling running issue #123 on agent/issue-123."]


def test_stranded_running_issue_recovers_exact_mr_without_another_invocation() -> None:
    issue = _issue(iid=123, labels=("agent::running",))
    gitlab, checkout, invoke, lock = (
        _GitLab(running=(issue,), publication=_publication(issue)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "awaiting_human"
    assert gitlab.events == [("awaiting_human", issue.iid)]
    assert invoke.calls == []


def test_applied_run_reclaims_a_conflicted_human_review_mr_for_remediation() -> None:
    issue = _issue(iid=123, labels=("agent::awaiting-human",))
    conflicted_publication = _publication(issue).model_copy(
        update={"has_conflicts": True}
    )
    resolved_publication = _publication(issue)

    class _ConflictResolvedByAgent(_GitLab):
        def publication(self, branch: str) -> MergeRequestEvidence | None:
            del branch
            return (
                resolved_publication
                if self.events == [("running", issue.iid)]
                else conflicted_publication
            )

    gitlab, checkout, invoke, lock = (
        _ConflictResolvedByAgent(awaiting_human=(issue,)),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "awaiting_human"
    assert gitlab.events == [("running", issue.iid), ("awaiting_human", issue.iid)]
    assert checkout.events == [
        "applied_preflight",
        "resume_branch",
        "worker_baseline",
        "publish",
        "publication_tip",
    ]
    assert len(invoke.calls) == 1
    assert invoke.calls[0][0] == checkout.path
    assert invoke.calls[0][2] == 30


def test_conflicted_handoff_fails_when_its_branch_cannot_be_restored() -> None:
    issue = _issue(iid=123, labels=("agent::awaiting-human",))
    publication = _publication(issue).model_copy(update={"has_conflicts": True})
    gitlab, checkout, invoke, lock = (
        _GitLab(awaiting_human=(issue,), publication=publication),
        _Checkout(resume_error=AdapterError("checkout validation failed")),
        _Invoke(),
        _Lock(),
    )
    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "failed"
    assert (
        report.failure_reason
        == "checkout preparation failed: checkout validation failed"
    )
    assert gitlab.events == [("running", issue.iid), ("failed", issue.iid)]
    assert invoke.calls == []


def test_applied_issue_fails_when_the_worker_baseline_cannot_be_captured() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(baseline_error=AdapterError("checkout validation failed")),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "failed"
    assert (
        report.failure_reason
        == "checkout preparation failed: checkout validation failed"
    )
    assert gitlab.events == [("running", issue.iid), ("failed", issue.iid)]
    assert checkout.events == [
        "applied_preflight",
        "ensure_branch_available",
        "prepare_branch",
        "worker_baseline",
    ]
    assert invoke.calls == []


@pytest.mark.parametrize(
    ("updates", "checkout_sha"),
    (
        ({"source_project_id": 999}, "a" * 40),
        ({"target_project_id": 999}, "a" * 40),
        ({"target_branch": "release"}, "a" * 40),
        ({"source_branch": "agent/issue-999"}, "a" * 40),
        ({"state": "closed"}, "a" * 40),
        ({}, "b" * 40),
    ),
)
def test_publication_must_match_project_branch_target_state_and_tip(
    updates: dict[str, object], checkout_sha: str
) -> None:
    issue = _issue(iid=123)
    evidence = _publication(issue).model_copy(update=updates)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,), publication=evidence),
        _Checkout(sha=checkout_sha),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "failed"
    assert gitlab.events[-1] == ("failed", issue.iid)


def test_multiple_running_issues_fail_closed_without_mutation_or_invocation() -> None:
    gitlab, checkout, invoke, lock = (
        _GitLab(running=(_issue(iid=1), _issue(iid=2))),
        _Checkout(),
        _Invoke(),
        _Lock(),
    )

    with pytest.raises(RuntimeError):
        run_next(_deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30)

    assert gitlab.events == []
    assert checkout.events == []
    assert invoke.calls == []


def test_nonzero_agent_exit_still_parks_a_verified_publication() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,), publication=_publication(issue)),
        _Checkout(),
        _Invoke(returncode=1),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "awaiting_human"
    assert gitlab.events[-1] == ("awaiting_human", issue.iid)
    assert len(invoke.calls) == 1


def test_agent_timeout_still_parks_a_verified_publication() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,), publication=_publication(issue)),
        _Checkout(),
        _Invoke(error=AdapterError("a required command could not be completed")),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "awaiting_human"
    assert gitlab.events == [("running", issue.iid), ("awaiting_human", issue.iid)]
    assert len(invoke.calls) == 1


def test_branch_conflict_marks_the_ready_issue_failed_without_invoking() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(branch_error=BranchConflict("branch conflict")),
        _Invoke(),
        _Lock(),
    )

    report = run_next(
        _deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30
    )

    assert report.outcome == "failed"
    assert report.failure_reason == "branch conflict"
    assert gitlab.events == [("failed", issue.iid)]
    assert gitlab.failure_comments == [(issue.iid, "branch conflict")]
    assert checkout.events == ["applied_preflight", "ensure_branch_available"]
    assert invoke.calls == []


def test_branch_check_boundary_failure_stops_without_marking_the_issue_failed() -> None:
    issue = _issue(iid=123)
    gitlab, checkout, invoke, lock = (
        _GitLab(eligible=(issue,)),
        _Checkout(branch_error=AdapterError("checkout validation failed")),
        _Invoke(),
        _Lock(),
    )

    with pytest.raises(AdapterError, match="checkout validation failed"):
        run_next(_deps(gitlab, checkout, invoke, lock), apply=True, timeout_seconds=30)

    assert gitlab.events == []
    assert checkout.events == ["applied_preflight", "ensure_branch_available"]
    assert invoke.calls == []
