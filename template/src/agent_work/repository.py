# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Git and durable-worktree boundary for recoverable agent work."""

from __future__ import annotations

import json
import math
import os
import re
import select
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import IO
from urllib.parse import urlparse

from pydantic import ValidationError

from .candidate_boundary import (
    CandidateBoundaryError,
    validate_git_read_paths,
    validate_git_read_size,
)
from .domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS, ErrorCode, sanitize_diagnostic
from .ledger import project_state_path
from .models import AgentWorkConfig
from .ports import CommandResult as CommandResultProtocol
from .ports import CommandRunner
from .red_checkpoint import RedCheckpointError, RedCheckpointEvidence, checkpoint_ref
from .test_timing import is_safe_timing_case_id
from .validation import (
    MAX_TIMING_CASE_LIST_BYTES,
    MAX_TIMING_CASE_NAME_BYTES,
    MAX_TIMING_CASES,
    HookFailure,
    is_safe_junit_test_name,
    parse_installed_precommit_failures,
)


class RepositoryError(RuntimeError):
    """A repository operation could not be completed safely."""


class CommandTimeoutError(RepositoryError):
    """A bounded command exceeded its allowed execution time."""


class RepositoryTransientError(RepositoryError):
    """A Git transport operation may be retried safely."""


class PublicationFailureCategory(str, Enum):
    """Safe categories for failed Git publication."""

    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    POLICY = "policy"
    TRANSPORT = "transport"
    CONFIGURATION = "configuration"


@dataclass(frozen=True)
class PublicationFailure:
    """Safe immutable publication-failure record."""

    category: PublicationFailureCategory
    code: ErrorCode
    retryable: bool
    summary: str
    next_action: str


class PublicationError(RepositoryError):
    """A permanent, safely classified Git publication failure."""

    def __init__(self, failure: PublicationFailure) -> None:
        self.failure = failure
        self.code = failure.code
        super().__init__(failure.summary)


@dataclass(frozen=True)
class PublicationCommitFailure:
    """Safe evidence from one rejected local publication commit."""

    diagnostic: str


@dataclass(frozen=True)
class LegacyPublicationCandidate:
    """Read-only Git identity retained by the legacy publication recovery."""

    branch: str
    head: str
    merge_head: str | None
    index_fingerprint: str
    staged_paths: tuple[str, ...]
    unstaged_paths: tuple[str, ...]
    has_untracked: bool
    has_unmerged: bool
    has_renames: bool


@dataclass(frozen=True)
class WorktreeStatus:
    """One immutable porcelain observation for retained recovery."""

    dirty: bool
    changed_paths: tuple[str, ...]
    porcelain: str


def _publication_commit_diagnostic(failures: tuple[HookFailure, ...]) -> str:
    """Render bounded allowlisted evidence from installed pre-commit output."""
    details = "; ".join(
        f"{failure.hook_id}: {', '.join(failure.diagnostic_lines)}"
        for failure in failures
    )
    return sanitize_diagnostic(f"Installed pre-commit rejected the commit: {details}")


class PublicationCommitRejected(RepositoryError):
    """The exact retained candidate was rejected by its local commit hook."""

    def __init__(self, failure: PublicationCommitFailure) -> None:
        self.failure = failure
        super().__init__(failure.diagnostic)


def classify_publication_failure(stderr: str) -> PublicationFailure:
    """Classify push stderr without retaining or exposing its content."""
    value = stderr.lower()
    categories = (
        (
            PublicationFailureCategory.AUTHENTICATION,
            ErrorCode.GIT_AUTHENTICATION,
            (
                "gpg_tty",
                "could not read username",
                "terminal prompts",
                "authentication failed",
                "credential helper",
                "auth failed",
            ),
            "Git authentication failed.",
            "Repair the configured Git credential helper, then retry explicitly.",
        ),
        (
            PublicationFailureCategory.AUTHORIZATION,
            ErrorCode.GIT_AUTHORIZATION,
            ("not allowed to push", "forbidden", "permission denied"),
            "Git authorization to publish was denied.",
            "Request the required repository write authorization, then retry explicitly.",
        ),
        (
            PublicationFailureCategory.POLICY,
            ErrorCode.GIT_POLICY,
            ("pre-receive", "protected branch", "remote rejected", "hook declined"),
            "Git publication was rejected by repository policy.",
            "Resolve the repository policy requirement, then retry explicitly.",
        ),
        (
            PublicationFailureCategory.TRANSPORT,
            ErrorCode.GIT_TRANSPORT_RETRYABLE,
            (
                "timed out",
                "timeout",
                "connection reset",
                "connection refused",
                "could not resolve host",
                "temporary failure in name resolution",
                "remote end hung up",
                "tls handshake timeout",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "requested url returned error: 500",
                "requested url returned error: 502",
                "requested url returned error: 503",
                "requested url returned error: 504",
            ),
            "Git publication transport failed.",
            "The runner will retry the transport failure within its bounded retry policy.",
        ),
    )
    for category, code, patterns, summary, next_action in categories:
        if any(pattern in value for pattern in patterns):
            return PublicationFailure(
                category,
                code,
                category is PublicationFailureCategory.TRANSPORT,
                summary,
                next_action,
            )
    return PublicationFailure(
        PublicationFailureCategory.CONFIGURATION,
        ErrorCode.GIT_PUBLICATION_CONFIGURATION,
        False,
        "Git publication configuration failed.",
        "Verify the configured Git publication route, then retry explicitly.",
    )


def build_git_environment(
    service_environment: Mapping[str, str], credential_helper: Path
) -> dict[str, str]:
    """Build the noninteractive, repository-helper-only environment for Git."""
    if not service_environment.get("GITLAB_TOKEN"):
        raise RepositoryError("GitLab token is required for Git operations")
    return _git_environment_controls(service_environment, credential_helper)


