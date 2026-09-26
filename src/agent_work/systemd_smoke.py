# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bounded Codex delegation proof used by the user-systemd smoke unit."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import cast

from .domain import RunnerState, WorkflowPhase, sanitize_diagnostic
from .repository import CommandResult, RepositoryError, run_subprocess
from .worker import (
    WorkerProtocolError,
    WorkerResultBrokerRequest,
    _agent_command,
    create_worker_result_broker,
)


class SystemdSmokeError(RuntimeError):
    """The isolated smoke producer did not prove its required delegation."""


@dataclass(frozen=True)
class SystemdSmokeDependencies:
    """Injected operating-system boundary for one smoke activation."""

    run_command: Callable[..., CommandResult]
    environment: Mapping[str, str]
    checkout: Path
    timeout_seconds: int
    codex_home: Path
    temporary_directory: Path | None = None
    read_rollout_metadata: Callable[[Path], Iterable[object]] | None = None
    run_git_command: Callable[..., CommandResult] = run_subprocess


@dataclass(frozen=True)
class SystemdSmokeReport:
    """The in-memory result accepted by the smoke controller."""

    broker_accepted: bool
    broker_artifact: str


_BALANCED_TASK = "architect_proposal_balanced"
_FAST_TASKS = {"architect_proposal_fast_one", "architect_proposal_fast_two"}
_SMOKE_ENVIRONMENT_NAMES = {
    "PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "AGENT_WORK_UV",
}
_MAX_ROLLOUT_FILES = 32
_MAX_ROLLOUT_BYTES = 256 * 1024
_MAX_STDOUT_BYTES = 256 * 1024
_MAX_STDOUT_LINES = 1024
_MAX_ROLLOUT_SCAN_SECONDS = 2.0
_GIT_INIT_TIMEOUT_SECONDS = 10


def _initialize_private_git_workspace(
    workspace: Path, dependencies: SystemdSmokeDependencies
) -> None:
    """Create a local Git repository without inherited templates or configuration."""
    git_value = dependencies.environment.get("AGENT_WORK_GIT", "")
    git_path = Path(git_value)
    if (
        not git_path.is_absolute()
        or not git_path.is_file()
        or not os.access(git_path, os.X_OK)
    ):
        raise SystemdSmokeError("AGENT_WORK_GIT must be an absolute executable path")
    try:
        result = dependencies.run_git_command(
            (
                str(git_path),
                "-c",
                "init.templateDir=/dev/null",
                "-C",
                str(workspace),
                "init",
                "--initial-branch=main",
                "--template=",
            ),
            timeout_seconds=_GIT_INIT_TIMEOUT_SECONDS,
            environment={
                "HOME": str(workspace),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
            },
        )
    except RepositoryError as error:
        raise SystemdSmokeError("private smoke Git initialization failed") from error
    if result.returncode != 0:
        raise SystemdSmokeError("private smoke Git initialization failed")


def _redact(value: str, environment: Mapping[str, str]) -> str:
    sensitive_values: list[str] = [
        secret
        for name, secret in environment.items()
        if secret
        and any(
            word in name.lower()
            for word in ("token", "secret", "key", "password", "credential")
        )
    ]
    sensitive_values.sort(key=len, reverse=True)
    redacted: str = value
    for secret in sensitive_values:
        redacted = redacted.replace(secret, "[REDACTED]")
    return sanitize_diagnostic(redacted)


def _owner_file(path: Path) -> None:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_uid != os.geteuid()
    ):
        raise SystemdSmokeError("owner Codex credential file must be mode 0600")


def _trusted_broker_client(checkout: Path) -> Path:
    """Require the checkout-owned broker client used from private scratch."""
    client = checkout / "bin" / "agent-work-from-checkout"
    if not client.is_file() or client.is_symlink() or not os.access(client, os.X_OK):
        raise SystemdSmokeError(
            "checkout-owned agent-work broker client is not executable"
        )
    return client.resolve(strict=True)


