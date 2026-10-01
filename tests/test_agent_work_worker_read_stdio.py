# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Public stdio dispatch preserves the real worker-read broker boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, ValidationError

from agent_work import mcp_server, serial
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase
from agent_work.models import LedgerRecord
from agent_work.worker import candidate_tools_for_role
from agent_work.worker_read import WorkerReadBrokerSession
from tests.test_agent_work_live_worker_read import HeadTrackingRepository
from tests.test_agent_work_serial_runtime import _issue, _record

NONCE = "a" * 32
SHA = "a" * 40
WORKER_TOOLS = {"worker_context", "worker_issue", "worker_ledger"}
READ_TOOLS = ("candidate_status", "candidate_changed_paths", "candidate_read")
ROLE_TOOLS = {
    "candidate-preview": (*READ_TOOLS, "candidate_submit_review"),
    "router": READ_TOOLS,
    "judge": (*READ_TOOLS, "candidate_submit_review"),
    "finalizer": READ_TOOLS,
    "supervisor-inspect": ("candidate_status",),
}
SOCKETS = st.text(st.characters(exclude_characters="\x00"), min_size=1)
NONCES = st.text(alphabet="0123456789abcdef", min_size=32, max_size=32)
UNKNOWN = st.text().filter(lambda value: value not in {"candidate", "worker-read"})
BAD_FIELDS = [
    ("socket_path", None),
    ("socket_path", ""),
    ("socket_path", "s\x00private"),
    ("socket_path", 12),
    ("nonce", None),
    ("nonce", ""),
    ("nonce", "a" * 31),
    ("nonce", "a" * 33),
    ("nonce", "g" * 32),
    ("nonce", "A" * 32),
    ("nonce", 12),
]


class _PublicMain(Protocol):
    def __call__(
        self, *, environment: Mapping[str, str], run_server: Callable[[MCPServer], None]
    ) -> None: ...


class _Configuration(Protocol):
    socket_path: str
    nonce: str


class _Parser(Protocol):
    def __call__(self, environment: Mapping[str, str]) -> _Configuration | None: ...


def _parse(environment: Mapping[str, str]) -> _Configuration | None:
    name = "parse_mcp_launch_configuration"
    parser = cast(_Parser, getattr(mcp_server, name))
    return parser(environment)


def _model() -> type[BaseModel]:
    name = "WorkerReadMCPLaunchConfiguration"
    return cast(type[BaseModel], getattr(mcp_server, name))


def _environment(
    socket_path: str = "private-socket", nonce: str = NONCE
) -> dict[str, str]:
    return {
        "AGENT_WORK_MCP_MODE": "worker-read",
        "AGENT_WORK_WORKER_READ_SOCKET": socket_path,
        "AGENT_WORK_WORKER_READ_NONCE": nonce,
    }


def _main(environment: Mapping[str, str], runner: Callable[[MCPServer], None]) -> None:
    cast(_PublicMain, mcp_server.main)(environment=environment, run_server=runner)


@given(socket_path=SOCKETS, nonce=NONCES, unrelated=st.text())
def test_parser_worker_preserves_fields_and_ignores_candidate_namespace(
    socket_path: str, nonce: str, unrelated: str
) -> None:
    environment = _environment(socket_path, nonce)
    environment.update(
        {
            **{
                f"AGENT_WORK_CANDIDATE_{field}": unrelated
                for field in ("SOCKET", "NONCE", "ISSUE_IID", "TREE", "ROLE")
            },
            "OTHER": unrelated,
        }
    )
    parsed = _parse(environment)
    assert parsed is not None
    assert (parsed.socket_path, parsed.nonce) == (socket_path, nonce)


@pytest.mark.parametrize(
    "socket_path", ["s", " ", "../socket", "λ/socket", "/absolute/socket"]
)
@pytest.mark.parametrize("nonce", ["0" * 32, "f" * 32, "0123456789abcdef" * 2])
def test_parser_and_model_finite_valid_fields_preserve_exact_values(
    socket_path: str, nonce: str
) -> None:
    parsed = _parse(_environment(socket_path, nonce))
    assert parsed is not None
    assert (parsed.socket_path, parsed.nonce) == (socket_path, nonce)
    assert _model().model_validate(
        {"socket_path": socket_path, "nonce": nonce}
    ).model_dump() == {"socket_path": socket_path, "nonce": nonce}


