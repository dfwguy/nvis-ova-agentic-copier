# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""An opt-in worker MCP reads only controller-owned, identity-bound facts."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import CallToolResult

from agent_work.mcp_server import create_candidate_mcp_server

NONCE = "a" * 32
SOURCE_SHA = "b" * 40
TOOLS = {"worker_context", "worker_issue", "worker_ledger"}
PUBLIC: dict[str, dict[str, object]] = {
    "context": {"state": "executing", "phase": "workflow", "workflow_lane": "coding"},
    "issue": {"title": "Bounded issue", "description": "Read-only details"},
    "ledger": {
        "revision": 7,
        "source_branch": "agent/issue-363",
        "source_sha": SOURCE_SHA,
        "state": "executing",
        "phase": "workflow",
    },
}
PRIVATE = {
    "nonce": NONCE,
    "project_id": 310967,
    "issue_iid": 363,
    "checkout": "/private/checkout",
    "credential": "secret-token",
    "gitlab_endpoint": "https://private.invalid/api",
}


def _worker_read() -> Any:
    return importlib.import_module("agent_work.worker_read")


def _identity(checkout: Path) -> Any:
    return _worker_read().WorkerReadSessionIdentity(
        nonce=NONCE,
        project_id=310967,
        issue_iid=363,
        checkout=checkout,
        phase="workflow",
        ledger_revision=7,
        source_sha=SOURCE_SHA,
    )


def _session(
    tmp_path: Path,
    *,
    identity_check: Callable[[Any], bool],
    read_action: Callable[[str], dict[str, object]],
) -> Any:
    return _worker_read().create_worker_read_broker(
        _identity(tmp_path / "checkout"),
        identity_check=identity_check,
        read_action=read_action,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )


def test_worker_read_socket_and_directory_are_owner_only(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        identity_check=lambda _identity: True,
        read_action=lambda action: PUBLIC[action],
    )
    try:
        socket_path = session.socket_path
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        socket_stat = socket_path.lstat()
        assert stat.S_ISSOCK(socket_stat.st_mode)
        assert stat.S_IMODE(socket_stat.st_mode) == 0o600
    finally:
        session.close()


def test_worker_read_uses_private_live_socket_with_long_temporary_parent(
    tmp_path: Path,
) -> None:
    long_parent = tmp_path / ("long-" + "x" * 100)
    long_parent.mkdir()
    session = _session(
        long_parent,
        identity_check=lambda _identity: True,
        read_action=lambda action: PUBLIC[action],
    )
    socket_path = session.socket_path
    try:
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        assert (
            _worker_read().request_worker_read(socket_path, NONCE, "context")
            == (PUBLIC["context"])
        )
    finally:
        session.close()
    assert not socket_path.parent.exists()
    assert list(long_parent.iterdir()) == []


def test_worker_read_cleans_private_directory_when_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_parent = tmp_path / ("long-" + "x" * 100)
    long_parent.mkdir()
    original_mkdtemp = tempfile.mkdtemp
    original_open = os.open
    created_roots: list[Path] = []

    def record_private_directory(*args: Any, **kwargs: Any) -> str:
        root = original_mkdtemp(*args, **kwargs)
        created_roots.append(Path(root))
        return root

    def fail_private_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        if created_roots and Path(path) == created_roots[-1]:
            raise OSError("private directory open failed")
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patcher:
        patcher.setattr(tempfile, "mkdtemp", record_private_directory)
        patcher.setattr(os, "open", fail_private_open)
        with pytest.raises((OSError, RuntimeError)):
            _session(
                long_parent,
                identity_check=lambda _identity: True,
                read_action=lambda action: PUBLIC[action],
            )
    assert len(created_roots) == 1
    assert created_roots[0].parent != long_parent
    assert not created_roots[0].exists()
    assert list(long_parent.iterdir()) == []


