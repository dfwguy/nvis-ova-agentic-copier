"""Concrete GitLab, Git, Codex, environment, and lock boundaries."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol, cast
from urllib.parse import urlparse

from pydantic import ValidationError

from .models import (
    AgentWorkConfig,
    GitLabIssue,
    GitLabProject,
    GitPublicationEvidence,
    MergeRequestEvidence,
)
from .serial import RunNextDependencies


class AdapterError(RuntimeError):
    """A safe boundary failure that omits child-process output."""


class LockBusy(AdapterError):
    """Another local process owns the project runner lock."""


@dataclass(frozen=True)
class CommandResult:
    """The captured result of one argv subprocess call."""

    returncode: int
    stdout: str
    stderr: str


def run_subprocess(
    command: tuple[str, ...],
    *,
    stdin: str | None = None,
    timeout_seconds: int | None = None,
) -> CommandResult:
    """Run one command without a shell and retain output at the boundary."""
    try:
        completed = subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AdapterError("a required command could not be completed") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _repository_location(origin: str) -> tuple[str, str]:
    """Return the GitLab hostname and namespace path from one origin URL."""
    normalized = origin.strip().removesuffix(".git")
    parsed = urlparse(normalized)
    if parsed.hostname and parsed.path.strip("/"):
        return parsed.hostname, parsed.path.strip("/")
    scp_style = re.fullmatch(r"(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)", normalized)
    if scp_style is not None:
        return scp_style["host"], scp_style["path"].strip("/")
    raise AdapterError("checkout origin is not a usable GitLab repository URL")


def _confirmed_command(
    run_command: CommandRunner, command: tuple[str, ...], failure: str
) -> str:
    """Run a non-secret confirmation and name its failed boundary."""
    result = run_command(command)
    if result.returncode != 0:
        raise AdapterError(failure)
    return result.stdout.strip()


def _discover_project(
    *, host: str, path: str, run_command: CommandRunner
) -> tuple[int, dict[str, object]]:
    """Resolve an exact repository path, then confirm it by numeric project ID."""
    name = path.rsplit("/", maxsplit=1)[-1]
    search_payload = _confirmed_command(
        run_command,
        (
            "glab",
            "api",
            "--hostname",
            host,
            "-X",
            "GET",
            f"projects?search={name}&simple=true&per_page=100",
        ),
        "GitLab project lookup failed",
    )
    try:
        candidates = json.loads(search_payload)
    except json.JSONDecodeError as error:
        raise AdapterError("GitLab returned invalid project lookup data") from error
    if not isinstance(candidates, list):
        raise AdapterError("GitLab returned invalid project lookup data")
    candidate = next(
        (
            item
            for item in candidates
            if isinstance(item, dict) and item.get("path_with_namespace") == path
        ),
        None,
    )
    if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), int):
        raise AdapterError("checkout origin project is not accessible through GitLab")
    project_id = candidate["id"]
    numeric_payload = _confirmed_command(
        run_command,
        ("glab", "api", "--hostname", host, "-X", "GET", f"projects/{project_id}"),
        "GitLab numeric project confirmation failed",
    )
    try:
        project = json.loads(numeric_payload)
    except json.JSONDecodeError as error:
        raise AdapterError("GitLab returned invalid project metadata") from error
    if not isinstance(project, dict) or project.get("id") != project_id:
        raise AdapterError("GitLab numeric project confirmation did not match origin")
    return project_id, project


def discover_checkout_config(
    *, current_directory: Path, run_command: CommandRunner
) -> AgentWorkConfig:
    """Confirm the current checkout and derive its GitLab target without env vars."""
    checkout = Path(
        _confirmed_command(
            run_command,
            ("git", "-C", str(current_directory), "rev-parse", "--show-toplevel"),
            "current directory is not inside a Git checkout",
        )
    ).resolve()
    _confirmed_command(
        run_command,
        ("git", "-C", str(checkout), "pull", "--ff-only"),
        "git pull --ff-only failed",
    )
    origin = _confirmed_command(
        run_command,
        ("git", "-C", str(checkout), "remote", "get-url", "origin"),
        "checkout origin could not be read",
    )
    host, path = _repository_location(origin)
    project_id, _project = _discover_project(
        host=host, path=path, run_command=run_command
    )
    return AgentWorkConfig(host=host, project_id=project_id, checkout=checkout)


class GitLabGateway:
    """Use only numeric-project GitLab API endpoints."""

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
            *(argument for field in fields for argument in ("-f", field)),
        )
        response = self._run_command(command)
        if response.returncode != 0:
            raise AdapterError("GitLab API request failed")
        try:
            return cast(object, json.loads(response.stdout))
        except json.JSONDecodeError as error:
            raise AdapterError("GitLab returned invalid JSON") from error

    @staticmethod
    def _mapping(payload: object, label: str) -> dict[str, object]:
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) for key in payload
        ):
            raise AdapterError(f"GitLab returned invalid {label}")
        return cast(dict[str, object], payload)

    @staticmethod
    def _items(payload: object, label: str) -> tuple[dict[str, object], ...]:
        if not isinstance(payload, list):
            raise AdapterError(f"GitLab returned invalid {label}")
        return tuple(GitLabGateway._mapping(item, label) for item in payload)

    def verify_project(self) -> GitLabProject:
        """Bind the configured host and numeric ID to project metadata."""
        payload = self._mapping(
            self._call(f"projects/{self._config.project_id}"), "project metadata"
        )
        try:
            project = GitLabProject.model_validate(
                payload | {"host": self._config.host}
            )
        except ValidationError as error:
            raise AdapterError("GitLab returned invalid project metadata") from error
        if project.id != self._config.project_id:
            raise AdapterError("GitLab returned a different project")
        return project

    def _list_issues(
        self,
        *,
        state: str,
        limit: int,
        filters: tuple[str, ...] = (),
    ) -> tuple[GitLabIssue, ...]:
        payload = self._call(
            f"projects/{self._config.project_id}/issues",
            fields=(
                f"state={state}",
                *filters,
                "order_by=created_at",
                "sort=asc",
                f"per_page={limit}",
            ),
        )
        try:
            return tuple(
                GitLabIssue.model_validate(item)
                for item in self._items(payload, "issue list")
            )
        except ValidationError as error:
            raise AdapterError("GitLab returned invalid issue data") from error

    def list_eligible(self) -> tuple[GitLabIssue, ...]:
        """Return public, open eligible issues from oldest to newest."""
        return tuple(
            issue
            for issue in self._list_issues(
                state="opened",
                limit=1,
                filters=(
                    "confidential=false",
                    "not[labels]=agent::running,agent::awaiting-human,agent::failed",
                ),
            )
            if issue.state == "opened"
            and not issue.confidential
            and not {
                "agent::running",
                "agent::awaiting-human",
                "agent::failed",
            }.intersection(issue.labels)
        )

    def list_running(self) -> tuple[GitLabIssue, ...]:
        """Return open or closed issues carrying the running projection."""
        return tuple(
            issue
            for issue in self._list_issues(
                state="all", limit=2, filters=("labels=agent::running",)
            )
            if "agent::running" in issue.labels
        )

    def fetch_issue(self, iid: int) -> GitLabIssue:
        """Refetch and validate one configured-project issue."""
        payload = self._mapping(
            self._call(f"projects/{self._config.project_id}/issues/{iid}"), "issue"
        )
        try:
            issue = GitLabIssue.model_validate(payload)
        except ValidationError as error:
            raise AdapterError("GitLab returned invalid issue data") from error
        if issue.iid != iid:
            raise AdapterError("GitLab returned a different issue")
        return issue

    def _transition(
        self, issue: GitLabIssue, *, add: str, remove: tuple[str, ...]
    ) -> None:
        self._call(
            f"projects/{self._config.project_id}/issues/{issue.iid}",
            method="PUT",
            fields=(f"add_labels={add}", f"remove_labels={','.join(remove)}"),
        )
        refreshed = self.fetch_issue(issue.iid)
        if add not in refreshed.labels or any(
            label in refreshed.labels for label in remove
        ):
            raise AdapterError("GitLab did not apply the requested issue labels")

    def mark_running(self, issue: GitLabIssue) -> None:
        """Move an eligible issue to the running projection."""
        self._transition(
            issue,
            add="agent::running",
            remove=("agent::awaiting-human", "agent::failed"),
        )

    def mark_awaiting_human(self, issue: GitLabIssue) -> None:
        """Move a running issue to the human-review projection."""
        self._transition(
            issue,
            add="agent::awaiting-human",
            remove=("agent::running", "agent::failed"),
        )

    def mark_failed(self, issue: GitLabIssue) -> None:
        """Move a running or eligible issue to the failed projection."""
        self._transition(
            issue,
            add="agent::failed",
            remove=("agent::running", "agent::awaiting-human"),
        )

    def publication(self, branch: str) -> MergeRequestEvidence | None:
        """Return the newest MR whose reported source branch is exact."""
        payload = self._call(
            f"projects/{self._config.project_id}/merge_requests",
            fields=("state=all", f"source_branch={branch}", "per_page=20"),
        )
        try:
            candidates = tuple(
                MergeRequestEvidence.model_validate(item)
                for item in self._items(payload, "merge request list")
            )
        except ValidationError as error:
            raise AdapterError("GitLab returned invalid merge request data") from error
        return next(
            (
                candidate
                for candidate in candidates
                if candidate.source_branch == branch
            ),
            None,
        )

    def mr_closes_issue(self, mr_iid: int, issue_iid: int) -> bool:
        """Ask GitLab whether the merge request closes this project issue."""
        payload = self._call(
            f"projects/{self._config.project_id}/merge_requests/{mr_iid}/closes_issues"
        )
        return any(
            item.get("iid") == issue_iid
            and item.get("project_id") == self._config.project_id
            for item in self._items(payload, "closing issue list")
        )


class CommandRunner(Protocol):
    """Structural callable annotation for subprocess injection."""

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Run one argv command."""
        ...


