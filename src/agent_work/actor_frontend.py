# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Owned, deadline-bound binary JSON-RPC transport for an optional actor."""

from __future__ import annotations

import json
import math
import os
import select
import subprocess
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import BinaryIO, Literal, Protocol, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

ActorReason = Literal[
    "actor_limits_unavailable",
    "actor_limits_invalid",
    "actor_plan_invalid",
    "actor_features_unavailable",
    "actor_privacy_unavailable",
    "actor_frontend_unavailable",
    "actor_protocol_invalid",
    "actor_config_unverified",
    "actor_thread_unverified",
    "actor_identity_changed",
    "actor_turn_failed",
    "actor_timeout",
    "actor_cleanup_failed",
]
ActorMethod = Literal["initialize", "config/read", "thread/start", "turn/start"]


class ActorHostError(RuntimeError):
    """Expose only a closed stage reason, never external diagnostic text."""

    def __init__(self, reason: ActorReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ActorTransportLimits:
    """Explicit trusted host limits; no operational policy defaults."""

    max_frame_bytes: int
    max_total_bytes: int
    max_output_bytes: int
    cleanup_timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value <= 0
                for value in (
                    self.max_frame_bytes,
                    self.max_total_bytes,
                    self.max_output_bytes,
                )
            )
            or self.max_frame_bytes > self.max_total_bytes
            or self.max_output_bytes > self.max_total_bytes
            or isinstance(self.cleanup_timeout_seconds, bool)
            or not isinstance(self.cleanup_timeout_seconds, (int, float))
            or not math.isfinite(self.cleanup_timeout_seconds)
            or self.cleanup_timeout_seconds <= 0
        ):
            raise ActorHostError("actor_limits_invalid")


@dataclass(frozen=True)
class ActorProcessSpec:
    """Controller-bound launch recipe, environment, location and deadline."""

    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    cwd: Path
    deadline: float


class ActorRawProcess(Protocol):
    """The owned Popen boundary, retained until close and reap."""

    stdin: BinaryIO | None
    stdout: BinaryIO | None
    stderr: BinaryIO | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float) -> int: ...


@dataclass(frozen=True)
class ActorProcessPorts:
    """Replace only process, descriptor, readiness and clock boundaries."""

    open_process: Callable[[ActorProcessSpec], ActorRawProcess]
    wait_ready: Callable[
        [tuple[int, ...], tuple[int, ...], float],
        tuple[tuple[int, ...], tuple[int, ...]],
    ]
    read_bytes: Callable[[int, int], bytes]
    write_bytes: Callable[[int, bytes], int]
    clock: Callable[[], float]
    set_blocking: Callable[[int, bool], None]


class ActorSessionPort(Protocol):
    """Closed request methods and notifications from the same live session."""

    def request(
        self, method: ActorMethod, parameters: Mapping[str, object]
    ) -> Mapping[str, object]: ...
    def notify_initialized(self) -> None: ...
    def next_event(self) -> Mapping[str, object]: ...


class ActorFrontendFactory(Protocol):
    """Own the entire process lifetime for exactly one actor session."""

    def __call__(
        self, spec: ActorProcessSpec, limits: ActorTransportLimits
    ) -> AbstractContextManager[ActorSessionPort]: ...


class ActorRpcError(BaseModel):
    """Strict external RPC error whose contents remain private."""

    model_config = ConfigDict(extra="forbid", strict=True)
    code: StrictInt
    message: StrictStr
    data: object | None = None