def _git_environment_controls(
    service_environment: Mapping[str, str], credential_helper: Path
) -> dict[str, str]:
    """Scrub ambient Git controls without adding a credential value."""
    environment = dict(service_environment)
    environment.pop("GIT_CONFIG_COUNT", None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    for key in tuple(environment):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del environment[key]
    environment["GIT_ASKPASS"] = "/bin/false"
    environment["SSH_ASKPASS"] = "/bin/false"
    environment["AGENT_WORK_GIT_CREDENTIAL_HELPER"] = str(credential_helper)
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
    return environment


@dataclass(frozen=True)
class CommandResult:
    """Captured output from an argv-only subprocess."""

    returncode: int
    stdout: str
    stderr: str


def run_subprocess(
    command: tuple[str, ...],
    *,
    stdin: str | None = None,
    timeout_seconds: float | None = None,
    environment: Mapping[str, str] | None = None,
    current_directory: Path | None = None,
) -> CommandResult:
    """Run one command without invoking a shell."""
    try:
        completed = subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            env=None if environment is None else dict(environment),
            cwd=current_directory,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CommandTimeoutError("a required command timed out") from error
    except OSError as error:
        raise RepositoryError("a required command could not be completed") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _stdout_readable(stream: IO[bytes], timeout_seconds: float) -> bool:
    readable, _, _ = select.select((stream,), (), (), timeout_seconds)
    return bool(readable)


def run_subprocess_bytes(
    command: tuple[str, ...],
    *,
    max_output_bytes: int,
    timeout_seconds: float,
    environment: Mapping[str, str] | None = None,
    current_directory: Path | None = None,
    open_process: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    stream_readable: Callable[[IO[bytes], float], bool] = _stdout_readable,
    read_bytes: Callable[[int, int], bytes] = os.read,
    clock: Callable[[], float] = monotonic,
) -> bytes:
    """Capture at most a prechecked Git blob size without text conversion."""
    if max_output_bytes < 0 or timeout_seconds <= 0:
        raise RepositoryError("candidate read failed")
    try:
        process = open_process(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=None if environment is None else dict(environment),
            cwd=current_directory,
        )
    except OSError as error:
        raise RepositoryError("candidate read failed") from error
    deadline = clock() + timeout_seconds
    try:
        assert process.stdout is not None
        output = bytearray()
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise CommandTimeoutError("a required command timed out")
            if not stream_readable(process.stdout, remaining):
                raise CommandTimeoutError("a required command timed out")
            chunk = read_bytes(
                process.stdout.fileno(),
                min(65_536, max_output_bytes - len(output) + 1),
            )
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_output_bytes:
                raise RepositoryError("candidate content is oversize or binary")
        remaining = deadline - clock()
        if remaining <= 0:
            raise CommandTimeoutError("a required command timed out")
        try:
            if process.wait(timeout=remaining) != 0:
                raise RepositoryError("candidate read failed")
        except subprocess.TimeoutExpired as error:
            raise CommandTimeoutError("a required command timed out") from error
        return bytes(output)
    except OSError as error:
        raise RepositoryError("candidate read failed") from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _confirmed(
    run_command: CommandRunner, command: tuple[str, ...], failure: str
) -> str:
    result = run_command(command, timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RepositoryError(failure)
    return result.stdout.strip()


def _origin_identity(origin: str) -> tuple[str, str]:
    normalized = origin.strip().removesuffix(".git")
    parsed = urlparse(normalized)
    if parsed.hostname and parsed.path.strip("/"):
        return parsed.hostname, parsed.path.strip("/")
    match = re.fullmatch(r"(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)", normalized)
    if match is None:
        raise RepositoryError("checkout origin is not a usable GitLab URL")
    return match["host"], match["path"].strip("/")


def discover_config(
    *,
    current_directory: Path,
    environment: Mapping[str, str],
    run_command: CommandRunner,
) -> AgentWorkConfig:
    """Discover and verify config without mutating the checkout."""
    base_branch = environment.get("AGENT_WORK_BASE_BRANCH", "")
    if not base_branch:
        raise RepositoryError("AGENT_WORK_BASE_BRANCH is required")
    factory_username = environment.get("AGENT_WORK_FACTORY_USERNAME", "")
    if not factory_username:
        raise RepositoryError("AGENT_WORK_FACTORY_USERNAME is required")
    git_path = Path(environment.get("AGENT_WORK_GIT", ""))
    glab_path = Path(environment.get("AGENT_WORK_GLAB", ""))
    if not git_path.is_absolute() or not glab_path.is_absolute():
        raise RepositoryError(
            "AGENT_WORK_GIT and AGENT_WORK_GLAB must be absolute executable paths"
        )
    start = current_directory.resolve()
    checkout = Path(
        _confirmed(
            run_command,
            (str(git_path), "-C", str(start), "rev-parse", "--show-toplevel"),
            "current directory is not inside a Git checkout",
        )
    ).resolve()
    origin = _confirmed(
        run_command,
        (str(git_path), "-C", str(checkout), "remote", "get-url", "origin"),
        "checkout origin could not be read",
    )
    parsed_origin = urlparse(origin.strip())
    if (
        parsed_origin.scheme != "https"
        or parsed_origin.username
        or parsed_origin.password
    ):
        raise RepositoryError("checkout origin must be credential-free HTTPS")
    origin_host, project_path = _origin_identity(origin)
    host = environment.get("GITLAB_HOST", "")
    project_id_text = environment.get("GITLAB_PROJECT_ID", "")
    if not host or "://" in host or "/" in host:
        raise RepositoryError("GITLAB_HOST must be a bare hostname")
    if not project_id_text.isdigit() or int(project_id_text) <= 0:
        raise RepositoryError("GITLAB_PROJECT_ID must be numeric")
    project_id = int(project_id_text)
    numeric = _confirmed(
        run_command,
        (str(glab_path), "api", "--hostname", host, f"projects/{project_id}"),
        "GitLab numeric project confirmation failed",
    )
    try:
        project = json.loads(numeric)
    except json.JSONDecodeError as error:
        raise RepositoryError("GitLab returned invalid project metadata") from error
    if (
        not isinstance(project, dict)
        or project.get("id") != project_id
        or origin_host != host
        or project.get("path_with_namespace") != project_path
    ):
        raise RepositoryError("GitLab project identity did not match the checkout")
    project_origin = str(project.get("http_url_to_repo") or "")
    parsed_project_origin = urlparse(project_origin)
    if (
        parsed_project_origin.scheme != "https"
        or parsed_project_origin.username
        or parsed_project_origin.password
        or project_origin.removesuffix(".git") != origin.strip().removesuffix(".git")
    ):
        raise RepositoryError("GitLab project HTTPS origin did not match the checkout")
    state_root = project_state_path(
        host=host, project_id=project_id, environment=environment
    )
    try:
        return AgentWorkConfig(
            host=host,
            project_id=project_id,
            checkout=checkout,
            state_root=state_root,
            default_branch=str(project.get("default_branch") or "main"),
            base_branch=base_branch,
            journal_tag=environment.get("AGENT_WORK_LOG_TAG") or "agent-work",
            git_path=git_path,
            glab_path=glab_path,
            factory_username=factory_username,
        )
    except ValidationError as error:
        raise RepositoryError("agent-work configuration is invalid") from error


def parse_porcelain_paths(status: str) -> tuple[str, ...]:
    """Parse porcelain-v1 NUL records without corrupting rename destinations."""
    entries = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) <= 3:
            continue
        paths.append(entry[3:])
        if ({"R", "C"} & set(entry[:2])) and index < len(entries):
            paths.append(entries[index])
            index += 1
    return tuple(paths)


class GitRepository:
    """Own durable worktrees and publication Git operations."""

    def __init__(
        self,
        config: AgentWorkConfig,
        run_command: CommandRunner,
        *,
        service_environment: Mapping[str, str] | None = None,
        diagnostic_sink: Callable[[dict[str, object]], None] | None = None,
        read_git_blob: Callable[..., bytes] | None = None,
    ) -> None:
        self.config = config
        self._run_command = run_command
        self._environment = _git_environment_controls(
            service_environment or {},
            config.checkout / "src" / "agent-work-git-credential.sh",
        )
        self._diagnostic_sink = diagnostic_sink or (lambda _diagnostic: None)
        self._read_git_blob = read_git_blob or run_subprocess_bytes

    def _bounded_command(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        current_directory: Path | None = None,
    ) -> CommandResultProtocol:
        """Run one Git command and preserve timeout retry semantics."""
        try:
            if current_directory is not None:
                return self._run_command(
                    command,
                    stdin=stdin,
                    timeout_seconds=timeout_seconds,
                    environment=environment,
                    current_directory=current_directory,
                )
            return self._run_command(
                command,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
        except CommandTimeoutError as error:
            raise RepositoryTransientError("Git operation timed out") from error

    def _git(
        self,
        *arguments: str,
        cwd: Path | None = None,
        transient: bool = False,
        preserve_output: bool = False,
    ) -> str:
        transport_operation = arguments[:1] in {
            ("fetch",),
            ("pull",),
            ("push",),
            ("ls-remote",),
        }
        if transport_operation and not self._environment.get("GITLAB_TOKEN"):
            raise RepositoryError("GitLab token is required for Git operations")
        command = (
            str(self.config.git_path),
            "-C",
            str(cwd or self.config.checkout),
        )
        if transport_operation:
            command += (
                "-c",
                "credential.helper=",
                "-c",
                'credential.helper=!exec "$AGENT_WORK_GIT_CREDENTIAL_HELPER"',
                "-c",
                "credential.useHttpPath=true",
                "-c",
                "core.askPass=/bin/false",
            )
        result = self._bounded_command(
            (*command, *arguments),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            if arguments and arguments[0] == "push":
                failure = classify_publication_failure(result.stderr)
                self._diagnostic_sink(
                    {
                        "category": failure.category.value,
                        "operation": "push",
                        "returncode": result.returncode,
                    }
                )
                if failure.retryable:
                    raise RepositoryTransientError(failure.summary)
                raise PublicationError(failure)
            error_type = RepositoryTransientError if transient else RepositoryError
            raise error_type("Git operation failed")
        return result.stdout if preserve_output else result.stdout.strip()

    def _merge_head(self, worktree: Path) -> str | None:
        """Return the pending merge target, if this worktree is merging."""
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "rev-parse",
                "-q",
                "--verify",
                "MERGE_HEAD",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode not in {0, 1}:
            raise RepositoryError("Git operation failed")
        return result.stdout.strip() or None

    def _is_ancestor(self, ancestor: str, descendant: str, worktree: Path) -> bool:
        """Return whether Git proves one commit is an ancestor of another."""
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode not in {0, 1}:
            raise RepositoryError("Git operation failed")
        return result.returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str, worktree: Path) -> bool:
        """Return whether the retained worktree proves a fast-forward relation."""
        return self._is_ancestor(ancestor, descendant, worktree)

    def _merge_current_default(self, default_sha: str, worktree: Path) -> None:
        """Start a merge while retaining conflict state for workflow repair."""
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "merge",
                "--no-ff",
                "--no-commit",
                default_sha,
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode not in {0, 1}:
            raise RepositoryError("Git operation failed")

    def refresh_base(self) -> None:
        """Require a clean base checkout and fast-forward it."""
        expected = self.config.base_branch
        if self._git("branch", "--show-current") != expected:
            raise RepositoryError(f"service checkout must be on {expected!r}")
        if self._git("status", "--porcelain"):
            raise RepositoryError("service checkout must be clean")
        self._git("pull", "--ff-only", transient=True)

    def default_branch_head(self) -> str:
        """Read the exact fetched default-branch commit."""
        return self._git("rev-parse", f"origin/{self.config.default_branch}")

    def refresh_default_branch(self) -> str:
        """Refresh the configured default branch before fresh issue work."""
        self.refresh_base()
        self._git("fetch", "origin", self.config.default_branch, transient=True)
        return self.default_branch_head()

    def fresh_issue_branch_available(self, issue_iid: int) -> bool:
        """Check the exact new issue branch against local and remote owners."""
        branch = f"agent/issue-{issue_iid}"
        path = self.config.state_root / "worktrees" / str(issue_iid)
        return (
            not path.exists()
            and not self._git("branch", "--list", branch)
            and not self._git("ls-remote", "--heads", "origin", branch, transient=True)
        )

    def create_issue_worktree(
        self,
        issue_iid: int,
        branch: str,
        default_branch: str,
        *,
        allow_existing_branch: bool = True,
    ) -> Path:
        """Create or verify the durable worktree for an ordinary issue."""
        path = self.config.state_root / "worktrees" / str(issue_iid)
        if path.exists():
            if not allow_existing_branch:
                raise RepositoryError("worktree ownership is ambiguous")
            actual = self._git("branch", "--show-current", cwd=path)
            if actual != branch:
                raise RepositoryError("worktree ownership is ambiguous")
            return path
        if not allow_existing_branch and not self.fresh_issue_branch_available(
            issue_iid
        ):
            raise RepositoryError("branch ownership is ambiguous")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote_ref = f"refs/remotes/origin/{branch}"
        local_exists = bool(self._git("branch", "--list", branch))
        remote = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(self.config.checkout),
                "show-ref",
                "--verify",
                "--quiet",
                remote_ref,
            ),
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if (local_exists or remote.returncode == 0) and not allow_existing_branch:
            raise RepositoryError("branch ownership is ambiguous")
        if local_exists:
            self._git("worktree", "add", str(path), branch)
        elif remote.returncode == 0:
            self._git("worktree", "add", "-b", branch, str(path), f"origin/{branch}")
        else:
            self._git(
                "worktree", "add", "-b", branch, str(path), f"origin/{default_branch}"
            )
        return path

    def refresh_retained_worktree(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str:
        """Fast-forward one exact untouched retained worktree to current default."""
        match = re.fullmatch(r"agent/issue-(?P<iid>[1-9][0-9]*)", branch)
        if match is None:
            raise RepositoryError("retained worktree branch is not an ordinary issue")
        expected_path = self.config.state_root / "worktrees" / match["iid"]
        if worktree.resolve() != expected_path.resolve() or not worktree.is_dir():
            raise RepositoryError("retained worktree path is not exact")
        if self._git("branch", "--show-current", cwd=worktree) != branch:
            raise RepositoryError("retained worktree is on the wrong branch")
        if self.has_changes(worktree):
            raise RepositoryError("retained worktree must be clean")
        merge_path_value = self._git(
            "rev-parse", "--git-path", "MERGE_HEAD", cwd=worktree
        )
        merge_path = Path(merge_path_value)
        if not merge_path.is_absolute():
            merge_path = worktree / merge_path
        if merge_path.exists():
            raise RepositoryError("retained worktree has a pending merge")

        self._git(
            "fetch", "origin", self.config.default_branch, cwd=worktree, transient=True
        )
        default_ref = f"origin/{self.config.default_branch}"
        default_sha = self._git("rev-parse", default_ref, cwd=worktree)
        head = self.head(worktree)
        if head == default_sha:
            if expected_head != default_sha and not self._is_ancestor(
                expected_head, default_sha, worktree
            ):
                raise RepositoryError(
                    "retained worktree does not match its recorded baseline"
                )
            return default_sha
        if head != expected_head and not self._is_ancestor(
            expected_head, head, worktree
        ):
            raise RepositoryError(
                "retained worktree does not match its recorded baseline"
            )
        if not self._is_ancestor(head, default_sha, worktree):
            if self._is_ancestor(default_sha, head, worktree):
                raise RepositoryError("retained worktree contains authored commits")
            raise RepositoryError("retained worktree diverged from current default")
        self._git("merge", "--ff-only", default_ref, cwd=worktree)
        if self.head(worktree) != default_sha:
            raise RepositoryError(
                "retained worktree fast-forward could not be verified"
            )
        return default_sha

    def worktree_for_existing_branch(
        self, issue_iid: int, branch: str, expected_sha: str
    ) -> Path:
        """Create a durable worktree from an exact existing remote branch."""
        path = self.config.state_root / "worktrees" / str(issue_iid)
        if path.exists():
            if self._git("branch", "--show-current", cwd=path) != branch:
                raise RepositoryError("worktree ownership is ambiguous")
            if self._git("rev-parse", "HEAD", cwd=path) != expected_sha:
                raise RepositoryError(
                    "worktree does not match recorded merge request SHA"
                )
            return path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._git("fetch", "origin", branch, transient=True)
        if self._git("rev-parse", f"origin/{branch}") != expected_sha:
            raise RepositoryError(
                "remote branch does not match recorded merge request SHA"
            )
        local_exists = bool(self._git("branch", "--list", branch))
        if local_exists:
            if self._git("rev-parse", branch) != expected_sha:
                raise RepositoryError(
                    "local branch does not match recorded merge request SHA"
                )
            self._git("worktree", "add", str(path), branch)
        else:
            self._git("worktree", "add", "-b", branch, str(path), f"origin/{branch}")
        return path

    def has_changes(self, worktree: Path) -> bool:
        """Return whether a worktree differs from its index."""
        return bool(self._git("status", "--porcelain", cwd=worktree))

    def head(self, worktree: Path) -> str:
        """Return the worktree's exact current commit."""
        return self._git("rev-parse", "HEAD", cwd=worktree)

    def changed_paths(self, worktree: Path) -> tuple[str, ...]:
        """Return paths changed relative to the index, including untracked paths."""
        return self._worktree_status(
            worktree, error_message="could not inspect changed paths"
        ).changed_paths

    def observe_worktree_status(self, worktree: Path) -> WorktreeStatus:
        """Observe dirty state and paths from one bounded NUL-safe Git status."""
        return self._worktree_status(
            worktree, error_message="could not inspect worktree status"
        )

    def _worktree_status(self, worktree: Path, *, error_message: str) -> WorktreeStatus:
        """Read and parse one bounded porcelain result for either public boundary."""
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "status",
                "--porcelain=v1",
                "-z",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise RepositoryError(error_message)
        return WorktreeStatus(
            dirty=bool(result.stdout),
            changed_paths=parse_porcelain_paths(result.stdout),
            porcelain=result.stdout,
        )

    def observe_legacy_publication_candidate(
        self, worktree: Path, *, status: WorktreeStatus | None = None
    ) -> LegacyPublicationCandidate:
        """Read the complete dirty candidate identity without changing Git state."""
        porcelain = (
            status.porcelain
            if status is not None
            else self._git(
                "status",
                "--porcelain=v1",
                "-z",
                cwd=worktree,
                preserve_output=True,
            )
        )

        entries = porcelain.split("\0")
        flags = [entry[:2] for entry in entries if entry]
        return LegacyPublicationCandidate(
            branch=self._git("branch", "--show-current", cwd=worktree),
            head=self.head(worktree),
            merge_head=self._merge_head(worktree),
            index_fingerprint=sha256(
                self._git(
                    "diff",
                    "--cached",
                    "--raw",
                    "-z",
                    cwd=worktree,
                    preserve_output=True,
                ).encode()
            ).hexdigest(),
            staged_paths=tuple(
                path
                for path in self._git(
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    cwd=worktree,
                    preserve_output=True,
                ).split("\0")
                if path
            ),
            unstaged_paths=tuple(
                path
                for path in self._git(
                    "diff",
                    "--name-only",
                    "-z",
                    cwd=worktree,
                    preserve_output=True,
                ).split("\0")
                if path
            ),
            has_untracked=any(flag == "??" for flag in flags),
            has_unmerged=any(
                flag in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"} for flag in flags
            ),
            has_renames=any("R" in flag for flag in flags),
        )

    def retained_worktree_fingerprint(self, worktree: Path) -> str:
        """Hash tracked unstaged bytes and file types without changing the index."""
        paths = tuple(
            path
            for path in self._git(
                "diff",
                "--name-only",
                "-z",
                cwd=worktree,
                preserve_output=True,
            ).split("\0")
            if path
        )
        digest = sha256()
        remaining_bytes = 64 * 1024 * 1024
        for path in sorted(set(paths)):
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts or not path:
                raise RepositoryError("retained worktree path is unsafe")
            if any(
                (worktree.joinpath(*relative.parts[:depth])).is_symlink()
                for depth in range(1, len(relative.parts))
            ):
                raise RepositoryError("retained worktree parent is a symlink")
            target = worktree.joinpath(*relative.parts)
            digest.update(path.encode())
            digest.update(b"\0")
            try:
                mode = target.lstat().st_mode
            except FileNotFoundError:
                digest.update(b"deleted\0")
                continue
            if stat.S_ISREG(mode):
                digest.update(b"file\0")
                digest.update(f"{stat.S_IMODE(mode):04o}".encode())
                digest.update(b"\0")
                file_digest = sha256()
                with target.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        remaining_bytes -= len(chunk)
                        if remaining_bytes < 0:
                            raise RepositoryError(
                                "retained worktree snapshot is too large"
                            )
                        file_digest.update(chunk)
                digest.update(file_digest.digest())
            elif stat.S_ISLNK(mode):
                digest.update(b"symlink\0")
                digest.update(os.readlink(target).encode())
            else:
                raise RepositoryError("retained worktree entry is unsupported")
            digest.update(b"\0")
        return digest.hexdigest()

    def single_parent(self, worktree: Path, candidate_sha: str) -> str | None:
        """Return the sole parent of one exact candidate commit, if it has one."""
        parents = self._git(
            "rev-list", "--parents", "-n", "1", candidate_sha, cwd=worktree
        ).split()
        if (
            len(parents) != 2
            or parents[0] != candidate_sha
            or not all(re.fullmatch(r"[0-9a-f]{40}", item) for item in parents)
        ):
            return None
        return parents[1]

    def run_red_command(
        self, worktree: Path, argv: tuple[str, ...]
    ) -> CommandResultProtocol:
        """Run one bounded shell-free Red command in its durable worktree."""
        return self._bounded_command(
            argv,
            environment=self._environment,
            current_directory=worktree,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )

    def checkpoint_tree(self, ref: str) -> str | None:
        """Resolve a local checkpoint ref to its exact tree, if it exists."""
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(self.config.checkout),
                "rev-parse",
                "-q",
                "--verify",
                f"{ref}^{{tree}}",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if result.returncode == 1:
            return None
        if result.returncode != 0 or not re.fullmatch(
            r"[0-9a-f]{40}", result.stdout.strip()
        ):
            raise RepositoryError("checkpoint ref could not be verified")
        return result.stdout.strip()

    def write_red_checkpoint_ref(
        self, worktree: Path, evidence: RedCheckpointEvidence
    ) -> None:
        """Atomically bind one evidence-bearing commit to its empty checkpoint ref."""
        message = evidence.model_dump_json()
        commit_environment = {
            **self._environment,
            "GIT_AUTHOR_NAME": "agent-work",
            "GIT_AUTHOR_EMAIL": "agent-work@localhost",
            "GIT_COMMITTER_NAME": "agent-work",
            "GIT_COMMITTER_EMAIL": "agent-work@localhost",
            "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        }
        commit = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "commit-tree",
                evidence.tree_oid,
                "-p",
                evidence.base_sha,
                "-m",
                message,
            ),
            environment=commit_environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        commit_oid = commit.stdout.strip()
        if commit.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", commit_oid):
            raise RepositoryError("red checkpoint commit could not be created")
        updated = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "update-ref",
                evidence.ref,
                commit_oid,
                "",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if updated.returncode != 0:
            raise RepositoryError("red checkpoint ref could not be created")

    def read_red_checkpoint_ref(
        self, worktree: Path, project_id: int, issue_iid: int, base_sha: str
    ) -> RedCheckpointEvidence | None:
        """Load and verify one atomic checkpoint commit without sidecar authority."""
        ref = checkpoint_ref(issue_iid, base_sha)
        resolved = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "rev-parse",
                "-q",
                "--verify",
                ref,
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if resolved.returncode == 1 and not resolved.stdout.strip():
            return None
        if resolved.returncode != 0 or not re.fullmatch(
            r"[0-9a-f]{40}", resolved.stdout.strip()
        ):
            raise RedCheckpointError("red checkpoint ref could not be verified")
        object_type = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "cat-file",
                "-t",
                resolved.stdout.strip(),
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if object_type.returncode != 0:
            raise RedCheckpointError("red checkpoint ref could not be verified")
        if object_type.stdout.strip() == "tree":
            return None
        if object_type.stdout.strip() != "commit":
            raise RedCheckpointError("red checkpoint ref has an invalid object type")
        object_size = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "cat-file",
                "-s",
                resolved.stdout.strip(),
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        try:
            size = int(object_size.stdout.strip())
        except ValueError as error:
            raise RedCheckpointError("red checkpoint object size is invalid") from error
        if object_size.returncode != 0 or not 0 < size <= 65_536:
            raise RedCheckpointError("red checkpoint object exceeds the durable bound")
        commit_oid = resolved.stdout.strip()
        body = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "show",
                "-s",
                "--format=%B",
                commit_oid,
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        parents = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "show",
                "-s",
                "--format=%P",
                commit_oid,
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        tree = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "rev-parse",
                f"{commit_oid}^{{tree}}",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if body.returncode != 0 or parents.returncode != 0 or tree.returncode != 0:
            raise RedCheckpointError("red checkpoint commit could not be read")
        try:
            evidence = RedCheckpointEvidence.model_validate_json(body.stdout)
        except ValueError as error:
            raise RedCheckpointError("red checkpoint evidence is malformed") from error
        if (
            evidence.project_id != project_id
            or evidence.issue_iid != issue_iid
            or evidence.base_sha != base_sha
            or evidence.ref != ref
            or evidence.tree_oid != tree.stdout.strip()
            or parents.stdout.strip() != base_sha
        ):
            raise RedCheckpointError("red checkpoint commit identity is invalid")
        return evidence

    def snapshot_test_tree(
        self, worktree: Path, base_sha: str, paths: tuple[str, ...]
    ) -> str:
        """Snapshot only declared tests over a base tree without publishing a ref."""
        descriptor, index_name = tempfile.mkstemp(prefix="agent-work-red-index-")
        os.close(descriptor)
        index_path = Path(index_name)
        index_path.unlink()
        environment = {**self._environment, "GIT_INDEX_FILE": str(index_path)}

        def indexed(*arguments: str) -> CommandResultProtocol:
            result = self._bounded_command(
                (
                    str(self.config.git_path),
                    "-C",
                    str(worktree),
                    *arguments,
                ),
                environment=environment,
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise RepositoryError("Red checkpoint snapshot failed")
            return result

        try:
            indexed("read-tree", base_sha)
            indexed("add", "--", *paths)
            tree_oid = indexed("write-tree").stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", tree_oid):
                raise RepositoryError("Red checkpoint tree identity is invalid")
            return tree_oid
        finally:
            index_path.unlink(missing_ok=True)

    def red_candidate_tree(
        self, worktree: Path, base_sha: str, paths: tuple[str, ...]
    ) -> str:
        """Observe exact current candidate content as an unpublished tree identity."""
        if not paths:
            return self._git("rev-parse", f"{base_sha}^{{tree}}", cwd=worktree)
        return self.snapshot_test_tree(worktree, base_sha, paths)

    def stage_candidate(
        self, worktree: Path, base_sha: str, *, allow_base_ancestor: bool = False
    ) -> str:
        """Stage only controller-observed paths and return the exact tree."""
        if self.head(worktree) != base_sha and not (
            allow_base_ancestor and self._is_ancestor(base_sha, "HEAD", worktree)
        ):
            raise RepositoryError("candidate head changed")
        paths = (
            self.publication_changed_paths(worktree, base_sha)
            if allow_base_ancestor
            else tuple(sorted(set(self.changed_paths(worktree))))
        )
        if len(paths) > 128:
            raise RepositoryError("candidate changed paths are invalid")
        if paths:
            self._git("add", "--all", "--", *paths, cwd=worktree)
        tree = self._git("write-tree", cwd=worktree)
        if not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise RepositoryError("candidate tree is invalid")
        if (
            self.publication_changed_paths(worktree, base_sha)
            if allow_base_ancestor
            else tuple(sorted(set(self.changed_paths(worktree))))
        ) != paths:
            raise RepositoryError("candidate changed paths changed during staging")
        if self.red_candidate_tree(worktree, base_sha, paths) != tree:
            raise RepositoryError("candidate changed during staging")
        return tree

    def read_candidate(
        self,
        worktree: Path,
        candidate_tree: str,
        *,
        base_sha: str,
        allow_base_ancestor: bool = False,
        changed_paths: tuple[str, ...],
    ) -> dict[str, object]:
        """Read bounded exact base-to-tree text from controller-owned Git objects."""
        if (
            self.head(worktree) != base_sha
            and not (
                allow_base_ancestor and self._is_ancestor(base_sha, "HEAD", worktree)
            )
        ) or self._git("write-tree", cwd=worktree) != candidate_tree:
            raise RepositoryError("candidate changed")

        def git_output(*args: str) -> str:
            try:
                result = self._bounded_command(
                    (str(self.config.git_path), "-C", str(worktree), *args),
                    environment=self._environment,
                    timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
                )
            except UnicodeError as error:
                raise RepositoryError(
                    "candidate content is oversize or binary"
                ) from error
            if result.returncode != 0:
                raise RepositoryError("candidate read failed")
            return result.stdout

        base_tree = self._git("rev-parse", f"{base_sha}^{{tree}}", cwd=worktree)
        if not re.fullmatch(r"[0-9a-f]{40}", base_tree):
            raise RepositoryError("candidate base tree is invalid")
        names = git_output(
            "diff", "--name-only", "-z", "--no-renames", base_tree, candidate_tree
        )
        paths = tuple(sorted(name for name in names.split("\0") if name))
        try:
            validate_git_read_paths(paths)
        except CandidateBoundaryError as error:
            raise RepositoryError(str(error)) from error
        if paths != changed_paths:
            raise RepositoryError("candidate changed paths are invalid")

        def content(tree: str, path: str) -> tuple[str | None, str | None, str | None]:
            entry = git_output("ls-tree", "-z", tree, "--", path)
            if not entry:
                return None, None, None
            fields = entry.removesuffix("\0").split("\t", 1)
            if len(fields) != 2 or fields[1] != path:
                raise RepositoryError("candidate object is invalid")
            metadata = fields[0].split(" ")
            if (
                len(metadata) != 3
                or metadata[0] not in {"100644", "100755"}
                or metadata[1] != "blob"
                or re.fullmatch(r"[0-9a-f]{40}", metadata[2]) is None
            ):
                raise RepositoryError("candidate object is invalid")
            size_text = git_output("cat-file", "-s", metadata[2]).strip()
            if not size_text.isdecimal():
                raise RepositoryError("candidate content is oversize or binary")
            try:
                validate_git_read_size(int(size_text), 0)
            except CandidateBoundaryError as error:
                raise RepositoryError(str(error)) from error
            blob = self._read_git_blob(
                (
                    str(self.config.git_path),
                    "-C",
                    str(worktree),
                    "cat-file",
                    "blob",
                    metadata[2],
                ),
                max_output_bytes=int(size_text),
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
                environment=self._environment,
            )
            if len(blob) != int(size_text) or b"\0" in blob:
                raise RepositoryError("candidate content is oversize or binary")
            try:
                value = blob.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RepositoryError(
                    "candidate content is oversize or binary"
                ) from error
            return metadata[0], metadata[2], value

        files: list[dict[str, str | None]] = []
        total = 0
        for path in paths:
            old_mode, old_oid, old = content(base_tree, path)
            new_mode, new_oid, new = content(candidate_tree, path)
            if old is None and new is None:
                raise RepositoryError("candidate object is invalid")
            try:
                total = validate_git_read_size(
                    len((old or "").encode()) + len((new or "").encode()),
                    total,
                    per_blob=False,
                )
            except CandidateBoundaryError as error:
                raise RepositoryError(str(error)) from error
            files.append(
                {
                    "path": path,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_oid": old_oid,
                    "new_oid": new_oid,
                    "old_content": old,
                    "new_content": new,
                }
            )
        if (
            (
                self.head(worktree) != base_sha
                and not (
                    allow_base_ancestor
                    and self._is_ancestor(base_sha, "HEAD", worktree)
                )
            )
            or (
                self.publication_changed_paths(worktree, base_sha)
                if allow_base_ancestor
                else tuple(sorted(set(self.changed_paths(worktree))))
            )
            != changed_paths
            or self._git("write-tree", cwd=worktree) != candidate_tree
            or self.red_candidate_tree(worktree, base_sha, changed_paths)
            != candidate_tree
        ):
            raise RepositoryError("candidate changed")
        return {
            "candidate_tree": candidate_tree,
            "base_tree": base_tree,
            "changed_paths": list(paths),
            "files": files,
        }

    def judge_candidate(
        self,
        worktree: Path,
        candidate_tree: str,
        *,
        base_sha: str,
        allow_base_ancestor: bool = False,
        changed_paths: tuple[str, ...],
        action: str,
        timeout_seconds: int,
        timing_review_required: bool = False,
    ) -> dict[str, object]:
        """Run or observe durable Judge evidence from the trusted checkout."""
        if action not in {"validate", "status"}:
            raise RepositoryError("candidate action is invalid")
        if self._git("write-tree", cwd=worktree) != candidate_tree:
            raise RepositoryError("candidate tree changed")
        result = self._bounded_command(
            (
                str(self.config.checkout / "bin" / "agent-work-from-checkout"),
                "validation",
                "run" if action == "validate" else "status",
                "--candidate-tree",
                candidate_tree,
                *(("--serial-timing",) if timing_review_required else ()),
                *(("--baseline-sha", base_sha) if timing_review_required else ()),
                *(
                    part
                    for path in changed_paths
                    if timing_review_required
                    for part in ("--changed-path", path)
                ),
                *(("--apply",) if action == "validate" else ()),
                "--json",
            ),
            current_directory=worktree,
            environment=self._environment,
            timeout_seconds=timeout_seconds,
        )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RepositoryError(
                "candidate validation response is malformed"
            ) from error
        if (
            result.returncode not in {0, 1}
            or not isinstance(report, dict)
            or report.get("candidate_tree") != candidate_tree
        ):
            raise RepositoryError("candidate validation response is invalid")
        if report.get("state") == "deferred":
            if (
                action != "validate"
                or result.returncode != 1
                or report
                != {
                    "candidate_tree": candidate_tree,
                    "state": "deferred",
                    "reason": "project_timing_admission_busy",
                }
            ):
                raise RepositoryError("candidate validation response is invalid")
            return {"candidate_tree": candidate_tree, "state": "deferred"}
        artifacts = report.get("junit_artifacts")
        if not isinstance(artifacts, list) or len(artifacts) > 2:
            raise RepositoryError("candidate validation evidence is invalid")
        safe_artifacts: list[dict[str, object]] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("environment") not in {
                "py311",
                "py314",
            }:
                raise RepositoryError("candidate validation evidence is invalid")
            sha = artifact.get("sha256")
            count = artifact.get("test_count")
            if (
                not isinstance(sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha) is None
                or not isinstance(count, int)
                or count < 0
            ):
                raise RepositoryError("candidate validation evidence is invalid")
            safe_artifacts.append(
                {
                    "environment": artifact["environment"],
                    "sha256": sha,
                    "test_count": count,
                }
            )
        state = report.get("state")
        if state not in {
            "preflight-failure",
            "running",
            "terminal-success",
            "terminal-failure",
            "terminal-artifact-failure",
        }:
            raise RepositoryError("candidate validation state is invalid")
        if (state == "terminal-success") != (result.returncode == 0):
            raise RepositoryError("candidate validation exit is inconsistent")
        raw_failure = report.get("artifact_failure")
        safe_failure: dict[str, str] | None = None
        if state == "terminal-artifact-failure":
            allowed_reasons = {
                "pre_hook_cleanup": {"os_error"},
                "post_hook_failed_evidence_read": {"os_error"},
                "post_hook_failed_cleanup": {"os_error"},
                "post_hook_success_evidence_read": {
                    "os_error",
                    "missing",
                    "invalid_xml",
                    "oversized",
                    "invalid_count",
                },
                "post_hook_success_cleanup": {"os_error"},
                "immutable_snapshot": {"os_error", "invalid_snapshot"},
                "timing_review": {
                    "invalid_timing",
                    "finding_budget_exhausted",
                    "focused_budget_exhausted",
                },
            }
            if not isinstance(raw_failure, dict) or set(raw_failure) != {
                "stage",
                "reason",
                "environment",
                "path",
            }:
                raise RepositoryError("candidate artifact failure is invalid")
            failure_stage = raw_failure["stage"]
            failure_reason = raw_failure["reason"]
            failure_environment = raw_failure["environment"]
            if (
                not isinstance(failure_stage, str)
                or failure_stage not in allowed_reasons
                or not isinstance(failure_reason, str)
                or failure_reason not in allowed_reasons[failure_stage]
                or failure_environment is not None
                and failure_environment not in ("py311", "py314")
                or not isinstance(raw_failure["path"], (str, type(None)))
                or (failure_environment is None) != (raw_failure["path"] is None)
                or failure_environment is not None
                and raw_failure["path"] != f"test-results-{failure_environment}.xml"
            ):
                raise RepositoryError("candidate artifact failure is invalid")
            safe_failure = {
                "stage": failure_stage,
                "reason": failure_reason,
            }
        elif raw_failure is not None:
            raise RepositoryError("candidate artifact failure is invalid")
        timing_failure_code = report.get("timing_failure_code")
        if timing_failure_code is not None and (
            not isinstance(timing_failure_code, str)
            or timing_failure_code
            not in {
                "baseline_hook_failed",
                "focused_timing_command_failed",
                "capture_failed",
            }
            or safe_failure is None
            or safe_failure["stage"] != "timing_review"
        ):
            raise RepositoryError("candidate timing failure code is invalid")
        timing_status = report.get("timing_status")
        timing_unknown = report.get("timing_unknown")
        if timing_status is not None and timing_status not in (
            "complete",
            "unknown",
        ):
            raise RepositoryError("candidate timing evidence is invalid")
        if timing_status == "unknown":
            if (
                state != "terminal-success"
                or report.get("exit_code") != 0
                or not isinstance(timing_unknown, dict)
                or set(timing_unknown) != {"environment", "reason"}
                or timing_unknown["environment"] not in ("py311", "py314")
                or timing_unknown["reason"] not in ("os_error", "missing")
                or safe_artifacts
                or report.get("junit_timings") != []
                or report.get("timing_review") is not None
            ):
                raise RepositoryError("candidate timing evidence is invalid")
        elif timing_unknown is not None or (
            timing_status == "complete" and state != "terminal-success"
        ):
            raise RepositoryError("candidate timing evidence is invalid")
        if (
            timing_review_required
            and state == "terminal-success"
            and timing_status != "unknown"
            and {item["environment"] for item in safe_artifacts} != {"py311", "py314"}
        ):
            raise RepositoryError("candidate timing proof is invalid")
        timing_review = report.get("timing_review")
        safe_timing_review: dict[str, object] | None = None
        if (
            timing_review_required
            and state == "terminal-success"
            and timing_status != "unknown"
        ):
            if (
                not isinstance(timing_review, dict)
                or timing_review.get("baseline_sha") != base_sha
                or timing_review.get("candidate_tree") != candidate_tree
                or not isinstance(timing_review.get("baseline_tree"), str)
                or re.fullmatch(r"[0-9a-f]{40}", timing_review["baseline_tree"]) is None
                or timing_review.get("outcome")
                not in {
                    "clear",
                    "report_only",
                    "correction_required",
                }
                or not isinstance(timing_review.get("transcript_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", timing_review["transcript_sha256"])
                is None
            ):
                raise RepositoryError("candidate timing proof is invalid")
            runs = timing_review.get("runs")
            if not isinstance(runs, dict) or set(runs) != {"py311", "py314"}:
                raise RepositoryError("candidate timing proof is invalid")
            safe_runs: dict[str, dict[str, dict[str, object]]] = {}
            for environment, pair in runs.items():
                if not isinstance(pair, dict) or set(pair) != {"baseline", "candidate"}:
                    raise RepositoryError("candidate timing proof is invalid")
                safe_pair: dict[str, dict[str, object]] = {}
                for source, run in pair.items():
                    if not isinstance(run, dict):
                        raise RepositoryError("candidate timing proof is invalid")
                    digest = run.get("sha256")
                    count = run.get("test_count")
                    if (
                        not isinstance(digest, str)
                        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                        or not isinstance(count, int)
                        or isinstance(count, bool)
                        or not 1 <= count <= 8_192
                        or run.get("timing_complete") is not True
                    ):
                        raise RepositoryError("candidate timing proof is invalid")
                    safe_pair[source] = {
                        "sha256": digest,
                        "test_count": count,
                        "timing_complete": True,
                    }
                safe_runs[environment] = safe_pair
            safe_findings: list[dict[str, object]] = []
            findings = timing_review.get("findings")
            if not isinstance(findings, list) or len(findings) > 128:
                raise RepositoryError("candidate timing proof is invalid")
            omitted_findings = timing_review.get("omitted_findings", 0)
            if (
                not isinstance(omitted_findings, int)
                or isinstance(omitted_findings, bool)
                or not 0 <= omitted_findings <= 32_768
            ):
                raise RepositoryError("candidate timing proof is invalid")
            for finding in findings:
                if not isinstance(finding, dict):
                    raise RepositoryError("candidate timing proof is invalid")
                name = finding.get("name")
                reason = finding.get("reason")
                environment = finding.get("environment")
                deltas = finding.get("phase_deltas")
                if (
                    environment not in {"py311", "py314"}
                    or not isinstance(name, str)
                    or not is_safe_timing_case_id(name)
                    or reason
                    not in {
                        "new_slow",
                        "touched_slow",
                        "inherited_slow",
                        "corroborated_regression",
                        "uncorroborated_spike",
                    }
                    or deltas is not None
                    and (
                        not isinstance(deltas, dict)
                        or set(deltas) != {"setup", "call", "teardown"}
                        or any(
                            not isinstance(value, (int, float))
                            or isinstance(value, bool)
                            or not math.isfinite(value)
                            or abs(value) > 1_000_000
                            for value in deltas.values()
                        )
                    )
                ):
                    raise RepositoryError("candidate timing proof is invalid")
                safe_findings.append(
                    {
                        "environment": environment,
                        "name": name,
                        "reason": reason,
                        "phase_deltas": deltas,
                    }
                )
            if len(json.dumps(safe_findings, separators=(",", ":")).encode()) > 65_536:
                raise RepositoryError("candidate timing proof is invalid")
            corrective = {"new_slow", "corroborated_regression"}
            has_correction = any(
                finding["reason"] in corrective for finding in safe_findings
            )
            if (
                timing_review["outcome"] == "clear"
                and (safe_findings or omitted_findings)
                or timing_review["outcome"] == "correction_required"
                and not has_correction
                or timing_review["outcome"] == "report_only"
                and (not (safe_findings or omitted_findings) or has_correction)
            ):
                raise RepositoryError("candidate timing proof is invalid")
            safe_timing_review = {
                "baseline_sha": base_sha,
                "baseline_tree": timing_review["baseline_tree"],
                "candidate_tree": candidate_tree,
                "outcome": timing_review["outcome"],
                "transcript_sha256": timing_review["transcript_sha256"],
                "runs": safe_runs,
                "findings": safe_findings,
            }
            if "omitted_findings" in timing_review:
                safe_timing_review["omitted_findings"] = omitted_findings
            for artifact in safe_artifacts:
                environment = str(artifact["environment"])
                candidate_run = safe_runs[environment]["candidate"]
                if (
                    candidate_run["sha256"] != artifact["sha256"]
                    or candidate_run["test_count"] != artifact["test_count"]
                ):
                    raise RepositoryError("candidate timing proof is invalid")
        failed_tests = report.get("failed_tests")
        failed_digests = report.get("failed_junit_digests")
        if (
            not isinstance(failed_tests, list)
            or len(failed_tests) > 16
            or any(
                not isinstance(name, str) or not is_safe_junit_test_name(name)
                for name in failed_tests
            )
            or not isinstance(failed_digests, list)
            or len(failed_digests) > 2
            or any(
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in failed_digests
            )
        ):
            raise RepositoryError("candidate failure evidence is invalid")
        if timing_status == "unknown" and (failed_tests or failed_digests):
            raise RepositoryError("candidate timing evidence is invalid")
        elapsed = report.get("elapsed_seconds")
        if elapsed is not None and (
            not isinstance(elapsed, (float, int))
            or not math.isfinite(elapsed)
            or elapsed < 0
            or elapsed > 10_800
        ):
            raise RepositoryError("candidate timing evidence is invalid")
        raw_timings = report.get("junit_timings")
        if not isinstance(raw_timings, list) or len(raw_timings) > 2:
            raise RepositoryError("candidate JUnit timing evidence is invalid")
        timing_name_is_safe = (
            is_safe_timing_case_id
            if timing_review_required
            else is_safe_junit_test_name
        )
        safe_timings: list[dict[str, object]] = []
        for item in raw_timings:
            if not isinstance(item, dict) or item.get("environment") not in {
                "py311",
                "py314",
            }:
                raise RepositoryError("candidate JUnit timing evidence is invalid")
            total = item.get("total_seconds")
            cases = item.get("slowest_cases")
            timing_complete = item.get("timing_complete")
            over_100ms_cases = item.get("over_100ms_cases")
            if (
                not isinstance(total, (int, float))
                or not math.isfinite(total)
                or total < 0
                or total > 1_000_000
                or not isinstance(cases, list)
                or len(cases) > 3
                or not isinstance(timing_complete, bool)
                or not isinstance(over_100ms_cases, list)
                or len(over_100ms_cases) > MAX_TIMING_CASES
            ):
                raise RepositoryError("candidate JUnit timing evidence is invalid")
            safe_cases: list[dict[str, object]] = []
            for case in cases:
                if not isinstance(case, dict):
                    raise RepositoryError("candidate JUnit timing evidence is invalid")
                name = case.get("name")
                seconds = case.get("seconds")
                if (
                    not isinstance(name, str)
                    or not timing_name_is_safe(name)
                    or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds)
                    or seconds < 0
                    or seconds > 1_000_000
                ):
                    raise RepositoryError("candidate JUnit timing evidence is invalid")
                safe_cases.append({"name": name, "seconds": seconds})
            safe_over_100ms: list[dict[str, object]] = []
            for case in over_100ms_cases:
                if not isinstance(case, dict):
                    raise RepositoryError("candidate JUnit timing evidence is invalid")
                name = case.get("name")
                seconds = case.get("seconds")
                if (
                    not isinstance(name, str)
                    or not timing_name_is_safe(name)
                    or len(name.encode("utf-8")) > MAX_TIMING_CASE_NAME_BYTES
                    or not isinstance(seconds, (int, float))
                    or not math.isfinite(seconds)
                    or not 0.100 < seconds <= 1_000_000
                ):
                    raise RepositoryError("candidate JUnit timing evidence is invalid")
                safe_over_100ms.append({"name": name, "seconds": seconds})
            if (
                len(json.dumps(safe_over_100ms, separators=(",", ":")).encode("utf-8"))
                > MAX_TIMING_CASE_LIST_BYTES
            ):
                raise RepositoryError("candidate JUnit timing evidence is invalid")
            safe_timings.append(
                {
                    "environment": item["environment"],
                    "total_seconds": total,
                    "slowest_cases": safe_cases,
                    "timing_complete": timing_complete,
                    "over_100ms_cases": safe_over_100ms,
                }
            )
        if (
            (
                self.head(worktree) != base_sha
                and not (
                    allow_base_ancestor
                    and self._is_ancestor(base_sha, "HEAD", worktree)
                )
            )
            or self._git("write-tree", cwd=worktree) != candidate_tree
            or (
                self.publication_changed_paths(worktree, base_sha)
                if allow_base_ancestor
                else tuple(sorted(set(self.changed_paths(worktree))))
            )
            != changed_paths
            or self.red_candidate_tree(worktree, base_sha, changed_paths)
            != candidate_tree
        ):
            raise RepositoryError("candidate changed during validation")
        durable_evidence = {
            key: report.get(key)
            for key in (
                "candidate_tree",
                "state",
                "exit_code",
                "diagnostic",
                "hook_failures",
                "junit_artifacts",
                "failed_tests",
                "failed_junit_digests",
                "elapsed_seconds",
                "junit_timings",
                "timing_review",
                "timing_status",
                "timing_unknown",
                "artifact_failure",
            )
        }
        durable_evidence["artifact_failure"] = safe_failure
        if timing_failure_code is not None:
            durable_evidence["timing_failure_code"] = timing_failure_code
        return {
            "candidate_tree": candidate_tree,
            "state": state,
            "exit_code": report.get("exit_code")
            if isinstance(report.get("exit_code"), int)
            else None,
            "diagnostic": str(report.get("diagnostic") or "")[:512],
            "hook_failures": report.get("hook_failures")
            if isinstance(report.get("hook_failures"), list)
            else [],
            "junit_artifacts": safe_artifacts,
            "failed_tests": failed_tests,
            "failed_junit_digests": failed_digests,
            "elapsed_seconds": elapsed,
            "junit_timings": safe_timings,
            "timing_review": safe_timing_review,
            "timing_status": timing_status,
            "timing_unknown": timing_unknown,
            "artifact_failure": safe_failure,
            **(
                {"timing_failure_code": timing_failure_code}
                if timing_failure_code is not None
                else {}
            ),
            "evidence_id": sha256(
                json.dumps(
                    durable_evidence, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        }

    def publication_changed_paths(
        self, worktree: Path, target_sha: str
    ) -> tuple[str, ...]:
        """Return the prospective published tree's paths relative to its target."""
        target = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(worktree),
                "rev-parse",
                "-q",
                "--verify",
                f"{target_sha}^{{commit}}",
            ),
            environment=self._environment,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        )
        if target.returncode != 0 or target.stdout.strip() != target_sha:
            raise RepositoryError("publication target is not an exact commit")
        merge_head = self._merge_head(worktree)
        if merge_head is not None:
            if merge_head != target_sha:
                raise RepositoryError("pending merge does not match publication target")
        elif not self._is_ancestor(target_sha, "HEAD", worktree):
            raise RepositoryError("publication target is not an ancestor of HEAD")
        if self._git("ls-files", "--unmerged", cwd=worktree):
            raise RepositoryError("publication tree has unresolved paths")
        changed = self._git(
            "diff", "--name-only", "--no-renames", target_sha, cwd=worktree
        ).splitlines()
        untracked = self._git(
            "ls-files", "--others", "--exclude-standard", cwd=worktree
        ).splitlines()
        return tuple(sorted(set(changed) | set(untracked)))

    def worktree_exists(self, worktree: Path) -> bool:
        """Return whether the durable worktree directory still exists."""
        return worktree.is_dir()

    def verify_worktree(self, worktree: Path, branch: str, expected_head: str) -> None:
        """Verify the durable worktree still owns its recorded baseline."""
        if self._git("branch", "--show-current", cwd=worktree) != branch:
            raise RepositoryError("durable worktree branch changed")
        if self.head(worktree) != expected_head:
            raise RepositoryError("durable worktree baseline changed")

    def sync_feedback_worktree(
        self, worktree: Path, branch: str, old_sha: str, new_sha: str
    ) -> None:
        """Fast-forward an existing clean feedback worktree to its exact MR SHA."""
        if self.has_changes(worktree):
            raise RepositoryError("durable worktree must be clean")
        if self._git("branch", "--show-current", cwd=worktree) != branch:
            raise RepositoryError("durable worktree branch changed")
        head = self.head(worktree)
        if head not in {old_sha, new_sha}:
            raise RepositoryError("feedback worktree baseline changed")
        if head == new_sha:
            return
        self._git("fetch", "origin", branch, cwd=worktree, transient=True)
        if self._git("rev-parse", f"origin/{branch}", cwd=worktree) != new_sha:
            raise RepositoryError(
                "feedback remote branch does not match merge request SHA"
            )
        self._git("merge", "--ff-only", f"origin/{branch}", cwd=worktree)
        self.verify_worktree(worktree, branch, new_sha)

    def observe_current_default(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str:
        """Verify the retained baseline and return the freshly fetched default SHA."""
        self.verify_worktree(worktree, branch, expected_head)
        self._git(
            "fetch", "origin", self.config.default_branch, cwd=worktree, transient=True
        )
        return self._git(
            "rev-parse", f"origin/{self.config.default_branch}", cwd=worktree
        )

    def reconcile_default_branch(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str | None:
        """Start or recover the exact merge needed to include current default."""
        default_sha = self.observe_current_default(worktree, branch, expected_head)
        merge_head = self._merge_head(worktree)
        if merge_head:
            if not self._is_ancestor(merge_head, default_sha, worktree):
                raise RepositoryError("pending merge does not target current default")
            return merge_head
        if self.has_changes(worktree):
            raise RepositoryError("publication worktree has uncommitted changes")
        if not self._is_ancestor(default_sha, expected_head, worktree):
            self._merge_current_default(default_sha, worktree)
            merge_head = self._merge_head(worktree)
            if merge_head != default_sha:
                raise RepositoryError("pending merge does not target fetched default")
            return default_sha
        return None

    def prepare_commit(
        self,
        worktree: Path,
        branch: str,
        issue_iid: int,
        *,
        expected_head: str,
        validated_paths: tuple[str, ...] | None = None,
    ) -> str:
        """Create exactly one runner commit and return its unpushed SHA."""
        current_branch = self._git("branch", "--show-current", cwd=worktree)
        if current_branch != branch:
            raise RepositoryError("publication worktree is on the wrong branch")
        current_head = self.head(worktree)
        merge_head = self._merge_head(worktree)
        resolved_merge = merge_head and not self._git(
            "ls-files", "--unmerged", cwd=worktree
        )
        if current_head == expected_head and (resolved_merge or not merge_head):
            if validated_paths is None:
                self._git("add", "--all", cwd=worktree)
            else:
                self._git("add", "--all", "--", *validated_paths, cwd=worktree)
            retained_changes = self._git("status", "--porcelain", cwd=worktree)
            candidate_tree = self._git("write-tree", cwd=worktree)
            if not resolved_merge and not retained_changes:
                raise RepositoryError("worker made no changes to publish")
            result = self._bounded_command(
                (
                    str(self.config.git_path),
                    "-C",
                    str(worktree),
                    "commit",
                    "-m",
                    f"Resolve issue #{issue_iid}",
                ),
                environment=self._environment,
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                failures = parse_installed_precommit_failures(
                    result.stdout, result.stderr
                )
                retained_identity = (
                    self._git("branch", "--show-current", cwd=worktree),
                    self.head(worktree),
                    self._merge_head(worktree),
                    self._git("write-tree", cwd=worktree),
                )
                if retained_identity != (
                    current_branch,
                    current_head,
                    merge_head,
                    candidate_tree,
                ) or not self._git("status", "--porcelain", cwd=worktree):
                    raise RepositoryError(
                        "publication identity changed while the commit hook failed"
                    )
                if not failures:
                    raise RepositoryError("Git operation failed")
                raise PublicationCommitRejected(
                    PublicationCommitFailure(
                        diagnostic=_publication_commit_diagnostic(failures),
                    )
                )
        else:
            message = (
                "worker-created commits cannot be published"
                if current_head != expected_head
                else (
                    "pending merge is not resolved"
                    if merge_head
                    else "worker made no changes to publish"
                )
            )
            raise RepositoryError(message)
        prepared = self._git("rev-parse", "HEAD", cwd=worktree)
        parents = self._git(
            "rev-list", "--parents", "-n", "1", prepared, cwd=worktree
        ).split()
        expected_parents = (
            [prepared, expected_head, merge_head]
            if merge_head
            else [prepared, expected_head]
        )
        if parents != expected_parents:
            raise RepositoryError("prepared publication merge parents are not exact")
        return prepared

    def push_prepared(self, worktree: Path, branch: str, publication_sha: str) -> str:
        """Push only the exact durable prepared commit."""
        if self._git("branch", "--show-current", cwd=worktree) != branch:
            raise RepositoryError("publication worktree is on the wrong branch")
        if self.has_changes(worktree) or self.head(worktree) != publication_sha:
            raise RepositoryError("prepared publication commit no longer matches")
        self._git(
            "push",
            "origin",
            f"{publication_sha}:refs/heads/{branch}",
            cwd=worktree,
            transient=True,
        )
        remote_sha = self._git("rev-parse", f"origin/{branch}", cwd=worktree)
        if publication_sha != remote_sha:
            raise RepositoryError("published branch SHA could not be verified")
        return publication_sha

    def bind_retained_commit(
        self, worktree: Path, branch: str, issue_iid: int, baseline_sha: str
    ) -> str:
        """Bind a legacy retained runner commit only after strict verification."""
        if (
            self.has_changes(worktree)
            or self._git("branch", "--show-current", cwd=worktree) != branch
        ):
            raise RepositoryError("retained publication worktree is not exact")
        head = self.head(worktree)
        parents = self._git(
            "rev-list", "--parents", "-n", "1", head, cwd=worktree
        ).split()
        if parents == [head, baseline_sha]:
            pass
        elif len(parents) == 3 and parents[1] == baseline_sha:
            self._git("fetch", "origin", self.config.default_branch, transient=True)
            current_default = self._git(
                "rev-parse", f"origin/{self.config.default_branch}", cwd=worktree
            )
            if not self._is_ancestor(parents[2], current_default, worktree):
                raise RepositoryError("retained merge does not include current default")
        else:
            raise RepositoryError(
                "retained publication commit is not based on baseline"
            )
        expected_count = "1"
        if (
            self._git(
                "rev-list",
                "--first-parent",
                "--count",
                f"{baseline_sha}..{head}",
                cwd=worktree,
            )
            != expected_count
        ):
            raise RepositoryError("retained publication history is not exact")
        if (
            self._git("log", "-1", "--format=%s", head, cwd=worktree)
            != f"Resolve issue #{issue_iid}"
        ):
            raise RepositoryError("retained publication commit identity is not exact")
        return head

    def inventory(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Return legacy branches, worktrees, and stash subjects without mutation."""
        branches = tuple(
            line.strip()
            for line in self._git(
                "for-each-ref", "--format=%(refname:short)", "refs/heads/agent"
            ).splitlines()
            if line.strip()
        )
        worktree_payload = self._git("worktree", "list", "--porcelain")
        worktrees = tuple(
            line.removeprefix("worktree ")
            for line in worktree_payload.splitlines()
            if line.startswith("worktree ")
        )
        stashes = tuple(
            line.strip()
            for line in self._git("stash", "list", "--format=%gs").splitlines()
            if line.strip()
        )
        return branches, worktrees, stashes
