# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Private, opt-in worker reads from a live controller."""

from __future__ import annotations

import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .worker import _clear_private_directory

WorkerReadAction = Literal["context", "issue", "ledger"]
_MAX_FRAME_BYTES = 131_072
_MAX_SOCKET_PATH_BYTES = 100
_SOCKET_DIRECTORY_PREFIX = "awr-"
_SOCKET_BASENAME = "s"


def _socket_path_fits(parent: Path) -> bool:
    candidate = parent / f"{_SOCKET_DIRECTORY_PREFIX}xxxxxxxx" / _SOCKET_BASENAME
    return len(os.fsencode(candidate)) <= _MAX_SOCKET_PATH_BYTES


@dataclass(frozen=True)
class WorkerReadSessionIdentity:
    """Controller-owned identity fixed for one worker session."""

    nonce: str
    project_id: int
    issue_iid: int
    checkout: Path
    phase: str
    ledger_revision: int
    source_sha: str


class _Projection(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Context(_Projection):
    state: str = Field(max_length=256)
    phase: str = Field(max_length=256)
    workflow_lane: str = Field(max_length=256)


class _Issue(_Projection):
    title: str = Field(max_length=4096)
    description: str = Field(max_length=12_288)


class _Ledger(_Projection):
    revision: int = Field(ge=0)
    source_branch: str = Field(max_length=512)
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    state: str = Field(max_length=256)
    phase: str = Field(max_length=256)


_PROJECTIONS: dict[str, type[_Projection]] = {
    "context": _Context,
    "issue": _Issue,
    "ledger": _Ledger,
}


def _read_frame(connection: socket.socket) -> bytes:
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            part = connection.recv(size - len(result))
            if not part:
                raise ValueError("worker_read: truncated frame")
            result.extend(part)
        return bytes(result)

    size = int.from_bytes(exact(4), "big")
    if size > _MAX_FRAME_BYTES:
        raise ValueError("worker_read: oversized frame")
    return exact(size)


def _write_frame(connection: socket.socket, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload).encode()
    if len(encoded) > _MAX_FRAME_BYTES:
        raise ValueError("worker_read: oversized frame")
    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)


