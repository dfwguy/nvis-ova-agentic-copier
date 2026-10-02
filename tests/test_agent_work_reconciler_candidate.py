# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Committed inspection requires current controller, remote and local proof."""

from __future__ import annotations

import asyncio
import json
import os
from base64 import b64decode
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha1
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult

from agent_work import mcp_server, serial, worker
from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.gitlab import GitLabGateway
from agent_work.ledger import JsonLedger
from agent_work.models import AgentWorkConfig, LedgerRecord
from agent_work.repository import CommandResult, GitRepository
from tests.test_agent_work_actor_frontend import limits as actor_limits
from tests.test_agent_work_actor_mcp_host import (
    SAFE_FEATURES,
    SAFE_PRIVACY,
    InvokerTranscript,
)

TARGET = "a" * 40
TREE = "b" * 40
BASE_TREE = "c" * 40
FINAL = "d" * 40
LEASE = "e" * 40
OTHER = "f" * 40
REMOTE_BRANCH = "clanker/preserved-source"
LOCAL_BRANCH = "clanker/fresh-inspection"
NONCE = "0123456789abcdef" * 2
DATA = b"committed content\n"
BLOB = sha1(b"blob " + str(len(DATA)).encode() + b"\0" + DATA).hexdigest()


class _Observations:
    """Generated Git/API assets served only at injected external boundaries."""

    def __init__(self, root: Path, *, same_branch: bool = False) -> None:
        root.mkdir(parents=True)
        self.root = root
        transport_name = sha1(str(root.parent).encode()).hexdigest()[:8]
        self.transport_root = root.parents[1] / f"t-{transport_name}"
        self.transport_root.mkdir(exist_ok=True)
        self.checkout = root / "inspection"
        self.checkout.mkdir()
        self.matching_checkouts = {self.checkout}
        self.local_branch = REMOTE_BRANCH if same_branch else LOCAL_BRANCH
        self.head = FINAL
        self.tree = TREE
        self.origin = "https://gitlab.example.com/group/project.git"
        self.source_ref = LEASE
        self.target_ref = TARGET
        self.dirty = ""
        self.ancestor = True
        self.git_state = "absent"
        self.blob_reads = 0
        self.drift_on_blob = 0
        self.ref_fault = ""
        self.commands: list[tuple[str, ...]] = []
        self.api: list[str] = []
        self.project: dict[str, object] = {
            "id": 81,
            "default_branch": "main",
            "web_url": "https://gitlab.example.com/group/project",
            "http_url_to_repo": self.origin,
        }
        self.mr: dict[str, object] = {
            "iid": 17,
            "state": "opened",
            "source_project_id": 81,
            "target_project_id": 81,
            "source_branch": REMOTE_BRANCH,
            "target_branch": "main",
            "sha": LEASE,
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/17",
        }
        self.config = AgentWorkConfig(
            host="gitlab.example.com",
            project_id=81,
            checkout=self.checkout,
            state_root=root / "state",
            git_path=Path("/fixture/git"),
            glab_path=Path("/fixture/glab"),
            base_branch="agent/base",
        )
        self.ledger = JsonLedger(
            root / "state", clock=lambda: datetime(2026, 10, 2, tzinfo=UTC)
        )
        self.record = self.ledger.persist(
            LedgerRecord(
                project_id=81,
                issue_iid=417,
                merge_request_iid=17,
                state=RunnerState.EXECUTING,
                phase=WorkflowPhase.WORKFLOW,
                source_branch=REMOTE_BRANCH,
                source_sha=LEASE,
                worktree=root / "preserved-owner-checkout",
            )
        )
        self.repository = GitRepository(
            self.config,
            self.git,
            service_environment={"GITLAB_TOKEN": "generated-unused-token"},
            read_git_blob=self.read_blob,
            asset_lstat=self.lstat,
        )
        self.gitlab = GitLabGateway(self.config, self.http)

    def identity(self, **changes: object) -> worker.ReconcilerCandidateIdentity:
        """Construct strict input, independently of observed owner and objects."""
        values: dict[str, object] = {
            "role": "finalizer",
            "host": "gitlab.example.com",
            "project_id": 81,
            "merge_request_iid": 17,
            "source_branch": REMOTE_BRANCH,
            "source_lease_sha": LEASE,
            "checkout_branch": self.local_branch,
            "target_branch": "main",
            "target_sha": TARGET,
            "final_sha": FINAL,
            "committed_tree": TREE,
            "identity_kind": "committed",
        }
        return worker.ReconcilerCandidateIdentity(**(values | changes))

    def http(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        """Serve actual GitLabGateway parsing without any host request."""
        assert command[:6] == (
            "/fixture/glab",
            "api",
            "--hostname",
            "gitlab.example.com",
            "-X",
            "GET",
        )
        endpoint = command[6]
        self.api.append(endpoint)
        payload = {
            "projects/81": self.project,
            "projects/81/merge_requests/17": self.mr,
        }
        if endpoint not in payload:
            pytest.fail(f"unexpected API operation: {endpoint}")
        return CommandResult(0, json.dumps(payload[endpoint]), "")

    def git(self, command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        """Observe finite read recipes, forbidding every mutating Git operation."""
        assert command[:2] == ("/fixture/git", "-C")
        assert Path(command[2]) in self.matching_checkouts
        args = command[3:]
        while args[:1] == ("-c",):
            args = args[2:]
        self.commands.append(args)
        if args[:1] == ("ls-remote",):
            refs = {
                f"refs/heads/{REMOTE_BRANCH}": self.source_ref,
                "refs/heads/main": self.target_ref,
            }
            selected = [ref for ref in args if ref in refs]
            assert selected
            if self.ref_fault == "missing":
                return CommandResult(0, "", "")
            if self.ref_fault == "malformed":
                return CommandResult(0, "PRIVATE_REF\n", "")
            if self.ref_fault == "duplicate":
                return CommandResult(
                    0, f"{LEASE}\trefs/heads/{REMOTE_BRANCH}\n" * 2, ""
                )
            return CommandResult(
                0, "".join(f"{refs[ref]}\t{ref}\n" for ref in selected), ""
            )
        if args[:2] == ("merge-base", "--is-ancestor"):
            assert args[2:] in {(TARGET, FINAL), (TARGET, OTHER)}
            return CommandResult(0 if self.ancestor else 1, "", "")
        if args[:2] == ("rev-parse", "--git-path"):
            assert args[2] in {"MERGE_HEAD", "rebase-merge", "rebase-apply"}
            return CommandResult(0, str(self.root / "git-state" / args[2]), "")
        outputs = {
            ("remote", "get-url", "origin"): self.origin,
            ("branch", "--show-current"): self.local_branch,
            ("symbolic-ref", "--short", "HEAD"): self.local_branch,
            ("rev-parse", "HEAD"): self.head,
            ("rev-parse", "HEAD^{tree}"): self.tree,
            ("rev-parse", f"{FINAL}^{{tree}}"): self.tree,
            ("rev-parse", f"{OTHER}^{{tree}}"): self.tree,
            ("rev-parse", f"{TARGET}^{{tree}}"): BASE_TREE,
            ("status", "--porcelain"): self.dirty,
            ("status", "--porcelain=v1", "-z"): self.dirty,
            ("status", "--porcelain", "-z"): self.dirty,
            (
                "diff",
                "--name-only",
                "-z",
                "--no-renames",
                BASE_TREE,
                TREE,
            ): "src/example.py\0template/src/example.py\0",
            ("cat-file", "-s", BLOB): str(len(DATA)),
        }
        if args in outputs:
            return CommandResult(0, outputs[args], "")
        if args[:2] == ("ls-tree", "-z"):
            tree, separator, path = args[2:]
            assert separator == "--" and path in {
                "src/example.py",
                "template/src/example.py",
            }
            return CommandResult(
                0, "" if tree == BASE_TREE else f"100644 blob {BLOB}\t{path}\0", ""
            )
        pytest.fail(f"unexpected or mutating Git operation: {args}")

    def lstat(self, path: Path) -> os.stat_result:
        """Only generated assets may be inspected, with explicit state failures."""
        assert path.is_relative_to(self.root)
        if path.name in {"MERGE_HEAD", "rebase-merge", "rebase-apply"}:
            if self.git_state == "unreadable":
                raise PermissionError("PRIVATE_STATE")
            if self.git_state == path.name:
                return self.checkout.lstat()
            raise FileNotFoundError(path)
        return path.lstat()

    def read_blob(self, command: tuple[str, ...], **_kwargs: object) -> bytes:
        """Return actual hash-bound bytes and optionally move observed HEAD."""
        assert command[3:] == ("cat-file", "blob", BLOB)
        self.blob_reads += 1
        if self.blob_reads == self.drift_on_blob:
            self.head = OTHER
        return DATA

    def transport_inventory(self) -> set[Path]:
        """Observe broker directories and sockets at their actual owning root."""
        return set(self.transport_root.rglob("*"))

    def session(self):
        """Enter the public producer, using the genuine current ledger record."""
        return serial.create_reconciler_candidate_session(
            self.identity(),
            worktree=self.checkout,
            record=self.record,
            ledger=self.ledger,
            gitlab=self.gitlab,
            repository=self.repository,
            timeout_seconds=10,
            nonce_factory=lambda: NONCE,
            temporary_directory=self.transport_root,
        )

    def checks(self):
        """Create both real checks, never a synthetic verification result."""
        return serial.create_reconciler_candidate_check(
            self.identity(),
            worktree=self.checkout,
            record=self.record,
            ledger=self.ledger,
            gitlab=self.gitlab,
            repository=self.repository,
        )


def _request(
    profile: worker.RoleExecutionProfile,
    action: worker.CandidateOperation,
    selector: dict[str, object] | None = None,
) -> dict[str, object]:
    return worker.request_candidate_validation(
        profile.candidate_socket,
        worker.WorkerResultBrokerClientRequest(
            nonce=profile.nonce, issue_iid=profile.issue_iid
        ),
        action=action,
        candidate_tree=profile.candidate_tree,
        selector=selector,
    )


def _mcp(
    subject: _Observations,
    profile: worker.RoleExecutionProfile,
    operation: Callable[[Client], Awaitable[CallToolResult]],
) -> object:
    servers: list[MCPServer] = []
    mcp_server.main(
        environment={
            "AGENT_WORK_CANDIDATE_SOCKET": str(profile.candidate_socket),
            "AGENT_WORK_CANDIDATE_NONCE": profile.nonce,
            "AGENT_WORK_CANDIDATE_ISSUE_IID": str(subject.record.issue_iid),
            "AGENT_WORK_CANDIDATE_TREE": profile.candidate_tree,
            "AGENT_WORK_CANDIDATE_ROLE": profile.role,
        },
        run_server=servers.append,
    )

    async def run() -> object:
        async with Client(servers[0]) as client:
            return await operation(client)

    return asyncio.run(run())


@pytest.mark.parametrize("same_branch", [False, True])
def test_committed_session_prebinds_reads_without_validation_or_staging(
    tmp_path: Path, *, same_branch: bool
) -> None:
    subject = _Observations(tmp_path / "subject", same_branch=same_branch)
    with subject.session() as (broker, profile):
        status = _request(profile, "status")
        assert status["state"] == "inspection-ready"
        assert (status["commit_sha"], status["committed_tree"]) == (FINAL, TREE)
        inventory = _request(profile, "read", {"mode": "changed_paths"})
        assert inventory["changed_paths"] == [
            "src/example.py",
            "template/src/example.py",
        ]
        assert inventory["ledger_revision"] == subject.record.revision
        assert inventory["base_sha"] == TARGET
        assert broker.validated_candidate is None
        with pytest.raises(worker.WorkerProtocolError):
            broker.begin_review(TREE)
    assert not profile.candidate_socket.exists()
    assert subject.ledger.load_read_only(417) == subject.record
    assert all(
        args[0]
        not in {
            "stage",
            "add",
            "write-tree",
            "commit",
            "fetch",
            "push",
            "rebase",
            "update-ref",
        }
        for args in subject.commands
    )


@pytest.mark.parametrize("batch", [False, True])
def test_real_session_socket_mcp_returns_exact_single_and_grouped_committed_bytes(
    tmp_path: Path, *, batch: bool
) -> None:
    subject = _Observations(tmp_path / "subject")

    async def read(client: Client) -> CallToolResult:
        await client.call_tool("candidate_changed_paths", {})
        selection = {
            "path": "src/example.py",
            "side": "new",
            "offset": 0,
            "length": len(DATA),
        }
        arguments = (
            {"reads": [selection, selection | {"path": "template/src/example.py"}]}
            if batch
            else selection
        )
        return await client.call_tool("candidate_read", arguments)

    with subject.session() as (_broker, profile):
        result = _mcp(subject, profile, read)
        assert isinstance(result, CallToolResult) and not result.is_error
        payload = result.structured_content
        assert isinstance(payload, dict)
        assert payload["commit_sha"] == FINAL and payload["committed_tree"] == TREE
        assert payload["ledger_revision"] == subject.record.revision
        assert (
            not {
                "merge_request_iid",
                "source_lease_sha",
                "checkout_branch",
                "target_sha",
            }
            & payload.keys()
        )
        if batch:
            pooled = payload["contents"]
            assert isinstance(pooled, list)
            assert len(pooled) == 1
            entry = pooled[0]
            assert isinstance(entry, dict)
            assert set(entry) == {"data_base64"}
            assert b64decode(entry["data_base64"], validate=True) == DATA
            assert payload["bindings"] == [
                {
                    "request_index": index,
                    "path": path,
                    "side": "new",
                    "offset": 0,
                    "length": len(DATA),
                    "oid": BLOB,
                    "size": len(DATA),
                    "content_index": 0,
                }
                for index, path in enumerate(
                    ("src/example.py", "template/src/example.py")
                )
            ]
        else:
            assert b64decode(payload["data_base64"]) == DATA


IDENTITY_MUTATIONS = (
    ("role", "router"),
    ("host", "other.example.com"),
    ("project_id", 82),
    ("merge_request_iid", 18),
    ("source_branch", LOCAL_BRANCH),
    ("checkout_branch", REMOTE_BRANCH),
    ("source_lease_sha", OTHER),
    ("target_branch", "release"),
    ("target_sha", OTHER),
    ("final_sha", OTHER),
    ("committed_tree", OTHER),
)


@pytest.mark.parametrize(("field", "value"), IDENTITY_MUTATIONS)
@settings(max_examples=4)
@given(
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8
    )
)
def test_generated_private_identity_mutations_fail_real_captured_checker(
    tmp_path_factory: pytest.TempPathFactory, field: str, value: object, suffix: str
) -> None:
    subject = _Observations(tmp_path_factory.mktemp("identity") / "subject")
    full, local = subject.checks()
    assert full(subject.identity()) and local(subject.identity())
    changed = (
        f"clanker/generated-{suffix}"
        if field in {"source_branch", "checkout_branch"}
        else value
    )
    assert not full(subject.identity(**{field: changed}))
    assert not local(subject.identity(**{field: changed}))
    gateway = serial.create_reconciler_candidate_gateway(
        subject.identity(**{field: changed}),
        worktree=subject.checkout,
        record=subject.record,
        check_candidate=local,
        repository=subject.repository,
    )
    with pytest.raises(worker.WorkerProtocolError):
        gateway("status", TREE, None)


@pytest.mark.parametrize(
    "fault",
    [
        "head",
        "index-only",
        "same-tree-ancestor",
        "tree",
        "branch",
        "source-ref",
        "target-ref",
        "dirty",
        "ancestry",
        "origin",
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "unreadable",
    ],
)
def test_independent_local_and_remote_observation_faults_refuse_session(
    tmp_path: Path, fault: str
) -> None:
    subject = _Observations(tmp_path / "subject")
    expected = subject.identity()
    before = subject.transport_inventory()
    if fault in {"head", "index-only", "same-tree-ancestor"}:
        subject.head = OTHER
    elif fault == "tree":
        subject.tree = OTHER
    elif fault == "branch":
        subject.local_branch = REMOTE_BRANCH
    elif fault == "source-ref":
        subject.source_ref = OTHER
    elif fault == "target-ref":
        subject.target_ref = OTHER
    elif fault == "dirty":
        subject.dirty = " M src/example.py\n"
    elif fault == "ancestry":
        subject.ancestor = False
    elif fault == "origin":
        subject.origin = "https://user:secret@gitlab.example.com/group/project.git"
    else:
        subject.git_state = fault
    with (
        pytest.raises(worker.WorkerProtocolError),
        serial.create_reconciler_candidate_session(
            expected,
            worktree=subject.checkout,
            record=subject.record,
            ledger=subject.ledger,
            gitlab=subject.gitlab,
            repository=subject.repository,
            timeout_seconds=10,
            nonce_factory=lambda: NONCE,
            temporary_directory=subject.transport_root,
        ),
    ):
        pytest.fail("unverified session released")
    assert subject.transport_inventory() == before


@pytest.mark.parametrize(
    ("owner", "field", "value"),
    [
        ("project", "id", 82),
        ("mr", "iid", 18),
        ("mr", "source_project_id", 82),
        ("mr", "target_project_id", 82),
        ("mr", "source_branch", LOCAL_BRANCH),
        ("mr", "target_branch", "release"),
        ("mr", "sha", OTHER),
        ("mr", "state", "merged"),
    ],
)
def test_actual_gitlab_gateway_wrong_project_or_mr_refuses(
    tmp_path: Path, owner: str, field: str, value: object
) -> None:
    subject = _Observations(tmp_path / "subject")
    full, _local = subject.checks()
    assert full(subject.identity())
    payload = subject.project if owner == "project" else subject.mr
    payload[field] = value
    assert not full(subject.identity())


def test_genuine_ledger_revision_drift_refuses_existing_checks_and_broker_reads(
    tmp_path: Path,
) -> None:
    subject = _Observations(tmp_path / "subject")
    with subject.session() as (_broker, profile):
        assert _request(profile, "status")["state"] == "inspection-ready"
        changed = subject.ledger.persist(subject.record)
        assert changed.revision == subject.record.revision + 1
        with pytest.raises(worker.WorkerProtocolError):
            _request(profile, "read", {"mode": "changed_paths"})


@pytest.mark.parametrize("fault", ["missing", "duplicate", "malformed"])
def test_remote_ref_evidence_must_be_present_unique_and_exact(
    tmp_path: Path, fault: str
) -> None:
    subject = _Observations(tmp_path / "subject")
    full, _local = subject.checks()
    assert full(subject.identity())
    subject.ref_fault = fault
    assert not full(subject.identity())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_id", 82),
        ("merge_request_iid", 18),
        ("source_branch", LOCAL_BRANCH),
        ("source_sha", OTHER),
    ],
)
def test_genuine_current_owner_fields_cannot_be_replaced_by_expected_input(
    tmp_path: Path, field: str, value: object
) -> None:
    subject = _Observations(tmp_path / "subject")
    full, local = subject.checks()
    assert full(subject.identity())
    subject.ledger.persist(subject.record.model_copy(update={field: value}))
    assert not full(subject.identity()) and not local(subject.identity())


