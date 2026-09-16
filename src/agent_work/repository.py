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
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import ValidationError

from .domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS, ErrorCode
from .ledger import project_state_path
from .models import AgentWorkConfig
from .ports import CommandResult as CommandResultProtocol
from .ports import CommandRunner


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
    service_environment: Mapping[str, str], glab_path: Path
) -> dict[str, str]:
    """Build the noninteractive, glab-only credential environment for Git."""
    environment = dict(service_environment)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    for key in tuple(environment):
        if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del environment[key]
    environment["GIT_ASKPASS"] = "/bin/false"
    environment["SSH_ASKPASS"] = "/bin/false"
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": f"{glab_path} auth git-credential",
            "GIT_CONFIG_KEY_2": "credential.useHttpPath",
            "GIT_CONFIG_VALUE_2": "true",
            "GIT_CONFIG_KEY_3": "core.askPass",
            "GIT_CONFIG_VALUE_3": "/bin/false",
        }
    )
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
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CommandTimeoutError("a required command timed out") from error
    except OSError as error:
        raise RepositoryError("a required command could not be completed") from error
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


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


class GitRepository:
    """Own durable worktrees and publication Git operations."""

    def __init__(
        self,
        config: AgentWorkConfig,
        run_command: CommandRunner,
        *,
        service_environment: Mapping[str, str] | None = None,
        diagnostic_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.config = config
        self._run_command = run_command
        self._environment = build_git_environment(
            service_environment or {}, config.glab_path
        )
        self._diagnostic_sink = diagnostic_sink or (lambda _diagnostic: None)

    def _bounded_command(
        self,
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResultProtocol:
        """Run one Git command and preserve timeout retry semantics."""
        try:
            return self._run_command(
                command,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
        except CommandTimeoutError as error:
            raise RepositoryTransientError("Git operation timed out") from error

    def _git(
        self, *arguments: str, cwd: Path | None = None, transient: bool = False
    ) -> str:
        result = self._bounded_command(
            (
                str(self.config.git_path),
                "-C",
                str(cwd or self.config.checkout),
                *arguments,
            ),
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
        return result.stdout.strip()

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
            actual = self._git("branch", "--show-current", cwd=path)
            if actual != branch:
                raise RepositoryError("worktree ownership is ambiguous")
            return path
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
        return tuple(
            path.removeprefix('"').removesuffix('"')
            for line in self._git("status", "--porcelain", cwd=worktree).splitlines()
            if len(line) > 3 and line[3:].strip()
            for path in line[3:].strip().split(" -> ")
        )

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

    def reconcile_default_branch(
        self, worktree: Path, branch: str, expected_head: str
    ) -> str | None:
        """Start or recover the exact merge needed to include current default."""
        self.verify_worktree(worktree, branch, expected_head)
        self._git("fetch", "origin", self.config.default_branch, transient=True)
        default_sha = self._git(
            "rev-parse", f"origin/{self.config.default_branch}", cwd=worktree
        )
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
    ) -> str:
        """Create exactly one runner commit and return its unpushed SHA."""
        if self._git("branch", "--show-current", cwd=worktree) != branch:
            raise RepositoryError("publication worktree is on the wrong branch")
        current_head = self.head(worktree)
        merge_head = self._merge_head(worktree)
        resolved_merge = merge_head and not self._git(
            "ls-files", "--unmerged", cwd=worktree
        )
        ordinary_candidate = not merge_head and self.has_changes(worktree)
        if current_head == expected_head and (resolved_merge or ordinary_candidate):
            self._git("add", "--all", cwd=worktree)
            self._git("commit", "-m", f"Resolve issue #{issue_iid}", cwd=worktree)
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
