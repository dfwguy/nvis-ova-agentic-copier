# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Safely rebind the service identity after an out-of-band credential rotation."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .domain import HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS
from .ports import CommandResult, CommandRunner
from .repository import build_git_environment


class CredentialRotationError(RuntimeError):
    """A rotation precondition or verification could not be established."""


class EnvironmentConfigurationError(ValueError):
    """A non-secret environment file does not satisfy the shared grammar."""


@dataclass(frozen=True)
class ServiceUnitStopState:
    """Observed stopped state after a service-unit stop attempt."""

    timer_stopped: bool
    service_stopped: bool


@dataclass(frozen=True)
class PathEvidence:
    """Immutable ownership and type evidence for one inspected path."""

    exists: bool
    is_dir: bool
    is_socket: bool
    owner_uid: int | None


class ServiceUnitStopError(CredentialRotationError):
    """Stopping units failed while retaining their independently observed state."""

    def __init__(self, state: ServiceUnitStopState) -> None:
        super().__init__("could not stop service units")
        self.state = state


class _AtomicWriteUncertain(OSError):
    """An atomic replacement completed but its directory durability is uncertain."""


class RotationEnvironment(BaseModel):
    """Validated non-secret values required by the isolated service route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    project_id: int = Field(gt=0)
    checkout: Path
    direnv: Path
    git: Path
    glab: Path
    jq: Path
    uv: Path
    flock: Path
    timeout: Path
    agent: str
    agent_path: Path
    base_branch: str
    factory_username: str

    @field_validator("host")
    @classmethod
    def _host_is_bare(cls, value: str) -> str:
        if not value or "://" in value or "/" in value:
            raise ValueError("host is not bare")
        return value

    @field_validator(
        "checkout",
        "direnv",
        "git",
        "glab",
        "jq",
        "uv",
        "flock",
        "timeout",
        "agent_path",
    )
    @classmethod
    def _path_is_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("path is not absolute")
        return value

    @field_validator("factory_username")
    @classmethod
    def _username_is_safe(cls, value: str) -> str:
        if not _USERNAME.fullmatch(value):
            raise ValueError("username is unsafe")
        return value


class CredentialRotationReport(BaseModel):
    """Stable redacted result of one credential-rotation attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = "credential.rotate"
    applied: bool
    outcome: str
    previous_username: str | None = None
    current_username: str | None = None
    config_updated: bool = False
    rolled_back: bool = False
    timer_stopped: bool = False
    service_stopped: bool = False
    next_action: str


@dataclass(frozen=True)
class RotationDependencies:
    """Injected effects used by one rotation attempt."""

    read_bytes: Callable[[Path], bytes]
    atomic_write: Callable[[Path, bytes], None]
    run_command: CommandRunner
    effective_uid: Callable[[], int]
    inspect_path: Callable[[Path], PathEvidence]
    environment: Mapping[str, str] = field(default_factory=dict)


_REQUIRED_KEYS = {
    "GITLAB_HOST": "host",
    "GITLAB_PROJECT_ID": "project_id",
    "AGENT_WORK_CHECKOUT": "checkout",
    "AGENT_WORK_DIRENV": "direnv",
    "AGENT_WORK_GIT": "git",
    "AGENT_WORK_GLAB": "glab",
    "AGENT_WORK_JQ": "jq",
    "AGENT_WORK_UV": "uv",
    "AGENT_WORK_FLOCK": "flock",
    "AGENT_WORK_TIMEOUT": "timeout",
    "AGENT_WORK_AGENT": "agent",
    "AGENT_WORK_AGENT_PATH": "agent_path",
    "AGENT_WORK_BASE_BRANCH": "base_branch",
    "AGENT_WORK_FACTORY_USERNAME": "factory_username",
}
_SECRET_NAME = re.compile(
    r"(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE[_-]?KEY)", re.IGNORECASE
)
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")


def parse_rotation_environment(content: bytes) -> RotationEnvironment:
    """Parse the mode-600 non-secret environment configuration fail-closed."""
    try:
        values = parse_nonsecret_environment(content)
    except EnvironmentConfigurationError as error:
        raise CredentialRotationError("environment configuration is invalid") from error
    if set(_REQUIRED_KEYS) - set(values):
        raise CredentialRotationError("environment configuration is invalid")
    try:
        return RotationEnvironment.model_validate(
            {_REQUIRED_KEYS[name]: values[name] for name in _REQUIRED_KEYS}
        )
    except ValidationError as error:
        raise CredentialRotationError("environment configuration is invalid") from error


