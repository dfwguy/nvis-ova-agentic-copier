# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Compile and verify one host-owned read-only actor session before its turn."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    TypeAdapter,
    ValidationError,
)

from .actor_frontend import (
    ActorFrontendFactory,
    ActorHostError,
    ActorProcessSpec,
    ActorTransportLimits,
)
from .ports import CommandRunner

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
FEATURES = dict.fromkeys(("shell_tool", "unified_exec", "code_mode_host"), False)


@dataclass(frozen=True)
class ProtectedPathSnapshot:
    """Finite lexical/canonical protected-path metadata, with explicit absence."""

    lexical_path: Path
    canonical_path: Path
    device: int | None
    inode: int | None
    mode: int | None
    symlink_target: str | None
    present: bool


@dataclass(frozen=True)
class ActorBinding:
    """Controller-owned candidate, private roots and candidate service recipe."""

    role: str
    issue_iid: int
    candidate_tree: str
    checkout: Path
    scoped_home: Path
    trusted_checkout: Path
    source_home: Path
    controller_state_root: Path
    executable: str
    candidate_socket: Path
    nonce: str
    candidate_command: tuple[str, ...]


@dataclass(frozen=True)
class ActorPermissionPlan:
    """Immutable profile and exact configuration shared by audits and actor."""

    binding: ActorBinding
    profile_id: str
    fingerprint: str
    protected_paths: tuple[ProtectedPathSnapshot, ...]
    configuration_tokens: tuple[str, ...]


@dataclass(frozen=True)
class ActorHostPorts:
    """Explicit audit, metadata, deadline and frontend boundaries."""

    snapshot_protected: Callable[[tuple[Path, ...]], tuple[ProtectedPathSnapshot, ...]]
    run_command: CommandRunner
    clock: Callable[[], float]
    frontend_factory: ActorFrontendFactory


@dataclass(frozen=True)
class ActorResult:
    """Safe output is exposed only after completion and owned cleanup."""

    returncode: int | None
    stdout: str
    stderr: str


class _Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ActorNetworkConfig(_Edge):
    """A closed network permission projection."""

    enabled: StrictBool


class ActorPermissionConfig(_Edge):
    """Exact filesystem permissions with no inherited authority."""

    filesystem: dict[StrictStr, Literal["read", "write", "deny"]]
    network: ActorNetworkConfig
    extends: StrictStr | None = None


class ActorMcpToolConfig(_Edge):
    """The existing candidate tool approval mode, without extra grants."""

    approval_mode: Literal["approve"]


class ActorCandidateMcpConfig(_Edge):
    """Complete trusted candidate service recipe and role tool allowlist."""

    command: StrictStr
    args: tuple[StrictStr, ...]
    required: StrictBool
    env_vars: tuple[StrictStr, ...]
    enabled_tools: tuple[StrictStr, ...]
    tools: dict[StrictStr, ActorMcpToolConfig]


class ActorEffectiveConfigEvidence(_Edge):
    """Complete selected security configuration; unknown capability keys deny."""

    default_permissions: StrictStr
    permissions: dict[StrictStr, ActorPermissionConfig]
    features: dict[StrictStr, StrictBool]
    mcp_servers: dict[StrictStr, ActorCandidateMcpConfig]
    approval_policy: Literal["never"]


class _FileLayer(_Edge):
    type: Literal["packagedDefaults", "system", "legacyManagedConfigTomlFromFile"]
    file: StrictStr


class _UserLayer(_Edge):
    type: Literal["user"]
    file: StrictStr
    profile: StrictStr | None


class _ProjectLayer(_Edge):
    type: Literal["project"]
    dotCodexFolder: StrictStr


class _SessionLayer(_Edge):
    type: Literal["sessionFlags", "legacyManagedConfigTomlFromMdm"]


class _MdmLayer(_Edge):
    type: Literal["mdm"]
    domain: StrictStr
    key: StrictStr


class _EnterpriseLayer(_Edge):
    type: Literal["enterpriseManaged"]
    id: StrictStr
    name: StrictStr


ActorConfigLayerSource = Annotated[
    _FileLayer
    | _UserLayer
    | _ProjectLayer
    | _SessionLayer
    | _MdmLayer
    | _EnterpriseLayer,
    Field(discriminator="type"),
]


