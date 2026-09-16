# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for versioned installed service environments."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_work.cli import main
from agent_work.repository import CommandResult, RepositoryError
from agent_work.service_environment import (
    ServiceEnvironmentDependencies,
    ServiceEnvironmentError,
    ServiceEnvironmentReport,
    migrate_service_environment,
    parse_service_environment,
)


def _legacy_environment() -> bytes:
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
        b"AGENT_WORK_AGENT=codex\n"
        b"AGENT_WORK_AGENT_PATH=/usr/bin/codex\n"
        b"AGENT_WORK_BASE_BRANCH=agent/factory\n"
        b"AGENT_WORK_FACTORY_USERNAME=factory-bot\n"
    )


def _current_environment() -> bytes:
    return b"AGENT_WORK_ENV_VERSION=1\n" + _legacy_environment().replace(
        b"AGENT_WORK_FLOCK=/usr/bin/flock\n",
        b"AGENT_WORK_FLOCK=/usr/bin/flock\nAGENT_WORK_TIMEOUT=/usr/bin/timeout\n",
    )


def _successful_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
    joined = " ".join(command)
    if " is-active " in f" {joined} ":
        return CommandResult(returncode=3, stdout="inactive\n", stderr="")
    if command[-1] == "user":
        return CommandResult(
            returncode=0, stdout='{"username":"factory-bot"}\n', stderr=""
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
                '{"id":12,"default_branch":"main","permissions":'
                '{"project_access":{"access_level":30},'
                '"group_access":{"access_level":20}}}\n'
            ),
            stderr="",
        )
    if "approval_rules" in joined:
        return CommandResult(
            returncode=0,
            stdout='[{"id":1,"approvals_required":1,"protected_branches":[]}]\n',
            stderr="",
        )
    return CommandResult(returncode=0, stdout="{}\n", stderr="")


def _dependencies(
    content: bytes,
    *,
    run_command=_successful_command,
    atomic_write=lambda _path, _content: None,
    environment: dict[str, str] | None = None,
) -> ServiceEnvironmentDependencies:
    return ServiceEnvironmentDependencies(
        read_bytes=lambda _path: content,
        atomic_write=atomic_write,
        run_command=run_command,
        resolve_executable=lambda name: f"/usr/bin/{name}",
        checkout=Path("/srv/agent-work"),
        environment=environment or {"PATH": "/usr/bin", "HOME": "/home/service"},
    )


def test_migration_dry_run_classifies_pre_version_install_as_stale_without_effects() -> (
    None
):
    calls: list[tuple[str, ...]] = []
    writes: list[bytes] = []

    def no_effects(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise AssertionError("dry-run must not stop units, call GitLab, or validate")

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=False,
        dependencies=_dependencies(
            _legacy_environment(),
            run_command=no_effects,
            atomic_write=lambda _p, b: writes.append(b),
        ),
    )

    assert report.outcome == "stale"
    assert report.config_updated is False
    assert calls == []
    assert writes == []


def test_migration_apply_regenerates_only_managed_fields_and_preserves_operator_values() -> (
    None
):
    original = _legacy_environment()
    writes: list[bytes] = []

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original, atomic_write=lambda _path, data: writes.append(data)
        ),
    )

    assert report.outcome == "verified"
    assert report.config_updated is True
    assert writes == [
        (
            b"AGENT_WORK_ENV_VERSION=1\n"
            b"AGENT_WORK_CHECKOUT=/srv/agent-work\n"
            b"AGENT_WORK_GIT=/usr/bin/git\n"
            b"AGENT_WORK_GLAB=/usr/bin/glab\n"
            b"AGENT_WORK_JQ=/usr/bin/jq\n"
            b"AGENT_WORK_FLOCK=/usr/bin/flock\n"
            b"AGENT_WORK_TIMEOUT=/usr/bin/timeout\n"
            b"AGENT_WORK_UV=/usr/bin/uv\n"
            b"AGENT_WORK_AGENT_PATH=/usr/bin/codex\n"
            b"AGENT_WORK_DIRENV=/usr/bin/direnv\n"
            b"AGENT_WORK_AGENT=codex\n"
            b"AGENT_WORK_BASE_BRANCH=agent/factory\n"
            b"AGENT_WORK_FACTORY_USERNAME=factory-bot\n"
            b"GITLAB_HOST=gitlab.example.test\n"
            b"GITLAB_PROJECT_ID=12\n"
        )
    ]


def test_migration_accepts_the_prior_unversioned_timeout_contract() -> None:
    writes: list[bytes] = []
    prior = _legacy_environment().replace(
        b"AGENT_WORK_FLOCK=/usr/bin/flock\n",
        b"AGENT_WORK_FLOCK=/usr/bin/flock\nAGENT_WORK_TIMEOUT=/old/timeout\n",
    )

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            prior, atomic_write=lambda _path, content: writes.append(content)
        ),
    )

    assert report.outcome == "verified"
    assert writes and b"AGENT_WORK_TIMEOUT=/usr/bin/timeout\n" in writes[0]