@pytest.mark.parametrize("mode", [None, "candidate"])
@given(worker_value=st.text())
def test_parser_candidate_selection_ignores_worker_namespace(
    mode: str | None, worker_value: str
) -> None:
    environment = _environment(worker_value, worker_value)
    if mode is None:
        environment.pop("AGENT_WORK_MCP_MODE")
    else:
        environment["AGENT_WORK_MCP_MODE"] = mode
    assert _parse(environment) is None


@given(mode=UNKNOWN)
def test_parser_unknown_modes_reject_without_private_values(mode: str) -> None:
    with pytest.raises(ValueError) as failure:
        _parse({**_environment(), "AGENT_WORK_MCP_MODE": mode})
    assert "private-socket" not in str(failure.value)
    assert NONCE not in str(failure.value)
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__


@pytest.mark.parametrize("mode", ["", " ", "WORKER-READ", "worker_read", "λ"])
def test_parser_finite_unknown_modes_reject(mode: str) -> None:
    with pytest.raises(ValueError):
        _parse({**_environment(), "AGENT_WORK_MCP_MODE": mode})


@pytest.mark.parametrize(
    ("field", "value"),
    [(f, v) for f, v in BAD_FIELDS if v is None or isinstance(v, str)],
)
def test_parser_independent_malformed_worker_fields_reject(
    field: str, value: str | None
) -> None:
    environment = _environment()
    key = (
        "AGENT_WORK_WORKER_READ_SOCKET"
        if field == "socket_path"
        else "AGENT_WORK_WORKER_READ_NONCE"
    )
    if value is None:
        environment.pop(key)
    else:
        environment[key] = value
    environment.update(
        {"AGENT_WORK_CANDIDATE_SOCKET": "valid", "AGENT_WORK_CANDIDATE_NONCE": NONCE}
    )
    with pytest.raises(ValueError):
        _parse(environment)


@pytest.mark.parametrize("field", ["socket_path", "nonce"])
@given(invalid=st.one_of(st.just(""), st.text().map(lambda text: text + "\x00")))
def test_parser_generated_independent_invalid_fields_reject(
    field: str, invalid: str
) -> None:
    environment = _environment()
    key = (
        "AGENT_WORK_WORKER_READ_SOCKET"
        if field == "socket_path"
        else "AGENT_WORK_WORKER_READ_NONCE"
    )
    environment[key] = invalid
    with pytest.raises(ValueError):
        _parse(environment)


@given(socket_path=SOCKETS, nonce=NONCES)
def test_model_preserves_exact_valid_fields_and_is_frozen(
    socket_path: str, nonce: str
) -> None:
    model = _model().model_validate({"socket_path": socket_path, "nonce": nonce})
    assert model.model_dump() == {"socket_path": socket_path, "nonce": nonce}
    with pytest.raises(ValidationError):
        model.__setattr__("nonce", "b" * 32)


