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


@pytest.mark.parametrize(
    "risk",
    (
        {"kind": "state", "scenario": "", "invariant": "Keep identity"},
        {"kind": "unknown", "scenario": "Restart", "invariant": "Keep identity"},
        {"kind": "protocol", "scenario": "Replay", "invariant": "\x00"},
        {
            "kind": "state",
            "scenario": "Restart",
            "invariant": "Keep identity",
            "nonce": PRIVATE_NONCE,
        },
    ),
)
def test_handoff_rejects_invalid_nested_risk_before_capture_gateway(
    risk: dict[str, object],
) -> None:
    calls: list[WorkerResultBuildInput] = []

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        calls.append(intent)
        return {"accepted": True}

    intent = {
        **INTENT,
        "workflow_lane": "coding",
        "result_kind": "red_checkpoint_ready",
        "red_checkpoint_request": {
            "argv": ["uv", "run", "pytest", "tests/test_widget.py::test_red"],
            "expected_failure": "assert expected failure",
            "test_paths": ["tests/test_widget.py"],
            "architect_risk_intent": {"risks": [risk]},
        },
    }

    async def submit(client: Any) -> Any:
        return await client.call_tool("worker_submit_result", {"intent": intent})

    result = _round_trip(_server(gateway), submit)
    assert result.is_error is True
    assert calls == []


def _red_handoff_session(
    tmp_path: Path, *, symlink_leaf: bool = False
) -> tuple[
    Any,
    Callable[[WorkerResultBuildInput], dict[str, object]],
    Any,
    list[tuple[str, ...]],
    list[int],
    WorkerResultBrokerRequest,
]:
    from agent_work import runtime, serial
    from agent_work.repository import CommandResult, GitRepository
    from tests.test_agent_work_test_executor_runtime import (
        SnapshotRepository,
        composition,
        config,
        retained_record,
    )

    settings = config(tmp_path)
    settings.checkout.mkdir()
    record = retained_record(settings)
    path = "tests/test_example.py"
    target = settings.checkout / path
    target.parent.mkdir()
    if symlink_leaf:
        external = tmp_path / "external.py"
        external.write_text("private fixture\n")
        target.symlink_to(external)
    else:
        target.write_text("assert False\n")
    commands: list[tuple[str, ...]] = []
    applied: list[int] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(1, "FAILED example", "")

    class AssetRepository(SnapshotRepository):
        """Fake public Git observations while keeping real checkpoint decisions."""

        def red_candidate_tree(
            self, worktree: Path, base_sha: str, paths: tuple[str, ...]
        ) -> str:
            assert worktree == record.worktree
            assert base_sha == record.source_sha
            assert paths == self.observed_paths
            return self.observed_tree

        def verify_test_assets(self, worktree: Path, paths: tuple[str, ...]) -> None:
            verifier: Any = GitRepository
            verifier.verify_test_assets(self, worktree, paths)

        def read_red_checkpoint_ref(
            self, worktree: Path, project_id: int, issue_iid: int, base_sha: str
        ) -> Any:
            assert worktree == record.worktree
            assert project_id == record.project_id
            assert issue_iid == record.issue_iid
            assert base_sha == record.source_sha
            return self.retained[-1] if self.retained else None

    repository = AssetRepository(settings, run)
    wired = runtime.build_runtime_dependencies(
        debug=False, composition=composition(settings, repository, run)
    )
    assert wired.apply_red_checkpoint is not None

    def apply_checkpoint(issue_iid: int, request: Any, tree: str) -> Any:
        applied.append(issue_iid)
        assert wired.apply_red_checkpoint is not None
        return wired.apply_red_checkpoint(issue_iid, request, tree)

    assert record.source_sha is not None
    request = WorkerResultBrokerRequest(
        nonce=PRIVATE_NONCE,
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        ledger_revision=record.revision,
        expected_state=record.state,
        expected_phase=record.phase,
        source_branch=record.source_branch,
        source_sha=record.source_sha,
    )
    session = create_worker_result_broker(
        request,
        build_artifact=serial._broker_artifact_builder(
            record,
            request,
            repository=repository,
            red_checkpoint=None,
            apply_red_checkpoint=apply_checkpoint,
            load_red_checkpoint=wired.load_red_checkpoint,
        ),
        timeout_seconds=10,
        temporary_directory=tmp_path,
    )
    client_request = WorkerResultBrokerClientRequest(
        nonce=PRIVATE_NONCE, issue_iid=record.issue_iid
    )

    def gateway(intent: WorkerResultBuildInput) -> dict[str, object]:
        return submit_worker_result_to_broker(
            session.socket_path,
            client_request,
            intent.model_dump_json(exclude_none=True).encode(),
            timeout_seconds=10,
        )

    return session, gateway, repository, commands, applied, request


