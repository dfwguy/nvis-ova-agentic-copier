# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A live worker's Git tools are fixed and bound to its controller session."""

from __future__ import annotations

import asyncio
import importlib
import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import CallToolResult

from agent_work import serial
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.green_commit import green_commit_ref
from agent_work.models import LedgerRecord, PublicationCorrectionIdentity
from agent_work.red_checkpoint import RedCheckpointEvidence
from agent_work.red_commit import red_commit_ref
from tests.test_agent_work_live_worker_read import (
    HeadTrackingRepository,
    MutableLedger,
)
from tests.test_agent_work_serial_runtime import _issue, _record

TREE = "b" * 40
CORRECTED_TREE = "c" * 40
COMMIT_SHA = "d" * 40
RED_SHA = "e" * 40
GREEN_SHA = "f" * 40
AUTHORED_SOURCE_SHA = "8" * 40
BASE_SHA = "a" * 40
RED_TREE = "1" * 40
GREEN_TREE = "2" * 40


class MutableGitLedger(MutableLedger):
    """Provide the full ledger read port for the bound Git session."""

    def __init__(self, record: LedgerRecord) -> None:
        super().__init__(record)
        self.state_root = record.worktree.parent

    def list_records(self) -> tuple[LedgerRecord, ...]:
        return () if self.current is None else (self.current,)

    def load_read_only(self, issue_iid: int) -> LedgerRecord | None:
        return self.load(issue_iid)

    def persist(self, record: LedgerRecord) -> LedgerRecord:
        self.current = record
        return record


class GitTrackingRepository(HeadTrackingRepository):
    """Expose branch drift through the repository's exact checkout check."""

    def verify_worktree(self, worktree: Path, branch: str, expected_head: str) -> None:
        super().verify_worktree(worktree, branch, expected_head)
        if self.legacy_branch != branch or self.current_head != expected_head:
            raise ValueError("worktree branch or head changed")


class AuthoredSourceRepository(GitTrackingRepository):
    """Expose the original authored source before any Green commit exists."""

    def __init__(
        self, tmp_path: Path, *, observed_head: str = AUTHORED_SOURCE_SHA
    ) -> None:
        super().__init__(tmp_path)
        self.current_head = observed_head

    def green_commit_dependencies(self, _worktree: Path, _scratch: Path) -> Any:
        raise AssertionError("initial authored source must not need Green proof")

    def red_commit_dependencies(self, _worktree: Path, _scratch: Path) -> Any:
        raise AssertionError("initial authored source must not need Red proof")


class GreenProofRepository(GitTrackingRepository):
    """Expose controller-owned Green and Red commit identity observations."""

    def __init__(self, tmp_path: Path, *, fault: str = "") -> None:
        super().__init__(tmp_path)
        self.current_head = "9" * 40 if fault == "head" else GREEN_SHA
        self.fault = fault

    def green_commit_dependencies(self, _worktree: Path, _scratch: Path) -> Any:
        repository = self

        class GreenDependencies:
            def commit_identity(self, sha: str) -> tuple[str, str, str]:
                if sha != GREEN_SHA:
                    raise ValueError("unknown Green commit")
                parent = BASE_SHA if repository.fault == "green_parent" else RED_SHA
                tree = RED_TREE if repository.fault == "green_tree" else GREEN_TREE
                return parent, tree, "Resolve issue #12"

            def read_ref(self, ref: str) -> str | None:
                expected = green_commit_ref(12, BASE_SHA, GREEN_TREE)
                if ref == expected and repository.fault not in {
                    "green_ref",
                    "unproven",
                }:
                    return GREEN_SHA
                return None

        return GreenDependencies()

    def red_commit_dependencies(self, _worktree: Path, _scratch: Path) -> Any:
        repository = self

        class RedDependencies:
            def commit_identity(self, sha: str) -> tuple[str, str]:
                if sha != RED_SHA:
                    raise ValueError("unknown Red commit")
                parent = GREEN_SHA if repository.fault == "red_parent" else BASE_SHA
                return parent, RED_TREE

            def read_ref(self, ref: str) -> str | None:
                expected = red_commit_ref(12, BASE_SHA)
                if ref == expected and repository.fault not in {"red_ref", "unproven"}:
                    return RED_SHA
                return None

        return RedDependencies()


