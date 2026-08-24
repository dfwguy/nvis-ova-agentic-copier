"""Validated boundary models and immutable internal work-item values."""

import hashlib
import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GitLabConfig(BaseModel):
    """Non-secret GitLab configuration supplied by the environment."""

    model_config = ConfigDict(frozen=True)

    host: str
    project_id: int = Field(gt=0)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Require a bare, non-empty GitLab hostname."""
        if not value or "://" in value or "/" in value:
            raise ValueError("host must be a bare hostname")
        return value


class GitLabIssue(BaseModel):
    """The validated subset of a GitLab issue used by Phase 1."""

    model_config = ConfigDict(frozen=True)

    iid: int = Field(gt=0)
    title: str = Field(min_length=1)
    description: str = ""
    labels: tuple[str, ...] = ()
    confidential: bool = Field(default=False, strict=True)
    updated_at: datetime | str | None = None
    state: str = Field(default="opened", min_length=1, strict=True)

    @field_validator("labels", mode="before")
    @classmethod
    def as_tuple(cls, value: object) -> tuple[str, ...]:
        """Normalize GitLab's label list into an immutable tuple."""
        if isinstance(value, (list, tuple)) and all(
            isinstance(label, str) for label in value
        ):
            return tuple(label for label in value if isinstance(label, str))
        raise ValueError("labels must be strings")

    @field_validator("description", mode="before")
    @classmethod
    def normalize_null_description(cls, value: object) -> str:
        """Accept GitLab's explicit null description without coercing other values."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        raise ValueError("description must be a string or null")

    def __setattr__(self, name: str, value: object) -> None:
        """Expose frozen-boundary mutation as a standard dataclass-style error."""
        if name in self.__class__.model_fields and name in self.__dict__:
            raise FrozenInstanceError("cannot assign to field '" + name + "'")
        super().__setattr__(name, value)


class GitLabProject(BaseModel):
    """Validated immutable configured-project metadata from GitLab."""

    model_config = ConfigDict(frozen=True, strict=True)

    id: int = Field(gt=0)
    default_branch: str = Field(min_length=1)
    http_url_to_repo: str = Field(min_length=1)
    ssh_url_to_repo: str = Field(min_length=1)


class GitLabPipeline(BaseModel):
    """Validated immutable pipeline item from GitLab."""

    model_config = ConfigDict(frozen=True, strict=True)

    status: str = Field(min_length=1)


class GitLabMergeRequest(BaseModel):
    """Validated immutable merge-request proof from GitLab."""

    model_config = ConfigDict(frozen=True, strict=True)

    web_url: str | None = Field(default=None, min_length=1)
    source_branch: str = Field(min_length=1)
    target_branch: str = Field(min_length=1)
    source_project_id: int = Field(gt=0)
    target_project_id: int = Field(gt=0)
    sha: str = Field(min_length=1)
    state: str = Field(min_length=1)


@dataclass(frozen=True)
class WorkItem:
    """A normalized internal unit of bounded issue work."""

    schema_version: int
    work_item_id: str
    run_id: str
    source_type: Literal["gitlab"]
    source_id: str
    source_revision: str
    repository: str
    title: str
    body: str
    acceptance_criteria: tuple[str, ...]
    deduplication_key: str
    status: Literal["discovered"]
    mode: Literal["inspect", "publish"]
    attempt_count: int
    created_at: str
    updated_at: str
    branch: str
    issue: GitLabIssue
    task_content_digest: str

    @classmethod
    def from_issue(
        cls,
        *,
        config: GitLabConfig,
        issue: GitLabIssue,
        run_id: str,
        mode: Literal["inspect", "publish"],
    ) -> "WorkItem":
        """Create the deterministic Phase 1 representation of an issue."""
        revision = str(issue.updated_at or "")
        work_item_id = f"gitlab:{config.project_id}:{issue.iid}"
        task_labels = sorted(
            label for label in issue.labels if not label.startswith("agent::")
        )
        digest_input = json.dumps(
            {
                "description": issue.description,
                "labels": task_labels,
                "title": issue.title,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return cls(
            schema_version=1,
            work_item_id=work_item_id,
            run_id=run_id,
            source_type="gitlab",
            source_id=str(issue.iid),
            source_revision=revision,
            repository=f"gitlab:{config.host}:{config.project_id}",
            title=issue.title,
            body=issue.description,
            acceptance_criteria=(),
            deduplication_key=f"{work_item_id}:{revision}",
            status="discovered",
            mode=mode,
            attempt_count=1,
            created_at=revision,
            updated_at=revision,
            branch=f"agent/issue-{issue.iid}",
            issue=issue,
            task_content_digest=hashlib.sha256(digest_input).hexdigest(),
        )