def parse_nonsecret_environment(content: bytes) -> dict[str, str]:
    """Parse the shared, non-secret assignment grammar without key policy."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EnvironmentConfigurationError from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EnvironmentConfigurationError
        name, raw_value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise EnvironmentConfigurationError
        if _SECRET_NAME.search(name):
            raise EnvironmentConfigurationError
        try:
            parsed_value = shlex.split(raw_value, posix=True)
        except ValueError as error:
            raise EnvironmentConfigurationError from error
        if len(parsed_value) != 1:
            raise EnvironmentConfigurationError
        values[name] = parsed_value[0]
    return values


def replace_factory_username(content: bytes, username: str) -> bytes:
    """Replace exactly one factory-username line while preserving other bytes."""
    if not _USERNAME.fullmatch(username):
        raise CredentialRotationError("environment configuration is invalid")
    marker = b"AGENT_WORK_FACTORY_USERNAME="
    lines = content.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith(marker)]
    if len(matches) != 1:
        raise CredentialRotationError("environment configuration is invalid")
    index = matches[0]
    ending = (
        b"\r\n"
        if lines[index].endswith(b"\r\n")
        else b"\n"
        if lines[index].endswith(b"\n")
        else b""
    )
    lines[index] = marker + username.encode("ascii") + ending
    return b"".join(lines)


def atomic_write_environment(path: Path, content: bytes) -> None:
    """Atomically replace an environment file with durable mode-0600 bytes."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise _AtomicWriteUncertain(
                "atomic replacement state is uncertain"
            ) from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _report(
    *,
    applied: bool,
    outcome: str,
    environment: RotationEnvironment | None = None,
    current: str | None = None,
    updated: bool = False,
    rolled_back: bool = False,
    timer: bool = False,
    service: bool = False,
    next_action: str,
) -> CredentialRotationReport:
    return CredentialRotationReport(
        applied=applied,
        outcome=outcome,
        previous_username=environment.factory_username if environment else None,
        current_username=current,
        config_updated=updated,
        rolled_back=rolled_back,
        timer_stopped=timer,
        service_stopped=service,
        next_action=next_action,
    )


def _call(
    dependencies: RotationDependencies,
    command: tuple[str, ...],
    environment: Mapping[str, str] | None = None,
) -> CommandResult:
    return dependencies.run_command(
        command,
        timeout_seconds=HOUSEKEEPING_COMMAND_TIMEOUT_SECONDS,
        environment=environment,
    )


def _user_bus_environment(dependencies: RotationDependencies) -> dict[str, str]:
    """Return the validated minimal user-manager environment."""
    uid = dependencies.effective_uid()
    runtime_value = dependencies.environment.get("XDG_RUNTIME_DIR")
    bus_value = dependencies.environment.get("DBUS_SESSION_BUS_ADDRESS")
    if runtime_value is None:
        runtime_value = f"/run/user/{uid}"
    if bus_value is None:
        bus_value = f"unix:path={runtime_value}/bus"
    if not runtime_value or not bus_value:
        raise CredentialRotationError("user bus environment is invalid")
    runtime = Path(runtime_value)
    prefix = "unix:path="
    if not runtime.is_absolute() or not bus_value.startswith(prefix):
        raise CredentialRotationError("user bus environment is invalid")
    bus = Path(bus_value.removeprefix(prefix))
    if not bus.is_absolute() or bus_value != f"{prefix}{bus}":
        raise CredentialRotationError("user bus environment is invalid")
    runtime_status = dependencies.inspect_path(runtime)
    bus_status = dependencies.inspect_path(bus)
    if not (
        runtime_status.exists
        and runtime_status.is_dir
        and runtime_status.owner_uid == uid
        and bus_status.exists
        and bus_status.is_socket
        and bus_status.owner_uid == uid
    ):
        raise CredentialRotationError("user bus environment is invalid")
    return service_baseline(dependencies.environment) | {
        "XDG_RUNTIME_DIR": runtime_value,
        "DBUS_SESSION_BUS_ADDRESS": bus_value,
    }


def service_baseline(environment: Mapping[str, str]) -> dict[str, str]:
    """Keep only the service-manager baseline needed before direnv runs."""
    return {
        name: environment[name]
        for name in ("HOME", "PATH", "XDG_CONFIG_HOME", "XDG_STATE_HOME")
        if name in environment
    }