def _red_handoff_intent(*, undeclared_target: bool = False) -> dict[str, object]:
    path = "tests/test_example.py"
    argv = ["uv", "run", "pytest", path]
    if undeclared_target:
        argv.append("tests/test_undeclared.py")
    return {
        **INTENT,
        "result_kind": "red_checkpoint_ready",
        "red_checkpoint_request": {
            "argv": argv,
            "expected_failure": "FAILED example",
            "test_paths": [path],
        },
    }


@pytest.mark.parametrize("defect", ("undeclared_target", "symlink_leaf"))
def test_typed_red_handoff_live_builder_rejects_scope_and_assets_before_execution_or_ref(
    tmp_path: Path, defect: str
) -> None:
    session, gateway, repository, commands, applied, request = _red_handoff_session(
        tmp_path, symlink_leaf=defect == "symlink_leaf"
    )
    result = asyncio.run(
        _server(gateway, issue_iid=request.issue_iid).call_tool(
            "worker_submit_result",
            {
                "intent": _red_handoff_intent(
                    undeclared_target=defect == "undeclared_target"
                )
            },
        )
    )
    with pytest.raises(
        WorkerProtocolError, match="correction absent|request was not accepted"
    ):
        session.close_and_capture()
    assert applied == [request.issue_iid]
    assert result.is_error is True
    assert PRIVATE_NONCE not in str(result)
    assert "private fixture" not in str(result)
    assert commands == []
    assert repository.retained == []


def test_typed_valid_red_handoff_live_builder_captures_actual_checkpoint_and_artifact(
    tmp_path: Path,
) -> None:
    from agent_work.worker import parse_worker_result_accepted_artifact

    session, gateway, repository, commands, applied, request = _red_handoff_session(
        tmp_path
    )
    result = asyncio.run(
        _server(gateway, issue_iid=request.issue_iid).call_tool(
            "worker_submit_result", {"intent": _red_handoff_intent()}
        )
    )
    captured = session.close_and_capture()
    artifact = parse_worker_result_accepted_artifact(
        captured, request=request, changed_paths=repository.observed_paths
    )

    assert result.is_error is not True
    assert result.structured_content == {"accepted": True}
    assert PRIVATE_NONCE not in str(result)
    assert "private fixture" not in str(result)
    assert applied == [request.issue_iid]
    assert commands == [("uv", "run", "pytest", "tests/test_example.py")]
    assert len(repository.retained) == 1
    evidence = repository.retained[0]
    assert artifact.red_checkpoint == evidence
    assert artifact.envelope.result_kind == "red_checkpoint_ready"
    assert artifact.envelope.red_checkpoint_request is not None
    assert artifact.envelope.red_checkpoint_request.argv == evidence.argv
    assert evidence.exit_code == 1
    assert evidence.expected_failure == "FAILED example"
    assert evidence.argv == commands[0]
    assert evidence.test_paths == repository.observed_paths
    assert evidence.tree_oid == repository.observed_tree
