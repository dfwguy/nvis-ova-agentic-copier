# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Real frontend framing and lifecycle through typed descriptor boundaries."""

from __future__ import annotations

import io
import json
import subprocess
import tomllib
from collections import deque
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_work import worker


class _CloseFailure(io.BufferedRandom):
    def close(self) -> None:
        if self.closed:
            return
        super().close()
        raise OSError("PRIVATE_CLOSE")


class TranscriptProcess:
    """Shared fake Popen boundary; replies only to bytes actually written."""

    def __init__(
        self, root: Path, *, phase: str = "", fault: str = "", partition: int = 37
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.stdin: BinaryIO | None = (root / "stdin").open("w+b")
        self.stdout: BinaryIO | None = (root / "stdout").open("w+b")
        self.stderr: BinaryIO | None = (root / "stderr").open("w+b")
        self.phase = phase
        self.fault = fault
        self.partition = partition
        self.pending = bytearray()
        self.output: deque[bytes] = deque()
        self.errors: deque[bytes] = deque([b"PRIVATE_SERVICE_DIAGNOSTIC\n"])
        self.requests: list[dict[str, object]] = []
        self.stderr_reads: list[int] = []
        self.completed_writes: list[int] = []
        self.delta = "review complete"
        self.specs: list[worker.actor_frontend.ActorProcessSpec] = []
        self.config: dict[str, object] = {}
        self.security: dict[str, object] | None = None
        self.config_reads = 0
        self.now = 0.0
        self.returncode: int | None = None
        self.blocking: list[tuple[int, bool]] = []
        self.write_attempts = 0
        self.read_attempts = 0
        self.waits = 0
        self.terminations = 0
        self.kills = 0
        self.escalate = False
        if fault.startswith("missing:"):
            name = fault.removeprefix("missing:")
            stream = {
                "stdin": self.stdin,
                "stdout": self.stdout,
                "stderr": self.stderr,
            }[name]
            assert stream is not None
            stream.close()
            if name == "stdin":
                self.stdin = None
            elif name == "stdout":
                self.stdout = None
            else:
                self.stderr = None
        if fault == "close":
            assert self.stdin is not None
            self.stdin.close()
            self.stdin = cast(
                BinaryIO, _CloseFailure(io.FileIO(root / "close-pipe", "w+b"))
            )
        if fault == "total":
            self.errors = deque([b"PRIVATE_STDERR_FLOOD" + b"x" * 262145])

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminations += 1
        if not self.escalate:
            self.returncode = -15

    def kill(self) -> None:
        self.kills += 1
        if self.fault == "kill":
            raise OSError("PRIVATE_KILL")
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        self.waits += 1
        if self.fault == "reap":
            raise OSError("PRIVATE_REAP")
        if self.escalate and self.returncode is None and self.waits == 1:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        if self.fault == "cleanup":
            raise subprocess.TimeoutExpired("PRIVATE_COMMAND", timeout)
        self.returncode = 0
        return 0

    def clock(self) -> float:
        return self.now

    def open_process(
        self, spec: worker.actor_frontend.ActorProcessSpec
    ) -> worker.actor_frontend.ActorRawProcess:
        if self.fault == "spawn":
            raise OSError("PRIVATE_SPAWN")
        self.specs.append(spec)
        assignments = [
            spec.argv[index + 1]
            for index, token in enumerate(spec.argv[:-1])
            if token == "-c"
        ]
        selected = tomllib.loads("\n".join(assignments))
        self.config = selected
        if self.security is not None:
            self.config = json.loads(json.dumps(self.security))
            self.config["default_permissions"] = selected["default_permissions"]
            self.config["permissions"] = {
                str(selected["default_permissions"]): self.security["permissions"]
            }
        return self

    def ready(
        self, readable: tuple[int, ...], writable: tuple[int, ...], timeout: float
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        assert timeout >= 0
        self.now += 0.001
        ready = tuple(
            descriptor
            for descriptor in readable
            if (
                self.stdout is not None
                and descriptor == self.stdout.fileno()
                and self.output
            )
            or (
                self.stderr is not None
                and descriptor == self.stderr.fileno()
                and self.errors
            )
        )
        return ready, writable

    def set_blocking(self, descriptor: int, blocking: bool) -> None:
        self.blocking.append((descriptor, blocking))
        assert blocking is False

    def read(self, descriptor: int, maximum: int) -> bytes:
        self.read_attempts += 1
        if self.fault == "retry_read" and self.read_attempts == 1:
            raise BlockingIOError
        assert maximum > 0
        if self.stderr is not None and descriptor == self.stderr.fileno():
            source = self.errors
            self.stderr_reads.append(self.write_attempts)
        else:
            source = self.output
        if self.fault == "read":
            raise OSError("PRIVATE_READ")
        if not source:
            return b""
        data = source.popleft()
        count = min(maximum, self.partition)
        if len(data) > count:
            source.appendleft(data[count:])
        return data[:count]

    def write(self, descriptor: int, payload: bytes) -> int:
        self.write_attempts += 1
        if self.fault == "retry_write" and self.write_attempts == 1:
            raise BlockingIOError
        assert self.stdin is not None and descriptor == self.stdin.fileno()
        if self.fault == "write":
            raise BrokenPipeError("PRIVATE_WRITE")
        if self.fault == "zero_write":
            return 0
        count = min(len(payload), self.partition)
        self.pending.extend(payload[:count])
        while b"\n" in self.pending:
            line, _, remainder = self.pending.partition(b"\n")
            self.pending = bytearray(remainder)
            message = cast(dict[str, object], json.loads(line))
            self.requests.append(message)
            self.completed_writes.append(self.write_attempts)
            if "id" in message:
                self.respond(message)
        return count

    def emit(self, value: dict[str, object]) -> None:
        self.output.append(json.dumps(value, ensure_ascii=True).encode() + b"\n")

    def respond(self, message: dict[str, object]) -> None:
        method = message["method"]
        phase = str(method)
        if method == "config/read":
            self.config_reads += 1
            phase = "recheck" if self.config_reads > 1 else "config/read"
        if phase == self.phase and self.fault in (
            "malformed",
            "unicode",
            "boolean_id",
            "wrong_id",
            "eof",
            "timeout",
            "server_request",
            "frame",
            "partial",
        ):
            if self.fault == "frame":
                self.output.append(b"x" * 32769 + b"\n")
            elif self.fault == "partial":
                self.output.extend((b'{"id":', b""))
            elif self.fault == "malformed":
                self.output.append(b"{invalid}\n")
            elif self.fault == "unicode":
                self.output.append(b"\xff\n")
            elif self.fault == "eof":
                self.output.append(b"")
            elif self.fault == "timeout":
                self.now = 20.0
            elif self.fault == "server_request":
                self.emit(
                    {
                        "id": 77,
                        "method": "item/commandExecution/requestApproval",
                        "params": {"command": "PRIVATE_COMMAND"},
                    }
                )
            else:
                self.emit(
                    {
                        "id": True
                        if self.fault == "boolean_id"
                        else cast(int, message["id"]) + 100,
                        "result": {},
                    }
                )
            return
        if method == "initialize":
            result: dict[str, object] = {
                "userAgent": "synthetic",
                "platformFamily": "unix",
                "platformOs": "linux",
            }
        elif method == "config/read":
            config = json.loads(json.dumps(self.config))
            if self.fault.startswith("config:") and phase == self.phase:
                fault = self.fault.removeprefix("config:")
                if fault == "feature":
                    config["features"]["unified_exec"] = True
                elif fault == "foreign_feature":
                    config["features"]["web_search"] = True
                elif fault == "missing":
                    del config["permissions"]
                elif fault == "legacy":
                    config["sandbox_mode"] = "danger-full-access"
                elif fault == "extra_grant":
                    config["permissions"][config["default_permissions"]]["filesystem"][
                        "/extra"
                    ] = "write"
                elif fault == "tools":
                    config["mcp_servers"]["candidate"]["enabled_tools"].append("shell")
                elif fault == "server":
                    config["mcp_servers"]["foreign"] = config["mcp_servers"][
                        "candidate"
                    ]
                elif fault == "network":
                    config["permissions"][config["default_permissions"]]["network"][
                        "enabled"
                    ] = True
                elif fault == "plugin":
                    config["plugins"] = {"browser": {"enabled": True}}
            origin = {"name": {"type": "sessionFlags"}, "version": "fixture"}
            result = {
                "config": config,
                "layers": [{**origin, "config": config, "disabledReason": None}],
                "origins": {key: origin for key in config},
            }
        elif method == "thread/start":
            spec = self.specs[-1]
            result = {
                "thread": {
                    "id": "thread-fixture",
                    "sessionId": "session-fixture",
                    "cwd": str(spec.cwd),
                },
                "activePermissionProfile": {
                    "id": self.config["default_permissions"],
                    "extends": None,
                },
                "approvalPolicy": "never",
                "approvalsReviewer": "user",
                "cwd": str(spec.cwd),
                "model": "fixture",
                "modelProvider": "fixture",
                "runtimeWorkspaceRoots": [str(spec.cwd)],
                "sandbox": {"type": "readOnly"},
            }
            result = cast(dict[str, object], result)
            if self.fault.startswith("thread:"):
                fault = self.fault.removeprefix("thread:")
                if fault == "profile":
                    result["activePermissionProfile"] = {
                        "id": "foreign",
                        "extends": None,
                    }
                elif fault == "extends":
                    result["activePermissionProfile"] = {
                        "id": self.config["default_permissions"],
                        "extends": "foreign",
                    }
                elif fault == "cwd":
                    result["cwd"] = str(spec.cwd / "foreign")
                elif fault == "roots":
                    result["runtimeWorkspaceRoots"] = ["/foreign"]
                elif fault == "null_profile":
                    result["activePermissionProfile"] = None
                elif fault == "missing_profile":
                    del result["activePermissionProfile"]
                else:
                    result["approvalPolicy"] = "on-request"
        else:
            result = {
                "turn": {
                    "id": "turn-fixture",
                    "status": "inProgress",
                    "items": [],
                    "error": None,
                }
            }
        if self.fault == "duplicate" and phase == self.phase:
            identifier = json.dumps(message["id"])
            payload = json.dumps(result, ensure_ascii=True)
            self.output.append(
                f'{{"id":{identifier},"id":{identifier},"result":{payload}}}\n'.encode()
            )
            return
        self.emit({"id": message["id"], "result": result})
        if self.fault == "duplicate_response" and phase == self.phase:
            self.emit({"id": message["id"], "result": result})
        if method == "turn/start":
            self.emit(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "itemId": "message-fixture",
                        "delta": self.delta,
                    },
                }
            )
            self.emit(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "foreign"
                        if self.fault == "foreign_thread"
                        else "thread-fixture",
                        "turn": {
                            "id": "foreign"
                            if self.fault == "foreign_turn"
                            else "turn-fixture",
                            "status": self.fault
                            if self.fault in ("failed", "interrupted")
                            else "completed",
                            "items": [],
                            "error": None,
                        },
                    },
                }
            )

    def ports(self) -> worker.actor_frontend.ActorProcessPorts:
        return worker.actor_frontend.ActorProcessPorts(
            open_process=self.open_process,
            set_blocking=self.set_blocking,
            wait_ready=self.ready,
            read_bytes=self.read,
            write_bytes=self.write,
            clock=self.clock,
        )

    def dispose(self) -> None:
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def limits() -> worker.actor_frontend.ActorTransportLimits:
    """Synthetic explicit host caps; never a production default or approval."""
    return worker.actor_frontend.ActorTransportLimits(
        max_frame_bytes=32768,
        max_total_bytes=262144,
        max_output_bytes=4096,
        cleanup_timeout_seconds=1.0,
    )


