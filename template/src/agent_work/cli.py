"""The ``agent-work`` command-line entry point for bounded work items."""

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .adapters import (
    AdapterFailure,
    CodexInvoker,
    CommandResult,
    GitLabGateway,
    LocalRunStore,
    WorktreeGateway,
    load_environment_config,
    load_json_schema,
    run_subprocess,
)
from .filesystem import (
    FileIntake,
    FileIntakeFailure,
    FileIntakeReport,
    FilesystemDependencies,
    LocalInboxOperations,
    RepositoryTarget,
    durable_finalize_bundle,
)
from .models import GitLabConfig, GitLabIssue, GitLabProject, WorkItem, validate_alias
from .result import AgentResult
from .runner import RunDependencies, RunFailure, RunReport, run_normalized
from .runner import run_issue as run_one_issue


class RunStore(Protocol):
    """The local state-store operations used by concrete CLI wiring."""

    root: Path

    def lock(self, *, blocking: bool) -> AbstractContextManager[None]:
        """Acquire the local runner lock."""

    def append_transition(self, transition: Mapping[str, object], /) -> None:
        """Append one transition record."""

    def write_canonical_result(
        self, run_id: str, result: Mapping[str, object], /
    ) -> Path:
        """Persist one create-only canonical result document."""

    def write_recovery_record(
        self, run_id: str, record: Mapping[str, object], /
    ) -> Path:
        """Persist a separate recovery record."""

    def write_workspace_provenance(
        self, work_item_id: str, provenance: Mapping[str, object], /
    ) -> Path:
        """Persist runner-owned workspace provenance."""

    def read_workspace_provenance(
        self, work_item_id: str, /
    ) -> Mapping[str, object] | None:
        """Load runner-owned workspace provenance."""


def _stdout(message: str) -> None:
    """Write normal command output to standard output."""
    sys.stdout.write(f"{message}\n")