@pytest.mark.parametrize(("field", "value"), BAD_FIELDS)
def test_model_independent_invalid_fields_reject(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate({"socket_path": "s", "nonce": NONCE, field: value})


@pytest.mark.parametrize("field", ["socket_path", "nonce"])
@given(invalid=st.one_of(st.just(""), st.text().map(lambda text: text + "\x00")))
def test_model_generated_independent_invalid_fields_reject(
    field: str, invalid: str
) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate({"socket_path": "s", "nonce": NONCE, field: invalid})


@given(extra=st.text().filter(lambda value: value not in {"socket_path", "nonce"}))
def test_model_generated_extra_fields_reject(extra: str) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate({"socket_path": "s", "nonce": NONCE, extra: "private"})


@pytest.mark.parametrize("role", ROLE_TOOLS)
def test_retained_candidate_role_grants_are_preserved(role: str) -> None:
    assert candidate_tools_for_role(role) == ROLE_TOOLS[role]


@given(role=st.text().filter(lambda value: value not in ROLE_TOOLS))
def test_retained_unsupported_candidate_roles_reject(role: str) -> None:
    with pytest.raises(ValueError, match="invalid candidate role"):
        candidate_tools_for_role(role)


@pytest.mark.parametrize("role", ["", "unknown", "λ"])
def test_retained_finite_unsupported_candidate_roles_reject(role: str) -> None:
    with pytest.raises(ValueError):
        candidate_tools_for_role(role)


class _Ledger:
    """Fake the complete ledger port without accessing durable controller state."""

    def __init__(self, record: LedgerRecord) -> None:
        self.current: LedgerRecord | None = record
        self.state_root = record.worktree.parent
        self.loads: list[int] = []
        self.failure = False

    def load(self, issue_iid: int) -> LedgerRecord | None:
        """Return current identity or a simulated private provider failure."""
        self.loads.append(issue_iid)
        if self.failure:
            raise RuntimeError("private-provider-token")
        return self.current

    def load_read_only(self, issue_iid: int) -> LedgerRecord | None:
        """Expose the same fake read port."""
        return self.load(issue_iid)

    def list_records(self) -> tuple[LedgerRecord, ...]:
        """List the single fake identity."""
        return () if self.current is None else (self.current,)

    def persist(self, record: LedgerRecord) -> LedgerRecord:
        """Refuse unneeded writes at this read-only test port."""
        raise AssertionError("worker read must not persist")


@contextmanager
def _live(
    *, timeout_seconds: float = 10, description: str = "ordinary", title: str = "Work"
) -> Iterator[
    tuple[WorkerReadBrokerSession, LedgerRecord, _Ledger, HeadTrackingRepository]
]:
    with TemporaryDirectory(prefix="wr-") as directory:
        root = Path(directory)
        record = _record(
            root, state=RunnerState.EXECUTING, phase=WorkflowPhase.WORKFLOW
        ).model_copy(update={"revision": 7})
        ledger = _Ledger(record)
        repository = HeadTrackingRepository(root)
        session = serial.create_live_worker_read_session(
            record,
            _issue(description=description).model_copy(update={"title": title}),
            ledger=ledger,
            repository=repository,
            timeout_seconds=timeout_seconds,
            temporary_directory=root,
        )
        try:
            yield session, record, ledger, repository
        finally:
            session.close()


def _run_client(
    environment: Mapping[str, str], operation: Callable[[Client], Awaitable[None]]
) -> None:
    def runner(server: MCPServer) -> None:
        async def run() -> None:
            async with Client(server) as client:
                await operation(client)

        asyncio.run(run())

    _main(environment, runner)


def _worker_environment(session: WorkerReadBrokerSession) -> dict[str, str]:
    return {
        **_environment(str(session.socket_path), session.nonce),
        "AGENT_WORK_CANDIDATE_SOCKET": "\x00invalid",
        "AGENT_WORK_CANDIDATE_NONCE": "bad",
        "AGENT_WORK_CANDIDATE_ISSUE_IID": "bad",
        "AGENT_WORK_CANDIDATE_ROLE": "bad",
        "AGENT_WORK_CANDIDATE_TREE": "bad",
    }


def _assert_error(result: CallToolResult) -> None:
    assert result.is_error
    assert result.structured_content is None
    assert "ordinary" not in str(result)
    assert "private-provider-token" not in str(result)


def test_public_main_worker_lists_closed_tools_and_reads_fresh_projections() -> None:
    with _live() as (session, record, ledger, repository):

        async def operation(client: Client) -> None:
            listed = await client.list_tools()
            assert {tool.name for tool in listed.tools} == WORKER_TOOLS
            for tool in listed.tools:
                assert tool.input_schema["properties"] == {}
                assert tool.input_schema["additionalProperties"] is False
            expected = {
                "worker_context": {
                    "state": "executing",
                    "phase": "workflow",
                    "workflow_lane": "coding",
                },
                "worker_issue": {"title": "Work", "description": "ordinary"},
                "worker_ledger": {
                    "revision": 7,
                    "source_branch": "agent/issue-12",
                    "source_sha": SHA,
                    "state": "executing",
                    "phase": "workflow",
                },
            }
            for index, (tool, projection) in enumerate(expected.items(), 1):
                result = await client.call_tool(tool, {})
                assert not result.is_error
                assert result.structured_content == projection
                assert ledger.loads == [12] * index
                assert repository.head_checks == [record.worktree] * index

        _run_client(_worker_environment(session), operation)


@pytest.mark.parametrize("mode", [None, "candidate"])
@pytest.mark.parametrize("role", ROLE_TOOLS)
def test_public_main_candidate_preserves_all_role_grants(
    mode: str | None, role: str
) -> None:
    environment = {
        "AGENT_WORK_CANDIDATE_SOCKET": "unused-private-socket",
        "AGENT_WORK_CANDIDATE_NONCE": NONCE,
        "AGENT_WORK_CANDIDATE_ISSUE_IID": "12",
        "AGENT_WORK_CANDIDATE_TREE": SHA,
        "AGENT_WORK_CANDIDATE_ROLE": role,
        "AGENT_WORK_WORKER_READ_SOCKET": "\x00invalid",
        "AGENT_WORK_WORKER_READ_NONCE": "bad",
    }
    if mode is not None:
        environment["AGENT_WORK_MCP_MODE"] = mode

    async def operation(client: Client) -> None:
        assert {tool.name for tool in (await client.list_tools()).tools} == set(
            ROLE_TOOLS[role]
        )

    _run_client(environment, operation)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("project_id", 310968),
        ("issue_iid", 13),
        ("worktree", Path("other")),
        ("revision", 8),
        ("state", RunnerState.PUBLISHING),
        ("phase", WorkflowPhase.READY_TO_PUBLISH),
        ("workflow_lane", WorkflowLane.CONTENT),
        ("source_branch", "agent/issue-13"),
        ("source_sha", "c" * 40),
    ],
)
def test_public_main_worker_rejects_each_ledger_identity_drift(
    field: str, replacement: object
) -> None:
    with _live() as (session, record, ledger, repository):
        ledger.current = record.model_copy(update={field: replacement})

        async def operation(client: Client) -> None:
            _assert_error(await client.call_tool("worker_issue", {}))

        _run_client(_worker_environment(session), operation)
        assert ledger.loads == [12]
        assert repository.head_checks == []


