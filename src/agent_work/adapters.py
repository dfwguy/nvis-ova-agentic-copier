"""Concrete, injectable adapters for subprocess, GitLab, worktrees, and state."""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    GitLabConfig,
    GitLabIssue,
    GitLabMergeRequest,
    GitLabPipeline,
    GitLabProject,
)
from .runner import CodexRequest


class AdapterFailure(RuntimeError):
    """A safe adapter failure that deliberately omits child-process output."""


@dataclass(frozen=True)
class CommandResult:
    """The non-secret outcome of a subprocess boundary."""

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[..., CommandResult]


def _atomic_durable_write(path: Path, content: str, mode: int) -> None:
    """Durably replace a private state file through a same-directory temp file."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise AdapterFailure("durable state write failed") from error


def _atomic_durable_create(path: Path, content: str, mode: int) -> None:
    """Durably create a private state file without replacing a competitor."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        Path(temporary).unlink()
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError as error:
        Path(temporary).unlink(missing_ok=True)
        raise AdapterFailure("create-only state record already exists") from error
    except OSError as error:
        Path(temporary).unlink(missing_ok=True)
        raise AdapterFailure("durable state create failed") from error


def run_subprocess(
    command: tuple[str, ...],
    *,
    stdin: str | None = None,
    timeout_seconds: int | None = None,
) -> CommandResult:
    """Run an argv command without a shell and retain output at this boundary."""
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
        raise AdapterFailure("command could not be completed") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def load_environment_config(environment: Mapping[str, str]) -> GitLabConfig:
    """Read only non-secret GitLab configuration from an injected environment."""
    try:
        return GitLabConfig(
            host=environment["GITLAB_HOST"],
            project_id=int(environment["GITLAB_PROJECT_ID"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterFailure(
            "GITLAB_HOST and a positive numeric GITLAB_PROJECT_ID are required"
        ) from error


def load_json_schema(path: Path) -> Mapping[str, object]:
    """Read one trusted local JSON schema through an explicit filesystem boundary."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterFailure("result schema could not be loaded") from error
    if not isinstance(document, dict):
        raise AdapterFailure("result schema must be a JSON object")
    return document


@dataclass(frozen=True)
class GitLabGateway:
    """GitLab numeric-project API gateway."""

    config: GitLabConfig
    run_command: CommandRunner

    def _call(self, command: tuple[str, ...]) -> dict[str, Any]:
        response = self.run_command(command)
        if response.returncode != 0:
            raise AdapterFailure("GitLab request failed")
        try:
            decoded = json.loads(response.stdout)
        except json.JSONDecodeError as error:
            raise AdapterFailure("GitLab returned invalid JSON") from error
        return decoded if isinstance(decoded, dict) else {"items": decoded}

    def verify_project(self) -> GitLabProject:
        """Verify and return the configured numeric project metadata."""
        try:
            return GitLabProject.model_validate(
                self._call(
                    (
                        "glab",
                        "api",
                        "--hostname",
                        self.config.host,
                        f"projects/{self.config.project_id}",
                    )
                )
            )
        except ValidationError as error:
            raise AdapterFailure("GitLab returned invalid project metadata") from error

    def fetch_issue(self, issue_iid: int) -> GitLabIssue:
        """Read and validate one issue from the configured project."""
        payload = self._call(
            (
                "glab",
                "api",
                "--hostname",
                self.config.host,
                f"projects/{self.config.project_id}/issues/{issue_iid}",
            )
        )
        if "state" not in payload or "confidential" not in payload:
            raise AdapterFailure("GitLab returned an invalid issue")
        try:
            issue = GitLabIssue.model_validate(payload)
        except ValidationError as error:
            raise AdapterFailure("GitLab returned an invalid issue") from error
        if issue.iid != issue_iid:
            raise AdapterFailure("GitLab returned a different issue than requested")
        return issue

    def project_awaiting_human(self, *, issue_iid: int, run_id: str) -> None:
        """Project a verified handoff as labels and a concise observable comment."""
        base = ("glab", "api", "--hostname", self.config.host)
        self._call(
            base
            + (
                "-X",
                "PUT",
                f"projects/{self.config.project_id}/issues/{issue_iid}",
                "-f",
                "add_labels=agent::awaiting-human",
                "-f",
                "remove_labels=agent::running,agent::ready",
            )
        )
        self._call(
            base
            + (
                "-X",
                "POST",
                f"projects/{self.config.project_id}/issues/{issue_iid}/notes",
                "-f",
                f"body=Run {run_id} reached awaiting_human.",
            )
        )

    def claim_running(self, *, issue_iid: int, run_id: str) -> None:
        """Project the selected ready issue as running before invocation."""
        base = ("glab", "api", "--hostname", self.config.host)
        self._call(
            base
            + (
                "-X",
                "PUT",
                f"projects/{self.config.project_id}/issues/{issue_iid}",
                "-f",
                "add_labels=agent::running",
                "-f",
                "remove_labels=agent::ready",
            )
        )
        self._call(
            base
            + (
                "-X",
                "POST",
                f"projects/{self.config.project_id}/issues/{issue_iid}/notes",
                "-f",
                f"body=Run {run_id} started.",
            )
        )

    def project_failed(self, *, issue_iid: int, run_id: str) -> None:
        """Project a bounded-run failure without exposing private diagnostics."""
        base = ("glab", "api", "--hostname", self.config.host)
        self._call(
            base
            + (
                "-X",
                "PUT",
                f"projects/{self.config.project_id}/issues/{issue_iid}",
                "-f",
                "add_labels=agent::failed",
                "-f",
                "remove_labels=agent::running,agent::awaiting-human",
            )
        )
        self._call(
            base
            + (
                "-X",
                "POST",
                f"projects/{self.config.project_id}/issues/{issue_iid}/notes",
                "-f",
                f"body=Run {run_id} failed; inspect local runner state.",
            )
        )

    def verify_merge_request(
        self,
        *,
        branch: str,
        branch_tip: str,
        merge_request_url: str,
        target_branch: str,
        source_project_id: int,
        target_project_id: int,
    ) -> None:
        """Ensure reported artifacts match an open MR in this numeric project."""
        response = self._call(
            (
                "glab",
                "api",
                "--hostname",
                self.config.host,
                "-X",
                "GET",
                f"projects/{self.config.project_id}/merge_requests?state=opened",
                "-f",
                f"source_branch={branch}",
            )
        )
        candidates = self._merge_request_items(response)
        if not any(
            item.web_url == merge_request_url
            and item.source_branch == branch
            and item.sha == branch_tip
            and item.state == "opened"
            and item.target_branch == target_branch
            and item.source_project_id == source_project_id
            and item.target_project_id == target_project_id
            for item in candidates
        ):
            raise AdapterFailure("reported merge request could not be verified")

    def latest_pipeline_is_healthy(self, *, target_branch: str) -> None:
        """Reject a failed or canceled latest configured-default pipeline."""
        response = self._call(
            (
                "glab",
                "api",
                "--hostname",
                self.config.host,
                f"projects/{self.config.project_id}/pipelines?ref={target_branch}&per_page=1",
            )
        )
        pipelines = self._pipeline_items(response)
        if not (pipelines and pipelines[0].status == "success"):
            raise AdapterFailure("the latest default-branch pipeline is unhealthy")

    def find_open_merge_request(
        self,
        *,
        branch: str,
        target_branch: str,
    ) -> GitLabMergeRequest | None:
        """Return the exact open same-project merge request, if present."""
        response = self._call(
            (
                "glab",
                "api",
                "--hostname",
                self.config.host,
                "-X",
                "GET",
                f"projects/{self.config.project_id}/merge_requests?state=opened",
                "-f",
                f"source_branch={branch}",
            )
        )
        for candidate in self._merge_request_items(response):
            if (
                candidate.state == "opened"
                and candidate.source_branch == branch
                and candidate.target_branch == target_branch
                and candidate.source_project_id == self.config.project_id
                and candidate.target_project_id == self.config.project_id
            ):
                return candidate
        return None

    @staticmethod
    def _pipeline_items(response: Mapping[str, Any]) -> tuple[GitLabPipeline, ...]:
        """Validate every external pipeline item before applying policy."""
        items = response.get("items", [])
        if not isinstance(items, list):
            raise AdapterFailure("GitLab returned invalid pipeline data")
        try:
            return tuple(GitLabPipeline.model_validate(item) for item in items)
        except ValidationError as error:
            raise AdapterFailure("GitLab returned invalid pipeline data") from error

    @staticmethod
    def _merge_request_items(
        response: Mapping[str, Any],
    ) -> tuple[GitLabMergeRequest, ...]:
        """Validate every external merge-request item before matching evidence."""
        raw_items = response.get("items", [response])
        if not isinstance(raw_items, list):
            raise AdapterFailure("GitLab returned invalid merge-request data")
        try:
            return tuple(GitLabMergeRequest.model_validate(item) for item in raw_items)
        except ValidationError as error:
            raise AdapterFailure(
                "GitLab returned invalid merge-request data"
            ) from error


@dataclass(frozen=True)
class WorktreeGateway:
    """Safe deterministic worktree creation for one issue branch."""

    repository: Path
    run_command: CommandRunner

    def create_or_reuse(
        self,
        *,
        issue_iid: int,
        default_branch: str = "main",
        origin_url: str | None = None,
        workspace_root: Path | None = None,
    ) -> Path:
        """Create a retained, fully independent clone for one issue branch."""
        if origin_url is None:
            source_origin = self.run_command(
                ("git", "-C", str(self.repository), "remote", "get-url", "origin")
            )
            if source_origin.returncode != 0 or not source_origin.stdout.strip():
                raise AdapterFailure("source origin could not be determined")
            clone_origin = source_origin.stdout.strip()
        else:
            clone_origin = origin_url
        key = hashlib.sha256(clone_origin.encode()).hexdigest()[:16]
        root = workspace_root or self.repository.parent / "agent-workspaces" / key
        workspace = root / f"issue-{issue_iid}"
        branch = f"agent/issue-{issue_iid}"
        if (
            root.is_symlink()
            or workspace.is_symlink()
            or (workspace / ".git").is_symlink()
        ):
            raise AdapterFailure("independent workspace path must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (
            root.is_symlink()
            or workspace.is_symlink()
            or (workspace / ".git").is_symlink()
        ):
            raise AdapterFailure("independent workspace path must not be a symlink")
        root.chmod(0o700)
        if (workspace / ".git").is_dir():
            self._verify_workspace(workspace, clone_origin, branch)
            return workspace
        created = self.run_command(
            ("git", "clone", "--no-checkout", clone_origin, str(workspace))
        )
        if created.returncode != 0 or not (workspace / ".git").is_dir():
            raise AdapterFailure("independent workspace could not be created")
        checkout = self.run_command(
            (
                "git",
                "-C",
                str(workspace),
                "checkout",
                "-b",
                branch,
                f"origin/{default_branch}",
            )
        )
        if checkout.returncode != 0:
            raise AdapterFailure("independent workspace could not be initialized")
        return workspace

    def _verify_workspace(self, workspace: Path, origin: str, branch: str) -> None:
        """Prove a retained clone has the expected origin, branch, and readable HEAD."""
        remote = self.run_command(
            ("git", "-C", str(workspace), "remote", "get-url", "origin")
        )
        checked_branch = self.run_command(
            ("git", "-C", str(workspace), "branch", "--show-current")
        )
        head = self.run_command(("git", "-C", str(workspace), "rev-parse", "HEAD"))
        if (
            remote.returncode != 0
            or remote.stdout.strip() != origin
            or checked_branch.returncode != 0
            or checked_branch.stdout.strip() != branch
            or head.returncode != 0
            or not head.stdout.strip()
        ):
            raise AdapterFailure("retained workspace provenance could not be verified")


@dataclass(frozen=True)
class LocalRunStore:
    """Private local lock, append-only journal, and result-file storage."""

    root: Path
    atomic_write: Callable[[Path, str, int], None] = _atomic_durable_write

    def _create_only(self, path: Path, content: str, mode: int) -> None:
        """Create one record atomically through the real or injected write boundary."""
        if self.atomic_write is _atomic_durable_write:
            _atomic_durable_create(path, content, mode)
            return
        try:
            self.atomic_write(path, content, mode)
        except FileExistsError as error:
            raise AdapterFailure("create-only state record already exists") from error

    @classmethod
    def for_git_common_dir(cls, common_dir: Path) -> "LocalRunStore":
        """Create state rooted in the private common-Git runner directory."""
        return cls(root=common_dir / "agent-work")

    def _ensure_safe_root(self) -> None:
        if self.root.is_symlink():
            raise AdapterFailure("runner state path must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise AdapterFailure("runner state path must not be a symlink")

    @staticmethod
    def _reject_symlink(path: Path, *, message: str) -> None:
        """Reject an attacker-controlled state child before opening it."""
        if path.is_symlink():
            raise AdapterFailure(message)

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        """Hold an exclusive local process lock for one run."""
        self._ensure_safe_root()
        lock_path = self.root / "lock"
        self._reject_symlink(
            lock_path, message="runner lock path must not be a symlink"
        )
        with lock_path.open("a+", encoding="utf-8") as handle:
            operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError as error:
                raise AdapterFailure("another runner holds the local lock") from error
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def append_transition(self, transition: Mapping[str, object]) -> None:
        """Append one immutable audit record to the local JSONL journal."""
        self._ensure_safe_root()
        journal_path = self.root / "journal.jsonl"
        self._reject_symlink(
            journal_path, message="runner journal path must not be a symlink"
        )
        content = json.dumps(transition, sort_keys=True) + "\n"
        previous = (
            journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        )
        self.atomic_write(journal_path, previous + content, 0o600)

    def write_canonical_result(self, run_id: str, result: Mapping[str, object]) -> Path:
        """Write a private result document for the selected run."""
        path = self._result_path(run_id)
        if path.exists() or path.is_symlink():
            raise AdapterFailure("canonical result already exists")
        content = json.dumps(result, sort_keys=True)
        self._create_only(path, content, 0o600)
        return path

    def write_recovery_record(self, run_id: str, record: Mapping[str, object]) -> Path:
        """Persist a separate private recovery record."""
        self._ensure_safe_root()
        directory = self.root / "recovery"
        self._reject_symlink(
            directory, message="runner recovery path must not be a symlink"
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f"{run_id}.json"
        if path.exists() or path.is_symlink():
            raise AdapterFailure("recovery record already exists")
        content = json.dumps(record, sort_keys=True)
        self._create_only(path, content, 0o600)
        return path

    def write_workspace_provenance(
        self, work_item_id: str, provenance: Mapping[str, object]
    ) -> Path:
        """Persist the runner-authored workspace manifest."""
        self._ensure_safe_root()
        directory = self.root / "workspaces"
        self._reject_symlink(
            directory, message="runner workspace manifest path must not be a symlink"
        )
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._reject_symlink(
            directory, message="runner workspace manifest path must not be a symlink"
        )
        name = hashlib.sha256(work_item_id.encode()).hexdigest() + ".json"
        path = directory / name
        if path.exists() or path.is_symlink():
            raise AdapterFailure("workspace provenance already exists")
        content = json.dumps(provenance, sort_keys=True)
        self._create_only(path, content, 0o600)
        return path

    def read_workspace_provenance(
        self, work_item_id: str
    ) -> Mapping[str, object] | None:
        """Read a runner-authored workspace manifest if it exists."""
        self._ensure_safe_root()
        self._reject_symlink(
            self.root / "workspaces",
            message="runner workspace manifest path must not be a symlink",
        )
        path = (
            self.root
            / "workspaces"
            / (hashlib.sha256(work_item_id.encode()).hexdigest() + ".json")
        )
        if not path.exists():
            return None
        if path.is_symlink():
            raise AdapterFailure("workspace provenance path must not be a symlink")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise AdapterFailure("workspace provenance could not be read") from error
        if not isinstance(record, dict):
            raise AdapterFailure("workspace provenance is invalid")
        return record

    def _result_path(self, run_id: str) -> Path:
        """Prepare a private result location before the Codex process starts."""
        self._ensure_safe_root()
        results = self.root / "results"
        self._reject_symlink(
            results, message="runner results path must not be a symlink"
        )
        results.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._reject_symlink(
            results, message="runner results path must not be a symlink"
        )
        if Path(run_id).name != run_id:
            raise AdapterFailure("result run identifier must be a file name")
        path = results / f"{run_id}.json"
        self._reject_symlink(path, message="runner result path must not be a symlink")
        return path


@dataclass(frozen=True)
class CodexInvoker:
    """Bounded Codex process adapter that extracts only its final JSON message."""

    run_command: CommandRunner

    def __call__(self, request: CodexRequest) -> dict[str, object]:
        """Invoke Codex once and parse its last agent-message JSON document."""
        response = self.run_command(
            request.command,
            stdin=request.stdin,
            timeout_seconds=request.timeout_seconds,
        )
        if response.returncode != 0:
            raise AdapterFailure("Codex invocation failed")
        try:
            events = [
                json.loads(line)
                for line in response.stdout.splitlines()
                if line.strip()
            ]
            messages = [
                item["text"]
                for event in events
                if isinstance(event, dict)
                and isinstance((item := event.get("item")), dict)
                and item.get("type") == "agent_message"
            ]
            if any(
                not isinstance(event, dict)
                or ("item" in event and not isinstance(event["item"], dict))
                for event in events
            ) or not all(isinstance(message, str) for message in messages):
                raise TypeError("Codex emitted an invalid JSONL event")
            document = json.loads(messages[-1])
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise AdapterFailure("Codex did not return a structured result") from error
        if not isinstance(document, dict):
            raise AdapterFailure("Codex did not return an object result")
        return document