class ActorConfigOriginEvidence(_Edge):
    """Strict same-session security origin identity."""

    name: ActorConfigLayerSource
    version: StrictStr


class ActorConfigLayerEvidence(ActorConfigOriginEvidence):
    """The complete layer configuration, not just its origin label."""

    config: dict[str, object]
    disabledReason: StrictStr | None


class ActorConfigReadEvidence(_Edge):
    """Complete config plus its actual layer and key origins."""

    config: ActorEffectiveConfigEvidence
    layers: tuple[ActorConfigLayerEvidence, ...]
    origins: dict[StrictStr, ActorConfigOriginEvidence]


class ActorPermissionProfileEvidence(_Edge):
    """A noninherited named active permission profile."""

    id: StrictStr
    extends: StrictStr | None


class ActorThreadIdentity(_Edge):
    """Correlated thread identity from the current process."""

    id: StrictStr
    sessionId: StrictStr | None
    cwd: StrictStr


class ActorThreadEvidence(_Edge):
    """Thread response security fields, independent of its profile label."""

    thread: ActorThreadIdentity
    activePermissionProfile: ActorPermissionProfileEvidence
    approvalPolicy: Literal["never"]
    approvalsReviewer: Literal["user"]
    cwd: StrictStr
    model: StrictStr
    modelProvider: StrictStr
    runtimeWorkspaceRoots: tuple[StrictStr, ...]
    sandbox: dict[str, object]


class ActorTurnError(_Edge):
    """Private terminal error fields, never rendered."""

    message: StrictStr
    additionalDetails: StrictStr | None = None
    codexErrorInfo: object | None = None
    misalignment: object | None = None


class ActorTurnIdentity(_Edge):
    """A same-session turn identity and explicit lifecycle status."""

    id: StrictStr
    status: Literal["inProgress", "completed", "interrupted", "failed"]
    items: tuple[object, ...]
    error: ActorTurnError | None = None


class ActorTurnEvidence(_Edge):
    """The turn/start response."""

    turn: ActorTurnIdentity


class _Delta(_Edge):
    threadId: StrictStr
    turnId: StrictStr
    itemId: StrictStr
    delta: StrictStr


class ActorMessageDeltaEvent(_Edge):
    """Only bounded assistant text from the expected thread/turn/item."""

    method: Literal["item/agentMessage/delta"]
    params: _Delta


class _Completed(_Edge):
    threadId: StrictStr
    turn: ActorTurnIdentity


class ActorTurnCompletedEvent(_Edge):
    """A correlated terminal notification."""

    method: Literal["turn/completed"]
    params: _Completed


ActorEvent = Annotated[
    ActorMessageDeltaEvent | ActorTurnCompletedEvent, Field(discriminator="method")
]


class _PrivacyEvidence(_Edge):
    write_denied: StrictBool
    network_denied: StrictBool
    auth_unreadable: StrictBool
    source_auth_unreadable: StrictBool
    controller_state_unreadable: StrictBool


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate security field")
        values[key] = value
    return values


