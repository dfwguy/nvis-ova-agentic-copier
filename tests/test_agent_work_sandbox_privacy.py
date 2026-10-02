# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only role launch requires independent feature and filesystem proof."""

from __future__ import annotations

import errno
import io
import json
import os
import socket
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from agent_work.repository import CommandResult
from agent_work.worker import RoleExecutionProfile
from tests.test_agent_work_actor_mcp_host import InvokerTranscript, actor_invoker

SAFE_FEATURES = dict.fromkeys(("shell_tool", "unified_exec", "code_mode_host"), False)
SAFE_PRIVACY = dict.fromkeys(
    (
        "write_denied",
        "network_denied",
        "auth_unreadable",
        "source_auth_unreadable",
        "controller_state_unreadable",
    ),
    True,
)


def _profile(tmp_path: Path) -> RoleExecutionProfile:
    return RoleExecutionProfile.read_only(
        role="judge",
        candidate_socket=tmp_path / "candidate.sock",
        nonce="private-role-nonce",
        issue_iid=351,
        candidate_tree="b" * 40,
    )


def _config(command: tuple[str, ...]) -> tuple[str, ...]:
    prefix = command[: command.index("--")] if "--" in command else command
    return tuple(
        prefix[index + 1] for index, token in enumerate(prefix[:-1]) if token == "-c"
    )


@pytest.mark.parametrize("role", ["router", "judge", "finalizer", "supervisor-inspect"])
def test_privacy_probe_and_role_share_isolated_home_and_sandbox(
    tmp_path: Path, role: str
) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}")
    (source_home / "auth.json").chmod(0o600)
    state_root = tmp_path / "private-state"
    state_root.mkdir()
    launches: list[tuple[tuple[str, ...], Mapping[str, str]]] = []
    processes: list[InvokerTranscript] = []
    probes: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    def audit_privacy(
        command: tuple[str, ...], environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        probes.append((command, environment))
        home = Path(environment["CODEX_HOME"])
        assert not (home / "config.toml").exists()
        assert (home / "auth.json").resolve() == source_home / "auth.json"
        inputs = " ".join((*command, *environment.values()))
        assert str(home / "auth.json") in inputs
        assert str(source_home / "auth.json") in inputs
        assert str(state_root) in inputs
        return SAFE_PRIVACY

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        pytest.fail("Legacy executable launch must not be used")

    profile = RoleExecutionProfile.read_only(
        role=role,
        candidate_socket=tmp_path / "candidate.sock",
        nonce="private-role-nonce",
        issue_iid=351,
        candidate_tree="b" * 40,
    )
    result = actor_invoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
            "GITLAB_TOKEN": "synthetic-secret",
        },
        temporary_directory=tmp_path,
        controller_state_root=state_root,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        read_only_sandbox_audit=audit_privacy,
        processes=processes,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=profile)
    launches.extend(
        (spec.argv, dict(spec.environment))
        for process in processes
        for spec in process.specs
    )
    assert result.returncode == 0
    assert len(probes) == len(launches) == 1
    probe, probe_environment = probes[0]
    launch, launch_environment = launches[0]
    assert probe[:2] == ("/codex", "sandbox")
    separator = probe.index("--")
    options = probe[2:separator]
    assert not any(
        token == "linux"
        and (index == 0 or options[index - 1] not in {"-c", "-C", "--cd"})
        for index, token in enumerate(options)
    )
    assert probe[separator + 1 : separator + 4] == (sys.executable, "-I", "-c")
    assert probe_environment["CODEX_HOME"] == launch_environment["CODEX_HOME"]
    assert "GITLAB_TOKEN" not in probe_environment
    assert launch[:4] == ("/codex", "app-server", "--stdio", "--strict-config")
    assert 'approval_policy="never"' in _config(probe)
    assert not any(
        value.startswith("sandbox_mode=")
        for value in set(_config(probe)) | set(_config(launch))
    )
    assert probe[probe.index("--permission-profile") + 1] == next(
        json.loads(value.partition("=")[2])
        for value in _config(launch)
        if value.startswith("default_permissions=")
    )
    for feature in SAFE_FEATURES:
        assert f"features.{feature}=false" in set(_config(probe)) & set(_config(launch))
    assert any(
        probe[index + 1] == str(tmp_path)
        for index, token in enumerate(probe[:-1])
        if token in {"-C", "--cd"}
    )
    assert "--ignore-user-config" not in launch
    assert _config(probe) == _config(launch)


