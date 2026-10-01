# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Trusted host composition reaches actual private broker and filesystem."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp.types import CallToolResult

from agent_work import serial
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.ledger import JsonLedger
from agent_work.mcp_server import MCPServer
from agent_work.models import AgentWorkConfig, GitLabIssue, LedgerRecord
from tests.test_agent_work_documentation_patch import (
    NEW,
    OLD,
    filesystem,
    grant,
    request,
)
from tests.test_agent_work_serial_runtime import FakeRepository


class _LiveFixture:
    def __init__(self, root: Path) -> None:
        self.config = AgentWorkConfig(
            host="gitlab.example.com",
            project_id=310967,
            checkout=root / "checkout",
            state_root=root / "state",
            git_path=root / "bin/git",
            glab_path=root / "bin/glab",
            base_branch="agent/work-base",
        )
        self.repository = FakeRepository(self.config)
        self.ledger = JsonLedger(
            self.config.state_root, clock=lambda: datetime(2026, 10, 1, tzinfo=UTC)
        )
        self.record = self.ledger.persist(
            LedgerRecord(
                project_id=310967,
                issue_iid=12,
                state=RunnerState.EXECUTING,
                phase=WorkflowPhase.WORKFLOW,
                workflow_lane=WorkflowLane.CONTENT,
                source_branch="agent/issue-12",
                source_sha="a" * 40,
                worktree=self.config.checkout,
            )
        )
        self.issue = GitLabIssue(
            iid=12,
            title="Guide",
            description="Ordinary documentation",
            labels=(),
            state="opened",
            confidential=False,
        )
        self.bound = replace(
            grant(self.config.checkout), ledger_revision=self.record.revision
        )
        self.time = 1.0

    def clock(self) -> float:
        return self.time


def _call(
    server: MCPServer, proposed: serial.DocumentationPatchRequest
) -> dict[str, object]:
    result = cast(
        CallToolResult,
        asyncio.run(
            server.call_tool(
                "worker_documentation_patch",
                {
                    "patches": [
                        patch.model_dump(mode="json") for patch in proposed.patches
                    ],
                    "apply": proposed.apply,
                },
            )
        ),
    )
    assert result.is_error is False
    assert result.structured_content is not None
    return cast(dict[str, object], result.structured_content)


@pytest.mark.parametrize("apply", [False, True])
def test_public_host_mcp_broker_default_filesystem_applies_only_host_grant(
    tmp_path: Path, *, apply: bool
) -> None:
    live = _LiveFixture(tmp_path)
    before = (live.config.checkout / "docs/guide.md").stat()
    ledger_before = live.ledger.load(12)
    with serial.create_live_worker_documentation_patch_session(
        live.record,
        live.issue,
        live.bound,
        ledger=live.ledger,
        repository=live.repository,
        timeout_seconds=10,
        temporary_directory=tmp_path.parent,
        clock=live.clock,
    ) as server:
        tools = asyncio.run(server.list_tools())
        assert tuple(tool.name for tool in tools) == ("worker_documentation_patch",)
        result = _call(server, request(live.bound, apply=apply))
        assert result["outcome"] == ("applied" if apply else "preview")
        assert result["quarantined"] is False and result["reason"] is None
        if apply:
            assert result["changed"] != []
            replay = _call(server, request(live.bound))
            assert replay["outcome"] == "refused"
        else:
            assert result["changed"] == []
            assert _call(server, request(live.bound, apply=False)) == result
    target = live.config.checkout / "docs/guide.md"
    assert target.read_text() == (NEW if apply else OLD)
    assert target.stat().st_mode == before.st_mode
    if not apply:
        assert target.stat().st_ino == before.st_ino
    assert live.ledger.load(12) == ledger_before
    assert live.repository.pushes == 0 and live.repository.prepares == 0


