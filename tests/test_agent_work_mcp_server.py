# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the typed, read-only candidate MCP bridge."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import Client, StdioServerParameters

from agent_work.candidate_boundary import GIT_READ_MAX_CHUNK_BYTES
from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.mcp_server import create_candidate_mcp_server
from agent_work.worker import (
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    WorkerResultBrokerRequest,
    create_candidate_broker,
    request_candidate_validation,
)

TREE = "b" * 40
BASE = "a" * 40
PRIVATE_NONCE = "private-controller-nonce"
TOOLS = {
    "candidate_status",
    "candidate_changed_paths",
    "candidate_read",
    "candidate_submit_review",
}


def _server(
    gateway: Callable[[str, str, dict[str, object] | None], dict[str, object]],
) -> Any:
    return create_candidate_mcp_server(gateway=gateway, candidate_tree=TREE)


def _client_round_trip(server: Any, operation: Callable[[Any], Any]) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await operation(client)

    return asyncio.run(run())


def test_candidate_mcp_exposes_four_stateless_operation_schemas() -> None:
    server = _server(lambda _action, _tree, _selector: {"candidate_tree": TREE})

    async def list_tools(client: Any) -> Any:
        return await client.list_tools()

    listed = _client_round_trip(server, list_tools)
    schemas = {tool.name: tool.input_schema for tool in listed.tools}
    assert set(schemas) == TOOLS
    assert schemas["candidate_status"]["properties"] == {}
    assert schemas["candidate_changed_paths"]["properties"] == {}
    assert set(schemas["candidate_read"]["properties"]) == {
        "path",
        "side",
        "offset",
        "length",
    }
    assert set(schemas["candidate_read"]["required"]) == {
        "path",
        "side",
        "offset",
        "length",
    }
    assert set(schemas["candidate_submit_review"]["properties"]) == {
        "verdict",
        "summary",
        "findings",
    }
    for schema in schemas.values():
        assert schema["additionalProperties"] is False
        assert not (
            {"nonce", "issue_iid", "project_id", "candidate_tree"}
            & set(schema["properties"])
        )


@pytest.mark.parametrize(
    ("tool", "arguments", "via_client"),
    [
        ("candidate_status", {"nonce": PRIVATE_NONCE}, True),
        ("candidate_changed_paths", {"issue_iid": 81}, False),
        (
            "candidate_submit_review",
            {
                "verdict": "approve",
                "summary": "Reviewed",
                "findings": [],
                "nonce": PRIVATE_NONCE,
            },
            True,
        ),
        (
            "candidate_submit_review",
            {"verdict": "approve", "summary": "x" * 3000, "findings": []},
            False,
        ),
        (
            "candidate_submit_review",
            {
                "verdict": "reject",
                "summary": "Found issue",
                "findings": [
                    {
                        "severity": "major",
                        "path": "docs/a.md",
                        "summary": "Issue",
                        "project_id": 310967,
                    }
                ],
            },
            False,
        ),
        *(
            (
                "candidate_submit_review",
                {
                    "verdict": "reject",
                    "summary": "Found issue",
                    "findings": [
                        {"severity": "major", "path": path, "summary": "Issue"}
                    ],
                },
                False,
            )
            for path in ("../docs/a.md", "docs//a.md", "docs/./a.md")
        ),
        (
            "candidate_submit_review",
            {
                "verdict": "reject",
                "summary": "Too many findings",
                "findings": [
                    {"severity": "major", "path": "docs/a.md", "summary": "Issue"}
                ]
                * 33,
            },
            False,
        ),
        *(
            (
                "candidate_submit_review",
                {
                    "verdict": "approve",
                    "summary": "Cannot approve blocking findings",
                    "findings": [
                        {
                            "severity": severity,
                            "path": "docs/a.md",
                            "summary": "Blocking issue",
                        }
                    ],
                },
                False,
            )
            for severity in ("major", "critical")
        ),
        (
            "candidate_read",
            {
                "path": "docs/a.md",
                "side": "new",
                "offset": 0,
                "length": 1,
                "project_id": 310967,
            },
            False,
        ),
        (
            "candidate_read",
            {
                "path": "docs/a.md",
                "side": "new",
                "offset": 0,
                "length": GIT_READ_MAX_CHUNK_BYTES + 1,
            },
            True,
        ),
        (
            "candidate_read",
            {"path": "docs/a.md", "side": "new", "offset": True, "length": 1},
            False,
        ),
        (
            "candidate_read",
            {"path": "docs/a.md", "side": "deleted", "offset": 0, "length": 1},
            False,
        ),
    ],
)
def test_candidate_mcp_rejects_extra_identity_and_invalid_range_arguments(
    tool: str, arguments: dict[str, object], *, via_client: bool
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def gateway(
        action: str, tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree, selector))
        return {"candidate_tree": tree, "state": "staged"}

    server = _server(gateway)
    if tool == "candidate_submit_review":

        async def list_tools(client: Any) -> Any:
            return await client.list_tools()

        listed = _client_round_trip(server, list_tools)
        assert tool in {item.name for item in listed.tools}
    if via_client:

        async def call(client: Any) -> Any:
            return await client.call_tool(tool, arguments)

        result = _client_round_trip(server, call)
    else:
        result = asyncio.run(server.call_tool(tool, arguments))
    assert result.is_error is True
    assert calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_action", "expected_selector"),
    [
        (
            "candidate_read",
            {"path": "docs/a.md", "side": "new", "offset": 0, "length": 3},
            "read",
            {
                "path": "docs/a.md",
                "side": "new",
                "offset": 0,
                "length": 3,
            },
        ),
        (
            "candidate_changed_paths",
            {},
            "read",
            {"mode": "changed_paths"},
        ),
        (
            "candidate_submit_review",
            {
                "verdict": "reject",
                "summary": "One issue",
                "findings": [
                    {
                        "severity": "minor",
                        "path": "docs/a.md",
                        "summary": "Clarify this file",
                    }
                ],
            },
            "review",
            {
                "verdict": "reject",
                "summary": "One issue",
                "findings": [
                    {
                        "severity": "minor",
                        "path": "docs/a.md",
                        "summary": "Clarify this file",
                    }
                ],
            },
        ),
    ],
)
def test_candidate_mcp_routes_valid_operations_to_bound_gateway(
    tool: str,
    arguments: dict[str, object],
    expected_action: str,
    expected_selector: dict[str, object],
) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def gateway(
        action: str, tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree, selector))
        return {"candidate_tree": tree, "state": "accepted"}

    async def call(client: Any) -> Any:
        return await client.call_tool(tool, arguments)

    result = _client_round_trip(_server(gateway), call)
    assert result.is_error is False
    assert result.structured_content["candidate_tree"] == TREE
    assert calls == [(expected_action, TREE, expected_selector)]


