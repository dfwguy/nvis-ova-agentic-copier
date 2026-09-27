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
import base64
import hashlib
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import stat
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlparse

from . import (
    registry_cleanup,
    template_comparison,
    template_update,
    template_update_publication,
)
from .candidate_boundary import GIT_READ_MAX_CHUNK_BYTES
from .credential_rotation import (
    CredentialRotationError,
    CredentialRotationReport,
    PathEvidence,
    RotationDependencies,
    atomic_write_environment,
)
from .credential_rotation import rotate_credential as default_rotate_credential
from .domain import (
    DEFAULT_MAX_ACTIVATION_SECONDS,
    HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
    CommandRequest,
)
from .gitlab import (
    GitLabError,
    GitLabIssuePublisher,
    GitLabMergeRequestPublisher,
    IssuePublicationReport,
    render_merge_request_description,
)
from .gitlab import publish_issue as default_publish_issue
from .gitlab import publish_merge_request as default_publish_merge_request
from .interactive_receipts import AtomicObjectStore, read_bounded_regular
from .ledger import LedgerCorrupt, LockBusy, state_home
from .models import (
    AgentWorkConfig,
    CommandReport,
    LedgerRecord,
    MergeRequestEvidence,
    WorkerResultEnvelope,
)
from .ports import CommandResult as CommandResultProtocol
from .ports import CommandRunner
from .published_commit import (
    PublishedCommitDependencies,
    PublishedCommitError,
    PublishedCommitInput,
    PublishedCommitRequest,
    append_published_commit,
)
from .red_checkpoint import (
    RedCheckpointEvidence,
)
from .repository import CommandResult, discover_config, run_subprocess
from .review_handoff import (
    ReviewHandoffDependencies,
    ReviewHandoffError,
    begin_review_handoff,
    read_review_handoff,
    review_handoff_manifest,
    submit_review_handoff,
)
from .runtime import dispatch as default_dispatch
from .service_environment import (
    ServiceEnvironmentDependencies,
    ServiceEnvironmentError,
    ServiceEnvironmentReport,
    parse_service_environment,
)
from .service_environment import (
    migrate_service_environment as default_migrate_service_environment,
)
from .slow_root_intake import MAX_SLOW_ROOT_MANIFEST_BYTES, SlowRootReport
from .slow_root_intake import reconcile_slow_roots as default_reconcile_slow_roots
from .status import project_status
from .systemd_smoke import (
    SystemdSmokeDependencies,
    SystemdSmokeError,
    SystemdSmokeReport,
    run_systemd_smoke,
)
from .template_comparison import (
    BinaryCommandRunner,
    TemplateComparison,
    TemplateComparisonError,
)
from .template_comparison import (
    compare_template_revisions as default_compare_template_revisions,
)
from .timing import summarize_phase_timings
from .timing_admission import (
    TimingAdmissionBusy,
    TimingAdmissionLease,
    project_timing_admission,
    run_project_timing_window,
    run_subprocess_child,
    run_supervised_command,
)
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
    retry_judge_validation,
    run_judge_validation,
)
from .worker import (
    AgentInvoker,
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    build_worker_result,
    format_worker_result_diagnostic,
    parse_worker_result_build_input,
    request_candidate_validation,
    submit_worker_result_to_broker,
)


def _package_checkout_root() -> Path:
    """Return the checkout containing the installed agent-work package."""
    return Path(__file__).resolve().parents[2]