@pytest.mark.parametrize(
    "fault",
    ["project", "issue", "checkout", "branch", "source", "phase", "revision", "expiry"],
)
def test_live_identity_mutations_refuse_before_filesystem_effects(
    tmp_path: Path, fault: str
) -> None:
    live = _LiveFixture(tmp_path)
    with serial.create_live_worker_documentation_patch_session(
        live.record,
        live.issue,
        live.bound,
        ledger=live.ledger,
        repository=live.repository,
        timeout_seconds=10,
        temporary_directory=tmp_path.parent,
        clock=live.clock,
    ) as server:
        assert _call(server, request(live.bound, apply=False))["outcome"] == "preview"
        if fault == "expiry":
            live.time = 20.0
        elif fault == "source":
            live.repository.current_head = "b" * 40
        else:
            updates: dict[str, object] = {}
            if fault == "project":
                updates["project_id"] = 310968
            elif fault == "issue":
                updates["issue_iid"] = 13
            elif fault == "checkout":
                updates["worktree"] = tmp_path / "sibling"
            elif fault == "branch":
                updates["source_branch"] = "agent/foreign"
            elif fault == "phase":
                updates["phase"] = WorkflowPhase.DISCOVERY
            else:
                updates["revision"] = live.record.revision + 1
            changed = live.record.model_copy(update=updates)
            # Mutate one retained field without JsonLedger.persist also
            # incrementing revision and masking the independently tested field.
            (live.ledger.records / "12.json").write_text(changed.model_dump_json())
        result = _call(server, request(live.bound))
        assert (result["outcome"], result["reason"], result["changed"]) == (
            "refused",
            "identity_changed",
            [],
        )
    assert (live.config.checkout / "docs/guide.md").read_text() == OLD
    assert live.repository.pushes == 0


@pytest.mark.parametrize(
    "fault", ["issue", "project", "checkout", "branch", "sha", "revision", "phase"]
)
def test_foreign_host_grant_cannot_authorize_a_live_session(
    tmp_path: Path, fault: str
) -> None:
    live = _LiveFixture(tmp_path)
    with serial.create_live_worker_documentation_patch_session(
        live.record,
        live.issue,
        live.bound,
        ledger=live.ledger,
        repository=live.repository,
        timeout_seconds=10,
        temporary_directory=tmp_path.parent,
        clock=live.clock,
    ) as server:
        assert _call(server, request(live.bound, apply=False))["outcome"] == "preview"
    bound = live.bound
    if fault == "issue":
        bound = replace(bound, issue_iid=13)
    elif fault == "project":
        bound = replace(bound, project_id=310968)
    elif fault == "checkout":
        bound = replace(bound, checkout=tmp_path / "foreign")
    elif fault == "branch":
        bound = replace(bound, source_branch="foreign")
    elif fault == "sha":
        bound = replace(bound, source_sha="b" * 40)
    elif fault == "revision":
        bound = replace(bound, ledger_revision=bound.ledger_revision + 1)
    else:
        bound = replace(bound, phase=WorkflowPhase.DISCOVERY)
    with (
        pytest.raises(serial.SegmentError),
        serial.create_live_worker_documentation_patch_session(
            live.record,
            live.issue,
            bound,
            ledger=live.ledger,
            repository=live.repository,
            timeout_seconds=10,
            temporary_directory=tmp_path.parent,
            clock=live.clock,
        ),
    ):
        pytest.fail("foreign grant admitted")
    assert (live.config.checkout / "docs/guide.md").read_text() == OLD


@pytest.mark.parametrize("fault", ["nonce", "oversized", "extra"])
def test_private_broker_rejects_unauthenticated_or_unframed_authority(
    tmp_path: Path, fault: str
) -> None:
    live = _LiveFixture(tmp_path)
    identity = serial.WorkerReadSessionIdentity(
        nonce="a" * 32,
        project_id=310967,
        issue_iid=12,
        checkout=live.config.checkout,
        phase="workflow",
        ledger_revision=live.record.revision,
        source_sha="a" * 40,
    )
    calls = 0

    def action(
        proposed: serial.DocumentationPatchRequest,
    ) -> serial.DocumentationPatchResult:
        nonlocal calls
        calls += 1
        return serial.apply_documentation_patch(
            live.bound, proposed, identity_check=lambda: True, filesystem=filesystem()
        )

    broker = serial.WorkerDocumentationPatchBrokerSession(
        identity,
        identity_check=lambda observed: observed == identity,
        patch_action=action,
        timeout_seconds=10,
        temporary_directory=tmp_path.parent,
        clock=live.clock,
    )
    try:
        valid = serial.request_worker_documentation_patch(
            broker.socket_path, broker.nonce, request(live.bound, apply=False)
        )
        assert valid.outcome == "preview" and calls == 1
        if fault == "nonce":
            with pytest.raises(ValueError):
                serial.request_worker_documentation_patch(
                    broker.socket_path, "b" * 32, request(live.bound)
                )
        elif fault == "oversized":
            proposed = request(live.bound, replacement="x" * 131073)
            with pytest.raises(ValueError):
                serial.request_worker_documentation_patch(
                    broker.socket_path, broker.nonce, proposed
                )
        else:
            with pytest.raises(ValueError):
                serial.DocumentationPatchRequest.model_validate_json(
                    json.dumps(
                        {
                            **request(live.bound).model_dump(mode="json"),
                            "grant": {"apply": True},
                        }
                    )
                )
        assert calls == 1
        assert (live.config.checkout / "docs/guide.md").read_text() == OLD
    finally:
        broker.close()
    assert not broker.socket_path.exists()