@pytest.mark.integration
def test_candidate_mcp_stdio_serves_live_broker_without_exposing_private_inputs(
    project_root: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    candidate_checkout = tmp_path_factory.mktemp("candidate")
    controlled_module = candidate_checkout / "agent_work" / "mcp_server.py"
    controlled_module.parent.mkdir()
    controlled_module.write_text("raise RuntimeError('candidate module ran')\n")
    uv_executable = shutil.which("uv")
    assert uv_executable is not None
    request = WorkerResultBrokerRequest(
        nonce="0123456789abcdef" * 2,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha=BASE,
    )
    client_request = WorkerResultBrokerClientRequest(
        nonce=request.nonce, issue_iid=request.issue_iid
    )
    calls: list[tuple[str, str | None, dict[str, object] | None]] = []

    def candidate_action(
        action: str, tree: str | None, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree, selector))
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        if action == "status":
            return {"candidate_tree": TREE, "state": "terminal-success"}
        assert action == "read" and selector == {"mode": "changed_paths"}
        return {
            "candidate_tree": TREE,
            "base_sha": BASE,
            "base_tree": "c" * 40,
            "changed_paths": ["docs/a.md"],
            "files": [
                {
                    "path": "docs/a.md",
                    "old_mode": None,
                    "old_oid": None,
                    "old_size": None,
                    "old_sha256": None,
                    "new_mode": "100644",
                    "new_oid": "d" * 40,
                    "new_size": 0,
                    "new_sha256": sha256(b"").hexdigest(),
                }
            ],
        }

    session = create_candidate_broker(
        candidate_action,
        request=request,
        timeout_seconds=10,
        temporary_directory=tmp_path_factory.mktemp("ms"),
    )
    socket_root = session.socket_path.parent
    try:
        request_candidate_validation(
            session.socket_path, client_request, action="stage"
        )
        parameters = StdioServerParameters(
            command=uv_executable,
            args=[
                "run",
                "--project",
                str(project_root),
                "--frozen",
                "python",
                "-I",
                "-m",
                "agent_work.mcp_server",
            ],
            cwd=candidate_checkout,
            env={
                "AGENT_WORK_CANDIDATE_SOCKET": str(session.socket_path),
                "AGENT_WORK_CANDIDATE_NONCE": request.nonce,
                "AGENT_WORK_CANDIDATE_ISSUE_IID": str(request.issue_iid),
                "AGENT_WORK_CANDIDATE_TREE": TREE,
                "AGENT_WORK_CANDIDATE_ROLE": "candidate-preview",
            },
        )

        async def check_stdio() -> None:
            async with Client(parameters) as client:
                listed = await client.list_tools()
                assert {tool.name for tool in listed.tools} == TOOLS
                status = await client.call_tool("candidate_status", {})
                inventory = await client.call_tool("candidate_changed_paths", {})
                invalid_identity = await client.call_tool(
                    "candidate_status", {"nonce": request.nonce}
                )
                invalid_range = await client.call_tool(
                    "candidate_read",
                    {
                        "path": "docs/a.md",
                        "side": "new",
                        "offset": 0,
                        "length": GIT_READ_MAX_CHUNK_BYTES + 1,
                    },
                )
                assert status.is_error is False
                assert status.structured_content["state"] == "terminal-success"
                assert inventory.is_error is False
                assert inventory.structured_content["changed_paths"] == ["docs/a.md"]
                assert invalid_identity.is_error is True
                assert invalid_range.is_error is True

        asyncio.run(asyncio.wait_for(check_stdio(), timeout=10))
        assert calls == [
            ("stage", None, None),
            ("status", TREE, None),
            ("read", TREE, {"mode": "changed_paths"}),
        ]
    finally:
        captured = session.close_and_capture()
    assert captured == TREE
    assert not socket_root.exists()