def test_migration_current_install_is_deterministically_nonmutating() -> None:
    calls: list[tuple[str, ...]] = []

    def no_effects(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        raise AssertionError("current dry-run must not have external effects")

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=False,
        dependencies=_dependencies(_current_environment(), run_command=no_effects),
    )

    assert report.outcome == "current"
    assert report.config_updated is False
    assert calls == []


def test_migration_apply_current_install_stops_and_verifies_without_rewriting() -> None:
    commands: list[tuple[str, ...]] = []
    writes: list[bytes] = []

    def record(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            _current_environment(),
            run_command=record,
            atomic_write=lambda _path, content: writes.append(content),
        ),
    )

    assert report.outcome == "verified"
    assert report.config_updated is False
    assert writes == []
    assert commands[:4] == [
        ("systemctl", "--user", "disable", "--now", "agent-work.timer"),
        ("systemctl", "--user", "stop", "agent-work.service"),
        ("systemctl", "--user", "is-active", "agent-work.timer"),
        ("systemctl", "--user", "is-active", "agent-work.service"),
    ]
    validation_commands = [
        command for command in commands if "agent-work-systemd-run.sh" in command[0]
    ]
    assert validation_commands == [
        (
            "/srv/agent-work/scripts/agent-work-systemd-run.sh",
            "--validate-service-environment",
        )
    ]


def test_migration_uses_only_the_service_baseline_for_safeguards_and_preview() -> None:
    command_environments: list[dict[str, str] | None] = []

    def record_environment(
        command: tuple[str, ...],
        *,
        environment: Mapping[str, str] | None = None,
        **kwargs: object,
    ) -> CommandResult:
        command_environments.append(
            dict(environment) if environment is not None else None
        )
        return _successful_command(command, environment=environment, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            _current_environment(),
            run_command=record_environment,
            environment={
                "HOME": "/home/service",
                "PATH": "/usr/bin",
                "GITLAB_TOKEN": "secret-value",
                "GIT_CONFIG_PARAMETERS": "hostile",
                "UNRELATED": "hostile",
            },
        ),
    )

    assert report.outcome == "verified"
    isolated = [environment for environment in command_environments if environment]
    assert isolated
    for environment in isolated:
        assert environment is not None
        assert "GITLAB_TOKEN" not in environment
        assert "GIT_CONFIG_PARAMETERS" not in environment
        assert "UNRELATED" not in environment
        assert environment["HOME"] == "/home/service"
        assert environment["PATH"] == "/usr/bin"


def test_migration_rejects_secret_or_unrecognized_configuration_before_effects() -> (
    None
):
    for unsafe in (b"GITLAB_TOKEN=secret-value\n", b"UNRELATED_VALUE=unsafe\n"):
        with pytest.raises(ServiceEnvironmentError, match="environment configuration"):
            parse_service_environment(_current_environment() + unsafe)


def test_migration_stops_units_before_safeguards_and_restores_prior_bytes_after_failed_preview() -> (
    None
):
    original = _legacy_environment()
    commands: list[tuple[str, ...]] = []
    writes: list[bytes] = []

    def failed_preview(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        if "agent-work-systemd-run.sh" in command[0]:
            assert (
                "loop" not in command
                and "run" not in command
                and "--apply" not in command
            )
            return CommandResult(returncode=1, stdout="", stderr="validation failed")
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original,
            run_command=failed_preview,
            atomic_write=lambda _path, data: writes.append(data),
        ),
    )

    first_api = next(
        index for index, command in enumerate(commands) if command[-1] == "user"
    )
    assert all(command[0] == "systemctl" for command in commands[:first_api])
    assert writes == [writes[0], original]
    assert b"AGENT_WORK_ENV_VERSION=1\n" in writes[0]
    assert report.outcome == "rolled-back"
    assert report.timer_stopped is True and report.service_stopped is True


def test_migration_rolls_back_after_repository_error_from_preview() -> None:
    original = _legacy_environment()
    writes: list[bytes] = []

    def repository_failure(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if "agent-work-systemd-run.sh" in command[0]:
            raise RepositoryError("secret-preview-detail")
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original,
            run_command=repository_failure,
            atomic_write=lambda _path, data: writes.append(data),
        ),
    )

    assert report.outcome == "rolled-back"
    assert writes[-1] == original
    assert "secret-preview-detail" not in report.model_dump_json()


