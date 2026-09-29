# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Numeric-project GitLab adapter for recoverable agent work."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from urllib.parse import quote

from pydantic import ValidationError

from .domain import (
    AGENT_OUTCOME_NOTE_MARKER,
    CANARY_LABEL_SPECS,
    HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
    LIFECYCLE_LABELS,
    MANUAL_TRACKER_LABEL,
    PREREQUISITE_LABEL_COLOR,
    PREREQUISITE_LABEL_DESCRIPTION,
    JobTraceEvidence,
    PipelineJobEvidence,
    legacy_prerequisite_label_iid,
    prerequisite_dependency_proof,
    prerequisite_label_iid,
    sanitize_outcome_note_field,
)
from .gitlab_safeguards import (
    SafeguardPayloadError,
    default_branch_from_project,
    username_from_payload,
    verify_approval_rules,
    verify_default_branch,
)
from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabIssueNote,
    GitLabProject,
    MergeRequestDraft,
    MergeRequestEvidence,
    MergeRequestFeedback,
    PipelineEvidence,
    render_decision_review_notice,
)
from .ports import CommandRunner
from .repository import CommandTimeoutError


class GitLabError(RuntimeError):
    """A GitLab boundary failed without exposing command output."""


class GitLabIdentityError(GitLabError):
    """The configured GitLab service identity changed unexpectedly."""


PROJECT_FIELDS = (
    "id",
    "default_branch",
    "web_url",
    "http_url_to_repo",
    "ssh_url_to_repo",
)
ISSUE_FIELDS = (
    "iid",
    "title",
    "description",
    "labels",
    "state",
    "confidential",
    "created_at",
)
MR_FIELDS = (
    "iid",
    "web_url",
    "state",
    "source_project_id",
    "target_project_id",
    "source_branch",
    "target_branch",
    "sha",
    "has_conflicts",
    "description",
    "title",
    "draft",
)
PIPELINE_FIELDS = ("id", "status", "sha", "web_url")
PIPELINE_JOB_FIELDS = (
    "id",
    "name",
    "stage",
    "status",
    "failure_reason",
    "retried",
    "allow_failure",
    "pipeline",
)
NOTE_FIELDS = ("id", "body", "author", "created_at", "system")
NOTE_AUTHOR_FIELDS = ("username",)
_MAX_NOTE_PAGES = 100
_MAX_ISSUE_PAGES = 100
_MAX_MERGE_REQUEST_PAGES = 100
_MAX_LABEL_PAGES = 100
_MAX_PIPELINE_JOB_PAGES = 100
_HIGH_PRIORITY_LABEL = "agent-priority::high"
_LOW_PRIORITY_LABEL = "agent-priority::low"
_MAX_CLOSING_PROJECTION_READS = 8
_CLOSING_PROJECTION_RETRY_DELAY_SECONDS = 1.0
_OUTCOME_NOTE_MARKER = re.compile(r"<!-- agent-work-outcome:v2 key=[0-9a-f]{64} -->")
_OUTCOME_NOTE_NAMESPACE = "<!-- agent-work-outcome:v2"


