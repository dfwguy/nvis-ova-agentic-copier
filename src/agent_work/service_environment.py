# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Versioned, non-secret service-environment migration boundary."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .credential_rotation import (
    CredentialRotationError,
    EnvironmentConfigurationError,
    RotationDependencies,
    RotationEnvironment,
    ServiceUnitStopError,
    parse_nonsecret_environment,
    service_baseline,
    stop_service_units,
    validate_service_unit_name,
    verify_service_safeguards,
)
from .ports import CommandRunner


class ServiceEnvironmentError(RuntimeError):
    """A service-environment contract could not be safely processed."""


_MANAGED = MappingProxyType(
    {
        "AGENT_WORK_ENV_VERSION": "version",
        "AGENT_WORK_CHECKOUT": "checkout",
        "AGENT_WORK_GIT": "git",
        "AGENT_WORK_GLAB": "glab",
        "AGENT_WORK_JQ": "jq",
        "AGENT_WORK_FLOCK": "flock",
        "AGENT_WORK_TIMEOUT": "timeout",
        "AGENT_WORK_UV": "uv",
        "AGENT_WORK_AGENT_PATH": "agent_path",
        "AGENT_WORK_DIRENV": "direnv",
    }
)
_OWNED = MappingProxyType(
    {
        "AGENT_WORK_AGENT": "agent",
        "AGENT_WORK_BASE_BRANCH": "base_branch",
        "AGENT_WORK_FACTORY_USERNAME": "factory_username",
        "GITLAB_HOST": "host",
        "GITLAB_PROJECT_ID": "project_id",
    }
)
_CURRENT_KEYS = frozenset(_MANAGED) | frozenset(_OWNED)


class ServiceEnvironment(RotationEnvironment):
    """The complete current, non-secret installed environment contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1, le=1)
    project_id: int = Field(gt=0)

    @field_validator("base_branch")
    @classmethod
    def _reserved_base(cls, value: str) -> str:
        if not value.startswith("agent/") or value == "agent/":
            raise ValueError("base branch is unsafe")
        return value

    @field_validator("agent")
    @classmethod
    def _known_agent(cls, value: str) -> str:
        if value not in {"codex", "claude", "pi"}:
            raise ValueError("agent is unsafe")
        return value


class ServiceEnvironmentReport(BaseModel):
    """Stable, sanitized migration result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = "service-environment.migrate"
    applied: bool
    outcome: str
    config_updated: bool = False
    rolled_back: bool = False
    timer_stopped: bool = False
    service_stopped: bool = False
    next_action: str


@dataclass(frozen=True)
class ServiceEnvironmentDependencies:
    """Effect boundaries for one migration."""

    read_bytes: Callable[[Path], bytes]
    atomic_write: Callable[[Path, bytes], None]
    run_command: CommandRunner
    resolve_executable: Callable[[str], str | None]
    checkout: Path
    environment: Mapping[str, str] = field(default_factory=dict)


def _values(content: bytes) -> dict[str, str]:
    try:
        values = parse_nonsecret_environment(content)
    except EnvironmentConfigurationError as error:
        raise ServiceEnvironmentError("environment configuration is invalid") from error
    if set(values) - _CURRENT_KEYS:
        raise ServiceEnvironmentError("environment configuration is invalid")
    return values


def parse_service_environment(content: bytes) -> ServiceEnvironment:
    """Parse only the exact version-one contract, fail-closed."""
    values = _values(content)
    if set(values) != _CURRENT_KEYS or values.get("AGENT_WORK_ENV_VERSION") != "1":
        raise ServiceEnvironmentError("environment configuration is invalid")
    try:
        return ServiceEnvironment.model_validate(
            {field: values[name] for name, field in _MANAGED.items() | _OWNED.items()}
        )
    except ValidationError as error:
        raise ServiceEnvironmentError("environment configuration is invalid") from error


def _legacy_configuration(
    values: dict[str, str], resolve: Callable[[str], str | None], checkout: Path
) -> ServiceEnvironment:
    allowed = _CURRENT_KEYS - {"AGENT_WORK_ENV_VERSION"}
    if set(values) - allowed:
        raise ServiceEnvironmentError("environment configuration is invalid")
    required = allowed - {"AGENT_WORK_TIMEOUT"}
    if not required.issubset(values):
        raise ServiceEnvironmentError("environment configuration is invalid")
    executable_names = {
        "AGENT_WORK_GIT": "git",
        "AGENT_WORK_GLAB": "glab",
        "AGENT_WORK_JQ": "jq",
        "AGENT_WORK_FLOCK": "flock",
        "AGENT_WORK_TIMEOUT": "timeout",
        "AGENT_WORK_UV": "uv",
        "AGENT_WORK_DIRENV": "direnv",
        "AGENT_WORK_AGENT_PATH": values["AGENT_WORK_AGENT"],
    }
    resolved = {
        name: resolve(executable) for name, executable in executable_names.items()
    }
    if any(not path or not Path(path).is_absolute() for path in resolved.values()):
        raise ServiceEnvironmentError("environment configuration is invalid")
    candidate = values | {"AGENT_WORK_ENV_VERSION": "1"} | resolved
    candidate["AGENT_WORK_CHECKOUT"] = str(checkout)
    try:
        return ServiceEnvironment.model_validate(
            {
                field: candidate[name]
                for name, field in _MANAGED.items() | _OWNED.items()
            }
        )
    except ValidationError as error:
        raise ServiceEnvironmentError("environment configuration is invalid") from error