def test_worker_read_closes_private_fd_and_directory_when_socket_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_parent = tmp_path / ("long-" + "x" * 100)
    long_parent.mkdir()
    original_mkdtemp = tempfile.mkdtemp
    original_open = os.open
    created_roots: list[Path] = []
    opened: list[int] = []

    def record_private_directory(*args: Any, **kwargs: Any) -> str:
        root = original_mkdtemp(*args, **kwargs)
        created_roots.append(Path(root))
        return root

    def record_private_open(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if created_roots and Path(path) == created_roots[-1]:
            opened.append(descriptor)
        return descriptor

    def fail_socket(*_args: object, **_kwargs: object) -> socket.socket:
        raise OSError("socket acquisition failed")

    with monkeypatch.context() as patcher:
        patcher.setattr(tempfile, "mkdtemp", record_private_directory)
        patcher.setattr(os, "open", record_private_open)
        patcher.setattr(socket, "socket", fail_socket)
        with pytest.raises((OSError, RuntimeError)):
            _session(
                long_parent,
                identity_check=lambda _identity: True,
                read_action=lambda action: PUBLIC[action],
            )
    assert len(created_roots) == 1
    assert created_roots[0].parent != long_parent
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not created_roots[0].exists()
    assert list(long_parent.iterdir()) == []


@pytest.mark.parametrize("action", ["context", "issue", "ledger"])
def test_worker_read_broker_projects_only_bounded_public_fields(
    tmp_path: Path, action: str
) -> None:
    checked: list[Any] = []
    dispatched: list[str] = []

    def identity_check(identity: Any) -> bool:
        checked.append(identity)
        return identity == _identity(tmp_path / "checkout")

    def read_action(requested: str) -> dict[str, object]:
        dispatched.append(requested)
        return {**PUBLIC[requested], **PRIVATE}

    session = _session(tmp_path, identity_check=identity_check, read_action=read_action)
    try:
        response = _worker_read().request_worker_read(
            session.socket_path, NONCE, action
        )
    finally:
        session.close()

    assert response == PUBLIC[action]
    assert len(checked) == 1
    assert dispatched == [action]
    assert not ({"nonce", "checkout", "credential"} & set(response))


def test_worker_read_broker_rechecks_live_identity_before_every_dispatch(
    tmp_path: Path,
) -> None:
    live = {"revision": 7, "phase": "workflow", "source_sha": SOURCE_SHA}
    checked: list[str] = []
    dispatched: list[str] = []

    def identity_check(identity: Any) -> bool:
        checked.append(identity.source_sha)
        return (
            identity.ledger_revision == live["revision"]
            and identity.phase == live["phase"]
            and identity.source_sha == live["source_sha"]
        )

    def read_action(action: str) -> dict[str, object]:
        dispatched.append(action)
        return PUBLIC[action]

    session = _session(tmp_path, identity_check=identity_check, read_action=read_action)
    try:
        first = _worker_read().request_worker_read(
            session.socket_path, NONCE, "context"
        )
        live["revision"] = 8
        with pytest.raises(Exception, match="worker_read|read failed|identity"):
            _worker_read().request_worker_read(session.socket_path, NONCE, "ledger")
    finally:
        session.close()

    assert first == PUBLIC["context"]
    assert len(checked) == 2
    assert dispatched == ["context"]


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("phase", "publication"),
        ("source_sha", "c" * 40),
        ("project_id", 310968),
        ("issue_iid", 364),
        ("checkout", Path("/other/checkout")),
    ],
)
def test_worker_read_broker_denies_cross_identity_before_provider(
    tmp_path: Path, field: str, changed: object
) -> None:
    dispatched: list[str] = []
    current = {field: changed}

    def identity_check(identity: Any) -> bool:
        return getattr(identity, field) == current[field]

    session = _session(
        tmp_path,
        identity_check=identity_check,
        read_action=lambda action: dispatched.append(action) or PUBLIC[action],
    )
    try:
        with pytest.raises(Exception, match="worker_read|read failed|identity"):
            _worker_read().request_worker_read(session.socket_path, NONCE, "context")
    finally:
        session.close()
    assert dispatched == []


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "admin"},
        {"project_id": 310967},
        {"issue_iid": 363},
        {"checkout": "/other"},
        {"phase": "workflow"},
        {"ledger_revision": 7},
        {"source_sha": SOURCE_SHA},
        {"path": "../secret"},
        {"git_argv": ["show", "HEAD"]},
        {"gitlab_endpoint": "/projects/other"},
        {"field": "credential"},
    ],
)
def test_worker_read_broker_rejects_caller_controlled_fields_before_provider(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    checked: list[Any] = []
    dispatched: list[str] = []
    session = _session(
        tmp_path,
        identity_check=lambda identity: checked.append(identity) or True,
        read_action=lambda action: dispatched.append(action) or PUBLIC[action],
    )
    try:
        response = _raw_request(
            session.socket_path, {"nonce": NONCE, "action": "context", **extra}
        )
    finally:
        session.close()
    assert response is None or response.get("accepted") is False
    assert checked == []
    assert dispatched == []


def _raw_request(path: Path, payload: dict[str, object]) -> dict[str, object] | None:
    encoded = json.dumps(payload).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        connection.connect(str(path))
        connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
        size_bytes = connection.recv(4)
        if not size_bytes:
            return None
        size = int.from_bytes(size_bytes, "big")
        return cast(dict[str, object], json.loads(connection.recv(size)))


@pytest.mark.parametrize("nonce", ["b" * 32, "", "a" * 31])
def test_worker_read_broker_rejects_wrong_nonce_before_provider(
    tmp_path: Path, nonce: str
) -> None:
    dispatched: list[str] = []
    session = _session(
        tmp_path,
        identity_check=lambda _identity: True,
        read_action=lambda action: dispatched.append(action) or PUBLIC[action],
    )
    try:
        with pytest.raises(Exception, match="worker_read|read failed|identity"):
            _worker_read().request_worker_read(session.socket_path, nonce, "context")
    finally:
        session.close()
    assert dispatched == []


@pytest.mark.parametrize("action", ["worker_context", "status", "stage", "read", ""])
def test_worker_read_broker_rejects_unknown_action_before_provider(
    tmp_path: Path, action: str
) -> None:
    dispatched: list[str] = []
    session = _session(
        tmp_path,
        identity_check=lambda _identity: True,
        read_action=lambda requested: dispatched.append(requested) or PUBLIC[requested],
    )
    try:
        with pytest.raises(Exception, match="worker_read|read failed|invalid"):
            _worker_read().request_worker_read(session.socket_path, NONCE, action)
    finally:
        session.close()
    assert dispatched == []


def test_closed_worker_read_session_cannot_reuse_socket_or_nonce(
    tmp_path: Path,
) -> None:
    dispatched: list[str] = []
    session = _session(
        tmp_path,
        identity_check=lambda _identity: True,
        read_action=lambda action: dispatched.append(action) or PUBLIC[action],
    )
    path = session.socket_path
    session.close()

    with pytest.raises(Exception, match="worker_read|read failed|submission"):
        _worker_read().request_worker_read(path, NONCE, "context")
    assert dispatched == []


def test_expired_worker_read_session_rejects_request_without_provider(
    tmp_path: Path,
) -> None:
    now = [0.0]
    dispatched: list[str] = []
    session = _worker_read().create_worker_read_broker(
        _identity(tmp_path / "checkout"),
        identity_check=lambda _identity: True,
        read_action=lambda action: dispatched.append(action) or PUBLIC[action],
        timeout_seconds=2,
        temporary_directory=tmp_path,
        clock=lambda: now[0],
    )
    now[0] = 3.0
    try:
        with pytest.raises(
            Exception, match="worker_read|read failed|expired|submission"
        ):
            _worker_read().request_worker_read(
                session.socket_path, NONCE, "context", timeout_seconds=0.2
            )
    finally:
        session.close()
    assert dispatched == []


@pytest.mark.parametrize(
    ("action", "bad_value"),
    [("context", "x" * 3000), ("issue", "x" * 100_000), ("ledger", -1)],
)
def test_worker_read_broker_rejects_oversized_or_invalid_projection_without_secret(
    tmp_path: Path, action: str, bad_value: object
) -> None:
    response: dict[str, object] = dict(PUBLIC[action])
    field = {"context": "state", "issue": "description", "ledger": "revision"}[action]
    response[field] = bad_value
    response["credential"] = "secret-token"
    session = _session(
        tmp_path, identity_check=lambda _identity: True, read_action=lambda _a: response
    )
    try:
        with pytest.raises(Exception) as failure:
            _worker_read().request_worker_read(session.socket_path, NONCE, action)
    finally:
        session.close()
    assert "secret-token" not in str(failure.value)


def test_worker_read_broker_redacts_provider_failure(tmp_path: Path) -> None:
    def read_action(_action: str) -> dict[str, object]:
        raise RuntimeError("secret-token from controller provider")

    session = _session(
        tmp_path, identity_check=lambda _identity: True, read_action=read_action
    )
    try:
        with pytest.raises(Exception) as failure:
            _worker_read().request_worker_read(session.socket_path, NONCE, "issue")
    finally:
        session.close()
    assert "secret-token" not in str(failure.value)


@pytest.mark.parametrize("action", ["context", "issue", "ledger"])
def test_worker_read_mcp_exposes_only_three_no_argument_tools(action: str) -> None:
    dispatched: list[str] = []

    def gateway(requested: str) -> dict[str, object]:
        dispatched.append(requested)
        return PUBLIC[requested]

    server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_read_mcp_server(gateway)
    listed = asyncio.run(server.list_tools())
    assert {tool.name for tool in listed} == TOOLS
    for tool in listed:
        assert tool.input_schema["properties"] == {}
        assert tool.input_schema["additionalProperties"] is False
    result = cast(CallToolResult, asyncio.run(server.call_tool(f"worker_{action}", {})))
    assert not result.is_error
    assert result.structured_content == PUBLIC[action]
    assert dispatched == [action]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("candidate_status", {}),
        ("worker_context", {"path": "README.md"}),
        ("worker_issue", {"field": "credential"}),
        ("worker_ledger", {"gitlab_endpoint": "/projects/other"}),
        ("worker_unknown", {}),
    ],
)
def test_worker_read_mcp_rejects_unknown_tool_or_any_argument(
    tool: str, arguments: dict[str, object]
) -> None:
    dispatched: list[str] = []
    server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_read_mcp_server(
        lambda action: dispatched.append(action) or PUBLIC[action]
    )

    result = cast(CallToolResult, asyncio.run(server.call_tool(tool, arguments)))

    assert result.is_error
    assert dispatched == []


def test_worker_read_mcp_does_not_expand_candidate_preview_listing() -> None:
    worker_server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_read_mcp_server(lambda action: PUBLIC[action])
    worker_tools = {tool.name for tool in asyncio.run(worker_server.list_tools())}
    server = create_candidate_mcp_server(
        lambda _action, _tree, _selector: {"candidate_tree": SOURCE_SHA}, SOURCE_SHA
    )

    listed = asyncio.run(server.list_tools())

    assert {tool.name for tool in listed} == {
        "candidate_status",
        "candidate_changed_paths",
        "candidate_read",
        "candidate_submit_review",
    }
    assert {tool.name for tool in listed}.isdisjoint(worker_tools)