def build_actor_permission_plan(
    binding: ActorBinding, protected_paths: tuple[ProtectedPathSnapshot, ...]
) -> ActorPermissionPlan:
    """Compile exact immutable denials and existing closed candidate tools."""
    expected_recipe = (
        "run",
        "--project",
        str(binding.trusted_checkout),
        "--frozen",
        "python",
        "-I",
        "-m",
        "agent_work.mcp_server",
    )
    paths = (
        binding.checkout,
        binding.scoped_home,
        binding.trusted_checkout,
        binding.source_home,
        binding.controller_state_root,
        binding.candidate_socket,
    )
    if (
        binding.role not in ROLE_TOOLS
        or type(binding.issue_iid) is not int
        or binding.issue_iid <= 0
        or re.fullmatch(r"[0-9a-f]{40}", binding.candidate_tree) is None
        or not all(path.is_absolute() and ".." not in path.parts for path in paths)
        or not Path(binding.executable).is_absolute()
        or not binding.nonce
        or len(binding.candidate_command) != 9
        or not Path(binding.candidate_command[0]).is_absolute()
        or binding.candidate_command[1:] != expected_recipe
    ):
        raise ActorHostError("actor_plan_invalid")
    covered = {entry.lexical_path for entry in protected_paths}
    required = {
        binding.scoped_home / "auth.json",
        binding.source_home / "auth.json",
        binding.controller_state_root,
    }
    if not required <= covered or len(covered) != len(protected_paths):
        raise ActorHostError("actor_plan_invalid")
    filesystem = {":root": "read"}
    protected_identity = []
    for entry in protected_paths:
        if (
            not entry.lexical_path.is_absolute()
            or not entry.canonical_path.is_absolute()
            or ".." in entry.lexical_path.parts
            or ".." in entry.canonical_path.parts
            or type(entry.present) is not bool
            or (
                entry.present
                and any(
                    type(value) is not int
                    for value in (entry.device, entry.inode, entry.mode)
                )
            )
            or (
                not entry.present
                and any(
                    value is not None
                    for value in (
                        entry.device,
                        entry.inode,
                        entry.mode,
                        entry.symlink_target,
                    )
                )
            )
        ):
            raise ActorHostError("actor_plan_invalid")
        for path in (entry.lexical_path, entry.canonical_path):
            text = str(path)
            if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in text):
                raise ActorHostError("actor_plan_invalid")
            filesystem[text] = "deny"
        protected_identity.append(
            (
                str(entry.lexical_path),
                str(entry.canonical_path),
                entry.device,
                entry.inode,
                entry.mode,
                entry.symlink_target,
                entry.present,
            )
        )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "binding": (
                    binding.role,
                    binding.issue_iid,
                    binding.candidate_tree,
                    *(str(path) for path in paths),
                    binding.executable,
                    binding.candidate_command,
                    binding.nonce,
                ),
                "protected": protected_identity,
                "filesystem": filesystem,
            },
            sort_keys=True,
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    profile_id = "agent-work-" + fingerprint
    tools = ROLE_TOOLS[binding.role]
    assignments = [
        "default_permissions=" + json.dumps(profile_id),
        'approval_policy="never"',
        *(f"features.{key}=false" for key in FEATURES),
        *(
            f"permissions.{profile_id}.filesystem.{json.dumps(path, ensure_ascii=False)}={json.dumps(value)}"
            for path, value in filesystem.items()
        ),
        f"permissions.{profile_id}.network.enabled=false",
        "mcp_servers.candidate.command=" + json.dumps(binding.candidate_command[0]),
        "mcp_servers.candidate.args=" + json.dumps(binding.candidate_command[1:]),
        "mcp_servers.candidate.required=true",
        "mcp_servers.candidate.env_vars=" + json.dumps(ENV_NAMES),
        "mcp_servers.candidate.enabled_tools=" + json.dumps(tools),
        *(
            f'mcp_servers.candidate.tools.{tool}.approval_mode="approve"'
            for tool in tools
        ),
    ]
    return ActorPermissionPlan(
        binding,
        profile_id,
        fingerprint,
        protected_paths,
        tuple(token for value in assignments for token in ("-c", value)),
    )


def validate_actor_provenance(
    plan: ActorPermissionPlan,
    config_evidence: Mapping[str, object],
    thread_evidence: Mapping[str, object],
) -> None:
    """Require complete exact config and independent active-thread security."""
    try:
        config = ActorConfigReadEvidence.model_validate_json(
            json.dumps(dict(config_evidence))
        )
        expected = ActorEffectiveConfigEvidence.model_validate_json(
            json.dumps(tomllib.loads("\n".join(plan.configuration_tokens[1::2])))
        )
        if (
            config.config != expected
            or not config.layers
            or config.origins.keys() != expected.model_dump().keys()
        ):
            raise ValueError("security projection mismatch")
        if any(
            origin.name.type != "sessionFlags" or not origin.version
            for origin in config.origins.values()
        ):
            raise ValueError("security origin mismatch")
        selected_layers = [
            layer for layer in config.layers if layer.name.type == "sessionFlags"
        ]
        if (
            len(selected_layers) != 1
            or selected_layers[0].disabledReason is not None
            or not selected_layers[0].version
        ):
            raise ValueError("security layer mismatch")
        layer = ActorEffectiveConfigEvidence.model_validate_json(
            json.dumps(selected_layers[0].config)
        )
        if layer != expected or any(
            origin.version != selected_layers[0].version
            for origin in config.origins.values()
        ):
            raise ValueError("security layer mismatch")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ActorHostError("actor_config_unverified") from None
    if thread_evidence:
        try:
            thread = ActorThreadEvidence.model_validate_json(
                json.dumps(dict(thread_evidence))
            )
            if (
                not thread.thread.id
                or thread.thread.cwd != str(plan.binding.checkout)
                or thread.cwd != str(plan.binding.checkout)
                or thread.runtimeWorkspaceRoots != (str(plan.binding.checkout),)
                or thread.activePermissionProfile.id != plan.profile_id
                or thread.activePermissionProfile.extends is not None
                or thread.sandbox != {"type": "readOnly"}
            ):
                raise ValueError("thread identity mismatch")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ActorHostError("actor_thread_unverified") from None


