# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A Judge's opt-in validation tools bind to one staged worker candidate."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import CallToolResult

from agent_work import serial
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.models import LedgerRecord
from tests.test_agent_work_live_worker_git import (
    GitTrackingRepository,
    MutableGitLedger,
)
from tests.test_agent_work_serial_runtime import _issue, _record

TREE = "b" * 40
OTHER_TREE = "c" * 40
ARTIFACT_SHA = "d" * 64
SUMMARY: dict[str, object] = {"view": "summary"}
JUNIT_PAGE: dict[str, object] = {
    "view": "junit",
    "environment": "py311",
    "offset": 0,
    "limit": 20,
}
TIMING_PAGE: dict[str, object] = {
    "view": "timing",
    "environment": "py311",
    "offset": 0,
    "limit": 20,
}


def _session(
    tmp_path: Path,
    *,
    role: str = "judge",
    record: LedgerRecord | None = None,
    response: dict[str, object] | None = None,
) -> tuple[
    Any,
    MutableGitLedger,
    GitTrackingRepository,
    list[tuple[str, str, dict[str, object] | None]],
]:
    current = record or _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(update={"revision": 7, "workflow_lane": WorkflowLane.CODING})
    ledger = MutableGitLedger(current)
    repository = GitTrackingRepository(tmp_path)
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def candidate_action(
        action: str, tree: str | None, selector: dict[str, object] | None
    ) -> dict[str, object]:
        assert tree is not None
        calls.append((action, tree, selector))
        if response is not None:
            return response
        default_response: dict[str, object] = {
            "candidate_tree": TREE,
            "state": "terminal-success",
            "verdict": "approve",
            "junit_artifacts": [
                {
                    "environment": "py311",
                    "sha256": ARTIFACT_SHA,
                    "test_count": 12,
                    "path": "/private/state/junit.xml",
                }
            ],
            "junit_timings": [
                {
                    "environment": "py311",
                    "total_seconds": 0.5,
                    "timing_complete": True,
                    "slowest_cases": [
                        {"name": "tests/test_example.py::test_slow", "seconds": 0.2},
                        {"name": "tests/test_example.py::test_fast", "seconds": 0.01},
                    ],
                    "over_100ms_cases": [
                        {"name": "tests/test_example.py::test_slow", "seconds": 0.2}
                    ],
                }
            ],
            "nonce": "private-nonce",
        }
        return default_response

    factory = getattr(serial, "create_live_worker_validation_session", None)
    assert callable(factory), "worker validation session is unavailable"
    session = factory(
        current,
        _issue(),
        ledger=ledger,
        repository=repository,
        candidate_action=candidate_action,
        candidate_tree=TREE,
        role=role,
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    return session, ledger, repository, calls


def _request(
    session: Any,
    action: str,
    *,
    tree: str = TREE,
    selector: dict[str, object] | None = None,
) -> dict[str, object]:
    client = getattr(
        importlib.import_module("agent_work.worker_read"),
        "request_worker_validation",
        None,
    )
    assert callable(client), "worker validation client is unavailable"
    return cast(
        dict[str, object],
        client(
            session.socket_path,
            session.nonce,
            action,
            candidate_tree=tree,
            selector=selector,
        ),
    )


def _raw_request(path: Path, payload: dict[str, object]) -> dict[str, object] | None:
    encoded = json.dumps(payload).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        connection.connect(str(path))
        connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
        size_bytes = connection.recv(4)
        if not size_bytes:
            return None
        size = int.from_bytes(size_bytes, "big")
        return cast(dict[str, object], json.loads(connection.recv(size)))


def test_judge_can_validate_and_read_bounded_status_and_evidence(
    tmp_path: Path,
) -> None:
    session, _ledger, _repository, calls = _session(tmp_path)
    try:
        validated = _request(session, "validate")
        status = _request(session, "status")
        summary = _request(session, "evidence", selector=SUMMARY)
        junit = _request(session, "evidence", selector=JUNIT_PAGE)
        timing = _request(session, "evidence", selector=TIMING_PAGE)
    finally:
        session.close()

    assert validated["candidate_tree"] == TREE
    assert status["state"] == "terminal-success"
    assert summary["candidate_tree"] == TREE
    assert summary["junit_artifacts"] == [
        {"environment": "py311", "sha256": ARTIFACT_SHA, "test_count": 12}
    ]
    assert timing["items"] == [
        {"name": "tests/test_example.py::test_slow", "seconds": 0.2}
    ]
    assert timing["timing_complete"] is True
    assert timing["total"] == 1
    assert timing["next_offset"] is None
    assert junit["items"] == [
        {"environment": "py311", "sha256": ARTIFACT_SHA, "test_count": 12}
    ]
    assert junit["total"] == 1
    assert junit["next_offset"] is None
    assert all(
        "/private/" not in str(value)
        for value in (validated, status, summary, junit, timing)
    )
    assert all(
        "private-nonce" not in str(value)
        for value in (validated, status, summary, junit, timing)
    )
    assert calls == [
        ("validate", TREE, None),
        ("status", TREE, None),
        ("status", TREE, None),
        ("status", TREE, None),
        ("status", TREE, None),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", 310968),
        ("issue_iid", 13),
        ("revision", 8),
        ("state", RunnerState.PUBLISHING),
        ("phase", WorkflowPhase.READY_TO_PUBLISH),
        ("workflow_lane", WorkflowLane.CONTENT),
        ("worktree", Path("/other/worktree")),
        ("source_sha", "f" * 40),
        ("source_branch", "agent/issue-13"),
    ],
)
def test_validation_denies_ledger_drift_before_hooks(
    tmp_path: Path, field: str, value: object
) -> None:
    session, ledger, _repository, calls = _session(tmp_path)
    try:
        assert ledger.current is not None
        ledger.current = ledger.current.model_copy(update={field: value})
        with pytest.raises(ValueError, match="validation|identity|candidate"):
            _request(session, "validate")
    finally:
        session.close()
    assert calls == []


