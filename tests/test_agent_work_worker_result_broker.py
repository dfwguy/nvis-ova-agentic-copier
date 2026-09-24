# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the producer-owned worker-result broker."""

from __future__ import annotations

import importlib
import json
import shutil
import socket
import stat
import threading
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, Self, cast

import pytest

from agent_work.cli import build_parser, main
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.models import LedgerRecord, WorkerResultEnvelope
from agent_work.repository import CommandResult
from agent_work.worker import (
    AgentInvoker,
    WorkerProtocolError,
    WorkerResultBrokerFactory,
)


def _request() -> Any:
    worker = importlib.import_module("agent_work.worker")
    return worker.WorkerResultBrokerRequest(
        nonce="0123456789abcdef" * 2,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )


def test_broker_request_contains_only_controller_derived_identity() -> None:
    """Workers receive no path that could become durable controller state."""
    request = _request()

    assert request.expected_state is RunnerState.EXECUTING
    assert request.expected_phase is WorkflowPhase.WORKFLOW
    assert not hasattr(request, "path")
    assert not hasattr(request, "state_root")
    assert not hasattr(request, "ledger_directory")


def test_worker_result_apply_requires_private_socket_and_nonce() -> None:
    """Applied CLI submission cannot run without both live broker capabilities."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "worker-result",
                "build",
                "--issue-iid",
                "81",
                "--input-file",
                "/tmp/input.json",
                "--apply",
            ]
        )


@pytest.mark.parametrize("as_json", (False, True))
def test_worker_result_preview_uses_the_selected_human_or_json_format(
    tmp_path: Path, as_json: bool
) -> None:
    """Preview selects concise human output or the exact JSON envelope."""
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "workflow_lane": "coding",
                "result_kind": "needs_human",
                "change_summary": "A reviewer decision is required.",
                "next_action": "Wait for the reviewer decision.",
            }
        )
    )
    output: list[str] = []
    arguments = [
        "worker-result",
        "build",
        "--issue-iid",
        "81",
        "--input-file",
        str(input_path),
    ]
    if as_json:
        arguments.append("--json")

    status = main(
        arguments,
        load_record=lambda issue_iid: _record(tmp_path) if issue_iid == 81 else None,
        changed_paths=lambda _worktree: (),
        load_red_checkpoint=lambda *_args: None,
        write_stdout=output.append,
    )

    assert status == 0 and len(output) == 1
    if as_json:
        envelope = json.loads(output[0])
        assert envelope["issue_iid"] == 81
        assert envelope["result_kind"] == "needs_human"
        assert envelope["change_summary"] == "A reviewer decision is required."
    else:
        assert output == [
            (
                "valid issue=#81 lane=coding result=needs_human "
                "summary=A reviewer decision is required. "
                "next_action=Wait for the reviewer decision."
            )
        ]


def test_worker_result_preview_removes_terminal_controls_from_human_output(
    tmp_path: Path,
) -> None:
    """Human output is terminal-safe while JSON preserves structured text."""
    change_summary = "Safe \x1b[31mred\x1b[0m café\x00\x1f\x7f\x80"
    next_action = "First\r\nSecond\x9b31mgreen\x9b0m"
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            {
                "workflow_lane": "coding",
                "result_kind": "needs_human",
                "change_summary": change_summary,
                "next_action": next_action,
            }
        )
    )

    human_output: list[str] = []
    json_output: list[str] = []
    common_arguments = [
        "worker-result",
        "build",
        "--issue-iid",
        "81",
        "--input-file",
        str(input_path),
    ]

    def run_preview(arguments: list[str], output: list[str]) -> int:
        return main(
            arguments,
            load_record=lambda issue_iid: (
                _record(tmp_path) if issue_iid == 81 else None
            ),
            changed_paths=lambda _worktree: (),
            load_red_checkpoint=lambda *_args: None,
            write_stdout=output.append,
        )

    assert run_preview(common_arguments, human_output) == 0
    assert run_preview([*common_arguments, "--json"], json_output) == 0

    assert human_output == [
        (
            "valid issue=#81 lane=coding result=needs_human "
            "summary=Safe red café next_action=First Secondgreen"
        )
    ]
    assert not any(
        unicodedata.category(character) in {"Cc", "Cf"} for character in human_output[0]
    )
    envelope = json.loads(json_output[0])
    assert envelope["change_summary"] == change_summary
    assert envelope["next_action"] == next_action


def test_broker_cache_setup_failure_removes_private_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before socket creation cannot retain scratch or its root fd."""
    worker = importlib.import_module("agent_work.worker")
    original_chmod = worker.os.chmod

    def reject_cache_chmod(path: Path, mode: int) -> None:
        if Path(path).name == "uv-cache":
            raise OSError("cache mode rejected")
        original_chmod(path, mode)

    monkeypatch.setattr(worker.os, "chmod", reject_cache_chmod)

    with pytest.raises(OSError, match="cache mode rejected"):
        worker.create_worker_result_broker(
            _request(),
            build_artifact=lambda received: received,
            timeout_seconds=10,
            temporary_directory=tmp_path,
        )

    assert tuple(tmp_path.glob("agent-work-broker-*")) == ()
    fd_targets: list[str] = []
    for fd_path in Path("/proc/self/fd").iterdir():
        try:
            fd_targets.append(str(fd_path.readlink()))
        except OSError:
            continue
    assert not any(str(tmp_path) in target for target in fd_targets)


