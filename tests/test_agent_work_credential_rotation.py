# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for safe service-credential identity rebinding."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from agent_work.cli import main
from agent_work.credential_rotation import (
    CredentialRotationError,
    CredentialRotationReport,
    RotationDependencies,
    _AtomicWriteUncertain,
    atomic_write_environment,
    parse_rotation_environment,
    replace_factory_username,
    rotate_credential,
)
from agent_work.repository import CommandResult


def test_replace_factory_username_preserves_all_unrelated_environment_bytes() -> None:
    original = (
        b"GITLAB_HOST=gitlab.example.test\n"
        b"AGENT_WORK_FACTORY_USERNAME=old-bot\n"
        b"UNRELATED_VALUE=keep=this\n"
    )

    updated = replace_factory_username(original, "new-bot")

    assert updated == (
        b"GITLAB_HOST=gitlab.example.test\n"
        b"AGENT_WORK_FACTORY_USERNAME=new-bot\n"
        b"UNRELATED_VALUE=keep=this\n"
    )


def test_atomic_write_environment_replaces_complete_bytes_with_mode_0600(
    tmp_path: Path,
) -> None:
    target = tmp_path / "agent-work.env"
    target.write_bytes(b"old\n")

    atomic_write_environment(target, b"new\n")

    assert target.read_bytes() == b"new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "content",
    (
        b"GITLAB_HOST=gitlab.example.test\nGITLAB_PROJECT_ID=0\n",
        b"GITLAB_HOST=https://gitlab.example.test\nGITLAB_PROJECT_ID=12\n",
        b"GITLAB_HOST=gitlab.example.test\nGITLAB_PROJECT_ID=12\n",
    ),
)
def test_parse_rotation_environment_rejects_missing_required_safe_runtime_values(
    content: bytes,
) -> None:
    with pytest.raises(CredentialRotationError, match="environment configuration"):
        parse_rotation_environment(content)


def test_parse_rotation_environment_accepts_only_nonsecret_operational_configuration() -> (
    None
):
    environment = parse_rotation_environment(
        b"GITLAB_HOST=gitlab.example.test\n"
        b"GITLAB_PROJECT_ID=12\n"
        b"AGENT_WORK_CHECKOUT=/srv/agent-work\n"
        b"AGENT_WORK_DIRENV=/usr/bin/direnv\n"
        b"AGENT_WORK_GIT=/usr/bin/git\n"
        b"AGENT_WORK_GLAB=/usr/bin/glab\n"
        b"AGENT_WORK_JQ=/usr/bin/jq\n"
        b"AGENT_WORK_UV=/usr/bin/uv\n"
        b"AGENT_WORK_FLOCK=/usr/bin/flock\n"
        b"AGENT_WORK_TIMEOUT=/usr/bin/timeout\n"
        b"AGENT_WORK_AGENT=codex\n"
        b"AGENT_WORK_AGENT_PATH=/usr/bin/codex\n"
        b"AGENT_WORK_BASE_BRANCH=agent/factory\n"
        b"AGENT_WORK_FACTORY_USERNAME=old-bot\n"
    )

    assert environment.host == "gitlab.example.test"
    assert environment.project_id == 12
    assert environment.factory_username == "old-bot"
    assert "TOKEN" not in repr(environment).upper()


def _environment_bytes(username: str = "old-bot") -> bytes:
    return (
        b"GITLAB_HOST=gitlab.example.test\n"
        b"GITLAB_PROJECT_ID=12\n"
        b"AGENT_WORK_CHECKOUT=/srv/agent-work\n"
        b"AGENT_WORK_DIRENV=/usr/bin/direnv\n"
        b"AGENT_WORK_GIT=/usr/bin/git\n"
        b"AGENT_WORK_GLAB=/usr/bin/glab\n"
        b"AGENT_WORK_JQ=/usr/bin/jq\n"
        b"AGENT_WORK_UV=/usr/bin/uv\n"
        b"AGENT_WORK_FLOCK=/usr/bin/flock\n"
        b"AGENT_WORK_TIMEOUT=/usr/bin/timeout\n"
        b"AGENT_WORK_AGENT=codex\n"
        b"AGENT_WORK_AGENT_PATH=/usr/bin/codex\n"
        b"AGENT_WORK_BASE_BRANCH=agent/factory\n"
        + f"AGENT_WORK_FACTORY_USERNAME={username}\n".encode()
        + b"UNRELATED_VALUE=preserved\n"
    )