def _spec(root: Path) -> worker.actor_frontend.ActorProcessSpec:
    return worker.actor_frontend.ActorProcessSpec(
        argv=(
            "/fixture/codex",
            "app-server",
            "--stdio",
            "--strict-config",
            "-c",
            'default_permissions="fixture-profile"',
        ),
        environment=(),
        cwd=root,
        deadline=10.0,
    )


@settings(max_examples=6)
@given(partition=st.integers(min_value=1, max_value=91))
def test_generated_fragmentation_preserves_actual_request_id_and_response(
    tmp_path_factory: pytest.TempPathFactory, partition: int
) -> None:
    root = tmp_path_factory.mktemp("front")
    process = TranscriptProcess(root, partition=partition)
    try:
        factory = worker.actor_frontend.create_stdio_actor_frontend(process.ports())
        with factory(_spec(root), limits()) as session:
            result = session.request(
                "initialize",
                {
                    "clientInfo": {"name": "fixture", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            session.notify_initialized()
            assert result["userAgent"] == "synthetic"
        assert [message["method"] for message in process.requests] == [
            "initialize",
            "initialized",
        ]
        assert len(process.blocking) == 3 and all(
            not blocking for _, blocking in process.blocking
        )
        assert process.waits > 0
        assert all(
            stream is not None and stream.closed
            for stream in (process.stdin, process.stdout, process.stderr)
        )
    finally:
        process.dispose()


@pytest.mark.parametrize(
    "phase", ["initialize", "config/read", "thread/start", "turn/start"]
)
@settings(max_examples=6)
@given(
    fault=st.sampled_from(
        (
            "malformed",
            "duplicate",
            "unicode",
            "boolean_id",
            "wrong_id",
            "server_request",
        )
    )
)
def test_generated_each_phase_protocol_fault_has_fixed_safe_refusal(
    tmp_path_factory: pytest.TempPathFactory, phase: str, fault: str
) -> None:
    root = tmp_path_factory.mktemp("fault")
    control = TranscriptProcess(root / "control")
    process = TranscriptProcess(root / "subject", phase=phase, fault=fault)
    methods = ("initialize", "config/read", "thread/start", "turn/start")
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(control.ports())(
            _spec(root), limits()
        ) as session:
            for method in methods[: methods.index(phase) + 1]:
                assert session.request(
                    method,
                    {},
                )
        with (
            pytest.raises(worker.actor_frontend.ActorHostError) as caught,
            worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                _spec(root), limits()
            ) as session,
        ):
            for method in methods[: methods.index(phase) + 1]:
                session.request(
                    method,
                    {},
                )
        assert caught.value.reason == "actor_protocol_invalid"
        assert "PRIVATE" not in str(caught.value)
        assert (
            all(message.get("method") != "turn/start" for message in process.requests)
            if phase != "turn/start"
            else True
        )
    finally:
        control.dispose()
        process.dispose()


class StartupTranscript(TranscriptProcess):
    """Inject wire frames at the existing OS boundary after real writes."""

    def __init__(
        self,
        root: Path,
        *,
        trigger: str = "config/read",
        position: str = "before",
        frames: tuple[bytes, ...] = (),
        partition: int = 32768,
        acknowledgment: str = "",
        timestamp: str = "omitted",
        fault: str = "",
        occurrence: int = 1,
        coalesced: bool = True,
    ) -> None:
        super().__init__(root, partition=partition, fault=fault)
        self.trigger = trigger
        self.position = position
        self.frames = frames
        self.acknowledgment = acknowledgment
        self.timestamp = timestamp
        self.sent = bytearray()
        self.received = bytearray()
        self.delay = 0.0
        self.occurrence = occurrence
        self.matches = 0
        self.coalesced = coalesced

    def write(self, descriptor: int, payload: bytes) -> int:
        previous = len(self.requests)
        count = super().write(descriptor, payload)
        self.sent.extend(payload[:count])
        for message in self.requests[previous:]:
            if message["method"] == "initialized" and self.trigger == "initialized":
                self.output.extend(self.frames)
        return count

    def read(self, descriptor: int, maximum: int) -> bytes:
        value = super().read(descriptor, maximum)
        if self.stdout is not None and descriptor == self.stdout.fileno():
            self.received.extend(value)
            self.now += self.delay
        return value

    def respond(self, message: dict[str, object]) -> None:
        previous = len(self.output)
        super().respond(message)
        if message["method"] != self.trigger:
            return
        self.matches += 1
        if self.matches != self.occurrence:
            return
        replies = tuple(self.output)[previous:]
        for _ in replies:
            self.output.pop()
        frames = self.frames
        if self.acknowledgment:
            response = cast(dict[str, object], json.loads(replies[0]))
            result = cast(dict[str, object], response["result"])
            method = str(message["method"])
            key = "thread" if method == "thread/start" else "turn"
            params: dict[str, object] = {key: result[key]}
            if key == "turn":
                params["threadId"] = cast(dict[str, object], message["params"])[
                    "threadId"
                ]
            ack: dict[str, object] = {
                "method": "thread/started" if key == "thread" else "turn/started",
                "params": params,
            }
            if self.timestamp != "omitted":
                ack["emittedAtMs"] = json.loads(self.timestamp)
            if self.acknowledgment == "mismatch":
                params[key] = {"id": "PRIVATE_FOREIGN"}
            elif self.acknowledgment == "nested":
                params[key] = {**cast(dict[str, object], result[key]), "extra": {}}
            elif self.acknowledgment == "thread_id":
                params["threadId"] = "PRIVATE_FOREIGN"
            elif self.acknowledgment == "envelope":
                ack["extra"] = "PRIVATE_EXTRA"
            elif self.acknowledgment == "params":
                params["extra"] = "PRIVATE_EXTRA"
            frames = (_wire(ack),)
            if self.acknowledgment == "duplicate":
                frames *= 2
        ordered = frames + replies if self.position == "before" else replies + frames
        # One deque entry gives a genuinely coalesced read when partition permits.
        self.output.extend((b"".join(ordered),) if self.coalesced else ordered)


def _wire(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True).encode() + b"\n"


def _remote_frame(mutation: str = "valid", timestamp: str = "omitted") -> bytes:
    params: dict[str, object] = {
        "status": "disabled",
        "environmentId": None,
        "installationId": "PRIVATE_INSTALLATION",
        "serverName": "PRIVATE_SERVER",
    }
    event: dict[str, object] = {
        "method": "remoteControl/status/changed",
        "params": params,
    }
    if timestamp != "omitted":
        event["emittedAtMs"] = json.loads(timestamp)
    if mutation.startswith("status:"):
        params["status"] = mutation.removeprefix("status:")
    elif mutation.startswith("missing:"):
        del params[mutation.removeprefix("missing:")]
    elif mutation.startswith("null:"):
        params[mutation.removeprefix("null:")] = None
    elif mutation.startswith("empty:"):
        params[mutation.removeprefix("empty:")] = ""
    elif mutation.startswith("type:"):
        params[mutation.removeprefix("type:")] = True
    elif mutation == "environment":
        params["environmentId"] = "PRIVATE_ENVIRONMENT"
    elif mutation == "nested":
        params["installationId"] = {"value": "PRIVATE_INSTALLATION"}
    elif mutation == "whitespace":
        params["installationId"] = " "
        params["serverName"] = "\t"
    elif mutation == "params_extra":
        params["extra"] = "PRIVATE_EXTRA"
    elif mutation in ("id", "jsonrpc", "extra"):
        event[mutation] = "PRIVATE_EXTRA"
    elif mutation == "unknown":
        event["method"] = "remoteControl/unknown"
    payload = _wire(event)
    if mutation == "duplicate_envelope":
        payload = payload.replace(b'{"method":', b'{"method":"duplicate","method":', 1)
    elif mutation == "duplicate_params":
        payload = payload.replace(b'{"status":', b'{"status":"disabled","status":', 1)
    elif mutation == "duplicate_nested":
        payload = payload.replace(
            b'"PRIVATE_INSTALLATION"', b'{"value":1,"value":2}', 1
        )
    return payload


def _startup(session: worker.actor_frontend.ActorSessionPort) -> None:
    assert session.request("initialize", {})["userAgent"] == "synthetic"
    session.notify_initialized()


def _assert_closed(process: TranscriptProcess) -> None:
    assert process.waits > 0
    assert all(
        stream is not None and stream.closed
        for stream in (process.stdin, process.stdout, process.stderr)
    )


@pytest.mark.parametrize(
    ("trigger", "position", "timestamp"),
    [
        ("initialize", "after", "omitted"),
        ("initialize", "after", "0"),
        ("initialized", "before", "17"),
        ("config/read", "before", "omitted"),
        ("config/read", "before", "0"),
        ("config/read", "before", "17"),
    ],
)
@settings(max_examples=4)
@given(partition=st.one_of(st.integers(min_value=1, max_value=91), st.just(32768)))
def test_disabled_startup_notification_preserves_correlated_response(
    tmp_path_factory: pytest.TempPathFactory,
    trigger: str,
    position: str,
    timestamp: str,
    partition: int,
) -> None:
    root = tmp_path_factory.mktemp("startup")
    process = StartupTranscript(
        root,
        trigger=trigger,
        position=position,
        frames=(_remote_frame(timestamp=timestamp),),
        partition=partition,
    )
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
            _spec(root), limits()
        ) as session:
            _startup(session)
            result = session.request("config/read", {})
            assert cast(dict[str, object], result["config"])["default_permissions"] == (
                "fixture-profile"
            )
        assert [message["method"] for message in process.requests] == [
            "initialize",
            "initialized",
            "config/read",
        ]
        assert [message["id"] for message in process.requests if "id" in message] == [
            1,
            2,
        ]
        _assert_closed(process)
    finally:
        process.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "status:connected",
        "status:connecting",
        "status:errored",
        "status:unknown",
        "missing:status",
        "missing:environmentId",
        "missing:installationId",
        "missing:serverName",
        "null:status",
        "null:installationId",
        "null:serverName",
        "empty:installationId",
        "empty:serverName",
        "type:status",
        "type:installationId",
        "type:serverName",
        "environment",
        "nested",
        "params_extra",
        "id",
        "jsonrpc",
        "extra",
        "unknown",
        "duplicate_envelope",
        "duplicate_params",
        "duplicate_nested",
    ],
)
@settings(max_examples=3)
@given(partition=st.sampled_from((1, 37, 32768)))
def test_remote_status_refuses_before_outbound_progression(
    tmp_path_factory: pytest.TempPathFactory, mutation: str, partition: int
) -> None:
    root = tmp_path_factory.mktemp("remote-fault")
    control = StartupTranscript(root / "control", frames=(_remote_frame(),))
    subject = StartupTranscript(
        root / "subject", frames=(_remote_frame(mutation),), partition=partition
    )
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(control.ports())(
            _spec(root), limits()
        ) as session:
            _startup(session)
            assert session.request("config/read", {})
        with (
            pytest.raises(worker.actor_frontend.ActorHostError) as caught,
            worker.actor_frontend.create_stdio_actor_frontend(subject.ports())(
                _spec(root), limits()
            ) as session,
        ):
            _startup(session)
            session.request("config/read", {})
            session.request("turn/start", {"threadId": "thread-fixture"})
        assert caught.value.reason == "actor_protocol_invalid"
        assert "PRIVATE" not in str(caught.value)
        assert b'"turn/start"' not in subject.sent
        _assert_closed(subject)
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    "timestamp", ["null", "true", "1.5", '"1"', "-1", "NaN", "Infinity"]
)
def test_remote_timestamp_refuses_strict_invalid_values(
    tmp_path: Path, timestamp: str
) -> None:
    control = StartupTranscript(
        tmp_path / "control", frames=(_remote_frame(timestamp="0"),)
    )
    subject = StartupTranscript(
        tmp_path / "subject", frames=(_remote_frame(timestamp=timestamp),)
    )
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(control.ports())(
            _spec(tmp_path), limits()
        ) as session:
            _startup(session)
            assert session.request("config/read", {})
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(subject.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            _startup(session)
            session.request("config/read", {})
        _assert_closed(subject)
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    ("trigger", "position", "occurrence", "actions"),
    [
        ("initialize", "before", 1, ("initialize",)),
        ("config/read", "before", 1, ("config/read",)),
        ("config/read", "after", 1, ("initialize", "config/read", "turn/start")),
        ("thread/start", "before", 1, ("initialize", "thread/start")),
        ("turn/start", "before", 1, ("initialize", "turn/start")),
        ("config/read", "before", 1, ("initialize", "thread/start", "config/read")),
        ("config/read", "before", 1, ("initialize", "initialize", "config/read")),
        ("config/read", "before", 2, ("config/read", "initialize", "config/read")),
        ("config/read", "before", 2, ("initialize", "config/read", "config/read")),
        (
            "initialize",
            "after",
            2,
            ("initialize", "config/read", "initialize", "config/read"),
        ),
        ("config/read", "after", 1, ("initialize", "config/read", "event")),
    ],
)
def test_remote_notification_window_is_finite_and_never_reopens(
    tmp_path: Path,
    trigger: str,
    position: str,
    occurrence: int,
    actions: tuple[str, ...],
) -> None:
    process = StartupTranscript(
        tmp_path,
        trigger=trigger,
        position=position,
        occurrence=occurrence,
        frames=(_remote_frame(),),
    )
    try:
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            for action in actions:
                if action == "event":
                    session.next_event()
                    continue
                session.request(
                    cast(worker.actor_frontend.ActorMethod, action),
                    {"threadId": "thread-fixture"},
                )
        if position == "after":
            assert b'"turn/start"' not in process.sent
        _assert_closed(process)
    finally:
        process.dispose()