def test_broker_startup_failure_removes_scratch_and_closes_its_root_fd(
    tmp_path: Path,
) -> None:
    """An AF_UNIX bind failure cannot leak private scratch or its root fd."""
    worker = importlib.import_module("agent_work.worker")
    long_temporary_directory = tmp_path / ("long-unix-socket-parent-" * 4)
    long_temporary_directory.mkdir()

    def matching_fd_targets() -> tuple[str, ...]:
        targets: list[str] = []
        for fd_path in Path("/proc/self/fd").iterdir():
            try:
                target = str(fd_path.readlink())
            except OSError:
                continue
            if str(long_temporary_directory) in target:
                targets.append(target)
        return tuple(targets)

    assert (
        len(str(long_temporary_directory / "agent-work-broker-xxxxxxxx/result.sock"))
        > 107
    )
    with pytest.raises(OSError):
        worker.create_worker_result_broker(
            _request(),
            build_artifact=lambda received: received,
            timeout_seconds=10,
            temporary_directory=long_temporary_directory,
        )

    assert tuple(long_temporary_directory.iterdir()) == ()
    assert matching_fd_targets() == ()


def test_worker_result_apply_json_emits_canonical_broker_receipt(
    tmp_path: Path,
) -> None:
    """A live accepted submission prints only the canonical nonce receipt."""
    request = _request()
    input_bytes = b'{"workflow_lane":"coding","result_kind":"needs_human"}'
    input_path = tmp_path / "input.json"
    input_path.write_bytes(input_bytes)
    session = importlib.import_module("agent_work.worker").create_worker_result_broker(
        request,
        build_artifact=lambda received: received,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    output: list[str] = []
    try:
        status = main(
            [
                "worker-result",
                "build",
                "--issue-iid",
                "81",
                "--input-file",
                str(input_path),
                "--broker-socket",
                str(session.socket_path),
                "--broker-nonce",
                request.nonce,
                "--apply",
                "--json",
            ],
            write_stdout=output.append,
        )
        artifact = session.close_and_capture()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass

    assert status == 0
    assert output == [f'{{"accepted":true,"nonce":"{request.nonce}"}}']
    assert artifact == input_bytes


def test_worker_result_apply_rejects_a_different_issue_iid(tmp_path: Path) -> None:
    """The applied CLI binds the issue argument to the live broker session."""
    request = _request()
    input_path = tmp_path / "input.json"
    input_path.write_bytes(b'{"workflow_lane":"coding"}')
    session = importlib.import_module("agent_work.worker").create_worker_result_broker(
        request,
        build_artifact=lambda received: received,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    errors: list[str] = []
    try:
        status = main(
            [
                "worker-result",
                "build",
                "--issue-iid",
                "82",
                "--input-file",
                str(input_path),
                "--broker-socket",
                str(session.socket_path),
                "--broker-nonce",
                request.nonce,
                "--apply",
            ],
            write_stderr=errors.append,
        )
        with pytest.raises(WorkerProtocolError, match="identity mismatch"):
            session.close_and_capture()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass

    assert status == 1
    assert errors == ["worker_result_broker: request identity mismatch"]


def test_broker_cleanup_removes_private_worker_scratch(tmp_path: Path) -> None:
    """Cleanup removes scratch without following worker-created symlinks."""
    worker = importlib.import_module("agent_work.worker")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("outside survives")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda received: received,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    private_root = session.socket_path.parent
    assert stat.S_IMODE(session.cache_path.stat().st_mode) == 0o700
    (session.cache_path / "cached-wheel").write_bytes(b"ephemeral")
    input_bytes = b'{"workflow_lane":"coding","result_kind":"needs_human"}'
    (private_root / "worker-input.json").write_bytes(input_bytes)
    nested = private_root / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("remove me")
    (nested / "outside-link").symlink_to(outside, target_is_directory=True)

    worker.submit_worker_result_to_broker(
        session.socket_path,
        _request(),
        input_bytes,
        timeout_seconds=10,
    )
    artifact = session.close_and_capture()

    assert artifact == input_bytes
    assert not private_root.exists()
    assert protected.read_text() == "outside survives"


@pytest.mark.parametrize("race", ("deleted", "replaced"))
def test_broker_cleanup_fails_closed_when_private_root_identity_changes(
    tmp_path: Path, race: str
) -> None:
    """A deleted or replaced controller root cannot be accepted as cleanup."""
    worker = importlib.import_module("agent_work.worker")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda received: received,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    private_root = session.socket_path.parent
    worker.submit_worker_result_to_broker(
        session.socket_path,
        _request(),
        b'{"workflow_lane":"coding"}',
        timeout_seconds=10,
    )
    if race == "deleted":
        shutil.rmtree(private_root)
    else:
        moved_root = tmp_path / "moved-private-root"
        private_root.rename(moved_root)
        private_root.mkdir()

    with pytest.raises(WorkerProtocolError, match="cleanup failed"):
        session.close_and_capture()

    assert not session._thread.is_alive()
    assert session._socket.fileno() == -1


def test_agent_invoker_keeps_the_broker_live_for_the_producer(
    tmp_path: Path,
) -> None:
    """The worker gets only the private socket directory and no result-file transport."""
    request = _request()
    checkout = tmp_path / "worker-checkout"
    checkout.mkdir()
    socket_path = tmp_path / "private-broker" / "broker.sock"
    calls: list[tuple[str, ...]] = []

    class Session:
        def __init__(self) -> None:
            self.closed = False
            self.socket_path = socket_path
            self.cache_path = socket_path.parent / "uv-cache"
            self.cache_path.mkdir(parents=True, mode=0o700)

        def close_and_capture(self) -> bytes:
            self.closed = True
            return b'{"accepted":true}'

    session = Session()

    def broker_factory(received: Any, **_kwargs: object) -> Session:
        assert received == request
        return session

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append(command)
        assert str(socket_path.parent) in command
        environment = cast(dict[str, str], kwargs["environment"])
        assert environment["UV_CACHE_DIR"] == str(session.cache_path)
        assert kwargs["stdin"] == (
            f"bin/agent-work-from-checkout --broker-socket {socket_path} "
            f"--broker-nonce {request.nonce}"
        )
        return CommandResult(0, "untrusted worker stdout", "")

    invocation = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        broker_factory=cast(WorkerResultBrokerFactory, broker_factory),
    ).invoke(
        checkout,
        (
            "bin/agent-work-from-checkout --broker-socket PATH --broker-nonce "
            f"{request.nonce}"
        ),
        10,
        broker_request=request,
        build_artifact=lambda _input: b'{"accepted":true}',
    )

    assert len(calls) == 1
    assert session.closed is True
    assert invocation.worker_result == b'{"accepted":true}'
    assert invocation.stdout == "untrusted worker stdout"


@pytest.mark.parametrize("returncode", (0, 23))
@pytest.mark.parametrize(
    "reason",
    (
        "request was not accepted",
        "malformed request",
        "request is not permitted",
        "request rejected",
        "cleanup failed",
    ),
)
def test_agent_invoker_preserves_typed_broker_failure_after_process_exit(
    tmp_path: Path, returncode: int, reason: str
) -> None:
    """The process result cannot turn a broker failure into a timeout."""
    request = _request()
    socket_path = tmp_path / "private-broker" / "broker.sock"

    class Session:
        def __init__(self) -> None:
            self.socket_path = socket_path
            self.cache_path = socket_path.parent / "uv-cache"
            self.cache_path.mkdir(parents=True, mode=0o700)

        def close_and_capture(self) -> bytes:
            raise WorkerProtocolError("worker_result_broker", reason)

    invocation = AgentInvoker(
        lambda *_args, **_kwargs: CommandResult(
            returncode, "untrusted stdout", "untrusted stderr"
        ),
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        broker_factory=cast(
            WorkerResultBrokerFactory, lambda _request, **_kwargs: Session()
        ),
    ).invoke(
        tmp_path,
        "--broker-socket PATH",
        10,
        broker_request=request,
        build_artifact=lambda _input: b"accepted",
    )

    assert invocation.returncode == returncode
    assert invocation.protocol_error is not None
    assert invocation.protocol_error.field == "worker_result_broker"
    assert invocation.protocol_error.reason == reason


def test_agent_invoker_command_timeout_has_no_protocol_failure(tmp_path: Path) -> None:
    """A command-runner timeout retains its distinct untyped timeout signal."""
    request = _request()
    socket_path = tmp_path / "private-broker" / "broker.sock"

    class Session:
        def __init__(self) -> None:
            self.socket_path = socket_path
            self.cache_path = socket_path.parent / "uv-cache"
            self.cache_path.mkdir(parents=True, mode=0o700)

        def close_and_capture(self) -> bytes:
            raise WorkerProtocolError(
                "worker_result_broker", "request was not accepted"
            )

    def timeout(*_args: object, **_kwargs: object) -> CommandResult:
        raise RuntimeError("worker timed out")

    invocation = AgentInvoker(
        timeout,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        broker_factory=cast(
            WorkerResultBrokerFactory, lambda _request, **_kwargs: Session()
        ),
    ).invoke(
        tmp_path,
        "--broker-socket PATH",
        10,
        broker_request=request,
        build_artifact=lambda _input: b"accepted",
    )

    assert invocation.returncode is None
    assert invocation.protocol_error is None


def test_broker_creates_private_socket_and_fails_closed_without_a_request(
    tmp_path: Path,
) -> None:
    """A fresh invocation owns a 0700 directory and 0600 socket until capture."""
    worker = importlib.import_module("agent_work.worker")
    attempts: list[bytes] = []

    def build_artifact(input_bytes: bytes) -> bytes:
        attempts.append(input_bytes)
        if len(attempts) == 1:
            raise WorkerProtocolError("builder_input", "rejected")
        return b'{"accepted":true}'

    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=lambda: 0.0,
    )

    try:
        assert stat.S_IMODE(session.socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(session.socket_path.stat().st_mode) == 0o600
        with pytest.raises(WorkerProtocolError, match="request"):
            session.close_and_capture()
    finally:
        try:
            session.close_and_capture()
        except WorkerProtocolError:
            pass


class _FakeSocket:
    """A bounded socket fake used through the public client injection seam."""

    def __init__(self, response: bytes) -> None:
        self._response = response
        self.sent = b""
        self.connected_to: Path | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def connect(self, path: Path) -> None:
        self.connected_to = path

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def settimeout(self, _timeout: float) -> None:
        pass

    def recv(self, size: int) -> bytes:
        chunk, self._response = self._response[:size], self._response[size:]
        return chunk


class _FragmentedSocket(_FakeSocket):
    """Return at most one byte per read while retaining socket semantics."""

    def __init__(self, response: bytes) -> None:
        super().__init__(response)
        self.reads = 0

    def recv(self, size: int) -> bytes:
        self.reads += 1
        return super().recv(min(size, 1))


class _ManualClock:
    """Advance broker time explicitly without depending on wall time."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


class _FragmentDeadlineClock(_ManualClock):
    """Expire after the broker starts reading a fragmented header or body."""

    def __init__(self, fragment: str) -> None:
        super().__init__()
        self._fragment = fragment
        self._connection_reads = 0
        self._watch_connection = False
        self.read_started = threading.Event()
        self.expired_read_checked = threading.Event()

    def connection_accepted(self) -> None:
        self._watch_connection = True

    def __call__(self) -> float:
        if self._watch_connection:
            self._connection_reads += 1
            # The first call checks the accepted connection; later calls guard
            # the header and body reads, including each fragmented retry.
            read_start = 2 if self._fragment == "header" else 3
            if self._connection_reads == read_start:
                self.read_started.set()
            elif self._connection_reads == read_start + 1:
                self.advance(2)
                self.expired_read_checked.set()
        return super().__call__()


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, byteorder="big") + payload


@pytest.mark.parametrize(
    "response",
    (
        _frame(b"not JSON"),
        (1_048_577).to_bytes(4, byteorder="big"),
        _frame(b'{"accepted":false,"diagnostic":"builder_input: rejected"}'),
        _frame(b'{"accepted":true,"nonce":"stale"}'),
    ),
)
def test_submit_worker_result_to_broker_rejects_invalid_or_noncanonical_responses(
    response: bytes,
) -> None:
    """Malformed, oversized, rejected, and stale broker responses are terminal."""
    worker = importlib.import_module("agent_work.worker")
    fake_socket = _FakeSocket(response)

    with pytest.raises(WorkerProtocolError):
        worker.submit_worker_result_to_broker(
            Path("/tmp/broker.sock"),
            _request(),
            b'{"workflow_lane":"coding","result_kind":"needs_human",'
            b'"change_summary":"Need review.","next_action":"Wait."}',
            timeout_seconds=10,
            socket_factory=cast(
                Callable[..., socket.socket], lambda *_args: fake_socket
            ),
        )

    assert fake_socket.connected_to == Path("/tmp/broker.sock")


@pytest.mark.parametrize(
    "request_count",
    (0, 3),
)
def test_broker_rejects_zero_or_more_than_two_requests(
    tmp_path: Path, request_count: int
) -> None:
    """One rejection may be corrected once; all other request counts fail closed."""
    worker = importlib.import_module("agent_work.worker")
    attempts: list[bytes] = []

    def build_artifact(input_bytes: bytes) -> bytes:
        attempts.append(input_bytes)
        if len(attempts) == 1:
            raise WorkerProtocolError("builder_input", "rejected")
        return b'{"accepted":true}'

    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=lambda: 0.0,
    )

    request_bytes = (
        b'{"workflow_lane":"coding","result_kind":"needs_human",'
        b'"change_summary":"Need review.","next_action":"Wait."}'
    )
    try:
        if request_count == 0:
            with pytest.raises(WorkerProtocolError):
                session.close_and_capture()
            return
        with pytest.raises(WorkerProtocolError):
            worker.submit_worker_result_to_broker(
                session.socket_path, _request(), request_bytes, timeout_seconds=10
            )
        worker.submit_worker_result_to_broker(
            session.socket_path, _request(), request_bytes, timeout_seconds=10
        )
        with pytest.raises(WorkerProtocolError):
            worker.submit_worker_result_to_broker(
                session.socket_path, _request(), request_bytes, timeout_seconds=10
            )
    finally:
        try:
            session.close_and_capture()
        except WorkerProtocolError:
            pass


def test_broker_rejects_a_request_after_success(tmp_path: Path) -> None:
    """Acceptance is terminal even when a worker tries to send a second result."""
    worker = importlib.import_module("agent_work.worker")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=lambda: 0.0,
    )
    request_bytes = (
        b'{"workflow_lane":"coding","result_kind":"needs_human",'
        b'"change_summary":"Need review.","next_action":"Wait."}'
    )

    try:
        worker.submit_worker_result_to_broker(
            session.socket_path, _request(), request_bytes, timeout_seconds=10
        )
        with pytest.raises(WorkerProtocolError):
            worker.submit_worker_result_to_broker(
                session.socket_path, _request(), request_bytes, timeout_seconds=10
            )
    finally:
        try:
            session.close_and_capture()
        except WorkerProtocolError:
            pass


def test_broker_accepts_one_corrected_request_and_retains_no_worker_prose(
    tmp_path: Path,
) -> None:
    """One canonical rejection returns to the producer before its one correction."""
    worker = importlib.import_module("agent_work.worker")
    submitted: list[bytes] = []

    def build_artifact(input_bytes: bytes) -> bytes:
        submitted.append(input_bytes)
        if len(submitted) == 1:
            raise WorkerProtocolError("builder_input.change_summary", "is required")
        return b'{"accepted":"controller-memory-artifact"}'

    clock = _ManualClock()
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=clock,
    )
    invalid = b'{"workflow_lane":"coding"}'
    corrected = (
        b'{"workflow_lane":"coding","result_kind":"needs_human",'
        b'"change_summary":"Need review.","next_action":"Wait."}'
    )

    try:
        with pytest.raises(WorkerProtocolError, match="builder_input.change_summary"):
            worker.submit_worker_result_to_broker(
                session.socket_path, _request(), invalid, timeout_seconds=10
            )
        clock.advance(9)
        worker.submit_worker_result_to_broker(
            session.socket_path, _request(), corrected, timeout_seconds=10
        )
        assert (
            session.close_and_capture() == b'{"accepted":"controller-memory-artifact"}'
        )
    finally:
        try:
            session.close_and_capture()
        except WorkerProtocolError:
            pass


def test_broker_rejects_correction_after_original_session_deadline(
    tmp_path: Path,
) -> None:
    """A rejected request cannot restart the broker's one session deadline."""
    worker = importlib.import_module("agent_work.worker")
    clock = _ManualClock()
    accepted_correction = threading.Event()
    accepted_count = 0

    def on_connection_accepted() -> None:
        nonlocal accepted_count
        accepted_count += 1
        if accepted_count == 2:
            clock.advance(2)
            accepted_correction.set()

    def build_artifact(_input_bytes: bytes) -> bytes:
        raise WorkerProtocolError("builder_input.change_summary", "is required")

    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=clock,
        connection_accepted=on_connection_accepted,
    )
    try:
        with pytest.raises(WorkerProtocolError, match="builder_input.change_summary"):
            worker.submit_worker_result_to_broker(
                session.socket_path,
                _request(),
                b'{"workflow_lane":"coding"}',
                timeout_seconds=10,
            )
        clock.advance(9)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(session.socket_path))
            client.sendall(
                _frame(
                    json.dumps(
                        {
                            "request": {"nonce": _request().nonce},
                            "input": '{"workflow_lane":"coding","result_kind":"needs_human"}',
                        }
                    ).encode()
                )
            )
            assert accepted_correction.wait(timeout=1)
            session._thread.join(timeout=1)
            assert not session._thread.is_alive()
        with pytest.raises(WorkerProtocolError, match="timed out"):
            session.close_and_capture()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass


@pytest.mark.parametrize("failure", ("deleted", "timeout", "replaced"))
def test_submit_worker_result_to_broker_fails_closed_for_socket_races_and_timeout(
    failure: str,
) -> None:
    """Deletion, replacement, and deadline failures never become worker results."""
    worker = importlib.import_module("agent_work.worker")

    def socket_factory(*_args: object) -> _FakeSocket:
        if failure == "deleted":
            raise FileNotFoundError("deleted socket")
        if failure == "timeout":
            raise TimeoutError("broker deadline")
        raise OSError("replaced socket")

    with pytest.raises(WorkerProtocolError):
        worker.submit_worker_result_to_broker(
            Path("/tmp/broker.sock"),
            _request(),
            b'{"workflow_lane":"coding"}',
            timeout_seconds=10,
            socket_factory=cast(Callable[..., socket.socket], socket_factory),
        )


@pytest.mark.parametrize("partial", ("header", "body"))
def test_broker_enforces_one_deadline_for_partial_production_frames(
    tmp_path: Path, partial: str
) -> None:
    """Every fragmented frame read rechecks the one invocation deadline."""
    worker = importlib.import_module("agent_work.worker")
    accepted = threading.Event()
    clock = _FragmentDeadlineClock(partial)

    def on_connection_accepted() -> None:
        clock.connection_accepted()
        accepted.set()

    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=1,
        temporary_directory=tmp_path,
        clock=clock,
        connection_accepted=on_connection_accepted,
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(session.socket_path))
            assert accepted.wait(timeout=1)
            payload = b'{"request":'
            frame = _frame(payload)
            client.sendall(frame[:2] if partial == "header" else frame[:6])
            assert clock.read_started.wait(timeout=1)
            assert clock.expired_read_checked.wait(timeout=1)
            session._thread.join(timeout=1)
            assert not session._thread.is_alive()
            with pytest.raises(WorkerProtocolError, match="timed out"):
                session.close_and_capture()
        assert not session.socket_path.parent.exists()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass


def test_client_reads_highly_fragmented_frames_once_per_byte() -> None:
    """Fragmented responses use bounded linear accumulation."""
    worker = importlib.import_module("agent_work.worker")
    payload = json.dumps(
        {
            "accepted": True,
            "nonce": _request().nonce,
            "padding": "x" * 16_384,
        }
    ).encode()
    fake_socket = _FragmentedSocket(_frame(payload))

    worker.submit_worker_result_to_broker(
        Path("/tmp/broker.sock"),
        _request(),
        b'{"workflow_lane":"content"}',
        timeout_seconds=10,
        socket_factory=cast(Callable[..., socket.socket], lambda *_args: fake_socket),
    )

    assert fake_socket.reads == len(payload) + 4


def test_broker_close_interrupts_an_active_connection_and_joins_its_thread(
    tmp_path: Path,
) -> None:
    """Closing an invocation deterministically interrupts a stalled accepted client."""
    worker = importlib.import_module("agent_work.worker")
    accepted = threading.Event()
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=30,
        temporary_directory=tmp_path,
        connection_accepted=accepted.set,
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(session.socket_path))
    assert accepted.wait(timeout=1)
    client.sendall(b"\x00\x00")

    with pytest.raises(WorkerProtocolError):
        session.close_and_capture()

    client.close()
    assert not session._thread.is_alive()
    assert not session.socket_path.parent.exists()