class ServiceCheckout:
    """Git operations in the runner's confirmed checkout."""

    def __init__(self, config: AgentWorkConfig, run_command: CommandRunner) -> None:
        self._config = config
        self._run_command = run_command

    @property
    def path(self) -> Path:
        """Return the confirmed checkout path."""
        return self._config.checkout

    def _run(self, *arguments: str, accepted: tuple[int, ...] = (0,)) -> str:
        result = self._run_command(("git", "-C", str(self.path), *arguments))
        if result.returncode not in accepted:
            raise AdapterError("checkout validation failed")
        return result.stdout.strip()

    def applied_preflight(self, project: GitLabProject) -> None:
        """Require a clean bound checkout and fast-forward its default branch."""
        if not self.path.is_dir():
            raise AdapterError("the checkout does not exist")
        top_level = Path(self._run("rev-parse", "--show-toplevel")).resolve()
        if top_level != self.path.resolve():
            raise AdapterError("the configured path is not the checkout root")
        if self._run("status", "--porcelain"):
            raise AdapterError("the checkout is dirty")
        origins = {
            url.rstrip("/")
            for url in (project.http_url_to_repo, project.ssh_url_to_repo)
            if url
        }
        if self._run("remote", "get-url", "origin").rstrip("/") not in origins:
            raise AdapterError("checkout origin does not match the configured project")
        self._run(
            "fetch",
            "origin",
            f"refs/heads/{project.default_branch}:"
            f"refs/remotes/origin/{project.default_branch}",
        )
        self._run("switch", project.default_branch)
        self._run("merge", "--ff-only", f"origin/{project.default_branch}")
        if self._run("status", "--porcelain"):
            raise AdapterError("the refreshed checkout is dirty")

    def ensure_branch_available(self, branch: str) -> None:
        """Reject a conflicting deterministic branch before issue claim."""
        if re.fullmatch(r"agent/issue-[1-9][0-9]*", branch) is None:
            raise AdapterError("issue branch name is invalid")
        local = self._run(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            accepted=(0, 1),
        )
        remote = self._run(
            "ls-remote",
            "--exit-code",
            "--heads",
            "origin",
            f"refs/heads/{branch}",
            accepted=(0, 2),
        )
        if local or remote:
            raise AdapterError("the deterministic issue branch already exists")

    def prepare_branch(self, branch: str, default_branch: str) -> Path:
        """Create one branch already checked for conflicts before issue claim."""
        if re.fullmatch(r"agent/issue-[1-9][0-9]*", branch) is None:
            raise AdapterError("issue branch name is invalid")
        self._run("switch", "-c", branch, f"origin/{default_branch}")
        return self.path

    def publication_tip(self, branch: str) -> GitPublicationEvidence:
        """Read the exact local and remote tips for one issue branch."""
        local_sha = self._run("rev-parse", f"refs/heads/{branch}")
        remote_line = self._run(
            "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch}"
        )
        remote_parts = remote_line.split()
        if len(remote_parts) != 2:
            raise AdapterError("the remote issue branch tip is invalid")
        remote_sha = remote_parts[0]
        if not all(
            re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (local_sha, remote_sha)
        ):
            raise AdapterError("the issue branch tip is invalid")
        return GitPublicationEvidence(local_sha=local_sha, remote_sha=remote_sha)


