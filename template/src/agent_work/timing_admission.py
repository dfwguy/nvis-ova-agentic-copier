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
from pathlib import Path
from typing import IO, Protocol, cast

from .ledger import project_state_path

_AUTH_SOCKET = "AGENT_WORK_TIMING_SOCKET"
_AUTH_NONCE = "AGENT_WORK_TIMING_NONCE"
_LEASE_FD = "AGENT_WORK_TIMING_LEASE_FD"


class TimingAdmissionBusy(RuntimeError):
    """Another local timing observation owns this project."""


@dataclass(frozen=True)
class TimingAdmissionLease:
    """Live descriptor for the project flock's open file description."""

    fd: int


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
) -> int:
    """Hold one admission while a child and its nested validations run."""
    with project_timing_admission(
        host=host,
        project_id=project_id,
        environment=environment,
        wait_seconds=wait_seconds,
        monotonic_clock=monotonic_clock,
        sleeper=sleeper,
    ) as lease:
        if lease is None:
            raise RuntimeError("timing window requires a direct project lease")
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
                    },
                )
            finally:
                stop.set()
                server.close()
                thread.join()


@contextmanager
def _supervised_process(
    command: tuple[str, ...],
    *,
    lease_fd: int,
    environment: Mapping[str, str],
    spawn_supervisor: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    **options: object,
) -> Iterator[tuple[subprocess.Popen[str], Callable[[], None]]]:
    """Share supervisor startup and owner-pipe lifetime across both callers."""
    parent_read, parent_write = os.pipe()
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
            pass_fds=(parent_read, lease_fd),
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
    with _supervised_process(
        command,
        lease_fd=int(lease_fd_text),
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
    child = subprocess.Popen(
        command, env=os.environ, start_new_session=True, pass_fds=(lease_fd,)
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