def test_live_broker_rejects_a_malformed_request_frame(tmp_path: Path) -> None:
    """The production server retains the exact malformed-request diagnostic."""
    worker = importlib.import_module("agent_work.worker")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(session.socket_path))
            client.sendall(_frame(b"not JSON"))
            client.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
        with pytest.raises(WorkerProtocolError, match="malformed request"):
            session.close_and_capture()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass


def test_live_broker_rejects_an_oversized_request_frame(tmp_path: Path) -> None:
    """The live server reports its exact inbound frame-size rejection."""
    worker = importlib.import_module("agent_work.worker")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(session.socket_path))
            client.sendall((1_048_577).to_bytes(4, byteorder="big"))
            response = json.loads(worker._read_frame(client))
        assert response == {
            "accepted": False,
            "nonce": _request().nonce,
            "diagnostic": "worker_result_broker: frame exceeds 1 MiB",
        }
        with pytest.raises(WorkerProtocolError, match="frame exceeds 1 MiB"):
            session.close_and_capture()
    finally:
        if session.socket_path.parent.exists():
            try:
                session.close_and_capture()
            except WorkerProtocolError:
                pass


def test_broker_eventually_cleans_up_after_blocked_builder_returns(
    tmp_path: Path,
) -> None:
    """A blocked callback cannot orphan the fd-anchored private directory."""
    worker = importlib.import_module("agent_work.worker")
    entered = threading.Event()
    release = threading.Event()

    def blocked_builder(_input: bytes) -> bytes:
        entered.set()
        assert release.wait(timeout=5)
        return b'{"controller":"must-not-be-accepted"}'

    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=blocked_builder,
        timeout_seconds=30,
        temporary_directory=tmp_path,
    )
    private_root = session.socket_path.parent
    client_done = threading.Event()

    def submit() -> None:
        try:
            worker.submit_worker_result_to_broker(
                session.socket_path,
                _request(),
                b'{"workflow_lane":"coding"}',
                timeout_seconds=30,
            )
        except WorkerProtocolError:
            pass
        finally:
            client_done.set()

    client = threading.Thread(target=submit)
    client.start()
    assert entered.wait(timeout=1)

    with pytest.raises(WorkerProtocolError, match="cleanup failed"):
        session.close_and_capture()
    assert private_root.exists()
    assert session._cleanup_thread is not None
    assert session._cleanup_thread.daemon is False

    release.set()
    client.join(timeout=2)
    session._cleanup_thread.join(timeout=2)

    assert client_done.is_set()
    assert not session._thread.is_alive()
    assert not session._cleanup_thread.is_alive()
    assert session._artifact is None
    assert not private_root.exists()