ControllerAction = Callable[
    [str, str | None, dict[str, object] | None, GitTrackingRepository],
    dict[str, object],
]


def _authored_record(tmp_path: Path) -> LedgerRecord:
    """Build an authored recovery whose source differs from its Red baseline."""
    record = _record(
        tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
    ).model_copy(update={"revision": 7, "source_sha": AUTHORED_SOURCE_SHA})
    checkpoint = RedCheckpointEvidence(
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        source_branch=record.source_branch,
        base_sha=BASE_SHA,
        tree_oid=RED_TREE,
        ref=f"refs/agent-work/red-checkpoints/{record.issue_iid}/{BASE_SHA}",
        test_paths=("tests/test_agent_work_live_worker_git.py",),
        argv=("uv", "run", "pytest", "tests/test_agent_work_live_worker_git.py"),
        exit_code=1,
        expected_failure="assert expected failure",
        output_sha256="f" * 64,
    )
    correction = PublicationCorrectionIdentity(
        mode="authored_candidate_recovery",
        project_id=record.project_id,
        issue_iid=record.issue_iid,
        worktree=record.worktree,
        source_branch=record.source_branch,
        baseline_sha=BASE_SHA,
        head_sha=AUTHORED_SOURCE_SHA,
        index_fingerprint="d" * 64,
        staged_paths=("src/example.py",),
        unstaged_paths=(),
        workflow_lane=WorkflowLane.CODING,
        red_checkpoint=checkpoint,
    )
    return record.model_copy(update={"correction_identity": correction})


def _large_timings() -> list[dict[str, object]]:
    """Return valid bounded per-environment timings larger than one wire frame."""
    cases = [
        {
            "name": f"tests/test_example.py::test_case_{index:04d}_" + "a" * 80,
            "seconds": 0.2,
        }
        for index in range(1_024)
    ]
    return [
        {
            "environment": environment,
            "total_seconds": 205.0,
            "slowest_cases": cases[:3],
            "timing_complete": True,
            "over_100ms_cases": cases,
        }
        for environment in ("py311", "py314")
    ]


