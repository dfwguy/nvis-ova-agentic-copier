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
import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .credential_rotation import (
    CredentialRotationError,
    CredentialRotationReport,
    RotationDependencies,
    atomic_write_environment,
)
from .credential_rotation import rotate_credential as default_rotate_credential
from .domain import DEFAULT_MAX_ACTIVATION_SECONDS, CommandRequest
from .ledger import LockBusy
from .models import CommandReport, LedgerRecord
from .ports import CommandRunner
from .repository import run_subprocess
from .runtime import dispatch as default_dispatch
from .status import project_status
from .upstream_finding import ManifestError, UpstreamFindingPreview
from .upstream_finding import preview_manifest as default_preview_manifest
from .upstream_gitlab import (
    UpstreamGitLabConfig,
    UpstreamGitLabError,
    UpstreamGitLabReader,
)
from .worker import (
    WorkerProtocolError,
    build_worker_result,
    format_worker_result_diagnostic,
    parse_worker_result_build_input,
)


class _PreviewManifest(Protocol):
    """Typed local preview boundary."""

    def __call__(
        self, path: Path, *, read_bytes: Callable[[Path], bytes]
    ) -> UpstreamFindingPreview: ...


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


class _LoadRecord(Protocol):
    """Typed read-only ledger boundary for result building."""

    def __call__(self, issue_iid: int) -> LedgerRecord | None: ...


class _ChangedPaths(Protocol):
    """Typed read-only worktree observation boundary."""

    def __call__(self, worktree: Path) -> tuple[str, ...]: ...


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
    apply_help: str = "apply the operation",
    debug_help: str = "show a traceback on failure",
) -> None:
    if apply:
        parser.add_argument("--apply", action="store_true", help=apply_help)
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    parser.add_argument("--debug", action="store_true", help=debug_help)


def build_parser() -> argparse.ArgumentParser:
    """Build the nested resource/action command surface."""
    parser = argparse.ArgumentParser(prog="agent-work")
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
    finding = resources.add_parser(
        "upstream-finding", help="preview a read-only upstream finding"
    )
    finding_actions = finding.add_subparsers(dest="action", required=True)
    finding_preview = finding_actions.add_parser(
        "preview",
        help="validate and deduplicate an upstream finding without writes",
        description="Read-only upstream GitLab preview; it never posts an issue or writes files.",
    )
    finding_preview.add_argument("--manifest-file", type=Path, required=True)
    finding_preview.add_argument("--upstream-host", required=True)
    finding_preview.add_argument(
        "--upstream-project-id", type=_positive_integer, required=True
    )
    finding_preview.add_argument("--upstream-credential-file", type=Path, required=True)
    _common(
        finding_preview,
        apply_help="accepted for command consistency; preview remains read-only",
    )
    worker_result = resources.add_parser(
        "worker-result", help="build a read-only validated worker result"
    )
    worker_result_actions = worker_result.add_subparsers(dest="action", required=True)
    worker_build = worker_result_actions.add_parser(
        "build", help="derive and validate one protocol-v2 worker result"
    )
    worker_build.add_argument("--issue-iid", type=_positive_integer, required=True)
    worker_build.add_argument("--input-file", type=Path, required=True)
    _common(
        worker_build,
        apply_help="accepted for command consistency; worker-result build remains read-only",
        debug_help="retain bounded diagnostics on failure",
    )
    return parser


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _render(report: CommandReport, *, as_json: bool) -> str:
    if as_json:
        return report.model_dump_json(exclude_none=True)
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
        return " ".join(details)

    def safe(value: object) -> str:
        return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())

    rendered: list[str] = []
    for item in report.items:
        item_details = [
            f"issue=#{item.issue_iid}",
            f"status={item.projected_status or project_status(state=item.state, phase=item.phase, source_sha=item.source_sha)}",
            f"state={item.state}",
            f"phase={item.phase}",
            f"project={item.project_id}",
            f"branch={safe(item.source_branch)}",
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
    """Compose the explicitly configured read-only upstream boundary."""
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
        config,
        read_credential=read_credential,
        run_command=run_command,
    ).preview(preview)


def main(
    arguments: Sequence[str] | None = None,
    *,
    dispatch: Callable[..., CommandReport] = default_dispatch,
    preview_manifest: _PreviewManifest = default_preview_manifest,
    read_bytes: Callable[[Path], bytes] = Path.read_bytes,
    preview_upstream: _PreviewUpstream | None = None,
    read_credential: Callable[[Path], bytes] = Path.read_bytes,
    run_command: CommandRunner = run_subprocess,
    environment: Mapping[str, str] = os.environ,
    find_executable: Callable[[str], str | None] = shutil.which,
    load_record: _LoadRecord | None = None,
    changed_paths: _ChangedPaths | None = None,
    rotate: Callable[..., CredentialRotationReport] = default_rotate_credential,
    atomic_write: Callable[[Path, bytes], None] = atomic_write_environment,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Dispatch one command and return the documented exit status."""
    parsed = build_parser().parse_args(arguments)
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
            if load_record is None or changed_paths is None:
                from .runtime import build_worker_result_dependencies

                dependencies = build_worker_result_dependencies(
                    environment=environment,
                    run_command=run_command,
                )
                record_loader = load_record or dependencies.load_record
                observe_paths = changed_paths or dependencies.changed_paths
            else:
                record_loader = load_record
                observe_paths = changed_paths
            record = record_loader(parsed.issue_iid)
            if record is None:
                raise WorkerProtocolError("issue_iid", "has no retained ledger record")
            build_input = parse_worker_result_build_input(read_bytes(parsed.input_file))
            result = build_worker_result(
                record, build_input, changed_paths=observe_paths(record.worktree)
            )
        except (OSError, RuntimeError, ValueError) as error:
            diagnostic = (
                format_worker_result_diagnostic(error)
                if isinstance(error, WorkerProtocolError)
                else "worker_result: could not be read or validated"
            )
            write_stderr(diagnostic)
            return 1
        rendered_result = result.model_dump_json(exclude_none=True)
        write_stdout(
            rendered_result
            if parsed.json
            else f"<agent-work-result>{rendered_result}</agent-work-result>"
        )
        write_stderr("worker result validated; no changes were made")
        return 0
    if parsed.resource == "upstream-finding" and parsed.action == "preview":
        try:
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