def _read_rollouts(home: Path) -> Iterable[object]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return ()
    records: list[object] = []
    remaining = _MAX_ROLLOUT_BYTES
    started = monotonic()
    for files_seen, path in enumerate(sessions.rglob("*.jsonl"), start=1):
        if (
            files_seen > _MAX_ROLLOUT_FILES
            or monotonic() - started > _MAX_ROLLOUT_SCAN_SECONDS
        ):
            raise SystemdSmokeError("rollout scan is incomplete")
        try:
            evidence = path.lstat()
            if not stat.S_ISREG(evidence.st_mode):
                raise SystemdSmokeError("rollout scan is incomplete")
            with path.open("rb") as source:
                first_line = source.readline(remaining + 1)
            if not first_line or len(first_line) > remaining:
                raise SystemdSmokeError("rollout scan is incomplete")
            remaining -= len(first_line)
            record = json.loads(first_line.decode("utf-8"))
            if (
                not isinstance(record, dict)
                or record.get("type") != "session_meta"
                or not isinstance(record.get("payload"), dict)
                or not isinstance(record["payload"].get("id"), str)
                or not record["payload"]["id"]
            ):
                raise SystemdSmokeError("rollout scan is incomplete")
            records.append(record)
        except (UnicodeError, ValueError) as error:
            raise SystemdSmokeError("rollout scan is incomplete") from error
        except OSError as error:
            raise SystemdSmokeError("rollout scan is incomplete") from error
    return records


def _root_thread_id(result: CommandResult) -> str | None:
    if len(result.stdout.encode("utf-8", errors="replace")) > _MAX_STDOUT_BYTES:
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        payload = []
        for line in result.stdout.splitlines()[:_MAX_STDOUT_LINES]:
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                payload.append(item)
    if isinstance(payload, list):
        for event in payload:
            if isinstance(event, dict) and event.get("type") == "thread.started":
                thread_id = event.get("thread_id")
                if isinstance(thread_id, str) and thread_id:
                    return thread_id
        return None
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def _metadata_root(records: Iterable[object]) -> str | None:
    roots: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        item = cast(dict[str, object], record)
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            continue
        metadata = cast(dict[str, object], payload)
        root_id = metadata.get("id")
        if isinstance(root_id, str) and not isinstance(metadata.get("source"), dict):
            roots.append(root_id)
    return roots[0] if len(roots) == 1 else None


def _prove_children(records: Iterable[object], root_thread_id: str) -> tuple[str, ...]:
    found: dict[str, str] = {}
    seen_sessions: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        item = cast(dict[str, object], record)
        payload = item.get("payload")
        if item.get("type") != "session_meta" or not isinstance(payload, dict):
            continue
        metadata = cast(dict[str, object], payload)
        session_id = metadata.get("id")
        source = metadata.get("source")
        if not isinstance(session_id, str) or not isinstance(source, dict):
            continue
        source_metadata = cast(dict[str, object], source)
        subagent = source_metadata.get("subagent")
        spawn = (
            cast(dict[str, object], subagent).get("thread_spawn")
            if isinstance(subagent, dict)
            else None
        )
        if not isinstance(spawn, dict):
            continue
        spawn_metadata = cast(dict[str, object], spawn)
        if spawn_metadata.get("parent_thread_id") != root_thread_id:
            continue
        if session_id in seen_sessions:
            raise SystemdSmokeError("duplicate direct child session evidence")
        seen_sessions.add(session_id)
        agent_path = spawn_metadata.get("agent_path")
        if not isinstance(agent_path, str):
            raise SystemdSmokeError("invalid direct child evidence")
        task = agent_path.rsplit("/", 1)[-1]
        if task not in {_BALANCED_TASK, *_FAST_TASKS}:
            raise SystemdSmokeError("unexpected direct child evidence")
        if task in found:
            raise SystemdSmokeError("duplicate required child task evidence")
        found[task] = session_id
    expected_tasks = (
        _BALANCED_TASK,
        "architect_proposal_fast_one",
        "architect_proposal_fast_two",
    )
    if set(found) != set(expected_tasks):
        raise SystemdSmokeError(
            "missing parent-bound child evidence for balanced and fast proposals"
        )
    return tuple(found[task] for task in expected_tasks)


