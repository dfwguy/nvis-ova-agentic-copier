# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bounded, read-only MCP view of one controller-owned candidate."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, cast

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .candidate_boundary import GIT_READ_MAX_CHUNK_BYTES
from .worker import (
    CandidateOperation,
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    candidate_tools_for_role,
    request_candidate_validation,
)

CandidateGateway = Callable[
    [CandidateOperation, str, dict[str, object] | None], dict[str, object]
]
_PRIVATE_FIELDS = frozenset({"nonce", "issue_iid", "project_id"})
_MAX_RESPONSE_BYTES = 900_000
_TOOL_NAMES = (
    "candidate_status",
    "candidate_changed_paths",
    "candidate_read",
    "candidate_submit_review",
)


class _ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    severity: Literal["critical", "major", "minor"]
    path: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=2048)

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or any(character in value for character in ("\n", "\r", "\x00"))
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or str(path) != value
        ):
            raise ValueError("path must be normalized and repository-relative")
        return value


class _ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["approve", "reject"]
    summary: str = Field(min_length=1, max_length=2048)
    findings: list[_ReviewFinding] = Field(max_length=32)

    @model_validator(mode="after")
    def _approve_has_no_blocking_finding(self) -> _ReviewSubmission:
        if self.verdict == "approve" and any(
            finding.severity in {"critical", "major"} for finding in self.findings
        ):
            raise ValueError("blocking findings require rejection")
        return self


