# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Project-wide local admission for trustworthy timing observations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import selectors
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Literal, Protocol, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .ledger import LockBusy, XdgProjectLock, project_state_path

_AUTH_SOCKET = "AGENT_WORK_TIMING_SOCKET"
_AUTH_NONCE = "AGENT_WORK_TIMING_NONCE"
_LEASE_FD = "AGENT_WORK_TIMING_LEASE_FD"
_PROJECT_LOCK_FD = "AGENT_WORK_PROJECT_LOCK_FD"


class TimingAdmissionBusy(RuntimeError):
    """Another local timing observation owns this project."""


class ValidationAdmissionStateError(RuntimeError):
    """Retained validation admission state is malformed or unreadable."""


class _ValidationRequestState(BaseModel):
    """Validated on-disk state of one interactive validation request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    host: str = Field(min_length=1)
    project_id: int = Field(gt=0)
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    request_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    status: Literal["waiting", "running", "complete", "unavailable"]
    wait_elapsed_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    issue_iid: int | None = Field(default=None, gt=0)
    yield_at: AwareDatetime | None = None
    validation_started_at: AwareDatetime | None = None
    validation_finished_at: AwareDatetime | None = None
    resume_at: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None
    resolution: Literal["closed_without_resume", "ineligible_without_resume"] | None = (
        None
    )

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> _ValidationRequestState:
        if self.status in {"waiting", "running"} and self.expires_at is None:
            raise ValueError("active admission requires expires_at")
        if self.status == "complete" and (
            self.validation_started_at is None or self.validation_finished_at is None
        ):
            raise ValueError("completed admission requires validation timestamps")
        if self.status == "unavailable" and self.validation_finished_at is None:
            raise ValueError("unavailable admission requires validation_finished_at")
        if self.yield_at is not None and self.issue_iid is None:
            raise ValueError("yielded admission requires issue_iid")
        if (self.resolution is None) != (self.resolved_at is None):
            raise ValueError("resolution and resolved_at must appear together")
        if self.resolution is not None and (
            self.status != "complete"
            or self.issue_iid is None
            or self.yield_at is None
            or self.resume_at is not None
        ):
            raise ValueError(
                "closed admission requires exact yielded issue and resolution time"
            )
        return self


@dataclass(frozen=True)
class TimingAdmissionLease:
    """Live descriptor for the project flock's open file description."""

    fd: int


@dataclass(frozen=True)
class InteractiveValidationRequest:
    """One live, project-bound validation request."""

    host: str
    project_id: int
    request_id: str
    requested_at: datetime
    expires_at: datetime
    state_path: Path
    status: str
    yielded_issue_iid: int | None


def _request_paths(root: Path) -> tuple[Path, Path]:
    return (
        root / "interactive-validation-request.json",
        root / "interactive-validation-request.lock",
    )


