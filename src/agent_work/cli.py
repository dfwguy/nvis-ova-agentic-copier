# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Command-line interface for recoverable agent-work operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast

from . import (
    registry_cleanup,
    template_comparison,
    template_update,
    template_update_publication,
)
from .credential_rotation import (
    CredentialRotationError,
    CredentialRotationReport,
    PathEvidence,
    RotationDependencies,
    atomic_write_environment,
)
from .credential_rotation import rotate_credential as default_rotate_credential
from .domain import DEFAULT_MAX_ACTIVATION_SECONDS, CommandRequest
from .gitlab import (
    GitLabError,
    GitLabIssuePublisher,
    GitLabMergeRequestPublisher,
    IssuePublicationReport,
    render_merge_request_description,
)
from .gitlab import publish_issue as default_publish_issue
from .gitlab import publish_merge_request as default_publish_merge_request
from .ledger import LedgerCorrupt, LockBusy, state_home
from .models import (
    AgentWorkConfig,
    CommandReport,
    LedgerRecord,
    MergeRequestEvidence,
    WorkerResultEnvelope,
)
from .ports import CommandRunner
from .red_checkpoint import (
    RedCheckpointEvidence,
)
from .repository import discover_config, run_subprocess
from .runtime import dispatch as default_dispatch
from .service_environment import (
    ServiceEnvironmentDependencies,
    ServiceEnvironmentError,
    ServiceEnvironmentReport,
)
from .service_environment import (
    migrate_service_environment as default_migrate_service_environment,
)
from .status import project_status
from .template_comparison import (
    BinaryCommandRunner,
    TemplateComparison,
    TemplateComparisonError,
)
from .template_comparison import (
    compare_template_revisions as default_compare_template_revisions,
)
from .timing import summarize_phase_timings
from .upstream_finding import ManifestError, PreparedIssueDraft, UpstreamFindingPreview
from .upstream_finding import prepare_issue_draft as default_prepare_issue_draft
from .upstream_gitlab import (
    UpstreamGitLabConfig,
    UpstreamGitLabError,
    UpstreamGitLabReader,
)
from .validation import (
    FilesystemJUnitSnapshotStore,
    JudgeValidationDependencies,
    JudgeValidationError,
    observe_judge_validation,
    run_judge_validation,
)
from .worker import (
    AgentInvoker,
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    build_worker_result,
    format_worker_result_diagnostic,
    parse_worker_result_build_input,
    submit_worker_result_to_broker,
)


class _PreviewUpstream(Protocol):
    """Typed read-only upstream preview boundary."""

    def __call__(
        self,
        preview: UpstreamFindingPreview,
        *,
        host: str,
        project_id: int,
        credential_file: Path,
        read_credential: Callable[[Path], bytes],
        run_command: CommandRunner,
        environment: Mapping[str, str],
    ) -> UpstreamFindingPreview: ...


class _PrepareIssueDraft(Protocol):
    """Typed local draft-preparation boundary."""

    def __call__(
        self, path: Path, *, read_bytes: Callable[[Path], bytes]
    ) -> PreparedIssueDraft: ...


class _HookFailure(Protocol):
    """Bounded structured failed-hook detail consumed by CLI rendering."""

    hook_id: str
    diagnostic_lines: Sequence[str]


class _JUnitArtifact(Protocol):
    """Immutable JUnit evidence consumed by CLI rendering."""

    environment: str
    path: Path
    sha256: str
    test_count: int


class _ValidationReport(Protocol):
    """Bounded validation report consumed by CLI rendering."""

    candidate_tree: str
    applied: bool
    action: Literal["preview", "ran", "existing", "status"]
    state: Literal[
        "preview",
        "running",
        "terminal-success",
        "terminal-failure",
        "terminal-artifact-failure",
    ]
    terminal: bool
    exit_code: int | None
    diagnostic: str | None

    @property
    def hook_failures(self) -> Sequence[_HookFailure]: ...

    @property
    def junit_artifacts(self) -> Sequence[_JUnitArtifact]: ...


class _ValidationRunAction(Protocol):
    """Injected durable validation-run action."""

    def __call__(self, *, candidate_tree: str, apply: bool) -> _ValidationReport: ...


class _ValidationStatusAction(Protocol):
    """Injected durable validation-status action."""

    def __call__(
        self, *, candidate_tree: str, wait_seconds: int | None
    ) -> _ValidationReport: ...


class _RegistryCleanupAction(Protocol):
    """Injected read-only registry cleanup assessment boundary."""

    def __call__(self, *, apply: bool) -> registry_cleanup.CleanupAssessment: ...


class _ApplyUpstream(Protocol):
    """Typed upstream apply boundary."""

    def __call__(
        self,
        draft: PreparedIssueDraft,
        *,
        host: str,
        project_id: int,
        credential_file: Path,
        read_credential: Callable[[Path], bytes],
        run_command: CommandRunner,
        environment: Mapping[str, str],
    ) -> UpstreamFindingPreview: ...


class _LoadRecord(Protocol):
    """Typed read-only ledger boundary for result building."""

    def __call__(self, issue_iid: int) -> LedgerRecord | None: ...


class _ChangedPaths(Protocol):
    """Typed read-only worktree observation boundary."""

    def __call__(self, worktree: Path, /) -> tuple[str, ...]: ...


class _MergeRequestPublicationReport(Protocol):
    """Minimal report shape returned by the injected publication boundary."""

    applied: bool
    action: str
    merge_request: MergeRequestEvidence | None
    description: str