class _CandidateMCPServer(MCPServer):
    def __init__(self, role: str) -> None:
        super().__init__("candidate")
        self._allowed_tools = frozenset(candidate_tools_for_role(role))

    async def list_tools(self) -> list[Tool]:
        """Advertise only role-granted tools with closed argument schemas."""
        tools = await super().list_tools()
        return [
            tool.model_copy(
                update={
                    "input_schema": {**tool.input_schema, "additionalProperties": False}
                }
            )
            for tool in tools
            if tool.name in self._allowed_tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Any = None,
    ) -> CallToolResult:
        if name not in self._allowed_tools:
            return CallToolResult(
                content=[TextContent(text="candidate_broker: invalid tool")],
                is_error=True,
            )
        if name == "candidate_read":
            offset = arguments.get("offset")
            length = arguments.get("length")
            valid = (
                set(arguments) == {"path", "side", "offset", "length"}
                and isinstance(arguments.get("path"), str)
                and bool(arguments["path"])
                and not PurePosixPath(arguments["path"]).is_absolute()
                and str(PurePosixPath(arguments["path"])) == arguments["path"]
                and all(
                    part not in {"", ".", ".."} for part in arguments["path"].split("/")
                )
                and not any(
                    char in arguments["path"] for char in ("\\", "\n", "\r", "\x00")
                )
                and arguments.get("side") in {"old", "new"}
                and type(offset) is int
                and offset >= 0
                and type(length) is int
                and 0 < length <= GIT_READ_MAX_CHUNK_BYTES
            )
        elif name == "candidate_submit_review":
            try:
                _ReviewSubmission.model_validate(arguments)
            except ValidationError:
                valid = False
            else:
                valid = True
        else:
            valid = arguments == {}
        if not valid:
            return CallToolResult(
                content=[TextContent(text="candidate_broker: invalid arguments")],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def _public_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _public_value(item)
            for key, item in value.items()
            if isinstance(key, str) and key not in _PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return value


def create_candidate_mcp_server(
    gateway: CandidateGateway, candidate_tree: str, *, role: str = "candidate-preview"
) -> MCPServer:
    """Expose bounded candidate reads and one advisory review submission."""
    server = _CandidateMCPServer(role)

    def read(
        action: CandidateOperation, selector: dict[str, object] | None = None
    ) -> CallToolResult:
        try:
            response = _public_value(gateway(action, candidate_tree, selector))
            if not isinstance(response, dict):
                raise TypeError("invalid gateway response")
            encoded = json.dumps(response, ensure_ascii=True)
            size = len(encoded.encode())
            if size > _MAX_RESPONSE_BYTES:
                raise ValueError("gateway response exceeds MCP limit")
        except (
            WorkerProtocolError,
            ValueError,
            TypeError,
            OverflowError,
            RecursionError,
        ):
            return CallToolResult(
                content=[TextContent(text="candidate_broker: read failed")],
                is_error=True,
            )
        return CallToolResult(
            content=[TextContent(text=encoded)],
            structured_content=response,
        )

    @server.tool()
    def candidate_status() -> CallToolResult:
        """Read validation state for the bound candidate."""
        return read("status")

    @server.tool()
    def candidate_changed_paths() -> CallToolResult:
        """List changed paths and exact Git side metadata."""
        return read("read", {"mode": "changed_paths"})

    @server.tool()
    def candidate_read(
        path: str,
        side: Literal["old", "new"],
        offset: Annotated[int, Field(strict=True, ge=0)],
        length: Annotated[int, Field(strict=True, gt=0, le=GIT_READ_MAX_CHUNK_BYTES)],
    ) -> CallToolResult:
        """Read one independent byte range from a changed Git object."""
        return read(
            "read",
            {
                "path": path,
                "side": side,
                "offset": offset,
                "length": length,
            },
        )

    @server.tool()
    def candidate_submit_review(
        verdict: Literal["approve", "reject"],
        summary: str,
        findings: list[_ReviewFinding],
    ) -> CallToolResult:
        """Submit one bounded advisory exact-candidate preview."""
        return read(
            "review",
            {
                "verdict": verdict,
                "summary": summary,
                "findings": [finding.model_dump() for finding in findings],
            },
        )

    return server


class _WorkerReadMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("worker-read")

    async def list_tools(self) -> list[Tool]:
        """Advertise only fixed reads with closed argument schemas."""
        return [
            tool.model_copy(
                update={
                    "input_schema": {**tool.input_schema, "additionalProperties": False}
                }
            )
            for tool in await super().list_tools()
        ]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Any = None
    ) -> CallToolResult:
        if (
            name not in {"worker_context", "worker_issue", "worker_ledger"}
            or arguments != {}
        ):
            return CallToolResult(
                content=[TextContent(text="worker_read: invalid tool or arguments")],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def create_worker_read_mcp_server(
    gateway: Callable[[str], dict[str, object]],
) -> MCPServer:
    """Expose three fixed worker reads on a separate MCP server."""
    server = _WorkerReadMCPServer()

    def read(action: str) -> CallToolResult:
        try:
            response = gateway(action)
            encoded = json.dumps(response, ensure_ascii=True)
            if len(encoded.encode()) > 131_072:
                raise ValueError("worker read too large")
        except (WorkerProtocolError, OSError, ValueError, TypeError, OverflowError):
            return CallToolResult(
                content=[TextContent(text="worker_read: read failed")],
                is_error=True,
            )
        return CallToolResult(
            content=[TextContent(text=encoded)], structured_content=response
        )

    @server.tool()
    def worker_context() -> CallToolResult:
        """Read bounded workflow state for this worker."""
        return read("context")

    @server.tool()
    def worker_issue() -> CallToolResult:
        """Read bounded issue title and description."""
        return read("issue")

    @server.tool()
    def worker_ledger() -> CallToolResult:
        """Read bounded ledger identity and workflow state."""
        return read("ledger")

    return server


def main() -> None:
    """Serve stdio MCP using private identity supplied by the controller."""
    try:
        socket_path = Path(os.environ["AGENT_WORK_CANDIDATE_SOCKET"])
        request = WorkerResultBrokerClientRequest(
            nonce=os.environ["AGENT_WORK_CANDIDATE_NONCE"],
            issue_iid=int(os.environ["AGENT_WORK_CANDIDATE_ISSUE_IID"]),
        )
        tree = os.environ["AGENT_WORK_CANDIDATE_TREE"]
        role = os.environ["AGENT_WORK_CANDIDATE_ROLE"]
        candidate_tools_for_role(role)
    except (KeyError, ValueError):
        raise RuntimeError("candidate MCP setup failed") from None

    def gateway(
        action: CandidateOperation,
        candidate_tree: str,
        selector: dict[str, object] | None,
    ) -> dict[str, object]:
        return request_candidate_validation(
            socket_path,
            request,
            action=action,
            candidate_tree=candidate_tree,
            selector=selector,
        )

    create_candidate_mcp_server(gateway, tree, role=role).run(transport="stdio")


if __name__ == "__main__":
    main()