@pytest.mark.parametrize(
    "failure", (InterruptedError("write interrupted"), OSError("replace failed"))
)
def test_migration_interrupted_write_keeps_prior_bytes_recoverable_and_units_stopped(
    failure: OSError,
) -> None:
    original = _legacy_environment()
    attempted: list[bytes] = []

    def interrupted_write(_path: Path, content: bytes) -> None:
        attempted.append(content)
        raise failure

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(original, atomic_write=interrupted_write),
    )

    assert len(attempted) == 2
    assert attempted[0] != original
    assert attempted[-1] == original
    assert report.outcome == "correction-needed"
    assert report.rolled_back is False
    assert report.config_updated is True
    assert report.timer_stopped is True and report.service_stopped is True
    assert "interrupted" not in report.model_dump_json().lower()
    assert "review" in report.next_action.lower()


def test_migration_uncertain_atomic_replacement_restores_exact_prior_bytes() -> None:
    original = _legacy_environment()
    stored = {"content": original}
    writes: list[bytes] = []

    def replacement_with_uncertain_durability(_path: Path, content: bytes) -> None:
        writes.append(content)
        stored["content"] = content
        if len(writes) == 1:
            raise OSError("durability state unavailable")

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original,
            atomic_write=replacement_with_uncertain_durability,
        ),
    )

    assert stored["content"] == original
    assert writes[0] != original
    assert writes[-1] == original
    assert report.outcome in {"rolled-back", "correction-needed"}
    assert report.timer_stopped is True and report.service_stopped is True
    assert "durability state unavailable" not in report.model_dump_json()


def test_migration_rejects_failed_safeguards_before_write_and_keeps_units_stopped() -> (
    None
):
    original = _legacy_environment()
    writes: list[bytes] = []

    def rejected_project(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if " projects/12" in " ".join(command):
            return CommandResult(
                returncode=0,
                stdout=(
                    '{"id":12,"default_branch":"main","permissions":'
                    '{"project_access":{"access_level":20},'
                    '"group_access":{"access_level":20}}}\n'
                ),
                stderr="credential-value-must-not-escape",
            )
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original,
            run_command=rejected_project,
            atomic_write=lambda _path, data: writes.append(data),
        ),
    )

    assert writes == []
    assert report.outcome == "correction-needed"
    assert report.timer_stopped is True and report.service_stopped is True
    assert "credential-value-must-not-escape" not in report.model_dump_json()


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("secret-runtime-detail"),
        RepositoryError("secret-repository-detail"),
    ),
)
def test_migration_reports_safeguard_errors_before_write_with_exact_stop_state(
    failure: RuntimeError,
) -> None:
    original = _legacy_environment()
    writes: list[bytes] = []

    def failed_safeguard(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if command[-1] == "user":
            raise failure
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(
            original,
            run_command=failed_safeguard,
            atomic_write=lambda _path, content: writes.append(content),
        ),
    )

    assert writes == []
    assert report.outcome == "correction-needed"
    assert report.timer_stopped is True and report.service_stopped is True
    assert "secret-" not in report.model_dump_json()