@pytest.mark.parametrize("mutation", ["valid", "status:connected", "params_extra"])
@pytest.mark.parametrize("trigger", ["config/read", "initialize"])
def test_received_remote_tail_refuses_before_any_later_write(
    tmp_path: Path, mutation: str, trigger: str
) -> None:
    frame = _remote_frame(mutation)
    process = StartupTranscript(
        tmp_path, trigger=trigger, position="after", frames=(frame,)
    )
    try:
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            session.request("initialize", {})
            if trigger == "config/read":
                session.notify_initialized()
                session.request("config/read", {})
            # A valid initialized tail is inert only until this request closes the window.
            session.request("turn/start", {"threadId": "thread-fixture"})
        assert frame in process.received
        assert b'"turn/start"' not in process.sent
        assert not process.pending
        _assert_closed(process)
    finally:
        process.dispose()


@pytest.mark.parametrize("method", ["thread/start", "turn/start"])
@pytest.mark.parametrize("timestamp", ["omitted", "0", "17"])
def test_timestamped_started_ack_preserves_exact_correlation(
    tmp_path: Path, method: str, timestamp: str
) -> None:
    process = StartupTranscript(
        tmp_path, trigger=method, acknowledgment="valid", timestamp=timestamp
    )
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
            _spec(tmp_path), limits()
        ) as session:
            _startup(session)
            result = session.request(
                cast(worker.actor_frontend.ActorMethod, method),
                {"threadId": "thread-fixture"},
            )
            key = "thread" if method == "thread/start" else "turn"
            identity = cast(dict[str, object], result[key])
            assert identity == (
                {
                    "id": "thread-fixture",
                    "sessionId": "session-fixture",
                    "cwd": str(tmp_path),
                }
                if key == "thread"
                else {
                    "id": "turn-fixture",
                    "status": "inProgress",
                    "items": [],
                    "error": None,
                }
            )
        _assert_closed(process)
    finally:
        process.dispose()