def _record(tmp_path: Path) -> LedgerRecord:
    return LedgerRecord(
        project_id=310967,
        issue_iid=81,
        revision=7,
        state=RunnerState.EXECUTING,
        phase=WorkflowPhase.WORKFLOW,
        workflow_lane=WorkflowLane.CODING,
        work_kind=WorkKind.ORDINARY,
        source_branch="agent/issue-81",
        worktree=tmp_path / "worker-checkout",
        source_sha="a" * 40,
    )


def _result() -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        workflow_lane=WorkflowLane.CODING,
        result_kind="needs_human",
        phase_status="complete",
        completed_phase=WorkflowPhase.WORKFLOW,
        change_summary="A human decision is required.",
        next_action="Await the reviewer decision.",
    )


def test_worker_result_artifact_round_trips_only_in_controller_memory(
    tmp_path: Path,
) -> None:
    """The controller can parse only its freshly built in-memory artifact."""
    worker = importlib.import_module("agent_work.worker")
    artifact = worker.build_worker_result_artifact(
        _record(tmp_path),
        _result(),
        nonce="0123456789abcdef" * 2,
        changed_paths=("tests/test_agent_work_worker_result_broker.py",),
    )

    parsed = worker.parse_worker_result_artifact(
        artifact,
        request=_request(),
        changed_paths=("tests/test_agent_work_worker_result_broker.py",),
    )

    assert parsed == _result()