def test_missing_current_owner_record_cannot_open_inspection_session(
    tmp_path: Path,
) -> None:
    subject = _Observations(tmp_path / "subject")
    (subject.ledger.records / "417.json").unlink()
    before = subject.transport_inventory()
    with pytest.raises(worker.WorkerProtocolError), subject.session():
        pytest.fail("missing controller ownership released a broker")
    assert subject.transport_inventory() == before


def test_per_read_checks_use_local_proof_and_full_check_reobserves_remote(
    tmp_path: Path,
) -> None:
    subject = _Observations(tmp_path / "subject")
    with subject.session() as (_broker, profile):
        api_count = len(subject.api)
        remote_count = sum(args[0] == "ls-remote" for args in subject.commands)
        for _ in range(3):
            assert (
                _request(profile, "read", {"mode": "changed_paths"})["base_sha"]
                == TARGET
            )
        assert len(subject.api) == api_count
        assert sum(args[0] == "ls-remote" for args in subject.commands) == remote_count
        subject.source_ref = OTHER
        full, _local = subject.checks()
        assert not full(subject.identity())


@settings(max_examples=6)
@given(offset=st.integers(min_value=0, max_value=len(DATA) - 1))
def test_generated_public_committed_ranges_preserve_exact_bytes_and_closed_identity(
    tmp_path_factory: pytest.TempPathFactory, offset: int
) -> None:
    subject = _Observations(tmp_path_factory.mktemp("range") / "subject")
    with subject.session() as (_broker, profile):
        result = _request(
            profile,
            "read",
            {
                "mode": "range",
                "path": "src/example.py",
                "side": "new",
                "offset": offset,
                "length": len(DATA) - offset,
            },
        )
        encoded = result["data_base64"]
        assert isinstance(encoded, str)
        assert b64decode(encoded) == DATA[offset:]
        assert result["commit_sha"] == FINAL and result["committed_tree"] == TREE
        assert result["ledger_revision"] == subject.record.revision
        assert (
            not {
                "checkout_branch",
                "target_sha",
                "source_lease_sha",
                "merge_request_iid",
            }
            & result.keys()
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "untrusted-role"),
        ("project_id", True),
        ("merge_request_iid", 0),
        ("source_lease_sha", "short"),
        ("identity_kind", "staged"),
        ("checkout_branch", "../escape"),
    ],
)
def test_strict_public_identity_constructor_rejects_malformed_expectations(
    tmp_path: Path, field: str, value: object
) -> None:
    subject = _Observations(tmp_path / "subject")
    with pytest.raises(ValueError):
        subject.identity(**{field: value})


