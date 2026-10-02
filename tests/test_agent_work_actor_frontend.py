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