def _session(
    tmp_path: Path,
    *,
    workflow_lane: WorkflowLane = WorkflowLane.CODING,
    controller: ControllerAction | None = None,
    make_repository: Callable[[Path], GitTrackingRepository] = GitTrackingRepository,
    make_record: Callable[[Path], LedgerRecord] | None = None,
) -> tuple[
    Any,
    MutableGitLedger,
    GitTrackingRepository,
    list[tuple[str, str | None, dict[str, object] | None]],
]:
    record = (
        make_record(tmp_path)
        if make_record is not None
        else _record(
            tmp_path, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
        ).model_copy(update={"revision": 7, "workflow_lane": workflow_lane})
    )
    ledger = MutableGitLedger(record)
    repository = make_repository(tmp_path)
    calls: list[tuple[str, str | None, dict[str, object] | None]] = []

    def candidate_action(
        action: str, tree: str | None, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append((action, tree, selector))
        if controller is not None:
            return controller(action, tree, selector, repository)
        if action == "commit":
            repository.current_head = COMMIT_SHA
            return {
                "candidate_tree": tree,
                "committed_tree": tree,
                "commit_sha": COMMIT_SHA,
                "red_sha": RED_SHA,
                "state": "terminal-success",
            }
        return {
            "candidate_tree": TREE,
            "state": "staged" if action == "stage" else "ready",
        }

    factory = getattr(serial, "create_live_worker_git_session", None)
    if factory is None:
        session = serial.create_live_worker_read_session(
            record,
            _issue(),
            ledger=ledger,
            repository=repository,
            timeout_seconds=10,
            temporary_directory=tmp_path,
        )
    else:
        session = factory(
            record,
            _issue(),
            ledger=ledger,
            repository=repository,
            candidate_action=candidate_action,
            timeout_seconds=10,
            temporary_directory=tmp_path,
        )
    return session, ledger, repository, calls


def _request(
    session: Any, action: str, candidate_tree: str | None = None
) -> dict[str, object]:
    worker_read = importlib.import_module("agent_work.worker_read")
    request = getattr(worker_read, "request_worker_git", None)
    if request is None:
        return worker_read.request_worker_read(
            session.socket_path, session.nonce, action
        )
    return cast(
        dict[str, object],
        request(
            session.socket_path, session.nonce, action, candidate_tree=candidate_tree
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


def test_live_worker_git_observes_status_and_forwards_exact_candidate_actions(
    tmp_path: Path,
) -> None:
    session, ledger, repository, calls = _session(tmp_path)
    try:
        status = _request(session, "git_status")
        stage = _request(session, "git_stage_candidate")
        candidate = _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()

    assert status["changed_paths"] == list(repository.paths)
    assert stage == {"candidate_tree": TREE, "state": "staged"}
    assert candidate == {
        "candidate_tree": TREE,
        "committed_tree": TREE,
        "commit_sha": COMMIT_SHA,
        "red_sha": RED_SHA,
        "state": "terminal-success",
    }
    assert calls == [("stage", None, None), ("commit", TREE, None)]
    assert ledger.loads == [12, 12, 12]


def test_live_worker_git_status_reports_clean_and_dirty_checkout(
    tmp_path: Path,
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        dirty = _request(session, "git_status")
        repository.paths = ()
        clean = _request(session, "git_status")
    finally:
        session.close()
    assert dirty["dirty"] is True
    assert dirty["changed_paths"]
    assert clean == {"changed_paths": [], "dirty": False}
    assert calls == []


def test_live_worker_git_accepts_initial_authored_source_without_commit_proof(
    tmp_path: Path,
) -> None:
    session, ledger, repository, calls = _session(
        tmp_path,
        make_record=_authored_record,
        make_repository=AuthoredSourceRepository,
    )
    try:
        status = _request(session, "git_status")
        staged = _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert status["changed_paths"] == list(repository.paths)
    assert staged["candidate_tree"] == TREE
    assert ledger.current is not None
    assert ledger.current.source_sha == AUTHORED_SOURCE_SHA
    assert ledger.current.correction_identity is not None
    assert ledger.current.correction_identity.baseline_sha == BASE_SHA
    assert ledger.current.correction_identity.head_sha == AUTHORED_SOURCE_SHA
    assert repository.current_head == AUTHORED_SOURCE_SHA
    assert calls == [("stage", None, None)]


def test_live_worker_git_denies_unrelated_authored_head_without_proof(
    tmp_path: Path,
) -> None:
    valid, _ledger, _repository, valid_calls = _session(
        tmp_path,
        make_record=_authored_record,
        make_repository=AuthoredSourceRepository,
    )
    try:
        assert _request(valid, "git_status")["dirty"] is True
    finally:
        valid.close()
    unexpected_session: Any = None
    try:
        with pytest.raises(
            (RuntimeError, ValueError), match="worker|identity|proof|head"
        ):
            unexpected_session, _ledger, _repository, _calls = _session(
                tmp_path,
                make_record=_authored_record,
                make_repository=lambda path: AuthoredSourceRepository(
                    path, observed_head="9" * 40
                ),
            )
    finally:
        if unexpected_session is not None:
            unexpected_session.close()
    assert valid_calls == []


def test_live_worker_git_recovers_authored_green_against_original_baseline(
    tmp_path: Path,
) -> None:
    session, ledger, repository, calls = _session(
        tmp_path,
        make_record=_authored_record,
        make_repository=GreenProofRepository,
    )
    try:
        status = _request(session, "git_status")
        staged = _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert status["changed_paths"] == list(repository.paths)
    assert staged["candidate_tree"] == TREE
    assert ledger.current is not None
    assert ledger.current.source_sha == AUTHORED_SOURCE_SHA
    assert ledger.current.correction_identity is not None
    assert ledger.current.correction_identity.baseline_sha == BASE_SHA
    assert ledger.current.correction_identity.head_sha == AUTHORED_SOURCE_SHA
    assert len({ledger.current.source_sha, repository.current_head, BASE_SHA}) == 3
    assert calls == [("stage", None, None)]


@pytest.mark.parametrize(
    "fault",
    [
        "unproven",
        "green_parent",
        "red_parent",
        "green_tree",
        "red_ref",
        "green_ref",
        "head",
    ],
)
def test_live_worker_git_denies_unproven_authored_green(
    tmp_path: Path, fault: str
) -> None:
    valid, _ledger, _repository, valid_calls = _session(
        tmp_path,
        make_record=_authored_record,
        make_repository=GreenProofRepository,
    )
    try:
        assert _request(valid, "git_stage_candidate")["candidate_tree"] == TREE
    finally:
        valid.close()
    unexpected_session: Any = None
    try:
        with pytest.raises(
            (RuntimeError, ValueError), match="worker|identity|proof|head"
        ):
            unexpected_session, _ledger, _repository, _calls = _session(
                tmp_path,
                make_record=_authored_record,
                make_repository=lambda path: GreenProofRepository(path, fault=fault),
            )
    finally:
        if unexpected_session is not None:
            unexpected_session.close()
    assert valid_calls == [("stage", None, None)]


@pytest.mark.parametrize("field", ["correction_identity", "work_kind"])
def test_live_worker_git_denies_same_revision_ledger_provenance_drift(
    tmp_path: Path, field: str
) -> None:
    session, ledger, _repository, calls = _session(
        tmp_path,
        make_record=_authored_record,
        make_repository=GreenProofRepository,
    )
    try:
        assert _request(session, "git_status")["dirty"] is True
        assert ledger.current is not None
        replacement: object = (
            None if field == "correction_identity" else WorkKind.EXISTING_MR_REMEDIATION
        )
        ledger.current = ledger.current.model_copy(update={field: replacement})
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert calls == []


def test_live_worker_git_content_lane_cannot_commit_candidate(tmp_path: Path) -> None:
    session, _ledger, _repository, calls = _session(
        tmp_path, workflow_lane=WorkflowLane.CONTENT
    )
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|candidate|lane"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None)]


def test_live_worker_git_compacts_large_commit_reply_and_replays_proof(
    tmp_path: Path,
) -> None:
    full_reply: dict[str, object] = {
        "candidate_tree": TREE,
        "committed_tree": TREE,
        "commit_sha": COMMIT_SHA,
        "red_sha": RED_SHA,
        "state": "terminal-success",
        "timing_status": "complete",
        "junit_timings": _large_timings(),
    }
    assert len(json.dumps(full_reply).encode()) > 131_072
    durable_commits: list[str] = []
    controller_replies: list[dict[str, object]] = []

    def controller(
        action: str,
        _tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        controller_replies.append(full_reply)
        if not durable_commits:
            durable_commits.append(COMMIT_SHA)
            repository.current_head = COMMIT_SHA
        return full_reply

    session, _ledger, _repository, _calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        first = _request(session, "git_commit_candidate", TREE)
        replay = _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert first == replay
    assert first["candidate_tree"] == TREE
    assert first["commit_sha"] == COMMIT_SHA
    assert first["state"] == "terminal-success"
    assert len(json.dumps(first).encode()) <= 131_072
    assert first != full_reply
    assert durable_commits == [COMMIT_SHA]
    assert controller_replies and all(item is full_reply for item in controller_replies)
    assert len(cast(list[object], full_reply["junit_timings"])) == 2


def test_live_worker_git_compacts_large_content_status_reply(tmp_path: Path) -> None:
    full_reply: dict[str, object] = {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "timing_status": "complete",
        "junit_timings": _large_timings(),
    }
    assert len(json.dumps(full_reply).encode()) > 131_072
    observed: list[dict[str, object]] = []

    def controller(
        action: str,
        _tree: str | None,
        _selector: dict[str, object] | None,
        _repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        observed.append(full_reply)
        return full_reply

    session, _ledger, _repository, calls = _session(
        tmp_path, workflow_lane=WorkflowLane.CONTENT, controller=controller
    )
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        status = _request(session, "git_candidate_status", TREE)
    finally:
        session.close()
    assert status["candidate_tree"] == TREE
    assert status["state"] == "terminal-success"
    assert len(json.dumps(status).encode()) <= 131_072
    assert status != full_reply
    assert observed == [full_reply]
    assert calls == [("stage", None, None), ("status", TREE, None)]


@pytest.mark.parametrize(
    ("state", "commit_sha"),
    [
        ("ready", COMMIT_SHA),
        ("terminal-success", "D" * 40),
        ("terminal-success", "z" * 40),
        ("terminal-success", "d" * 39),
    ],
)
def test_live_worker_git_rejects_invalid_controller_commit_proof(
    tmp_path: Path, state: str, commit_sha: str
) -> None:
    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        repository.current_head = commit_sha
        return {
            "candidate_tree": tree,
            "committed_tree": tree,
            "commit_sha": commit_sha,
            "red_sha": RED_SHA,
            "state": state,
        }

    session, _ledger, _repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None), ("commit", TREE, None)]


def test_live_worker_git_recovers_exact_retained_green_head(tmp_path: Path) -> None:
    session, _ledger, repository, calls = _session(
        tmp_path, make_repository=GreenProofRepository
    )
    try:
        status = _request(session, "git_status")
        staged = _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert status["changed_paths"] == list(repository.paths)
    assert staged["candidate_tree"] == TREE
    assert repository.current_head == GREEN_SHA
    assert calls == [("stage", None, None)]


@pytest.mark.parametrize(
    "fault",
    ["unproven", "green_ref", "red_ref", "green_parent", "red_parent", "green_tree"],
)
def test_live_worker_git_rejects_unproven_retained_green_head(
    tmp_path: Path, fault: str
) -> None:
    valid, _ledger, _repository, calls = _session(
        tmp_path, make_repository=GreenProofRepository
    )
    try:
        assert _request(valid, "git_stage_candidate")["candidate_tree"] == TREE
    finally:
        valid.close()
    unexpected_session: Any = None
    try:
        with pytest.raises(
            (RuntimeError, ValueError), match="worker|identity|proof|head"
        ):
            unexpected_session, _ledger, _repository, _calls = _session(
                tmp_path,
                make_repository=lambda path: GreenProofRepository(path, fault=fault),
            )
    finally:
        if unexpected_session is not None:
            unexpected_session.close()
    assert calls == [("stage", None, None)]


@pytest.mark.parametrize(
    "paths",
    [
        tuple(f"{index:03d}" + "a" * 125 for index in range(128)),
        ("é" * 256,),
    ],
    ids=["aggregate-and-count", "utf8-path"],
)
def test_live_worker_git_accepts_status_at_utf8_transport_limits(
    tmp_path: Path, paths: tuple[str, ...]
) -> None:
    session, _ledger, repository, _calls = _session(tmp_path)
    assert len(paths) <= 128
    assert all(len(path.encode()) <= 512 for path in paths)
    assert sum(len(path.encode()) for path in paths) <= 16_384
    repository.paths = paths
    try:
        status = _request(session, "git_status")
    finally:
        session.close()
    assert status["changed_paths"] == list(paths)


@pytest.mark.parametrize(
    "paths",
    [
        tuple(f"file-{index:03d}" for index in range(129)),
        ("a" * 513,),
        ("é" * 257,),
        tuple(f"{index:03d}" + "a" * 126 for index in range(128)),
    ],
    ids=["path-count", "ascii-path-bytes", "utf8-path-bytes", "aggregate-bytes"],
)
def test_live_worker_git_rejects_status_beyond_utf8_transport_limits(
    tmp_path: Path, paths: tuple[str, ...]
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
        repository.paths = paths
        with pytest.raises(Exception, match="worker|read|status|limit"):
            _request(session, "git_status")
    finally:
        session.close()
    assert calls == []


@pytest.mark.parametrize(
    ("drift", "value"),
    [
        ("project_id", 310968),
        ("issue_iid", 13),
        ("revision", 8),
        ("state", RunnerState.PUBLISHING),
        ("phase", WorkflowPhase.READY_TO_PUBLISH),
        ("workflow_lane", WorkflowLane.CONTENT),
        ("checkout", Path("/other/worktree")),
        ("source_sha", "f" * 40),
        ("head", "c" * 40),
        ("branch", "agent/issue-13"),
    ],
)
def test_live_worker_git_denies_identity_or_checkout_drift_before_action(
    tmp_path: Path, drift: str, value: object
) -> None:
    session, ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
        if drift not in {"head", "branch"}:
            assert ledger.current is not None
            field = "worktree" if drift == "checkout" else drift
            ledger.current = ledger.current.model_copy(update={field: value})
        elif drift == "head":
            repository.current_head = cast(str, value)
        elif drift == "branch":
            repository.legacy_branch = cast(str, value)
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert calls == []


def test_live_worker_git_rejects_unstaged_or_stale_candidate_tree(
    tmp_path: Path,
) -> None:
    session, _ledger, _repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"]
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(session, "git_candidate_status", TREE)
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(session, "git_commit_candidate", CORRECTED_TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None)]


def test_live_worker_git_allows_tracked_edit_before_status_and_stage(
    tmp_path: Path,
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        repository.paths = ("src/corrected.py",)
        repository.retained_worktree_fingerprint_value = "d" * 64
        status = _request(session, "git_status")
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
    finally:
        session.close()
    assert status["changed_paths"] == ["src/corrected.py"]
    assert calls == [("stage", None, None)]


def test_live_worker_git_restaging_replaces_bound_tree_after_correction(
    tmp_path: Path,
) -> None:
    staged = [TREE, CORRECTED_TREE]

    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": staged.pop(0), "state": "staged"}
        repository.current_head = COMMIT_SHA
        return {
            "candidate_tree": tree,
            "committed_tree": tree,
            "commit_sha": COMMIT_SHA,
            "red_sha": RED_SHA,
            "state": "terminal-success",
        }

    session, _ledger, repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        repository.paths = ("src/corrected.py",)
        repository.retained_worktree_fingerprint_value = "d" * 64
        assert (
            _request(session, "git_stage_candidate")["candidate_tree"] == CORRECTED_TREE
        )
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(session, "git_commit_candidate", TREE)
        result = _request(session, "git_commit_candidate", CORRECTED_TREE)
    finally:
        session.close()
    assert result["commit_sha"] == COMMIT_SHA
    assert calls == [
        ("stage", None, None),
        ("stage", None, None),
        ("commit", CORRECTED_TREE, None),
    ]


def test_live_worker_git_replays_lost_commit_reply_without_second_commit(
    tmp_path: Path,
) -> None:
    staged_trees = [TREE, CORRECTED_TREE]
    commit_effects: list[str] = []
    cached_proof: dict[str, object] | None = None

    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        nonlocal cached_proof
        if action == "stage":
            return {"candidate_tree": staged_trees.pop(0), "state": "staged"}
        if cached_proof is None:
            commit_effects.append("commit")
            repository.current_head = COMMIT_SHA
            cached_proof = {
                "candidate_tree": TREE,
                "committed_tree": TREE,
                "commit_sha": COMMIT_SHA,
                "red_sha": RED_SHA,
                "state": "terminal-success",
            }
        return cached_proof

    session, _ledger, repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        _request(session, "git_commit_candidate", TREE)  # Simulate a lost reply.
        replay = _request(session, "git_commit_candidate", TREE)
        repository.paths = ("src/corrected.py",)
        repository.retained_worktree_fingerprint_value = "d" * 64
        corrected = _request(session, "git_stage_candidate")
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert replay == {
        "candidate_tree": TREE,
        "committed_tree": TREE,
        "commit_sha": COMMIT_SHA,
        "red_sha": RED_SHA,
        "state": "terminal-success",
    }
    assert corrected["candidate_tree"] == CORRECTED_TREE
    assert commit_effects == ["commit"]
    assert calls.count(("stage", None, None)) == 2


def test_live_worker_git_candidate_status_respects_controller_lane(
    tmp_path: Path,
) -> None:
    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        _repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "status":
            raise ValueError("candidate request is invalid in coding")
        return {"candidate_tree": tree or TREE, "state": "staged"}

    coding, _ledger, _repository, coding_calls = _session(
        tmp_path, controller=controller
    )
    try:
        assert _request(coding, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|candidate"):
            _request(coding, "git_candidate_status", TREE)
    finally:
        coding.close()
    assert coding_calls == [("stage", None, None), ("status", TREE, None)]

    allowed, _ledger, _repository, allowed_calls = _session(
        tmp_path, workflow_lane=WorkflowLane.CONTENT
    )
    try:
        assert _request(allowed, "git_stage_candidate")["candidate_tree"] == TREE
        status = _request(allowed, "git_candidate_status", TREE)
    finally:
        allowed.close()
    assert status == {"candidate_tree": TREE, "state": "ready"}
    assert allowed_calls == [("stage", None, None), ("status", TREE, None)]


@pytest.mark.parametrize("drift", ["head", "branch"])
def test_live_worker_git_denies_checkout_drift_during_stage_action(
    tmp_path: Path, drift: str
) -> None:
    def controller(
        _action: str,
        _tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if drift == "head":
            repository.current_head = "f" * 40
        else:
            repository.legacy_branch = "agent/issue-13"
        return {"candidate_tree": TREE, "state": "staged"}

    session, _ledger, _repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_status")["dirty"] is True
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_stage_candidate")
    finally:
        session.close()
    assert calls == [("stage", None, None)]


@pytest.mark.parametrize("mismatch", ["candidate_tree", "committed_tree"])
def test_live_worker_git_denies_commit_with_mismatched_controller_tree(
    tmp_path: Path, mismatch: str
) -> None:
    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        repository.current_head = COMMIT_SHA
        response: dict[str, object] = {
            "candidate_tree": tree,
            "committed_tree": tree,
            "commit_sha": COMMIT_SHA,
            "red_sha": RED_SHA,
            "state": "terminal-success",
        }
        response[mismatch] = CORRECTED_TREE
        return response

    session, _ledger, _repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None), ("commit", TREE, None)]


@pytest.mark.parametrize("drift", ["head", "branch"])
def test_live_worker_git_denies_checkout_drift_after_stage(
    tmp_path: Path, drift: str
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        if drift == "head":
            repository.current_head = "f" * 40
        else:
            repository.legacy_branch = "agent/issue-13"
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None)]


@pytest.mark.parametrize("drift", ["head", "branch"])
def test_live_worker_git_denies_checkout_drift_during_controller_action(
    tmp_path: Path, drift: str
) -> None:
    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        if action == "stage":
            return {"candidate_tree": TREE, "state": "staged"}
        if drift == "head":
            repository.current_head = "f" * 40
        else:
            repository.current_head = COMMIT_SHA
            repository.legacy_branch = "agent/issue-13"
        return {
            "candidate_tree": tree,
            "committed_tree": tree,
            "commit_sha": COMMIT_SHA,
            "red_sha": RED_SHA,
            "state": "terminal-success",
        }

    session, _ledger, _repository, calls = _session(tmp_path, controller=controller)
    try:
        assert _request(session, "git_stage_candidate")["candidate_tree"] == TREE
        with pytest.raises(Exception, match="worker|read|identity|candidate"):
            _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert calls == [("stage", None, None), ("commit", TREE, None)]


def test_live_worker_git_never_discloses_private_controller_fields(
    tmp_path: Path,
) -> None:
    def controller(
        action: str,
        tree: str | None,
        _selector: dict[str, object] | None,
        repository: GitTrackingRepository,
    ) -> dict[str, object]:
        public: dict[str, object]
        if action == "stage":
            public = {"candidate_tree": TREE, "state": "staged"}
        else:
            repository.current_head = COMMIT_SHA
            public = {
                "candidate_tree": tree,
                "committed_tree": tree,
                "commit_sha": COMMIT_SHA,
                "red_sha": RED_SHA,
                "state": "terminal-success",
            }
        return public | {
            "nonce": "private-nonce",
            "token": "private-token",
            "secret": "private-secret",
            "credential": "private-credential",
        }

    session, _ledger, _repository, calls = _session(tmp_path, controller=controller)
    try:
        stage = _request(session, "git_stage_candidate")
        commit = _request(session, "git_commit_candidate", TREE)
    finally:
        session.close()
    assert stage == {"candidate_tree": TREE, "state": "staged"}
    assert commit == {
        "candidate_tree": TREE,
        "committed_tree": TREE,
        "commit_sha": COMMIT_SHA,
        "red_sha": RED_SHA,
        "state": "terminal-success",
    }
    assert calls == [("stage", None, None), ("commit", TREE, None)]


def test_live_worker_git_denies_unknown_operation_before_provider(
    tmp_path: Path,
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
        response = _raw_request(
            session.socket_path, {"nonce": session.nonce, "action": "git_show"}
        )
    finally:
        session.close()
    assert response is None or response.get("accepted") is False
    assert calls == []


@pytest.mark.parametrize(
    ("state", "phase"),
    [
        (RunnerState.PUBLISHING, WorkflowPhase.WORKFLOW),
        (RunnerState.EXECUTING, WorkflowPhase.READY_TO_PUBLISH),
    ],
)
def test_live_worker_git_factory_rejects_nonworker_phase(
    tmp_path: Path, state: RunnerState, phase: WorkflowPhase
) -> None:
    session, ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
    finally:
        session.close()
    assert ledger.current is not None
    record = ledger.current.model_copy(update={"state": state, "phase": phase})
    with pytest.raises((RuntimeError, ValueError), match="worker|phase|identity"):
        cast(Any, serial).create_live_worker_git_session(
            record,
            _issue(),
            ledger=ledger,
            repository=repository,
            candidate_action=lambda _action, _tree, _selector: {},
            timeout_seconds=10,
            temporary_directory=tmp_path,
        )
    assert calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {"argv": ["git", "show", "HEAD"]},
        {"path": "../secret"},
        {"checkout": "/other"},
        {"role": "candidate-preview"},
        {"project_id": 1},
        {"source_sha": GREEN_SHA},
        {"green_ref": green_commit_ref(12, BASE_SHA, GREEN_TREE)},
        {"red_ref": red_commit_ref(12, BASE_SHA)},
        {"selector": {"path": "README.md"}},
    ],
)
def test_live_worker_git_rejects_caller_selected_git_inputs(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
        response = _raw_request(
            session.socket_path,
            {"nonce": session.nonce, "action": "git_stage_candidate", **extra},
        )
    finally:
        session.close()
    assert response is None or response.get("accepted") is False
    assert calls == []


def test_worker_git_mcp_exposes_only_named_closed_tools(tmp_path: Path) -> None:
    session, _ledger, repository, _calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
    finally:
        session.close()
    server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_git_mcp_server(
        lambda action, tree=None: {"candidate_tree": tree or TREE}
    )
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "worker_git_status",
        "worker_git_stage_candidate",
        "worker_git_candidate_status",
        "worker_git_commit_candidate",
    }
    for tool in tools:
        assert tool.input_schema["additionalProperties"] is False
    for name, arguments in [
        ("worker_context", {}),
        ("worker_git_status", {"argv": ["status"]}),
        ("worker_git_stage_candidate", {"path": "README.md"}),
        ("worker_git_candidate_status", {"candidate_tree": TREE, "extra": True}),
        ("worker_git_commit_candidate", {"candidate_tree": TREE, "argv": ["commit"]}),
    ]:
        result = cast(CallToolResult, asyncio.run(server.call_tool(name, arguments)))
        assert result.is_error


def test_worker_git_mcp_redacts_nested_private_fields_in_both_outputs(
    tmp_path: Path,
) -> None:
    session, _ledger, _repository, _calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["dirty"] is True
    finally:
        session.close()
    private = "private-value"
    raw: dict[str, object] = {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "timing_review": {
            "outcome": "clear",
            "nonce": private,
            "entries": [
                {"name": "safe-case", "token": private},
                {"credential": private, "detail": "safe-detail"},
            ],
        },
        "private_key": private,
    }
    server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_git_mcp_server(lambda _action, _tree=None: raw)
    result = cast(
        CallToolResult,
        asyncio.run(
            server.call_tool("worker_git_candidate_status", {"candidate_tree": TREE})
        ),
    )
    assert not result.is_error
    assert result.structured_content == {
        "candidate_tree": TREE,
        "state": "terminal-success",
        "timing_review": {
            "outcome": "clear",
            "entries": [
                {"name": "safe-case"},
                {"detail": "safe-detail"},
            ],
        },
    }
    assert private not in str(result.structured_content)
    assert private not in str(result.content)


def test_worker_read_mcp_denies_git_action_for_wrong_role(tmp_path: Path) -> None:
    session, _ledger, repository, calls = _session(tmp_path)
    try:
        assert _request(session, "git_status")["changed_paths"] == list(
            repository.paths
        )
    finally:
        session.close()
    dispatched: list[str] = []
    server = cast(
        Any, importlib.import_module("agent_work.mcp_server")
    ).create_worker_read_mcp_server(lambda action: dispatched.append(action) or {})
    result = cast(
        CallToolResult,
        asyncio.run(server.call_tool("worker_git_stage_candidate", {})),
    )
    assert result.is_error
    assert dispatched == []
    assert calls == []