@pytest.mark.parametrize("action", ["stage", "commit", "validate", "review"])
def test_inspection_session_never_grants_mutation_validation_or_review(
    tmp_path: Path, action: worker.CandidateOperation
) -> None:
    subject = _Observations(tmp_path / "subject")
    with subject.session() as (broker, profile):
        assert _request(profile, "status")["state"] == "inspection-ready"
        with pytest.raises(worker.WorkerProtocolError):
            _request(profile, action)
        assert broker.validated_candidate is None and broker.review_result is None


def test_public_session_refuses_real_factory_broker_bound_to_another_role(
    tmp_path: Path,
) -> None:
    subject = _Observations(tmp_path / "subject")
    foreign = _Observations(tmp_path / "foreign")
    with serial.create_reconciler_candidate_session(
        foreign.identity(role="router"),
        worktree=foreign.checkout,
        record=foreign.record,
        ledger=foreign.ledger,
        gitlab=foreign.gitlab,
        repository=foreign.repository,
        timeout_seconds=10,
        nonce_factory=lambda: "fedcba9876543210" * 2,
        temporary_directory=foreign.transport_root,
    ) as (foreign_broker, foreign_profile):
        assert _request(foreign_profile, "status")["state"] == "inspection-ready"

        before = subject.transport_inventory()

        def external_factory(
            *_args: object, **_kwargs: object
        ) -> worker.CandidateReviewBroker:
            return foreign_broker

        with (
            pytest.raises(worker.WorkerProtocolError),
            serial.create_reconciler_candidate_session(
                subject.identity(),
                worktree=subject.checkout,
                record=subject.record,
                ledger=subject.ledger,
                gitlab=subject.gitlab,
                repository=subject.repository,
                timeout_seconds=10,
                nonce_factory=lambda: NONCE,
                temporary_directory=subject.transport_root,
                broker_factory=external_factory,
            ),
        ):
            pytest.fail("factory broker for another verified role released content")
        assert subject.transport_inventory() == before
        assert _request(foreign_profile, "status")["state"] == "inspection-ready"
    assert not foreign_profile.candidate_socket.exists()
    assert subject.ledger.load_read_only(417) == subject.record
    assert foreign.ledger.load_read_only(417) == foreign.record


