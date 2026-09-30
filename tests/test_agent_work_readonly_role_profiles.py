# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Host-issued read-only roles cannot acquire shell or mutating MCP tools."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from mcp.server import MCPServer
from mcp.types import CallToolResult

from agent_work import mcp_server
from agent_work.mcp_server import create_candidate_mcp_server
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult
from agent_work.worker import AgentInvoker, RoleExecutionProfile


class _SandboxInvokerConstructor(Protocol):
    """Declare the approved future constructor contract without untyped values."""

    def __call__(
        self,
        run_command: CommandRunner,
        *,
        environment: Mapping[str, str],
        temporary_directory: Path | None = None,
        controller_state_root: Path | None = None,
        read_only_host_audit: Callable[
            [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
        ]
        | None = None,
        read_only_sandbox_audit: Callable[
            [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
        ]
        | None = None,
    ) -> AgentInvoker:
        """Construct the invoker with the architect-approved audit seams."""
        ...


TREE = "b" * 40
NONCE = "read-only-role-nonce"
READ_TOOLS = {"candidate_status", "candidate_changed_paths", "candidate_read"}
ROLE_TOOLS = {
    "router": READ_TOOLS,
    "judge": READ_TOOLS | {"candidate_submit_review"},
    "finalizer": READ_TOOLS,
    "supervisor-inspect": {"candidate_status"},
}
SAFE_FEATURES = {
    "shell_tool": False,
    "unified_exec": False,
    "code_mode_host": False,
}


def _assert_audit_command(command: tuple[str, ...]) -> None:
    assert command[0] == "/codex"
    assert command[-2:] == ("features", "list")
    config = [
        command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"
    ]
    for feature in SAFE_FEATURES:
        assert f"features.{feature}=false" in config


def _role_profile(role: str, socket: Path) -> RoleExecutionProfile:
    profile_type = cast(Any, RoleExecutionProfile)
    return cast(
        RoleExecutionProfile,
        profile_type.read_only(
            role=role,
            candidate_socket=socket,
            nonce=NONCE,
            issue_iid=351,
            candidate_tree=TREE,
        ),
    )


@pytest.mark.parametrize("role", ROLE_TOOLS)
def test_read_only_role_launch_has_closed_host_tool_profile(
    tmp_path: Path, role: str
) -> None:
    commands: list[tuple[str, ...]] = []
    events: list[tuple[str, str]] = []

    def audit_host(
        command: tuple[str, ...], environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        _assert_audit_command(command)
        home = Path(environment["CODEX_HOME"])
        assert home != source_home
        assert not (home / "config.toml").exists()
        events.append(("audit", str(home)))
        return SAFE_FEATURES

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        environment = cast(Mapping[str, str], kwargs["environment"])
        events.append(("launch", environment["CODEX_HOME"]))
        assert environment["AGENT_WORK_CANDIDATE_ROLE"] == role
        assert environment["AGENT_WORK_CANDIDATE_TREE"] == TREE
        assert environment["AGENT_WORK_CANDIDATE_NONCE"] == NONCE
        assert "GITLAB_TOKEN" not in environment
        assert not (Path(environment["CODEX_HOME"]) / "config.toml").exists()
        return CommandResult(0, "review complete", "")

    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        '[mcp_servers.untrusted]\ncommand = "false"\n'
    )
    (source_home / "config.toml").chmod(0o600)
    invocation = cast(_SandboxInvokerConstructor, AgentInvoker)(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
            "GITLAB_TOKEN": "private-token",
        },
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_sandbox_audit=lambda _command, _environment: {
            "write_denied": True,
            "network_denied": True,
            "auth_unreadable": True,
            "source_auth_unreadable": True,
            "controller_state_unreadable": True,
        },
        read_only_host_audit=audit_host,
    ).invoke(
        tmp_path,
        "Inspect the bound candidate.",
        10,
        role_profile=_role_profile(role, tmp_path / "candidate.sock"),
    )

    assert invocation.returncode == 0
    assert len(commands) == 1
    assert [event[0] for event in events] == ["audit", "launch"]
    assert events[0][1] == events[1][1]
    command = commands[0]
    config = [
        command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"
    ]
    enabled = next(
        json.loads(value.partition("=")[2])
        for value in config
        if value.startswith("mcp_servers.candidate.enabled_tools=")
    )
    assert set(enabled) == ROLE_TOOLS[role]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--strict-config" in command
    assert "features.shell_tool=false" in config
    assert "features.unified_exec=false" in config
    assert "features.code_mode_host=false" in config
    assert "mcp_servers.candidate.required=true" in config
    assert not any(token in command for token in ("--add-dir", "--enable"))
    assert TREE not in command
    assert NONCE not in command


@pytest.mark.parametrize(
    "role", ["", "candidate-preview", "tdd-green", "judge; bash", "router-extra"]
)
def test_read_only_profile_rejects_forged_or_writable_role(
    tmp_path: Path, role: str
) -> None:
    with pytest.raises(ValueError, match="role"):
        _role_profile(role, tmp_path / "candidate.sock")


@pytest.mark.parametrize("agent", ["claude", "pi"])
def test_read_only_role_fails_closed_without_supported_host(
    tmp_path: Path, agent: str
) -> None:
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "unexpected", "")

    invoker = cast(_SandboxInvokerConstructor, AgentInvoker)(
        run_command,
        environment={"AGENT_WORK_AGENT": agent},
        temporary_directory=tmp_path,
    )
    with pytest.raises(ValueError, match="Codex|read.only|role"):
        invoker.invoke(
            tmp_path,
            "Inspect the bound candidate.",
            10,
            role_profile=_role_profile("judge", tmp_path / "candidate.sock"),
        )
    assert commands == []


