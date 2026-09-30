# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Contracts for a typed worker-result MCP handoff with private issue identity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from agent_work import mcp_server
from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.worker import (
    WorkerProtocolError,
    WorkerResultBrokerClientRequest,
    WorkerResultBrokerRequest,
    WorkerResultBuildInput,
    create_worker_result_broker,
    is_safe_worker_result_diagnostic,
    submit_worker_result_to_broker,
)

INTENT: dict[str, object] = {
    "workflow_lane": "coding",
    "result_kind": "needs_human",
    "change_summary": "Human review is required.",
    "next_action": "Review the remaining decision.",
}
CLOSE_INTENT: dict[str, object] = {
    **INTENT,
    "result_kind": "suggest_close",
    "suggest_close_evidence": {
        "reason": "already-satisfied",
        "evidence": ["The requested behavior is already present."],
    },
}
PRIVATE_NONCE = "0123456789abcdef" * 2


def _server(
    gateway: Callable[[WorkerResultBuildInput], dict[str, object]],
    *,
    issue_iid: int = 81,
) -> Any:
    return mcp_server.create_worker_handoff_mcp_server(gateway, issue_iid=issue_iid)


def _round_trip(server: Any, action: Callable[[Any], Any]) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await action(client)

    return asyncio.run(run())


def test_handoff_advertises_one_closed_schema_without_private_identity() -> None:
    server = _server(lambda _intent: {"accepted": True})

    async def list_tools(client: Any) -> Any:
        return await client.list_tools()

    listed = _round_trip(server, list_tools)
    assert [tool.name for tool in listed.tools] == ["worker_submit_result"]
    schema = listed.tools[0].input_schema
    assert set(schema["properties"]) == {"intent"}
    assert schema["required"] == ["intent"]
    assert schema["additionalProperties"] is False
    encoded = json.dumps(schema)
    assert all(
        name in encoded
        for name in (
            "workflow_lane",
            "result_kind",
            "suggest_close",
            "suggest_close_evidence",
        )
    )
    assert not any(
        f'"{private}":' in encoded
        for private in (
            "nonce",
            "issue_iid",
            "project_id",
            "checkout",
            "source_sha",
            "candidate_tree",
            "protocol_version",
        )
    )


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("worker_publish", {"intent": INTENT}),
        ("worker_submit_result", {"intent": INTENT, "nonce": PRIVATE_NONCE}),
        ("worker_submit_result", {"intent": {**INTENT, "issue_iid": 81}}),
        ("worker_submit_result", {"intent": {**INTENT, "protocol_version": 2}}),
        ("worker_submit_result", {"intent": {**INTENT, "change_summary": ""}}),
        (
            "worker_submit_result",
            {
                "intent": {
                    **CLOSE_INTENT,
                    "suggest_close_evidence": {
                        "reason": "already-satisfied",
                        "evidence": ["The requested behavior is already present."],
                        "issue_iid": 81,
                    },
                }
            },
        ),
    ],
)
def test_handoff_rejects_unknown_tools_and_fields_before_gateway(
    tool: str, arguments: dict[str, object]
) -> None:
    calls: list[WorkerResultBuildInput] = []

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        calls.append(intent)
        return {"accepted": True}

    result = asyncio.run(_server(gateway).call_tool(tool, arguments))

    assert result.is_error is True
    assert calls == []


@pytest.mark.parametrize(
    "kind",
    [
        "ready_to_publish",
        "red_checkpoint_ready",
        "retryable",
        "needs_human",
        "split_required",
        "suggest_close",
    ],
)
def test_handoff_accepts_each_builder_result_kind(kind: str) -> None:
    calls: list[WorkerResultBuildInput] = []

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        calls.append(intent)
        return {"accepted": True, "nonce": PRIVATE_NONCE, "issue_iid": 81}

    intent = {
        **(CLOSE_INTENT if kind == "suggest_close" else INTENT),
        "result_kind": kind,
    }

    async def submit(client: Any) -> Any:
        return await client.call_tool("worker_submit_result", {"intent": intent})

    result = _round_trip(_server(gateway), submit)

    assert result.is_error is not True
    assert len(calls) == 1
    assert isinstance(calls[0], WorkerResultBuildInput)
    assert calls[0].result_kind == kind
    if kind == "suggest_close":
        assert calls[0].suggest_close_evidence is not None
        assert calls[0].suggest_close_evidence.issue_iid == 81
        assert calls[0].suggest_close_evidence.reason == "already-satisfied"
        assert calls[0].suggest_close_evidence.evidence == (
            "The requested behavior is already present.",
        )
    assert result.structured_content == {"accepted": True}
    assert PRIVATE_NONCE not in str(result)


@pytest.mark.parametrize(
    "response",
    [{"accepted": False}, {"accepted": "true"}, {"status": "accepted"}],
)
def test_handoff_rejects_gateway_response_without_true_acceptance(
    response: dict[str, object],
) -> None:
    server = _server(lambda _intent: response)

    result = asyncio.run(server.call_tool("worker_submit_result", {"intent": INTENT}))

    assert result.is_error is True