def test_grouped_member_head_drift_releases_no_partial_mcp_result(
    tmp_path: Path,
) -> None:
    subject = _Observations(tmp_path / "subject")
    subject.drift_on_blob = 2

    async def read(client: Client) -> CallToolResult:
        return await client.call_tool(
            "candidate_read",
            {
                "reads": [
                    {"path": path, "side": "new", "offset": 0, "length": len(DATA)}
                    for path in ("src/example.py", "template/src/example.py")
                ]
            },
        )

    with subject.session() as (_broker, profile):
        result = _mcp(subject, profile, read)
        assert isinstance(result, CallToolResult) and result.is_error
        assert result.structured_content is None
        assert DATA.decode().strip() not in str(result.content)


class _DriftingTranscript(InvokerTranscript):
    """External actor transcript moves controller proof at one exact boundary."""

    callback: Callable[[str], None] = staticmethod(lambda _method: None)
    setup_calls: list[str]
    command_calls: list[tuple[str, ...]]

    def respond(self, message: dict[str, object]) -> None:
        super().respond(message)
        self.callback(str(message["method"]))


def _invoker(
    subject: _Observations,
    *,
    phase: str = "",
    fault: str = "",
    missing_limits: bool = False,
):
    uv = subject.root / "fixture-uv"
    uv.write_text("generated nonexecuted executable")
    uv.chmod(0o700)
    source = subject.root / "source-home"
    source.mkdir()
    process = _DriftingTranscript(
        subject.root / "process",
        source,
        subject.ledger.state_root,
        uv,
        phase=phase,
        fault=fault,
    )

    process.setup_calls = []
    process.command_calls = []

    def preview_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        process.command_calls.append(command)
        return CommandResult(0, "generated preview success", "")

    def host_audit(
        _command: tuple[str, ...], _environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        process.setup_calls.append("host-audit")
        return SAFE_FEATURES

    def sandbox_audit(
        _command: tuple[str, ...], _environment: Mapping[str, str]
    ) -> Mapping[str, bool]:
        process.setup_calls.append("sandbox-audit")
        return SAFE_PRIVACY | (
            {"write_denied": False} if fault == "privacy-audit" else {}
        )

    frontend = worker.actor_frontend.create_stdio_actor_frontend(process.ports())

    def start_frontend(
        spec: worker.actor_frontend.ActorProcessSpec,
        limits: worker.actor_frontend.ActorTransportLimits,
    ) -> AbstractContextManager[worker.actor_frontend.ActorSessionPort]:
        process.setup_calls.append("frontend")
        return frontend(spec, limits)

    invoker = worker.AgentInvoker(
        preview_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source),
            "AGENT_WORK_UV": str(uv),
        },
        temporary_directory=subject.root,
        controller_state_root=subject.ledger.state_root,
        read_only_host_audit=host_audit,
        read_only_sandbox_audit=sandbox_audit,
        actor_transport_limits=None if missing_limits else actor_limits(),
        actor_frontend_factory=start_frontend,
        clock=process.clock,
    )
    return invoker, process