def _successful_commands(
    *, preview_returncode: int = 0
) -> Callable[..., CommandResult]:
    def run_command(
        command: tuple[str, ...],
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del stdin, timeout_seconds, environment
        joined = " ".join(command)
        if " is-active " in f" {joined} ":
            return CommandResult(returncode=3, stdout="inactive\n", stderr="")
        if joined.endswith(" user"):
            return CommandResult(
                returncode=0, stdout='{"username":"new-bot"}\n', stderr=""
            )
        if "repository/branches/main" in joined:
            return CommandResult(
                returncode=0,
                stdout=(
                    '{"name":"main","default":true,"protected":true,'
                    '"developers_can_push":false,"developers_can_merge":false,'
                    '"can_push":false}\n'
                ),
                stderr="",
            )
        if " projects/12" in joined:
            return CommandResult(
                returncode=0,
                stdout=(
                    '{"id":12,"default_branch":"main","path_with_namespace":"group/project",'
                    '"permissions":{"project_access":{"access_level":30},'
                    '"group_access":{"access_level":20}}}\n'
                ),
                stderr="",
            )
        if "approval_rules" in joined:
            return CommandResult(
                returncode=0,
                stdout=(
                    '[{"id":1,"name":"global","approvals_required":1,'
                    '"protected_branches":[]}]\n'
                ),
                stderr="",
            )
        if "scripts/agent-work-systemd-run.sh" in joined:
            return CommandResult(
                returncode=preview_returncode,
                stdout="{}\n",
                stderr="",
            )
        return CommandResult(returncode=0, stdout="", stderr="")

    return run_command


def test_rotate_dry_run_makes_no_systemd_gitlab_preview_or_write_calls() -> None:
    calls: list[tuple[str, ...]] = []
    writes: list[bytes] = []

    def read_bytes(_path: Path) -> bytes:
        return _environment_bytes()

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise AssertionError("dry run must not invoke subprocesses")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=False,
        dependencies=RotationDependencies(
            read_bytes=read_bytes,
            atomic_write=lambda _path, content: writes.append(content),
            run_command=run_command,
        ),
    )

    assert report.outcome == "preview"
    assert report.config_updated is False
    assert calls == []
    assert writes == []


def test_rotate_dry_run_rejects_invalid_unit_without_effects() -> None:
    calls: list[tuple[str, ...]] = []
    writes: list[bytes] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise AssertionError("invalid dry run must not invoke subprocesses")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="unsafe unit",
        apply=False,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: _environment_bytes(),
            atomic_write=lambda _path, content: writes.append(content),
            run_command=run_command,
        ),
    )

    assert report.outcome == "correction-needed"
    assert calls == []
    assert writes == []


def test_rotate_unchanged_identity_stops_verifies_then_previews_without_write() -> None:
    commands: list[tuple[str, ...]] = []
    original = _environment_bytes("new-bot")

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, _content: pytest.fail(
                "unchanged identity writes"
            ),
            run_command=run_command,
        ),
    )

    first_api = next(
        index for index, command in enumerate(commands) if command[-1] == "user"
    )
    assert all("systemctl" in command[0] for command in commands[:first_api])
    assert report.outcome == "verified"
    assert report.config_updated is False
    assert report.timer_stopped is True and report.service_stopped is True
    assert "scripts/agent-work-systemd-run.sh" in commands[-1][0]
    assert "--apply" not in commands[-1]


def test_rotate_changed_identity_writes_only_rebound_value_then_previews() -> None:
    original = _environment_bytes()
    writes: list[bytes] = []

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, content: writes.append(content),
            run_command=_successful_commands(),
        ),
    )

    assert writes == [replace_factory_username(original, "new-bot")]
    assert report.outcome == "verified"
    assert report.config_updated is True
    assert report.rolled_back is False