def _json_call(
    dependencies: RotationDependencies,
    configuration: RotationEnvironment,
    endpoint: str,
) -> object:
    controls = build_git_environment({}, configuration.glab) | {
        "GITLAB_HOST": configuration.host,
        "GITLAB_PROJECT_ID": str(configuration.project_id),
        "AGENT_WORK_FACTORY_USERNAME": configuration.factory_username,
        "AGENT_WORK_GIT": str(configuration.git),
        "AGENT_WORK_GLAB": str(configuration.glab),
    }
    command = (
        str(configuration.direnv),
        "exec",
        str(configuration.checkout),
        "/usr/bin/env",
        "-u",
        "GIT_CONFIG_PARAMETERS",
        *(f"{name}={value}" for name, value in controls.items()),
        str(configuration.glab),
        "api",
        "--hostname",
        configuration.host,
        endpoint,
    )
    result = _call(dependencies, command, service_baseline(dependencies.environment))
    if result.returncode != 0:
        raise CredentialRotationError("GitLab verification failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CredentialRotationError("GitLab verification failed") from error


def _require_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CredentialRotationError("GitLab verification failed")
    return cast(dict[str, object], value)


def _optional_mapping(value: object) -> dict[str, object] | None:
    """Narrow an API list member when it is an object."""
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _numeric_value(value: object) -> int | None:
    """Accept JSON numbers while excluding Python's Boolean subclass."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _is_global_approval_rule(value: object) -> bool:
    """Recognize one unambiguous globally applicable approval rule."""
    rule = _optional_mapping(value)
    if rule is None:
        return False
    required = _numeric_value(rule.get("approvals_required"))
    return (
        required is not None
        and required >= 1
        and (
            "protected_branches" not in rule
            or rule["protected_branches"] == []
            or rule.get("applies_to_all_protected_branches") is True
        )
    )


def verify_service_safeguards(
    dependencies: RotationDependencies, configuration: RotationEnvironment
) -> str:
    user = _require_mapping(_json_call(dependencies, configuration, "user"))
    username = user.get("username")
    if not isinstance(username, str) or not _USERNAME.fullmatch(username):
        raise CredentialRotationError("GitLab verification failed")
    project = _require_mapping(
        _json_call(dependencies, configuration, f"projects/{configuration.project_id}")
    )
    if (
        project.get("id") != configuration.project_id
        or not isinstance(project.get("default_branch"), str)
        or not project["default_branch"]
    ):
        raise CredentialRotationError("GitLab verification failed")
    permissions_value = project.get("permissions")
    if not isinstance(permissions_value, dict):
        raise CredentialRotationError("GitLab verification failed")
    permissions = _require_mapping(permissions_value)
    levels = tuple(
        level
        for entry in permissions.values()
        if (permission := _optional_mapping(entry)) is not None
        and (level := _numeric_value(permission.get("access_level"))) is not None
    )
    if not levels or max(levels) != 30:
        raise CredentialRotationError("GitLab verification failed")
    default_branch = cast(str, project["default_branch"])
    branch = _require_mapping(
        _json_call(
            dependencies,
            configuration,
            f"/projects/{configuration.project_id}/repository/branches/{quote(default_branch, safe='')}",
        )
    )
    if not (
        branch.get("name") == default_branch
        and branch.get("default") is True
        and branch.get("protected") is True
        and branch.get("developers_can_push") is False
        and branch.get("developers_can_merge") is False
        and branch.get("can_push") is False
    ):
        raise CredentialRotationError("GitLab verification failed")
    rules = _json_call(
        dependencies,
        configuration,
        f"/projects/{configuration.project_id}/approval_rules",
    )
    if not isinstance(rules, list) or not any(
        _is_global_approval_rule(rule) for rule in rules
    ):
        raise CredentialRotationError("GitLab verification failed")
    return username


def validate_service_unit_name(unit_name: str) -> str:
    """Accept only one safe user-systemd unit stem."""
    if not _UNIT_NAME.fullmatch(unit_name):
        raise CredentialRotationError("service unit name is invalid")
    return unit_name


def stop_service_units(
    dependencies: RotationDependencies, unit_name: str
) -> ServiceUnitStopState:
    validate_service_unit_name(unit_name)
    environment = _user_bus_environment(dependencies)
    commands = (
        ("systemctl", "--user", "disable", "--now", f"{unit_name}.timer"),
        ("systemctl", "--user", "stop", f"{unit_name}.service"),
        ("systemctl", "--user", "is-active", f"{unit_name}.timer"),
        ("systemctl", "--user", "is-active", f"{unit_name}.service"),
    )

    def attempt(command: tuple[str, ...]) -> CommandResult | None:
        try:
            return _call(dependencies, command, environment)
        except (OSError, RuntimeError):
            return None

    results = tuple(attempt(command) for command in commands)
    stop_results = tuple(
        result is not None and result.returncode == 0 for result in results[:2]
    )
    inactive = tuple(
        result is not None
        and result.returncode == 3
        and result.stdout.strip() == "inactive"
        for result in results[2:]
    )
    state = ServiceUnitStopState(*inactive)
    if stop_results != (True, True) or inactive != (True, True):
        raise ServiceUnitStopError(state)
    return state


def rotate_credential(
    *,
    environment_file: Path,
    unit_name: str,
    apply: bool,
    dependencies: RotationDependencies,
) -> CredentialRotationReport:
    """Validate, stop, verify, and safely rebind one service identity."""
    try:
        original = dependencies.read_bytes(environment_file)
        configuration = parse_rotation_environment(original)
    except (OSError, CredentialRotationError):
        return _report(
            applied=apply,
            outcome="correction-needed",
            next_action="Correct the environment configuration before retrying.",
        )
    if not apply:
        if not _UNIT_NAME.fullmatch(unit_name):
            return _report(
                applied=False,
                outcome="correction-needed",
                environment=configuration,
                next_action="Correct the service unit name before retrying.",
            )
        return _report(
            applied=False,
            outcome="preview",
            environment=configuration,
            next_action="Review this preview, then rerun with --apply to stop and verify the service.",
        )
    timer = service = False
    try:
        stopped = stop_service_units(dependencies, unit_name)
        timer, service = stopped.timer_stopped, stopped.service_stopped
        username = verify_service_safeguards(dependencies, configuration)
    except ServiceUnitStopError as error:
        return _report(
            applied=True,
            outcome="correction-needed",
            environment=configuration,
            timer=error.state.timer_stopped,
            service=error.state.service_stopped,
            next_action="Keep any stopped unit stopped; correct the service state and retry.",
        )
    except (CredentialRotationError, OSError):
        return _report(
            applied=True,
            outcome="correction-needed",
            environment=configuration,
            timer=timer,
            service=service,
            next_action=(
                "Correct and validate the user bus before retrying."
                if not timer and not service
                else "Keep the timer and service stopped; correct the credential or GitLab safeguards, then retry."
            ),
        )
    updated = username != configuration.factory_username
    if not updated:
        changed = original
    else:
        try:
            changed = replace_factory_username(original, username)
            dependencies.atomic_write(environment_file, changed)
        except _AtomicWriteUncertain:
            return _report(
                applied=True,
                outcome="correction-needed",
                environment=configuration,
                current=username,
                updated=True,
                timer=timer,
                service=service,
                next_action="Keep the timer and service stopped; configuration state is uncertain and requires corrective review.",
            )
        except (CredentialRotationError, OSError, InterruptedError):
            return _report(
                applied=True,
                outcome="correction-needed",
                environment=configuration,
                current=username,
                timer=timer,
                service=service,
                next_action="Keep the timer and service stopped and inspect the environment configuration before retrying.",
            )
    preview = (
        "/usr/bin/env",
        "bash",
        str(configuration.checkout / "scripts/agent-work-systemd-run.sh"),
    )
    preview_environment = service_baseline(dependencies.environment) | {
        name: str(getattr(configuration, field))
        for name, field in _REQUIRED_KEYS.items()
    }
    preview_environment["AGENT_WORK_FACTORY_USERNAME"] = username
    try:
        if _call(dependencies, preview, preview_environment).returncode != 0:
            raise CredentialRotationError("isolated preview failed")
    except (CredentialRotationError, OSError):
        if updated:
            try:
                dependencies.atomic_write(environment_file, original)
            except (OSError, InterruptedError):
                return _report(
                    applied=True,
                    outcome="correction-needed",
                    environment=configuration,
                    current=username,
                    updated=True,
                    timer=timer,
                    service=service,
                    next_action="Keep the timer and service stopped; configuration state is uncertain and requires corrective review.",
                )
            return _report(
                applied=True,
                outcome="rolled-back",
                environment=configuration,
                current=username,
                updated=True,
                rolled_back=True,
                timer=timer,
                service=service,
                next_action="Keep the timer and service stopped; correct the isolated preview failure before retrying.",
            )
        return _report(
            applied=True,
            outcome="correction-needed",
            environment=configuration,
            current=username,
            timer=timer,
            service=service,
            next_action="Keep the timer and service stopped; correct the isolated preview failure before retrying.",
        )
    return _report(
        applied=True,
        outcome="verified",
        environment=configuration,
        current=username,
        updated=updated,
        timer=timer,
        service=service,
        next_action="Review the retained record before re-enabling the timer.",
    )