def test_supplied_long_socket_parent_never_falls_back_to_global_tmp(
    tmp_path: Path,
) -> None:
    live = _LiveFixture(tmp_path)
    parent = tmp_path / ("long" * 30)
    parent.mkdir()
    with (
        pytest.raises(serial.SegmentError),
        serial.create_live_worker_documentation_patch_session(
            live.record,
            live.issue,
            live.bound,
            ledger=live.ledger,
            repository=live.repository,
            timeout_seconds=10,
            temporary_directory=parent,
            clock=live.clock,
        ),
    ):
        pytest.fail("unsupported socket path accepted")
    assert tuple(parent.iterdir()) == ()
    assert (live.config.checkout / "docs/guide.md").read_text() == OLD


@settings(max_examples=8)
@given(
    prose=st.text(alphabet="abc XYZé", max_size=32),
    extra=st.sampled_from(("root", "grant", "role", "command")),
)
def test_generated_closed_mcp_projection_never_returns_proposed_content(
    tmp_path_factory: pytest.TempPathFactory, prose: str, extra: str
) -> None:
    root = tmp_path_factory.mktemp("projection")
    live = _LiveFixture(root)
    replacement = NEW + "Explanation: " + prose + "\n"
    live.bound = replace(
        grant(live.config.checkout, replacement=replacement),
        ledger_revision=live.record.revision,
    )
    with serial.create_live_worker_documentation_patch_session(
        live.record,
        live.issue,
        live.bound,
        ledger=live.ledger,
        repository=live.repository,
        timeout_seconds=10,
        temporary_directory=root.parent,
        clock=live.clock,
    ) as server:
        proposed = request(live.bound, replacement=replacement, apply=False)
        result = _call(server, proposed)
        assert set(result) == {"outcome", "changed", "reason", "quarantined"}
        assert result == {
            "outcome": "preview",
            "changed": [],
            "reason": None,
            "quarantined": False,
        }
        response = cast(
            CallToolResult,
            asyncio.run(
                server.call_tool(
                    "worker_documentation_patch",
                    {
                        "patches": [
                            patch.model_dump(mode="json") for patch in proposed.patches
                        ],
                        "apply": True,
                        extra: "PRIVATE_AUTHORITY",
                    },
                )
            ),
        )
        assert response.is_error is True
        assert "PRIVATE_AUTHORITY" not in str(response)
    assert (live.config.checkout / "docs/guide.md").read_text() == OLD


@pytest.mark.parametrize("terminal", ["applied", "failed_partial"])
def test_terminal_broker_result_prevents_action_redispatch_even_with_valid_old_content(
    tmp_path: Path, terminal: str
) -> None:
    live = _LiveFixture(tmp_path)
    identity = serial.WorkerReadSessionIdentity(
        nonce="a" * 32,
        project_id=310967,
        issue_iid=12,
        checkout=live.config.checkout,
        phase="workflow",
        ledger_revision=live.record.revision,
        source_sha="a" * 40,
    )
    calls = 0

    def action(
        proposed: serial.DocumentationPatchRequest,
    ) -> serial.DocumentationPatchResult:
        nonlocal calls
        calls += 1
        assert proposed == request(live.bound)
        if terminal == "applied":
            return serial.DocumentationPatchResult(
                outcome="applied",
                changed=(
                    serial.DocumentationPatchMember(
                        path="docs/guide.md",
                        sha256=live.bound.groups[0][0].replacement_sha256,
                    ),
                ),
            )
        return serial.DocumentationPatchResult(
            outcome="failed_partial", reason="io_failed", quarantined=True
        )

    broker = serial.WorkerDocumentationPatchBrokerSession(
        identity,
        identity_check=lambda observed: observed == identity,
        patch_action=action,
        timeout_seconds=10,
        temporary_directory=tmp_path.parent,
        clock=live.clock,
    )
    try:
        first = serial.request_worker_documentation_patch(
            broker.socket_path, broker.nonce, request(live.bound)
        )
        assert first.outcome == terminal and calls == 1
        # Action fake leaves original bytes intact: stale content cannot explain
        # the second refusal; only the session's terminal lifecycle can.
        assert (live.config.checkout / "docs/guide.md").read_text() == OLD
        second = serial.request_worker_documentation_patch(
            broker.socket_path, broker.nonce, request(live.bound)
        )
        assert second.outcome == "refused" and calls == 1
        assert second.changed == ()
    finally:
        broker.close()