class ActorRpcResponse(BaseModel):
    """A host-correlated response with exactly one result or error."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: StrictInt
    result: dict[str, object] | None = None
    error: ActorRpcError | None = None
    jsonrpc: Literal["2.0"] | None = None

    @model_validator(mode="after")
    def _one_payload(self) -> ActorRpcResponse:
        if (self.result is None) == (self.error is None):
            raise ValueError("invalid RPC response")
        return self


class _ThreadStartedParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    thread: dict[str, object]


class _TurnStartedParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    threadId: StrictStr
    turn: dict[str, object]


class _TimestampedNotification(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    emittedAtMs: StrictInt | None = Field(default=None, ge=0)

    @field_validator("emittedAtMs", mode="before")
    @classmethod
    def _present_timestamp(cls, value: object) -> object:
        if value is None:
            raise ValueError("invalid notification timestamp")
        return value


class _RemoteControlStatusParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["disabled"]
    environmentId: None
    installationId: StrictStr = Field(min_length=1)
    serverName: StrictStr = Field(min_length=1)


class _RemoteControlStatusChanged(_TimestampedNotification):
    method: Literal["remoteControl/status/changed"]
    params: _RemoteControlStatusParameters


class _ThreadStarted(_TimestampedNotification):
    method: Literal["thread/started"]
    params: _ThreadStartedParameters


class _TurnStarted(_TimestampedNotification):
    method: Literal["turn/started"]
    params: _TurnStartedParameters


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, value in pairs:
        if name in values:
            raise ValueError("duplicate protocol field")
        values[name] = value
    return values


class _Session:
    def __init__(
        self,
        process: ActorRawProcess,
        spec: ActorProcessSpec,
        limits: ActorTransportLimits,
        ports: ActorProcessPorts,
    ) -> None:
        self.process = process
        self.spec = spec
        self.limits = limits
        self.ports = ports
        self.identifier = 0
        self.startup_phase: Literal["awaiting_initialize", "open", "closed"] = (
            "awaiting_initialize"
        )
        self.buffer = bytearray()
        self.events: deque[dict[str, object]] = deque()
        self.total = 0
        self.stderr_open = True
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ActorHostError("actor_frontend_unavailable")
        self.input_fd = process.stdin.fileno()
        self.output_fd = process.stdout.fileno()
        self.error_fd = process.stderr.fileno()
        if len({self.input_fd, self.output_fd, self.error_fd}) != 3:
            raise ActorHostError("actor_frontend_unavailable")
        for descriptor in (self.input_fd, self.output_fd, self.error_fd):
            ports.set_blocking(descriptor, False)

    def _remaining(self) -> float:
        remaining = self.spec.deadline - self.ports.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise ActorHostError("actor_timeout")
        return remaining

    def _account(self, count: int) -> None:
        self.total += count
        if self.total > self.limits.max_total_bytes:
            raise ActorHostError("actor_protocol_invalid")

    def _pump(self, *, writable: bool = False) -> bool:
        readable = (
            (self.output_fd, self.error_fd) if self.stderr_open else (self.output_fd,)
        )
        writes = (self.input_fd,) if writable else ()
        ready, write_ready = self.ports.wait_ready(readable, writes, self._remaining())
        if (
            len(set(ready)) != len(ready)
            or len(set(write_ready)) != len(write_ready)
            or any(fd not in readable for fd in ready)
            or any(fd not in writes for fd in write_ready)
        ):
            raise ActorHostError("actor_protocol_invalid")
        self._remaining()
        for descriptor in ready:
            if descriptor == self.error_fd:
                while self.stderr_open:
                    self._remaining()
                    maximum = self.limits.max_total_bytes - self.total + 1
                    try:
                        discarded = self.ports.read_bytes(descriptor, maximum)
                    except BlockingIOError:
                        break
                    if not isinstance(discarded, bytes) or len(discarded) > maximum:
                        raise ActorHostError("actor_protocol_invalid")
                    self._account(len(discarded))
                    if not discarded:
                        self.stderr_open = False
                continue
            maximum = self.limits.max_total_bytes - self.total + 1
            if descriptor == self.output_fd:
                maximum = min(
                    maximum, self.limits.max_frame_bytes - len(self.buffer) + 1
                )
            try:
                data = self.ports.read_bytes(descriptor, maximum)
            except BlockingIOError:
                continue
            if not isinstance(data, bytes) or len(data) > maximum:
                raise ActorHostError("actor_protocol_invalid")
            self._account(len(data))
            if not data:
                raise ActorHostError("actor_frontend_unavailable")
            else:
                self.buffer.extend(data)
                while b"\n" in self.buffer:
                    line, _, rest = self.buffer.partition(b"\n")
                    if len(line) > self.limits.max_frame_bytes:
                        raise ActorHostError("actor_protocol_invalid")
                    self.buffer = bytearray(rest)
                    try:
                        value = json.loads(
                            line.decode("utf-8", errors="strict"),
                            object_pairs_hook=_unique_fields,
                        )
                    except (ValueError, UnicodeError, RecursionError):
                        raise ActorHostError("actor_protocol_invalid") from None
                    if not isinstance(value, dict):
                        raise ActorHostError("actor_protocol_invalid")
                    if value.get("method") == "remoteControl/status/changed":
                        try:
                            _RemoteControlStatusChanged.model_validate(value)
                        except ValueError:
                            raise ActorHostError("actor_protocol_invalid") from None
                    self.events.append(value)
                if len(self.buffer) > self.limits.max_frame_bytes:
                    raise ActorHostError("actor_protocol_invalid")
        if not ready and not write_ready and self.process.poll() is not None:
            raise ActorHostError("actor_frontend_unavailable")
        return bool(write_ready)

    def _check_pending_remote_notifications(self) -> None:
        if self.startup_phase != "open" and any(
            event.get("method") == "remoteControl/status/changed"
            for event in self.events
        ):
            raise ActorHostError("actor_protocol_invalid")

    def _send(self, message: Mapping[str, object]) -> None:
        self._check_pending_remote_notifications()
        try:
            payload = (
                json.dumps(dict(message), ensure_ascii=True, allow_nan=False).encode()
                + b"\n"
            )
        except (ValueError, UnicodeError, RecursionError):
            raise ActorHostError("actor_protocol_invalid") from None
        if len(payload) - 1 > self.limits.max_frame_bytes:
            raise ActorHostError("actor_protocol_invalid")
        offset = 0
        while offset < len(payload):
            if not self._pump(writable=True):
                continue
            self._check_pending_remote_notifications()
            try:
                written = self.ports.write_bytes(self.input_fd, payload[offset:])
            except BlockingIOError:
                continue
            if type(written) is not int or not 0 < written <= len(payload) - offset:
                raise ActorHostError("actor_frontend_unavailable")
            offset += written
            self._account(written)

    def request(
        self, method: ActorMethod, parameters: Mapping[str, object]
    ) -> Mapping[str, object]:
        if method not in {"initialize", "config/read", "thread/start", "turn/start"}:
            raise ActorHostError("actor_protocol_invalid")
        if (
            method in {"thread/start", "turn/start"}
            or (method == "initialize" and self.identifier != 0)
            or (method == "config/read" and self.startup_phase == "awaiting_initialize")
        ):
            self.startup_phase = "closed"
        self.identifier += 1
        try:
            self._send(
                {"id": self.identifier, "method": method, "params": dict(parameters)}
            )
            acknowledgment: _ThreadStarted | _TurnStarted | None = None
            while True:
                event = self.next_event()
                if "method" in event:
                    if acknowledgment is not None:
                        raise ActorHostError("actor_protocol_invalid")
                    try:
                        if method == "thread/start":
                            acknowledgment = _ThreadStarted.model_validate(event)
                        elif method == "turn/start":
                            acknowledgment = _TurnStarted.model_validate(event)
                        else:
                            raise ActorHostError("actor_protocol_invalid")
                    except ValueError:
                        raise ActorHostError("actor_protocol_invalid") from None
                    continue
                try:
                    response = ActorRpcResponse.model_validate(event)
                except ValueError:
                    raise ActorHostError("actor_protocol_invalid") from None
                if (
                    response.id != self.identifier
                    or response.error is not None
                    or response.result is None
                ):
                    raise ActorHostError("actor_protocol_invalid")
                if isinstance(acknowledgment, _ThreadStarted) and (
                    acknowledgment.params.thread != response.result.get("thread")
                ):
                    raise ActorHostError("actor_protocol_invalid")
                if isinstance(acknowledgment, _TurnStarted) and (
                    acknowledgment.params.threadId != parameters.get("threadId")
                    or acknowledgment.params.turn != response.result.get("turn")
                ):
                    raise ActorHostError("actor_protocol_invalid")
                if (
                    method == "initialize"
                    and self.startup_phase == "awaiting_initialize"
                ):
                    self.startup_phase = "open"
                elif method == "config/read":
                    self.startup_phase = "closed"
                self._check_pending_remote_notifications()
                return response.result
        except (OSError, subprocess.SubprocessError):
            raise ActorHostError("actor_frontend_unavailable") from None

    def notify_initialized(self) -> None:
        try:
            self._send({"method": "initialized"})
        except (OSError, subprocess.SubprocessError):
            raise ActorHostError("actor_frontend_unavailable") from None

    def next_event(self) -> Mapping[str, object]:
        try:
            while True:
                while not self.events:
                    self._pump()
                event = self.events.popleft()
                if event.get("method") != "remoteControl/status/changed":
                    return event
                if self.startup_phase != "open":
                    raise ActorHostError("actor_protocol_invalid")
                self._remaining()
        except (OSError, subprocess.SubprocessError):
            raise ActorHostError("actor_frontend_unavailable") from None


def _open_process(spec: ActorProcessSpec) -> ActorRawProcess:
    return cast(
        ActorRawProcess,
        subprocess.Popen(
            spec.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=spec.cwd,
            env=dict(spec.environment),
            close_fds=True,
        ),
    )


def _wait_ready(
    readable: tuple[int, ...], writable: tuple[int, ...], timeout: float
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    reads, writes, _ = select.select(readable, writable, (), timeout)
    return tuple(reads), tuple(writes)


def _close_process(
    process: ActorRawProcess, limits: ActorTransportLimits, ports: ActorProcessPorts
) -> None:
    deadline = ports.clock() + limits.cleanup_timeout_seconds
    failed = False
    try:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=max(0.0, deadline - ports.clock()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=max(0.0, deadline - ports.clock()))
    except (OSError, subprocess.SubprocessError):
        failed = True
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                failed = True
    if failed:
        raise ActorHostError("actor_cleanup_failed")


def create_stdio_actor_frontend(
    process_ports: ActorProcessPorts | None = None,
) -> ActorFrontendFactory:
    """Compose real streaming ports once; never use completed-process capture."""
    ports = process_ports or ActorProcessPorts(
        _open_process, _wait_ready, os.read, os.write, monotonic, os.set_blocking
    )

    @contextmanager
    def frontend(
        spec: ActorProcessSpec, limits: ActorTransportLimits
    ) -> Iterator[ActorSessionPort]:
        try:
            process = ports.open_process(spec)
        except (OSError, subprocess.SubprocessError):
            raise ActorHostError("actor_frontend_unavailable") from None
        try:
            try:
                session = _Session(process, spec, limits, ports)
            except (OSError, ValueError):
                raise ActorHostError("actor_frontend_unavailable") from None
            yield session
        finally:
            _close_process(process, limits, ports)

    return frontend