@pytest.mark.parametrize("drift", ["missing", "head", "provider"])
def test_public_main_worker_rejects_missing_head_or_provider_failure(
    drift: str,
) -> None:
    with _live() as (session, record, ledger, repository):
        if drift == "missing":
            ledger.current = None
        elif drift == "head":
            repository.current_head = "c" * 40
        else:
            ledger.failure = True

        async def operation(client: Client) -> None:
            _assert_error(await client.call_tool("worker_issue", {}))

        _run_client(_worker_environment(session), operation)
        assert ledger.loads == [12]
        assert repository.head_checks == ([record.worktree] if drift == "head" else [])


@pytest.mark.parametrize("drift", ["revision", "head"])
def test_public_main_worker_success_then_drift_rechecks_each_read(drift: str) -> None:
    with _live() as (session, record, ledger, repository):

        async def operation(client: Client) -> None:
            first = await client.call_tool("worker_context", {})
            assert not first.is_error
            if drift == "revision":
                ledger.current = record.model_copy(update={"revision": 8})
            else:
                repository.current_head = "c" * 40
            _assert_error(await client.call_tool("worker_ledger", {}))

        _run_client(_worker_environment(session), operation)
        assert ledger.loads == [12, 12]
        assert repository.head_checks == [record.worktree] * (
            2 if drift == "head" else 1
        )