class WorkerReadBrokerSession:
    """Serve bounded reads while the controller's worker is live."""

    def __init__(
        self,
        identity: WorkerReadSessionIdentity,
        *,
        identity_check: Callable[[WorkerReadSessionIdentity], bool],
        read_action: Callable[[str], dict[str, object]],
        timeout_seconds: float,
        temporary_directory: Path | None,
        clock: Callable[[], float],
    ) -> None:
        self._identity = identity
        self._identity_check = identity_check
        self._read_action = read_action
        self._clock = clock
        self._deadline = clock() + timeout_seconds
        self._stop = threading.Event()
        socket_parent = temporary_directory or Path(tempfile.gettempdir())
        if not _socket_path_fits(socket_parent):
            socket_parent = Path(tempfile.gettempdir())
        if not _socket_path_fits(socket_parent):
            raise OSError("worker_read: temporary socket path too long")
        self._root = Path(
            tempfile.mkdtemp(prefix=_SOCKET_DIRECTORY_PREFIX, dir=socket_parent)
        )
        root_fd: int | None = None
        listener: socket.socket | None = None
        self._socket_identity: tuple[int, int] | None = None
        try:
            self._root.chmod(0o700)
            root_stat = os.stat(self._root, follow_symlinks=False)
            self._root_identity = (root_stat.st_dev, root_stat.st_ino)
            root_fd = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
            self._root_fd = root_fd
            self.socket_path = self._root / _SOCKET_BASENAME
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket = listener
            self._socket.bind(str(self.socket_path))
            self.socket_path.chmod(0o600)
            socket_stat = os.stat(self.socket_path, follow_symlinks=False)
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            self._socket.listen(2)
            self._socket.settimeout(0.05)
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()
        except BaseException:
            if listener is not None:
                listener.close()
            if root_fd is None:
                self._root.rmdir()
            else:
                self._cleanup_root()
            raise

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.5)
                try:
                    request = json.loads(_read_frame(connection))
                    if (
                        not isinstance(request, dict)
                        or set(request) != {"nonce", "action"}
                        or request["nonce"] != self._identity.nonce
                        or request["action"] not in _PROJECTIONS
                    ):
                        raise ValueError("worker_read: invalid request")
                    if self._clock() >= self._deadline:
                        raise ValueError("worker_read: expired")
                    if not self._identity_check(self._identity):
                        raise ValueError("worker_read: identity changed")
                    action = cast(str, request["action"])
                    projection = (
                        _PROJECTIONS[action]
                        .model_validate(self._read_action(action))
                        .model_dump()
                    )
                    if self._stop.is_set() or self._clock() >= self._deadline:
                        raise ValueError("worker_read: expired")
                    _write_frame(connection, {"accepted": True, "value": projection})
                except (
                    OSError,
                    ValueError,
                    TypeError,
                    ValidationError,
                    OverflowError,
                    RuntimeError,
                ):
                    try:
                        _write_frame(connection, {"accepted": False})
                    except (OSError, ValueError):
                        pass

    @property
    def nonce(self) -> str:
        """Return the private nonce for the controller's MCP launcher."""
        return self._identity.nonce

    def close(self) -> None:
        """Stop the socket and remove its private directory."""
        self._stop.set()
        self._socket.close()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            threading.Thread(target=self._finish_deferred_cleanup, daemon=False).start()
            raise RuntimeError("worker_read: cleanup failed")
        self._cleanup_root()

    def _cleanup_root(self) -> None:
        try:
            root_stat = os.stat(self._root, follow_symlinks=False)
            fd_stat = os.fstat(self._root_fd)
            if (
                not stat.S_ISDIR(root_stat.st_mode)
                or (root_stat.st_dev, root_stat.st_ino) != self._root_identity
                or (fd_stat.st_dev, fd_stat.st_ino) != self._root_identity
            ):
                raise OSError("private worker read directory changed")
            if self._socket_identity is not None:
                socket_stat = os.stat(self.socket_path, follow_symlinks=False)
                if (
                    not stat.S_ISSOCK(socket_stat.st_mode)
                    or (socket_stat.st_dev, socket_stat.st_ino) != self._socket_identity
                ):
                    raise OSError("private worker read socket changed")
            _clear_private_directory(self._root_fd)
            self._root.rmdir()
            if os.fstat(self._root_fd).st_nlink != 0:
                raise OSError("private worker read directory changed")
        except OSError:
            raise RuntimeError("worker_read: cleanup failed") from None
        finally:
            os.close(self._root_fd)

    def _finish_deferred_cleanup(self) -> None:
        self._thread.join()
        try:
            self._cleanup_root()
        except RuntimeError:
            pass


def create_worker_read_broker(
    identity: WorkerReadSessionIdentity,
    *,
    identity_check: Callable[[WorkerReadSessionIdentity], bool],
    read_action: Callable[[str], dict[str, object]],
    timeout_seconds: float,
    temporary_directory: Path | None = None,
    clock: Callable[[], float] = monotonic,
) -> WorkerReadBrokerSession:
    """Create a private session with an injected live identity check."""
    return WorkerReadBrokerSession(
        identity,
        identity_check=identity_check,
        read_action=read_action,
        timeout_seconds=timeout_seconds,
        temporary_directory=temporary_directory,
        clock=clock,
    )


def request_worker_read(
    socket_path: Path, nonce: str, action: str, *, timeout_seconds: float = 10
) -> dict[str, object]:
    """Request one fixed read from the live controller."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            _write_frame(connection, {"nonce": nonce, "action": action})
            response = json.loads(_read_frame(connection))
    except (OSError, ValueError, TimeoutError):
        raise ValueError("worker_read: submission failed") from None
    if (
        isinstance(response, dict)
        and response.get("accepted") is True
        and isinstance(response.get("value"), dict)
    ):
        return cast(dict[str, object], response["value"])
    raise ValueError("worker_read: read failed")
