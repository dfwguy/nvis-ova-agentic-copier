"""Validated GitLab boundaries and small immutable runner values."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

PROJECT_LOCK_FILENAME = ".agent-work.lock"


class AgentWorkConfig(BaseModel):
    """Non-secret runner configuration supplied by the environment."""

    model_config = ConfigDict(frozen=True)

    host: str
    project_id: int = Field(gt=0)
    checkout: Path
    journal_tag: str = Field(default="agent-work", min_length=1)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Require one bare GitLab hostname."""
        if not value or "://" in value or "/" in value:
            raise ValueError("GITLAB_HOST must be a bare hostname")
        return value

    @field_validator("checkout")
    @classmethod
    def validate_absolute_path(cls, value: Path) -> Path:
        """Keep the service checkout target explicit."""
        if not value.is_absolute():
            raise ValueError("runner paths must be absolute")
        return value

    @property
    def lock_file(self) -> Path:
        """Use one fixed, checkout-scoped lock shared by every local runner."""
        return self.checkout / PROJECT_LOCK_FILENAME


class GitLabIssue(BaseModel):
    """The validated subset of a GitLab issue used by the runner."""

    model_config = ConfigDict(frozen=True)

    iid: int = Field(gt=0)
    title: str = Field(min_length=1)
    description: str = Field(default="", strict=True)
    labels: tuple[str, ...] = ()
    state: str = Field(default="opened", min_length=1)
    confidential: bool = False
    created_at: str = ""

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, value: object) -> object:
        """Accept GitLab's nullable issue description."""
        if value is None:
            return ""
        return value

    @field_validator("labels", mode="before")
    @classmethod
    def normalize_labels(cls, value: object) -> tuple[str, ...]:
        """Normalize GitLab's label array to an immutable tuple."""
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(label, str) for label in value
        ):
            raise ValueError("issue labels must be strings")
        return tuple(label for label in value if isinstance(label, str))


class GitLabProject(BaseModel):
    """Configured-project metadata used to bind every operation."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(gt=0)
    host: str
    default_branch: str = Field(min_length=1)
    web_url: str = Field(min_length=1)
    http_url_to_repo: str = ""
    ssh_url_to_repo: str = ""


class PipelineEvidence(BaseModel):
    """The latest default-branch pipeline state needed for remediation."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(gt=0)
    status: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    web_url: str = Field(min_length=1)

    @property
    def needs_remediation(self) -> bool:
        """Return whether this terminal pipeline state requires a repair."""
        return self.status in {"failed", "canceled"}


class MergeRequestEvidence(BaseModel):
    """The GitLab fields needed to verify one publication."""

    model_config = ConfigDict(frozen=True)

    iid: int = Field(gt=0)
    web_url: str = Field(min_length=1)
    state: Literal["opened", "closed", "merged", "locked"]
    source_project_id: int = Field(gt=0)
    target_project_id: int = Field(gt=0)
    source_branch: str = Field(min_length=1)
    target_branch: str = Field(min_length=1)
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    has_conflicts: bool = False


class MergeRequestDraft(BaseModel):
    """Worker-supplied metadata for trusted merge-request publication."""

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=20_000)


@dataclass(frozen=True)
class GitPublicationEvidence:
    """Local and remote tips for one deterministic issue branch."""

    local_sha: str
    remote_sha: str


@dataclass(frozen=True)
class AgentInvocation:
    """Result and redacted diagnostic output from one agent invocation."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""

    @property
    def diagnostic(self) -> str:
        """Return the available redacted output in command-stream order."""
        return "\n".join(part for part in (self.stdout, self.stderr) if part)

    def merge_request_draft(self) -> MergeRequestDraft:
        """Parse the sole delimited JSON draft emitted by the worker."""
        matches = re.findall(
            r"<agent-work-merge-request>\s*(.*?)\s*</agent-work-merge-request>",
            self.stdout,
            flags=re.DOTALL,
        )
        if len(matches) != 1:
            raise ValueError("worker did not return one merge-request draft")
        try:
            return MergeRequestDraft.model_validate(json.loads(matches[0]))
        except (json.JSONDecodeError, ValidationError) as error:
            raise ValueError(
                "worker returned an invalid merge-request draft"
            ) from error


@dataclass(frozen=True)
class RunReport:
    """Safe, non-persistent output from one bounded command."""

    outcome: Literal[
        "idle",
        "preview_ready",
        "preview_running",
        "awaiting_human",
        "failed",
    ]
    issue_iid: int | None = None
    branch: str | None = None
    merge_request_url: str | None = None
    failure_reason: str | None = None
    agent_diagnostic: str | None = None
