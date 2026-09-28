# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The opt-in candidate preview gets a bounded Codex command profile."""

from __future__ import annotations

import importlib
import json
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args

import pytest

from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.repository import CommandResult
from agent_work.worker import (
    AgentInvoker,
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    WorkerResultBrokerRequest,
    create_candidate_broker,
    request_candidate_validation,
)

if TYPE_CHECKING:
    from agent_work.worker import RoleExecutionProfile

TREE = "b" * 40
NONCE = "private-mcp-preview-nonce"
GITLAB_SECRET = "private-gitlab-token"
TOOLS = {
    "candidate_status",
    "candidate_read_manifest",
    "candidate_read_chunk",
    "candidate_read_complete",
    "candidate_submit_review",
}


def _preview_profile(
    socket_path: Path, *, role: str = "candidate-preview"
) -> RoleExecutionProfile:
    worker = importlib.import_module("agent_work.worker")
    return worker.RoleExecutionProfile.candidate_preview(
        role=role,
        candidate_socket=socket_path,
        nonce=NONCE,
        issue_iid=81,
        candidate_tree=TREE,
    )


def _run_preview(
    tmp_path: Path,
    *,
    prompt: str = "Review the candidate tree through candidate MCP tools.",
) -> tuple[tuple[str, ...], str | None, Mapping[str, str], str]:
    captured: list[tuple[tuple[str, ...], str | None, Mapping[str, str]]] = []
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_config = source_home / "config.toml"
    source_auth.write_text('{"token":"codex-auth"}\n')
    source_config.write_text('[mcp_servers.untrusted]\ncommand = "false"\n')
    source_auth.chmod(0o600)
    source_config.chmod(0o600)

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        environment = dict(cast(Mapping[str, str], kwargs["environment"]))
        isolated_home = Path(environment["CODEX_HOME"])
        assert isolated_home != source_home
        assert (isolated_home / "auth.json").read_bytes() == source_auth.read_bytes()
        assert not (isolated_home / "config.toml").exists()
        assert "GITLAB_TOKEN" not in environment
        captured.append(
            (
                command,
                kwargs["stdin"] if isinstance(kwargs["stdin"], str) else None,
                environment,
            )
        )
        return CommandResult(0, "review complete", "")

    socket_path = tmp_path / "controller" / "candidate.sock"
    invocation = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source_home),
            "GITLAB_TOKEN": GITLAB_SECRET,
        },
        temporary_directory=tmp_path,
    ).invoke(
        tmp_path,
        prompt,
        10,
        role_profile=_preview_profile(socket_path),
    )
    assert invocation.returncode == 0
    assert len(captured) == 1
    return *captured[0], invocation.stdout


def test_candidate_operation_type_names_every_broker_action() -> None:
    worker = importlib.import_module("agent_work.worker")

    assert set(get_args(worker.CandidateOperation)) == {
        "stage",
        "read",
        "validate",
        "status",
        "review",
    }


def test_candidate_preview_command_requires_read_only_mcp_and_explicit_approval(
    tmp_path: Path,
) -> None:
    command, stdin, environment, output = _run_preview(tmp_path)
    config = [
        command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"
    ]
    rendered = "\n".join(config)

    assert command[:2] == ("/codex", "exec")
    assert "--ignore-user-config" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=false" in config
    assert "features.unified_exec=false" in config
    assert re.search(r"mcp_servers\.candidate\.required\s*=\s*true", rendered)
    assert "agent_work.mcp_server" in rendered
    enabled_tools = next(
        value.partition("=")[2]
        for value in config
        if value.startswith("mcp_servers.candidate.enabled_tools=")
    )
    enabled = json.loads(enabled_tools)
    assert isinstance(enabled, list)
    assert set(enabled) == TOOLS
    assert len(enabled) == len(TOOLS)
    for tool in TOOLS:
        assert re.search(rf"{tool}.*approval_mode\s*=\s*['\"]approve['\"]", rendered)
    for unapproved in ("candidate_stage", "candidate_validate", "worker_result_build"):
        assert unapproved not in rendered
    for private in (
        NONCE,
        TREE,
        str(tmp_path / "controller" / "candidate.sock"),
    ):
        assert private not in " ".join(command)
        assert private not in (stdin or "")
        assert private not in output
    assert "issue_iid" not in (stdin or "")
    assert "issue #81" not in (stdin or "")
    assert environment["AGENT_WORK_CANDIDATE_NONCE"] == NONCE
    assert environment["AGENT_WORK_CANDIDATE_ISSUE_IID"] == "81"
    assert environment["AGENT_WORK_CANDIDATE_TREE"] == TREE
    assert environment["AGENT_WORK_CANDIDATE_SOCKET"] == str(
        tmp_path / "controller" / "candidate.sock"
    )
    assert "GITLAB_TOKEN" not in environment


@pytest.mark.parametrize(
    "role", ["judge-quality", "tdd-green", "diataxis-writer", "tdd-finalizer"]
)
def test_candidate_preview_profile_rejects_other_roles(
    tmp_path: Path, role: str
) -> None:
    with pytest.raises(ValueError, match="preview|reviewer|read.only|role"):
        _preview_profile(tmp_path / "candidate.sock", role=role)


def test_host_facing_candidate_preview_disables_edit_and_keeps_candidate_read(
    tmp_path: Path,
) -> None:
    command, _stdin, _environment, _output = _run_preview(tmp_path)
    config = [
        command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"
    ]
    host_controls = "\n".join(config)

    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "features.shell_tool=false" in config
    assert "features.unified_exec=false" in config
    assert "candidate_read_manifest" in host_controls
    assert "candidate_read_chunk" in host_controls
    assert "candidate_read_complete" in host_controls
    assert "candidate_status" in host_controls
    assert "candidate_submit_review" in host_controls