def _stderr(message: str) -> None:
    """Write diagnostics to standard error."""
    sys.stderr.write(f"{message}\n")


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_default_dependencies(
    *,
    repository: Path,
    run_command: Callable[..., CommandResult] = run_subprocess,
    environment: Mapping[str, str] = os.environ,
    run_store: RunStore | None = None,
    config_override: GitLabConfig | None = None,
) -> RunDependencies:
    """Wire real subprocess and local-state adapters while retaining DI in the runner."""
    source_root: Path | None = None
    common_git_dir: Path | None = None
    store = run_store
    cached_config: GitLabConfig | None = None
    cached_project: GitLabProject | None = None

    def topology() -> tuple[Path, Path]:
        """Resolve the checked-out source root and common Git directory once."""
        nonlocal source_root, common_git_dir
        if source_root is None:
            top_level = run_command(
                ("git", "-C", str(repository), "rev-parse", "--show-toplevel")
            )
            if top_level.returncode != 0 or not top_level.stdout.strip():
                raise AdapterFailure("repository top-level could not be determined")
            source_root = Path(top_level.stdout.strip())
            if not source_root.is_absolute():
                source_root = repository / source_root
        if common_git_dir is None:
            common = run_command(
                ("git", "-C", str(source_root), "rev-parse", "--git-common-dir")
            )
            if common.returncode != 0 or not common.stdout.strip():
                raise AdapterFailure("Git common directory could not be determined")
            common_git_dir = Path(common.stdout.strip())
            if not common_git_dir.is_absolute():
                common_git_dir = source_root / common_git_dir
        return source_root, common_git_dir

    def local_store() -> RunStore:
        """Return the explicitly injected or verified common-Git state store."""
        nonlocal store
        if store is None:
            _, common = topology()
            configured = config()
            namespace = f"{configured.host}-{configured.project_id}".replace("/", "_")
            store = LocalRunStore(root=common / "agent-work" / namespace)
        return store

    def config() -> GitLabConfig:
        """Capture validated non-secret configuration once for the entire run."""
        nonlocal cached_config
        if cached_config is None:
            cached_config = config_override or load_environment_config(environment)
        return cached_config

    def project_metadata(config_value: GitLabConfig) -> GitLabProject:
        """Read and validate the configured GitLab project's binding metadata."""
        nonlocal cached_project
        if cached_project is None:
            project = GitLabGateway(config_value, run_command).verify_project()
            if project.id != config_value.project_id:
                raise AdapterFailure("configured GitLab project did not match response")
            cached_project = project
        return cached_project

    def issue(config_value: GitLabConfig, iid: int) -> GitLabIssue:
        gateway = GitLabGateway(config_value, run_command)
        project_metadata(config_value)
        return gateway.fetch_issue(iid)

    @contextmanager
    def lock(_work_item_id: str) -> Iterator[None]:
        with local_store().lock(blocking=False):
            yield

    def worktree(work_item: WorkItem) -> Path:
        if not isinstance(work_item, WorkItem):
            raise TypeError("workspace creation requires a normalized work item")
        source, _ = topology()
        branch = work_item.branch
        provenance = local_store().read_workspace_provenance(work_item.work_item_id)
        workspace_key = branch.removeprefix("agent/").replace("/", "-")
        project = project_metadata(config())
        default_branch = project.default_branch
        origin_url = project.http_url_to_repo
        namespace = f"{config().host}-{config().project_id}".replace("/", "_")
        workspace_root = local_store().root / "workspaces" / namespace
        workspace = workspace_root / workspace_key
        expected_provenance = {
            "work_item_id": work_item.work_item_id,
            "task_content_digest": work_item.task_content_digest,
            "branch": work_item.branch,
            "workspace": str(workspace),
            "origin_url": origin_url,
        }
        if provenance is not None and dict(provenance) != expected_provenance:
            raise AdapterFailure("retained workspace lacks matching runner provenance")
        if provenance is None and (workspace / ".git").exists():
            raise AdapterFailure(
                "unmanifested deterministic workspace cannot be adopted"
            )
        workspace = WorktreeGateway(source, run_command).create_or_reuse(
            workspace_key=workspace_key,
            branch=branch,
            default_branch=default_branch,
            origin_url=origin_url,
            workspace_root=workspace_root,
        )
        if provenance is None:
            local_store().write_workspace_provenance(
                work_item.work_item_id, expected_provenance
            )
        return workspace

    def verify(result: AgentResult, worktree_path: Path) -> None:
        """Bind the retained worktree's published result to the configured project."""
        branch = result.branch
        tip = result.branch_tip
        url = result.merge_request_url
        if branch is None or tip is None or url is None:
            raise RunFailure("published handoff artifacts are required")
        project = project_metadata(config())
        default_branch = project.default_branch
        GitLabGateway(config(), run_command).verify_merge_request(
            branch=branch,
            branch_tip=tip,
            merge_request_url=url,
            target_branch=default_branch,
            source_project_id=config().project_id,
            target_project_id=config().project_id,
        )

    def project(result: AgentResult) -> None:
        gateway = GitLabGateway(config(), run_command)
        issue_iid = int(result.document["work_item_id"].rsplit(":", 1)[1])
        run_id = str(result.document["run_id"])
        if result.outcome == "awaiting_human":
            gateway.project_awaiting_human(issue_iid=issue_iid, run_id=run_id)
        else:
            gateway.project_failed(issue_iid=issue_iid, run_id=run_id)

    def claim(work_item: WorkItem) -> None:
        issue = work_item.issue
        if issue is None:
            raise AdapterFailure("GitLab claim requires a GitLab work item")
        GitLabGateway(config(), run_command).claim_running(
            issue_iid=issue.iid, run_id=work_item.run_id
        )

    def result_schema() -> Mapping[str, object]:
        source, _ = topology()
        return load_json_schema(
            source / ".agents/contracts/work-item-result-v1.schema.json"
        )

    def preflight(config_value: GitLabConfig, work_item: WorkItem) -> None:
        if not isinstance(work_item, WorkItem):
            raise TypeError("repository preflight requires a normalized work item")
        source, _ = topology()
        branch_name = work_item.branch
        clean = run_command(("git", "-C", str(source), "status", "--porcelain"))
        if clean.returncode != 0 or clean.stdout.strip():
            raise AdapterFailure("repository must have a clean working tree")
        project = project_metadata(config_value)
        default_branch = project.default_branch
        expected_origins = {
            value.rstrip("/")
            for value in (
                project.http_url_to_repo,
                project.ssh_url_to_repo,
            )
        }
        origin = run_command(("git", "-C", str(source), "remote", "get-url", "origin"))
        if (
            origin.returncode != 0
            or origin.stdout.strip().rstrip("/") not in expected_origins
        ):
            raise AdapterFailure("origin does not match the configured GitLab project")
        fetched = run_command(
            (
                "git",
                "-C",
                str(source),
                "fetch",
                "origin",
                f"refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
            )
        )
        if fetched.returncode != 0:
            raise AdapterFailure("the default branch could not be refreshed")
        tests = run_command(("uv", "run", "pytest"))
        if tests.returncode != 0:
            raise AdapterFailure("repository tests must pass before an applied run")
        gateway = GitLabGateway(config_value, run_command)
        gateway.latest_pipeline_is_healthy(target_branch=default_branch)
        remote_branch = run_command(
            (
                "git",
                "-C",
                str(source),
                "ls-remote",
                "--exit-code",
                "--heads",
                "origin",
                f"refs/heads/{branch_name}",
            )
        )
        if remote_branch.returncode not in {0, 2}:
            raise AdapterFailure("issue branch could not be checked")
        retained_merge_request = gateway.find_open_merge_request(
            branch=branch_name, target_branch=default_branch
        )
        remote_exists = remote_branch.returncode == 0 and bool(
            remote_branch.stdout.strip()
        )
        if remote_exists != (retained_merge_request is not None):
            raise AdapterFailure("remote issue branch and handoff are inconsistent")
        provenance = local_store().read_workspace_provenance(work_item.work_item_id)
        if provenance is None:
            if remote_exists:
                raise AdapterFailure("remote handoff lacks runner workspace provenance")
            return
        workspace_text = provenance.get("workspace")
        if not isinstance(workspace_text, str):
            raise AdapterFailure("retained workspace provenance is invalid")
        local_head = run_command(("git", "-C", workspace_text, "rev-parse", "HEAD"))
        if local_head.returncode != 0 or not local_head.stdout.strip():
            raise AdapterFailure("retained workspace head could not be determined")
        remote_tip = remote_branch.stdout.split()[0] if remote_exists else None
        mr_tip = retained_merge_request.sha if retained_merge_request else None
        if remote_exists and (
            local_head.stdout.strip() != remote_tip or remote_tip != mr_tip
        ):
            raise AdapterFailure("retained publication revisions are inconsistent")

    def write_canonical_result(run_id: str, result: Mapping[str, object]) -> None:
        """Persist the validated result without exposing a storage-path detail."""
        local_store().write_canonical_result(run_id, result)

    def append_journal(transition: Mapping[str, object]) -> None:
        """Append one transition through the verified private state store."""
        local_store().append_transition(transition)

    def write_recovery(run_id: str, record: Mapping[str, object]) -> None:
        local_store().write_recovery_record(run_id, record)

    def schema_path() -> Path:
        """Return the trusted canonical schema below the resolved source root."""
        source, _ = topology()
        return source / ".agents/contracts/work-item-result-v1.schema.json"

    def read_worktree_head(worktree_path: Path) -> str:
        """Read the retained worktree HEAD through the injected Git boundary."""
        head = run_command(("git", "-C", str(worktree_path), "rev-parse", "HEAD"))
        if head.returncode != 0 or not head.stdout.strip():
            raise AdapterFailure("retained worktree head could not be determined")
        return head.stdout.strip()

    def now() -> str:
        """Provide time only at the concrete CLI wiring boundary."""
        return datetime.now(UTC).isoformat()

    def result_digest(document: Mapping[str, object]) -> str:
        """Provide stable result hashing only at the concrete CLI wiring boundary."""
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    return RunDependencies(
        load_config=config,
        fetch_issue=issue,
        repository_lock=lock,
        preflight_repository=preflight,
        create_worktree=worktree,
        invoke_codex=CodexInvoker(run_command),
        verify_publication=verify,
        append_journal=append_journal,
        project_issue=project,
        new_run_id=lambda: uuid.uuid4().hex,
        load_result_schema=result_schema,
        claim_issue=claim,
        write_canonical_result=write_canonical_result,
        write_recovery_record=write_recovery,
        schema_path=schema_path,
        now=now,
        result_digest=result_digest,
        read_worktree_head=read_worktree_head,
    )