@pytest.mark.parametrize("method", ["thread/start", "turn/start"])
@pytest.mark.parametrize(
    ("mutation", "timestamp"),
    [
        ("valid", value)
        for value in ("null", "true", "1.5", '"1"', "-1", "NaN", "Infinity")
    ]
    + [
        (value, "omitted")
        for value in ("duplicate", "mismatch", "nested", "envelope", "params")
    ],
)
def test_started_ack_invalid_timestamp_or_payload_refuses(
    tmp_path: Path, method: str, mutation: str, timestamp: str
) -> None:
    control = StartupTranscript(
        tmp_path / "control", trigger=method, acknowledgment="valid"
    )
    subject = StartupTranscript(
        tmp_path / "subject",
        trigger=method,
        acknowledgment=mutation,
        timestamp=timestamp,
    )
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(control.ports())(
            _spec(tmp_path), limits()
        ) as session:
            _startup(session)
            assert session.request(
                cast(worker.actor_frontend.ActorMethod, method),
                {"threadId": "thread-fixture"},
            )
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(subject.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            _startup(session)
            session.request(
                cast(worker.actor_frontend.ActorMethod, method),
                {"threadId": "thread-fixture"},
            )
        _assert_closed(subject)
    finally:
        control.dispose()
        subject.dispose()


def test_turn_started_ack_wrong_thread_refuses(tmp_path: Path) -> None:
    process = StartupTranscript(
        tmp_path, trigger="turn/start", acknowledgment="thread_id"
    )
    try:
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            session.request("turn/start", {"threadId": "thread-fixture"})
        _assert_closed(process)
    finally:
        process.dispose()


@pytest.mark.parametrize("fault", ["total", "deadline", "cleanup", "valid"])
def test_startup_notifications_consume_existing_bounds(
    tmp_path: Path, fault: str
) -> None:
    process = StartupTranscript(
        tmp_path,
        frames=(
            _remote_frame("status:connected" if fault == "cleanup" else "whitespace"),
        )
        * (2000 if fault == "total" else 3),
        fault="close" if fault == "cleanup" else "",
        coalesced=False,
    )
    try:
        if fault == "valid":
            with worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                _spec(tmp_path), limits()
            ) as session:
                _startup(session)
                assert session.request("config/read", {})
        else:
            reason = {
                "total": "actor_protocol_invalid",
                "deadline": "actor_timeout",
                "cleanup": "actor_cleanup_failed",
            }[fault]
            with (
                pytest.raises(
                    worker.actor_frontend.ActorHostError, match="^" + reason + "$"
                ),
                worker.actor_frontend.create_stdio_actor_frontend(process.ports())(
                    _spec(tmp_path), limits()
                ) as session,
            ):
                _startup(session)
                if fault == "deadline":
                    process.delay = 4.0
                session.request("config/read", {})
        _assert_closed(process)
    finally:
        process.dispose()


