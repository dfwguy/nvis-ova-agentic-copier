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

import json
import re
from datetime import datetime
from typing import cast

from pydantic import ValidationError

from .domain import AGENT_OUTCOME_NOTE_MARKER, LIFECYCLE_LABELS, parse_split_marker
from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabIssueNote,
    GitLabProject,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
    render_decision_review_notice,
)
from .ports import CommandRunner


class GitLabError(RuntimeError):
    """A GitLab boundary failed without exposing command output."""


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
NOTE_FIELDS = ("id", "body", "author", "created_at", "system")
NOTE_AUTHOR_FIELDS = ("username",)
_MAX_NOTE_PAGES = 100
_MAX_ISSUE_PAGES = 100
_MAX_MERGE_REQUEST_PAGES = 100
_HIGH_PRIORITY_LABEL = "agent-priority::high"
_LOW_PRIORITY_LABEL = "agent-priority::low"


def _selected(payload: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Select repository-owned fields from a larger GitLab response."""
    return {field: payload[field] for field in fields if field in payload}


def _desired_description(draft: MergeRequestDraft, issue_iid: int) -> str:
    """Build one canonical description with exactly one closing reference."""
    body = (
        render_decision_review_notice(draft.decision_review, draft.description)
        if draft.decision_review is not None
        else draft.description.rstrip()
    )
    closing = f"Closes #{issue_iid}"
    pattern = re.compile(rf"^\s*closes\s*#\s*{issue_iid}\s*$", re.IGNORECASE)
    retained = "\n".join(line for line in body.splitlines() if not pattern.match(line))
    return f"{retained.rstrip()}\n\n{closing}"


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


class GitLabGateway:
    """Use only the configured numeric GitLab project endpoint."""

    def __init__(self, config: AgentWorkConfig, run_command: CommandRunner) -> None:
        self._config = config
        self._run_command = run_command

    def _call(
        self, endpoint: str, *, method: str = "GET", fields: tuple[str, ...] = ()
    ) -> object:
        command = (
            "glab",
            "api",
            "--hostname",
            self._config.host,
            "-X",
            method,
            endpoint,
            *(part for field in fields for part in ("-f", field)),
        )
        result = self._run_command(command)
        if result.returncode != 0:
            raise GitLabError("GitLab API request failed")
        try:
            return cast(object, json.loads(result.stdout))
        except json.JSONDecodeError as error:
            raise GitLabError("GitLab returned invalid JSON") from error

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
        issues = self._issues(state="opened")
        dependency_inventory = (
            self._issues(state="all")
            if any("<!-- agent-work-split:v1" in issue.description for issue in issues)
            else issues
        )
        by_iid = {issue.iid: issue for issue in dependency_inventory}
        eligible = tuple(
            issue
            for issue in issues
            if issue.state == "opened"
            and not issue.confidential
            and not set(issue.labels).intersection(LIFECYCLE_LABELS)
            and self._split_dependencies_closed(issue, by_iid)
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

    def _split_dependencies_closed(
        self, issue: GitLabIssue, by_iid: dict[int, GitLabIssue]
    ) -> bool:
        """Validate split provenance and defer children with open prerequisites."""
        try:
            parsed = parse_split_marker(issue.description, self._config.project_id)
        except ValueError as error:
            raise GitLabError("GitLab split marker is invalid") from error
        if parsed is None:
            return True
        parent, _key, dependency_iids = parsed
        if parent == issue.iid:
            raise GitLabError("GitLab split marker is invalid")
        if issue.iid in dependency_iids:
            raise GitLabError("GitLab split marker is invalid")
        try:
            dependency_issues = tuple(by_iid[value] for value in dependency_iids)
        except KeyError as error:
            raise GitLabError("GitLab split dependency is missing") from error
        return all(dependency.state == "closed" for dependency in dependency_issues)

    def list_projected(self) -> tuple[GitLabIssue, ...]:
        """Return issues carrying any lifecycle projection."""
        return tuple(
            issue
            for issue in self._issues(state="all")
            if set(issue.labels).intersection(LIFECYCLE_LABELS)
        )

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

    def open_merge_requests_by_branch(
        self, branch: str
    ) -> tuple[MergeRequestEvidence, ...]:
        """Return every validated opened merge request for one source branch."""
        try:
            items: list[dict[str, object]] = []
            for page in range(1, _MAX_MERGE_REQUEST_PAGES + 1):
                page_items = self._items(
                    self._call(
                        f"projects/{self._config.project_id}/merge_requests",
                        fields=(
                            "state=opened",
                            f"source_branch={branch}",
                            "per_page=100",
                            f"page={page}",
                        ),
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
        return any(
            item.get("iid") == issue_iid
            and item.get("project_id") == self._config.project_id
            for item in self._items(
                self._call(
                    f"projects/{self._config.project_id}/merge_requests/{mr_iid}/closes_issues"
                ),
                "closing issue list",
            )
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

    def split_child_inventory(self) -> tuple[GitLabIssue, ...]:
        """Return one bounded inventory used for a decomposition segment."""
        return tuple(
            issue
            for issue in self._issues(state="all")
            if "<!-- agent-work-split:v1" in issue.description
        )

    def create_split_child(
        self, *, title: str, description: str, marker: str
    ) -> GitLabIssue:
        """Create and verify one marker-bound child issue."""
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
        return issue

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