def _run_systemd_smoke_in_root(
    *, dependencies: SystemdSmokeDependencies, root: Path
) -> SystemdSmokeReport:
    """Run one smoke activation after the caller creates its private root."""
    owner_auth = dependencies.codex_home / "auth.json"
    owner_config = dependencies.codex_home / "config.toml"
    private_home = root / "codex"
    private_home.mkdir(mode=0o700)
    os.symlink(owner_auth, private_home / "auth.json")
    os.symlink(owner_config, private_home / "config.toml")
    nonce = secrets.token_hex(16)
    root_thread: str | None = None
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    uv_cache = workspace / "uv-cache"
    uv_cache.mkdir(mode=0o700)
    xdg_cache = workspace / "xdg-cache"
    xdg_cache.mkdir(mode=0o700)
    broker_client = _trusted_broker_client(dependencies.checkout)
    for relative in (Path(".agents"), Path("template") / ".agents"):
        source = dependencies.checkout / relative
        destination = workspace / relative
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False)
        else:
            destination.mkdir(parents=True, mode=0o700)
    _initialize_private_git_workspace(workspace, dependencies)

    def build_artifact(value: bytes) -> bytes:
        reader = dependencies.read_rollout_metadata or _read_rollouts
        try:
            records = tuple(reader(private_home))
        except SystemdSmokeError as error:
            raise WorkerProtocolError("systemd_smoke", str(error)) from error
        parent = root_thread or _metadata_root(records)
        if parent is None:
            raise WorkerProtocolError("systemd_smoke", "missing machine child evidence")
        try:
            session_ids = _prove_children(records, parent)
        except (OSError, SystemdSmokeError, ValueError) as error:
            raise WorkerProtocolError("systemd_smoke", str(error)) from error
        try:
            intent = json.loads(value)
        except ValueError as error:
            raise WorkerProtocolError(
                "systemd_smoke", "smoke intent is invalid"
            ) from error
        if (
            not isinstance(intent, dict)
            or set(intent) != {"kind", "session_ids"}
            or intent.get("kind") != "systemd-smoke"
            or intent.get("session_ids") != list(session_ids)
        ):
            raise WorkerProtocolError(
                "systemd_smoke", "smoke intent does not bind child evidence"
            )
        return json.dumps(intent, separators=(",", ":")).encode()

    request = WorkerResultBrokerRequest(
        nonce=nonce,
        project_id=1,
        issue_iid=251,
        ledger_revision=0,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.CODING_GREEN,
        source_branch="systemd-smoke",
        source_sha="0" * 40,
    )
    session = create_worker_result_broker(
        request,
        build_artifact=build_artifact,
        timeout_seconds=dependencies.timeout_seconds,
    )
    session_closed = False
    primary_error: Exception | None = None
    try:
        prompt = (
            "Create exactly these parent-bound architecture children: "
            "architect_proposal_balanced with skill "
            ".agents/skills/architect-proposal-balanced/SKILL.md; "
            "architect_proposal_fast_one and architect_proposal_fast_two with skill "
            ".agents/skills/architect-proposal-fast/SKILL.md. Wait for all three. "
            "The balanced child must use the balanced skill tier; each fast child must "
            "use the fast skill tier. Each child must read its named exact skill before "
            "proposing, stay read-only, and neither delegate, mutate, nor build or submit "
            "a broker result. Wait for all three. "
            "Do not access GitLab or mutate repository state. Then submit one JSON object "
            "to an input file in the disposable workspace. The object must have exactly "
            "kind and session_ids keys, kind must be systemd-smoke, and session_ids must "
            "list the three observed child session IDs in balanced, fast-one, fast-two order. "
            f"Invoke only `{broker_client} worker-result build --issue-iid 251 "
            f"--input-file {workspace / 'smoke-result.json'} --broker-socket "
            f"{session.socket_path} --broker-nonce {nonce} --apply --json`."
        )
        command, stdin = _agent_command(
            "codex",
            dependencies.environment.get("AGENT_WORK_AGENT_PATH", "codex"),
            workspace,
            prompt,
            broker_directory=session.socket_path.parent,
        )
        command = (*command[:-1], "--json", command[-1])
        environment = {
            name: dependencies.environment[name]
            for name in _SMOKE_ENVIRONMENT_NAMES & dependencies.environment.keys()
        }
        environment.update(
            {
                "CODEX_HOME": str(private_home),
                "HOME": str(root),
                "UV_CACHE_DIR": str(uv_cache),
                "XDG_CACHE_HOME": str(xdg_cache),
                "AGENT_WORK_SMOKE_BROKER_SOCKET": str(session.socket_path),
                "AGENT_WORK_SMOKE_BROKER_NONCE": nonce,
            }
        )
        result = dependencies.run_command(
            command,
            stdin=stdin,
            timeout_seconds=dependencies.timeout_seconds,
            environment=environment,
        )
        root_thread = _root_thread_id(result)
        try:
            session_closed = True
            artifact = session.close_and_capture().decode("utf-8")
        except WorkerProtocolError as error:
            reader = dependencies.read_rollout_metadata or _read_rollouts
            try:
                records = tuple(reader(private_home))
            except SystemdSmokeError as scan_error:
                records = ()
                reason = str(scan_error)
            else:
                reason = ""
            parent = root_thread or _metadata_root(records)
            if reason:
                pass
            elif parent is None:
                reason = "missing machine child evidence"
            else:
                try:
                    _prove_children(records, parent)
                except SystemdSmokeError as proof_error:
                    reason = str(proof_error)
                else:
                    reason = error.reason
            detail = _redact(result.stderr, dependencies.environment)
            raise SystemdSmokeError(f"{reason}: {detail}") from error
        if result.returncode != 0:
            raise SystemdSmokeError(_redact(result.stderr, dependencies.environment))
        return SystemdSmokeReport(broker_accepted=True, broker_artifact=artifact)
    except Exception as error:
        primary_error = error
        raise
    finally:
        if not session_closed:
            try:
                session.close_and_capture()
            except WorkerProtocolError as cleanup_error:
                if primary_error is not None:
                    raise SystemdSmokeError(
                        f"{primary_error}; smoke broker cleanup failed"
                    ) from primary_error
                raise SystemdSmokeError(
                    "smoke broker cleanup failed"
                ) from cleanup_error