@pytest.mark.parametrize("accepted", [True, False])
def test_brokered_worker_keeps_trusted_result_authoritative_without_preview(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, *, accepted: bool
) -> None:
    request = WorkerResultBrokerRequest(
        nonce="0123456789abcdef" * 2,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )
    client_request = WorkerResultBrokerClientRequest(
        nonce=request.nonce, issue_iid=request.issue_iid
    )
    calls: list[tuple[str, str | None]] = []
    commands: list[tuple[str, ...]] = []
    candidate_session: Any = None
    result_root = tmp_path / "result"
    result_root.mkdir()
    cache = result_root / "cache"
    cache.mkdir()

    class ResultSession:
        socket_path = result_root / "result.sock"
        cache_path = cache

        def close_and_capture(self) -> bytes:
            if not accepted:
                raise WorkerProtocolError(
                    "worker_result_broker", "request was not accepted"
                )
            return b'{"accepted":true}'

    def candidate_action(action: str, tree: str | None) -> dict[str, object]:
        calls.append((action, tree))
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        assert action == "validate" and tree == TREE
        return {"candidate_tree": TREE, "state": "terminal-success"}

    def candidate_factory(action: Any, **kwargs: object) -> Any:
        nonlocal candidate_session
        candidate_session = create_candidate_broker(
            action,
            request=cast(WorkerResultBrokerRequest, kwargs["request"]),
            timeout_seconds=10,
            temporary_directory=tmp_path_factory.mktemp("cr"),
        )
        return candidate_session

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        assert len(commands) == 1
        assert candidate_session is not None
        assert command[command.index("--sandbox") + 1] == "workspace-write"
        staged = request_candidate_validation(
            candidate_session.socket_path, client_request, action="stage"
        )
        assert staged["candidate_tree"] == TREE
        receipt = request_candidate_validation(
            candidate_session.socket_path,
            client_request,
            action="validate",
            candidate_tree=TREE,
        )
        assert receipt["state"] == "terminal-success"
        return CommandResult(0, "worker complete", "")

    invocation = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        broker_factory=cast(Any, lambda *_args, **_kwargs: ResultSession()),
        candidate_broker_factory=cast(Any, candidate_factory),
        temporary_directory=tmp_path,
    ).invoke(
        tmp_path,
        "candidate --candidate-socket PATH --broker-socket PATH",
        10,
        broker_request=request,
        build_artifact=lambda payload: payload,
        candidate_action=candidate_action,
    )

    assert len(commands) == 1
    assert calls == [("stage", None), ("validate", TREE)]
    assert candidate_session is not None
    assert not candidate_session.socket_path.parent.exists()
    if accepted:
        assert invocation.protocol_error is None
        assert invocation.worker_result == b'{"accepted":true}'
        assert invocation.validated_candidate == TREE
    else:
        assert invocation.protocol_error is not None
        assert invocation.protocol_error.field == "worker_result_broker"


def test_explicit_candidate_preview_returns_nonzero_exit_to_caller(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(23, "", "preview failed")

    invocation = AgentInvoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
    ).invoke(
        tmp_path,
        "Preview exact candidate content.",
        10,
        role_profile=_preview_profile(tmp_path / "candidate.sock"),
    )

    assert len(commands) == 1
    assert commands[0][commands[0].index("--sandbox") + 1] == "read-only"
    assert invocation.returncode == 23
    assert invocation.protocol_error is None


def test_candidate_preview_uses_trusted_controller_package_and_scrubs_import_overrides(
    tmp_path: Path,
) -> None:
    candidate_checkout = tmp_path / "candidate"
    candidate_module = candidate_checkout / "src" / "agent_work" / "mcp_server.py"
    candidate_module.parent.mkdir(parents=True)
    candidate_module.write_text("raise RuntimeError('candidate-controlled MCP')\n")
    trusted_checkout = tmp_path / "trusted-controller"
    trusted_checkout.mkdir()
    captured: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        captured.append((command, dict(cast(Mapping[str, str], kwargs["environment"]))))
        return CommandResult(0, "review complete", "")

    invocation = AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "PYTHONPATH": str(candidate_checkout / "src"),
            "PYTHONHOME": str(candidate_checkout),
            "VIRTUAL_ENV": str(candidate_checkout / ".venv"),
            "UV_PROJECT": str(candidate_checkout),
            "UV_PROJECT_ENVIRONMENT": str(candidate_checkout / ".venv"),
        },
        trusted_controller_checkout=trusted_checkout,
        temporary_directory=tmp_path,
    ).invoke(
        candidate_checkout,
        "Review this candidate through MCP.",
        10,
        role_profile=_preview_profile(tmp_path / "private" / "candidate.sock"),
    )

    assert invocation.returncode == 0
    assert len(captured) == 1
    command, environment = captured[0]
    config = [
        command[index + 1] for index, token in enumerate(command[:-1]) if token == "-c"
    ]
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    mcp_command = next(
        value.partition("=")[2]
        for value in config
        if value.startswith("mcp_servers.candidate.command=")
    )
    assert json.loads(mcp_command) == str(Path(uv_executable).resolve())
    mcp_args = next(
        value.partition("=")[2]
        for value in config
        if value.startswith("mcp_servers.candidate.args=")
    )
    assert json.loads(mcp_args) == [
        "run",
        "--project",
        str(trusted_checkout),
        "--frozen",
        "python",
        "-I",
        "-m",
        "agent_work.mcp_server",
    ]
    assert str(candidate_checkout) not in mcp_args
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
    ):
        assert name not in environment
