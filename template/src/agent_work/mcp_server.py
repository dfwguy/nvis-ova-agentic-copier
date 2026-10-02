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
from base64 import b64decode, b64encode
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, cast

from mcp.server import MCPServer
from mcp.types import (
    SERVER_INFO_META_KEY,
    CallToolResult,
    Implementation,
    TextContent,
    Tool,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .candidate_boundary import (
    GIT_READ_MAX_CHUNK_BYTES,
    GIT_READ_MAX_FILE_BYTES,
    GIT_READ_MAX_PATHS,
    GIT_READ_MAX_TOTAL_BYTES,
)
from .domain import ErrorCode, WorkflowLane, WorkflowPhase
from .file_patch import (
    DocumentationPatch,
    DocumentationPatchRequest,
    DocumentationPatchResult,
)
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


_GitOID = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
_FileSize = Annotated[int, Field(ge=0, le=GIT_READ_MAX_FILE_BYTES)]


class _ClosedReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ReadSelector(_ClosedReadModel):
    path: str = Field(min_length=1)
    side: Literal["old", "new"]
    offset: int = Field(ge=0)
    length: int = Field(gt=0, le=GIT_READ_MAX_CHUNK_BYTES)

    @field_validator("path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or str(path) != value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(char in value for char in ("\\", "\n", "\r", "\x00"))
        ):
            raise ValueError("invalid candidate path")
        return value


class _ReadBatch(_ClosedReadModel):
    reads: list[_ReadSelector] = Field(min_length=1)


class _ReadIdentity(_ClosedReadModel):
    accepted: Literal[True]
    candidate_tree: _GitOID
    base_sha: _GitOID
    base_tree: _GitOID
    ledger_revision: int = Field(ge=0)
    source_branch: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    issue_iid: int = Field(gt=0)
    project_id: int = Field(gt=0)
    commit_sha: _GitOID | None = None
    committed_tree: _GitOID | None = None

    @field_validator("accepted", mode="before")
    @classmethod
    def _accepted_response(cls, value: object) -> object:
        if value is not True:
            raise ValueError("candidate response was not accepted")
        return value

    @model_validator(mode="after")
    def _paired_commit(self) -> _ReadIdentity:
        if (self.commit_sha is None) != (self.committed_tree is None) or (
            self.committed_tree is not None
            and self.committed_tree != self.candidate_tree
        ):
            raise ValueError("invalid committed identity")
        return self


class _ReadFile(_ClosedReadModel):
    path: str
    old_mode: Literal["100644", "100755"] | None
    old_oid: _GitOID | None
    old_size: _FileSize | None
    new_mode: Literal["100644", "100755"] | None
    new_oid: _GitOID | None
    new_size: _FileSize | None

    @field_validator("path")
    @classmethod
    def _canonical_path(cls, value: str) -> str:
        return _ReadSelector._canonical_path(value)

    @model_validator(mode="after")
    def _side_metadata(self) -> _ReadFile:
        for side in ("old", "new"):
            values = tuple(
                getattr(self, f"{side}_{key}") for key in ("mode", "oid", "size")
            )
            if any(value is None for value in values) and any(
                value is not None for value in values
            ):
                raise ValueError("incomplete side metadata")
        if self.old_oid is None and self.new_oid is None:
            raise ValueError("absent changed file")
        if self.old_mode == self.new_mode and self.old_oid == self.new_oid:
            raise ValueError("unchanged file")
        return self


class _ReadInventory(_ReadIdentity):
    changed_paths: list[str] = Field(min_length=1, max_length=GIT_READ_MAX_PATHS)
    files: list[_ReadFile] = Field(min_length=1, max_length=GIT_READ_MAX_PATHS)

    @model_validator(mode="after")
    def _complete_inventory(self) -> _ReadInventory:
        if self.changed_paths != sorted(
            set(self.changed_paths)
        ) or self.changed_paths != [file.path for file in self.files]:
            raise ValueError("invalid inventory paths")
        sizes: dict[str, int] = {}
        total = 0
        for file in self.files:
            for side in ("old", "new"):
                oid = getattr(file, f"{side}_oid")
                size = getattr(file, f"{side}_size")
                if oid is not None:
                    if oid in sizes and sizes[oid] != size:
                        raise ValueError("inconsistent object size")
                    sizes[oid] = size
                    total += size
        if total > GIT_READ_MAX_TOTAL_BYTES:
            raise ValueError("inventory exceeds existing byte limit")
        return self


class _ReadRange(_ReadIdentity, _ReadSelector):
    oid: _GitOID
    size: _FileSize
    data_base64: str


def _read_identity(value: _ReadIdentity) -> dict[str, object]:
    return {
        key: getattr(value, key)
        for key in _ReadIdentity.model_fields
        if key in value.model_fields_set
    }


def group_candidate_read_results(
    inventory: dict[str, object],
    selectors: tuple[dict[str, object], ...],
    results: tuple[dict[str, object], ...],
    *,
    candidate_tree: str,
) -> dict[str, object]:
    """Validate independent range receipts and pool equal bytes in request order."""
    described = _ReadInventory.model_validate(inventory)
    identity = _read_identity(described)
    if (
        described.candidate_tree != candidate_tree
        or not selectors
        or len(selectors) != len(results)
    ):
        raise ValueError("invalid grouped candidate identity or count")
    files = {file.path: file for file in described.files}
    contents: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    byte_indices: dict[bytes, int] = {}
    range_bytes: dict[tuple[str, int, int], bytes] = {}
    for index, (raw_selector, raw_result) in enumerate(
        zip(selectors, results, strict=True)
    ):
        selector = _ReadSelector.model_validate(raw_selector)
        result = _ReadRange.model_validate(raw_result)
        if _read_identity(result) != identity or any(
            getattr(result, key) != getattr(selector, key)
            for key in _ReadSelector.model_fields
        ):
            raise ValueError("range identity or selector mismatch")
        file = files.get(selector.path)
        if (
            file is None
            or getattr(file, f"{selector.side}_oid") != result.oid
            or getattr(file, f"{selector.side}_size") != result.size
            or selector.offset + selector.length > result.size
        ):
            raise ValueError("range does not match inventory")
        decoded = b64decode(result.data_base64, validate=True)
        if len(decoded) != selector.length:
            raise ValueError("range byte length mismatch")
        context = (result.oid, selector.offset, selector.length)
        if context in range_bytes and range_bytes[context] != decoded:
            raise ValueError("inconsistent object range bytes")
        range_bytes[context] = decoded
        if decoded not in byte_indices:
            byte_indices[decoded] = len(contents)
            contents.append({"data_base64": b64encode(decoded).decode("ascii")})
        bindings.append(
            selector.model_dump()
            | {
                "request_index": index,
                "oid": result.oid,
                "size": result.size,
                "content_index": byte_indices[decoded],
            }
        )
    return identity | {
        "inventory": {
            "changed_paths": described.changed_paths,
            "files": [file.model_dump() for file in described.files],
        },
        "bindings": bindings,
        "contents": contents,
    }


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
        listed = []
        for tool in tools:
            if tool.name not in self._allowed_tools:
                continue
            schema = {**tool.input_schema, "additionalProperties": False}
            if tool.name == "candidate_read":
                batch_schema = _ReadBatch.model_json_schema()
                schema["properties"] = (
                    _ReadSelector.model_json_schema()["properties"]
                    | batch_schema["properties"]
                )
                schema["$defs"] = batch_schema["$defs"]
                schema.pop("required", None)
                schema["oneOf"] = [
                    {
                        "required": ["reads"],
                        "not": {
                            "anyOf": [
                                {"required": [key]}
                                for key in ("path", "side", "offset", "length")
                            ]
                        },
                    },
                    {
                        "required": ["path", "side", "offset", "length"],
                        "not": {"required": ["reads"]},
                    },
                ]
            listed.append(tool.model_copy(update={"input_schema": schema}))
        return listed

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
            try:
                if set(arguments) == {"reads"}:
                    _ReadBatch.model_validate(arguments)
                else:
                    _ReadSelector.model_validate(arguments)
            except ValidationError:
                valid = False
            else:
                valid = True
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
        path: str | None = None,
        side: Literal["old", "new"] | None = None,
        offset: Annotated[int | None, Field(strict=True, ge=0)] = None,
        length: Annotated[
            int | None, Field(strict=True, gt=0, le=GIT_READ_MAX_CHUNK_BYTES)
        ] = None,
        reads: list[_ReadSelector] | None = None,
    ) -> CallToolResult:
        """Read one range, or pool independently verified ranges with full bindings."""
        if reads is None:
            return read(
                "read", {"path": path, "side": side, "offset": offset, "length": length}
            )
        try:
            selectors = tuple(selector.model_dump() for selector in reads)
            # Every emitted binding necessarily contains these fields. This
            # lower bound rejects impossible output before consuming broker calls.
            minimum: dict[str, object] = {
                "bindings": [
                    selector | {"request_index": index}
                    for index, selector in enumerate(selectors)
                ]
            }
            # Prepopulate the SDK's public server identity stamp so the runner
            # adds no unchecked metadata to either complete result body.
            stamp = Implementation(
                name=server.name,
                version=server.version,
                title=server.title,
                description=server.description,
                website_url=server.website_url,
                icons=server.icons,
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
            minimum_result = CallToolResult(
                content=[TextContent(text=json.dumps(minimum, ensure_ascii=True))],
                structured_content=minimum,
                _meta={SERVER_INFO_META_KEY: stamp},
            )
            if (
                len(
                    minimum_result.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode("utf-8")
                )
                > _MAX_RESPONSE_BYTES
            ):
                raise ValueError("mandatory bindings exceed MCP limit")
            inventory = gateway("read", candidate_tree, {"mode": "changed_paths"})
            described = _ReadInventory.model_validate(inventory)
            if described.candidate_tree != candidate_tree:
                raise ValueError("inventory candidate identity mismatch")
            results = tuple(
                gateway("read", candidate_tree, selector) for selector in selectors
            )
            response = cast(
                dict[str, object],
                _public_value(
                    group_candidate_read_results(
                        inventory, selectors, results, candidate_tree=candidate_tree
                    )
                ),
            )
            result = CallToolResult(
                content=[TextContent(text=json.dumps(response, ensure_ascii=True))],
                structured_content=response,
                _meta={SERVER_INFO_META_KEY: stamp},
            )
            if (
                len(
                    result.model_dump_json(by_alias=True, exclude_none=True).encode(
                        "utf-8"
                    )
                )
                > _MAX_RESPONSE_BYTES
            ):
                raise ValueError("grouped response body exceeds MCP limit")
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
        return result

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


class _DocumentationPatchMCPServer(MCPServer):
    def __init__(self) -> None:
        super().__init__("worker-documentation-patch")

    async def list_tools(self) -> list[Tool]:
        """Advertise the single closed documentation replacement tool."""
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
        try:
            if name != "worker_documentation_patch":
                raise ValueError("invalid tool")
            DocumentationPatchRequest.model_validate_json(json.dumps(arguments))
        except (ValueError, TypeError):
            return CallToolResult(
                content=[
                    TextContent(text="documentation_patch: invalid tool or arguments")
                ],
                is_error=True,
            )
        return cast(CallToolResult, await super().call_tool(name, arguments, context))


def create_worker_documentation_patch_mcp_server(
    gateway: Callable[[DocumentationPatchRequest], DocumentationPatchResult],
) -> MCPServer:
    """Expose one host-composed private documentation patch service."""
    server = _DocumentationPatchMCPServer()

    @server.tool()
    def worker_documentation_patch(
        patches: list[DocumentationPatch], *, apply: bool = False
    ) -> CallToolResult:
        """Preview or apply all members of the host-reviewed documentation grant."""
        try:
            request = DocumentationPatchRequest(patches=tuple(patches), apply=apply)
            result = DocumentationPatchResult.model_validate_json(
                gateway(request).model_dump_json()
            )
        except (OSError, ValueError, TypeError, WorkerProtocolError):
            return CallToolResult(
                content=[TextContent(text="documentation_patch: submission failed")],
                is_error=True,
            )
        public = result.model_dump(mode="json")
        return CallToolResult(
            content=[TextContent(text=json.dumps(public))],
            structured_content=public,
            is_error=False,
        )

    return server