@pytest.mark.parametrize("drift", ["head", "branch"])
def test_validation_denies_checkout_drift_before_hooks(
    tmp_path: Path, drift: str
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        if drift == "head":
            repository.current_head = OTHER_TREE
        else:
            repository.legacy_branch = "agent/issue-13"
        with pytest.raises(ValueError, match="validation|identity|candidate"):
            _request(session, "validate")
    finally:
        session.close()
    assert calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "validate", "candidate_tree": OTHER_TREE},
        {"action": "validate", "candidate_tree": TREE, "selector": SUMMARY},
        {"action": "validate", "candidate_tree": TREE, "argv": ["pytest"]},
        {"action": "status", "candidate_tree": TREE, "selector": SUMMARY},
        {"action": "evidence", "candidate_tree": TREE, "selector": {"view": "all"}},
        {
            "action": "evidence",
            "candidate_tree": TREE,
            "selector": {
                "view": "junit",
                "environment": "../secret",
                "offset": 0,
                "limit": 20,
            },
        },
        {
            "action": "evidence",
            "candidate_tree": TREE,
            "selector": {
                "view": "junit",
                "environment": "py311",
                "offset": 0,
                "limit": 100000,
            },
        },
        {
            "action": "evidence",
            "candidate_tree": TREE,
            "selector": {"view": "summary", "path": "/private/state"},
        },
    ],
)
def test_validation_rejects_wrong_tree_or_open_inputs_without_dispatch(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    session, _ledger, _repository, calls = _session(tmp_path)
    try:
        reply = _raw_request(session.socket_path, {"nonce": session.nonce, **payload})
    finally:
        session.close()
    assert reply is None or reply.get("accepted") is False
    assert calls == []


@pytest.mark.parametrize("role", ["candidate-preview", "worker", "finalizer"])
def test_validation_requires_judge_role_before_hooks(tmp_path: Path, role: str) -> None:
    record = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(update={"revision": 7, "workflow_lane": WorkflowLane.CODING})
    with pytest.raises((RuntimeError, ValueError), match="validation|judge|role"):
        _session(tmp_path, role=role, record=record)


def test_validation_rejects_oversized_and_private_controller_output(
    tmp_path: Path,
) -> None:
    response: dict[str, object] = {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "verdict": "approve",
        "raw_receipt": "x" * 200_000,
        "nonce": "private-nonce",
    }
    session, _ledger, _repository, calls = _session(tmp_path, response=response)
    try:
        with pytest.raises(ValueError, match="validation|response|limit"):
            _request(session, "evidence", selector=SUMMARY)
    finally:
        session.close()
    assert calls == [("status", TREE, None)]


@pytest.mark.parametrize(
    ("action", "selector"),
    [
        ("validate", None),
        ("status", None),
        ("evidence", SUMMARY),
        ("evidence", JUNIT_PAGE),
        ("evidence", TIMING_PAGE),
    ],
)
def test_validation_rejects_controller_response_for_another_tree(
    tmp_path: Path, action: str, selector: dict[str, object] | None
) -> None:
    response: dict[str, object] = {
        "candidate_tree": OTHER_TREE,
        "state": "terminal-success",
        "verdict": "approve",
        "junit_artifacts": [],
        "junit_timings": [],
    }
    session, _ledger, _repository, calls = _session(tmp_path, response=response)
    try:
        with pytest.raises(ValueError, match="candidate|tree|validation|response"):
            _request(session, action, selector=selector)
    finally:
        session.close()
    assert calls == [(action if action != "evidence" else "status", TREE, None)]


def test_terminal_success_without_trusted_verdict_reports_unknown(
    tmp_path: Path,
) -> None:
    response: dict[str, object] = {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "junit_artifacts": [],
    }
    session, _ledger, _repository, _calls = _session(tmp_path, response=response)
    try:
        status = _request(session, "status")
        summary = _request(session, "evidence", selector=SUMMARY)
    finally:
        session.close()
    assert status["verdict"] == "unknown"
    assert summary["verdict"] == "unknown"


@pytest.mark.parametrize(
    ("state", "verdict"),
    [
        ("terminal-success", "approve"),
        ("running", "unknown"),
        ("terminal-failure", "reject"),
    ],
)
def test_absent_timing_status_reports_unknown_while_preserving_state(
    tmp_path: Path, state: str, verdict: str
) -> None:
    response: dict[str, object] = {
        "candidate_tree": TREE,
        "state": state,
        "verdict": verdict,
        "timing_status": None,
        "junit_artifacts": [],
        "junit_timings": [],
    }
    session, _ledger, _repository, _calls = _session(tmp_path, response=response)
    try:
        status = _request(session, "status")
        summary = _request(session, "evidence", selector=SUMMARY)
    finally:
        session.close()
    assert status["state"] == state
    assert status["timing_status"] == "unknown"
    assert summary["state"] == state
    assert summary["timing_status"] == "unknown"


def test_large_valid_timing_status_has_bounded_summary_and_page(tmp_path: Path) -> None:
    cases = [
        {
            "name": f"tests/test_example.py::test_{index:04d}_{'a' * 100}",
            "seconds": 0.2,
        }
        for index in range(1024)
    ]
    response: dict[str, object] = {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "verdict": "approve",
        "junit_artifacts": [
            {"environment": "py311", "sha256": ARTIFACT_SHA, "test_count": 1024}
        ],
        "junit_timings": [
            {
                "environment": "py311",
                "total_seconds": 204.8,
                "timing_complete": True,
                "slowest_cases": cases[:3],
                "over_100ms_cases": cases,
            }
        ],
    }
    assert len(json.dumps(response).encode()) > 131_072
    session, _ledger, _repository, _calls = _session(tmp_path, response=response)
    try:
        summary = _request(session, "evidence", selector=SUMMARY)
        first_page = _request(session, "evidence", selector=TIMING_PAGE)
        middle_page = _request(
            session,
            "evidence",
            selector={**TIMING_PAGE, "offset": 100},
        )
        last_page = _request(
            session,
            "evidence",
            selector={**TIMING_PAGE, "offset": 1020},
        )
    finally:
        session.close()
    assert summary["candidate_tree"] == TREE
    assert first_page["items"] == cases[:20]
    assert first_page["total"] == 1024
    assert first_page["next_offset"] == 20
    assert first_page["timing_complete"] is True
    assert middle_page["items"] == cases[100:120]
    assert middle_page["total"] == 1024
    assert middle_page["next_offset"] == 120
    assert last_page["items"] == cases[1020:]
    assert last_page["total"] == 1024
    assert last_page["next_offset"] is None
    assert last_page["timing_complete"] is True
    assert len(json.dumps(summary).encode()) < 16_384
    assert len(json.dumps(first_page).encode()) < 16_384


def test_validation_mcp_exposes_only_closed_judge_tools() -> None:
    factory = getattr(
        importlib.import_module("agent_work.mcp_server"),
        "create_worker_validation_mcp_server",
        None,
    )
    assert callable(factory), "worker validation MCP server is unavailable"
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def gateway(
        action: str, tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree, selector))
        return {"candidate_tree": TREE, "state": "terminal-success"}

    server = factory(gateway, TREE)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "worker_validation_validate",
        "worker_validation_status",
        "worker_validation_evidence",
    }
    assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
    for name, arguments in [
        ("candidate_read", {}),
        ("worker_validation_validate", {"argv": ["pytest"]}),
        ("worker_validation_status", {"candidate_tree": OTHER_TREE}),
        ("worker_validation_evidence", {"selector": {"view": "all"}}),
        (
            "worker_validation_evidence",
            {"selector": {"view": "summary", "path": "/private"}},
        ),
    ]:
        result = cast(CallToolResult, asyncio.run(server.call_tool(name, arguments)))
        assert result.is_error
    assert calls == []


def test_validation_mcp_does_not_expose_private_gateway_fields() -> None:
    factory = getattr(
        importlib.import_module("agent_work.mcp_server"),
        "create_worker_validation_mcp_server",
        None,
    )
    assert callable(factory), "worker validation MCP server is unavailable"

    def gateway(
        _action: str, _tree: str, _selector: dict[str, object] | None
    ) -> dict[str, object]:
        return {
            "candidate_tree": TREE,
            "state": "terminal-success",
            "nonce": "private-nonce",
            "path": "/private/state/junit.xml",
            "raw_receipt": "private-receipt",
        }

    server = factory(gateway, TREE)
    result = cast(
        CallToolResult,
        asyncio.run(
            server.call_tool("worker_validation_evidence", {"selector": SUMMARY})
        ),
    )
    public_reply = json.dumps(result.model_dump(mode="json"))
    assert "private-nonce" not in public_reply
    assert "/private/state/junit.xml" not in public_reply
    assert "private-receipt" not in public_reply