@pytest.mark.parametrize(
    "drift", ["none", "before-host", "before-thread", "before-turn", "completion"]
)
def test_actual_invoker_committed_checks_gate_turn_and_discard_completion_drift(
    tmp_path: Path, drift: str
) -> None:
    subject = _Observations(tmp_path / "subject")
    invoker, process = _invoker(subject)
    try:
        with subject.session() as (broker, profile):
            assert subject.checkout != subject.record.worktree
            assert replace(profile, inspection_checkout=subject.checkout) == profile
            if drift == "before-host":
                subject.head = OTHER

            def move(method: str) -> None:
                if (
                    (drift == "before-thread" and method == "config/read")
                    or (drift == "before-turn" and method == "thread/start")
                    or (drift == "completion" and method == "turn/start")
                ):
                    subject.head = OTHER

            process.callback = move
            result = invoker.invoke(
                subject.checkout, "Inspect committed bytes", 10, role_profile=profile
            )
            turns = sum(
                message["method"] == "turn/start" for message in process.requests
            )
            if drift == "none":
                assert (
                    result.returncode == 0
                    and result.stdout == "review complete"
                    and turns == 1
                )
            else:
                assert (
                    result.returncode is None
                    and result.stdout == ""
                    and result.stderr == "actor_identity_changed"
                )
                assert turns == (1 if drift == "completion" else 0)
            assert broker.validated_candidate is None
            assert process.command_calls == []
    finally:
        process.dispose()