def _cleanup_private_root(root: Path, evidence: os.stat_result) -> None:
    """Remove only the owned inode created for this smoke activation."""
    current = root.lstat()
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) != 0o700
        or (current.st_dev, current.st_ino) != (evidence.st_dev, evidence.st_ino)
    ):
        raise OSError("private Codex root ownership changed")
    shutil.rmtree(root)


def run_systemd_smoke(*, dependencies: SystemdSmokeDependencies) -> SystemdSmokeReport:
    """Run Codex in a private home and accept its broker result only after proof."""
    if dependencies.environment.get("AGENT_WORK_AGENT") != "codex":
        raise SystemdSmokeError("systemd smoke requires AGENT_WORK_AGENT=codex")
    owner_auth = dependencies.codex_home / "auth.json"
    owner_config = dependencies.codex_home / "config.toml"
    _owner_file(owner_auth)
    _owner_file(owner_config)
    root = Path(
        tempfile.mkdtemp(
            prefix="agent-work-smoke-", dir=dependencies.temporary_directory
        )
    )
    root_evidence: os.stat_result | None = None
    primary_error: Exception | None = None
    try:
        os.chmod(root, 0o700)
        root_evidence = root.lstat()
        return _run_systemd_smoke_in_root(dependencies=dependencies, root=root)
    except Exception as error:
        primary_error = error
        raise
    finally:
        try:
            if root_evidence is not None:
                _cleanup_private_root(root, root_evidence)
            else:
                _cleanup_private_root(root, root.lstat())
        except OSError as cleanup_error:
            if primary_error is not None:
                raise SystemdSmokeError(
                    f"{primary_error}; private Codex home cleanup failed"
                ) from primary_error
            raise SystemdSmokeError(
                "private Codex home cleanup failed"
            ) from cleanup_error