class WritablePumpTranscript(StartupTranscript):
    """Expose one frame only when the armed stdin pump asks for readiness."""

    def __init__(self, root: Path, *, frame: bytes, release_pump: int) -> None:
        super().__init__(root)
        self.frame = frame
        self.release_pump = release_pump
        self.armed = False
        self.pumps = 0
        self.release_sent: bytes | None = None

    def ready(
        self, readable: tuple[int, ...], writable: tuple[int, ...], timeout: float
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if self.armed and writable:
            self.pumps += 1
            if self.pumps == self.release_pump:
                self.release_sent = bytes(self.sent)
                if self.frame:
                    self.output.append(self.frame)
        return super().ready(readable, writable, timeout)

    def write(self, descriptor: int, payload: bytes) -> int:
        if self.armed and self.release_pump == 2:
            payload = payload[:7]
        return super().write(descriptor, payload)


@pytest.mark.parametrize("method", ["thread/start", "turn/start"])
@pytest.mark.parametrize("release_pump", [1, 2])
def test_writable_pump_remote_receipt_prevents_additional_request_bytes(
    tmp_path: Path, method: str, release_pump: int
) -> None:
    frame = _remote_frame()
    control = WritablePumpTranscript(
        tmp_path / "control", frame=b"", release_pump=release_pump
    )
    subject = WritablePumpTranscript(
        tmp_path / "subject", frame=frame, release_pump=release_pump
    )
    parameters: dict[str, object] = {"threadId": "thread-fixture"}
    try:
        with worker.actor_frontend.create_stdio_actor_frontend(control.ports())(
            _spec(tmp_path), limits()
        ) as session:
            _startup(session)
            control.armed = True
            result = session.request(
                cast(worker.actor_frontend.ActorMethod, method), parameters
            )
            key = "thread" if method == "thread/start" else "turn"
            assert cast(dict[str, object], result[key])["id"] == (
                "thread-fixture" if key == "thread" else "turn-fixture"
            )
        assert control.release_sent is not None
        assert control.requests[-1]["method"] == method
        _assert_closed(control)
        before = b""
        with (
            pytest.raises(
                worker.actor_frontend.ActorHostError, match="^actor_protocol_invalid$"
            ),
            worker.actor_frontend.create_stdio_actor_frontend(subject.ports())(
                _spec(tmp_path), limits()
            ) as session,
        ):
            _startup(session)
            before = bytes(subject.sent)
            assert frame not in subject.received
            subject.armed = True
            session.request(cast(worker.actor_frontend.ActorMethod, method), parameters)
        assert frame in subject.received
        assert subject.release_sent is not None
        assert bytes(subject.sent) == subject.release_sent
        expected_prefix = (
            b""
            if release_pump == 1
            else _wire({"id": 2, "method": method, "params": parameters})[:7]
        )
        assert bytes(subject.sent) == before + expected_prefix
        assert bytes(subject.pending) == expected_prefix
        assert [message["method"] for message in subject.requests] == [
            "initialize",
            "initialized",
        ]
        _assert_closed(subject)
    finally:
        control.dispose()
        subject.dispose()