class CodexInvoker:
    """Invoke Codex once in noninteractive ephemeral mode."""

    def __init__(self, run_command: CommandRunner) -> None:
        self._run_command = run_command

    def invoke(self, checkout: Path, prompt: str, timeout: int) -> int:
        """Run one Codex process and return its status for later verification."""
        result = self._run_command(
            (
                "codex",
                "exec",
                "-C",
                str(checkout),
                "--sandbox",
                "workspace-write",
                "-c",
                "sandbox_workspace_write.network_access=true",
                "--ephemeral",
                "--color",
                "never",
                "-",
            ),
            stdin=prompt,
            timeout_seconds=timeout,
        )
        return result.returncode


class ProjectLock:
    """One project-scoped nonblocking advisory file lock."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def __enter__(self) -> None:
        """Acquire the lock or report a temporary contention failure."""
        descriptor: int | None = None
        handle: IO[str] | None = None
        try:
            descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            handle = os.fdopen(descriptor, "a+", encoding="utf-8")
            descriptor = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise LockBusy("another run owns the project lock") from error
        except OSError as error:
            if handle is not None:
                handle.close()
            elif descriptor is not None:
                os.close(descriptor)
            raise AdapterError("the project lock could not be acquired") from error
        self._handle = handle

    def __exit__(self, *_args: object) -> bool:
        """Release the advisory lock."""
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False


def build_default_dependencies(
    run_command: CommandRunner = run_subprocess,
    current_directory: Path | None = None,
) -> RunNextDependencies:
    """Wire the real boundaries from the current checkout once at the CLI edge."""
    config = discover_checkout_config(
        current_directory=current_directory or Path.cwd(), run_command=run_command
    )
    return RunNextDependencies(
        gitlab=GitLabGateway(config, run_command),
        checkout=ServiceCheckout(config, run_command),
        invoke=CodexInvoker(run_command),
        lock=lambda: ProjectLock(config.lock_file),
    )