def _audit(
    plan: ActorPermissionPlan, spec: ActorProcessSpec, ports: ActorHostPorts
) -> None:
    environment = dict(spec.environment)
    try:
        feature = ports.run_command(
            (plan.binding.executable, *plan.configuration_tokens, "features", "list"),
            stdin=None,
            timeout_seconds=max(0.0, spec.deadline - ports.clock()),
            environment=environment,
        )
        rows = [
            line.split()
            for line in feature.stdout.splitlines()
            if line.split()[:1] and line.split()[0] in FEATURES
        ]
        if (
            feature.returncode != 0
            or len(rows) != len(FEATURES)
            or {row[0] for row in rows} != FEATURES.keys()
            or any(len(row) != 3 or row[-1] != "false" for row in rows)
        ):
            raise ValueError("unverified features")
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, TypeError):
        raise ActorHostError("actor_features_unavailable") from None
    try:
        command = (
            plan.binding.executable,
            "sandbox",
            "--permission-profile",
            plan.profile_id,
            "-C",
            str(plan.binding.checkout),
            *plan.configuration_tokens,
            "--",
            sys.executable,
            "-I",
            "-c",
            READ_ONLY_SANDBOX_PROBE,
            str(
                plan.binding.checkout
                / (plan.binding.scoped_home.parent.name + "-write-probe")
            ),
            str(plan.binding.scoped_home / "auth.json"),
            str(plan.binding.source_home / "auth.json"),
            str(plan.binding.controller_state_root),
        )
        result = ports.run_command(
            command,
            stdin=None,
            timeout_seconds=max(0.0, spec.deadline - ports.clock()),
            environment=environment,
        )
        if result.returncode != 0:
            raise ValueError("privacy probe failed")
        evidence = _PrivacyEvidence.model_validate(
            json.loads(result.stdout, object_pairs_hook=_unique_fields)
        )
        if not all(evidence.model_dump().values()):
            raise ValueError("privacy unverified")
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError, TypeError):
        raise ActorHostError("actor_privacy_unavailable") from None


