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
from collections.abc import Callable, Mapping
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
from .domain import ErrorCode, WorkflowLane, WorkflowPhase
from .models import BoundaryModel, MergeRequestDraft, SplitPlan
from .red_checkpoint import RedCheckpointRequest
from .serial import _worker_validation_projection
from .worker import (
    CandidateOperation,
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    WorkerResultBuildInput,
    candidate_tools_for_role,
    is_safe_worker_result_diagnostic,
    request_candidate_validation,
)
from .worker_read import _public_git_value, _validation_selector, request_worker_read

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


class _WorkerGitMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("worker-git")

    async def list_tools(self) -> list[Tool]:
        """Advertise only the four closed worker Git operations."""
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
        valid = (
            name in {"worker_git_status", "worker_git_stage_candidate"}
            and arguments == {}
        ) or (
            name in {"worker_git_candidate_status", "worker_git_commit_candidate"}
            and set(arguments) == {"candidate_tree"}
            and isinstance(arguments["candidate_tree"], str)
            and len(arguments["candidate_tree"]) == 40
            and all(char in "0123456789abcdef" for char in arguments["candidate_tree"])
        )
        if not valid:
            return CallToolResult(
                content=[TextContent(text="worker_git: invalid tool or arguments")],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def create_worker_git_mcp_server(
    gateway: Callable[[str, str | None], dict[str, object]],
) -> MCPServer:
    """Expose the separate candidate-bound worker Git MCP surface."""
    server = _WorkerGitMCPServer()

    def dispatch(action: str, tree: str | None = None) -> CallToolResult:
        try:
            raw = gateway(action, tree)
            if len(json.dumps(raw, ensure_ascii=True).encode()) > 131_072:
                raise ValueError("worker git response too large")
            response = cast(dict[str, object], _public_git_value(raw))
            encoded = json.dumps(response, ensure_ascii=True)
            if len(encoded.encode()) > 131_072:
                raise ValueError("worker git response too large")
        except (WorkerProtocolError, OSError, ValueError, TypeError, OverflowError):
            return CallToolResult(
                content=[TextContent(text="worker_git: request failed")],
                is_error=True,
            )
        return CallToolResult(
            content=[TextContent(text=encoded)], structured_content=response
        )

    @server.tool()
    def worker_git_status() -> CallToolResult:
        """Read bounded worktree change status."""
        return dispatch("git_status")

    @server.tool()
    def worker_git_stage_candidate() -> CallToolResult:
        """Ask the controller to stage its current candidate."""
        return dispatch("git_stage_candidate")

    @server.tool()
    def worker_git_candidate_status(candidate_tree: str) -> CallToolResult:
        """Read status for the exact tree staged in this session."""
        return dispatch("git_candidate_status", candidate_tree)

    @server.tool()
    def worker_git_commit_candidate(candidate_tree: str) -> CallToolResult:
        """Ask the controller to commit the exact staged coding candidate."""
        return dispatch("git_commit_candidate", candidate_tree)

    return server


class _WorkerValidationMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("worker-validation")

    async def list_tools(self) -> list[Tool]:
        """Advertise only three closed Judge operations."""
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
        valid = (
            name in {"worker_validation_validate", "worker_validation_status"}
            and arguments == {}
        ) or (
            name == "worker_validation_evidence"
            and set(arguments) == {"selector"}
            and _validation_selector(arguments.get("selector"))
        )
        if not valid:
            return CallToolResult(
                content=[
                    TextContent(text="worker_validation: invalid tool or arguments")
                ],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def create_worker_validation_mcp_server(
    gateway: Callable[[str, str, dict[str, object] | None], dict[str, object]],
    candidate_tree: str,
) -> MCPServer:
    """Expose a separate Judge-only exact-candidate validation surface."""
    server = _WorkerValidationMCPServer()

    def dispatch(
        action: str, selector: dict[str, object] | None = None
    ) -> CallToolResult:
        try:
            raw = gateway(action, candidate_tree, selector)
            response = _worker_validation_projection(raw, candidate_tree, selector)
            encoded = json.dumps(response, ensure_ascii=True)
            if len(encoded.encode()) > 131_072:
                raise ValueError("worker validation response too large")
        except (WorkerProtocolError, OSError, ValueError, TypeError, OverflowError):
            return CallToolResult(
                content=[TextContent(text="worker_validation: request failed")],
                is_error=True,
            )
        return CallToolResult(
            content=[TextContent(text=encoded)], structured_content=response
        )

    @server.tool()
    def worker_validation_validate() -> CallToolResult:
        """Run controller validation of this staged candidate."""
        return dispatch("validate")

    @server.tool()
    def worker_validation_status() -> CallToolResult:
        """Read this candidate's validation state."""
        return dispatch("status")

    @server.tool()
    def worker_validation_evidence(selector: dict[str, object]) -> CallToolResult:
        """Read one bounded summary or evidence page."""
        return dispatch("evidence", selector)

    return server


class _PublicSuggestCloseEvidence(BoundaryModel):
    """Close suggestion data with the controller-owned issue identity omitted."""

    reason: Literal[
        "duplicate", "superseded", "already-satisfied", "no-longer-applicable"
    ]
    evidence: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("evidence")
    @classmethod
    def _valid_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() or len(item) > 2048 for item in value):
            raise ValueError("evidence must contain nonempty bounded items")
        return value


class WorkerHandoffIntent(BoundaryModel):
    """Public worker intent; private broker and issue identity are never inputs."""

    workflow_lane: WorkflowLane
    result_kind: Literal[
        "ready_to_publish",
        "red_checkpoint_ready",
        "retryable",
        "needs_human",
        "split_required",
        "suggest_close",
    ]
    rejection_route: WorkflowPhase | None = None
    change_summary: str = Field(min_length=1, max_length=20_000)
    next_action: str = Field(min_length=1, max_length=2_000)
    merge_request_draft: MergeRequestDraft | None = None
    error_code: ErrorCode | None = None
    redacted_summary: str = ""
    split_plan: SplitPlan | None = None
    suggest_close_evidence: _PublicSuggestCloseEvidence | None = None
    red_checkpoint_request: RedCheckpointRequest | None = None


class _WorkerHandoffMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("worker-handoff")

    async def list_tools(self) -> list[Tool]:
        """Advertise only one closed result-submission tool."""
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
        if name != "worker_submit_result" or set(arguments) != {"intent"}:
            return CallToolResult(
                content=[TextContent(text="worker_handoff: invalid tool or arguments")],
                is_error=True,
            )
        try:
            WorkerHandoffIntent.model_validate(arguments["intent"])
        except ValidationError:
            return CallToolResult(
                content=[TextContent(text="worker_handoff: invalid arguments")],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def create_worker_handoff_mcp_server(
    gateway: Callable[[WorkerResultBuildInput], dict[str, object]], *, issue_iid: int
) -> MCPServer:
    """Submit typed worker intent through the controller-owned result broker."""
    if issue_iid <= 0:
        raise ValueError("issue identity must be positive")
    server = _WorkerHandoffMCPServer()

    @server.tool()
    def worker_submit_result(intent: WorkerHandoffIntent) -> CallToolResult:
        """Submit one result for the controller-bound issue."""
        try:
            payload = intent.model_dump(exclude_none=True)
            close_evidence = payload.get("suggest_close_evidence")
            if isinstance(close_evidence, dict):
                close_evidence["issue_iid"] = issue_iid
            response = gateway(WorkerResultBuildInput.model_validate(payload))
            if not isinstance(response, dict) or response.get("accepted") is not True:
                raise ValueError("broker did not accept result")
        except WorkerProtocolError as error:
            diagnostic = str(error)
            message = (
                diagnostic
                if is_safe_worker_result_diagnostic(diagnostic)
                else "worker_handoff: request failed"
            )
            return CallToolResult(content=[TextContent(text=message)], is_error=True)
        except (ValidationError, OSError, ValueError, TypeError, OverflowError):
            return CallToolResult(
                content=[TextContent(text="worker_handoff: request failed")],
                is_error=True,
            )
        accepted = {"accepted": True}
        return CallToolResult(
            content=[TextContent(text=json.dumps(accepted))],
            structured_content=accepted,
        )

    return server


class WorkerReadMCPLaunchConfiguration(BaseModel):
    """Validate private worker-read launch fields without changing their values."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    socket_path: str = Field(min_length=1)
    nonce: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")

    @field_validator("socket_path")
    @classmethod
    def _socket_without_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("socket path contains NUL")
        return value


def parse_mcp_launch_configuration(
    environment: Mapping[str, str],
) -> WorkerReadMCPLaunchConfiguration | None:
    """Select candidate mode or validate explicit private worker-read settings."""
    mode = environment.get("AGENT_WORK_MCP_MODE", "candidate")
    if mode == "candidate":
        return None
    if mode != "worker-read":
        raise ValueError("MCP setup failed") from None
    try:
        return WorkerReadMCPLaunchConfiguration(
            socket_path=environment["AGENT_WORK_WORKER_READ_SOCKET"],
            nonce=environment["AGENT_WORK_WORKER_READ_NONCE"],
        )
    except (KeyError, ValidationError):
        raise ValueError("MCP setup failed") from None


def main(
    *,
    environment: Mapping[str, str] | None = None,
    run_server: Callable[[MCPServer], None] | None = None,
) -> None:
    """Serve stdio MCP using private identity supplied by the controller."""
    environment = os.environ if environment is None else environment
    try:
        configuration = parse_mcp_launch_configuration(environment)
    except ValueError:
        raise RuntimeError("MCP setup failed") from None

    if configuration is not None:

        def worker_gateway(action: str) -> dict[str, object]:
            return request_worker_read(
                Path(configuration.socket_path), configuration.nonce, action
            )

        server = create_worker_read_mcp_server(worker_gateway)
        if run_server is None:
            server.run(transport="stdio")
        else:
            run_server(server)
        return

    try:
        socket_path = Path(environment["AGENT_WORK_CANDIDATE_SOCKET"])
        request = WorkerResultBrokerClientRequest(
            nonce=environment["AGENT_WORK_CANDIDATE_NONCE"],
            issue_iid=int(environment["AGENT_WORK_CANDIDATE_ISSUE_IID"]),
        )
        tree = environment["AGENT_WORK_CANDIDATE_TREE"]
        role = environment["AGENT_WORK_CANDIDATE_ROLE"]
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

    server = create_candidate_mcp_server(gateway, tree, role=role)
    if run_server is None:
        server.run(transport="stdio")
    else:
        run_server(server)


if __name__ == "__main__":
    main()