def test_rotate_scrubs_outer_environment_but_preserves_the_direnv_credential_route() -> (
    None
):
    seen: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        seen.append(
            (command, cast(Mapping[str, str] | None, kwargs.get("environment")))
        )
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: _environment_bytes(),
            atomic_write=lambda _path, _content: None,
            run_command=run_command,
            environment={
                "HOME": "/home/service",
                "PATH": "/safe/bin",
                "XDG_STATE_HOME": "/state",
                "GITLAB_TOKEN": "sentinel-not-rendered",
                "GITLAB_HOST": "hostile.example",
                "GIT_CONFIG_PARAMETERS": "hostile",
            },
        ),
    )

    api_command, api_environment = next(
        entry for entry in seen if entry[0][-1] == "user"
    )
    assert report.outcome == "verified"
    assert api_command[:5] == (
        "/usr/bin/direnv",
        "exec",
        "/srv/agent-work",
        "/usr/bin/env",
        "-u",
    )
    assert "-i" not in api_command
    assert api_environment == {
        "HOME": "/home/service",
        "PATH": "/safe/bin",
        "XDG_STATE_HOME": "/state",
    }
    assert all("sentinel-not-rendered" not in " ".join(command) for command, _ in seen)


def test_rotate_preview_uses_the_rebound_username_and_numeric_project_endpoints() -> (
    None
):
    calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run_command(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append(
            (command, cast(Mapping[str, str] | None, kwargs.get("environment")))
        )
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: _environment_bytes(),
            atomic_write=lambda _path, _content: None,
            run_command=run_command,
        ),
    )

    preview = next(entry for entry in calls if "systemd-run.sh" in entry[0][0])
    assert preview[1] is not None
    assert report.outcome == "verified"
    assert preview[1]["AGENT_WORK_FACTORY_USERNAME"] == "new-bot"
    assert any(command[-1] == "/projects/12/approval_rules" for command, _ in calls)
    assert any(
        "/projects/12/repository/branches/main" in command for command, _ in calls
    )


def test_rotate_rejects_noninactive_unit_evidence() -> None:
    original = _environment_bytes()

    def hostile(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if " is-active " in f" {' '.join(command)} ":
            return CommandResult(returncode=1, stdout="failed\n", stderr="")
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, _content: pytest.fail("unsafe write"),
            run_command=hostile,
        ),
    )

    assert report.outcome == "correction-needed"
    assert report.timer_stopped is False


def test_rotate_api_failure_after_stop_preserves_original_bytes_and_never_writes() -> (
    None
):
    original = _environment_bytes()
    commands: list[tuple[str, ...]] = []

    def expired_user(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        if command[-1] == "user":
            return CommandResult(returncode=1, stdout="", stderr="expired-token-value")
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, _content: pytest.fail(
                "expired credential writes"
            ),
            run_command=expired_user,
        ),
    )

    assert report.outcome == "correction-needed"
    assert report.config_updated is False
    assert report.timer_stopped is True and report.service_stopped is True
    assert "expired-token-value" not in report.model_dump_json()
    assert any(command[-1] == "user" for command in commands)


@pytest.mark.parametrize(
    "endpoint, payload",
    (
        ("projects/12", '{"id":13}'),
        (
            "projects/12",
            '{"id":12,"default_branch":"main","permissions":{"project_access":{"access_level":20},"group_access":{"access_level":20}}}',
        ),
        (
            "projects/12",
            '{"id":12,"default_branch":"main","permissions":{"project_access":{"access_level":40},"group_access":{"access_level":20}}}',
        ),
        (
            "projects/12",
            '{"id":12,"default_branch":"main","permissions":{"project_access":{"access_level":true},"group_access":{"access_level":20}}}',
        ),
        (
            "repository/branches/main",
            '{"name":"main","default":true,"protected":true,"developers_can_push":false,"developers_can_merge":false}',
        ),
        (
            "approval_rules",
            '[{"id":1,"name":"branch-only","approvals_required":1,"protected_branches":[{"id":3}]}]',
        ),
    ),
)
def test_rotate_rejects_untrusted_gitlab_evidence_before_config_write(
    endpoint: str, payload: str
) -> None:
    original = _environment_bytes()

    def evidence_failure(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if " ".join(command).endswith(endpoint):
            return CommandResult(returncode=0, stdout=payload, stderr="")
        return _successful_commands()(command, **kwargs)

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, _content: pytest.fail("invalid evidence writes"),
            run_command=evidence_failure,
        ),
    )

    assert report.outcome == "correction-needed"
    assert report.config_updated is False
    assert report.timer_stopped is True and report.service_stopped is True