@pytest.mark.parametrize(
    "evidence",
    [
        *({**SAFE_PRIVACY, key: False} for key in SAFE_PRIVACY),
        *(
            {name: value for name, value in SAFE_PRIVACY.items() if name != key}
            for key in SAFE_PRIVACY
        ),
        {**SAFE_PRIVACY, "auth_unreadable": "true"},
        {**SAFE_PRIVACY, "network_denied": 1},
        {**SAFE_PRIVACY, "extra": True},
        None,
    ],
)
def test_role_never_launches_with_incomplete_or_nonboolean_privacy(
    tmp_path: Path, evidence: object
) -> None:
    launches: list[tuple[str, ...]] = []
    probes: list[tuple[str, ...]] = []

    def audit_privacy(
        command: tuple[str, ...], _environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        probes.append(command)
        return cast(Mapping[str, bool], evidence)

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        launches.append(command)
        return CommandResult(0, "unexpected launch", "")

    result = actor_invoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        read_only_sandbox_audit=audit_privacy,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert len(probes) == 1
    assert launches == []
    assert result.returncode != 0


@pytest.mark.parametrize(
    "feature_output",
    [
        "shell_tool stable false\nunified_exec stable true\ncode_mode_host stable false\n",
        "shell_tool stable false\ncode_mode_host stable false\n",
        "shell_tool stable false\nunified_exec stable false\nunified_exec stable false\ncode_mode_host stable false\n",
    ],
)
def test_privacy_success_cannot_override_unsafe_effective_host_features(
    tmp_path: Path, feature_output: str
) -> None:
    launches: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[-2:] == ("features", "list"):
            return CommandResult(0, feature_output, "")
        launches.append(command)
        return CommandResult(0, "unexpected launch", "")

    result = actor_invoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_sandbox_audit=lambda _command, _environment: SAFE_PRIVACY,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert launches == []
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("not-json", 0),
        (json.dumps(SAFE_PRIVACY), 1),
        (
            json.dumps(SAFE_PRIVACY).replace(
                '"write_denied": true', '"write_denied": true, "write_denied": true'
            ),
            0,
        ),
        (json.dumps({**SAFE_PRIVACY, "extra": True}), 0),
        (json.dumps({**SAFE_PRIVACY, "source_auth_unreadable": False}), 0),
    ],
)
def test_default_sandbox_audit_denies_malformed_or_failed_probe(
    tmp_path: Path, output: str, exit_code: int
) -> None:
    launches: list[tuple[str, ...]] = []
    probes: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[:2] == ("/codex", "sandbox"):
            probes.append(command)
            return CommandResult(exit_code, output, "")
        launches.append(command)
        return CommandResult(0, "unexpected launch", "")

    result = actor_invoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert len(probes) == 1
    assert launches == []
    assert result.returncode != 0


@pytest.mark.parametrize("private_access", ["denied", "readable", "missing"])
def test_emitted_probe_checks_known_state_file_even_when_directory_listing_denied(
    tmp_path: Path, private_access: str
) -> None:
    state_root = tmp_path / "controller-state"
    state_root.mkdir()
    (state_root / "project.lock").write_text("synthetic private state")
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    (source_home / "auth.json").write_text("{}")
    (source_home / "auth.json").chmod(0o600)
    launches: list[tuple[str, ...]] = []
    processes: list[InvokerTranscript] = []
    state_reads: list[str] = []
    directory_reads: list[str] = []

    def deny_boundary(*_args: object, **_kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "synthetic sandbox denial")

    class DescriptorFile(io.BytesIO):
        """Synthetic descriptor rejects attempts to read private bytes."""

        def fileno(self) -> int:
            return 17

        def read(self, _size: int | None = -1, /) -> bytes:
            raise AssertionError("Probe must not consume private record bytes")

    def deny_directory(path: object) -> object:
        directory_reads.append(str(path))
        raise PermissionError(errno.EACCES, "synthetic sandbox denial")

    def open_file(path: object, *_args: object, **_kwargs: object) -> io.BytesIO:
        if str(path).startswith(str(state_root) + "/"):
            state_reads.append(str(path))
            if private_access == "readable":
                return DescriptorFile(b"synthetic")
            if private_access == "missing":
                raise FileNotFoundError(errno.ENOENT, "synthetic absent record")
        raise PermissionError(errno.EACCES, "synthetic sandbox denial")

    def open_descriptor(path: object, flags: int, *_args: object) -> int:
        if not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            open_file(path)
            return 17
        raise PermissionError(errno.EACCES, "synthetic sandbox denial")

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if command[:2] != ("/codex", "sandbox"):
            launches.append(command)
            return CommandResult(0, "review complete", "")
        python_index = command.index("--") + 1
        script_index = command.index("-c", python_index) + 1
        buffer = io.StringIO()
        with (
            patch.object(sys, "argv", ["probe", *command[script_index + 1 :]]),
            patch("builtins.open", open_file),
            patch("io.open", open_file),
            patch.object(os, "open", open_descriptor),
            patch.object(os, "close", lambda _descriptor: None),
            patch.object(os, "listdir", deny_directory),
            patch.object(socket, "socket", deny_boundary),
            redirect_stdout(buffer),
        ):
            exec(compile(command[script_index], "sandbox-probe", "exec"), {})  # noqa: S102 - Execute only the emitted repository probe under fake system boundaries.
        return CommandResult(0, buffer.getvalue(), "")

    result = actor_invoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
        },
        temporary_directory=tmp_path,
        controller_state_root=state_root,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        processes=processes,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert str(state_root) in directory_reads
    assert str(state_root / "project.lock") in state_reads
    assert launches == []
    assert len(processes[0].specs) == (1 if private_access == "denied" else 0)
    turns = [
        message
        for message in processes[0].requests
        if message["method"] == "turn/start"
    ]
    assert len(turns) == (1 if private_access == "denied" else 0)
    if private_access == "denied":
        assert (
            cast(dict[str, object], turns[0]["params"])["threadId"] == "thread-fixture"
        )
        assert processes[0].waits > 0
        assert all(
            stream is not None and stream.closed
            for stream in (processes[0].stdin, processes[0].stdout, processes[0].stderr)
        )
    for process in processes:
        process.dispose()
    assert (result.returncode == 0) is (private_access == "denied")