class _PublishMergeRequest(Protocol):
    """Structured publication boundary shared by CLI composition and tests."""

    def __call__(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> _MergeRequestPublicationReport: ...


class _PublishIssue(Protocol):
    """Structured issue publication boundary shared by CLI composition and tests."""

    def __call__(
        self,
        *,
        title: str,
        problem: str,
        outcome: str,
        acceptance: tuple[str, ...],
        impact: str | None,
        additional_context: str | None,
        marker: str | None,
        priority: str | None,
        severity: str | None,
        labels: tuple[str, ...],
        issue_iid: int | None,
        apply: bool,
    ) -> IssuePublicationReport: ...


class _CompareTemplateRevisions(Protocol):
    """Typed template-preview composition boundary."""

    def __call__(
        self,
        answers_path: Path,
        candidate_revision: str,
        *,
        read_text: Callable[[Path], str],
        run_command: BinaryCommandRunner,
        private_repository: Callable[[], AbstractContextManager[Path]],
        timeout_seconds: float,
    ) -> TemplateComparison: ...


def _read_template_text(path: Path) -> str:
    """Read Copier answers with deterministic text decoding."""
    return path.read_text(encoding="utf-8")


def _configured_merge_request_publisher(
    *,
    environment: Mapping[str, str],
    find_executable: Callable[[str], str | None],
    run_command: CommandRunner,
    sleeper: Callable[[float], None],
) -> _PublishMergeRequest:
    """Compose the structured publisher at the CLI's external boundaries."""
    host = environment.get("GITLAB_HOST", "")
    try:
        project_id = int(environment.get("GITLAB_PROJECT_ID", ""))
    except ValueError as error:
        raise GitLabError("GitLab publication configuration is invalid") from error
    glab = find_executable("glab")
    if not host or project_id <= 0 or glab is None or not Path(glab).is_absolute():
        raise GitLabError("GitLab publication configuration is invalid")
    port = GitLabMergeRequestPublisher(
        host=host,
        project_id=project_id,
        glab_path=str(Path(glab).resolve()),
        run_command=run_command,
    )

    def publish(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> _MergeRequestPublicationReport:
        """Render then delegate one structured CLI request."""
        return default_publish_merge_request(
            project_id=project_id,
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=render_merge_request_description(
                problem=problem,
                outcome=outcome,
                validation=validation,
                impact=impact,
                human_action=human_action,
                tracking=tracking,
                issue_iid=issue_iid,
            ),
            issue_iid=issue_iid,
            apply=apply,
            port=port,
            sleeper=sleeper,
        )

    return publish


def _configured_issue_publisher(
    *,
    environment: Mapping[str, str],
    find_executable: Callable[[str], str | None],
    run_command: CommandRunner,
) -> _PublishIssue:
    """Compose the configured numeric-project issue publisher once at the CLI edge."""
    host = environment.get("GITLAB_HOST", "")
    try:
        project_id = int(environment.get("GITLAB_PROJECT_ID", ""))
    except ValueError as error:
        raise GitLabError(
            "GitLab issue publication configuration is invalid"
        ) from error
    glab = find_executable("glab")
    if not host or project_id <= 0 or glab is None or not Path(glab).is_absolute():
        raise GitLabError("GitLab issue publication configuration is invalid")
    port = GitLabIssuePublisher(
        host=host,
        project_id=project_id,
        glab_path=str(Path(glab).resolve()),
        run_command=run_command,
    )

    def publish(
        *,
        title: str,
        problem: str,
        outcome: str,
        acceptance: tuple[str, ...],
        impact: str | None,
        additional_context: str | None,
        marker: str | None,
        priority: str | None,
        severity: str | None,
        labels: tuple[str, ...],
        issue_iid: int | None,
        apply: bool,
    ) -> IssuePublicationReport:
        return default_publish_issue(
            project_id=project_id,
            title=title,
            problem=problem,
            outcome=outcome,
            acceptance=acceptance,
            impact=impact,
            additional_context=additional_context,
            marker=marker,
            priority=priority,
            severity=severity,
            labels=labels,
            issue_iid=issue_iid,
            apply=apply,
            port=port,
        )

    return publish


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _common(
    parser: argparse.ArgumentParser,
    *,
    apply: bool = True,
    explicit_dry_run: bool = False,
    apply_help: str = "apply the operation",
    debug_help: str = "show a traceback on failure",
    json_help: str = "emit stable JSON",
) -> None:
    if apply:
        if explicit_dry_run:
            action = parser.add_mutually_exclusive_group()
            action.add_argument("--apply", action="store_true", help=apply_help)
            action.add_argument(
                "--dry-run",
                action="store_true",
                help="validate without applying changes",
            )
        else:
            parser.add_argument("--apply", action="store_true", help=apply_help)
    parser.add_argument("--json", action="store_true", help=json_help)
    parser.add_argument("--debug", action="store_true", help=debug_help)


class _AgentWorkParser(argparse.ArgumentParser):
    """Keep the conditional reviewed-plan requirement at the parse boundary."""

    def parse_args(
        self,
        args: Iterable[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        if parsed is None:
            raise RuntimeError("argument parser returned no namespace")
        if (
            (getattr(parsed, "resource", None), getattr(parsed, "action", None))
            == ("recovery", "resolve")
            and parsed.apply
            and parsed.plan_file is None
        ):
            self.error("--apply requires --plan-file")
        if (
            (getattr(parsed, "resource", None), getattr(parsed, "action", None))
            == ("worker-result", "build")
            and parsed.apply
            and (parsed.broker_socket is None or parsed.broker_nonce is None)
        ):
            self.error("--apply requires --broker-socket and --broker-nonce")
        return parsed


def _candidate_tree(value: str) -> str:
    """Require the exact staged-tree identifier at the CLI boundary."""
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError(
            "candidate tree must be 40 lowercase hexadecimal characters"
        )
    return value


def _broker_nonce(value: str) -> str:
    """Require one bounded lowercase nonce at the CLI boundary."""
    if re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError(
            "broker nonce must be 32 lowercase hexadecimal characters"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the nested resource/action command surface."""
    parser = _AgentWorkParser(prog="agent-work")
    resources = parser.add_subparsers(dest="resource", required=True)
    loop = resources.add_parser("loop", help="inspect or advance the serial loop")
    loop_actions = loop.add_subparsers(dest="action", required=True)
    run = loop_actions.add_parser(
        "run",
        help="preview or run one bounded recoverable activation",
        description=(
            "Preview the next safe action without mutation, or use --apply to "
            "advance one bounded recoverable activation."
        ),
    )
    _common(
        run,
        apply_help=(
            "advance one bounded recoverable activation; omit for a read-only preview"
        ),
    )
    run.add_argument(
        "--max-segments",
        type=_positive_integer,
        default=16,
        help="maximum persisted workflow attempts in one applied activation (default: %(default)s)",
    )
    run.add_argument(
        "--max-activation-seconds",
        type=_positive_integer,
        default=DEFAULT_MAX_ACTIVATION_SECONDS,
        help="maximum seconds for one applied activation (default: %(default)s)",
    )
    status = loop_actions.add_parser("status", help="show durable loop state")
    _common(
        status,
        apply_help="accepted for command consistency; status remains read-only",
    )
    status.add_argument("--issue-iid", type=_positive_integer)
    retry = loop_actions.add_parser("retry", help="preview or retry one blocked item")
    _common(retry)
    retry.add_argument("--issue-iid", type=_positive_integer, required=True)
    canary = loop_actions.add_parser(
        "canary",
        help="preview a non-claiming operational checkpoint",
        description="Preview a safe checkpoint; --apply requires the exact reviewed --plan-file.",
    )
    _common(canary, apply_help="validate the exact reviewed plan without claiming work")
    canary.add_argument("--plan-file", type=Path)
    recover = loop_actions.add_parser(
        "recover",
        help="preview or apply one exact retained-item recovery",
        description="Preview one target recovery; --apply requires the exact reviewed --plan-file.",
    )
    _common(recover, apply_help="apply only the exact reviewed target plan")
    recover.add_argument("--issue-iid", type=_positive_integer, required=True)
    recover.add_argument("--plan-file", type=Path)
    recover.add_argument(
        "--max-segments",
        type=_positive_integer,
        default=16,
        help="maximum recovery segments per applied activation (default: %(default)s)",
    )

    recovery = resources.add_parser(
        "recovery", help="inventory and resolve retained factory issues"
    )
    recovery_actions = recovery.add_subparsers(dest="action", required=True)
    inventory = recovery_actions.add_parser(
        "inventory", help="read-only recovery evidence inventory"
    )
    _common(
        inventory,
        apply_help="accepted for command consistency; inventory remains read-only",
    )
    inventory.add_argument("--issue-iid", type=_positive_integer)
    resolve = recovery_actions.add_parser(
        "resolve", help="preview or apply one reviewed recovery decision"
    )
    _common(resolve, apply_help="apply only the exact reviewed resolution plan")
    resolve.add_argument("--issue-iid", type=_positive_integer, required=True)
    resolve.add_argument(
        "--choice",
        choices=("retry", "close", "reopen", "relabel", "supersede"),
        required=True,
    )
    resolve.add_argument("--reason", required=True)
    resolve.add_argument(
        "--evidence-url", dest="evidence_urls", action="append", default=[]
    )
    resolve.add_argument("--replacement-issue-iid", type=_positive_integer)
    resolve.add_argument("--target-label")
    resolve.add_argument("--plan-file", type=Path)
    recover.add_argument(
        "--max-activation-seconds",
        type=_positive_integer,
        default=DEFAULT_MAX_ACTIVATION_SECONDS,
        help="maximum seconds for one applied activation (default: %(default)s)",
    )

    credential = resources.add_parser(
        "credential", help="safely inspect or rebind a service credential identity"
    )
    credential_actions = credential.add_subparsers(dest="action", required=True)
    rotate = credential_actions.add_parser(
        "rotate", help="validate a rotated credential and rebind its service identity"
    )
    rotate.add_argument("--environment-file", type=Path, required=True)
    rotate.add_argument("--unit-name", required=True)
    _common(
        rotate,
        apply_help="stop, verify, and safely rebind the service identity",
    )

    service_environment = resources.add_parser(
        "service-environment",
        help="inspect or migrate an installed service environment",
    )
    service_environment_actions = service_environment.add_subparsers(
        dest="action", required=True
    )
    migrate = service_environment_actions.add_parser(
        "migrate", help="validate or upgrade one versioned service environment"
    )
    migrate.add_argument("--environment-file", type=Path, required=True)
    migrate.add_argument("--unit-name", required=True)
    _common(
        migrate,
        explicit_dry_run=True,
        apply_help="stop, validate, and apply the service migration",
    )

    migration = resources.add_parser("migration", help="inventory legacy evidence")
    migration_actions = migration.add_subparsers(dest="action", required=True)
    preview = migration_actions.add_parser(
        "preview", help="build a read-only migration plan"
    )
    _common(preview, apply=False)
    apply_parser = migration_actions.add_parser(
        "apply", help="apply an exact reviewed plan"
    )
    _common(apply_parser)
    apply_parser.add_argument("--plan-file", type=Path, required=True)
    template = resources.add_parser(
        "template", help="inspect Copier template revisions"
    )
    template_actions = template.add_subparsers(dest="action", required=True)
    template_preview = template_actions.add_parser(
        "preview", help="compare a recorded template commit with a candidate revision"
    )
    template_preview.add_argument("--candidate-revision", required=True)
    template_preview.add_argument("--project-root", type=Path, default=Path("."))
    _common(
        template_preview,
        apply=False,
        debug_help="show a safe failure stage on failure",
    )
    template_update_parser = template_actions.add_parser(
        "update",
        help="plan or apply one isolated recoverable Copier template update",
        description="Dry-run is the default. --apply creates only a retained isolated worktree.",
    )
    template_update_parser.add_argument("--candidate-revision", required=True)
    template_update_parser.add_argument("--project-root", type=Path, default=Path("."))
    _common(
        template_update_parser,
        apply_help="apply one isolated Copier update after recording durable state",
        debug_help="show a traceback on failure",
    )
    finding = resources.add_parser(
        "upstream-finding", help="preview or file an upstream finding"
    )
    finding_actions = finding.add_subparsers(dest="action", required=True)
    finding_preview = finding_actions.add_parser(
        "preview",
        help="preview an upstream finding; use --apply to file one issue",
        description="Read-only GitLab preview by default; --apply can write one deduplicated upstream issue.",
    )
    finding_preview.add_argument("--manifest-file", type=Path, required=True)
    finding_preview.add_argument("--upstream-host", required=True)
    finding_preview.add_argument(
        "--upstream-project-id", type=_positive_integer, required=True
    )
    finding_preview.add_argument("--upstream-credential-file", type=Path, required=True)
    _common(
        finding_preview,
        apply_help="create one deduplicated upstream issue; omit for a read-only preview",
    )
    worker_result = resources.add_parser(
        "worker-result",
        help="preview or submit a validated worker result",
        description="Preview or submit a validated worker result.",
    )
    worker_result_actions = worker_result.add_subparsers(dest="action", required=True)
    worker_build = worker_result_actions.add_parser(
        "build",
        help="submit one protocol-v2 worker result to its live broker",
        description=("Submit bounded worker intent to a controller-owned live broker."),
    )
    worker_build.add_argument("--issue-iid", type=_positive_integer, required=True)
    worker_build.add_argument("--input-file", type=Path, required=True)
    worker_build.add_argument(
        "--broker-socket",
        type=Path,
        help="private live controller broker socket; required with --apply",
    )
    worker_build.add_argument(
        "--broker-nonce",
        type=_broker_nonce,
        help="broker 32-hex nonce; required with --apply",
    )
    _common(
        worker_build,
        apply_help="submit one worker result through the live broker",
        debug_help="retain bounded diagnostics on failure",
        json_help="emit the preview envelope or applied broker receipt as stable JSON",
    )
    merge_request = resources.add_parser(
        "merge-request", help="preview or publish one merge request"
    )
    merge_request_actions = merge_request.add_subparsers(dest="action", required=True)
    publish = merge_request_actions.add_parser(
        "publish", help="render and publish one reviewable merge request"
    )
    publish.add_argument("--source-branch", required=True)
    publish.add_argument("--target-branch", required=True)
    publish.add_argument("--title", required=True)
    publish.add_argument("--problem", required=True)
    publish.add_argument("--outcome", required=True)
    publish.add_argument("--validation", action="append", required=True)
    publish.add_argument("--impact")
    publish.add_argument("--human-action")
    publish.add_argument("--tracking")
    publish.add_argument("--issue-iid", type=_positive_integer)
    _common(publish, apply_help="create or update the merge request; omit for preview")
    issue = resources.add_parser("issue", help="preview or publish one issue")
    issue_actions = issue.add_subparsers(dest="action", required=True)
    issue_publish = issue_actions.add_parser(
        "publish", help="render and publish one structured issue"
    )
    issue_publish.add_argument("--title", required=True)
    issue_publish.add_argument("--problem", required=True)
    issue_publish.add_argument("--outcome", required=True)
    issue_publish.add_argument("--acceptance", action="append", required=True)
    issue_publish.add_argument("--impact")
    issue_publish.add_argument("--additional-context")
    issue_publish.add_argument("--marker")
    issue_publish.add_argument("--priority")
    issue_publish.add_argument("--severity")
    issue_publish.add_argument("--label", action="append", default=[])
    issue_publish.add_argument("--issue-iid", type=_positive_integer)
    _common(issue_publish, apply_help="create or update the issue; omit for preview")
    validation = resources.add_parser(
        "validation", help="run or observe one durable Judge validation"
    )
    validation_actions = validation.add_subparsers(dest="action", required=True)
    validation_run = validation_actions.add_parser(
        "run", help="preview or apply one exact candidate validation"
    )
    validation_run.add_argument("--candidate-tree", type=_candidate_tree, required=True)
    _common(
        validation_run, apply_help="run the fixed validation hook for this staged tree"
    )
    validation_status = validation_actions.add_parser(
        "status", help="read one candidate validation without running it"
    )
    validation_status.add_argument(
        "--candidate-tree", type=_candidate_tree, required=True
    )
    validation_status.add_argument("--wait-seconds", type=_positive_integer)
    _common(
        validation_status,
        apply_help="accepted for command consistency; validation status remains read-only",
    )
    repository = resources.add_parser("repository", help="inspect repository policy")
    repository_actions = repository.add_subparsers(dest="action", required=True)
    cleanup_policy = repository_actions.add_parser(
        "cleanup-policy", help="read the container registry cleanup policy"
    )
    _common(
        cleanup_policy,
        apply_help="accepted for command consistency; cleanup-policy remains read-only",
    )
    return parser


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _read_validation_artifact(path: Path) -> bytes:
    """Read at most one bounded fixed validation artifact."""
    with path.open("rb") as handle:
        return handle.read(1_048_577)


def _remove_validation_artifact(path: Path) -> None:
    """Remove one mutable hook-produced JUnit artifact when it exists."""
    path.unlink(missing_ok=True)


_TERMINAL_SEQUENCE = re.compile(
    r"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)"
    r"|\x1b[ -/][0-~]?|\x1b[0-9=><])"
)


def _sanitize_human_value(value: object) -> str:
    """Render one bounded value on a single human-readable line."""
    without_terminal_sequences = _TERMINAL_SEQUENCE.sub("", str(value))
    safe = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in without_terminal_sequences
    )
    return " ".join(safe.split())


def _render_worker_result(result: WorkerResultEnvelope, *, as_json: bool) -> str:
    """Render a worker-result preview for a human or a machine consumer."""
    if as_json:
        return result.model_dump_json(exclude_none=True)
    safe = _sanitize_human_value
    return " ".join(
        (
            "valid",
            f"issue=#{result.issue_iid}",
            f"lane={result.workflow_lane.value}",
            f"result={result.result_kind}",
            f"summary={safe(result.change_summary)}",
            f"next_action={safe(result.next_action)}",
        )
    )


def _inspect_path(path: Path) -> PathEvidence:
    """Read only the ownership and type evidence needed for a user bus."""
    try:
        status = path.stat()
    except OSError:
        return PathEvidence(False, False, False, None)
    import stat

    return PathEvidence(
        True, stat.S_ISDIR(status.st_mode), stat.S_ISSOCK(status.st_mode), status.st_uid
    )


def _render(report: CommandReport, *, as_json: bool) -> str:
    if as_json:
        payload = report.model_dump(mode="json", exclude_none=True)
        if not payload.get("recovery_candidates"):
            payload.pop("recovery_candidates", None)
        if not payload.get("prerequisite_projections"):
            payload.pop("prerequisite_projections", None)
        return json.dumps(payload, default=str, separators=(",", ":"))
    if report.command != "loop.status":
        details = [report.outcome]
        if report.stop_reason:
            details.append(f"stop_reason={report.stop_reason}")
        if report.summary:
            details.append(f"summary={report.summary}")
        if report.next_action:
            details.append(f"next_action={report.next_action}")
        for item in report.items[-1:]:
            details.extend(
                (
                    f"issue=#{item.issue_iid}",
                    f"state={item.state}",
                    f"phase={item.phase}",
                )
            )
            if item.next_action:
                details.append(f"next_action={item.next_action}")
            if item.worker_exit:
                details.extend(
                    (
                        f"returncode={item.worker_exit.returncode}",
                        f"worker_diagnostic={item.worker_exit.diagnostic}",
                    )
                )
        if report.recovery_summary:
            details.append(
                "recovery="
                + ",".join(
                    f"{name}={value}"
                    for name, value in report.recovery_summary.model_dump().items()
                )
            )
        for candidate in report.recovery_candidates:
            details.extend(
                (
                    f"issue=#{candidate.issue_iid}",
                    f"classification={candidate.classification}",
                    f"priority={candidate.priority}",
                    f"active={str(candidate.active).lower()}",
                    f"human_action_required={str(candidate.human_action_required).lower()}",
                    f"next_action={_sanitize_human_value(candidate.next_action)}",
                )
            )
            if candidate.blocked_cause:
                details.append(
                    f"blocked_cause={_sanitize_human_value(candidate.blocked_cause)}"
                )
            if candidate.dependency_explanation:
                details.append(
                    f"dependency_explanation={_sanitize_human_value(candidate.dependency_explanation)}"
                )
        for projection in report.prerequisite_projections:
            details.extend(
                (
                    f"issue=#{projection.issue_iid}",
                    "current_labels="
                    + ",".join(
                        _sanitize_human_value(label)
                        for label in projection.current_labels
                    ),
                    "intended_labels="
                    + ",".join(
                        _sanitize_human_value(label)
                        for label in projection.intended_labels
                    ),
                )
            )
        return " ".join(details)

    safe = _sanitize_human_value
    rendered: list[str] = []
    for item in report.items:
        item_details = [
            f"issue=#{item.issue_iid}",
            f"status={item.projected_status or project_status(state=item.state, phase=item.phase, source_sha=item.source_sha)}",
            f"state={item.state}",
            f"phase={item.phase}",
            f"project={item.project_id}",
            f"branch={_sanitize_human_value(item.source_branch)}",
            f"worktree={safe(item.worktree)}",
            f"main={safe(item.main_branch)}@{safe(item.main_sha)}",
            f"source={safe(item.source_sha)}",
            f"publication={safe(item.publication_sha)}",
            f"mr={safe(item.merge_request_iid)}:{safe(item.merge_request_url)}",
            f"retry={item.attempt - 1}",
        ]
        if item.next_action:
            item_details.append(f"next={safe(item.next_action)}")
        if item.summary:
            item_details.append(f"summary={safe(item.summary)}")
        if item.current_condition:
            item_details.append(
                f"current={safe(item.current_condition.code)}:{safe(item.current_condition.detail)}"
            )
        if item.worker_exit:
            item_details.extend(
                (
                    f"returncode={item.worker_exit.returncode}",
                    f"worker_diagnostic={safe(item.worker_exit.diagnostic)}",
                )
            )
        if item.phase_timings:
            item_details.append(
                f"timing={safe(summarize_phase_timings(item.phase_timings))}"
            )
        rendered.append(" ".join(item_details))
    return (
        "\n".join(f"{report.outcome} {line}" for line in rendered)
        if rendered
        else report.outcome
    )


def _render_finding_preview(preview: UpstreamFindingPreview, *, as_json: bool) -> str:
    """Render the intentionally content-free upstream preview."""
    if as_json:
        return preview.as_json()
    details = [
        f"outcome={preview.outcome}",
        f"project_fingerprint={preview.project_fingerprint}",
        f"revision_fingerprint={preview.revision_fingerprint}",
        f"semantic_finding_fingerprint={preview.semantic_finding_fingerprint}",
        f"paths={preview.copied_path_count}",
        f"evidence_links={preview.evidence_link_count}",
    ]
    if preview.matching_iid is not None:
        details.append(f"matching_iid={preview.matching_iid}")
    return " ".join(details)


def _render_template_preview(result: TemplateComparison, *, as_json: bool) -> str:
    """Render the stable template-preview result without project details."""
    outcome = "changed" if result.paths else "unchanged"
    if as_json:
        return json.dumps(
            {
                "schema_version": 1,
                "command": "template.preview",
                "outcome": outcome,
                "current_commit": result.current_commit,
                "candidate_commit": result.candidate_commit,
                "paths": list(result.paths),
            },
            separators=(",", ":"),
        )
    header = (
        f"outcome={outcome} current_commit={result.current_commit} "
        f"candidate_commit={result.candidate_commit} paths={len(result.paths)}"
    )
    return "\n".join((header, *(f"path={path}" for path in result.paths)))


def _render_template_update(
    report: template_update.TemplateUpdateReport,
    *,
    as_json: bool,
    publication: object | None = None,
) -> str:
    """Render the stable non-secret template-update result."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "command": "template.update",
        "outcome": report.outcome,
        "state": getattr(report.state, "value", report.state),
        "current_commit": report.current_commit,
        "candidate_commit": report.candidate_commit,
        "changed_paths": list(report.changed_paths),
        "next_action": report.next_action,
    }
    if report.branch is not None:
        payload["branch"] = report.branch
    if report.worktree is not None:
        payload["worktree"] = str(report.worktree)
    if report.conflict_paths:
        payload["conflict_paths"] = list(report.conflict_paths)
    if report.recovery_stage is not None:
        payload["recovery_stage"] = report.recovery_stage
    if report.completion is not None and publication is None:
        payload["completion"] = report.completion.model_dump(mode="json")
    if publication is not None:
        payload["outcome"] = getattr(publication, "outcome", "needs-recovery")
        payload["state"] = getattr(publication, "state", "needs-recovery")
        payload["next_action"] = getattr(
            publication, "next_action", "inspect retained template update evidence"
        )
        iid = getattr(publication, "merge_request_iid", None)
        url = getattr(publication, "merge_request_url", None)
        if isinstance(iid, int) and iid > 0 and isinstance(url, str) and url:
            payload["merge_request_iid"] = iid
            payload["merge_request_url"] = url
    if as_json:
        return json.dumps(payload, separators=(",", ":"))
    return " ".join(f"{key}={value}" for key, value in payload.items())


def _render_rotation(report: CredentialRotationReport, *, as_json: bool) -> str:
    """Render only the stable, non-secret credential-rotation result."""
    if as_json:
        return report.model_dump_json(exclude_none=True)
    return " ".join(
        (
            f"outcome={report.outcome}",
            f"config_updated={str(report.config_updated).lower()}",
            f"rolled_back={str(report.rolled_back).lower()}",
            f"timer_stopped={str(report.timer_stopped).lower()}",
            f"service_stopped={str(report.service_stopped).lower()}",
            f"next_action={report.next_action}",
        )
    )


def _render_service_environment(
    report: ServiceEnvironmentReport, *, as_json: bool
) -> str:
    """Render only stable, non-secret service-environment migration results."""
    if as_json:
        return report.model_dump_json(exclude_none=True)
    return " ".join(
        (
            f"outcome={report.outcome}",
            f"config_updated={str(report.config_updated).lower()}",
            f"rolled_back={str(report.rolled_back).lower()}",
            f"timer_stopped={str(report.timer_stopped).lower()}",
            f"service_stopped={str(report.service_stopped).lower()}",
            f"next_action={report.next_action}",
        )
    )


def _upstream_reader(
    *,
    host: str,
    project_id: int,
    credential_file: Path,
    read_credential: Callable[[Path], bytes],
    run_command: CommandRunner,
    environment: Mapping[str, str],
    find_executable: Callable[[str], str | None],
) -> UpstreamGitLabReader:
    """Construct the one configured upstream client for either operation."""
    configured = environment.get("AGENT_WORK_GLAB")
    candidate = configured if configured else find_executable("glab")
    if candidate is None or not Path(candidate).is_absolute():
        raise UpstreamGitLabError("upstream glab is unavailable")
    try:
        config = UpstreamGitLabConfig(
            host, project_id, credential_file, Path(candidate).resolve()
        )
    except (OSError, ValueError):
        raise UpstreamGitLabError("upstream configuration is invalid") from None
    return UpstreamGitLabReader(
        config, read_credential=read_credential, run_command=run_command
    )


def _default_preview_upstream(
    preview: UpstreamFindingPreview,
    *,
    host: str,
    project_id: int,
    credential_file: Path,
    read_credential: Callable[[Path], bytes],
    run_command: CommandRunner,
    environment: Mapping[str, str],
    find_executable: Callable[[str], str | None],
) -> UpstreamFindingPreview:
    """Preview through the shared configured upstream client."""
    return _upstream_reader(
        host=host,
        project_id=project_id,
        credential_file=credential_file,
        read_credential=read_credential,
        run_command=run_command,
        environment=environment,
        find_executable=find_executable,
    ).preview(preview)


def _default_apply_upstream(
    draft: PreparedIssueDraft,
    *,
    host: str,
    project_id: int,
    credential_file: Path,
    read_credential: Callable[[Path], bytes],
    run_command: CommandRunner,
    environment: Mapping[str, str],
    find_executable: Callable[[str], str | None],
) -> UpstreamFindingPreview:
    """Apply through the shared configured upstream client."""
    return _upstream_reader(
        host=host,
        project_id=project_id,
        credential_file=credential_file,
        read_credential=read_credential,
        run_command=run_command,
        environment=environment,
        find_executable=find_executable,
    ).apply(draft.preview, draft)


def main(
    arguments: Sequence[str] | None = None,
    *,
    dispatch: Callable[..., CommandReport] = default_dispatch,
    prepare_issue_draft: _PrepareIssueDraft = default_prepare_issue_draft,
    preview_manifest: Callable[..., UpstreamFindingPreview] | None = None,
    read_bytes: Callable[[Path], bytes] = Path.read_bytes,
    preview_upstream: _PreviewUpstream | None = None,
    apply_upstream: _ApplyUpstream | None = None,
    read_credential: Callable[[Path], bytes] = Path.read_bytes,
    run_command: CommandRunner = run_subprocess,
    discover_agent_work_config: Callable[..., AgentWorkConfig] = discover_config,
    sleeper: Callable[[float], None] = time.sleep,
    environment: Mapping[str, str] = os.environ,
    find_executable: Callable[[str], str | None] = shutil.which,
    current_checkout: Callable[[], Path] = Path.cwd,
    load_record: _LoadRecord | None = None,
    changed_paths: _ChangedPaths | None = None,
    load_red_checkpoint: Callable[[int, int, str], RedCheckpointEvidence | None]
    | None = None,
    publish_merge_request: _PublishMergeRequest | None = None,
    publish_issue: _PublishIssue | None = None,
    rotate: Callable[..., CredentialRotationReport] = default_rotate_credential,
    migrate_service_environment: Callable[
        ..., ServiceEnvironmentReport
    ] = default_migrate_service_environment,
    atomic_write: Callable[[Path, bytes], None] = atomic_write_environment,
    compare_template_revisions: _CompareTemplateRevisions = default_compare_template_revisions,
    template_read_text: Callable[[Path], str] = _read_template_text,
    template_run_command: BinaryCommandRunner | None = None,
    template_private_repository: Callable[
        [], AbstractContextManager[Path]
    ] = template_comparison.private_repository,
    run_template_update: Callable[
        ..., object
    ] = template_update.execute_template_update,
    template_update_dependencies: template_update.TemplateUpdateDependencies
    | None = None,
    publish_template_update: Callable[..., object] | None = None,
    validation_run: _ValidationRunAction | None = None,
    validation_status: _ValidationStatusAction | None = None,
    read_validation_artifact: Callable[[Path], bytes | None] = (
        _read_validation_artifact
    ),
    registry_cleanup_action: _RegistryCleanupAction | None = None,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Dispatch one command and return the documented exit status."""
    parsed = build_parser().parse_args(arguments)
    if parsed.resource == "template" and parsed.action == "update":
        try:
            comparison_runner = template_run_command
            if comparison_runner is None:
                resolved_git = find_executable("git")
                if resolved_git is None or not Path(resolved_git).is_absolute():
                    raise TemplateComparisonError(stage="input")
                comparison_runner = partial(
                    template_comparison.run_git_command,
                    git_executable=Path(resolved_git),
                    environment=environment,
                )
            compare_update = partial(
                compare_template_revisions,
                read_text=template_read_text,
                run_command=comparison_runner,
                private_repository=template_private_repository,
                timeout_seconds=30,
            )
            config: AgentWorkConfig | None = None
            if (
                parsed.apply
                and template_update_dependencies is None
                and run_template_update is template_update.execute_template_update
            ):
                config = discover_agent_work_config(
                    current_directory=parsed.project_root,
                    environment=environment,
                    run_command=run_command,
                )
                project = template_update.TemplateUpdateProject(
                    project_id=config.project_id,
                    checkout=config.checkout,
                    default_branch=config.default_branch,
                    state_root=config.state_root,
                )
                dependencies = template_update.default_dependencies(
                    project=project,
                    agent_invoker=AgentInvoker(run_command, environment=environment),
                )
            else:
                dependencies = cast(
                    template_update.TemplateUpdateDependencies,
                    template_update_dependencies,
                )
            report = cast(
                template_update.TemplateUpdateReport,
                run_template_update(
                    parsed.candidate_revision,
                    parsed.project_root,
                    apply=parsed.apply,
                    compare=compare_update,
                    dependencies=dependencies,
                ),
            )
            publication: object | None = None
            if (
                parsed.apply
                and report.state is template_update.TemplateUpdateState.READY
                and report.changed_paths
            ):
                if publish_template_update is not None:
                    publication = publish_template_update(report)
                else:
                    if config is None:
                        raise RuntimeError(
                            "template update publication needs configured project identity"
                        )
                    publication = template_update_publication.publish_ready_template_update(
                        report,
                        dependencies=template_update_publication.default_publication_dependencies(
                            config=config,
                            update=dependencies,
                            service_environment=environment,
                            run_command=run_command,
                        ),
                    )
        except (
            OSError,
            RuntimeError,
            ValueError,
            template_comparison.TemplateComparisonError,
        ) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        write_stdout(
            _render_template_update(
                report, as_json=parsed.json, publication=publication
            )
        )
        if publication is not None:
            return 0 if getattr(publication, "outcome", None) == "published" else 1
        return 0 if report.outcome in {"planned", "ready"} else 1
    if parsed.resource == "issue" and parsed.action == "publish":
        try:
            publisher = publish_issue or _configured_issue_publisher(
                environment=environment,
                find_executable=find_executable,
                run_command=run_command,
            )
            report = publisher(
                title=parsed.title,
                problem=parsed.problem,
                outcome=parsed.outcome,
                acceptance=tuple(parsed.acceptance),
                impact=parsed.impact,
                additional_context=parsed.additional_context,
                marker=parsed.marker,
                priority=parsed.priority,
                severity=parsed.severity,
                labels=tuple(parsed.label),
                issue_iid=parsed.issue_iid,
                apply=parsed.apply,
            )
        except (GitLabError, RuntimeError, ValueError, OSError) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        if report.applied != parsed.apply:
            write_stderr("publication result did not match requested apply state")
            return 1
        payload: dict[str, object] = {
            "applied": report.applied,
            "action": report.action,
        }
        if report.issue is not None:
            payload["iid"] = report.issue.iid
            payload["web_url"] = report.issue.web_url
        else:
            payload["title"] = parsed.title
            payload["description"] = report.description
        write_stdout(
            json.dumps(payload, sort_keys=True)
            if parsed.json
            else f"{parsed.title}\n\n{report.description}"
        )
        return 0
    if parsed.resource == "repository" and parsed.action == "cleanup-policy":
        try:
            if registry_cleanup_action is None:
                glab = environment.get("AGENT_WORK_GLAB") or find_executable("glab")
                if glab is None:
                    raise registry_cleanup.RegistryBoundaryError(
                        "registry cleanup configuration is invalid"
                    )
                observed = registry_cleanup.fetch_registry_cleanup_policy(
                    host=environment.get("GITLAB_HOST", ""),
                    project_id=environment.get("GITLAB_PROJECT_ID", ""),
                    glab_path=Path(glab),
                    run_command=run_command,
                )
                assessment = registry_cleanup.evaluate_cleanup_policy(observed)
            else:
                assessment = registry_cleanup_action(apply=parsed.apply)
        except (OSError, RuntimeError, registry_cleanup.RegistryBoundaryError) as error:
            write_stderr(str(error))
            return 1
        write_stdout(
            json.dumps(assessment.json_payload, separators=(",", ":"))
            if parsed.json
            else assessment.human_message
        )
        return assessment.exit_code
    if parsed.resource == "validation":
        try:
            if (parsed.action == "run" and validation_run is None) or (
                parsed.action == "status" and validation_status is None
            ):
                import fcntl

                def git_read(command: tuple[str, ...]) -> str:
                    result = run_command(command)
                    if result.returncode != 0:
                        raise RuntimeError("validation Git probe failed")
                    return result.stdout.strip()

                state_root = (
                    state_home(environment=environment)
                    / "agent-work-validation"
                    / hashlib.sha256(
                        str(
                            Path(git_read(("git", "rev-parse", "--git-dir"))).resolve()
                        ).encode()
                    ).hexdigest()
                )

                def read_record(tree: str) -> dict[str, object] | None:
                    path = state_root / f"{tree}.json"
                    if not path.exists():
                        return None
                    record = json.loads(path.read_text())
                    if not isinstance(record, dict):
                        raise JudgeValidationError("validation record is malformed")
                    return record

                def write_record(tree: str, record: dict[str, object]) -> None:
                    state_root.mkdir(parents=True, exist_ok=True)
                    path = state_root / f"{tree}.json"
                    atomic_write(
                        path,
                        json.dumps(record, separators=(",", ":")).encode(),
                    )

                @contextmanager
                def candidate_lock(tree: str) -> Iterator[None]:
                    state_root.mkdir(parents=True, exist_ok=True)
                    with (state_root / f"{tree}.lock").open("w") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX)
                        yield

                snapshot_store = FilesystemJUnitSnapshotStore(root=state_root)
                validation_dependencies = JudgeValidationDependencies(
                    current_staged_tree=lambda: git_read(("git", "write-tree")),
                    read_record=read_record,
                    atomic_write_record=write_record,
                    candidate_lock=candidate_lock,
                    run_command=run_command,
                    monotonic_clock=time.monotonic,
                    sleeper=time.sleep,
                    read_artifact=read_validation_artifact,
                    remove_junit_artifact=_remove_validation_artifact,
                    capture_junit_artifacts=lambda tree, artifacts: (
                        snapshot_store.capture(candidate_tree=tree, artifacts=artifacts)
                    ),
                )
                if parsed.action == "run":
                    validation_run = partial(
                        run_judge_validation, dependencies=validation_dependencies
                    )
                else:
                    validation_status = partial(
                        observe_judge_validation, dependencies=validation_dependencies
                    )
            if parsed.action == "run":
                assert validation_run is not None
                report = validation_run(
                    candidate_tree=parsed.candidate_tree,
                    apply=getattr(parsed, "apply", False),
                )
            else:
                assert validation_status is not None
                report = validation_status(
                    candidate_tree=parsed.candidate_tree,
                    wait_seconds=getattr(parsed, "wait_seconds", None),
                )
        except (JudgeValidationError, OSError, RuntimeError, ValueError) as error:
            write_stderr(str(error))
            return 1
        payload = {
            "candidate_tree": report.candidate_tree,
            "applied": report.applied,
            "action": report.action,
            "state": report.state,
            "terminal": report.terminal,
            "exit_code": report.exit_code if report.terminal else None,
            "diagnostic": report.diagnostic,
            "hook_failures": [
                {
                    "hook_id": failure.hook_id,
                    "diagnostic_lines": list(failure.diagnostic_lines),
                }
                for failure in report.hook_failures
            ],
            "junit_artifacts": [
                {
                    "environment": artifact.environment,
                    "path": str(artifact.path),
                    "sha256": artifact.sha256,
                    "test_count": artifact.test_count,
                }
                for artifact in report.junit_artifacts
            ],
        }
        write_stdout(
            json.dumps(payload, separators=(",", ":"))
            if parsed.json
            else " ".join(
                [
                    *(
                        f"{key}={value}"
                        for key, value in payload.items()
                        if key != "hook_failures"
                    ),
                    *(
                        part
                        for failure in report.hook_failures
                        for part in (
                            f"failed_hook={failure.hook_id}",
                            *failure.diagnostic_lines,
                        )
                    ),
                ]
            )
        )
        if parsed.action == "run" and not getattr(parsed, "apply", False):
            return 0
        return 0 if report.state == "terminal-success" else 1
    if parsed.resource == "template" and parsed.action == "preview":
        try:
            runner = template_run_command
            if runner is None:
                try:
                    resolved_git = find_executable("git")
                except (OSError, RuntimeError, ValueError):
                    raise TemplateComparisonError(stage="input") from None
                if resolved_git is None or not Path(resolved_git).is_absolute():
                    raise TemplateComparisonError(stage="input")
                runner = partial(
                    template_comparison.run_git_command,
                    git_executable=Path(resolved_git),
                    environment=environment,
                )

            result = compare_template_revisions(
                parsed.project_root / ".copier-answers.yml",
                parsed.candidate_revision,
                read_text=template_read_text,
                run_command=(runner),
                private_repository=template_private_repository,
                timeout_seconds=30,
            )
        except TemplateComparisonError as error:
            message = str(error)
            if parsed.debug:
                message = f"{message} stage={error.stage}"
            write_stderr(message)
            return 1
        write_stdout(_render_template_preview(result, as_json=parsed.json))
        return 0
    if parsed.resource == "merge-request" and parsed.action == "publish":
        try:
            publisher = publish_merge_request or _configured_merge_request_publisher(
                environment=environment,
                find_executable=find_executable,
                run_command=run_command,
                sleeper=sleeper,
            )
            report = publisher(
                source_branch=parsed.source_branch,
                target_branch=parsed.target_branch,
                title=parsed.title,
                problem=parsed.problem,
                outcome=parsed.outcome,
                validation=tuple(parsed.validation),
                impact=parsed.impact,
                human_action=parsed.human_action,
                tracking=parsed.tracking,
                apply=parsed.apply,
                issue_iid=parsed.issue_iid,
            )
        except (GitLabError, RuntimeError, ValueError, OSError) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        if report.applied != parsed.apply:
            write_stderr("publication result did not match requested apply state")
            return 1
        payload: dict[str, object] = {
            "applied": report.applied,
            "action": report.action,
        }
        if not parsed.apply:
            payload["description"] = report.description
        elif (merge_request := report.merge_request) is not None:
            payload["iid"] = merge_request.iid
            payload["web_url"] = merge_request.web_url
        write_stdout(
            json.dumps(payload, sort_keys=True)
            if parsed.json
            else (
                f"{report.action}: applied={str(report.applied).lower()}\n\n{report.description}"
                if not parsed.apply
                else (
                    f"{report.action}: applied=true iid={merge_request.iid} url={merge_request.web_url}"
                    if merge_request is not None
                    else ""
                )
            )
        )
        return 0
    if parsed.resource == "service-environment" and parsed.action == "migrate":
        try:
            report = migrate_service_environment(
                environment_file=parsed.environment_file,
                unit_name=parsed.unit_name,
                apply=parsed.apply,
                dependencies=ServiceEnvironmentDependencies(
                    read_bytes=read_bytes,
                    atomic_write=atomic_write,
                    run_command=run_command,
                    resolve_executable=find_executable,
                    checkout=current_checkout(),
                    environment=environment,
                    effective_uid=os.geteuid,
                    inspect_path=_inspect_path,
                ),
            )
        except (ServiceEnvironmentError, OSError, RuntimeError):
            if parsed.debug:
                raise
            write_stderr("service environment migration could not be completed")
            return 1
        write_stdout(_render_service_environment(report, as_json=parsed.json))
        return 0 if report.outcome in {"current", "stale", "verified"} else 1
    if parsed.resource == "credential" and parsed.action == "rotate":
        try:
            report = rotate(
                environment_file=parsed.environment_file,
                unit_name=parsed.unit_name,
                apply=parsed.apply,
                dependencies=RotationDependencies(
                    read_bytes=read_bytes,
                    atomic_write=atomic_write,
                    run_command=run_command,
                    environment=environment,
                    effective_uid=os.geteuid,
                    inspect_path=_inspect_path,
                ),
            )
        except (CredentialRotationError, OSError, RuntimeError):
            if parsed.debug:
                raise
            write_stderr("credential rotation could not be completed")
            return 1
        write_stdout(_render_rotation(report, as_json=parsed.json))
        return 0 if report.outcome in {"preview", "verified"} else 1
    if (
        parsed.resource == "loop"
        and parsed.action in {"canary", "recover"}
        and parsed.apply
        and parsed.plan_file is None
    ):
        write_stderr("--apply requires --plan-file")
        return 2
    if parsed.resource == "worker-result" and parsed.action == "build":
        try:
            input_bytes = read_bytes(parsed.input_file)
        except OSError:
            write_stderr("worker_result_capture: input artifact missing or unreadable")
            return 1
        if parsed.apply:
            try:
                receipt = submit_worker_result_to_broker(
                    cast(Path, parsed.broker_socket),
                    WorkerResultBrokerClientRequest(
                        nonce=cast(str, parsed.broker_nonce),
                        issue_iid=parsed.issue_iid,
                    ),
                    input_bytes,
                    timeout_seconds=30,
                )
            except WorkerProtocolError as error:
                write_stderr(format_worker_result_diagnostic(error))
                return 1
            write_stderr("worker result submitted to live broker")
            if parsed.json:
                write_stdout(
                    json.dumps(
                        receipt,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
            return 0
        try:
            if load_record is None or changed_paths is None:
                from .runtime import build_worker_result_dependencies

                dependencies = build_worker_result_dependencies(
                    environment=environment,
                    run_command=run_command,
                )
                record_loader = load_record or dependencies.load_record
                observe_paths = changed_paths or dependencies.changed_paths
                checkpoint_loader = (
                    load_red_checkpoint or dependencies.load_red_checkpoint
                )
            else:
                record_loader = load_record
                observe_paths = changed_paths
                checkpoint_loader = load_red_checkpoint
            record = record_loader(parsed.issue_iid)
            if record is None:
                raise WorkerProtocolError("issue_iid", "has no retained ledger record")
            build_input = parse_worker_result_build_input(input_bytes)
            observed_paths = observe_paths(record.worktree)
            if checkpoint_loader is None:
                result = build_worker_result(
                    record,
                    build_input,
                    changed_paths=observed_paths,
                    red_checkpoint=None,
                )
            else:
                result = build_worker_result(
                    record,
                    build_input,
                    changed_paths=observed_paths,
                    red_checkpoint=checkpoint_loader(
                        record.project_id, record.issue_iid, record.source_sha or ""
                    ),
                )
        except WorkerProtocolError as error:
            write_stderr(format_worker_result_diagnostic(error))
            return 1
        except LedgerCorrupt:
            write_stderr("worker_result: could not be read or validated")
            return 1
        except (OSError, RuntimeError, ValueError):
            write_stderr("worker_result_validation: unexpected internal failure")
            return 1
        write_stdout(_render_worker_result(result, as_json=parsed.json))
        write_stderr("worker result validated; no changes were made")
        return 0
    if parsed.resource == "upstream-finding" and parsed.action == "preview":
        try:
            if preview_manifest is not None:
                preview = preview_manifest(parsed.manifest_file, read_bytes=read_bytes)
                if preview_upstream is None:
                    preview = _default_preview_upstream(
                        preview,
                        host=parsed.upstream_host,
                        project_id=parsed.upstream_project_id,
                        credential_file=parsed.upstream_credential_file,
                        read_credential=read_credential,
                        run_command=run_command,
                        environment=environment,
                        find_executable=find_executable,
                    )
                else:
                    preview = preview_upstream(
                        preview,
                        host=parsed.upstream_host,
                        project_id=parsed.upstream_project_id,
                        credential_file=parsed.upstream_credential_file,
                        read_credential=read_credential,
                        run_command=run_command,
                        environment=environment,
                    )
            else:
                draft = prepare_issue_draft(parsed.manifest_file, read_bytes=read_bytes)
                if parsed.apply:
                    if apply_upstream is None:
                        preview = _default_apply_upstream(
                            draft,
                            host=parsed.upstream_host,
                            project_id=parsed.upstream_project_id,
                            credential_file=parsed.upstream_credential_file,
                            read_credential=read_credential,
                            run_command=run_command,
                            environment=environment,
                            find_executable=find_executable,
                        )
                    else:
                        preview = apply_upstream(
                            draft,
                            host=parsed.upstream_host,
                            project_id=parsed.upstream_project_id,
                            credential_file=parsed.upstream_credential_file,
                            read_credential=read_credential,
                            run_command=run_command,
                            environment=environment,
                        )
                elif preview_upstream is None:
                    preview = _default_preview_upstream(
                        draft.preview,
                        host=parsed.upstream_host,
                        project_id=parsed.upstream_project_id,
                        credential_file=parsed.upstream_credential_file,
                        read_credential=read_credential,
                        run_command=run_command,
                        environment=environment,
                        find_executable=find_executable,
                    )
                else:
                    preview = preview_upstream(
                        draft.preview,
                        host=parsed.upstream_host,
                        project_id=parsed.upstream_project_id,
                        credential_file=parsed.upstream_credential_file,
                        read_credential=read_credential,
                        run_command=run_command,
                        environment=environment,
                    )
        except (ManifestError, UpstreamGitLabError) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        write_stdout(_render_finding_preview(preview, as_json=parsed.json))
        return 0
    command = f"{parsed.resource}.{parsed.action}"
    request = CommandRequest(
        command=command,
        issue_iid=getattr(parsed, "issue_iid", None),
        choice=getattr(parsed, "choice", None),
        reason=getattr(parsed, "reason", None),
        evidence_urls=tuple(getattr(parsed, "evidence_urls", [])),
        replacement_issue_iid=getattr(parsed, "replacement_issue_iid", None),
        target_label=getattr(parsed, "target_label", None),
        plan_file=getattr(parsed, "plan_file", None),
        max_segments=getattr(parsed, "max_segments", 16),
        max_activation_seconds=getattr(
            parsed, "max_activation_seconds", DEFAULT_MAX_ACTIVATION_SECONDS
        ),
        debug=parsed.debug,
    )
    try:
        report = dispatch(request, apply=getattr(parsed, "apply", False))
    except LockBusy as error:
        logging.getLogger("agent-work").error("failure=%s", error)
        write_stderr(str(error))
        return 75
    except (RuntimeError, ValueError, OSError) as error:
        logging.getLogger("agent-work").error("failure=%s", error)
        if parsed.debug:
            raise
        write_stderr(str(error))
        return 1
    logging.getLogger("agent-work").info(
        "outcome=%s command=%s", report.outcome, report.command
    )
    write_stdout(_render(report, as_json=parsed.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