def _render(configuration: ServiceEnvironment) -> bytes:
    fields = _MANAGED | _OWNED
    return b"".join(
        f"{name}={shlex.quote(str(getattr(configuration, field)))}\n".encode()
        for name, field in fields.items()
    )


def _report(**values: Any) -> ServiceEnvironmentReport:
    return ServiceEnvironmentReport(**values)


def migrate_service_environment(
    *,
    environment_file: Path,
    unit_name: str,
    apply: bool,
    dependencies: ServiceEnvironmentDependencies,
) -> ServiceEnvironmentReport:
    """Inspect or migrate one installed environment without rebinding identity."""
    try:
        validate_service_unit_name(unit_name)
        original = dependencies.read_bytes(environment_file)
        values = _values(original)
        current = (
            set(values) == _CURRENT_KEYS and values.get("AGENT_WORK_ENV_VERSION") == "1"
        )
        configuration = (
            parse_service_environment(original)
            if current
            else _legacy_configuration(
                values, dependencies.resolve_executable, dependencies.checkout
            )
        )
    except (CredentialRotationError, OSError, RuntimeError, ServiceEnvironmentError):
        return _report(
            applied=apply,
            outcome="correction-needed",
            next_action="Correct the environment configuration before retrying.",
        )
    changed = _render(configuration)
    if not apply:
        return _report(
            applied=False,
            outcome="current" if current else "stale",
            next_action="Review the migration result before applying it.",
        )
    timer = service = False
    try:
        rotation = RotationDependencies(
            dependencies.read_bytes,
            dependencies.atomic_write,
            dependencies.run_command,
            dependencies.environment,
        )
        stopped = stop_service_units(rotation, unit_name)
        timer, service = stopped.timer_stopped, stopped.service_stopped
        username = verify_service_safeguards(
            rotation,
            RotationEnvironment.model_validate(
                configuration.model_dump(exclude={"version"})
            ),
        )
        if username != configuration.factory_username:
            raise CredentialRotationError("identity mismatch")
    except ServiceUnitStopError as error:
        return _report(
            applied=True,
            outcome="correction-needed",
            timer_stopped=error.state.timer_stopped,
            service_stopped=error.state.service_stopped,
            next_action="Keep any stopped unit stopped; correct the service state and retry.",
        )
    except (CredentialRotationError, OSError, RuntimeError):
        return _report(
            applied=True,
            outcome="correction-needed",
            timer_stopped=timer,
            service_stopped=service,
            next_action="Keep the timer and service stopped; correct safeguards and retry.",
        )
    updated = not current and changed != original
    if updated:
        try:
            dependencies.atomic_write(environment_file, changed)
        except (OSError, InterruptedError):
            try:
                dependencies.atomic_write(environment_file, original)
            except (OSError, InterruptedError):
                return _report(
                    applied=True,
                    outcome="correction-needed",
                    config_updated=True,
                    timer_stopped=timer,
                    service_stopped=service,
                    next_action="Keep the timer and service stopped; corrective review is required before retrying.",
                )
            return _report(
                applied=True,
                outcome="correction-needed",
                rolled_back=True,
                timer_stopped=timer,
                service_stopped=service,
                next_action="Keep the timer and service stopped; review the recoverable configuration before retrying.",
            )
    preview_environment = service_baseline(dependencies.environment) | {
        name: str(getattr(configuration, field))
        for name, field in _MANAGED.items() | _OWNED.items()
    }
    preview = (
        str(configuration.checkout / "scripts/agent-work-systemd-run.sh"),
        "--validate-service-environment",
    )
    try:
        if (
            dependencies.run_command(
                preview, timeout_seconds=120, environment=preview_environment
            ).returncode
            != 0
        ):
            raise OSError("preview")
    except (OSError, RuntimeError):
        if updated:
            try:
                dependencies.atomic_write(environment_file, original)
            except (OSError, InterruptedError):
                return _report(
                    applied=True,
                    outcome="correction-needed",
                    config_updated=True,
                    timer_stopped=timer,
                    service_stopped=service,
                    next_action="Keep the timer and service stopped; review the recoverable configuration before retrying.",
                )
            return _report(
                applied=True,
                outcome="rolled-back",
                config_updated=True,
                rolled_back=True,
                timer_stopped=timer,
                service_stopped=service,
                next_action="Keep the timer and service stopped; correct preview validation before retrying.",
            )
        return _report(
            applied=True,
            outcome="correction-needed",
            timer_stopped=timer,
            service_stopped=service,
            next_action="Keep the timer and service stopped; correct preview validation before retrying.",
        )
    return _report(
        applied=True,
        outcome="verified",
        config_updated=updated,
        timer_stopped=timer,
        service_stopped=service,
        next_action="Review the migration result before enabling the timer.",
    )