def run_actor_session(
    plan: ActorPermissionPlan,
    prompt: str,
    *,
    spec: ActorProcessSpec,
    limits: ActorTransportLimits,
    ports: ActorHostPorts,
) -> ActorResult:
    """Audit then verify same-session provenance twice before one bounded turn."""
    output = ""
    try:
        if (
            spec.argv
            != (
                plan.binding.executable,
                "app-server",
                "--stdio",
                "--strict-config",
                *plan.configuration_tokens,
            )
            or spec.cwd != plan.binding.checkout
            or len(dict(spec.environment)) != len(spec.environment)
            or dict(spec.environment).get("CODEX_HOME") != str(plan.binding.scoped_home)
            or any(
                dict(spec.environment).get(name) != value
                for name, value in (
                    ("AGENT_WORK_CANDIDATE_ROLE", plan.binding.role),
                    ("AGENT_WORK_CANDIDATE_SOCKET", str(plan.binding.candidate_socket)),
                    ("AGENT_WORK_CANDIDATE_NONCE", plan.binding.nonce),
                    ("AGENT_WORK_CANDIDATE_ISSUE_IID", str(plan.binding.issue_iid)),
                    ("AGENT_WORK_CANDIDATE_TREE", plan.binding.candidate_tree),
                )
            )
        ):
            raise ActorHostError("actor_plan_invalid")
        _audit(plan, spec, ports)
        if ports.clock() >= spec.deadline:
            raise ActorHostError("actor_timeout")
        with ports.frontend_factory(spec, limits) as session:
            session.request(
                "initialize",
                {
                    "clientInfo": {"name": "agent-work", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            session.notify_initialized()
            parameters: dict[str, object] = {
                "cwd": str(plan.binding.checkout),
                "includeLayers": True,
            }
            config = session.request("config/read", parameters)
            validate_actor_provenance(plan, config, {})
            thread = session.request(
                "thread/start",
                {
                    "cwd": str(plan.binding.checkout),
                    "approvalPolicy": "never",
                    "permissions": plan.profile_id,
                },
            )
            validate_actor_provenance(plan, config, thread)
            config = session.request("config/read", parameters)
            validate_actor_provenance(plan, config, thread)
            current = ports.snapshot_protected(
                tuple(entry.lexical_path for entry in plan.protected_paths)
            )
            if current != plan.protected_paths:
                raise ActorHostError("actor_identity_changed")
            thread_identity = ActorThreadEvidence.model_validate_json(
                json.dumps(dict(thread))
            ).thread.id
            try:
                response = ActorTurnEvidence.model_validate_json(
                    json.dumps(
                        dict(
                            session.request(
                                "turn/start",
                                {
                                    "threadId": thread_identity,
                                    "permissions": plan.profile_id,
                                    "input": [{"type": "text", "text": prompt}],
                                },
                            )
                        )
                    )
                )
            except ValidationError:
                raise ActorHostError("actor_turn_failed") from None
            turn_id = response.turn.id
            if (
                not turn_id
                or response.turn.status != "inProgress"
                or response.turn.error is not None
            ):
                raise ActorHostError("actor_turn_failed")
            item_id: str | None = None
            while True:
                raw = session.next_event()
                try:
                    event = TypeAdapter(ActorEvent).validate_json(json.dumps(dict(raw)))
                except ValueError:
                    raise ActorHostError("actor_protocol_invalid") from None
                if isinstance(event, ActorTurnCompletedEvent):
                    if (
                        event.params.threadId != thread_identity
                        or event.params.turn.id != turn_id
                        or event.params.turn.status != "completed"
                        or event.params.turn.error is not None
                    ):
                        raise ActorHostError("actor_turn_failed")
                    break
                if (
                    event.params.threadId != thread_identity
                    or event.params.turnId != turn_id
                    or not event.params.itemId
                    or (item_id is not None and item_id != event.params.itemId)
                ):
                    raise ActorHostError("actor_turn_failed")
                item_id = event.params.itemId
                output += event.params.delta
                try:
                    size = len(output.encode("utf-8", errors="strict"))
                except UnicodeError:
                    raise ActorHostError("actor_protocol_invalid") from None
                if size > limits.max_output_bytes:
                    raise ActorHostError("actor_protocol_invalid")
    except ActorHostError as error:
        return ActorResult(None, "", error.reason)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ActorResult(None, "", "actor_frontend_unavailable")
    return ActorResult(0, output, "")


READ_ONLY_SANDBOX_PROBE = """\
import errno
import json
import os
import socket
import sys
from pathlib import Path

DENIED = {errno.EACCES, errno.EPERM}

def descriptor_denied(path: Path, flags: int) -> bool:
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        return error.errno in DENIED
    os.close(descriptor)
    if flags & os.O_CREAT:
        Path(path).unlink()
    return False

def listing_denied(path: Path) -> bool:
    try:
        os.listdir(path)
    except OSError as error:
        return error.errno in DENIED
    return False

def network_denied() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(("192.0.2.1", 9))
    except OSError as error:
        return error.errno in DENIED
    return False

write_path, auth_path, source_auth_path, state_root = map(Path, sys.argv[1:])
state_listing_denied = listing_denied(state_root)
state_file_denied = descriptor_denied(state_root / "project.lock", os.O_RDONLY)
print(json.dumps({
    "write_denied": descriptor_denied(write_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL),
    "network_denied": network_denied(),
    "auth_unreadable": descriptor_denied(auth_path, os.O_RDONLY),
    "source_auth_unreadable": descriptor_denied(source_auth_path, os.O_RDONLY),
    "controller_state_unreadable": state_listing_denied and state_file_denied,
}))
"""