def _account_home() -> Path:
    """Return the operating-system home for this effective user."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _validation_project_identity(environment: Mapping[str, str]) -> tuple[str, int]:
    """Require an explicit numeric project for applied timing work."""
    host = environment.get("GITLAB_HOST", "")
    project_text = environment.get("GITLAB_PROJECT_ID", "")
    if not host or "://" in host or "/" in host:
        raise JudgeValidationError("GITLAB_HOST must be a bare hostname")
    if not project_text.isdigit() or int(project_text) <= 0:
        raise JudgeValidationError("GITLAB_PROJECT_ID must be numeric")
    return host, int(project_text)


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


class _SlowTestCase(Protocol):
    name: str
    seconds: float


class _JUnitTiming(Protocol):
    environment: str
    total_seconds: float
    timing_complete: bool

    @property
    def slowest_cases(self) -> Sequence[_SlowTestCase]: ...

    @property
    def over_100ms_cases(self) -> Sequence[_SlowTestCase]: ...


class _ArtifactFailure(Protocol):
    @property
    def stage(self) -> str: ...

    @property
    def reason(self) -> str: ...

    @property
    def environment(self) -> str | None: ...

    @property
    def path(self) -> Path | None: ...


class _ValidationReport(Protocol):
    """Bounded validation report consumed by CLI rendering."""

    candidate_tree: str
    applied: bool
    action: Literal[
        "preview",
        "ran",
        "existing",
        "status",
        "retry_preview",
        "retried",
        "existing_retry",
    ]
    state: Literal[
        "preview",
        "preflight-failure",
        "running",
        "terminal-success",
        "terminal-failure",
        "terminal-artifact-failure",
    ]
    terminal: bool
    exit_code: int | None
    diagnostic: str | None
    elapsed_seconds: float | None

    @property
    def artifact_failure(self) -> _ArtifactFailure | None: ...

    @property
    def failed_tests(self) -> Sequence[str]: ...

    @property
    def failed_junit_digests(self) -> Sequence[str]: ...

    @property
    def hook_failures(self) -> Sequence[_HookFailure]: ...

    @property
    def junit_artifacts(self) -> Sequence[_JUnitArtifact]: ...

    @property
    def junit_timings(self) -> Sequence[_JUnitTiming]: ...


class _ValidationRunAction(Protocol):
    """Injected durable validation-run action."""

    def __call__(self, *, candidate_tree: str, apply: bool) -> _ValidationReport: ...


class _ValidationStatusAction(Protocol):
    """Injected durable validation-status action."""

    def __call__(
        self, *, candidate_tree: str, wait_seconds: int | None
    ) -> _ValidationReport: ...


class _ValidationRetryAction(Protocol):
    """Injected append-only validation retry action."""

    def __call__(self, *, candidate_tree: str, apply: bool) -> _ValidationReport: ...


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
        feedback_audit: bool = False,
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
    read_bytes: Callable[[Path], bytes],
    checkout_root: Callable[[], Path],
    home_directory: Callable[[], Path],
    effective_uid: Callable[[], int],
    inspect_path: Callable[[Path], os.stat_result],
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
        feedback_audit: bool = False,
    ) -> IssuePublicationReport:
        def audit_feedback() -> None:
            checkout = checkout_root()
            units = tuple((checkout / "systemd").glob("*-agent-work.service"))
            if len(units) != 1:
                raise GitLabError("feedback service unit is ambiguous")
            unit = units[0]
            home = home_directory()
            installed = home / ".config/systemd/user" / unit.name
            config_dir = home / ".config" / unit.stem
            env_file = config_dir / "agent-work.env"
            uid = effective_uid()
            for path, expected_mode, directory in (
                (installed, None, False),
                (config_dir, 0o700, True),
                (env_file, 0o600, False),
            ):
                metadata = inspect_path(path)
                if (
                    metadata.st_uid != uid
                    or (
                        stat.S_ISDIR(metadata.st_mode)
                        if directory
                        else stat.S_ISREG(metadata.st_mode)
                    )
                    is False
                    or (
                        expected_mode is not None
                        and stat.S_IMODE(metadata.st_mode) != expected_mode
                    )
                ):
                    raise GitLabError(
                        "installed feedback service configuration is unsafe"
                    )
            source = read_bytes(unit)
            installed_bytes = read_bytes(installed)
            if (
                len(source) > 65536
                or source != installed_bytes
                or source.splitlines().count(
                    f"EnvironmentFile=%h/.config/{unit.stem}/agent-work.env".encode()
                )
                != 1
            ):
                raise GitLabError("installed feedback service unit is invalid")
            environment_bytes = read_bytes(env_file)
            if len(environment_bytes) > 65536:
                raise GitLabError("installed feedback environment is invalid")
            service = parse_service_environment(environment_bytes)
            if service.host != host or service.project_id != project_id:
                raise GitLabError(
                    "installed feedback environment does not match project"
                )
            port.audit_feedback(username=service.factory_username)

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
            feedback_audit=feedback_audit,
            audit_feedback=audit_feedback if feedback_audit else None,
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
        if (
            (getattr(parsed, "resource", None), getattr(parsed, "action", None))
            in {
                ("candidate", "stage"),
                ("candidate", "validate"),
                ("candidate", "status"),
            }
            and parsed.apply
            and (parsed.candidate_socket is None or parsed.broker_nonce is None)
        ):
            self.error("--apply requires --candidate-socket and --broker-nonce")
        return parsed


def _candidate_tree(value: str) -> str:
    """Require the exact staged-tree identifier at the CLI boundary."""
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise argparse.ArgumentTypeError(
            "candidate tree must be 40 lowercase hexadecimal characters"
        )
    return value


def _sha256_digest(value: str) -> str:
    """Require one canonical SHA-256 digest at the CLI boundary."""
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "digest must be 64 lowercase hexadecimal characters"
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
    slow_root = resources.add_parser("slow-root", help="reconcile confirmed slow roots")
    slow_root_actions = slow_root.add_subparsers(dest="action", required=True)
    reconcile = slow_root_actions.add_parser(
        "reconcile", help="preview or file confirmed slow root issues"
    )
    reconcile.add_argument("--manifest-file", type=Path, required=True)
    _common(
        reconcile, apply_help="create confirmed root issues after a second inventory"
    )
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
    smoke = loop_actions.add_parser(
        "smoke", help="run one isolated Codex delegation smoke"
    )
    _common(
        smoke,
        apply_help="run the isolated smoke; omit for a read-only preview",
        debug_help="accepted for command consistency; smoke keeps bounded diagnostics",
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
    _common(retry, apply_help="apply only the exact reviewed retry plan")
    retry.add_argument("--issue-iid", type=_positive_integer, required=True)
    retry.add_argument("--plan-file", type=Path)
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
        description=(
            "Dry-run is the default. --apply prepares and validates an isolated "
            "update; a ready update with changes is published as a merge request "
            "for human review."
        ),
    )
    template_update_parser.add_argument("--candidate-revision", required=True)
    template_update_parser.add_argument("--project-root", type=Path, default=Path("."))
    _common(
        template_update_parser,
        apply_help="apply the update and publish ready changes for human review",
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
    candidate = resources.add_parser(
        "candidate", help="preview or request live controller candidate operations"
    )
    candidate_actions = candidate.add_subparsers(dest="action", required=True)
    for action in ("stage", "read", "validate", "status"):
        candidate_action = candidate_actions.add_parser(
            action,
            help=f"preview or request live candidate {action}; --json selects JSON output",
            description=(
                f"Preview candidate {action} without --apply. A live request needs "
                "--candidate-socket and --broker-nonce; --json selects JSON output."
            ),
        )
        candidate_action.add_argument(
            "--issue-iid", type=_positive_integer, required=True
        )
        candidate_action.add_argument("--candidate-socket", type=Path)
        candidate_action.add_argument("--broker-nonce", type=_broker_nonce)
        if action != "stage":
            candidate_action.add_argument(
                "--candidate-tree", type=_candidate_tree, required=True
            )
        if action == "read":
            candidate_action.add_argument(
                "--manifest",
                action="store_true",
                help="request a paged manifest; exclusive with --complete and chunk flags",
            )
            candidate_action.add_argument(
                "--complete",
                action="store_true",
                help="finish a paged read after every existing side was fetched; exclusive with other read modes",
            )
            candidate_action.add_argument(
                "--path",
                help="changed path for one chunk; requires --side, --offset, and --length",
            )
            candidate_action.add_argument(
                "--side",
                choices=("old", "new"),
                help="old or new side for one chunk; use with --path",
            )
            candidate_action.add_argument(
                "--offset", type=int, help="zero-based byte offset for one chunk"
            )
            candidate_action.add_argument(
                "--length",
                type=int,
                help="chunk byte count (1 to 131072); chunk flags exclude --manifest and --complete",
            )
        _common(
            candidate_action,
            apply_help=(
                f"send candidate {action} to the live private controller socket "
                "instead of previewing"
            ),
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
    issue_publish.add_argument("--marker", required=True)
    issue_publish.add_argument("--priority")
    issue_publish.add_argument("--severity")
    issue_publish.add_argument("--label", action="append", default=[])
    issue_publish.add_argument("--issue-iid", type=_positive_integer)
    issue_publish.add_argument(
        "--feedback-audit",
        action="store_true",
        help="verify factory identity and review safeguards before creating an issue",
    )
    _common(issue_publish, apply_help="create or update the issue; omit for preview")
    validation = resources.add_parser(
        "validation", help="run, retry, or observe one durable Judge validation"
    )
    validation_actions = validation.add_subparsers(dest="action", required=True)
    validation_window = validation_actions.add_parser(
        "window", help="hold project timing admission while a child runs"
    )
    _common(validation_window, apply_help="run the child inside the timing window")
    validation_window.add_argument("command", nargs=argparse.REMAINDER)
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
    validation_retry_parser = validation_actions.add_parser(
        "retry",
        help="preview or apply one pre-hook cleanup retry",
        description=(
            "Without --apply, preview without changing state. With --apply, run one "
            "retry only when recorded cleanup failed before the hook started and "
            "the candidate is the current staged tree."
        ),
    )
    validation_retry_parser.add_argument(
        "--candidate-tree", type=_candidate_tree, required=True
    )
    _common(validation_retry_parser, apply_help="run the one permitted cleanup retry")
    handoff = resources.add_parser(
        "review-handoff", help="retain Green's interactive correction ledger"
    )
    handoff_actions = handoff.add_subparsers(dest="action", required=True)
    for action in ("begin", "submit", "manifest", "read"):
        command = handoff_actions.add_parser(action, help=f"{action} a ledger handoff")
        command.add_argument("--worktree", type=Path, required=True)
        if action == "begin":
            command.add_argument("--issue-iid", type=_positive_integer)
        else:
            command.add_argument("--session-id", required=True)
        if action == "submit":
            command.add_argument("--role", choices=("$tdd-green",), required=True)
            command.add_argument(
                "--review-version", type=_positive_integer, required=True
            )
            command.add_argument(
                "--kind", choices=("correction-ledger",), required=True
            )
            command.add_argument("--input-file", type=Path, required=True)
        if action == "read":
            command.add_argument("--receipt-id", required=True)
        if action in {"manifest", "read"}:
            command.add_argument(
                "--expected-manifest-sha256",
                type=_sha256_digest,
                help="fail if the current manifest differs from the saved digest",
            )
        _common(
            command,
            apply_help="persist the ledger handoff"
            if action in {"begin", "submit"}
            else "accepted for read-only inspection",
        )
    repository = resources.add_parser("repository", help="inspect repository policy")
    published = resources.add_parser(
        "published-commit", help="append one reviewed local commit to an open MR source"
    )
    published_actions = published.add_subparsers(dest="action", required=True)
    append = published_actions.add_parser(
        "append", help="verify and append a Judge tree"
    )
    append.add_argument("--project-id", type=_positive_integer, required=True)
    append.add_argument("--mr-iid", type=_positive_integer, required=True)
    append.add_argument("--worktree", type=Path, required=True)
    append.add_argument("--expected-source-sha", type=_candidate_tree, required=True)
    append.add_argument("--judge-tree", type=_candidate_tree, required=True)
    append.add_argument("--red-ref", required=True)
    append.add_argument("--red-sha", type=_candidate_tree, required=True)
    append.add_argument("--review-session-id", required=True)
    append.add_argument("--review-manifest-sha256", type=_sha256_digest, required=True)
    append.add_argument("--commit-message", required=True)
    _common(append, apply_help="append the reviewed commit to the local source branch")
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


def _read_slow_root_manifest(
    path: Path, *, read_bounded: Callable[..., bytes | None]
) -> bytes:
    """Read one manifest up to its declared byte limit before parsing."""
    payload = read_bounded(path.resolve(), limit=MAX_SLOW_ROOT_MANIFEST_BYTES)
    if payload is None:
        raise ValueError("slow root manifest is unavailable")
    if len(payload) > MAX_SLOW_ROOT_MANIFEST_BYTES:
        raise ValueError("slow root manifest exceeds 262144 bytes")
    return payload


def _review_handoff_dependencies(
    *,
    worktree: Path,
    issue_iid: int | None,
    environment: Mapping[str, str],
    run_command: CommandRunner,
) -> ReviewHandoffDependencies:
    """Wire candidate observation to a private controller-side object store."""
    candidate = worktree.resolve(strict=True)

    git_environment = {
        key: value for key, value in environment.items() if not key.startswith("GIT_")
    }
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"

    def git_read(
        checkout: Path,
        *args: str,
        command_environment: Mapping[str, str] | None = None,
    ) -> str:
        result = run_command(
            ("git", "-C", str(checkout), *args),
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            environment=command_environment or git_environment,
        )
        if result.returncode != 0:
            raise ReviewHandoffError("candidate Git observation failed")
        return result.stdout.strip()

    controller = Path(__file__).resolve().parents[2]
    host = environment.get("GITLAB_HOST", "")
    if not host or "://" in host or "/" in host or ":" in host:
        raise ReviewHandoffError("GITLAB_HOST must be a bare hostname")

    def origin_identity(checkout: Path) -> tuple[str, str]:
        origin = git_read(checkout, "remote", "get-url", "origin")
        parsed = urlparse(origin)
        origin_host = parsed.hostname
        path = parsed.path.removesuffix(".git").strip("/")
        if (
            parsed.scheme != "https"
            or origin_host is None
            or origin_host != host
            or parsed.username
            or parsed.password
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.params
            or not path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ReviewHandoffError(
                "review handoff origin must be credential-free project HTTPS"
            )
        return origin_host, path

    if origin_identity(candidate) != origin_identity(controller):
        raise ReviewHandoffError("candidate origin differs from trusted controller")

    git_directory = Path(git_read(candidate, "rev-parse", "--absolute-git-dir"))
    if not git_directory.is_absolute():
        raise ReviewHandoffError("candidate Git directory is not absolute")
    key = hashlib.sha256(str(git_directory.resolve()).encode()).hexdigest()
    state = state_home(environment=environment)
    validation_root = state / "agent-work-validation" / key
    store = AtomicObjectStore(state / "agent-work-review-handoff" / key)
    try:
        project_id = int(environment["GITLAB_PROJECT_ID"])
    except (KeyError, ValueError) as error:
        raise ReviewHandoffError("numeric project ID is required") from error
    if project_id <= 0:
        raise ReviewHandoffError("numeric project ID is required")

    def observe() -> dict[str, object]:
        index_path = Path(git_read(candidate, "rev-parse", "--git-path", "index"))
        objects_path = Path(git_read(candidate, "rev-parse", "--git-path", "objects"))
        if not index_path.is_absolute() or not objects_path.is_absolute():
            raise ReviewHandoffError("candidate Git paths are not absolute")
        index_bytes = read_bounded_regular(index_path, limit=64 * 1024 * 1024)
        if index_bytes is None or len(index_bytes) > 64 * 1024 * 1024:
            raise ReviewHandoffError("candidate Git index is missing or oversized")
        try:
            shared_objects = objects_path.resolve(strict=True)
        except OSError as error:
            raise ReviewHandoffError("candidate Git objects are unavailable") from error
        if not shared_objects.is_dir() or ":" in str(shared_objects):
            raise ReviewHandoffError("candidate Git objects path is invalid")
        with tempfile.TemporaryDirectory(
            prefix="agent-work-review-tree-", dir="/tmp"
        ) as scratch_name:
            scratch = Path(scratch_name)
            private_index = scratch / "index"
            descriptor = os.open(
                private_index,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(index_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            private_objects = scratch / "objects"
            private_objects.mkdir(mode=0o700)
            tree = git_read(
                candidate,
                "write-tree",
                command_environment={
                    **git_environment,
                    "GIT_INDEX_FILE": str(private_index),
                    "GIT_OBJECT_DIRECTORY": str(private_objects),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(shared_objects),
                },
            )
        if read_bounded_regular(index_path, limit=64 * 1024 * 1024) != index_bytes:
            raise ReviewHandoffError("candidate Git index changed during observation")
        branch = git_read(candidate, "symbolic-ref", "--short", "HEAD")
        if re.fullmatch(r"[0-9a-f]{40}", tree) is None or not branch:
            raise ReviewHandoffError("candidate Git identity is malformed")
        issue_match = re.search(
            r"(?:^|/)(?:issue-?|agent/issue-)([1-9][0-9]*)(?:-|$)", branch
        )
        observed_issue = int(issue_match.group(1)) if issue_match else None
        if issue_iid is not None and issue_iid != observed_issue:
            raise ReviewHandoffError(
                "requested issue differs from branch-derived association"
            )
        record_path = validation_root / f"{tree}.json"
        raw = read_bounded_regular(record_path)
        if raw is None or len(raw) > 1_048_576:
            raise ReviewHandoffError("successful Judge validation is missing")
        try:
            record = json.loads(raw)
            if not isinstance(record, dict):
                raise TypeError("invalid validation record")
            artifacts = record["junit_artifacts"]
            if (
                type(record["schema_version"]) is not int
                or record["schema_version"] != 2
                or record["candidate_tree"] != tree
                or record["state"] != "terminal-success"
                or type(record["returncode"]) is not int
                or record["returncode"] != 0
                or not isinstance(artifacts, list)
                or len(artifacts) != 2
            ):
                raise ValueError("invalid validation record")
            digests = []
            for index, artifact in enumerate(artifacts):
                if not isinstance(artifact, dict):
                    raise TypeError("invalid JUnit artifact")
                artifact_fields = cast("Mapping[str, object]", artifact)
                expected_environment = ("py311", "py314")[index]
                expected_path = (
                    validation_root / tree / f"test-results-{expected_environment}.xml"
                )
                artifact_path = artifact_fields.get("path")
                digest = artifact_fields.get("sha256")
                count = artifact_fields.get("test_count")
                if (
                    artifact_fields.get("environment") != expected_environment
                    or not isinstance(artifact_path, str)
                    or Path(artifact_path) != expected_path
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or type(count) is not int
                    or count < 0
                ):
                    raise ValueError("invalid JUnit identity")
                content = read_bounded_regular(expected_path)
                if content is None or hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("invalid JUnit digest")
                digests.append(f"{expected_environment}:{digest}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ReviewHandoffError("Judge evidence is malformed or stale") from error
        return {
            "project_id": project_id,
            "issue_iid": observed_issue,
            "branch": branch,
            "worktree": str(candidate),
            "candidate_tree": tree,
            "validation_state": "terminal-success",
            "junit_digests": tuple(digests),
        }

    return ReviewHandoffDependencies(
        observe_candidate=observe,
        read_input=read_bounded_regular,
        write_object=store.write_once,
        read_object=store.read,
        list_objects=store.list_prefix,
        nonce=lambda: secrets.token_hex(16),
        clock=time.time,
    )


def _published_commit_dependencies(
    *,
    worktree: Path,
    environment: Mapping[str, str],
    run_command: CommandRunner,
) -> PublishedCommitDependencies:
    """Bind the append operation to real Git and fresh private Judge evidence."""
    host = environment.get("GITLAB_HOST", "")
    project_text = environment.get("GITLAB_PROJECT_ID", "")
    if not host or "://" in host or "/" in host or ":" in host:
        raise PublishedCommitError("GITLAB_HOST must be a bare hostname")
    if not project_text.isdigit() or int(project_text) <= 0:
        raise PublishedCommitError("GITLAB_PROJECT_ID must be numeric")
    configured_project = int(project_text)
    candidate = worktree.resolve(strict=True)
    handoff = _review_handoff_dependencies(
        worktree=candidate,
        issue_iid=None,
        environment=environment,
        run_command=run_command,
    )
    git_environment = {
        key: value for key, value in environment.items() if not key.startswith("GIT_")
    }
    git_environment["GIT_OPTIONAL_LOCKS"] = "0"

    def command(*arguments: str, stdin: str | None = None) -> str:
        result = run_command(
            arguments,
            stdin=stdin,
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            environment=git_environment,
        )
        if result.returncode != 0:
            raise PublishedCommitError("published commit boundary command failed")
        return result.stdout.strip()

    def git(*arguments: str, stdin: str | None = None) -> str:
        return command("git", "-C", str(candidate), *arguments, stdin=stdin)

    def api(path: str) -> object:
        raw = command("glab", "api", "--hostname", host, path)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise PublishedCommitError("GitLab returned invalid JSON") from error

    def read_project(project_id: int) -> dict[str, object]:
        if project_id != configured_project:
            raise PublishedCommitError(
                "project differs from configured numeric project"
            )
        value = api(f"projects/{project_id}")
        if not isinstance(value, dict):
            raise PublishedCommitError("GitLab project payload is invalid")
        return cast("dict[str, object]", value)

    def read_open_mrs(project_id: int, branch: str) -> list[dict[str, object]]:
        path = (
            f"projects/{project_id}/merge_requests?state=opened"
            f"&source_branch={quote(branch, safe='')}&per_page=100"
        )
        value = api(path)
        if not isinstance(value, list) or len(value) >= 100:
            raise PublishedCommitError("open merge request inventory is incomplete")
        if not all(isinstance(item, dict) for item in value):
            raise PublishedCommitError("open merge request payload is invalid")
        return cast("list[dict[str, object]]", value)

    def read_remote_source(branch: str) -> str:
        value = api(
            f"projects/{configured_project}/repository/branches"
            f"?search={quote(branch, safe='')}&per_page=100"
        )
        if not isinstance(value, list) or len(value) >= 100:
            raise PublishedCommitError("remote branch inventory is incomplete")
        if not all(isinstance(item, dict) for item in value):
            raise PublishedCommitError("remote branch inventory is malformed")
        branches = cast("list[dict[str, object]]", value)
        matching = [item for item in branches if item.get("name") == branch]
        if len(matching) != 1:
            raise PublishedCommitError("remote source branch is missing or ambiguous")
        branch_data = matching[0]
        commit = branch_data.get("commit")
        if not isinstance(commit, dict):
            raise PublishedCommitError("remote source commit is malformed")
        commit_data = cast("dict[str, object]", commit)
        if not isinstance(commit_data.get("id"), str):
            raise PublishedCommitError("remote source commit is malformed")
        return cast("str", commit_data["id"])

    def observe_local(checkout: Path, red_ref: str) -> dict[str, object]:
        if checkout.resolve(strict=True) != candidate:
            raise PublishedCommitError("requested worktree changed")
        judged = handoff.observe_candidate()
        branch = git("symbolic-ref", "--short", "HEAD")
        head_ref = git("symbolic-ref", "HEAD")
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"):
            result = run_command(
                ("git", "-C", str(candidate), "rev-parse", "-q", "--verify", marker),
                timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
                environment=git_environment,
            )
            if result.returncode == 0:
                raise PublishedCommitError("merge or rewrite is in progress")
        return {
            "branch": branch,
            "head_ref": head_ref,
            "head_sha": git("rev-parse", "HEAD"),
            "staged_tree": judged["candidate_tree"],
            "red_sha": git("rev-parse", "--verify", red_ref),
            "origin_url": git("remote", "get-url", "origin"),
            "merge_in_progress": False,
            "unstaged_changes": bool(git("diff", "--name-only")),
            "untracked_files": bool(git("ls-files", "--others", "--exclude-standard")),
        }

    def create_commit(tree: str, parent: str, message: str) -> str:
        return git("commit-tree", tree, "-p", parent, "-F", "-", stdin=message)

    def compare_and_swap_ref(ref: str, new: str, old: str) -> bool:
        result = run_command(
            ("git", "-C", str(candidate), "update-ref", ref, new, old),
            timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
            environment=git_environment,
        )
        return result.returncode == 0

    def inspect_commit(sha: str) -> dict[str, object]:
        raw = git("cat-file", "-p", sha)
        header, separator, message = raw.partition("\n\n")
        if not separator:
            raise PublishedCommitError("new commit object is malformed")
        lines = header.splitlines()
        trees = [
            line.removeprefix("tree ") for line in lines if line.startswith("tree ")
        ]
        parents = tuple(
            line.removeprefix("parent ") for line in lines if line.startswith("parent ")
        )
        return {
            "sha": sha,
            "parents": parents,
            "tree": trees[0] if len(trees) == 1 else None,
            "message": message,
        }

    return PublishedCommitDependencies(
        read_project=read_project,
        read_open_mrs=read_open_mrs,
        read_remote_source=read_remote_source,
        observe_local=observe_local,
        read_review_manifest=lambda checkout, session: review_handoff_manifest(
            session_ref=session, dependencies=handoff
        ),
        create_commit=create_commit,
        compare_and_swap_ref=compare_and_swap_ref,
        inspect_commit=inspect_commit,
    )


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


def _render_candidate_read(receipt: dict[str, object]) -> str:
    """Show the exact read evidence with labels while escaping terminal text."""
    safe = _sanitize_human_value
    lines = ["Candidate read"]
    for key, label in (
        ("candidate_tree", "Tree"),
        ("base_tree", "Base tree"),
        ("base_sha", "Base commit"),
        ("project_id", "Project"),
        ("issue_iid", "Issue"),
        ("ledger_revision", "Ledger revision"),
        ("source_branch", "Source branch"),
        ("nonce", "Session nonce"),
        ("state", "State"),
    ):
        if key in receipt:
            lines.append(f"{label}: {safe(receipt[key])}")
    files = receipt.get("files")
    if isinstance(files, list):
        lines.append("Paged manifest:" if receipt.get("paged") is True else "Files:")
        for item in files:
            if not isinstance(item, dict):
                continue
            file = cast(dict[str, object], item)
            lines.append(f"  Path: {safe(file.get('path', ''))}")
            for side, label in (("old", "Old"), ("new", "New")):
                mode = file.get(f"{side}_mode")
                oid = file.get(f"{side}_oid")
                if mode is None:
                    lines.append(f"    {label}: absent")
                    continue
                lines.append(f"    {label} mode: {safe(mode)}")
                lines.append(f"    {label} blob: {safe(oid)}")
                if receipt.get("paged") is True:
                    lines.append(f"    {label} bytes: {safe(file.get(f'{side}_size'))}")
                    lines.append(
                        f"    {label} SHA-256: {safe(file.get(f'{side}_sha256'))}"
                    )
                else:
                    lines.append(
                        f"    {label} content: "
                        + json.dumps(file.get(f"{side}_content"), ensure_ascii=True)
                    )
    elif "data_base64" in receipt:
        for key, label in (
            ("path", "Path"),
            ("side", "Side"),
            ("offset", "Byte offset"),
            ("length", "Byte count"),
            ("sha256", "Whole-side SHA-256"),
            ("data_base64", "Bytes (base64)"),
        ):
            lines.append(f"{label}: {safe(receipt.get(key, ''))}")
    return "\n".join(lines)


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
    systemd_smoke_action: Callable[..., SystemdSmokeReport] = run_systemd_smoke,
    discover_agent_work_config: Callable[..., AgentWorkConfig] = discover_config,
    sleeper: Callable[[float], None] = time.sleep,
    environment: Mapping[str, str] = os.environ,
    find_executable: Callable[[str], str | None] = shutil.which,
    current_checkout: Callable[[], Path] = Path.cwd,
    checkout_root: Callable[[], Path] = _package_checkout_root,
    home_directory: Callable[[], Path] = _account_home,
    effective_uid: Callable[[], int] = os.geteuid,
    inspect_path: Callable[[Path], os.stat_result] = lambda path: path.lstat(),
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
    candidate_request: Callable[..., dict[str, object]] = request_candidate_validation,
    publish_template_update: Callable[..., object] | None = None,
    validation_run: _ValidationRunAction | None = None,
    validation_status: _ValidationStatusAction | None = None,
    validation_retry: _ValidationRetryAction | None = None,
    run_validation_window: Callable[[tuple[str, ...], bool], int] | None = None,
    run_validation_hook: (
        Callable[[tuple[str, ...], TimingAdmissionLease | None], CommandResultProtocol]
        | None
    ) = None,
    read_validation_artifact: Callable[[Path], bytes | None] = (
        _read_validation_artifact
    ),
    remove_validation_artifact: Callable[[Path], None] = _remove_validation_artifact,
    registry_cleanup_action: _RegistryCleanupAction | None = None,
    slow_root_reconcile_action: Callable[..., SlowRootReport] | None = None,
    slow_root_manifest_reader: Callable[[Path], bytes] | None = None,
    read_bounded_regular_file: Callable[..., bytes | None] = read_bounded_regular,
    build_published_commit_dependencies: Callable[
        ..., PublishedCommitDependencies
    ] = _published_commit_dependencies,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Dispatch one command and return the documented exit status."""
    parsed = build_parser().parse_args(arguments)
    if parsed.resource == "published-commit":
        try:
            user_input = PublishedCommitInput.model_validate(vars(parsed))
            request = PublishedCommitRequest(
                project_id=user_input.project_id,
                mr_iid=user_input.mr_iid,
                worktree=user_input.worktree.resolve(strict=True),
                expected_source_sha=user_input.expected_source_sha,
                judge_tree=user_input.judge_tree,
                red_ref=user_input.red_ref,
                red_sha=user_input.red_sha,
                review_session_id=user_input.review_session_id,
                review_manifest_sha256=user_input.review_manifest_sha256,
                commit_message=user_input.commit_message,
            )
            report = append_published_commit(
                request,
                apply=parsed.apply,
                dependencies=build_published_commit_dependencies(
                    worktree=request.worktree,
                    environment=environment,
                    run_command=run_command,
                ),
            )
        except (OSError, RuntimeError, ValueError) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        payload = {
            "project_id": report.project_id,
            "mr_iid": report.mr_iid,
            "old_sha": report.old_sha,
            "new_sha": report.new_sha,
            "tree": report.tree,
            "applied": report.applied,
        }
        write_stdout(
            json.dumps(payload, separators=(",", ":"))
            if parsed.json
            else (
                f"appended {report.new_sha} to merge request {report.mr_iid}"
                if report.applied
                else f"[dry-run] reviewed tree {report.tree} is ready to append"
            )
        )
        return 0
    if parsed.resource == "slow-root" and parsed.action == "reconcile":
        try:
            project_id = int(environment.get("GITLAB_PROJECT_ID", ""))
            host = environment.get("GITLAB_HOST", "")
            if project_id <= 0 or not host:
                raise ValueError("slow root GitLab configuration is invalid")
            manifest_reader = (
                slow_root_manifest_reader
                if slow_root_manifest_reader is not None
                else partial(
                    _read_slow_root_manifest, read_bounded=read_bounded_regular_file
                )
            )
            manifest_bytes = manifest_reader(parsed.manifest_file)
            action = slow_root_reconcile_action
            if action is None:
                glab = find_executable("glab")
                if glab is None or not Path(glab).is_absolute():
                    raise ValueError("slow root GitLab configuration is invalid")
                port = GitLabIssuePublisher(
                    host=host,
                    project_id=project_id,
                    glab_path=str(Path(glab).resolve()),
                    run_command=run_command,
                )
                action = partial(
                    default_reconcile_slow_roots,
                    list_issues=partial(port.list_all_issues, project_id=project_id),
                    create_issue=port.create_issue,
                    fetch_issue=lambda iid: port.fetch_issue(
                        project_id=project_id, iid=iid
                    ),
                )
            report = action(manifest_bytes, project_id=project_id, apply=parsed.apply)
        except (GitLabError, OSError, RuntimeError, ValueError) as error:
            if parsed.debug:
                raise
            write_stderr(str(error))
            return 1
        payload = {
            "applied": report.applied,
            "items": [
                {
                    "root_key": item.root_key,
                    "action": item.action,
                    "issue_iid": item.issue_iid,
                }
                for item in report.items
            ],
        }
        write_stdout(
            json.dumps(payload, sort_keys=True)
            if parsed.json
            else "\n".join(
                f"{item.root_key}: {item.action}"
                + (f" #{item.issue_iid}" if item.issue_iid is not None else "")
                for item in report.items
            )
        )
        return 0
    if parsed.resource == "loop" and parsed.action == "smoke":
        if not parsed.apply:
            write_stdout(
                json.dumps(
                    {
                        "schema_version": 1,
                        "command": "loop.smoke",
                        "applied": False,
                        "outcome": "preview",
                    },
                    separators=(",", ":"),
                )
                if parsed.json
                else "preview"
            )
            return 0
        try:
            configuration = discover_agent_work_config(
                current_directory=current_checkout(),
                environment=environment,
                run_command=run_command,
            )
            report = systemd_smoke_action(
                dependencies=SystemdSmokeDependencies(
                    run_command=cast(Callable[..., CommandResult], run_command),
                    environment=environment,
                    checkout=configuration.checkout,
                    timeout_seconds=300,
                    codex_home=Path(
                        environment.get("CODEX_HOME", str(home_directory() / ".codex"))
                    ),
                )
            )
        except (OSError, RuntimeError, SystemdSmokeError) as error:
            write_stderr(str(error))
            return 1
        payload = {
            "schema_version": 1,
            "command": "loop.smoke",
            "applied": True,
            "outcome": "passed",
            "broker_accepted": report.broker_accepted,
        }
        write_stdout(
            json.dumps(payload, separators=(",", ":")) if parsed.json else "passed"
        )
        return 0
    if parsed.resource == "review-handoff":
        human_output: str | None = None
        try:
            dependencies = _review_handoff_dependencies(
                worktree=parsed.worktree,
                issue_iid=getattr(parsed, "issue_iid", None),
                environment=environment,
                run_command=run_command,
            )
            if parsed.action == "begin":
                result = begin_review_handoff(
                    worktree=parsed.worktree.resolve(strict=True),
                    apply=parsed.apply,
                    dependencies=dependencies,
                )
                payload: dict[str, object] = {
                    "session_id": result.session_ref,
                    "applied": result.applied,
                }
            elif parsed.action == "submit":
                input_path = parsed.input_file
                if not input_path.is_absolute():
                    input_path = Path.cwd() / input_path
                result = submit_review_handoff(
                    session_ref=parsed.session_id,
                    role=parsed.role,
                    version=parsed.review_version,
                    kind=parsed.kind,
                    payload_path=input_path,
                    apply=parsed.apply,
                    dependencies=dependencies,
                )
                payload = {
                    "receipt_id": result.receipt_ref,
                    "sha256": result.sha256,
                    "applied": result.applied,
                }
            elif parsed.action == "manifest":
                result = review_handoff_manifest(
                    session_ref=parsed.session_id, dependencies=dependencies
                )
                if (
                    parsed.expected_manifest_sha256 is not None
                    and result.sha256 != parsed.expected_manifest_sha256
                ):
                    raise ReviewHandoffError(
                        "review manifest changed since the saved handoff"
                    )
                payload = {
                    "session_id": result.session_ref,
                    "project_id": result.project_id,
                    "issue_iid": result.issue_iid,
                    "branch": result.branch,
                    "candidate_tree": result.candidate_tree,
                    "junit_digests": list(result.junit_digests),
                    "receipt_ids": list(result.receipt_refs),
                    "receipts": [
                        {
                            "role": item.role,
                            "review_version": item.review_version,
                            "kind": item.kind,
                            "receipt_id": item.receipt_ref,
                            "sha256": item.sha256,
                        }
                        for item in result.receipts
                    ],
                    "sha256": result.sha256,
                }
            else:
                contents = read_review_handoff(
                    session_ref=parsed.session_id,
                    receipt_ref=parsed.receipt_id,
                    dependencies=dependencies,
                    expected_manifest_sha256=parsed.expected_manifest_sha256,
                )
                payload = {
                    "receipt_id": parsed.receipt_id,
                    "sha256": hashlib.sha256(contents).hexdigest(),
                    "content_base64": base64.b64encode(contents).decode("ascii"),
                }
                try:
                    decoded = contents.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = ""
                human_output = (
                    decoded
                    if decoded
                    and all(
                        character.isprintable() or character in "\n\t"
                        for character in decoded
                    )
                    else "Receipt contains binary or control bytes; use --json to read exact bytes."
                )
        except (ReviewHandoffError, OSError, ValueError) as error:
            write_stderr(str(error))
            return 1
        write_stdout(
            json.dumps(payload, separators=(",", ":"))
            if parsed.json
            else human_output
            if human_output is not None
            else " ".join(f"{key}={value}" for key, value in payload.items())
        )
        return 0
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
                read_bytes=read_bytes,
                checkout_root=checkout_root,
                home_directory=home_directory,
                effective_uid=effective_uid,
                inspect_path=inspect_path,
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
                **({"feedback_audit": True} if parsed.feedback_audit else {}),
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
            if parsed.action == "window":
                command = tuple(parsed.command)
                if command and command[0] == "--":
                    command = command[1:]
                if not command:
                    build_parser().error("validation window requires a child command")
                if not parsed.apply:
                    write_stdout(
                        json.dumps(
                            {
                                "action": "window",
                                "applied": False,
                                "command": list(command),
                            }
                        )
                        if parsed.json
                        else "[dry-run] project timing window preview"
                    )
                    return 0
                if run_validation_window is not None:
                    exit_code = run_validation_window(command, True)
                else:
                    host, project_id = _validation_project_identity(environment)
                    exit_code = run_project_timing_window(
                        command=command,
                        run_child=partial(
                            run_subprocess_child, json_output=parsed.json
                        ),
                        host=host,
                        project_id=project_id,
                        environment=environment,
                        wait_seconds=30.0,
                        monotonic_clock=time.monotonic,
                        sleeper=sleeper,
                    )
                if parsed.json:
                    write_stdout(
                        json.dumps(
                            {
                                "action": "window",
                                "applied": True,
                                "exit_code": exit_code,
                            },
                            separators=(",", ":"),
                        )
                    )
                return 0 if exit_code == 0 else 1
            if (
                (parsed.action == "run" and validation_run is None)
                or (parsed.action == "status" and validation_status is None)
                or (parsed.action == "retry" and validation_retry is None)
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

                def read_retry_record(tree: str) -> dict[str, object] | None:
                    path = state_root / f"{tree}.retry.json"
                    if not path.exists():
                        return None
                    record = json.loads(path.read_text())
                    if not isinstance(record, dict):
                        raise JudgeValidationError(
                            "validation retry record is malformed"
                        )
                    return record

                def write_retry_record(tree: str, record: dict[str, object]) -> None:
                    state_root.mkdir(parents=True, exist_ok=True)
                    atomic_write(
                        state_root / f"{tree}.retry.json",
                        json.dumps(record, separators=(",", ":")).encode(),
                    )

                @contextmanager
                def candidate_lock(tree: str) -> Iterator[None]:
                    state_root.mkdir(parents=True, exist_ok=True)
                    with (state_root / f"{tree}.lock").open("w") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX)
                        yield

                snapshot_store = FilesystemJUnitSnapshotStore(root=state_root)
                project_identity = (
                    _validation_project_identity(environment)
                    if parsed.action in {"run", "retry"} and parsed.apply
                    else None
                )

                def project_admission() -> AbstractContextManager[object]:
                    if project_identity is None:
                        return nullcontext()
                    host, project_id = project_identity
                    return project_timing_admission(
                        host=host,
                        project_id=project_id,
                        environment=environment,
                        wait_seconds=30.0,
                        monotonic_clock=time.monotonic,
                        sleeper=sleeper,
                    )

                def validation_hook(
                    command: tuple[str, ...], lease: TimingAdmissionLease | None
                ) -> CommandResultProtocol:
                    if run_validation_hook is not None:
                        return run_validation_hook(command, lease)
                    if lease is None:
                        return run_command(command)
                    result = run_supervised_command(
                        command, lease=lease, environment=environment
                    )
                    return CommandResult(
                        result.returncode, result.stdout, result.stderr
                    )

                validation_dependencies = JudgeValidationDependencies(
                    current_staged_tree=lambda: git_read(("git", "write-tree")),
                    read_record=read_record,
                    atomic_write_record=write_record,
                    candidate_lock=candidate_lock,
                    project_admission=project_admission,
                    run_command=run_command,
                    run_license_preflight=lambda tree: run_command(
                        (
                            sys.executable,
                            "scripts/check_licenses.py",
                            "--root",
                            ".",
                            "--policy",
                            ".license-policy.toml",
                            "--candidate-tree",
                            tree,
                            "--json",
                        )
                    ),
                    run_hook=validation_hook,
                    monotonic_clock=time.monotonic,
                    sleeper=time.sleep,
                    read_artifact=read_validation_artifact,
                    remove_junit_artifact=remove_validation_artifact,
                    capture_junit_artifacts=lambda tree, artifacts: (
                        snapshot_store.capture(candidate_tree=tree, artifacts=artifacts)
                    ),
                    read_retry_record=read_retry_record,
                    atomic_write_retry_record=write_retry_record,
                )
                if parsed.action == "run":
                    validation_run = partial(
                        run_judge_validation, dependencies=validation_dependencies
                    )
                elif parsed.action == "status":
                    validation_status = partial(
                        observe_judge_validation, dependencies=validation_dependencies
                    )
                else:
                    validation_retry = partial(
                        retry_judge_validation, dependencies=validation_dependencies
                    )
            if parsed.action == "run":
                assert validation_run is not None
                report = validation_run(
                    candidate_tree=parsed.candidate_tree,
                    apply=getattr(parsed, "apply", False),
                )
            elif parsed.action == "status":
                assert validation_status is not None
                report = validation_status(
                    candidate_tree=parsed.candidate_tree,
                    wait_seconds=getattr(parsed, "wait_seconds", None),
                )
            else:
                assert validation_retry is not None
                report = validation_retry(
                    candidate_tree=parsed.candidate_tree,
                    apply=getattr(parsed, "apply", False),
                )
        except TimingAdmissionBusy:
            if parsed.action == "window" and parsed.json and parsed.apply:
                write_stdout(
                    json.dumps(
                        {
                            "action": "window",
                            "applied": True,
                            "state": "deferred",
                            "reason": "project_timing_admission_busy",
                        },
                        separators=(",", ":"),
                    )
                )
            elif parsed.action in {"run", "retry"} and parsed.json and parsed.apply:
                write_stdout(
                    json.dumps(
                        {
                            "candidate_tree": parsed.candidate_tree,
                            "state": "deferred",
                            "reason": "project_timing_admission_busy",
                        },
                        separators=(",", ":"),
                    )
                )
            else:
                write_stderr("project timing admission busy")
            return 1
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
            "failed_tests": list(report.failed_tests),
            "failed_junit_digests": list(report.failed_junit_digests),
            "elapsed_seconds": report.elapsed_seconds,
            "junit_timings": [
                {
                    "environment": item.environment,
                    "total_seconds": item.total_seconds,
                    "slowest_cases": [
                        {"name": case.name, "seconds": case.seconds}
                        for case in item.slowest_cases
                    ],
                    "timing_complete": item.timing_complete,
                    "over_100ms_cases": [
                        {"name": case.name, "seconds": case.seconds}
                        for case in item.over_100ms_cases
                    ],
                }
                for item in report.junit_timings
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
        if (failure := getattr(report, "artifact_failure", None)) is not None:
            payload = {
                **payload,
                "artifact_failure": {
                    "stage": failure.stage,
                    "reason": failure.reason,
                    "environment": failure.environment,
                    "path": str(failure.path) if failure.path is not None else None,
                },
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
        return 0 if report.state in {"preview", "terminal-success"} else 1
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
        and parsed.action in {"canary", "recover", "retry"}
        and parsed.apply
        and parsed.plan_file is None
    ):
        write_stderr("--apply requires --plan-file")
        return 2
    if parsed.resource == "candidate" and parsed.action in {
        "stage",
        "read",
        "validate",
        "status",
    }:
        selector: dict[str, object] | None = None
        if parsed.action == "read":
            chunk_values = (parsed.path, parsed.side, parsed.offset, parsed.length)
            has_chunk = any(value is not None for value in chunk_values)
            if sum((parsed.manifest, parsed.complete, has_chunk)) > 1 or (
                has_chunk
                and (
                    any(value is None for value in chunk_values)
                    or parsed.offset < 0
                    or not 0 < parsed.length <= GIT_READ_MAX_CHUNK_BYTES
                )
            ):
                write_stderr("candidate read selector is invalid")
                return 2
            if parsed.manifest:
                selector = {"mode": "manifest"}
            elif parsed.complete:
                selector = {"mode": "complete"}
            elif has_chunk:
                selector = {
                    "mode": "chunk",
                    "path": parsed.path,
                    "side": parsed.side,
                    "offset": parsed.offset,
                    "length": parsed.length,
                }
        if not parsed.apply:
            preview = {
                "action": parsed.action,
                "applied": False,
                "candidate_tree": getattr(parsed, "candidate_tree", None),
                "issue_iid": parsed.issue_iid,
                "state": "preview",
            }
            if parsed.action == "read":
                preview["read_mode"] = (
                    "plain" if selector is None else cast(str, selector["mode"])
                )
                if selector is not None and selector["mode"] == "chunk":
                    preview.update(
                        {
                            key: selector[key]
                            for key in ("path", "side", "offset", "length")
                        }
                    )
            write_stdout(
                json.dumps(preview, separators=(",", ":"), sort_keys=True)
                if parsed.json
                else " ".join(
                    f"{key}={json.dumps(value) if key == 'path' else value}"
                    for key, value in preview.items()
                )
            )
            return 0
        if parsed.candidate_socket is None or parsed.broker_nonce is None:
            write_stderr("--apply requires --candidate-socket and --broker-nonce")
            return 2
        try:
            receipt = candidate_request(
                cast(Path, parsed.candidate_socket),
                WorkerResultBrokerClientRequest(
                    nonce=cast(str, parsed.broker_nonce), issue_iid=parsed.issue_iid
                ),
                action=parsed.action,
                candidate_tree=getattr(parsed, "candidate_tree", None),
                **({"selector": selector} if selector is not None else {}),
            )
        except WorkerProtocolError as error:
            write_stderr(format_worker_result_diagnostic(error))
            return 1
        write_stdout(
            json.dumps(receipt, separators=(",", ":"), sort_keys=True)
            if parsed.json
            else _render_candidate_read(receipt)
            if parsed.action == "read"
            else " ".join(
                f"{key}={receipt[key]}"
                for key in (
                    "candidate_tree",
                    "state",
                    "changed_path_count",
                    "evidence_id",
                    "diagnostic",
                )
                if key in receipt and receipt[key] is not None
            )
        )
        return (
            0
            if parsed.action in {"stage", "read"}
            or receipt.get("state") == "terminal-success"
            else 1
        )
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
