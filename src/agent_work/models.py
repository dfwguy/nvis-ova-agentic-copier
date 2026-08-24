"""Validated boundary models and immutable internal work-item values."""

import hashlib
import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_alias(value: str) -> str:
    """Validate one portable, bounded producer or repository alias."""
    if (
        not value
        or len(value) > 128
        or not all(
            character.isascii() and (character.isalnum() or character in "._-")
            for character in value
        )
    ):
        raise ValueError("alias contains unsafe characters")
    return value


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


class FilesystemEnvelope(BaseModel):
    """The deliberately small, frozen v1 producer file contract."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: Literal[1]
    source_id: str = Field(min_length=1, max_length=128)
    repository: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(max_length=65_536)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> int | float:
        """Accept JSON numeric one while rejecting Boolean and nonnumeric lookalikes."""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 1:
            raise ValueError("schema_version must be the numeric value 1")
        return value

    @field_validator("source_id", "repository")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        """Allow only portable producer and repository aliases."""
        return validate_alias(value)

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def criteria_as_tuple(cls, value: object) -> tuple[str, ...]:
        """Accept JSON arrays while retaining immutable internal values."""
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError("acceptance criteria must be strings")
        return tuple(cast(str, item) for item in value)

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Require unique, bounded non-empty acceptance criteria."""
        if any(not criterion or len(criterion) > 2048 for criterion in value):
            raise ValueError("acceptance criteria must be bounded non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("acceptance criteria must be unique")
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
    source_type: Literal["gitlab", "filesystem"]
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
    issue: GitLabIssue | None
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

    @classmethod
    def from_file(
        cls,
        *,
        envelope: FilesystemEnvelope,
        content_sha256: str,
        run_id: str,
        mode: Literal["inspect", "publish"],
    ) -> "WorkItem":
        """Create a source-neutral work item from exact producer bytes."""
        if len(content_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in content_sha256
        ):
            raise ValueError("content_sha256 must be a lowercase SHA-256 digest")
        return cls(
            schema_version=1,
            work_item_id=f"filesystem:{content_sha256}",
            run_id=run_id,
            source_type="filesystem",
            source_id=envelope.source_id,
            source_revision=content_sha256,
            repository=envelope.repository,
            title=envelope.title,
            body=envelope.body,
            acceptance_criteria=envelope.acceptance_criteria,
            deduplication_key=content_sha256,
            status="discovered",
            mode=mode,
            attempt_count=1,
            created_at=content_sha256,
            updated_at=content_sha256,
            branch=f"agent/file-{content_sha256[:16]}",
            issue=None,
            task_content_digest=content_sha256,
        )