@pytest.mark.parametrize(
    ("mutation_path", "value"),
    (
        (("nonce",), "f" * 32),
        (("project_id",), 99),
        (("issue_iid",), 99),
        (("ledger_revision",), 99),
        (("source_branch",), "agent/other"),
        (("source_sha",), "b" * 40),
        (("envelope", "expected_state"), "retry_wait"),
        (("envelope", "expected_phase"), "coding_green"),
        (("envelope", "project_id"), 99),
        (("envelope", "issue_iid"), 99),
        (("envelope", "ledger_revision"), 99),
    ),
)
def test_worker_result_artifact_rejects_any_controller_identity_mismatch(
    tmp_path: Path, mutation_path: tuple[str, ...], value: object
) -> None:
    """Post-exit parsing compares every identity field afresh with the request."""
    worker = importlib.import_module("agent_work.worker")
    artifact = worker.build_worker_result_artifact(
        _record(tmp_path),
        _result(),
        nonce="0123456789abcdef" * 2,
        changed_paths=("tests/test_agent_work_worker_result_broker.py",),
    )
    payload = json.loads(artifact)
    target = payload
    for key in mutation_path[:-1]:
        target = target[key]
    target[mutation_path[-1]] = value

    with pytest.raises(WorkerProtocolError):
        worker.parse_worker_result_artifact(
            json.dumps(payload).encode(),
            request=_request(),
            changed_paths=("tests/test_agent_work_worker_result_broker.py",),
        )