def test_candidate_review_reset_requires_validated_exact_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    request = WorkerResultBrokerRequest(
        nonce="0123456789abcdef" * 2,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha=BASE,
    )
    client_request = WorkerResultBrokerClientRequest(
        nonce=request.nonce, issue_iid=request.issue_iid
    )

    def action(
        kind: str, _tree: str | None, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        return {
            "candidate_tree": TREE,
            "state": "staged" if kind == "stage" else "terminal-success",
        }

    session = create_candidate_broker(
        action,
        request=request,
        timeout_seconds=10,
        temporary_directory=tmp_path_factory.mktemp("mr"),
    )
    socket_root = session.socket_path.parent
    try:
        with pytest.raises(WorkerProtocolError):
            session.begin_review(TREE)
        request_candidate_validation(
            session.socket_path, client_request, action="stage"
        )
        with pytest.raises(WorkerProtocolError):
            session.begin_review(TREE)
        request_candidate_validation(
            session.socket_path,
            client_request,
            action="validate",
            candidate_tree=TREE,
        )
        with pytest.raises(WorkerProtocolError):
            session.begin_review("c" * 40)
        session.begin_review(TREE)
        with pytest.raises(WorkerProtocolError):
            request_candidate_validation(
                session.socket_path,
                client_request,
                action=cast(Any, "review"),
                candidate_tree=TREE,
                selector={
                    "verdict": "approve",
                    "summary": "No findings",
                    "findings": [],
                },
            )
    finally:
        with suppress(WorkerProtocolError):
            session.close_and_capture()
    assert not socket_root.exists()


def test_candidate_mcp_preserves_safe_broker_error_without_private_detail() -> None:
    def gateway(
        _action: str, _tree: str, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        raise WorkerProtocolError(
            "candidate_broker", f"request is not permitted: {PRIVATE_NONCE}"
        )

    async def call(client: Any) -> Any:
        return await client.call_tool("candidate_status", {})

    result = _client_round_trip(_server(gateway), call)
    assert result.is_error is True
    rendered = json.dumps(result.model_dump())
    assert "candidate_broker" in rendered
    assert PRIVATE_NONCE not in rendered


def test_candidate_mcp_rejects_unbounded_gateway_output() -> None:
    def gateway(
        _action: str, tree: str, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        return {
            "candidate_tree": tree,
            "state": "terminal-success",
            "secret": "x" * 1_100_000,
        }

    async def call(client: Any) -> Any:
        return await client.call_tool("candidate_status", {})

    result = _client_round_trip(_server(gateway), call)
    assert result.is_error is True
    assert len(json.dumps(result.model_dump()).encode()) < 4096


def test_candidate_mcp_root_and_template_source_match(
    project_root: Path, template_dir: Path
) -> None:
    root = project_root / "src" / "agent_work" / "mcp_server.py"
    mirror = template_dir / "src" / "agent_work" / "mcp_server.py"
    assert root.read_bytes() == mirror.read_bytes()


def test_candidate_mcp_dependency_parses_in_root_and_template(
    project_root: Path, template_dir: Path
) -> None:
    import tomllib

    rendered_template = (
        (template_dir / "pyproject.toml.jinja")
        .read_text()
        .replace("{{ project_slug }}", "sample_project")
        .replace("{{ description }}", "A sample project")
    )
    for content in ((project_root / "pyproject.toml").read_text(), rendered_template):
        dependencies = tomllib.loads(content)["project"]["dependencies"]
        assert dependencies.count("mcp>=2,<3") == 1