@pytest.mark.parametrize(
    "fault",
    [
        "profile-tree",
        "profile-role",
        "profile-socket",
        "profile-nonce",
        "profile-issue",
        "privacy",
        "transport",
        "limits",
    ],
)
def test_actual_invoker_profile_disagreement_and_host_refusals_have_no_fallback(
    tmp_path: Path, fault: str
) -> None:
    subject = _Observations(tmp_path / "subject")
    invoker, process = _invoker(
        subject,
        phase="config/read",
        fault="privacy-audit"
        if fault == "privacy"
        else "eof"
        if fault == "transport"
        else "",
        missing_limits=fault == "limits",
    )
    try:
        with subject.session() as (_broker, profile):
            if fault == "profile-tree":
                profile = replace(profile, candidate_tree=OTHER)
            elif fault == "profile-role":
                profile = replace(profile, role="router")
            elif fault == "profile-socket":
                profile = replace(
                    profile, candidate_socket=subject.root / "unbound.sock"
                )
            elif fault == "profile-nonce":
                profile = replace(profile, nonce="different-controller-nonce")
            elif fault == "profile-issue":
                profile = replace(profile, issue_iid=418)
            result = invoker.invoke(
                subject.checkout, "Inspect", 10, role_profile=profile
            )
            assert result.returncode is None and result.stdout == ""
            assert result.stderr in {
                "actor_identity_changed",
                "actor_config_unverified",
                "actor_frontend_unavailable",
                "actor_limits_unavailable",
                "actor_privacy_unavailable",
            }
            assert process.command_calls == []
            assert all(
                message["method"] != "turn/start" for message in process.requests
            )
    finally:
        process.dispose()