@pytest.mark.parametrize("kind", ["wrong-nonce", "cross-session", "closed", "expired"])
def test_public_main_worker_rejects_invalid_or_ended_session_before_provider(
    kind: str,
) -> None:
    with _live(timeout_seconds=0 if kind == "expired" else 10) as (
        session,
        _record_value,
        ledger,
        repository,
    ):
        environment = _worker_environment(session)
        if kind == "wrong-nonce":
            environment["AGENT_WORK_WORKER_READ_NONCE"] = (
                "b" if session.nonce[0] != "b" else "c"
            ) + session.nonce[1:]

        async def operation(client: Client) -> None:
            _assert_error(await client.call_tool("worker_context", {}))

        if kind == "cross-session":
            with _live() as (other, _other_record, other_ledger, other_repository):
                assert other.nonce != session.nonce
                environment["AGENT_WORK_WORKER_READ_NONCE"] = other.nonce
                _run_client(environment, operation)
                assert other_ledger.loads == []
                assert other_repository.head_checks == []
        elif kind != "closed":
            _run_client(environment, operation)
    if kind == "closed":
        _run_client(environment, operation)
    assert ledger.loads == []
    assert repository.head_checks == []


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("candidate_status", {}),
        ("worker_git_stage_candidate", {}),
        ("worker_validation_validate", {}),
        ("worker_submit_result", {}),
        ("arbitrary", {}),
        ("worker_context", {"path": "private"}),
        ("worker_issue", {"nonce": NONCE}),
        ("worker_ledger", {"field": "secret"}),
    ],
)
def test_public_main_worker_rejects_tools_and_arguments_before_identity_reads(
    tool: str, arguments: dict[str, object]
) -> None:
    with _live() as (session, _record_value, ledger, repository):

        async def operation(client: Client) -> None:
            _assert_error(await client.call_tool(tool, arguments))

        _run_client(_worker_environment(session), operation)
        assert ledger.loads == []
        assert repository.head_checks == []


@pytest.mark.parametrize(("size", "accepted"), [(12_288, True), (12_289, False)])
def test_public_main_worker_preserves_issue_description_projection_boundary(
    size: int, *, accepted: bool
) -> None:
    with _live(description="x" * size) as (
        session,
        _record_value,
        _ledger,
        _repository,
    ):

        async def operation(client: Client) -> None:
            result = await client.call_tool("worker_issue", {})
            assert result.is_error is not accepted
            if accepted:
                assert result.structured_content == {
                    "title": "Work",
                    "description": "x" * size,
                }
            else:
                assert result.structured_content is None

        _run_client(_worker_environment(session), operation)


@pytest.mark.parametrize("tool", sorted(WORKER_TOOLS))
@pytest.mark.parametrize(
    "arguments",
    [
        {"role": "admin"},
        {"project_id": 310967},
        {"issue_iid": 12},
        {"checkout": "private"},
        {"phase": "workflow"},
        {"ledger_revision": 7},
        {"source_sha": SHA},
        {"path": "private"},
        {"git_argv": ["show", "HEAD"]},
        {"gitlab_endpoint": "private"},
        {"field": "credential"},
        {"arbitrary": None},
    ],
)
def test_public_main_worker_rejects_each_nonempty_argument_before_provider(
    tool: str, arguments: dict[str, object]
) -> None:
    with _live() as (session, _record_value, ledger, repository):

        async def operation(client: Client) -> None:
            _assert_error(await client.call_tool(tool, arguments))

        _run_client(_worker_environment(session), operation)
        assert ledger.loads == []
        assert repository.head_checks == []


@pytest.mark.parametrize(("size", "accepted"), [(4096, True), (4097, False)])
def test_public_main_worker_preserves_issue_title_projection_boundary(
    size: int, *, accepted: bool
) -> None:
    with _live(title="x" * size) as (session, _record_value, _ledger, _repository):

        async def operation(client: Client) -> None:
            result = await client.call_tool("worker_issue", {})
            assert result.is_error is not accepted
            if accepted:
                assert result.structured_content == {
                    "title": "x" * size,
                    "description": "ordinary",
                }
            else:
                assert result.structured_content is None

        _run_client(_worker_environment(session), operation)


@pytest.mark.parametrize(
    "environment",
    [
        {"AGENT_WORK_MCP_MODE": "private-unknown"},
        {"AGENT_WORK_MCP_MODE": "worker-read"},
        _environment("\x00private-socket"),
        _environment(nonce="private-bad-nonce"),
        {"AGENT_WORK_MCP_MODE": "candidate"},
    ],
)
def test_public_main_invalid_setup_rejects_before_io_and_redacts_private_fields(
    environment: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    invoked: list[MCPServer] = []
    with pytest.raises(RuntimeError) as failure:
        _main(environment, invoked.append)
    assert invoked == []
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__
    captured = capsys.readouterr()
    assert captured.out == ""
    for value in environment.values():
        if value.startswith("private") or "\x00" in value:
            assert value not in str(failure.value)
            assert value not in captured.err