def test_missing_state_root_denies_role_before_any_audit_or_launch(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, "unexpected launch", "")

    def audit_privacy(
        command: tuple[str, ...], _environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        calls.append(command)
        return SAFE_PRIVACY

    result = actor_invoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        read_only_sandbox_audit=audit_privacy,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert calls == []
    assert result.returncode != 0


@pytest.mark.parametrize(
    ("operation", "expected_field"),
    [
        ("write", "write_denied"),
        ("network", "network_denied"),
        ("isolated-auth", "auth_unreadable"),
        ("source-auth", "source_auth_unreadable"),
        ("missing-isolated-auth", "auth_unreadable"),
        ("missing-source-auth", "source_auth_unreadable"),
        ("network-refused", "network_denied"),
    ],
)
def test_emitted_probe_rejects_each_unsafe_operation_or_nonpermission_failure(
    tmp_path: Path, operation: str, expected_field: str
) -> None:
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text("{}")
    source_auth.chmod(0o600)
    state_root = tmp_path / "controller-state"
    state_root.mkdir()
    (state_root / "project.lock").write_text("synthetic controller state")
    launches: list[tuple[str, ...]] = []
    evidence: list[dict[str, bool]] = []
    attempted: list[str] = []

    def deny_access(*_args: object, **_kwargs: object) -> object:
        raise PermissionError(errno.EACCES, "synthetic sandbox denial")

    def prohibit_bytes(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Probe must not consume private bytes")

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if command[:2] != ("/codex", "sandbox"):
            launches.append(command)
            return CommandResult(0, "unexpected role launch", "")
        environment = cast(Mapping[str, str], kwargs["environment"])
        isolated_auth = Path(environment["CODEX_HOME"]) / "auth.json"
        python_index = command.index("--") + 1
        script_index = command.index("-c", python_index) + 1

        def open_descriptor(path: object, flags: int, *_args: object) -> int:
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                boundary = "write"
            elif Path(str(path)) == isolated_auth:
                boundary = "isolated-auth"
            elif Path(str(path)) == source_auth:
                boundary = "source-auth"
            else:
                boundary = "controller-state"
            attempted.append(boundary)
            if operation == boundary:
                return 17
            if operation == f"missing-{boundary}":
                raise FileNotFoundError(errno.ENOENT, "synthetic absent auth")
            raise PermissionError(errno.EACCES, "synthetic sandbox denial")

        class Connection:
            """Synthetic network connection never contacts a real endpoint."""

            def settimeout(self, _timeout: float) -> None:
                pass

            def connect(self, _address: tuple[str, int]) -> None:
                attempted.append("network")
                if operation == "network-refused":
                    raise ConnectionRefusedError(
                        errno.ECONNREFUSED, "synthetic refusal"
                    )

        @contextmanager
        def socket_boundary(*_args: object, **_kwargs: object) -> Iterator[Connection]:
            if operation not in {"network", "network-refused"}:
                raise PermissionError(errno.EACCES, "synthetic sandbox denial")
            yield Connection()

        buffer = io.StringIO()
        with (
            patch.object(sys, "argv", ["probe", *command[script_index + 1 :]]),
            patch("builtins.open", prohibit_bytes),
            patch("io.open", prohibit_bytes),
            patch.object(os, "open", open_descriptor),
            patch.object(os, "close", lambda _descriptor: None),
            patch.object(os, "read", prohibit_bytes),
            patch.object(os, "listdir", deny_access),
            patch.object(Path, "unlink", lambda _path: None),
            patch.object(socket, "socket", socket_boundary),
            redirect_stdout(buffer),
        ):
            exec(compile(command[script_index], "sandbox-probe", "exec"), {})  # noqa: S102 - Execute only the emitted repository probe under fake system boundaries.
        output = buffer.getvalue()
        evidence.append(json.loads(output))
        return CommandResult(0, output, "")

    result = actor_invoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
        },
        temporary_directory=tmp_path,
        controller_state_root=state_root,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
    ).invoke(tmp_path, "Inspect candidate.", 10, role_profile=_profile(tmp_path))
    assert evidence == [{**SAFE_PRIVACY, expected_field: False}]
    assert operation.removeprefix("missing-").removesuffix("-refused") in attempted
    assert launches == []
    assert result.returncode != 0