def test_handoff_uses_live_broker_once_and_replay_fails(tmp_path: Path) -> None:
    request = WorkerResultBrokerRequest(
        nonce=PRIVATE_NONCE,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )
    built: list[bytes] = []

    def build_artifact(data: bytes) -> bytes:
        built.append(data)
        return b'{"controller":"accepted"}'

    session = create_worker_result_broker(
        request,
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    client_request = WorkerResultBrokerClientRequest(nonce=PRIVATE_NONCE, issue_iid=81)

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        return submit_worker_result_to_broker(
            session.socket_path,
            client_request,
            intent.model_dump_json(exclude_none=True).encode(),
            timeout_seconds=10,
        )

    server = _server(gateway)
    first = asyncio.run(
        server.call_tool("worker_submit_result", {"intent": CLOSE_INTENT})
    )
    replay = asyncio.run(
        server.call_tool("worker_submit_result", {"intent": CLOSE_INTENT})
    )
    with pytest.raises(WorkerProtocolError, match="request after acceptance"):
        session.close_and_capture()

    assert first.structured_content == {"accepted": True}
    assert replay.is_error is True
    assert len(built) == 1
    evidence = WorkerResultBuildInput.model_validate_json(
        built[0]
    ).suggest_close_evidence
    assert evidence is not None
    assert evidence.issue_iid == 81


def test_handoff_wrong_controller_issue_is_rejected_by_live_broker(
    tmp_path: Path,
) -> None:
    request = WorkerResultBrokerRequest(
        nonce=PRIVATE_NONCE,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )
    built: list[bytes] = []

    def build_artifact(data: bytes) -> bytes:
        built.append(data)
        return b'{"controller":"accepted"}'

    session = create_worker_result_broker(
        request,
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    wrong_client = WorkerResultBrokerClientRequest(nonce=PRIVATE_NONCE, issue_iid=82)

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        return submit_worker_result_to_broker(
            session.socket_path,
            wrong_client,
            intent.model_dump_json(exclude_none=True).encode(),
            timeout_seconds=10,
        )

    result = asyncio.run(
        _server(gateway, issue_iid=82).call_tool(
            "worker_submit_result", {"intent": CLOSE_INTENT}
        )
    )
    with pytest.raises(WorkerProtocolError, match="request identity mismatch"):
        session.close_and_capture()

    assert result.is_error is True
    assert built == []


def test_handoff_returns_safe_builder_rejection_for_live_correction(
    tmp_path: Path,
) -> None:
    request = WorkerResultBrokerRequest(
        nonce=PRIVATE_NONCE,
        project_id=310967,
        issue_iid=81,
        ledger_revision=7,
        expected_state=RunnerState.EXECUTING,
        expected_phase=WorkflowPhase.WORKFLOW,
        source_branch="agent/issue-81",
        source_sha="a" * 40,
    )
    diagnostic = "builder_input.change_summary: invalid type/shape/value"
    assert is_safe_worker_result_diagnostic(diagnostic)
    built: list[WorkerResultBuildInput] = []
    artifact = b'{"controller":"accepted"}'

    def build_artifact(data: bytes) -> bytes:
        intent = WorkerResultBuildInput.model_validate_json(data)
        built.append(intent)
        if len(built) == 1:
            raise WorkerProtocolError(
                "builder_input.change_summary", "invalid type/shape/value"
            )
        return artifact

    session = create_worker_result_broker(
        request,
        build_artifact=build_artifact,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    client_request = WorkerResultBrokerClientRequest(nonce=PRIVATE_NONCE, issue_iid=81)

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        return submit_worker_result_to_broker(
            session.socket_path,
            client_request,
            intent.model_dump_json(exclude_none=True).encode(),
            timeout_seconds=10,
        )

    server = _server(gateway)
    rejected = asyncio.run(server.call_tool("worker_submit_result", {"intent": INTENT}))
    corrected = asyncio.run(
        server.call_tool(
            "worker_submit_result",
            {"intent": {**INTENT, "change_summary": "Corrected summary."}},
        )
    )
    captured = session.close_and_capture()

    assert rejected.is_error is True
    assert diagnostic in str(rejected)
    assert PRIVATE_NONCE not in str(rejected)
    assert corrected.is_error is not True
    assert corrected.structured_content == {"accepted": True}
    assert [intent.change_summary for intent in built] == [
        INTENT["change_summary"],
        "Corrected summary.",
    ]
    assert captured == artifact


def test_handoff_masks_noncanonical_gateway_error() -> None:
    secret = "secret-token-1234567890"
    unsafe_error = WorkerProtocolError("builder_input.change_summary", secret)
    assert not is_safe_worker_result_diagnostic(str(unsafe_error))

    def gateway(_intent: WorkerResultBuildInput) -> dict[str, object]:
        raise unsafe_error

    result = asyncio.run(
        _server(gateway).call_tool("worker_submit_result", {"intent": INTENT})
    )

    assert result.is_error is True
    assert secret not in str(result)
    assert "builder_input.change_summary" not in str(result)