def test_worker_result_artifact_rejects_changed_path_mismatch(tmp_path: Path) -> None:
    """Post-exit parsing compares observed paths rather than worker-selected paths."""
    worker = importlib.import_module("agent_work.worker")
    artifact = worker.build_worker_result_artifact(
        _record(tmp_path),
        _result(),
        nonce="0123456789abcdef" * 2,
        changed_paths=("tests/test_agent_work_worker_result_broker.py",),
    )

    with pytest.raises(WorkerProtocolError):
        worker.parse_worker_result_artifact(
            artifact,
            request=_request(),
            changed_paths=("src/agent_work/worker.py",),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (("nonce", "f" * 32), ("issue_iid", 99)),
)
def test_broker_rejects_a_client_request_bound_to_another_invocation(
    tmp_path: Path, field: str, value: object
) -> None:
    """Server-side request checks reject stale nonce and cross-issue submissions."""
    worker = importlib.import_module("agent_work.worker")
    session = worker.create_worker_result_broker(
        _request(),
        build_artifact=lambda _input: b'{"controller":"accepted"}',
        timeout_seconds=10,
        temporary_directory=tmp_path,
        clock=lambda: 0.0,
    )
    mismatched = replace(_request(), **{field: value})

    try:
        with pytest.raises(WorkerProtocolError):
            worker.submit_worker_result_to_broker(
                session.socket_path,
                mismatched,
                b'{"accepted":"model-handcrafted-artifact"}',
                timeout_seconds=10,
            )
    finally:
        try:
            session.close_and_capture()
        except WorkerProtocolError:
            pass