def _write_request(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}-{secrets.token_hex(8)}")
    try:
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _update_request(
    path: Path,
    request_id: object,
    updates: dict[str, object],
    *,
    required_status: str | None = None,
    valid_at: datetime | None = None,
    required_issue_iid: int | None = None,
    first_yield_issue_iid: int | None = None,
    forbid_existing_disposition: bool = False,
) -> bool:
    descriptor = os.open(
        path.with_suffix(".update.lock"), os.O_RDWR | os.O_CREAT, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        state = _read_request(path)
        if state is None or state.get("request_id") != request_id:
            return False
        if required_status is not None and state.get("status") != required_status:
            return False
        if (
            required_issue_iid is not None
            and state.get("issue_iid") != required_issue_iid
        ):
            return False
        if forbid_existing_disposition and (
            "resume_at" in state or "resolution" in state
        ):
            return False
        if valid_at is not None:
            try:
                requested_at = datetime.fromisoformat(str(state["request_at"]))
                expires_at = datetime.fromisoformat(str(state["expires_at"]))
            except (KeyError, TypeError, ValueError):
                return False
            if not requested_at <= valid_at < expires_at:
                return False
        if first_yield_issue_iid is not None and "yield_at" in state:
            return state.get("issue_iid") == first_yield_issue_iid
        state.update(updates)
        _write_request(path, state)
        return True
    finally:
        os.close(descriptor)


def _read_request(path: Path) -> dict[str, object] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise ValidationAdmissionStateError(
            f"invalid validation admission at {path}"
        ) from error
    try:
        return _ValidationRequestState.model_validate_json(raw).model_dump(
            mode="json", exclude_none=True
        )
    except ValidationError as error:
        raise ValidationAdmissionStateError(
            f"invalid validation admission at {path}"
        ) from error


def observe_waiting_interactive_validation(
    *,
    host: str,
    project_id: int,
    environment: Mapping[str, str],
    utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> InteractiveValidationRequest | None:
    """Return only a live request for the exact project."""
    if _AUTH_NONCE in environment or _AUTH_SOCKET in environment:
        return None
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    state_path, lock_path = _request_paths(root)
    try:
        descriptor = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return None
    finally:
        os.close(descriptor)
    state = _read_request(state_path)
    if (
        state is None
        or state.get("host") != host
        or state.get("project_id") != project_id
    ):
        return None
    try:
        requested_at = datetime.fromisoformat(cast(str, state["request_at"]))
        expires_at = datetime.fromisoformat(cast(str, state["expires_at"]))
        request_id = cast(str, state["request_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if state.get("status") not in {"waiting", "running"} or not request_id:
        return None
    if not requested_at <= utc_clock() < expires_at:
        return None
    yielded_issue = state.get("issue_iid")
    return InteractiveValidationRequest(
        host,
        project_id,
        request_id,
        requested_at,
        expires_at,
        state_path,
        cast(str, state["status"]),
        (
            yielded_issue
            if isinstance(yielded_issue, int)
            and yielded_issue > 0
            and "yield_at" in state
            else None
        ),
    )


def mark_interactive_validation_yield(
    request: InteractiveValidationRequest, *, at: datetime, issue_iid: int
) -> bool:
    """Record the controller's safe yield for the same live request."""
    if request.status != "waiting" or issue_iid <= 0:
        return False
    lock_path = request.state_path.with_name("interactive-validation-request.lock")
    try:
        descriptor = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _update_request(
                request.state_path,
                request.request_id,
                {"yield_at": at.isoformat(), "issue_iid": issue_iid},
                required_status="waiting",
                valid_at=at,
                first_yield_issue_iid=issue_iid,
            )
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def read_validation_admission(
    *, host: str, project_id: int, environment: Mapping[str, str]
) -> dict[str, object] | None:
    """Read bounded completed admission timing for a resumed activation."""
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    state_path, _ = _request_paths(root)
    state = _read_request(state_path)
    if (
        state is None
        or state.get("host") != host
        or state.get("project_id") != project_id
    ):
        return None
    if state.get("status") not in {"complete", "unavailable"} or (
        "resume_at" in state or "resolution" in state
    ):
        return None
    if state.get("status") == "complete" and "yield_at" not in state:
        return None
    issue_iid = state.get("issue_iid")
    if state.get("status") == "complete" and "yield_at" in state and issue_iid is None:
        return None
    state["_state_path"] = str(state_path)
    return state


def mark_validation_admission_resumed(
    state: dict[str, object], *, at: datetime, issue_iid: int | None = None
) -> bool:
    """Consume one completed admission after its first resumed report."""
    state_path = state.get("_state_path")
    if not isinstance(state_path, str):
        return False
    return _update_request(
        Path(state_path),
        state.get("request_id"),
        {"resume_at": at.isoformat()},
        required_issue_iid=issue_iid,
        required_status=cast(str, state["status"]),
        forbid_existing_disposition=True,
    )


def mark_validation_admission_resolved(
    state: dict[str, object],
    *,
    at: datetime,
    issue_iid: int,
    resolution: Literal["closed_without_resume", "ineligible_without_resume"],
) -> bool:
    """Consume a completed request when its exact yielded issue cannot resume."""
    state_path = state.get("_state_path")
    if not isinstance(state_path, str) or state.get("yield_at") is None:
        return False
    return _update_request(
        Path(state_path),
        state.get("request_id"),
        {"resolution": resolution, "resolved_at": at.isoformat()},
        required_issue_iid=issue_iid,
        required_status="complete",
        forbid_existing_disposition=True,
    )


def _record_contention(
    root: Path, *, host: str, project_id: int, wait_seconds: float, event_id: str
) -> None:
    """Atomically retain the first failed project lock attempt."""
    observation = root / "timing-contention.json"
    temporary = root / f".timing-contention-{secrets.token_hex(8)}"
    try:
        temporary.write_text(
            json.dumps(
                {
                    "event": "contention",
                    "host": host,
                    "project_id": project_id,
                    "wait_seconds": wait_seconds,
                    "event_id": event_id,
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(observation)
    finally:
        temporary.unlink(missing_ok=True)


def _authorized(environment: Mapping[str, str], project_root: str) -> bool:
    address = environment.get(_AUTH_SOCKET)
    nonce = environment.get(_AUTH_NONCE)
    prefix = f"@agent-work-{hashlib.sha256(project_root.encode()).hexdigest()[:24]}-"
    if not address or not address.startswith(prefix) or not nonce:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect("\0" + address[1:])
            client.sendall(nonce.encode("ascii"))
            return client.recv(2) == b"OK"
    except (OSError, UnicodeError):
        return False


@contextmanager
def project_timing_admission(
    *,
    host: str,
    project_id: int,
    environment: Mapping[str, str],
    wait_seconds: float,
    monotonic_clock: Callable[[], float],
    sleeper: Callable[[float], None],
    new_event_id: object = None,
) -> Iterator[TimingAdmissionLease | None]:
    """Acquire a bounded project-local flock, or use a live window grant."""
    if not host or project_id <= 0 or wait_seconds < 0:
        raise ValueError("valid project identity and wait are required")
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    if _authorized(environment, str(root)):
        yield
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / "timing-admission.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    handle: IO[bytes] = os.fdopen(descriptor, "r+b")
    acquired = False
    contended = False
    started = monotonic_clock()
    try:
        while not acquired:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError as error:
                if not contended:
                    event_id = (
                        cast(Callable[[], str], new_event_id)()
                        if callable(new_event_id)
                        else secrets.token_hex(8)
                    )
                    if (
                        not isinstance(event_id, str)
                        or not event_id.isascii()
                        or not 1 <= len(event_id) <= 64
                    ):
                        raise ValueError("timing contention event ID is invalid")
                    _record_contention(
                        root,
                        host=host,
                        project_id=project_id,
                        wait_seconds=wait_seconds,
                        event_id=event_id,
                    )
                    contended = True
                if monotonic_clock() - started >= wait_seconds:
                    raise TimingAdmissionBusy(
                        "project timing admission busy"
                    ) from error
                sleeper(
                    min(0.05, max(0.0, wait_seconds - (monotonic_clock() - started)))
                )
        yield TimingAdmissionLease(handle.fileno())
    finally:
        handle.close()


def _serve_grants(server: socket.socket, nonce: str, stop: threading.Event) -> None:
    server.settimeout(0.005)
    while not stop.is_set():
        try:
            connection, _ = server.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        with connection:
            connection.settimeout(1.0)
            try:
                presented = connection.recv(128).decode("ascii")
                connection.sendall(
                    b"OK" if secrets.compare_digest(presented, nonce) else b"NO"
                )
            except (OSError, UnicodeError):
                continue


@contextmanager
def _project_worker_admission(
    root: Path,
    *,
    deadline: float,
    allow_immediate: bool,
    monotonic_clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> Iterator[int]:
    """Hold the controller's project lock before starting validation."""
    attempted = False
    while True:
        if attempted and monotonic_clock() >= deadline:
            raise TimingAdmissionBusy("project worker admission busy")
        attempted = True
        lock = XdgProjectLock(root / "project.lock")
        try:
            lock.__enter__()
        except LockBusy as error:
            remaining = deadline - monotonic_clock()
            if remaining <= 0:
                raise TimingAdmissionBusy("project worker admission busy") from error
            sleeper(min(0.05, remaining))
        else:
            break
    try:
        if not allow_immediate and monotonic_clock() >= deadline:
            raise TimingAdmissionBusy("project worker admission busy")
        yield lock.fd
    finally:
        lock.__exit__(None, None, None)


def run_project_timing_window(
    *,
    command: tuple[str, ...],
    run_child: Callable[[tuple[str, ...], Mapping[str, str]], int],
    host: str,
    project_id: int,
    environment: Mapping[str, str],
    wait_seconds: float,
    monotonic_clock: Callable[[], float],
    sleeper: Callable[[float], None],
    utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Hold one admission while a child and its nested validations run."""
    if _authorized(
        environment,
        str(
            project_state_path(
                host=host, project_id=project_id, environment=environment
            )
        ),
    ):
        raise RuntimeError("timing window requires a direct project lease")
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path, lock_path = _request_paths(root)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise TimingAdmissionBusy(
            "interactive validation request already active"
        ) from error
    requested_at = utc_clock()
    request: dict[str, object] = {
        "host": host,
        "project_id": project_id,
        "request_id": secrets.token_hex(16),
        "request_at": requested_at.isoformat(),
        "expires_at": (
            requested_at + timedelta(seconds=max(wait_seconds, 30.0))
        ).isoformat(),
        "status": "waiting",
        "wait_elapsed_seconds": 0.0,
    }
    try:
        if (
            read_validation_admission(
                host=host, project_id=project_id, environment=environment
            )
            is not None
        ):
            raise TimingAdmissionBusy(
                "previous interactive validation admission awaits controller report"
            )
        _write_request(state_path, request)
        return _run_requested_timing_window(
            command=command,
            run_child=run_child,
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=wait_seconds,
            monotonic_clock=monotonic_clock,
            sleeper=sleeper,
            utc_clock=utc_clock,
            request=request,
            state_path=state_path,
        )
    finally:
        os.close(descriptor)


def _run_requested_timing_window(
    *,
    command: tuple[str, ...],
    run_child: Callable[[tuple[str, ...], Mapping[str, str]], int],
    host: str,
    project_id: int,
    environment: Mapping[str, str],
    wait_seconds: float,
    monotonic_clock: Callable[[], float],
    sleeper: Callable[[float], None],
    utc_clock: Callable[[], datetime],
    request: dict[str, object],
    state_path: Path,
) -> int:
    wait_started = monotonic_clock()
    deadline = wait_started + wait_seconds
    try:
        with (
            _project_worker_admission(
                project_state_path(
                    host=host, project_id=project_id, environment=environment
                ),
                deadline=deadline,
                allow_immediate=wait_seconds == 0,
                monotonic_clock=monotonic_clock,
                sleeper=sleeper,
            ) as project_lock_fd,
            project_timing_admission(
                host=host,
                project_id=project_id,
                environment=environment,
                wait_seconds=max(0.0, deadline - monotonic_clock()),
                monotonic_clock=monotonic_clock,
                sleeper=sleeper,
            ) as lease,
        ):
            if lease is None:
                raise RuntimeError("timing window requires a direct project lease")
            if wait_seconds > 0 and monotonic_clock() >= deadline:
                raise TimingAdmissionBusy("project timing admission busy")
            request["status"] = "running"
            _update_request(
                state_path,
                request["request_id"],
                {
                    "status": "running",
                    "validation_started_at": utc_clock().isoformat(),
                    "wait_elapsed_seconds": max(0.0, monotonic_clock() - wait_started),
                },
            )
            root = project_state_path(
                host=host, project_id=project_id, environment=environment
            )
            address = (
                f"@agent-work-{hashlib.sha256(str(root).encode()).hexdigest()[:24]}-"
                f"{secrets.token_hex(6)}"
            )
            nonce = secrets.token_hex(24)
            stop = threading.Event()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind("\0" + address[1:])
                server.listen()
                thread = threading.Thread(
                    target=_serve_grants, args=(server, nonce, stop), daemon=True
                )
                thread.start()
                try:
                    return run_child(
                        command,
                        {
                            **environment,
                            _AUTH_SOCKET: address,
                            _AUTH_NONCE: nonce,
                            _LEASE_FD: str(lease.fd),
                            _PROJECT_LOCK_FD: str(project_lock_fd),
                        },
                    )
                finally:
                    stop.set()
                    server.close()
                    thread.join()
    except TimingAdmissionBusy:
        request["status"] = "unavailable"
        _update_request(
            state_path,
            request["request_id"],
            {
                "status": "unavailable",
                "validation_finished_at": utc_clock().isoformat(),
                "wait_elapsed_seconds": max(0.0, monotonic_clock() - wait_started),
            },
        )
        raise
    finally:
        if request["status"] == "running":
            _update_request(
                state_path,
                request["request_id"],
                {
                    "status": "complete",
                    "validation_finished_at": utc_clock().isoformat(),
                },
            )


@contextmanager
def _supervised_process(
    command: tuple[str, ...],
    *,
    lease_fd: int,
    project_lock_fd: int | None = None,
    environment: Mapping[str, str],
    spawn_supervisor: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    **options: object,
) -> Iterator[tuple[subprocess.Popen[str], Callable[[], None]]]:
    """Share supervisor startup and owner-pipe lifetime across both callers."""
    parent_read, parent_write = os.pipe()
    inherited_fds = (
        (parent_read, lease_fd)
        if project_lock_fd is None
        else (parent_read, lease_fd, project_lock_fd)
    )
    try:
        supervisor = spawn_supervisor(
            (
                sys.executable,
                "-m",
                "agent_work.timing_admission",
                "--supervise",
                str(parent_read),
                str(lease_fd),
                "--",
                *command,
            ),
            env=dict(environment),
            text=True,
            start_new_session=True,
            pass_fds=inherited_fds,
            **options,
        )
    except OSError:
        os.close(parent_write)
        raise
    finally:
        os.close(parent_read)

    owner_open = True

    def close_owner() -> None:
        nonlocal owner_open
        if owner_open:
            os.close(parent_write)
            owner_open = False

    try:
        yield supervisor, close_owner
    finally:
        close_owner()
        supervisor.wait()


def run_subprocess_child(
    command: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    json_output: bool = False,
) -> int:
    """Run the child under a lease-holding owner-death supervisor."""
    lease_fd_text = environment.get(_LEASE_FD, "")
    if not lease_fd_text.isdigit():
        raise RuntimeError("timing window child lease is unavailable")
    project_lock_fd_text = environment.get(_PROJECT_LOCK_FD, "")
    if not project_lock_fd_text.isdigit():
        raise RuntimeError("timing window project lock is unavailable")
    with _supervised_process(
        command,
        lease_fd=int(lease_fd_text),
        project_lock_fd=int(project_lock_fd_text),
        environment=environment,
        stdout=sys.stderr if json_output else None,
    ) as (supervisor, _close_owner):
        return supervisor.wait()


def run_supervised_command(
    command: tuple[str, ...],
    *,
    lease: TimingAdmissionLease,
    environment: Mapping[str, str],
    current_directory: Path | None = None,
    timeout_seconds: float | None = None,
    capture_output: bool = True,
    spawn_supervisor: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> subprocess.CompletedProcess[str]:
    """Keep the project lease alive until a hook session and descendants stop."""
    with _supervised_process(
        command,
        lease_fd=lease.fd,
        environment=environment,
        spawn_supervisor=spawn_supervisor,
        cwd=current_directory,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
    ) as (supervisor, close_owner):
        try:
            stdout, stderr = supervisor.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            close_owner()
            supervisor.communicate()
            raise
    return subprocess.CompletedProcess(
        command, supervisor.returncode, stdout or "", stderr or ""
    )


class _ChildProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self) -> int: ...


def _session_members(
    session_id: int, *, proc_root: Path = Path("/proc")
) -> tuple[int, ...]:
    """Find live Linux processes in the supervised shell's session."""
    members: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rpartition(") ")[2].split()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if len(fields) >= 4 and fields[0] != "Z" and fields[3] == str(session_id):
            members.append(int(entry.name))
    return tuple(members)


def _signal_session(
    session_id: int,
    signum: signal.Signals,
    *,
    members: Callable[[int], tuple[int, ...]],
    send_signal: Callable[[int, int], None],
) -> None:
    for pid in members(session_id):
        try:
            send_signal(pid, signum)
        except ProcessLookupError:
            continue


def _stop_child_session(
    child: _ChildProcess,
    *,
    members: Callable[[int], tuple[int, ...]] = _session_members,
    send_signal: Callable[[int, int], None] = os.kill,
    monotonic_clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Stop all process groups in the shell session before dropping its lease."""
    _signal_session(child.pid, signal.SIGTERM, members=members, send_signal=send_signal)
    deadline = monotonic_clock() + 30.0
    while members(child.pid) and monotonic_clock() < deadline:
        sleeper(0.01)
    _signal_session(child.pid, signal.SIGKILL, members=members, send_signal=send_signal)
    while members(child.pid):
        sleeper(0.01)
    child.wait()


def _supervise_child(
    child: _ChildProcess,
    *,
    parent_closed: Callable[[], bool],
    stop_child: Callable[[_ChildProcess], None] = _stop_child_session,
) -> int:
    """Release the lease only after normal exit or owner-death cleanup."""
    try:
        while True:
            result = child.poll()
            if result is not None:
                return result
            if parent_closed():
                return 1
    finally:
        stop_child(child)


def _supervisor_main(arguments: list[str]) -> int:
    """Hold the inherited lease until child exit or parent-death cleanup."""
    if len(arguments) < 5 or arguments[0] != "--supervise" or arguments[3] != "--":
        raise ValueError("timing supervisor arguments are invalid")
    parent_fd = int(arguments[1])
    lease_fd = int(arguments[2])
    command = tuple(arguments[4:])
    child_environment = dict(os.environ)
    child_environment.pop(_PROJECT_LOCK_FD, None)
    child = subprocess.Popen(
        command,
        env=child_environment,
        start_new_session=True,
        pass_fds=(lease_fd,),
    )
    try:
        with selectors.DefaultSelector() as watcher:
            watcher.register(parent_fd, selectors.EVENT_READ)
            return _supervise_child(
                child,
                parent_closed=lambda: (
                    bool(watcher.select(timeout=0.05)) and os.read(parent_fd, 1) == b""
                ),
            )
    finally:
        os.close(parent_fd)


if __name__ == "__main__":
    raise SystemExit(_supervisor_main(sys.argv[1:]))
