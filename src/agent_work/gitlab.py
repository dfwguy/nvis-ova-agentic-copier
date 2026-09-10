"""Numeric-project GitLab adapter for recoverable agent work."""

from __future__ import annotations

import json
from typing import cast

from pydantic import ValidationError

from .domain import LIFECYCLE_LABELS
from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabProject,
    MergeRequestDraft,
    MergeRequestEvidence,
    PipelineEvidence,
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
)
PIPELINE_FIELDS = ("id", "status", "sha", "web_url")


def _selected(payload: dict[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Select repository-owned fields from a larger GitLab response."""
    return {field: payload[field] for field in fields if field in payload}


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
            page = 1
            while True:
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
                page += 1
            return tuple(
                GitLabIssue.model_validate(_selected(item, ISSUE_FIELDS))
                for item in items
            )
        except ValidationError as error:
            raise GitLabError("GitLab returned invalid issue data") from error

    def list_ready(self) -> tuple[GitLabIssue, ...]:
        """Return only explicitly opted-in issues, oldest first."""
        return tuple(
            issue
            for issue in self._issues(state="opened", labels="agent::ready")
            if issue.state == "opened"
            and not issue.confidential
            and set(issue.labels).intersection(LIFECYCLE_LABELS) == {"agent::ready"}
        )

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

    def create_merge_request(
        self,
        *,
        branch: str,
        target: str,
        issue_iid: int,
        draft: MergeRequestDraft,
    ) -> MergeRequestEvidence:
        """Create one same-project human-review merge request."""
        description = f"{draft.description.rstrip()}\n\nCloses #{issue_iid}"
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
            return MergeRequestEvidence.model_validate(_selected(payload, MR_FIELDS))
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
        body = f"Agent outcome `{error_code}`: {summary}\n\nNext action: {next_action}"
        self._call(
            f"projects/{self._config.project_id}/issues/{iid}/notes",
            method="POST",
            fields=(f"body={body}",),
        )