@pytest.mark.parametrize(
    "fault",
    [
        "preview-role",
        "other-checkout",
        "redirected-profile",
        "missing-identity",
        "missing-check",
        "missing-profile-check",
        "missing-checkout",
        "checkout-only",
    ],
)
def test_committed_invocation_refuses_dispatch_checkout_and_incomplete_proof_before_setup(
    tmp_path: Path, fault: str
) -> None:
    subject = _Observations(tmp_path / "subject")
    other_checkout = subject.root / "matching-other-checkout"
    other_checkout.mkdir()
    subject.matching_checkouts.add(other_checkout)
    # B has the same committed content and observations, but is a different path.
    observed = subject.repository.read_candidate_range(
        other_checkout,
        TREE,
        base_sha=TARGET,
        candidate_sha=FINAL,
        allow_base_ancestor=True,
        path="src/example.py",
        side="new",
        offset=0,
        length=len(DATA),
    )
    assert observed["oid"] == BLOB and observed["size"] == len(DATA)
    assert b64decode(observed["data_base64"], validate=True) == DATA
    assert subject.checkout != subject.record.worktree
    invoker, process = _invoker(subject)
    try:
        with subject.session() as (_broker, profile):
            invocation_checkout = subject.checkout
            if fault == "preview-role":
                profile = replace(profile, role="candidate-preview")
            elif fault in {"other-checkout", "redirected-profile"}:
                invocation_checkout = other_checkout
                if fault == "redirected-profile":
                    profile = replace(profile, inspection_checkout=other_checkout)
            else:
                field = {
                    "missing-identity": "committed_identity",
                    "missing-check": "committed_check",
                    "missing-profile-check": "inspection_profile_check",
                    "missing-checkout": "inspection_checkout",
                }
                changes = (
                    {
                        "committed_identity": None,
                        "committed_check": None,
                        "inspection_profile_check": None,
                    }
                    if fault == "checkout-only"
                    else {field[fault]: None}
                )
                profile = replace(profile, **changes)
            result = invoker.invoke(
                invocation_checkout, "Inspect committed bytes", 10, role_profile=profile
            )
            assert result == worker.WorkerInvocation(
                None, stdout="", stderr="actor_identity_changed"
            )
            assert process.command_calls == []
            assert process.setup_calls == []
            assert process.events == []
            assert process.requests == []
    finally:
        process.dispose()


