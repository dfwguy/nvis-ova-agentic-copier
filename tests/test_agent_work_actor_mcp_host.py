# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Independent security envelope and actual invoker use the real actor parser."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pytest_subprocess import FakeProcess

from agent_work import runtime, worker
from agent_work.ledger import project_state_path
from agent_work.ports import CommandRunner
from agent_work.repository import CommandResult
from tests.test_agent_work_actor_frontend import TranscriptProcess, limits

SAFE_FEATURES = {"shell_tool": False, "unified_exec": False, "code_mode_host": False}
SAFE_PRIVACY = {
    "write_denied": True,
    "network_denied": True,
    "auth_unreadable": True,
    "source_auth_unreadable": True,
    "controller_state_unreadable": True,
}
ROLE_TOOLS = {
    "router": ("candidate_status", "candidate_changed_paths", "candidate_read"),
    "judge": (
        "candidate_status",
        "candidate_changed_paths",
        "candidate_read",
        "candidate_submit_review",
    ),
    "finalizer": ("candidate_status", "candidate_changed_paths", "candidate_read"),
    "supervisor-inspect": ("candidate_status",),
}
ENV_NAMES = (
    "AGENT_WORK_CANDIDATE_SOCKET",
    "AGENT_WORK_CANDIDATE_NONCE",
    "AGENT_WORK_CANDIDATE_ISSUE_IID",
    "AGENT_WORK_CANDIDATE_TREE",
    "AGENT_WORK_CANDIDATE_ROLE",
)