def _selected(payload: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Select repository-owned fields from a larger GitLab response."""
    return {field: payload[field] for field in fields if field in payload}


def _desired_description(draft: MergeRequestDraft, issue_iid: int) -> str:
    """Build one canonical description with exactly one closing reference."""
    _reject_escaped_linebreaks(draft.description)
    body = (
        render_decision_review_notice(draft.decision_review, draft.description)
        if draft.decision_review is not None
        else draft.description.rstrip()
    )
    closing = f"Closes #{issue_iid}"
    pattern = re.compile(rf"^\s*closes\s*#\s*{issue_iid}\s*$", re.IGNORECASE)
    retained = "\n".join(line for line in body.splitlines() if not pattern.match(line))
    notice = (
        "<!-- agent-work-feedback-notice:v1 -->\n"
        "Human MR comments and inline replies are read and acted on; "
        "no-action comments are acknowledged."
    )
    retained = re.sub(
        r"(?m)^<!-- agent-work-feedback-notice:v1 -->\n"
        r"Human MR comments and inline replies are read and acted on; "
        r"no-action comments are acknowledged\.\n?",
        "",
        retained,
    )
    retained = "\n".join(
        line
        for line in retained.splitlines()
        if "agent-work-feedback-notice:v1" not in line
    )
    return f"{retained.rstrip()}\n\n{notice}\n\n{closing}"


def reject_unsafe_issue_text(value: str) -> None:
    """Reject encoded linebreak text before it reaches a GitLab transport."""
    if re.search(r"\\(?:r\\n|r|n)|%0[ad]", value, re.IGNORECASE):
        raise GitLabError("issue text contains an escaped linebreak")


def _reject_escaped_linebreaks(description: str) -> None:
    """Apply the shared issue-text guard to merge-request Markdown too."""
    try:
        reject_unsafe_issue_text(description)
    except GitLabError as error:
        raise GitLabError(
            "merge request description contains escaped linebreaks"
        ) from error


@dataclass(frozen=True)
class IssuePublicationEvidence:
    """The exact GitLab fields verified after one issue mutation."""

    project_id: int
    iid: int
    title: str
    description: str
    labels: tuple[str, ...]
    state: str
    web_url: str | None = None


@dataclass(frozen=True)
class IssuePublicationReport:
    """The preview or verified result of structured issue publication."""

    applied: bool
    action: str
    issue: IssuePublicationEvidence | None
    description: str
    marker: str


class IssuePublicationError(GitLabError):
    """A structured issue publication request was rejected."""


class IssuePublicationPort(Protocol):
    """Minimal transport needed to select, mutate, and read back one issue."""

    def list_issues(
        self, *, project_id: int, marker: str
    ) -> tuple[dict[str, object], ...]: ...

    def create_issue(self, **kwargs: object) -> dict[str, object]: ...

    def update_issue(self, **kwargs: object) -> dict[str, object]: ...

    def fetch_issue(self, *, project_id: int, iid: int) -> dict[str, object]: ...


def _issue_evidence(value: object) -> IssuePublicationEvidence:
    """Project an untrusted transport result into exact issue evidence."""
    if not isinstance(value, dict):
        raise IssuePublicationError("issue read-back is invalid")
    payload = cast("dict[str, object]", value)
    project_id, iid = payload.get("project_id"), payload.get("iid")
    title, description, labels, state = (
        payload.get("title"),
        payload.get("description"),
        payload.get("labels"),
        payload.get("state", "opened"),
    )
    if (
        isinstance(project_id, bool)
        or not isinstance(project_id, int)
        or project_id <= 0
        or isinstance(iid, bool)
        or not isinstance(iid, int)
        or iid <= 0
        or not isinstance(title, str)
        or not isinstance(description, str)
        or not isinstance(labels, (tuple, list))
        or not all(isinstance(label, str) for label in labels)
        or not isinstance(state, str)
    ):
        raise IssuePublicationError("issue read-back is invalid")
    web_url = payload.get("web_url")
    if web_url is not None and not isinstance(web_url, str):
        raise IssuePublicationError("issue read-back is invalid")
    return IssuePublicationEvidence(
        project_id,
        iid,
        title,
        description,
        tuple(cast(str, label) for label in labels),
        state,
        web_url,
    )


def _render_issue_description(
    *,
    problem: str,
    impact: str | None,
    outcome: str,
    acceptance: tuple[str, ...],
    additional_context: str | None,
    marker: str,
    priority: str | None,
    severity: str | None,
) -> str:
    sections = [("Problem", problem)]
    if impact is not None:
        sections.append(("Impact", impact))
    sections.extend(
        (
            ("Outcome", outcome),
            ("Acceptance criteria", "\n".join(f"- {item}" for item in acceptance)),
        )
    )
    if additional_context is not None:
        sections.append(("Additional context", additional_context))
    if priority is not None:
        sections.append(("Priority", priority))
    if severity is not None:
        sections.append(("Severity", severity))
    return "\n\n".join(
        [*(f"## {heading}\n{body}" for heading, body in sections), marker]
    )


def _issue_has_marker(item: dict[str, object], marker: str) -> bool:
    candidate_description = item.get("description")
    return isinstance(candidate_description, str) and marker in candidate_description


def _reject_duplicate_remediation_markers(
    *, additional_context: str | None, marker: str
) -> None:
    """Reject issue input that would publish more than one remediation marker."""
    marker_count = sum(
        value.count("agent-mr-remediation")
        for value in (additional_context, marker)
        if value is not None
    )
    if marker_count > 1:
        raise IssuePublicationError(
            "issue publication contains duplicate remediation marker"
        )


def publish_issue(
    *,
    project_id: int,
    title: str,
    problem: str,
    outcome: str,
    acceptance: tuple[str, ...],
    impact: str | None = None,
    additional_context: str | None = None,
    marker: str | None = None,
    priority: str | None = None,
    severity: str | None = None,
    labels: tuple[str, ...] = (),
    issue_iid: int | None = None,
    apply: bool = False,
    port: IssuePublicationPort,
    feedback_audit: bool = False,
    audit_feedback: Callable[[], None] | None = None,
) -> IssuePublicationReport:
    """Preview or mutate one exact issue and verify its authoritative read-back."""
    if (
        not isinstance(marker, str)
        or not marker.strip()
        or marker == "<!-- agent-work-issue-publication:v1 -->"
    ):
        raise IssuePublicationError("issue publication marker is invalid")
    _reject_duplicate_remediation_markers(
        additional_context=additional_context, marker=marker
    )
    values = (
        title,
        problem,
        outcome,
        *acceptance,
        impact,
        additional_context,
        marker,
        priority,
        severity,
        *labels,
    )
    try:
        if (
            isinstance(project_id, bool)
            or not isinstance(project_id, int)
            or project_id <= 0
            or not title.strip()
            or not problem.strip()
            or not outcome.strip()
            or not acceptance
            or any(not item.strip() for item in acceptance)
            or any(value is not None and not isinstance(value, str) for value in values)
        ):
            raise ValueError
        for value in values:
            if value is not None:
                reject_unsafe_issue_text(value)
    except (AttributeError, GitLabError, ValueError):
        raise IssuePublicationError(
            "issue publication contains invalid linebreak text"
        ) from None
    description = _render_issue_description(
        problem=problem,
        impact=impact,
        outcome=outcome,
        acceptance=acceptance,
        additional_context=additional_context,
        marker=marker,
        priority=priority,
        severity=severity,
    )
    if issue_iid is not None and (
        isinstance(issue_iid, bool) or not isinstance(issue_iid, int) or issue_iid <= 0
    ):
        raise IssuePublicationError("issue IID must be a positive integer")
    if not apply:
        return IssuePublicationReport(False, "would-create", None, description, marker)
    matches = tuple(
        item
        for item in port.list_issues(project_id=project_id, marker=marker)
        if _issue_has_marker(item, marker)
    )
    if issue_iid is not None:
        matches = tuple(item for item in matches if item.get("iid") == issue_iid)
        if len(matches) != 1:
            raise IssuePublicationError("requested issue IID has no exact marker match")
    if len(matches) > 1:
        raise IssuePublicationError("issue publication marker is ambiguous")
    action = "would-update" if matches else "would-create"
    fields: dict[str, object] = {
        "project_id": project_id,
        "title": title,
        "description": description,
        "labels": labels,
    }
    if matches:
        selected = _issue_evidence(matches[0])
        if selected.project_id != project_id or selected.state != "opened":
            raise IssuePublicationError("selected issue evidence is not open")
        port.update_issue(iid=selected.iid, **fields)
        expected_iid = selected.iid
    else:
        if feedback_audit:
            if audit_feedback is None:
                raise IssuePublicationError("feedback audit is unavailable")
            audit_feedback()
        submitted = port.create_issue(**fields)
        if not isinstance(submitted, dict) or not isinstance(submitted.get("iid"), int):
            raise IssuePublicationError("issue creation response is invalid")
        expected_iid = cast(int, submitted["iid"])
    read_back = _issue_evidence(
        port.fetch_issue(project_id=project_id, iid=expected_iid)
    )
    if (
        read_back.project_id != project_id
        or read_back.iid != expected_iid
        or read_back.title != title
        or read_back.description != description
        or read_back.labels != labels
        or read_back.state != "opened"
    ):
        raise IssuePublicationError(
            "issue read-back does not match requested publication"
        )
    return IssuePublicationReport(
        True, action.removeprefix("would-"), read_back, description, marker
    )


def _validated_issue_iid(issue_iid: int | None) -> int | None:
    """Return an optional positive issue IID while rejecting bools and malformed values."""
    if issue_iid is None:
        return None
    if isinstance(issue_iid, bool) or not isinstance(issue_iid, int) or issue_iid <= 0:
        raise GitLabError("merge request issue IID must be a positive integer")
    return issue_iid


_CLOSING_KEYWORD = (
    r"(?:clos(?:e|es|ed|ing)|fix(?:es|ed|ing)?|resolv(?:e|es|ed|ing)|"
    r"implement(?:s|ed|ing)?)"
)
_CLOSING_REFERENCE = (
    r"(?:(?:[\w.-]+/)*[\w.-]*#\d+|https?://\S+/(?:issues|work_items)/\d+)"
)
_CLOSING_DIRECTIVE = re.compile(
    rf"\b{_CLOSING_KEYWORD}\s*:?(?:\s+issues?)?\s+{_CLOSING_REFERENCE}",
    re.IGNORECASE,
)


def _reject_user_closing_directives(values: tuple[str | None, ...]) -> None:
    """Reserve GitLab closing directives for the single structured issue option."""
    if any(value is not None and _CLOSING_DIRECTIVE.search(value) for value in values):
        raise GitLabError("use --issue-iid for a merge request closing issue IID")


def render_merge_request_description(
    *,
    problem: str,
    outcome: str,
    validation: tuple[str, ...],
    impact: str | None = None,
    human_action: str | None = None,
    tracking: str | None = None,
    issue_iid: int | None = None,
) -> str:
    """Render the stable, human-readable ordinary merge-request body."""
    issue_iid = _validated_issue_iid(issue_iid)
    _reject_user_closing_directives(
        (problem, outcome, *validation, impact, human_action, tracking)
    )
    sections = [("Problem", problem)]
    if impact:
        sections.append(("Impact", impact))
    sections.append(("Outcome", outcome))
    if human_action:
        sections.append(("Human action", human_action))
    sections.append(("Validation", "\n".join(f"- {item}" for item in validation)))
    if tracking:
        sections.append(("Tracking", tracking))
    description = "\n\n".join(f"## {heading}\n{body}" for heading, body in sections)
    return description if issue_iid is None else f"{description}\n\nCloses #{issue_iid}"


class MergeRequestPublicationPort(Protocol):
    """Direct publication transport for one ordinary merge request."""

    def list_merge_requests(
        self, *, project_id: int, source_branch: str
    ) -> tuple[MergeRequestEvidence, ...]: ...
    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence: ...
    def update_merge_request(
        self,
        *,
        project_id: int,
        iid: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence: ...
    def fetch_merge_request(
        self, *, project_id: int, iid: int
    ) -> MergeRequestEvidence: ...
    def fetch_closing_issues(
        self, *, project_id: int, iid: int
    ) -> tuple[tuple[int, int], ...]: ...


@dataclass(frozen=True)
class MergeRequestPublicationReport:
    """Immutable result of a preview or exact ordinary publication."""

    applied: bool
    action: str
    merge_request: MergeRequestEvidence | None
    description: str

    def __post_init__(self) -> None:
        """Require authoritative evidence for any applied result."""
        if self.applied != (self.merge_request is not None):
            raise GitLabError("publication report evidence does not match apply state")


def publish_merge_request(
    *,
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    issue_iid: int | None = None,
    apply: bool,
    port: MergeRequestPublicationPort,
    sleeper: Callable[[float], None],
) -> MergeRequestPublicationReport:
    """Preview or publish exactly one merge request and verify it by read-back."""
    issue_iid = _validated_issue_iid(issue_iid)
    _reject_escaped_linebreaks(description)
    candidates = port.list_merge_requests(
        project_id=project_id, source_branch=source_branch
    )
    if len(candidates) > 1:
        raise GitLabError(
            "more than one open merge request exists for the source branch"
        )
    action = "would-update" if candidates else "would-create"
    if not apply:
        return MergeRequestPublicationReport(False, action, None, description)
    if candidates and (
        candidates[0].state != "opened"
        or candidates[0].source_branch != source_branch
        or candidates[0].target_branch != target_branch
        or candidates[0].source_project_id != project_id
        or candidates[0].target_project_id != project_id
    ):
        raise GitLabError("listed merge request does not match requested publication")
    submitted = (
        port.update_merge_request(
            project_id=project_id,
            iid=candidates[0].iid,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
        )
        if candidates
        else port.create_merge_request(
            project_id=project_id,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
        )
    )
    if candidates and submitted.iid != candidates[0].iid:
        raise GitLabError(
            "updated merge request does not match the listed merge request"
        )
    read_back = port.fetch_merge_request(project_id=project_id, iid=submitted.iid)
    for read_number in range(
        _MAX_CLOSING_PROJECTION_READS if issue_iid is not None else 1
    ):
        if (
            read_back.iid != submitted.iid
            or read_back.web_url != submitted.web_url
            or read_back.sha != submitted.sha
            or read_back.state != "opened"
            or read_back.source_branch != source_branch
            or read_back.target_branch != target_branch
            or read_back.title != title
            or read_back.description != description
            or read_back.has_conflicts
            or (
                read_back.source_project_id is not None
                and read_back.source_project_id != project_id
            )
            or (
                read_back.target_project_id is not None
                and read_back.target_project_id != project_id
            )
        ):
            raise GitLabError(
                "merge request read-back does not match the requested publication"
            )
        if issue_iid is None:
            break
        projection = port.fetch_closing_issues(project_id=project_id, iid=read_back.iid)
        if projection == ((project_id, issue_iid),):
            break
        if projection or read_number == _MAX_CLOSING_PROJECTION_READS - 1:
            raise GitLabError(
                "merge request closing issue projection is not exact for "
                f"MR !{submitted.iid} at {submitted.sha}"
            )
        sleeper(_CLOSING_PROJECTION_RETRY_DELAY_SECONDS)
        read_back = port.fetch_merge_request(project_id=project_id, iid=submitted.iid)
    return MergeRequestPublicationReport(
        True, action.removeprefix("would-"), read_back, description
    )


class GitLabMergeRequestPublisher:
    """Small numeric-project GitLab transport for ordinary MR publication."""

    def __init__(
        self, *, host: str, project_id: int, glab_path: str, run_command: CommandRunner
    ) -> None:
        self._host, self._project_id, self._glab_path, self._run_command = (
            host,
            project_id,
            glab_path,
            run_command,
        )

    def _require_project(self, project_id: int) -> None:
        if project_id != self._project_id:
            raise GitLabError("publication project does not match configured project")

    def _call(
        self, endpoint: str, *, method: str = "GET", fields: tuple[str, ...] = ()
    ) -> object:
        return _call_gitlab(
            host=self._host,
            glab_path=self._glab_path,
            run_command=self._run_command,
            endpoint=endpoint,
            method=method,
            fields=fields,
        )

    def _mr(self, payload: object) -> MergeRequestEvidence:
        return _merge_request(payload)

    def list_merge_requests(
        self, *, project_id: int, source_branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        self._require_project(project_id)
        payload = self._call(
            f"projects/{project_id}/merge_requests",
            fields=("state=opened", f"source_branch={source_branch}"),
        )
        if not isinstance(payload, list):
            raise GitLabError("GitLab returned invalid merge request list")
        return tuple(self._mr(item) for item in payload)

    def list_any_merge_requests(
        self, *, project_id: int, source_branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        """Find even closed requests before a replayable branch publication."""
        self._require_project(project_id)
        payload = self._call(
            f"projects/{project_id}/merge_requests",
            fields=("state=all", f"source_branch={source_branch}", "per_page=100"),
        )
        if not isinstance(payload, list) or len(payload) >= 100:
            raise GitLabError("GitLab returned ambiguous merge request inventory")
        return tuple(self._mr(item) for item in payload)

    def create_merge_request(
        self,
        *,
        project_id: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence:
        self._require_project(project_id)
        _reject_escaped_linebreaks(description)
        return self._mr(
            self._call(
                f"projects/{project_id}/merge_requests",
                method="POST",
                fields=(
                    f"source_branch={source_branch}",
                    f"target_branch={target_branch}",
                    f"title={title}",
                    f"description={description}",
                    "remove_source_branch=true",
                    "squash=true",
                ),
            )
        )

    def update_merge_request(
        self,
        *,
        project_id: int,
        iid: int,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> MergeRequestEvidence:
        self._require_project(project_id)
        _reject_escaped_linebreaks(description)
        return self._mr(
            self._call(
                f"projects/{project_id}/merge_requests/{iid}",
                method="PUT",
                fields=(
                    f"target_branch={target_branch}",
                    f"title={title}",
                    f"description={description}",
                ),
            )
        )

    def fetch_merge_request(self, *, project_id: int, iid: int) -> MergeRequestEvidence:
        self._require_project(project_id)
        return self._mr(self._call(f"projects/{project_id}/merge_requests/{iid}"))

    def fetch_closing_issues(
        self, *, project_id: int, iid: int
    ) -> tuple[tuple[int, int], ...]:
        """Return the validated GitLab closing-issue projection for one MR."""
        self._require_project(project_id)
        payload = self._call(
            f"projects/{project_id}/merge_requests/{iid}/closes_issues"
        )
        if not isinstance(payload, list):
            raise GitLabError("GitLab returned invalid closing issue list")
        projection: list[tuple[int, int]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise GitLabError("GitLab returned invalid closing issue list")
            closing = cast("dict[str, object]", item)
            closing_project_id, closing_iid = (
                closing.get("project_id"),
                closing.get("iid"),
            )
            if (
                isinstance(closing_project_id, bool)
                or not isinstance(closing_project_id, int)
                or closing_project_id <= 0
                or isinstance(closing_iid, bool)
                or not isinstance(closing_iid, int)
                or closing_iid <= 0
            ):
                raise GitLabError("GitLab returned invalid closing issue list")
            projection.append((closing_project_id, closing_iid))
        return tuple(projection)


class GitLabIssuePublisher:
    """Numeric-project transport for the shared structured issue boundary."""

    def __init__(
        self, *, host: str, project_id: int, glab_path: str, run_command: CommandRunner
    ) -> None:
        self._host = host
        self._project_id = project_id
        self._glab_path = glab_path
        self._run_command = run_command

    def _call(
        self, endpoint: str, *, method: str = "GET", fields: tuple[str, ...] = ()
    ) -> object:
        return _call_gitlab(
            host=self._host,
            glab_path=self._glab_path,
            run_command=self._run_command,
            endpoint=endpoint,
            method=method,
            fields=fields,
        )

    def _require_project(self, project_id: int) -> None:
        if project_id != self._project_id:
            raise GitLabError("publication project does not match configured project")

    def audit_feedback(self, *, username: str) -> None:
        """Verify the configured factory identity and human review safeguards."""
        try:
            if username_from_payload(self._call("user")) != username:
                raise SafeguardPayloadError("feedback audit user is invalid")
            default_branch = default_branch_from_project(
                self._call(f"projects/{self._project_id}"),
                project_id=self._project_id,
                require_issues=True,
            )
            verify_default_branch(
                self._call(
                    f"projects/{self._project_id}/repository/branches/"
                    f"{quote(default_branch, safe='')}"
                ),
                branch=default_branch,
            )
            verify_approval_rules(
                self._call(f"projects/{self._project_id}/approval_rules")
            )
        except SafeguardPayloadError as error:
            raise IssuePublicationError(
                "feedback audit safeguards are invalid"
            ) from error

    def list_issues(
        self, *, project_id: int, marker: str
    ) -> tuple[dict[str, object], ...]:
        self._require_project(project_id)
        matches: list[dict[str, object]] = []
        for page in range(1, _MAX_ISSUE_PAGES + 1):
            payload = self._call(
                f"projects/{project_id}/issues",
                fields=("state=opened", "per_page=100", f"page={page}"),
            )
            if not isinstance(payload, list) or not all(
                isinstance(item, dict) for item in payload
            ):
                raise GitLabError("GitLab returned invalid issue publication list")
            items = tuple(cast(dict[str, object], item) for item in payload)
            matches.extend(
                item for item in items if marker in str(item.get("description", ""))
            )
            if len(items) < 100:
                return tuple(matches)
        raise GitLabError("GitLab returned too many issue publication pages")

    def list_all_issues(self, *, project_id: int) -> tuple[dict[str, object], ...]:
        """Read a complete bounded open and closed issue inventory."""
        self._require_project(project_id)
        inventory: list[dict[str, object]] = []
        seen: set[int] = set()
        for page in range(1, _MAX_ISSUE_PAGES + 1):
            payload = self._call(
                f"projects/{project_id}/issues",
                fields=("state=all", "per_page=100", f"page={page}"),
            )
            if not isinstance(payload, list) or len(payload) > 100:
                raise GitLabError("GitLab returned invalid issue inventory")
            for value in payload:
                if not isinstance(value, dict):
                    raise GitLabError("GitLab returned invalid issue inventory")
                issue = cast(dict[str, object], value)
                iid = issue.get("iid")
                if issue.get("project_id") != project_id:
                    raise GitLabError("GitLab returned wrong-project issue inventory")
                if type(iid) is not int or iid <= 0 or iid in seen:
                    raise GitLabError("GitLab returned duplicate or invalid issue IID")
                seen.add(iid)
                inventory.append(issue)
            if len(payload) < 100:
                return tuple(inventory)
        raise GitLabError("GitLab issue inventory exceeds page limit")

    def create_issue(self, **kwargs: object) -> dict[str, object]:
        project_id = cast(int, kwargs["project_id"])
        self._require_project(project_id)
        payload = self._call(
            f"projects/{project_id}/issues",
            method="POST",
            fields=(
                f"title={kwargs['title']}",
                f"description={kwargs['description']}",
                f"labels={','.join(cast(tuple[str, ...], kwargs['labels']))}",
            ),
        )
        if not isinstance(payload, dict):
            raise GitLabError("GitLab returned invalid issue publication data")
        return cast(dict[str, object], payload)

    def update_issue(self, **kwargs: object) -> dict[str, object]:
        project_id, iid = cast(int, kwargs["project_id"]), cast(int, kwargs["iid"])
        self._require_project(project_id)
        payload = self._call(
            f"projects/{project_id}/issues/{iid}",
            method="PUT",
            fields=(
                f"title={kwargs['title']}",
                f"description={kwargs['description']}",
                f"labels={','.join(cast(tuple[str, ...], kwargs['labels']))}",
            ),
        )
        if not isinstance(payload, dict):
            raise GitLabError("GitLab returned invalid issue publication data")
        return cast(dict[str, object], payload)

    def fetch_issue(self, *, project_id: int, iid: int) -> dict[str, object]:
        self._require_project(project_id)
        payload = self._call(f"projects/{project_id}/issues/{iid}")
        if not isinstance(payload, dict):
            raise GitLabError("GitLab returned invalid issue publication data")
        return cast(dict[str, object], payload)


def _require_exact_metadata(
    mr: MergeRequestEvidence, *, title: str, description: str, is_draft: bool
) -> MergeRequestEvidence:
    """Reject a GitLab response that did not apply requested MR metadata."""
    if mr.title != title or mr.description != description or mr.is_draft != is_draft:
        raise GitLabError("GitLab did not apply merge request metadata")
    return mr


def _priority_rank(issue: GitLabIssue) -> int:
    """Return the queue rank for one eligible issue."""
    if _HIGH_PRIORITY_LABEL in issue.labels:
        return 0
    if _LOW_PRIORITY_LABEL in issue.labels:
        return 2
    return 1


def _creation_time(issue: GitLabIssue) -> datetime:
    """Parse one absolute issue creation time or fail closed."""
    try:
        created_at = datetime.fromisoformat(issue.created_at)
    except ValueError as error:
        raise GitLabError("GitLab returned invalid issue creation time") from error
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise GitLabError("GitLab returned invalid issue creation time")
    return created_at


def _call_gitlab(
    *,
    host: str,
    glab_path: str,
    run_command: CommandRunner,
    endpoint: str,
    method: str = "GET",
    fields: tuple[str, ...] = (),
    allow_empty_response: bool = False,
) -> object:
    """Run one bounded numeric-project GitLab request and parse JSON."""
    try:
        result = run_command(
            (
                glab_path,
                "api",
                "--hostname",
                host,
                "-X",
                method,
                endpoint,
                *(part for field in fields for part in ("-f", field)),
            ),
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
    except CommandTimeoutError as error:
        raise GitLabError("GitLab API request timed out") from error
    if result.returncode != 0:
        raise GitLabError("GitLab API request failed")
    if allow_empty_response and not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GitLabError("GitLab returned invalid JSON") from error


def _merge_request(payload: object) -> MergeRequestEvidence:
    """Validate one GitLab merge-request payload."""
    if not isinstance(payload, dict):
        raise GitLabError("GitLab returned invalid merge request data")
    try:
        return MergeRequestEvidence.model_validate(
            _selected(cast(dict[str, object], payload), MR_FIELDS)
        )
    except ValidationError as error:
        raise GitLabError("GitLab returned invalid merge request data") from error


class GitLabGateway:
    """Use only the configured numeric GitLab project endpoint."""

    def __init__(self, config: AgentWorkConfig, run_command: CommandRunner) -> None:
        self._config = config
        self._run_command = run_command

    def _call(
        self, endpoint: str, *, method: str = "GET", fields: tuple[str, ...] = ()
    ) -> object:
        return _call_gitlab(
            host=self._config.host,
            glab_path=str(self._config.glab_path),
            run_command=self._run_command,
            endpoint=endpoint,
            method=method,
            fields=fields,
            allow_empty_response=method == "DELETE",
        )

    @staticmethod
    def _mapping(payload: object, name: str) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise GitLabError(f"GitLab returned invalid {name}")
        return cast(dict[str, object], payload)

    @classmethod
    def _items(cls, payload: object, name: str) -> tuple[dict[str, object], ...]:
        if not isinstance(payload, list):
            raise GitLabError(f"GitLab returned invalid {name}")
        return tuple(cls._mapping(item, name) for item in payload)

    def verify_project(self) -> GitLabProject:
        """Verify the configured numeric project identity."""
        payload = self._mapping(
            self._call(f"projects/{self._config.project_id}"), "project"
        )
        try:
            project = GitLabProject.model_validate(
                _selected(payload, PROJECT_FIELDS) | {"host": self._config.host}
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid project metadata") from error
        if project.id != self._config.project_id:
            raise GitLabError("GitLab returned a different project")
        return project

    def verify_authenticated_user(self) -> str:
        """Prove the configured service identity before unattended work."""
        user = self._mapping(self._call("user"), "authenticated user")
        if user.get("username") != self._config.factory_username:
            raise GitLabIdentityError(
                "GitLab authenticated identity does not match factory identity"
            )
        return self._config.factory_username

    def fetch_merge_request_feedback(
        self, iid: int
    ) -> tuple[MergeRequestFeedback, ...]:
        """Fetch general and inline comments, fail-closed on duplicate IDs."""
        notes: dict[int, MergeRequestFeedback] = {}
        for source, endpoint in (
            (
                "general",
                f"projects/{self._config.project_id}/merge_requests/{iid}/notes",
            ),
            (
                "discussion",
                f"projects/{self._config.project_id}/merge_requests/{iid}/discussions",
            ),
        ):
            for page in range(1, _MAX_NOTE_PAGES + 1):
                payload = self._items(
                    self._call(endpoint, fields=("per_page=100", f"page={page}")),
                    "merge request feedback",
                )
                if source == "discussion" and any(
                    not isinstance(item.get("id"), str)
                    or not cast(str, item["id"]).strip()
                    for item in payload
                ):
                    raise GitLabError("GitLab returned invalid merge request feedback")
                raw_notes = (
                    ((item, None) for item in payload)
                    if source == "general"
                    else (
                        (note, item.get("id"))
                        for item in payload
                        for note in self._items(item.get("notes"), "discussion notes")
                    )
                )
                for raw, discussion_id in raw_notes:
                    author = raw.get("author")
                    try:
                        entry = MergeRequestFeedback.model_validate(
                            _selected(raw, NOTE_FIELDS)
                            | {
                                "source": source,
                                "discussion_id": discussion_id,
                                "author": cast(dict[str, object], author).get(
                                    "username"
                                )
                                if isinstance(author, dict)
                                else None,
                            }
                        )
                    except ValidationError as error:
                        raise GitLabError(
                            "GitLab returned invalid merge request feedback"
                        ) from error
                    if entry.author is None:
                        raise GitLabError(
                            "GitLab returned invalid merge request feedback"
                        )
                    prior = notes.get(entry.id)
                    if prior is not None and prior != entry:
                        same_content = (
                            prior.body == entry.body
                            and prior.author == entry.author
                            and prior.created_at == entry.created_at
                            and prior.system == entry.system
                        )
                        if not (
                            same_content
                            and prior.source == "general"
                            and source == "discussion"
                        ):
                            raise GitLabError(
                                "GitLab returned conflicting merge request feedback"
                            )
                    notes[entry.id] = entry
                if len(payload) < 100:
                    break
            else:
                raise GitLabError(
                    "GitLab returned too many merge request feedback pages"
                )
        return tuple(
            sorted(notes.values(), key=lambda note: (note.created_at, note.id))
        )

    def comment_feedback_outcome(
        self,
        iid: int,
        *,
        issue_iid: int,
        source: str,
        note_id: int,
        discussion_id: str | None,
        selection_sha: str,
        summary: str,
    ) -> None:
        """Post one idempotent concise acknowledgement on the exact MR."""
        identity = json.dumps(
            {
                "project": self._config.project_id,
                "issue": issue_iid,
                "mr": iid,
                "source": source,
                "note": note_id,
                "discussion": discussion_id,
                "sha": selection_sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        marker = (
            "<!-- agent-work-feedback-outcome:v2 key="
            + hashlib.sha256(identity.encode()).hexdigest()
            + " -->"
        )
        endpoint = f"projects/{self._config.project_id}/merge_requests/{iid}/notes"
        for page in range(1, _MAX_NOTE_PAGES + 1):
            notes = self._items(
                self._call(endpoint, fields=("per_page=100", f"page={page}")),
                "merge request note list",
            )
            if any(
                marker in str(note.get("body", ""))
                and isinstance(note.get("author"), dict)
                and cast(dict[str, object], note["author"]).get("username")
                == self._config.factory_username
                for note in notes
            ):
                return
            if len(notes) < 100:
                break
        else:
            raise GitLabError("GitLab returned too many merge request note pages")
        self._call(
            endpoint,
            method="POST",
            fields=(f"body={marker}\n{summary[:2000]}",),
        )

    def _issues(
        self, *, state: str, labels: str | None = None
    ) -> tuple[GitLabIssue, ...]:
        fields = [f"state={state}", "order_by=created_at", "sort=asc", "per_page=100"]
        if labels is not None:
            fields.append(f"labels={labels}")
        try:
            items: list[dict[str, object]] = []
            for page in range(1, _MAX_ISSUE_PAGES + 1):
                page_items = self._items(
                    self._call(
                        f"projects/{self._config.project_id}/issues",
                        fields=(*fields, f"page={page}"),
                    ),
                    "issue list",
                )
                items.extend(page_items)
                if len(page_items) < 100:
                    break
            else:
                raise GitLabError("GitLab returned too many issue pages")
            return tuple(
                GitLabIssue.model_validate(_selected(item, ISSUE_FIELDS))
                for item in items
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid issue data") from error

    def list_eligible(self) -> tuple[GitLabIssue, ...]:
        """Return eligible issues by priority, absolute creation time, and IID."""
        inventory = self._issues(state="all")
        try:
            dependency_proofs = dict(
                prerequisite_dependency_proof(
                    inventory, project_id=self._config.project_id
                )
            )
        except ValueError as error:
            raise GitLabError("GitLab split dependency evidence is invalid") from error
        eligible = tuple(
            issue
            for issue in inventory
            if issue.state == "opened"
            and not issue.confidential
            and MANUAL_TRACKER_LABEL not in issue.labels
            and not set(issue.labels).intersection(LIFECYCLE_LABELS)
            and all(state == "closed" for _, state in dependency_proofs[issue.iid])
        )
        return tuple(
            sorted(
                eligible,
                key=lambda issue: (
                    _priority_rank(issue),
                    _creation_time(issue),
                    issue.iid,
                ),
            )
        )

    def list_projected(self) -> tuple[GitLabIssue, ...]:
        """Return issues carrying any lifecycle projection."""
        return tuple(
            issue
            for issue in self._issues(state="all")
            if set(issue.labels).intersection(LIFECYCLE_LABELS)
        )

    def list_closed_projected(self) -> tuple[GitLabIssue, ...]:
        """Return closed lifecycle projections in deterministic IID order."""
        return tuple(
            sorted(
                (
                    issue
                    for issue in self._issues(state="closed")
                    if set(issue.labels).intersection(LIFECYCLE_LABELS)
                ),
                key=lambda issue: issue.iid,
            )
        )

    def prerequisite_projection_inventory(self) -> tuple[GitLabIssue, ...]:
        """Return the complete bounded issue snapshot for label reconciliation."""
        return self._issues(state="all")

    def ensure_prerequisite_labels(self, labels: tuple[str, ...]) -> bool:
        """Create or normalize exact dynamic prerequisite label definitions."""
        endpoint = f"projects/{self._config.project_id}/labels"
        existing = self._label_inventory(endpoint)
        changed = False
        for name in sorted(
            labels, key=lambda label: prerequisite_label_iid(label) or 0
        ):
            current = existing.get(name)
            if current is None:
                self._call(
                    endpoint,
                    method="POST",
                    fields=(
                        f"name={name}",
                        f"color={PREREQUISITE_LABEL_COLOR}",
                        f"description={PREREQUISITE_LABEL_DESCRIPTION}",
                    ),
                )
                changed = True
            elif (
                current.get("color") != PREREQUISITE_LABEL_COLOR
                or current.get("description") != PREREQUISITE_LABEL_DESCRIPTION
            ):
                self._call(
                    f"{endpoint}/{quote(name, safe='')}",
                    method="PUT",
                    fields=(
                        f"color={PREREQUISITE_LABEL_COLOR}",
                        f"description={PREREQUISITE_LABEL_DESCRIPTION}",
                    ),
                )
                changed = True
        return changed

    def retire_legacy_prerequisite_labels(self) -> bool:
        """Delete exact legacy prerequisite definitions in numeric IID order."""
        endpoint = f"projects/{self._config.project_id}/labels"
        legacy_names = tuple(
            sorted(
                (
                    name
                    for name in self._label_inventory(endpoint)
                    if isinstance(name, str)
                    and legacy_prerequisite_label_iid(name) is not None
                ),
                key=lambda name: legacy_prerequisite_label_iid(name) or 0,
            )
        )
        for name in legacy_names:
            self._call(f"{endpoint}/{quote(name, safe='')}", method="DELETE")
        return bool(legacy_names)

    def transition_prerequisite_labels(
        self, iid: int, labels: tuple[str, ...]
    ) -> GitLabIssue:
        """Replace only dynamic prerequisite labels and verify the exact result."""
        issue = self.fetch_issue(iid)
        current = tuple(
            label for label in issue.labels if prerequisite_label_iid(label) is not None
        )
        removed = tuple(label for label in current if label not in labels)
        additions = tuple(label for label in labels if label not in current)
        fields = ((f"remove_labels={','.join(removed)}",) if removed else ()) + (
            (f"add_labels={','.join(additions)}",) if additions else ()
        )
        if not fields:
            return issue
        self._call(
            f"projects/{self._config.project_id}/issues/{iid}",
            method="PUT",
            fields=fields,
        )
        updated = self.fetch_issue(iid)
        expected = (
            *(label for label in issue.labels if prerequisite_label_iid(label) is None),
            *labels,
        )
        if frozenset(updated.labels) != frozenset(expected):
            raise GitLabError("GitLab did not apply the prerequisite projection")
        return updated

    def fetch_issue(self, iid: int) -> GitLabIssue:
        """Fetch one issue by IID."""
        try:
            payload = self._mapping(
                self._call(f"projects/{self._config.project_id}/issues/{iid}"), "issue"
            )
            return GitLabIssue.model_validate(_selected(payload, ISSUE_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid issue data") from error

    def fetch_issue_notes(self, iid: int) -> tuple[GitLabIssueNote, ...]:
        """Fetch all validated issue notes in deterministic chronological order."""
        notes_by_id: dict[int, GitLabIssueNote] = {}
        for page in range(1, _MAX_NOTE_PAGES + 1):
            payload = self._call(
                f"projects/{self._config.project_id}/issues/{iid}/notes",
                fields=(
                    "order_by=created_at",
                    "sort=asc",
                    "per_page=100",
                    f"page={page}",
                ),
            )
            try:
                items = self._items(payload, "issue note list")
                page_notes = tuple(
                    GitLabIssueNote.model_validate(
                        _selected(item, NOTE_FIELDS)
                        | {
                            "author": _selected(
                                cast(dict[str, object], author), NOTE_AUTHOR_FIELDS
                            )
                            if isinstance((author := item.get("author")), dict)
                            else author
                        }
                    )
                    for item in items
                )
            except (GitLabError, ValidationError) as error:
                raise GitLabError("GitLab returned invalid issue note data") from error
            for note in page_notes:
                existing = notes_by_id.get(note.id)
                if existing is not None and existing != note:
                    raise GitLabError("GitLab returned invalid issue note data")
                notes_by_id[note.id] = note
            if len(items) < 100:
                break
        else:
            raise GitLabError("GitLab returned too many issue note pages")
        return tuple(
            sorted(notes_by_id.values(), key=lambda note: (note.created_at, note.id))
        )

    def ensure_human_handoff(
        self, *, issue_iid: int, merge_request: MergeRequestEvidence
    ) -> bool:
        """Create one factory-authored actionable review handoff per exact SHA."""
        identity = json.dumps(
            {
                "issue": issue_iid,
                "mr": merge_request.iid,
                "project": self._config.project_id,
                "sha": merge_request.sha,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        marker = (
            "<!-- agent-work-human-handoff:v1 key="
            + hashlib.sha256(identity.encode()).hexdigest()
            + " -->"
        )
        for page in range(1, _MAX_NOTE_PAGES + 1):
            notes = self._items(
                self._call(
                    f"projects/{self._config.project_id}/issues/{issue_iid}/notes",
                    fields=("per_page=100", f"page={page}"),
                ),
                "issue note list",
            )
            if any(
                marker in str(note.get("body", ""))
                and isinstance(note.get("author"), dict)
                and cast(dict[str, object], note["author"]).get("username")
                == self._config.factory_username
                for note in notes
            ):
                return False
            if len(notes) < 100:
                break
        else:
            raise GitLabError("GitLab returned too many issue note pages")
        body = (
            f"{marker}\n"
            f"Human review is ready for MR !{merge_request.iid}: {merge_request.web_url}\n"
            f"Exact source commit: {merge_request.sha}\n\n"
            "Please review this merge request. If it is acceptable, approve and merge it; "
            "otherwise leave actionable general or inline feedback on that MR. "
            "Only humans approve or merge."
        )
        self._call(
            f"projects/{self._config.project_id}/issues/{issue_iid}/notes",
            method="POST",
            fields=(f"body={body}",),
        )
        return True

    def transition_labels(self, iid: int, label: str | None) -> GitLabIssue:
        """Project exactly one lifecycle label and verify the result."""
        removed_labels = tuple(
            lifecycle_label
            for lifecycle_label in LIFECYCLE_LABELS
            if lifecycle_label != label
        )
        fields: list[str] = [f"remove_labels={','.join(removed_labels)}"]
        if label is not None:
            fields.append(f"add_labels={label}")
        self._call(
            f"projects/{self._config.project_id}/issues/{iid}",
            method="PUT",
            fields=tuple(fields),
        )
        issue = self.fetch_issue(iid)
        if set(issue.labels).intersection(LIFECYCLE_LABELS) != (
            {label} if label else set()
        ):
            raise GitLabError("GitLab did not apply the lifecycle projection")
        return issue

    def ensure_lifecycle_labels(self) -> None:
        """Idempotently provision the fixed canary labels in this numeric project."""
        endpoint = f"projects/{self._config.project_id}/labels"
        existing = self._label_inventory(endpoint)
        for name, spec in CANARY_LABEL_SPECS.items():
            current = existing.get(name)
            if current is None:
                self._call(
                    endpoint,
                    method="POST",
                    fields=(
                        f"name={name}",
                        f"color={spec['color']}",
                        f"description={spec['description']}",
                    ),
                )
            elif (
                current.get("color") != spec["color"]
                or current.get("description") != spec["description"]
            ):
                self._call(
                    f"{endpoint}/{quote(name, safe='')}",
                    method="PUT",
                    fields=(
                        f"color={spec['color']}",
                        f"description={spec['description']}",
                    ),
                )

    def lifecycle_label_drift(self) -> tuple[str, ...]:
        """Return fixed canary labels missing or differing from their specification."""
        endpoint = f"projects/{self._config.project_id}/labels"
        existing = self._label_inventory(endpoint)
        return tuple(
            name
            for name, spec in CANARY_LABEL_SPECS.items()
            if existing.get(name) is None
            or existing[name].get("color") != spec["color"]
            or existing[name].get("description") != spec["description"]
        )

    def _label_inventory(self, endpoint: str) -> dict[object, dict[str, object]]:
        """Read the complete bounded label inventory for safe provisioning."""
        labels: dict[object, dict[str, object]] = {}
        for page in range(1, _MAX_LABEL_PAGES + 1):
            current = self._items(
                self._call(endpoint, fields=("per_page=100", f"page={page}")),
                "labels",
            )
            labels.update({item.get("name"): item for item in current})
            if len(current) < 100:
                return labels
        raise GitLabError("GitLab returned too many label pages")

    def fetch_merge_request(self, iid: int) -> MergeRequestEvidence:
        """Fetch exact MR evidence."""
        try:
            payload = self._mapping(
                self._call(f"projects/{self._config.project_id}/merge_requests/{iid}"),
                "merge request",
            )
            return MergeRequestEvidence.model_validate(_selected(payload, MR_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid merge request data") from error

    def merge_request_contains_commit(self, mr_iid: int, sha: str) -> bool:
        """Verify an exact SHA in the configured project's MR commit inventory."""
        if mr_iid <= 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitLabError("invalid merge request commit identity")
        endpoint = f"projects/{self._config.project_id}/merge_requests/{mr_iid}/commits"
        for page in range(1, _MAX_MERGE_REQUEST_PAGES + 1):
            commits = self._items(
                self._call(endpoint, fields=("per_page=100", f"page={page}")),
                "merge request commits",
            )
            ids = tuple(commit.get("id") for commit in commits)
            if any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in ids
            ):
                raise GitLabError(
                    "GitLab returned invalid merge request commit identity"
                )
            if sha in ids:
                return True
            if len(commits) < 100:
                return False
        raise GitLabError("GitLab returned too many merge request commit pages")

    def find_merge_request_by_branch(self, branch: str) -> MergeRequestEvidence | None:
        """Return the newest exact source-branch MR."""
        payload = self._call(
            f"projects/{self._config.project_id}/merge_requests",
            fields=("state=opened", f"source_branch={branch}", "per_page=20"),
        )
        try:
            candidates = tuple(
                MergeRequestEvidence.model_validate(_selected(item, MR_FIELDS))
                for item in self._items(payload, "merge request list")
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid merge request data") from error
        return next(
            (
                item
                for item in candidates
                if item.state == "opened" and item.source_branch == branch
            ),
            None,
        )

    def _open_merge_request_inventory(
        self, *, branch: str | None = None
    ) -> tuple[MergeRequestEvidence, ...]:
        """Return bounded validated opened-MR inventory, optionally by source branch."""
        try:
            items: list[dict[str, object]] = []
            for page in range(1, _MAX_MERGE_REQUEST_PAGES + 1):
                fields = ["state=opened", "per_page=100", f"page={page}"]
                if branch is not None:
                    fields.insert(1, f"source_branch={branch}")
                page_items = self._items(
                    self._call(
                        f"projects/{self._config.project_id}/merge_requests",
                        fields=tuple(fields),
                    ),
                    "merge request list",
                )
                items.extend(page_items)
                if len(page_items) < 100:
                    break
            else:
                raise GitLabError("GitLab returned too many merge request pages")
            return tuple(
                MergeRequestEvidence.model_validate(_selected(item, MR_FIELDS))
                for item in items
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid merge request data") from error

    def open_merge_requests_by_branch(
        self, branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        """Return every validated opened merge request for one source branch."""
        return self._open_merge_request_inventory(branch=branch)

    def create_merge_request(
        self,
        *,
        branch: str,
        target: str,
        issue_iid: int,
        draft: MergeRequestDraft,
    ) -> MergeRequestEvidence:
        """Create one same-project human-review merge request."""
        description = _desired_description(draft, issue_iid)
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/merge_requests",
                    method="POST",
                    fields=(
                        f"source_branch={branch}",
                        f"target_branch={target}",
                        f"title={draft.title}",
                        f"description={description}",
                        "remove_source_branch=true",
                        "squash=true",
                    ),
                ),
                "merge request",
            )
            return _require_exact_metadata(
                MergeRequestEvidence.model_validate(_selected(payload, MR_FIELDS)),
                title=draft.title,
                description=description,
                is_draft=draft.title.startswith("Draft:"),
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid merge request data") from error

    def reconcile_merge_request(
        self, mr: MergeRequestEvidence, *, issue_iid: int, draft: MergeRequestDraft
    ) -> MergeRequestEvidence:
        """Converge one existing MR on durable metadata without clearing Draft."""
        desired_description = _desired_description(draft, issue_iid)
        desired_draft = mr.is_draft or draft.title.startswith("Draft:")
        desired_title = (
            mr.title
            if mr.is_draft and not draft.title.startswith("Draft:")
            else draft.title
        )
        fields = tuple(
            part
            for part, changed in (
                (f"title={desired_title}", mr.title != desired_title),
                (
                    f"description={desired_description}",
                    mr.description != desired_description,
                ),
            )
            if changed
        )
        if not fields:
            return mr
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/merge_requests/{mr.iid}",
                    method="PUT",
                    fields=fields,
                ),
                "merge request",
            )
            returned = MergeRequestEvidence.model_validate(
                _selected(payload, MR_FIELDS)
            )
            return _require_exact_metadata(
                returned,
                title=desired_title,
                description=desired_description,
                is_draft=desired_draft,
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid merge request data") from error

    def ensure_closing_issue_reference(
        self, mr: MergeRequestEvidence, issue_iid: int
    ) -> MergeRequestEvidence:
        """Append one closing reference while preserving an existing MR description."""
        closing = f"Closes #{issue_iid}"
        if closing in mr.description:
            return mr
        payload = self._mapping(
            self._call(
                f"projects/{self._config.project_id}/merge_requests/{mr.iid}",
                method="PUT",
                fields=(f"description={mr.description.rstrip()}\n\n{closing}",),
            ),
            "merge request",
        )
        return MergeRequestEvidence.model_validate(_selected(payload, MR_FIELDS))

    def mr_closes_issue(self, mr_iid: int, issue_iid: int) -> bool:
        """Return whether GitLab reports the closing relationship."""
        return issue_iid in self.closing_issue_iids(mr_iid)

    def closing_issue_iids(self, mr_iid: int) -> frozenset[int]:
        """Read one validated closing-issue projection for this project."""
        closing_issues = self._items(
            self._call(
                f"projects/{self._config.project_id}/merge_requests/{mr_iid}/closes_issues"
            ),
            "closing issue list",
        )
        for item in closing_issues:
            iid = item.get("iid")
            project_id = item.get("project_id")
            if not (
                type(iid) is int
                and iid > 0
                and type(project_id) is int
                and project_id > 0
            ):
                raise GitLabError("GitLab returned invalid closing issue projection")
        return frozenset(
            cast(int, item["iid"])
            for item in closing_issues
            if item["project_id"] == self._config.project_id
        )

    def latest_pipeline_for_sha(self, mr_iid: int, sha: str) -> PipelineEvidence | None:
        """Return the newest MR pipeline matching an exact source SHA."""
        payload = self._call(
            f"projects/{self._config.project_id}/merge_requests/{mr_iid}/pipelines",
            fields=("per_page=100",),
        )
        try:
            pipelines = tuple(
                PipelineEvidence.model_validate(_selected(item, PIPELINE_FIELDS))
                for item in self._items(payload, "pipeline list")
                if item.get("sha") == sha
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid pipeline data") from error
        return pipelines[0] if pipelines else None

    def latest_pipeline_for_ref(self, ref: str) -> PipelineEvidence | None:
        """Return the newest validated pipeline for one configured-project ref."""
        payload = self._call(
            f"projects/{self._config.project_id}/pipelines",
            fields=(f"ref={ref}", "per_page=100"),
        )
        try:
            pipelines = tuple(
                PipelineEvidence.model_validate(_selected(item, PIPELINE_FIELDS))
                for item in self._items(payload, "pipeline list")
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid pipeline data") from error
        return pipelines[0] if pipelines else None

    def list_pipeline_jobs(
        self, pipeline_id: int, source_sha: str
    ) -> tuple[PipelineJobEvidence, ...]:
        """Return one bounded validated inventory for an exact pipeline."""
        try:
            raw_jobs: list[dict[str, object]] = []
            for page in range(1, _MAX_PIPELINE_JOB_PAGES + 1):
                batch = self._items(
                    self._call(
                        f"projects/{self._config.project_id}/pipelines/{pipeline_id}/jobs",
                        fields=("per_page=100", "include_retried=true", f"page={page}"),
                    ),
                    "pipeline job list",
                )
                if any("allow_failure" not in item for item in batch):
                    raise GitLabError(
                        "GitLab pipeline job inventory lacks failure policy"
                    )
                raw_jobs.extend(batch)
                if len(batch) < 100:
                    break
            else:
                raise GitLabError("GitLab pipeline job inventory exceeded the bound")
            jobs = tuple(
                PipelineJobEvidence.model_validate(
                    {
                        key: item[key]
                        for key in PIPELINE_JOB_FIELDS
                        if key != "pipeline" and key in item
                    }
                    | {
                        "pipeline_id": self._mapping(
                            item.get("pipeline"), "job pipeline"
                        )["id"],
                        "pipeline_sha": self._mapping(
                            item.get("pipeline"), "job pipeline"
                        )["sha"],
                    }
                )
                for item in raw_jobs
            )
        except (KeyError, ValidationError) as error:
            raise GitLabError("GitLab returned invalid pipeline job data") from error
        if len({job.id for job in jobs}) != len(jobs):
            raise GitLabError("GitLab returned duplicate pipeline job identities")
        if any(
            job.pipeline_id != pipeline_id or job.pipeline_sha != source_sha
            for job in jobs
        ):
            raise GitLabError("GitLab returned jobs from a different exact pipeline")
        return jobs

    def fetch_job_trace(self, pipeline_id: int, job_id: int) -> JobTraceEvidence:
        """Fetch a selected job trace only after its pipeline identity is validated."""
        if pipeline_id <= 0 or job_id <= 0:
            raise GitLabError("GitLab job trace identity is invalid")
        try:
            result = self._run_command(
                (
                    str(self._config.glab_path),
                    "api",
                    "--hostname",
                    self._config.host,
                    "-X",
                    "GET",
                    f"projects/{self._config.project_id}/jobs/{job_id}/trace",
                ),
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            )
        except CommandTimeoutError as error:
            raise GitLabError("GitLab job trace request timed out") from error
        if result.returncode != 0 or not result.stdout.strip():
            raise GitLabError("GitLab returned an empty job trace")
        try:
            return JobTraceEvidence(text=result.stdout)
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid job trace") from error

    def open_merge_requests(self) -> tuple[MergeRequestEvidence, ...]:
        """Return every opened merge request from bounded configured-project pages."""
        return self._open_merge_request_inventory()

    def comment_outcome(
        self, iid: int, *, error_code: str, summary: str, next_action: str
    ) -> None:
        """Record one safe actionable outcome on its issue."""
        body = (
            f"{AGENT_OUTCOME_NOTE_MARKER}\n"
            f"Agent outcome `{error_code}`: {summary}\n\nNext action: {next_action}"
        )
        self._call(
            f"projects/{self._config.project_id}/issues/{iid}/notes",
            method="POST",
            fields=(f"body={body}",),
        )

    def upsert_outcome_note(
        self,
        iid: int,
        *,
        outcome_key: str,
        outcome: str,
        explanation: str,
        evidence: str,
        retained_work: str,
        next_action: str,
        human_decision: str,
    ) -> None:
        """Create or update one factory-authored structured issue outcome note."""
        if (
            isinstance(self._config.project_id, bool)
            or not isinstance(self._config.project_id, int)
            or self._config.project_id <= 0
            or isinstance(iid, bool)
            or not isinstance(iid, int)
            or iid <= 0
            or not isinstance(outcome_key, str)
            or not outcome_key
            or len(outcome_key.encode()) > 512
        ):
            raise GitLabError("outcome note identity is invalid")
        values = (
            outcome,
            explanation,
            evidence,
            retained_work,
            next_action,
            human_decision,
        )
        if not all(isinstance(value, str) for value in values):
            raise GitLabError("outcome note content is invalid")
        identity = json.dumps(
            {
                "issue": iid,
                "outcome": outcome_key,
                "project": self._config.project_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        marker = (
            "<!-- agent-work-outcome:v2 key="
            + hashlib.sha256(identity.encode()).hexdigest()
            + " -->"
        )
        safe_values = tuple(sanitize_outcome_note_field(value) for value in values)
        body = "\n\n".join(
            (
                marker,
                f"## Outcome\n{safe_values[0]}",
                f"## What happened\n{safe_values[1]}",
                f"## Evidence\n{safe_values[2]}",
                f"## Retained work\n{safe_values[3]}",
                f"## Next action\n{safe_values[4]}",
                f"## Human decision\n{safe_values[5]}",
            )
        )
        endpoint = f"projects/{self._config.project_id}/issues/{iid}/notes"
        target_note_id: int | None = None
        for page in range(1, _MAX_NOTE_PAGES + 1):
            notes = self._items(
                self._call(endpoint, fields=("per_page=100", f"page={page}")),
                "issue note list",
            )
            for note in notes:
                author = note.get("author")
                if (
                    not isinstance(author, dict)
                    or cast("dict[str, object]", author).get("username")
                    != self._config.factory_username
                ):
                    continue
                note_body = note.get("body")
                if (
                    not isinstance(note_body, str)
                    or _OUTCOME_NOTE_NAMESPACE not in note_body
                ):
                    continue
                markers = _OUTCOME_NOTE_MARKER.findall(note_body)
                if note_body.count(_OUTCOME_NOTE_NAMESPACE) != 1 or len(markers) != 1:
                    raise GitLabError("outcome note marker is malformed")
                if markers[0] != marker:
                    continue
                note_id = note.get("id")
                if (
                    isinstance(note_id, bool)
                    or not isinstance(note_id, int)
                    or note_id <= 0
                ):
                    raise GitLabError("outcome note target ID is invalid")
                if target_note_id is not None:
                    raise GitLabError("outcome note target is duplicate")
                target_note_id = note_id
            if len(notes) < 100:
                break
        else:
            raise GitLabError("GitLab returned too many issue note pages")
        if target_note_id is None:
            self._call(endpoint, method="POST", fields=(f"body={body}",))
            return
        self._call(
            f"{endpoint}/{target_note_id}", method="PUT", fields=(f"body={body}",)
        )

    def split_child_inventory(self) -> tuple[GitLabIssue, ...]:
        """Return one bounded inventory used for a decomposition segment."""
        return tuple(
            issue
            for issue in self._issues(state="all")
            if "<!-- agent-work-split:v1" in issue.description
        )

    def recovery_child_inventory(self) -> tuple[GitLabIssue, ...]:
        """Return all recovery children, including closed ones, for adoption."""
        return tuple(
            issue
            for issue in self._issues(state="all")
            if "<!-- agent-work-recovery-child:v1" in issue.description
        )

    def create_recovery_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue:
        """Create one marker-bound recovery issue and read it back exactly."""
        reject_unsafe_issue_text(title)
        reject_unsafe_issue_text(description)
        reject_unsafe_issue_text(marker)
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/issues",
                    method="POST",
                    fields=(f"title={title}", f"description={description}"),
                ),
                "recovery child issue",
            )
            issue = GitLabIssue.model_validate(_selected(payload, ISSUE_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid recovery child issue") from error
        if (
            issue.title != title
            or issue.description != description
            or marker not in issue.description
            or issue.state != "opened"
            or issue.labels
            or issue.confidential
            or "confidential" not in issue.model_fields_set
        ):
            raise GitLabError("GitLab recovery child evidence is not exact")
        read_back = self.fetch_issue(issue.iid)
        if (
            read_back.iid != issue.iid
            or read_back.title != title
            or read_back.description != description
            or read_back.state != "opened"
            or read_back.labels
            or read_back.confidential
            or "confidential" not in read_back.model_fields_set
        ):
            raise GitLabError("GitLab recovery child evidence is not exact")
        return read_back

    def create_split_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue:
        """Create and verify one marker-bound child issue."""
        reject_unsafe_issue_text(title)
        reject_unsafe_issue_text(description)
        reject_unsafe_issue_text(marker)
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/issues",
                    method="POST",
                    fields=(
                        f"title={title}",
                        f"description={description}",
                    ),
                ),
                "split child issue",
            )
            issue = GitLabIssue.model_validate(_selected(payload, ISSUE_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid split child issue") from error
        if issue.title != title or marker not in issue.description:
            raise GitLabError("GitLab split child evidence is not exact")
        read_back = self.fetch_issue(issue.iid)
        if (
            read_back.iid != issue.iid
            or read_back.title != title
            or read_back.description != description
            or read_back.state != "opened"
        ):
            raise GitLabError("GitLab split child evidence is not exact")
        return read_back

    def ensure_decomposition_note(self, iid: int, body: str) -> None:
        """Create at most one exact parent decomposition note."""
        if any(note.body == body for note in self.fetch_issue_notes(iid)):
            return
        self._call(
            f"projects/{self._config.project_id}/issues/{iid}/notes",
            method="POST",
            fields=(f"body={body}",),
        )

    def close_issue(self, iid: int) -> GitLabIssue:
        """Close one parent issue and return exact resulting evidence."""
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/issues/{iid}",
                    method="PUT",
                    fields=("state_event=close",),
                ),
                "closed issue",
            )
            issue = GitLabIssue.model_validate(_selected(payload, ISSUE_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid closed issue") from error
        if issue.iid != iid or issue.state != "closed":
            raise GitLabError("GitLab did not close split parent")
        return issue

    def reopen_issue(self, iid: int) -> GitLabIssue:
        """Reopen one reviewed issue and return exact resulting evidence."""
        try:
            payload = self._mapping(
                self._call(
                    f"projects/{self._config.project_id}/issues/{iid}",
                    method="PUT",
                    fields=(
                        "state_event=reopen",
                        f"remove_labels={','.join(LIFECYCLE_LABELS)}",
                    ),
                ),
                "reopened issue",
            )
            issue = GitLabIssue.model_validate(_selected(payload, ISSUE_FIELDS))
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid reopened issue") from error
        if (
            issue.iid != iid
            or issue.state != "opened"
            or set(issue.labels).intersection(LIFECYCLE_LABELS)
        ):
            raise GitLabError("GitLab did not reopen issue")
        return issue