def test_migration_reports_each_independently_stopped_unit_after_partial_stop() -> None:
    original = _legacy_environment()

    def partial_stop(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if command[-2:] == ("is-active", "agent-work.timer"):
            return CommandResult(returncode=0, stdout="active\n", stderr="")
        if command[-2:] == ("is-active", "agent-work.service"):
            return CommandResult(returncode=3, stdout="inactive\n", stderr="")
        if command[-1] == "agent-work.timer":
            return CommandResult(returncode=1, stdout="", stderr="failed")
        if command[-1] == "agent-work.service":
            return CommandResult(returncode=0, stdout="", stderr="")
        return _successful_command(command, **kwargs)

    report = migrate_service_environment(
        environment_file=Path("/etc/agent-work.env"),
        unit_name="agent-work",
        apply=True,
        dependencies=_dependencies(original, run_command=partial_stop),
    )

    assert report.outcome == "correction-needed"
    assert report.timer_stopped is False
    assert report.service_stopped is True


@pytest.mark.parametrize(
    "unsafe",
    (
        b"AGENT_WORK_AGENT=codex\nAGENT_WORK_AGENT=pi\n",
        b"AGENT_WORK_TIMEOUT=relative-timeout\n",
        b"GITLAB_HOST=https://gitlab.example.test\n",
        b"AGENT_WORK_ENV_VERSION=2\n",
    ),
)
def test_parser_rejects_duplicate_or_malformed_contract_values(unsafe: bytes) -> None:
    if unsafe.startswith(b"AGENT_WORK_AGENT="):
        content = _current_environment() + unsafe
    else:
        name = unsafe.split(b"=", 1)[0]
        content = (
            b"\n".join(
                line
                for line in _current_environment().splitlines()
                if not line.startswith(name)
            )
            + b"\n"
            + unsafe
        )

    with pytest.raises(ServiceEnvironmentError, match="environment configuration"):
        parse_service_environment(content)


@pytest.mark.parametrize(
    "unsafe",
    (b"AGENT_WORK_AGENT=unknown\n", b"AGENT_WORK_BASE_BRANCH=main\n"),
)
def test_parser_rejects_unsafe_owned_identity_values(unsafe: bytes) -> None:
    name = unsafe.split(b"=", 1)[0]
    content = (
        b"\n".join(
            line
            for line in _current_environment().splitlines()
            if line.split(b"=", 1)[0] != name
        )
        + b"\n"
        + unsafe
    )

    with pytest.raises(ServiceEnvironmentError, match="environment configuration"):
        parse_service_environment(content)


@pytest.mark.parametrize(
    ("apply", "outcome", "expected_exit"),
    ((False, "stale", 0), (True, "verified", 0), (True, "correction-needed", 1)),
)
def test_service_environment_migrate_cli_uses_dedicated_boundary_and_exit_statuses(
    apply: bool, outcome: str, expected_exit: int
) -> None:
    seen: list[tuple[Path, str, bool, Path]] = []
    output: list[str] = []

    def migrate(
        *,
        environment_file: Path,
        unit_name: str,
        apply: bool,
        dependencies: ServiceEnvironmentDependencies,
    ) -> ServiceEnvironmentReport:
        seen.append((environment_file, unit_name, apply, dependencies.checkout))
        return ServiceEnvironmentReport(
            applied=apply,
            outcome=outcome,
            config_updated=apply and outcome == "verified",
            timer_stopped=apply,
            service_stopped=apply,
            next_action="Review the migration result before enabling the timer.",
        )

    arguments = [
        "service-environment",
        "migrate",
        "--environment-file",
        "/etc/agent-work.env",
        "--unit-name",
        "agent-work",
        "--json",
    ]
    if apply:
        arguments.append("--apply")
    else:
        arguments.append("--dry-run")
    exit_code = main(
        arguments,
        dispatch=lambda *_args, **_kwargs: pytest.fail(
            "must not enter runtime dispatch"
        ),
        migrate_service_environment=migrate,
        current_checkout=lambda: Path("/explicit/current-checkout"),
        write_stdout=output.append,
    )

    assert exit_code == expected_exit
    assert seen == [
        (
            Path("/etc/agent-work.env"),
            "agent-work",
            apply,
            Path("/explicit/current-checkout"),
        )
    ]
    assert '"outcome":"' + outcome + '"' in output[0]


def test_service_environment_migrate_cli_returns_usage_exit_for_missing_target() -> (
    None
):
    with pytest.raises(SystemExit) as error:
        main(["service-environment", "migrate", "--unit-name", "agent-work"])

    assert error.value.code == 2


def test_service_environment_migrate_cli_rejects_conflicting_apply_modes() -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "service-environment",
                "migrate",
                "--environment-file",
                "/etc/agent-work.env",
                "--unit-name",
                "agent-work",
                "--apply",
                "--dry-run",
            ]
        )

    assert error.value.code == 2


def test_service_environment_cli_redacts_migration_failures_from_both_streams() -> None:
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = main(
        [
            "service-environment",
            "migrate",
            "--environment-file",
            "/etc/agent-work.env",
            "--unit-name",
            "agent-work",
        ],
        migrate_service_environment=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("secret-value")
        ),
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["service environment migration could not be completed"]


def test_installed_setup_and_wrapper_require_the_versioned_contract_before_controller(
    project_root: Path,
) -> None:
    root_wrapper = (project_root / "scripts" / "agent-work-systemd-run.sh").read_text()
    template_wrapper = (
        project_root / "template" / "scripts" / "agent-work-systemd-run.sh"
    ).read_text()
    root_setup = (
        project_root / "docs" / "how-to" / "setup-serial-gitlab-agent-loop.md"
    ).read_text()
    template_setup = (
        project_root
        / "template"
        / "docs"
        / "how-to"
        / "setup-serial-gitlab-agent-loop.md"
    ).read_text()
    root_service = (
        project_root / "systemd" / "agentic-copier-agent-work.service"
    ).read_text()
    template_service = (
        project_root
        / "template"
        / "systemd"
        / "{{ project_slug }}-agent-work.service.jinja"
    ).read_text()

    assert root_wrapper == template_wrapper
    assert "AGENT_WORK_ENV_VERSION" in root_wrapper
    assert "AGENT_WORK_ENV_VERSION=1" in root_wrapper
    assert root_wrapper.index("AGENT_WORK_ENV_VERSION") < root_wrapper.index(
        "agent-work loop run"
    )
    assert "AGENT_WORK_ENV_VERSION=%q" in root_setup
    assert "AGENT_WORK_ENV_VERSION=%q" in template_setup
    for service in (root_service, template_service):
        assert "EnvironmentFile=" in service
        assert "agent-work-systemd-run.sh --apply" in service