@pytest.mark.parametrize(
    "audit_result",
    [
        {"shell_tool": True, "unified_exec": False, "code_mode_host": False},
        {"shell_tool": False, "unified_exec": True, "code_mode_host": False},
        {"shell_tool": False, "unified_exec": False, "code_mode_host": True},
        {"shell_tool": False, "unified_exec": False},
        {"shell_tool": False, "unified_exec": False, "code_mode_host": "false"},
        None,
    ],
)
def test_read_only_role_refuses_unsafe_or_unverifiable_host_before_launch(
    tmp_path: Path, audit_result: object
) -> None:
    commands: list[tuple[str, ...]] = []
    audits: list[tuple[str, ...]] = []

    def audit_host(command: tuple[str, ...], environment: Mapping[str, str]) -> object:
        _assert_audit_command(command)
        assert not (Path(environment["CODEX_HOME"]) / "config.toml").exists()
        audits.append(command)
        return audit_result

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "unexpected launch", "")

    invoker = cast(_SandboxInvokerConstructor, AgentInvoker)(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_sandbox_audit=lambda _command, _environment: {
            "write_denied": True,
            "network_denied": True,
            "auth_unreadable": True,
            "source_auth_unreadable": True,
            "controller_state_unreadable": True,
        },
        read_only_host_audit=cast(Any, audit_host),
    )
    invocation = invoker.invoke(
        tmp_path,
        "Inspect the bound candidate.",
        10,
        role_profile=_role_profile("judge", tmp_path / "candidate.sock"),
    )
    assert len(audits) == 1
    assert commands == []
    assert invocation.returncode is None or invocation.returncode != 0