def test_rotate_failed_isolated_preview_rolls_back_once_to_exact_original_bytes() -> (
    None
):
    original = _environment_bytes()
    writes: list[bytes] = []

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=lambda _path, content: writes.append(content),
            run_command=_successful_commands(preview_returncode=1),
        ),
    )

    assert writes == [replace_factory_username(original, "new-bot"), original]
    assert report.outcome == "rolled-back"
    assert report.rolled_back is True
    assert report.timer_stopped is True and report.service_stopped is True


def test_rotate_write_interruption_keeps_stopped_and_reports_sanitized_handoff() -> (
    None
):
    original = _environment_bytes()
    complete_files: list[bytes] = []

    def interrupted_write(_path: Path, content: bytes) -> None:
        complete_files.append(content)
        raise InterruptedError("token-value must not escape")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=interrupted_write,
            run_command=_successful_commands(),
        ),
    )

    assert complete_files == [replace_factory_username(original, "new-bot")]
    assert report.outcome == "correction-needed"
    assert report.timer_stopped is True and report.service_stopped is True
    assert "token-value" not in report.model_dump_json()


def test_rotate_replace_failure_reports_unchanged_configuration() -> None:
    original = _environment_bytes()

    def replace_failure(_path: Path, _content: bytes) -> None:
        raise OSError("replace did not run")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=replace_failure,
            run_command=_successful_commands(),
        ),
    )

    assert report.outcome == "correction-needed"
    assert report.config_updated is False
    assert report.timer_stopped is True and report.service_stopped is True


def test_rotate_post_replace_uncertainty_reports_updated_configuration() -> None:
    original = _environment_bytes()

    def uncertain_write(_path: Path, _content: bytes) -> None:
        raise _AtomicWriteUncertain("directory durability is uncertain")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=uncertain_write,
            run_command=_successful_commands(),
        ),
    )

    assert report.outcome == "correction-needed"
    assert report.config_updated is True
    assert report.timer_stopped is True and report.service_stopped is True


def test_rotate_rollback_failure_is_bounded_and_reports_config_uncertainty() -> None:
    original = _environment_bytes()
    writes: list[bytes] = []

    def fail_rollback(_path: Path, content: bytes) -> None:
        writes.append(content)
        if content == original:
            raise OSError("rollback unavailable")

    report = rotate_credential(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=RotationDependencies(
            read_bytes=lambda _path: original,
            atomic_write=fail_rollback,
            run_command=_successful_commands(preview_returncode=1),
        ),
    )

    assert writes == [replace_factory_username(original, "new-bot"), original]
    assert report.outcome == "correction-needed"
    assert report.rolled_back is False
    assert "config" in report.next_action.lower()


def test_cli_uses_injected_rotation_boundary_and_never_renders_a_token() -> None:
    output: list[str] = []
    errors: list[str] = []
    seen: list[tuple[Path, str, bool]] = []

    def rotate(
        *,
        environment_file: Path,
        unit_name: str,
        apply: bool,
        dependencies: RotationDependencies,
    ) -> CredentialRotationReport:
        del dependencies
        seen.append((environment_file, unit_name, apply))
        return CredentialRotationReport(
            command="credential.rotate",
            applied=apply,
            outcome="verified",
            previous_username="old-bot",
            current_username="new-bot",
            config_updated=True,
            rolled_back=False,
            timer_stopped=True,
            service_stopped=True,
            next_action="Review the retained record before re-enabling the timer.",
        )

    exit_code = main(
        [
            "credential",
            "rotate",
            "--environment-file",
            "/etc/agent-work.env",
            "--unit-name",
            "agent-work",
            "--apply",
            "--json",
        ],
        rotate=rotate,
        write_stdout=output.append,
        write_stderr=errors.append,
    )

    assert exit_code == 0
    assert seen == [(Path("/etc/agent-work.env"), "agent-work", True)]
    assert json.loads(output[0])["outcome"] == "verified"
    assert "token" not in f"{output}{errors}".lower()