@pytest.mark.parametrize("exceptional_exit", [False, True])
def test_real_transport_cleanup_preserves_foreign_live_session(
    tmp_path: Path, *, exceptional_exit: bool
) -> None:
    subject = _Observations(tmp_path / "subject")
    foreign = _Observations(tmp_path / "foreign")
    assert subject.transport_root == foreign.transport_root
    original = subject.transport_inventory()
    with foreign.session() as (_foreign_broker, foreign_profile):
        foreign_inventory = subject.transport_inventory()
        assert foreign_profile.candidate_socket.is_socket()
        assert _request(foreign_profile, "status")["state"] == "inspection-ready"

        admitted_sockets: list[Path] = []

        def enter_and_leave() -> None:
            with subject.session() as (_broker, profile):
                socket_path = profile.candidate_socket
                admitted_sockets.append(socket_path)
                assert socket_path.is_socket()
                assert socket_path.parent in subject.transport_inventory()
                assert _request(profile, "status")["state"] == "inspection-ready"
                assert (
                    _request(profile, "read", {"mode": "changed_paths"})["commit_sha"]
                    == FINAL
                )
                if exceptional_exit:
                    raise ValueError("generated context exit")

        if exceptional_exit:
            with pytest.raises(ValueError, match="generated context exit"):
                enter_and_leave()
        else:
            enter_and_leave()
        assert len(admitted_sockets) == 1
        removed = admitted_sockets[0]
        assert not removed.exists() and not removed.parent.exists()
        assert subject.transport_inventory() == foreign_inventory
        assert (
            _request(foreign_profile, "read", {"mode": "changed_paths"})["commit_sha"]
            == FINAL
        )
        assert foreign_profile.candidate_socket.is_socket()
    assert not foreign_profile.candidate_socket.exists()
    assert not foreign_profile.candidate_socket.parent.exists()
    assert subject.transport_inventory() == original


@pytest.mark.parametrize("bound_checkout", [False, True])
@settings(max_examples=3)
@given(
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=8
    )
)
def test_generated_invocation_checkout_binding_accepts_only_captured_session_path(
    tmp_path_factory: pytest.TempPathFactory, suffix: str, *, bound_checkout: bool
) -> None:
    subject = _Observations(tmp_path_factory.mktemp("invocation") / "subject")
    other = subject.root / f"checkout-{suffix}"
    other.mkdir()
    subject.matching_checkouts.add(other)
    invoker, process = _invoker(subject)
    try:
        with subject.session() as (_broker, profile):
            result = invoker.invoke(
                subject.checkout if bound_checkout else other,
                "Inspect generated committed checkout",
                10,
                role_profile=profile,
            )
            if bound_checkout:
                assert result.returncode == 0 and result.stdout == "review complete"
                assert (
                    sum(
                        message["method"] == "turn/start"
                        for message in process.requests
                    )
                    == 1
                )
            else:
                assert result == worker.WorkerInvocation(
                    None, stdout="", stderr="actor_identity_changed"
                )
                assert process.setup_calls == [] and process.requests == []
            assert process.command_calls == []
    finally:
        process.dispose()