def test_read_only_role_refuses_audit_failure_before_launch(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []

    def audit_host(
        _command: tuple[str, ...], _environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        raise RuntimeError("host capability command unavailable")

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "unexpected launch", "")

    invoker = cast(_SandboxInvokerConstructor, AgentInvoker)(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_sandbox_audit=lambda _command, _environment: {
            "write_denied": True,
            "network_denied": True,
            "auth_unreadable": True,
            "source_auth_unreadable": True,
            "controller_state_unreadable": True,
        },
        read_only_host_audit=audit_host,
    )
    invocation = invoker.invoke(
        tmp_path,
        "Inspect the bound candidate.",
        10,
        role_profile=_role_profile("judge", tmp_path / "candidate.sock"),
    )
    assert commands == []
    assert invocation.returncode is None or invocation.returncode != 0


@pytest.mark.parametrize(
    ("audit_output", "audit_exit", "should_launch"),
    [
        (
            "shell_tool stable false\nunified_exec stable false\ncode_mode_host stable false\n",
            0,
            True,
        ),
        (
            "shell_tool stable true\nunified_exec stable false\ncode_mode_host stable false\n",
            0,
            False,
        ),
        ("shell_tool stable false\nunified_exec stable false\n", 0, False),
        (
            "shell_tool stable false\nshell_tool stable false\nunified_exec stable false\ncode_mode_host stable false\n",
            0,
            False,
        ),
        (
            "shell_tool stable false\nunified_exec stable maybe\ncode_mode_host stable false\n",
            0,
            False,
        ),
        ("", 1, False),
    ],
)
def test_default_host_audit_parses_effective_capabilities_before_launch(
    tmp_path: Path, audit_output: str, audit_exit: int, *, should_launch: bool
) -> None:
    commands: list[tuple[str, ...]] = []
    homes: list[str] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        environment = cast(Mapping[str, str], kwargs["environment"])
        home = environment["CODEX_HOME"]
        assert not (Path(home) / "config.toml").exists()
        commands.append(command)
        homes.append(home)
        if command[-2:] == ("features", "list"):
            _assert_audit_command(command)
            return CommandResult(audit_exit, audit_output, "")
        return CommandResult(0, "review complete", "")

    invocation = cast(_SandboxInvokerConstructor, AgentInvoker)(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_sandbox_audit=lambda _command, _environment: {
            "write_denied": True,
            "network_denied": True,
            "auth_unreadable": True,
            "source_auth_unreadable": True,
            "controller_state_unreadable": True,
        },
    ).invoke(
        tmp_path,
        "Inspect the bound candidate.",
        10,
        role_profile=_role_profile("judge", tmp_path / "candidate.sock"),
    )
    assert commands[0][-2:] == ("features", "list")
    assert len(commands) == (2 if should_launch else 1)
    assert len(set(homes)) == 1
    assert (invocation.returncode == 0) is should_launch


@pytest.mark.parametrize(
    ("role", "tool", "arguments"),
    [
        (
            "router",
            "candidate_submit_review",
            {"verdict": "approve", "summary": "ok", "findings": []},
        ),
        (
            "finalizer",
            "candidate_submit_review",
            {"verdict": "approve", "summary": "ok", "findings": []},
        ),
        ("supervisor-inspect", "candidate_changed_paths", {}),
        (
            "supervisor-inspect",
            "candidate_read",
            {"path": "README.md", "side": "new", "offset": 0, "length": 16},
        ),
    ],
)
def test_mcp_server_rejects_tool_outside_host_issued_role(
    role: str, tool: str, arguments: dict[str, object]
) -> None:
    calls: list[tuple[str, str]] = []

    def gateway(
        action: str, tree: str, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree))
        return {"candidate_tree": tree}

    server = cast(Any, create_candidate_mcp_server)(
        cast(Any, gateway), candidate_tree=TREE, role=role
    )
    advertised = asyncio.run(server.list_tools())
    assert {entry.name for entry in advertised} == ROLE_TOOLS[role]
    result = cast(CallToolResult, asyncio.run(server.call_tool(tool, arguments)))
    assert result.is_error
    assert calls == []


def test_mcp_server_keeps_allowed_judge_review_bound_to_exact_tree() -> None:
    calls: list[tuple[str, str]] = []

    def gateway(
        action: str, tree: str, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree))
        return {"candidate_tree": tree, "state": "reviewed"}

    server = cast(Any, create_candidate_mcp_server)(
        cast(Any, gateway), candidate_tree=TREE, role="judge"
    )
    result = cast(
        CallToolResult,
        asyncio.run(
            server.call_tool(
                "candidate_submit_review",
                {"verdict": "approve", "summary": "ok", "findings": []},
            )
        ),
    )
    assert not result.is_error
    assert calls == [("review", TREE)]


def test_mcp_entry_point_requires_explicit_host_role_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_WORK_CANDIDATE_SOCKET", str(tmp_path / "candidate.sock"))
    monkeypatch.setenv("AGENT_WORK_CANDIDATE_NONCE", NONCE)
    monkeypatch.setenv("AGENT_WORK_CANDIDATE_ISSUE_IID", "351")
    monkeypatch.setenv("AGENT_WORK_CANDIDATE_TREE", TREE)
    monkeypatch.delenv("AGENT_WORK_CANDIDATE_ROLE", raising=False)
    served: list[object] = []

    def run_server(_server: MCPServer, **_kwargs: object) -> None:
        served.append(_server)

    monkeypatch.setattr(MCPServer, "run", run_server)
    with pytest.raises(RuntimeError, match="role|setup failed"):
        mcp_server.main()
    assert served == []