class ActorInvokerConstructor(Protocol):
    """Narrow approved future constructor, keeping values and results typed."""

    def __call__(
        self,
        run_command: CommandRunner,
        *,
        environment: Mapping[str, str],
        temporary_directory: Path | None = None,
        controller_state_root: Path | None = None,
        trusted_controller_checkout: Path | None = None,
        clock: Callable[[], float] = ...,
        read_only_host_audit: Callable[
            [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
        ]
        | None = None,
        read_only_sandbox_audit: Callable[
            [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
        ]
        | None = None,
        actor_transport_limits: worker.actor_frontend.ActorTransportLimits
        | None = None,
        actor_frontend_factory: worker.actor_frontend.ActorFrontendFactory
        | None = None,
    ) -> worker.AgentInvoker: ...


def snapshots(
    paths: tuple[Path, ...],
) -> tuple[worker.actor_mcp_host.ProtectedPathSnapshot, ...]:
    """Trusted metadata observation of synthetic files only, never contents."""
    values = []
    for path in paths:
        present = path.exists() or path.is_symlink()
        observed = path.lstat() if present else None
        values.append(
            worker.actor_mcp_host.ProtectedPathSnapshot(
                lexical_path=path,
                canonical_path=path.resolve(),
                device=observed.st_dev if observed else None,
                inode=observed.st_ino if observed else None,
                mode=observed.st_mode if observed else None,
                symlink_target=os.readlink(path) if path.is_symlink() else None,
                present=present,
            )
        )
    return tuple(values)


def security(
    binding: worker.actor_mcp_host.ActorBinding,
    protected: tuple[worker.actor_mcp_host.ProtectedPathSnapshot, ...],
) -> dict[str, object]:
    """Independent expected capabilities, never copied from compiler output."""
    filesystem = {":root": "read"}
    for entry in protected:
        filesystem[str(entry.lexical_path)] = "deny"
        filesystem[str(entry.canonical_path)] = "deny"
    return {
        "approval_policy": "never",
        "permissions": {
            "filesystem": filesystem,
            "network": {"enabled": False},
            "extends": None,
        },
        "features": dict(SAFE_FEATURES),
        "mcp_servers": {
            "candidate": {
                "command": binding.candidate_command[0],
                "args": list(binding.candidate_command[1:]),
                "required": True,
                "env_vars": list(ENV_NAMES),
                "enabled_tools": list(ROLE_TOOLS[binding.role]),
                "tools": {
                    tool: {"approval_mode": "approve"}
                    for tool in ROLE_TOOLS[binding.role]
                },
            }
        },
    }


class _ActorFixture:
    """Shared fixture with real public host and injected raw descriptor ports."""

    def __init__(
        self, root: Path, *, role: str = "judge", phase: str = "", fault: str = ""
    ) -> None:
        self.root = root
        self.process = TranscriptProcess(root / "process", phase=phase, fault=fault)
        for name in ("checkout", "scoped", "source", "state", "trusted"):
            (root / name).mkdir(parents=True, exist_ok=True)
        for path in (
            root / "scoped/auth.json",
            root / "source/auth.json",
            root / "state/project.lock",
        ):
            path.write_text("synthetic private marker")
            path.chmod(0o600)
        self.binding = worker.actor_mcp_host.ActorBinding(
            role=role,
            issue_iid=355,
            candidate_tree="b" * 40,
            checkout=root / "checkout",
            scoped_home=root / "scoped",
            trusted_checkout=root / "trusted",
            source_home=root / "source",
            controller_state_root=root / "state",
            executable="/fixture/codex",
            candidate_socket=root / "candidate.sock",
            nonce="private-actor-nonce",
            candidate_command=(
                "/fixture/uv",
                "run",
                "--project",
                str(root / "trusted"),
                "--frozen",
                "python",
                "-I",
                "-m",
                "agent_work.mcp_server",
            ),
        )
        self.protected = snapshots(
            (
                self.binding.scoped_home / "auth.json",
                self.binding.source_home / "auth.json",
                self.binding.controller_state_root,
            )
        )
        self.plan = worker.actor_mcp_host.build_actor_permission_plan(
            self.binding, self.protected
        )
        self.process.security = security(self.binding, self.protected)
        self.commands: list[tuple[str, ...]] = []
        self.drift = False

    def run_command(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        self.commands.append(command)
        prefix = command[: command.index("--")] if "--" in command else command
        assert tuple(
            token
            for index, token in enumerate(prefix)
            if index and prefix[index - 1] == "-c"
        ) == tuple(
            self.plan.configuration_tokens[index + 1]
            for index, token in enumerate(self.plan.configuration_tokens[:-1])
            if token == "-c"
        )
        if command[-2:] == ("features", "list"):
            return CommandResult(
                0,
                "\n".join(f"{key} stable false" for key in SAFE_FEATURES),
                "PRIVATE_AUDIT",
            )
        assert command[1] == "sandbox"
        assert (
            command[command.index("--permission-profile") + 1] == self.plan.profile_id
        )
        return CommandResult(0, json.dumps(SAFE_PRIVACY), "PRIVATE_AUDIT")

    def snapshot(
        self, paths: tuple[Path, ...]
    ) -> tuple[worker.actor_mcp_host.ProtectedPathSnapshot, ...]:
        result = snapshots(paths)
        if self.drift:
            return (replace(result[0], inode=(result[0].inode or 0) + 1), *result[1:])
        return result

    def run(self) -> worker.actor_mcp_host.ActorResult:
        spec = worker.actor_frontend.ActorProcessSpec(
            argv=(
                self.binding.executable,
                "app-server",
                "--stdio",
                "--strict-config",
                *self.plan.configuration_tokens,
            ),
            environment=(
                ("CODEX_HOME", str(self.binding.scoped_home)),
                ("HOME", str(self.binding.scoped_home)),
                ("AGENT_WORK_CANDIDATE_ROLE", self.binding.role),
                ("AGENT_WORK_CANDIDATE_SOCKET", str(self.binding.candidate_socket)),
                ("AGENT_WORK_CANDIDATE_NONCE", self.binding.nonce),
                ("AGENT_WORK_CANDIDATE_ISSUE_IID", str(self.binding.issue_iid)),
                ("AGENT_WORK_CANDIDATE_TREE", self.binding.candidate_tree),
            ),
            cwd=self.binding.checkout,
            deadline=10.0,
        )
        ports = worker.actor_mcp_host.ActorHostPorts(
            snapshot_protected=self.snapshot,
            run_command=self.run_command,
            clock=self.process.clock,
            frontend_factory=worker.actor_frontend.create_stdio_actor_frontend(
                self.process.ports()
            ),
        )
        return worker.actor_mcp_host.run_actor_session(
            self.plan,
            "Inspect bound candidate",
            spec=spec,
            limits=limits(),
            ports=ports,
        )

    def dispose(self) -> None:
        self.process.dispose()


@pytest.mark.parametrize("role", tuple(ROLE_TOOLS))
def test_actual_host_parser_requires_two_security_reads_before_single_turn(
    tmp_path: Path, role: str
) -> None:
    fixture = _ActorFixture(tmp_path, role=role)
    try:
        result = fixture.run()
        assert (result.returncode, result.stdout, result.stderr) == (
            0,
            "review complete",
            "",
        )
        methods = [message["method"] for message in fixture.process.requests]
        assert methods == [
            "initialize",
            "initialized",
            "config/read",
            "thread/start",
            "config/read",
            "turn/start",
        ]
        thread = next(
            message
            for message in fixture.process.requests
            if message["method"] == "thread/start"
        )
        turn = next(
            message
            for message in fixture.process.requests
            if message["method"] == "turn/start"
        )
        assert "permissions" in cast(dict[str, object], thread["params"])
        assert "permissions" in cast(dict[str, object], turn["params"])
        assert "sandboxPolicy" not in cast(dict[str, object], turn["params"])
        assert fixture.process.waits > 0
    finally:
        fixture.dispose()


@pytest.mark.parametrize("phase", ["config/read", "recheck"])
@pytest.mark.parametrize(
    "fault",
    [
        "feature",
        "foreign_feature",
        "missing",
        "legacy",
        "extra_grant",
        "tools",
        "server",
        "network",
        "plugin",
    ],
)
def test_independent_security_mutation_never_sends_prompt(
    tmp_path: Path, phase: str, fault: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", phase=phase, fault=f"config:{fault}")
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (
            None,
            "",
            "actor_config_unverified",
        )
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    "fault",
    [
        "profile",
        "extends",
        "cwd",
        "roots",
        "approval",
        "null_profile",
        "missing_profile",
    ],
)
def test_active_profile_or_thread_mismatch_never_sends_prompt(
    tmp_path: Path, fault: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", fault=f"thread:{fault}")
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (
            None,
            "",
            "actor_thread_unverified",
        )
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    "fault", ["foreign_thread", "foreign_turn", "failed", "interrupted", "cleanup"]
)
def test_correlated_completion_and_owned_cleanup_are_required(
    tmp_path: Path, fault: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", fault=fault)
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert result.returncode != 0 and result.stdout == ""
        assert result.stderr == (
            "actor_cleanup_failed" if fault == "cleanup" else "actor_turn_failed"
        )
        assert "PRIVATE" not in result.stderr
        assert subject.process.waits > 0
    finally:
        control.dispose()
        subject.dispose()


@settings(max_examples=6)
@given(suffix=st.text(alphabet='abcXYZ "é', min_size=1, max_size=12))
def test_generated_profile_paths_escaping_and_binding_fingerprint(
    tmp_path_factory: pytest.TempPathFactory, suffix: str
) -> None:
    root = tmp_path_factory.mktemp("plan")
    fixture = _ActorFixture(root)
    try:
        path = root / ("protected-" + suffix)
        path.write_text("synthetic")
        protected = (*fixture.protected, *snapshots((path,)))
        plan = worker.actor_mcp_host.build_actor_permission_plan(
            fixture.binding, protected
        )
        repeated = worker.actor_mcp_host.build_actor_permission_plan(
            fixture.binding, protected
        )
        assert plan == repeated
        config = tomllib.loads(
            "\n".join(
                plan.configuration_tokens[index + 1]
                for index, token in enumerate(plan.configuration_tokens[:-1])
                if token == "-c"
            )
        )
        assert (
            config["permissions"][plan.profile_id]["filesystem"]
            == cast(
                dict[str, object], security(fixture.binding, protected)["permissions"]
            )["filesystem"]
        )
        assert config["permissions"][plan.profile_id]["network"] == {"enabled": False}
        for binding in (
            replace(fixture.binding, issue_iid=356),
            replace(fixture.binding, candidate_tree="c" * 40),
            replace(fixture.binding, role="router"),
        ):
            changed = worker.actor_mcp_host.build_actor_permission_plan(
                binding, protected
            )
            assert changed.fingerprint != plan.fingerprint
        assert fixture.binding.nonce not in str(plan.configuration_tokens)
    finally:
        fixture.dispose()


@pytest.mark.parametrize("phase", ["config/read", "recheck"])
@settings(max_examples=6)
@given(fault=st.sampled_from(("feature", "network", "tools", "extra_grant", "plugin")))
def test_generated_effective_security_can_only_reduce_authority(
    tmp_path_factory: pytest.TempPathFactory, phase: str, fault: str
) -> None:
    root = tmp_path_factory.mktemp("projection")
    control = _ActorFixture(root / "control")
    subject = _ActorFixture(root / "subject", phase=phase, fault=f"config:{fault}")
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert result.stderr == "actor_config_unverified" and result.returncode is None
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


class InvokerTranscript(TranscriptProcess):
    """Real adapter fixture knows host paths independently of profile compiler."""

    def __init__(
        self,
        root: Path,
        source_home: Path,
        controller_root: Path,
        uv: Path,
        *,
        phase: str = "",
        fault: str = "",
    ) -> None:
        super().__init__(root, phase=phase, fault=fault)
        self.source_home = source_home
        self.controller_root = controller_root
        self.uv = uv
        self.events: list[tuple[str, str]] = []

    def open_process(
        self, spec: worker.actor_frontend.ActorProcessSpec
    ) -> worker.actor_frontend.ActorRawProcess:
        result = super().open_process(spec)
        selected_id = self.config["default_permissions"]
        environment = dict(spec.environment)
        self.events.append(("launch", environment["CODEX_HOME"]))
        trusted = Path(worker.__file__).resolve().parents[2]
        binding = worker.actor_mcp_host.ActorBinding(
            role=environment["AGENT_WORK_CANDIDATE_ROLE"],
            issue_iid=int(environment["AGENT_WORK_CANDIDATE_ISSUE_IID"]),
            candidate_tree=environment["AGENT_WORK_CANDIDATE_TREE"],
            checkout=spec.cwd,
            scoped_home=Path(environment["CODEX_HOME"]),
            trusted_checkout=trusted,
            source_home=self.source_home,
            controller_state_root=self.controller_root,
            executable=spec.argv[0],
            candidate_socket=Path(environment["AGENT_WORK_CANDIDATE_SOCKET"]),
            nonce=environment["AGENT_WORK_CANDIDATE_NONCE"],
            candidate_command=(
                str(self.uv),
                "run",
                "--project",
                str(trusted),
                "--frozen",
                "python",
                "-I",
                "-m",
                "agent_work.mcp_server",
            ),
        )
        protected = snapshots(
            (
                binding.scoped_home / "auth.json",
                binding.source_home / "auth.json",
                binding.controller_state_root,
            )
        )
        expected = security(binding, protected)
        permission = expected["permissions"]
        expected["default_permissions"] = selected_id
        expected["permissions"] = {str(selected_id): permission}
        self.config = expected
        return result


def actor_invoker(
    run_command: CommandRunner,
    *,
    environment: Mapping[str, str],
    temporary_directory: Path,
    controller_state_root: Path | None = None,
    read_only_host_audit: Callable[
        [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
    ]
    | None = None,
    read_only_sandbox_audit: Callable[
        [tuple[str, ...], Mapping[str, str]], Mapping[str, bool]
    ]
    | None = None,
    processes: list[InvokerTranscript] | None = None,
    events: list[tuple[str, str]] | None = None,
    phase: str = "",
    fault: str = "",
) -> worker.AgentInvoker:
    """Shared actual invoker; only raw process/descriptor/time boundaries faked."""
    uv = temporary_directory / "fixture-uv"
    uv.write_text("synthetic nonexecuted command")
    uv.chmod(0o700)
    source_home = Path(
        environment.get("CODEX_HOME", str(temporary_directory / "source-home"))
    )
    source_home.mkdir(parents=True, exist_ok=True)
    host_environment = {
        **environment,
        "CODEX_HOME": str(source_home),
        "AGENT_WORK_UV": str(uv),
    }
    process = InvokerTranscript(
        temporary_directory / "actor-process",
        source_home,
        controller_state_root or temporary_directory / "missing-state",
        uv,
        phase=phase,
        fault=fault,
    )
    if processes is not None:
        processes.append(process)
    if events is not None:
        process.events = events
    return cast(ActorInvokerConstructor, worker.AgentInvoker)(
        run_command,
        environment=host_environment,
        temporary_directory=temporary_directory,
        controller_state_root=controller_state_root,
        read_only_host_audit=read_only_host_audit,
        read_only_sandbox_audit=read_only_sandbox_audit,
        actor_transport_limits=limits(),
        actor_frontend_factory=worker.actor_frontend.create_stdio_actor_frontend(
            process.ports()
        ),
        clock=process.clock,
    )


@pytest.mark.parametrize("phase", ["config/read", "recheck"])
def test_actual_invoker_preserves_safe_actor_refusal_and_never_sends_prompt(
    tmp_path: Path, phase: str
) -> None:
    observed: list[InvokerTranscript] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        pytest.fail("No legacy exec fallback permitted")

    invoker = actor_invoker(
        run_command,
        environment={"AGENT_WORK_AGENT": "codex", "AGENT_WORK_AGENT_PATH": "/codex"},
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        read_only_sandbox_audit=lambda _command, _environment: SAFE_PRIVACY,
        processes=observed,
        phase=phase,
        fault="config:network",
    )
    profile = worker.RoleExecutionProfile.read_only(
        role="judge",
        candidate_socket=tmp_path / "candidate.sock",
        nonce="private-role-nonce",
        issue_iid=355,
        candidate_tree="b" * 40,
    )
    result = invoker.invoke(tmp_path, "Inspect candidate", 10, role_profile=profile)
    assert result.returncode is None and result.stdout == ""
    assert result.stderr == "actor_config_unverified"
    assert all(message["method"] != "turn/start" for message in observed[0].requests)
    assert "PRIVATE" not in result.stderr


def test_missing_trusted_limits_closes_actual_optional_role_before_any_process(
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "synthetic legacy success", "")

    source = tmp_path / "source"
    source.mkdir()
    invoker = worker.AgentInvoker(
        run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/codex",
            "CODEX_HOME": str(source),
        },
        temporary_directory=tmp_path,
        controller_state_root=tmp_path,
        read_only_host_audit=lambda _command, _environment: SAFE_FEATURES,
        read_only_sandbox_audit=lambda _command, _environment: SAFE_PRIVACY,
    )
    result = invoker.invoke(
        tmp_path,
        "Inspect",
        10,
        role_profile=worker.RoleExecutionProfile.read_only(
            role="judge",
            candidate_socket=tmp_path / "candidate.sock",
            nonce="nonce",
            issue_iid=355,
            candidate_tree="b" * 40,
        ),
    )
    assert result.returncode is None and result.stderr == "actor_limits_unavailable"
    assert commands == []


@pytest.mark.parametrize(
    ("phase", "fault", "reason"),
    [
        ("initialize", "spawn", "actor_frontend_unavailable"),
        ("initialize", "read", "actor_frontend_unavailable"),
        ("initialize", "write", "actor_frontend_unavailable"),
        ("initialize", "zero_write", "actor_frontend_unavailable"),
        ("initialize", "frame", "actor_protocol_invalid"),
        ("initialize", "partial", "actor_frontend_unavailable"),
        ("initialize", "timeout", "actor_timeout"),
        *[
            (phase, "eof", "actor_frontend_unavailable")
            for phase in ("initialize", "config/read", "thread/start", "recheck")
        ],
    ],
)
def test_actual_transport_failure_has_closed_reason_and_no_turn(
    tmp_path: Path, phase: str, fault: str, reason: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", phase=phase, fault=fault)
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (None, "", reason)
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize("retry", ["retry_read", "retry_write"])
def test_nonblocking_readiness_retry_does_not_replay_request_or_extend_deadline(
    tmp_path: Path, retry: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", fault=retry)
    try:
        assert control.run().returncode == 0
        result = subject.run()
        assert result.returncode == 0
        requests = [message for message in subject.process.requests if "id" in message]
        assert len({message["id"] for message in requests}) == len(requests)
        assert sum(message["method"] == "turn/start" for message in requests) == 1
        assert len(subject.process.blocking) == 3
        assert subject.process.specs[0].deadline == 10.0
    finally:
        control.dispose()
        subject.dispose()


def test_metadata_recheck_refuses_before_prompt(tmp_path: Path) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject")
    try:
        assert control.run().returncode == 0
        subject.drift = True
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (
            None,
            "",
            "actor_identity_changed",
        )
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


@settings(max_examples=6)
@given(count=st.integers(min_value=1, max_value=16))
def test_generated_output_bound_is_utf8_bytes_and_never_private_diagnostics(
    tmp_path_factory: pytest.TempPathFactory, count: int
) -> None:
    root = tmp_path_factory.mktemp("output")
    control = _ActorFixture(root / "control")
    subject = _ActorFixture(root / "subject")
    try:
        control.process.delta = "é" * count
        result = control.run()
        assert result.returncode == 0 and result.stdout == "é" * count
        assert len(result.stdout.encode()) <= limits().max_output_bytes
        subject.process.delta = "é" * (limits().max_output_bytes // 2 + 1)
        result = subject.run()
        assert result.returncode is None and result.stdout == ""
        assert result.stderr == "actor_protocol_invalid"
        assert "PRIVATE" not in result.stderr
    finally:
        control.dispose()
        subject.dispose()


class _RuntimeBuilder(Protocol):
    def __call__(
        self,
        *,
        debug: bool,
        actor_transport_limits: worker.actor_frontend.ActorTransportLimits
        | None = None,
        actor_frontend_factory: worker.actor_frontend.ActorFrontendFactory
        | None = None,
    ) -> runtime.RuntimeDependencies: ...


def test_actual_default_runtime_composition_uses_actor_frontend_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fp: FakeProcess
) -> None:
    monkeypatch.chdir(tmp_path)
    values = {
        "AGENT_WORK_BASE_BRANCH": "agent/work-base",
        "AGENT_WORK_FACTORY_USERNAME": "fixture",
        "AGENT_WORK_GIT": "/fixture/git",
        "AGENT_WORK_GLAB": "/fixture/glab",
        "GITLAB_HOST": "gitlab.example.com",
        "GITLAB_PROJECT_ID": "310967",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "source"),
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": "/codex",
        "AGENT_WORK_UV": str(tmp_path / "uv"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "uv").write_text("nonexecuted synthetic command")
    (tmp_path / "uv").chmod(0o700)
    (tmp_path / "source").mkdir()
    fp.register(
        ["/fixture/git", "-C", str(tmp_path), "rev-parse", "--show-toplevel"],
        stdout=str(tmp_path),
    )
    fp.register(
        ["/fixture/git", "-C", str(tmp_path), "remote", "get-url", "origin"],
        stdout="https://gitlab.example.com/group/project.git",
    )
    fp.register(
        ["/fixture/glab", "api", "--hostname", "gitlab.example.com", "projects/310967"],
        stdout=json.dumps(
            {
                "id": 310967,
                "path_with_namespace": "group/project",
                "http_url_to_repo": "https://gitlab.example.com/group/project.git",
                "default_branch": "main",
            }
        ),
    )
    fp.register(
        ["/codex", fp.any(), "features", "list"],
        stdout="\n".join(f"{key} stable false" for key in SAFE_FEATURES),
    )
    fp.register(["/codex", "sandbox", fp.any()], stdout=json.dumps(SAFE_PRIVACY))
    state = project_state_path(
        host="gitlab.example.com", project_id=310967, environment=values
    )
    process = InvokerTranscript(
        tmp_path / "process", tmp_path / "source", state, tmp_path / "uv"
    )
    try:
        dependencies = cast(_RuntimeBuilder, runtime.build_runtime_dependencies)(
            debug=False,
            actor_transport_limits=limits(),
            actor_frontend_factory=worker.actor_frontend.create_stdio_actor_frontend(
                process.ports()
            ),
        )
        assert isinstance(dependencies.invoke, worker.AgentInvoker)
        profile = worker.RoleExecutionProfile.read_only(
            role="judge",
            candidate_socket=tmp_path / "candidate.sock",
            nonce="private-nonce",
            issue_iid=355,
            candidate_tree="b" * 40,
        )
        result = dependencies.invoke.invoke(
            tmp_path, "Inspect candidate", 10, role_profile=profile
        )
        assert result.returncode == 0 and result.stdout == "review complete"
        assert (
            sum(message["method"] == "turn/start" for message in process.requests) == 1
        )
        assert process.specs[0].argv[:4] == (
            "/codex",
            "app-server",
            "--stdio",
            "--strict-config",
        )
    finally:
        process.dispose()


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("total", "actor_protocol_invalid"),
        ("duplicate_response", "actor_protocol_invalid"),
        *[
            ("missing:" + pipe, "actor_frontend_unavailable")
            for pipe in ("stdin", "stdout", "stderr")
        ],
        ("close", "actor_cleanup_failed"),
        ("kill", "actor_cleanup_failed"),
        ("reap", "actor_cleanup_failed"),
    ],
)
def test_remaining_stream_and_owned_cleanup_boundaries_refuse_independently(
    tmp_path: Path, fault: str, reason: str
) -> None:
    control = _ActorFixture(tmp_path / "control")
    subject = _ActorFixture(tmp_path / "subject", phase="initialize", fault=fault)
    try:
        if fault == "kill":
            control.process.escalate = True
            subject.process.escalate = True
        assert control.run().returncode == 0
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (None, "", reason)
        if reason != "actor_cleanup_failed":
            assert all(
                message["method"] != "turn/start"
                for message in subject.process.requests
            )
        else:
            assert subject.process.waits > 0
        if fault == "kill":
            assert subject.process.kills > 0
        assert "PRIVATE" not in result.stderr
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    "fault",
    [
        "frame_bool",
        "frame_zero",
        "frame_over_total",
        "output_over_total",
        "cleanup_bool",
        "cleanup_zero",
        "cleanup_nan",
        "cleanup_infinite",
    ],
)
def test_invalid_explicit_host_limits_never_become_authority(fault: str) -> None:
    valid = limits()
    assert valid.max_frame_bytes > 0 and valid.max_frame_bytes <= valid.max_total_bytes
    with pytest.raises(worker.actor_frontend.ActorHostError) as caught:
        if fault == "frame_bool":
            replace(valid, max_frame_bytes=True)
        elif fault == "frame_zero":
            replace(valid, max_frame_bytes=0)
        elif fault == "frame_over_total":
            replace(valid, max_frame_bytes=valid.max_total_bytes + 1)
        elif fault == "output_over_total":
            replace(valid, max_output_bytes=valid.max_total_bytes + 1)
        elif fault == "cleanup_bool":
            replace(valid, cleanup_timeout_seconds=True)
        elif fault == "cleanup_zero":
            replace(valid, cleanup_timeout_seconds=0.0)
        elif fault == "cleanup_nan":
            replace(valid, cleanup_timeout_seconds=float("nan"))
        else:
            replace(valid, cleanup_timeout_seconds=float("inf"))
    assert str(caught.value) == "actor_limits_invalid"


def _permission_fixture() -> tuple[
    worker.actor_mcp_host.ActorBinding,
    tuple[worker.actor_mcp_host.ProtectedPathSnapshot, ...],
]:
    root = Path("/synthetic.issue418")
    binding = worker.actor_mcp_host.ActorBinding(
        role="judge",
        issue_iid=418,
        candidate_tree="b" * 40,
        checkout=root / "checkout",
        scoped_home=root / "scoped",
        trusted_checkout=root / "trusted",
        source_home=root / "source",
        controller_state_root=root / "state",
        executable="/fixture/codex",
        candidate_socket=root / "candidate.sock",
        nonce="synthetic-nonce",
        candidate_command=(
            "/fixture/uv",
            "run",
            "--project",
            str(root / "trusted"),
            "--frozen",
            "python",
            "-I",
            "-m",
            "agent_work.mcp_server",
        ),
    )
    protected = tuple(
        worker.actor_mcp_host.ProtectedPathSnapshot(
            lexical_path=path,
            canonical_path=path,
            device=None,
            inode=None,
            mode=None,
            symlink_target=None,
            present=False,
        )
        for path in (
            binding.scoped_home / "auth.json",
            binding.source_home / "auth.json",
            binding.controller_state_root,
        )
    )
    return binding, protected


def _assert_filesystem_table(plan: worker.actor_mcp_host.ActorPermissionPlan) -> None:
    assignments = plan.configuration_tokens[1::2]
    key = f"permissions.{plan.profile_id}.filesystem"
    filesystem_assignments = [value for value in assignments if value.startswith(key)]
    assert len(filesystem_assignments) == 1
    assignment_key, value = filesystem_assignments[0].split("=", 1)
    assert assignment_key == key
    assert value.startswith("{") and value.endswith("}")
    expected = cast(
        dict[str, object], security(plan.binding, plan.protected_paths)["permissions"]
    )
    assert tomllib.loads("filesystem=" + value)["filesystem"] == expected["filesystem"]
    config = tomllib.loads("\n".join(assignments))
    permission = config["permissions"][plan.profile_id]
    assert permission == {key: expected[key] for key in ("filesystem", "network")}


@pytest.mark.parametrize(
    "suffix",
    [
        "dotted.path",
        "with space",
        'quote"path',
        "back\\slash",
        "BMP-é",
        "nonBMP-😀",
        "DEL-\x7f",
    ],
)
def test_permission_cli_filesystem_table_round_trips_protected_aliases(
    suffix: str,
) -> None:
    binding, protected = _permission_fixture()
    alias = worker.actor_mcp_host.ProtectedPathSnapshot(
        lexical_path=Path("/synthetic.issue418") / suffix,
        canonical_path=Path("/canonical.issue418") / suffix,
        device=7,
        inode=11,
        mode=0o120600,
        symlink_target=str(Path("/canonical.issue418") / suffix),
        present=True,
    )
    plan = worker.actor_mcp_host.build_actor_permission_plan(
        binding, (*protected, alias)
    )
    _assert_filesystem_table(plan)


@settings(max_examples=12)
@given(suffix=st.text(alphabet='abcXYZ. "\\é中😀\x7f', min_size=1, max_size=16))
def test_permission_cli_generated_valid_paths_keep_exact_filesystem_map(
    suffix: str,
) -> None:
    binding, protected = _permission_fixture()
    absent = replace(
        protected[0],
        lexical_path=Path("/generated") / ("path-" + suffix),
        canonical_path=Path("/resolved") / ("path-" + suffix),
    )
    plan = worker.actor_mcp_host.build_actor_permission_plan(
        binding, (*protected, absent)
    )
    _assert_filesystem_table(plan)


@pytest.mark.parametrize(
    "character", ["\x00", "\t", "\n", "\x1f", chr(0xD800), chr(0xDFFF)]
)
@pytest.mark.parametrize("path_field", ["lexical_path", "canonical_path"])
def test_permission_cli_invalid_paths_refuse_without_broadening_valid_plan(
    character: str,
    path_field: str,
) -> None:
    binding, protected = _permission_fixture()
    extra = replace(
        protected[0],
        lexical_path=Path("/invalid-lexical/extra"),
        canonical_path=Path("/invalid-canonical/extra"),
    )
    invalid = replace(extra, **{path_field: Path("/invalid") / ("path-" + character)})
    with pytest.raises(
        worker.actor_frontend.ActorHostError, match="^actor_plan_invalid$"
    ):
        worker.actor_mcp_host.build_actor_permission_plan(
            binding, (*protected, invalid)
        )
    _assert_filesystem_table(
        worker.actor_mcp_host.build_actor_permission_plan(binding, protected)
    )


def test_permission_cli_serialization_preserves_fixed_fingerprint_and_detects_drift() -> (
    None
):
    binding, protected = _permission_fixture()
    plan = worker.actor_mcp_host.build_actor_permission_plan(binding, protected)
    assert (
        plan.fingerprint
        == "6831b65045908225be47025247f10f799f9f89c2c6fbd0023ab4403aa76e3c73"
    )
    assert plan.profile_id == "agent-work-" + plan.fingerprint
    for changed_binding in (
        replace(binding, role="router"),
        replace(binding, issue_iid=419),
        replace(binding, candidate_tree="c" * 40),
    ):
        assert (
            worker.actor_mcp_host.build_actor_permission_plan(
                changed_binding, protected
            ).fingerprint
            != plan.fingerprint
        )
    changed_metadata = replace(
        protected[0], present=True, device=7, inode=11, mode=0o100600
    )
    assert (
        worker.actor_mcp_host.build_actor_permission_plan(
            binding, (changed_metadata, *protected[1:])
        ).fingerprint
        != plan.fingerprint
    )
    _assert_filesystem_table(plan)


class _ProvenanceTranscript(TranscriptProcess):
    """Inject provenance faults only at the raw external response producer."""

    def __init__(self, root: Path, *, phase: str = "", fault: str = "") -> None:
        super().__init__(root, phase=phase, fault=fault)
        self.user_config_path = root / "user-config.toml"
        self.config_payloads: list[dict[str, object]] = []

    def emit(self, value: dict[str, object]) -> None:
        result = value.get("result")
        if (
            isinstance(result, dict)
            and "origins" in result
            and self.fault.startswith("provenance:")
        ):
            phase = "config/read" if self.config_reads == 1 else "recheck"
            if self.phase == phase:
                result = cast(dict[str, object], result)
                if self.fault == "provenance:origin":
                    origins = cast(dict[str, dict[str, object]], result["origins"])
                    origins["permissions"] = {
                        **origins["permissions"],
                        "name": {
                            "type": "user",
                            "file": str(self.user_config_path),
                            "profile": None,
                        },
                    }
                else:
                    layers = cast(list[dict[str, object]], result["layers"])
                    layers[0]["disabledReason"] = "PRIVATE_LAYER"
        if isinstance(result, dict) and "origins" in result:
            self.config_payloads.append(cast(dict[str, object], result))
        super().emit(value)


class _PermissionAuditFixture(_ActorFixture):
    """Exercise real host gates with injected command and descriptor producers."""

    def __init__(self, root: Path, *, fault: str = "", phase: str = "") -> None:
        super().__init__(root)
        self.process.dispose()
        self.process = _ProvenanceTranscript(root / "process", phase=phase, fault=fault)
        self.process.security = security(self.binding, self.protected)
        self.audit_fault = fault

    def run_command(self, command: tuple[str, ...], **kwargs: object) -> CommandResult:
        result = super().run_command(command, **kwargs)
        if command[-2:] == ("features", "list"):
            if self.audit_fault == "feature_exit":
                return CommandResult(1, result.stdout, "PRIVATE_FEATURE")
            if self.audit_fault.startswith("feature:"):
                feature = self.audit_fault.removeprefix("feature:")
                return CommandResult(
                    0,
                    result.stdout.replace(
                        feature + " stable false", feature + " stable true"
                    ),
                    "PRIVATE_FEATURE",
                )
        elif self.audit_fault == "sandbox_exit":
            return CommandResult(1, result.stdout, "PRIVATE_SANDBOX")
        elif self.audit_fault.startswith("privacy:"):
            flag = self.audit_fault.removeprefix("privacy:")
            return CommandResult(
                0, json.dumps({**SAFE_PRIVACY, flag: False}), "PRIVATE_SANDBOX"
            )
        return result


@pytest.mark.parametrize("phase", ["config/read", "recheck"])
def test_schema_valid_user_origin_reaches_semantic_guard_on_both_reads(
    tmp_path: Path, phase: str
) -> None:
    control = _PermissionAuditFixture(tmp_path / "control")
    subject = _PermissionAuditFixture(
        tmp_path / "subject", fault="provenance:origin", phase=phase
    )
    try:
        assert isinstance(control.process, _ProvenanceTranscript)
        assert isinstance(subject.process, _ProvenanceTranscript)
        control_result = control.run()
        assert (
            control_result.returncode,
            control_result.stdout,
            control_result.stderr,
        ) == (
            0,
            "review complete",
            "",
        )
        assert control.process.config_reads == 2
        for payload in control.process.config_payloads:
            worker.actor_mcp_host.ActorConfigReadEvidence.model_validate_json(
                json.dumps(payload)
            )
            worker.actor_mcp_host.validate_actor_provenance(control.plan, payload, {})
        result = subject.run()
        assert (result.returncode, result.stdout, result.stderr) == (
            None,
            "",
            "actor_config_unverified",
        )
        assert subject.process.config_reads == (1 if phase == "config/read" else 2)
        payload = subject.process.config_payloads[-1]
        evidence = worker.actor_mcp_host.ActorConfigReadEvidence.model_validate_json(
            json.dumps(payload)
        )
        assert evidence.origins["permissions"].name.type == "user"
        assert all(
            origin.name.type == "sessionFlags"
            for key, origin in evidence.origins.items()
            if key != "permissions"
        )
        assert evidence.layers[0].name.type == "sessionFlags"
        assert evidence.layers[0].disabledReason is None
        assert evidence.layers[0].config == evidence.config.model_dump(mode="json")
        with pytest.raises(
            worker.actor_frontend.ActorHostError, match="^actor_config_unverified$"
        ):
            worker.actor_mcp_host.validate_actor_provenance(subject.plan, payload, {})
        for fixture in (control, subject):
            assert len(fixture.commands) == 2
            assert (
                fixture.process.specs[0].argv[4:] == fixture.plan.configuration_tokens
            )
            assert fixture.process.waits > 0
            assert all(
                stream is not None and stream.closed
                for stream in (
                    fixture.process.stdin,
                    fixture.process.stdout,
                    fixture.process.stderr,
                )
            )
        assert [message["method"] for message in control.process.requests] == [
            "initialize",
            "initialized",
            "config/read",
            "thread/start",
            "config/read",
            "turn/start",
        ]
        assert all(
            message["method"] != "turn/start" for message in subject.process.requests
        )
    finally:
        control.dispose()
        subject.dispose()


@pytest.mark.parametrize(
    ("fault", "phase", "reason"),
    [
        ("", "", ""),
        ("feature_exit", "", "actor_features_unavailable"),
        *[
            (f"feature:{feature}", "", "actor_features_unavailable")
            for feature in SAFE_FEATURES
        ],
        ("sandbox_exit", "", "actor_privacy_unavailable"),
        *[
            (f"privacy:{flag}", "", "actor_privacy_unavailable")
            for flag in SAFE_PRIVACY
        ],
        *[
            (f"provenance:{fault}", phase, "actor_config_unverified")
            for fault in ("origin", "layer")
            for phase in ("config/read", "recheck")
        ],
    ],
)
def test_permission_cli_public_session_preserves_audits_provenance_and_cleanup(
    tmp_path: Path,
    fault: str,
    phase: str,
    reason: str,
) -> None:
    control = _PermissionAuditFixture(tmp_path / "control")
    subject = _PermissionAuditFixture(tmp_path / "subject", fault=fault, phase=phase)
    try:
        control_result = control.run()
        assert (
            control_result.returncode,
            control_result.stdout,
            control_result.stderr,
        ) == (0, "review complete", "")
        methods = [message["method"] for message in control.process.requests]
        assert methods == [
            "initialize",
            "initialized",
            "config/read",
            "thread/start",
            "config/read",
            "turn/start",
        ]
        assert len(control.commands) == 2
        assert control.process.specs[0].argv[4:] == control.plan.configuration_tokens
        assert control.process.waits > 0
        assert all(
            stream is not None and stream.closed
            for stream in (
                control.process.stdin,
                control.process.stdout,
                control.process.stderr,
            )
        )
        result = subject.run()
        if reason:
            assert (result.returncode, result.stdout, result.stderr) == (
                None,
                "",
                reason,
            )
            assert all(
                message["method"] != "turn/start"
                for message in subject.process.requests
            )
            if reason in ("actor_features_unavailable", "actor_privacy_unavailable"):
                assert subject.process.specs == []
                assert subject.process.requests == []
                assert len(subject.commands) == (
                    1 if reason == "actor_features_unavailable" else 2
                )
            else:
                assert subject.process.waits > 0
                assert subject.process.config_reads == (
                    1 if phase == "config/read" else 2
                )
                assert all(
                    stream is not None and stream.closed
                    for stream in (
                        subject.process.stdin,
                        subject.process.stdout,
                        subject.process.stderr,
                    )
                )
        else:
            assert (result.returncode, result.stdout, result.stderr) == (
                0,
                "review complete",
                "",
            )
            assert (
                subject.process.specs[0].argv[4:] == subject.plan.configuration_tokens
            )
            assert subject.process.waits > 0
        _assert_filesystem_table(control.plan)
    finally:
        control.dispose()
        subject.dispose()


@settings(max_examples=12)
@given(
    codepoint=st.one_of(
        st.integers(min_value=0, max_value=31),
        st.integers(min_value=0xD800, max_value=0xDFFF),
    )
)
def test_permission_cli_generated_forbidden_characters_refuse_before_valid_control(
    codepoint: int,
) -> None:
    binding, protected = _permission_fixture()
    extra = replace(
        protected[0],
        lexical_path=Path("/generated-invalid-lexical/extra"),
        canonical_path=Path("/generated-invalid-canonical/extra"),
    )
    for path_field in ("lexical_path", "canonical_path"):
        invalid = replace(
            extra, **{path_field: Path("/invalid") / ("path-" + chr(codepoint))}
        )
        with pytest.raises(
            worker.actor_frontend.ActorHostError, match="^actor_plan_invalid$"
        ):
            worker.actor_mcp_host.build_actor_permission_plan(
                binding, (*protected, invalid)
            )
    _assert_filesystem_table(
        worker.actor_mcp_host.build_actor_permission_plan(binding, protected)
    )
