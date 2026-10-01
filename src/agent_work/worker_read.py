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
import re
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

from .file_patch import DocumentationPatchRequest, DocumentationPatchResult
from .worker import _clear_private_directory

WorkerReadAction = Literal["context", "issue", "ledger"]
_TREE_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
_MAX_FRAME_BYTES = 131_072
_MAX_SOCKET_PATH_BYTES = 100
_SOCKET_DIRECTORY_PREFIX = "awr-"
_SOCKET_BASENAME = "s"
_PRIVATE_GIT_FIELDS = frozenset(
    {"nonce", "token", "secret", "credential", "password", "private_key"}
)


def _public_git_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _public_git_value(item)
            for key, item in value.items()
            if isinstance(key, str) and key.lower() not in _PRIVATE_GIT_FIELDS
        }
    if isinstance(value, (list, tuple)):
        return [_public_git_value(item) for item in value]
    return value


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


class WorkerGitBrokerSession(WorkerReadBrokerSession):
    """Serve four closed Git operations for one live worker checkout."""

    def __init__(
        self,
        identity: WorkerReadSessionIdentity,
        *,
        identity_check: Callable[[WorkerReadSessionIdentity], bool],
        git_action: Callable[[str, str | None], dict[str, object]],
        reply_check: Callable[[], bool],
        timeout_seconds: float,
        temporary_directory: Path | None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._git_action = git_action
        self._reply_check = reply_check
        self._staged_tree: str | None = None
        super().__init__(
            identity,
            identity_check=identity_check,
            read_action=lambda _action: {},
            timeout_seconds=timeout_seconds,
            temporary_directory=temporary_directory,
            clock=clock,
        )

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
                    if not isinstance(request, dict):
                        raise TypeError("worker_git: invalid request")
                    action = request.get("action")
                    expected_keys = (
                        {"nonce", "action", "candidate_tree"}
                        if action in {"git_candidate_status", "git_commit_candidate"}
                        else {"nonce", "action"}
                    )
                    if (
                        set(request) != expected_keys
                        or request.get("nonce") != self._identity.nonce
                        or action
                        not in {
                            "git_status",
                            "git_stage_candidate",
                            "git_candidate_status",
                            "git_commit_candidate",
                        }
                        or self._clock() >= self._deadline
                        or not self._identity_check(self._identity)
                    ):
                        raise ValueError("worker_git: invalid identity or operation")
                    tree = request.get("candidate_tree")
                    if action in {"git_candidate_status", "git_commit_candidate"} and (
                        not isinstance(tree, str)
                        or _TREE_PATTERN.fullmatch(tree) is None
                        or tree != self._staged_tree
                    ):
                        raise ValueError("worker_git: candidate tree is not staged")
                    result = self._git_action(cast(str, action), cast(str | None, tree))
                    if not self._reply_check():
                        raise ValueError("worker_git: identity changed")
                    if action == "git_stage_candidate":
                        staged = result.get("candidate_tree")
                        if (
                            not isinstance(staged, str)
                            or _TREE_PATTERN.fullmatch(staged) is None
                        ):
                            raise ValueError("worker_git: invalid candidate tree")
                        self._staged_tree = staged
                    public = _public_git_value(result)
                    encoded = json.dumps(public)
                    if len(encoded.encode()) > _MAX_FRAME_BYTES:
                        raise ValueError("worker_git: response too large")
                    if self._stop.is_set() or self._clock() >= self._deadline:
                        raise ValueError("worker_git: expired")
                    _write_frame(connection, {"accepted": True, "value": public})
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


def request_worker_git(
    socket_path: Path,
    nonce: str,
    action: str,
    *,
    candidate_tree: str | None = None,
    timeout_seconds: float = 10,
) -> dict[str, object]:
    """Request one controller-bound worker Git operation."""
    payload: dict[str, object] = {"nonce": nonce, "action": action}
    if candidate_tree is not None:
        payload["candidate_tree"] = candidate_tree
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            _write_frame(connection, payload)
            response = json.loads(_read_frame(connection))
    except (OSError, ValueError, TimeoutError):
        raise ValueError("worker_git: submission failed") from None
    if (
        isinstance(response, dict)
        and response.get("accepted") is True
        and isinstance(response.get("value"), dict)
    ):
        return cast(dict[str, object], response["value"])
    raise ValueError("worker_git: request failed")


def _validation_selector(value: object) -> bool:
    """Check one closed, bounded evidence page selector."""
    if not isinstance(value, dict):
        return False
    value = cast(dict[str, object], value)
    if value == {"view": "summary"}:
        return True
    environment = value.get("environment")
    offset = value.get("offset")
    limit = value.get("limit")
    return (
        set(value) == {"view", "environment", "offset", "limit"}
        and value.get("view") in {"junit", "timing"}
        and isinstance(environment, str)
        and environment in {"py311", "py314"}
        and type(offset) is int
        and 0 <= offset <= 100_000
        and type(limit) is int
        and 1 <= limit <= 100
    )


class WorkerValidationBrokerSession(WorkerReadBrokerSession):
    """Serve only validation and bounded evidence for one staged Judge tree."""

    def __init__(
        self,
        identity: WorkerReadSessionIdentity,
        *,
        identity_check: Callable[[WorkerReadSessionIdentity], bool],
        validation_action: Callable[[str, dict[str, object] | None], dict[str, object]],
        candidate_tree: str,
        timeout_seconds: float,
        temporary_directory: Path | None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._validation_action = validation_action
        self._candidate_tree = candidate_tree
        super().__init__(
            identity,
            identity_check=identity_check,
            read_action=lambda _action: {},
            timeout_seconds=timeout_seconds,
            temporary_directory=temporary_directory,
            clock=clock,
        )

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
                    if not isinstance(request, dict):
                        raise TypeError("worker_validation: invalid request")
                    action = request.get("action")
                    selector = request.get("selector")
                    expected = (
                        {"nonce", "action", "candidate_tree", "selector"}
                        if action == "evidence"
                        else {"nonce", "action", "candidate_tree"}
                    )
                    if (
                        set(request) != expected
                        or action not in {"validate", "status", "evidence"}
                        or request.get("nonce") != self._identity.nonce
                        or request.get("candidate_tree") != self._candidate_tree
                        or (action == "evidence" and not _validation_selector(selector))
                        or self._clock() >= self._deadline
                        or not self._identity_check(self._identity)
                    ):
                        raise ValueError(
                            "worker_validation: identity or request invalid"
                        )
                    result = self._validation_action(
                        cast(str, action), cast(dict[str, object] | None, selector)
                    )
                    encoded = json.dumps(result, ensure_ascii=True)
                    if len(encoded.encode()) > _MAX_FRAME_BYTES:
                        raise ValueError("worker_validation: response limit exceeded")
                    if (
                        self._stop.is_set()
                        or self._clock() >= self._deadline
                        or not self._identity_check(self._identity)
                    ):
                        raise ValueError("worker_validation: identity changed")
                    _write_frame(connection, {"accepted": True, "value": result})
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


def request_worker_validation(
    socket_path: Path,
    nonce: str,
    action: str,
    *,
    candidate_tree: str,
    selector: dict[str, object] | None = None,
    timeout_seconds: float = 3600,
) -> dict[str, object]:
    """Request one closed validation operation from a live controller."""
    payload: dict[str, object] = {
        "nonce": nonce,
        "action": action,
        "candidate_tree": candidate_tree,
    }
    if selector is not None:
        payload["selector"] = selector
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            _write_frame(connection, payload)
            response = json.loads(_read_frame(connection))
    except (OSError, ValueError, TimeoutError):
        raise ValueError("worker_validation: submission failed") from None
    if (
        isinstance(response, dict)
        and response.get("accepted") is True
        and isinstance(response.get("value"), dict)
    ):
        return cast(dict[str, object], response["value"])
    raise ValueError("worker_validation: request failed")


class _DocumentationPatchFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    nonce: str
    request: DocumentationPatchRequest


class _DocumentationPatchReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    accepted: bool
    value: DocumentationPatchResult | None = None


class WorkerDocumentationPatchBrokerSession(WorkerReadBrokerSession):
    """Authenticate one closed documentation service without Git authority."""

    def __init__(
        self,
        identity: WorkerReadSessionIdentity,
        *,
        identity_check: Callable[[WorkerReadSessionIdentity], bool],
        patch_action: Callable[[DocumentationPatchRequest], DocumentationPatchResult],
        timeout_seconds: float,
        temporary_directory: Path | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if temporary_directory is not None and not _socket_path_fits(
            temporary_directory
        ):
            raise ValueError("documentation_patch: socket path unavailable")
        self._patch_action = patch_action
        self._patch_terminal = False
        super().__init__(
            identity,
            identity_check=identity_check,
            read_action=lambda _action: {},
            timeout_seconds=timeout_seconds,
            temporary_directory=temporary_directory,
            clock=clock,
        )

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
                    frame = _DocumentationPatchFrame.model_validate_json(
                        _read_frame(connection)
                    )
                    if frame.nonce != self._identity.nonce:
                        raise ValueError("documentation_patch: invalid session")
                    if self._patch_terminal:
                        result = DocumentationPatchResult(
                            outcome="refused", reason="patch_refused"
                        )
                    elif self._clock() >= self._deadline or not self._identity_check(
                        self._identity
                    ):
                        result = DocumentationPatchResult(
                            outcome="refused", reason="identity_changed"
                        )
                    else:
                        result = DocumentationPatchResult.model_validate_json(
                            self._patch_action(frame.request).model_dump_json()
                        )
                        if (
                            result.outcome in {"applied", "failed_partial"}
                            or result.quarantined
                        ):
                            self._patch_terminal = True
                    _write_frame(
                        connection,
                        {"accepted": True, "value": result.model_dump(mode="json")},
                    )
                except (OSError, ValueError, TypeError, RuntimeError):
                    try:
                        _write_frame(connection, {"accepted": False})
                    except (OSError, ValueError):
                        pass


def request_worker_documentation_patch(
    socket_path: Path,
    nonce: str,
    request: DocumentationPatchRequest,
    *,
    timeout_seconds: float = 10,
) -> DocumentationPatchResult:
    """Submit a bounded authenticated patch frame to the private host service."""
    try:
        validated = DocumentationPatchRequest.model_validate_json(
            request.model_dump_json()
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout_seconds)
            connection.connect(str(socket_path))
            _write_frame(
                connection,
                {"nonce": nonce, "request": validated.model_dump(mode="json")},
            )
            reply = _DocumentationPatchReply.model_validate_json(
                _read_frame(connection)
            )
        if reply.accepted and reply.value is not None:
            return reply.value
    except (OSError, ValueError, TypeError):
        raise ValueError("documentation_patch: submission failed") from None
    raise ValueError("documentation_patch: request failed")