def _load_repositories(environment: Mapping[str, str]) -> dict[str, RepositoryTarget]:
    """Load the non-secret, explicitly configured repository allowlist."""
    filename = environment.get("AGENT_WORK_REPOSITORIES_FILE")
    if not filename:
        raise AdapterFailure("AGENT_WORK_REPOSITORIES_FILE is required")
    allowlist = Path(filename)
    if not _safe_absolute_regular_file(allowlist):
        raise AdapterFailure("repository allowlist is invalid")
    try:
        document = json.loads(allowlist.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise AdapterFailure("repository allowlist could not be read") from error
    if not isinstance(document, dict):
        raise AdapterFailure("repository allowlist is invalid")
    targets: dict[str, RepositoryTarget] = {}
    for alias, value in document.items():
        if (
            not isinstance(alias, str)
            or not isinstance(value, dict)
            or set(value) != {"checkout", "host", "project_id"}
        ):
            raise AdapterFailure("repository allowlist is invalid")
        try:
            validate_alias(alias)
        except ValueError as error:
            raise AdapterFailure("repository allowlist is invalid") from error
        checkout, host, project_id = (
            value.get("checkout"),
            value.get("host"),
            value.get("project_id"),
        )
        checkout_path = Path(checkout) if isinstance(checkout, str) else None
        if (
            checkout_path is None
            or not _safe_absolute_directory(checkout_path)
            or not isinstance(host, str)
            or not isinstance(project_id, int)
            or isinstance(project_id, bool)
            or project_id <= 0
        ):
            raise AdapterFailure("repository allowlist is invalid")
        try:
            GitLabConfig(host=host, project_id=project_id)
        except ValueError as error:
            raise AdapterFailure("repository allowlist is invalid") from error
        targets[alias] = RepositoryTarget(
            alias=alias, checkout=checkout_path, host=host, project_id=project_id
        )
    return targets


def _safe_absolute_regular_file(path: Path) -> bool:
    """Validate a trusted config file without traversing symlink components."""
    return _safe_absolute_path(path) and path.is_file() and path.stat().st_nlink == 1


def _safe_absolute_directory(path: Path) -> bool:
    """Validate an absolute normalized checkout with no symlink component."""
    return _safe_absolute_path(path) and path.is_dir()


def _safe_absolute_path(path: Path) -> bool:
    """Reject relative, non-normalized, and symlink-bearing trusted paths."""
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        return False
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return False
    return True


def build_default_file_runner(
    *,
    environment: Mapping[str, str] = os.environ,
    run_dependencies_factory: Callable[[RepositoryTarget], RunDependencies]
    | None = None,
) -> Callable[..., object]:
    """Wire filesystem intake to the existing normalized publication engine."""
    targets = _load_repositories(environment)
    dependencies_by_target: dict[RepositoryTarget, RunDependencies] = {}

    def dependencies_for(target: RepositoryTarget) -> RunDependencies:
        """Create one source-neutral dependency set per allowlisted checkout."""
        if target not in dependencies_by_target:
            dependencies_by_target[target] = (
                run_dependencies_factory(target)
                if run_dependencies_factory is not None
                else build_default_dependencies(
                    repository=target.checkout,
                    environment=environment,
                    config_override=GitLabConfig(
                        host=target.host, project_id=target.project_id
                    ),
                )
            )
        return dependencies_by_target[target]

    @contextmanager
    def inbox_lock(root: Path) -> Iterator[None]:
        """Exclude competing consumers of this one inbox state machine."""
        lock_path = root / ".intake.lock"
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            yield
        finally:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def finalize(
        *,
        destination: Path,
        request: bytes,
        intake_result: Mapping[str, object],
        agent_result: Mapping[str, object],
    ) -> None:
        durable_finalize_bundle(
            destination=destination,
            request=request,
            intake_result=intake_result,
            agent_result=agent_result,
        )

    def execute(
        item: WorkItem, target: RepositoryTarget, timeout: int
    ) -> Mapping[str, object]:
        dependencies = dependencies_for(target)
        with dependencies.repository_lock(item.work_item_id):
            return run_normalized(
                work_item=item, timeout_seconds=timeout, dependencies=dependencies
            )

    intake = FileIntake(
        FilesystemDependencies(
            resolve_repository=targets.get,
            repository_lock=lambda _target: nullcontext(),
            run_normalized=execute,
            finalize_bundle=finalize,
            new_run_id=lambda: uuid.uuid4().hex,
            inbox=LocalInboxOperations(),
            inbox_lock=inbox_lock,
        )
    )
    return intake.run


def main(
    arguments: Sequence[str] | None = None,
    *,
    run_issue: Callable[..., object] | None = None,
    run_file: Callable[..., object] | None = None,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Parse one bounded work-item command and emit one report or safe error."""
    parser = argparse.ArgumentParser(prog="agent-work")
    subparsers = parser.add_subparsers(dest="command", required=True)
    issue_command = subparsers.add_parser("run-issue")
    issue_command.add_argument("issue_iid", type=_positive)
    issue_command.add_argument("--mode", choices=("publish",), required=True)
    issue_command.add_argument("--apply", action="store_true")
    issue_command.add_argument("--json", action="store_true")
    issue_command.add_argument("--timeout-seconds", type=_positive, default=900)
    file_command = subparsers.add_parser(
        "run-file",
        help="controlled filesystem intake for one producer request",
        description="Process one bounded filesystem intake request.",
    )
    file_command.add_argument(
        "--path", required=True, help="JSON basename in the inbox directory"
    )
    file_command.add_argument(
        "--inbox-root", type=Path, required=True, help="absolute inbox layout root"
    )
    file_command.add_argument(
        "--repository", required=True, help="allowlisted repository alias"
    )
    file_command.add_argument(
        "--mode", choices=("publish",), required=True, help="publish mode"
    )
    file_command.add_argument(
        "--apply",
        action="store_true",
        help="enables mutation; default is a read-only preview",
    )
    file_command.add_argument("--json", action="store_true", help="emit a JSON report")
    file_command.add_argument(
        "--timeout-seconds",
        type=_positive,
        default=900,
        help="bounded workflow timeout in seconds (default: 900)",
    )
    try:
        parsed = parser.parse_args(arguments)
    except SystemExit as error:
        if error.code == 0:
            return 0
        write_stderr("invalid command usage")
        return 2
    if parsed.command == "run-file":
        file_path = Path(parsed.path)
        if (
            file_path.is_absolute()
            or len(file_path.parts) != 1
            or file_path.suffix != ".json"
            or not parsed.inbox_root.is_absolute()
        ):
            write_stderr("invalid command usage")
            return 2
        try:
            runner = run_file or build_default_file_runner()
        except (AdapterFailure, OSError):
            write_stderr("agent-work run failed")
            return 1
        kwargs = {
            "path": file_path,
            "inbox_root": parsed.inbox_root,
            "repository": parsed.repository,
            "mode": parsed.mode,
            "apply": parsed.apply,
            "timeout_seconds": parsed.timeout_seconds,
        }
    else:
        runner = run_issue or (
            lambda **kwargs: run_one_issue(
                dependencies=build_default_dependencies(repository=Path.cwd()), **kwargs
            ).as_dict()
        )
        kwargs = {
            "issue_iid": parsed.issue_iid,
            "mode": parsed.mode,
            "apply": parsed.apply,
            "timeout_seconds": parsed.timeout_seconds,
        }
    try:
        report = runner(**kwargs)
    except (RunFailure, FileIntakeFailure) as error:
        write_stderr(str(error))
        return 1
    except (AdapterFailure, OSError):
        write_stderr("agent-work run failed")
        return 1
    except RuntimeError:
        write_stderr("agent-work run failed")
        return 1
    if isinstance(report, RunReport):
        serialized: Mapping[str, object] = report.as_dict()
    elif isinstance(report, FileIntakeReport):
        serialized = report.as_dict()
    elif isinstance(report, Mapping):
        if not all(isinstance(key, str) for key in report):
            write_stderr("agent-work run failed")
            return 1
        serialized = {
            key: value for key, value in report.items() if isinstance(key, str)
        }
    else:
        write_stderr("agent-work run failed")
        return 1
    if parsed.json:
        write_stdout(json.dumps(serialized, sort_keys=True))
    else:
        if parsed.command == "run-file":
            write_stdout(
                "File intake: "
                f"{serialized.get('outcome', 'unknown')} "
                f"{serialized.get('sha256', 'unknown')}"
            )
        else:
            write_stdout(f"Run report: {serialized.get('branch', 'unknown')}")
    if parsed.command == "run-file" and serialized.get("outcome") == "quarantined":
        return 1
    return 0
