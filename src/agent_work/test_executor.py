# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Host-owned Red execution policy and reference evidence, without an OS backend."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from pydantic import StrictBool, StrictInt, StrictStr

from .models import BoundaryModel
from .ports import CommandResult
from .red_checkpoint import validate_pytest_targets, validate_test_manifest

_REASON = Literal[
    "invalid_identity",
    "identity_changed",
    "unavailable",
    "sandbox_or_probe_failed",
    "invalid_probe",
    "asset_changed",
    "input_descriptor_failure",
    "execution_failed",
    "output_invalid",
    "timeout",
    "descendants_survived",
    "cleanup_failed",
    "unsupported_hook",
    "policy_binding_failed",
]
_MAX_OUTPUT_BYTES = 262144
_CATEGORIES = ("auth", "source_auth", "controller", "git_admin", "sibling")


class TestExecutionError(RuntimeError):
    """A closed, sanitized refusal; it cannot be retained as a failing Red result."""

    def __init__(self, reason: _REASON, *, cause_proven: bool = False) -> None:
        if reason not in get_args(_REASON):
            reason = "unavailable"
        super().__init__(reason)
        self.reason = reason
        self.cause_proven = cause_proven is True


def _absolute(path: Path) -> bool:
    return path.is_absolute() and ".." not in path.parts


@dataclass(frozen=True)
class TestProtectedPaths:
    """Five distinct normalized known-file paths declared by the trusted host."""

    auth: Path
    source_auth: Path
    controller: Path
    git_admin: Path
    sibling: Path

    def __post_init__(self) -> None:
        paths = self.paths()
        if not all(_absolute(path) for path in paths) or len(set(paths)) != 5:
            raise ValueError("protected manifest is invalid")

    def paths(self) -> tuple[Path, ...]:
        """Return the pinned category order without filesystem resolution."""
        return (
            self.auth,
            self.source_auth,
            self.controller,
            self.git_admin,
            self.sibling,
        )

    def digest(self) -> str:
        """Hash the exact canonical named-path manifest, without reading its files."""
        encoded = json.dumps(
            list(zip(_CATEGORIES, map(str, self.paths()), strict=True)),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TestExecutionIdentity:
    """Immutable observed controller and candidate identity, never a host grant."""

    project_id: int
    issue_iid: int
    ledger_revision: int
    source_branch: str
    base_sha: str
    candidate_tree: str
    test_paths: tuple[str, ...]
    nonce: str

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or value <= 0
                for value in (self.project_id, self.issue_iid, self.ledger_revision)
            )
            or not self.source_branch
            or len(self.source_branch) > 255
            or any(char.isspace() or ord(char) < 32 for char in self.source_branch)
            or not re.fullmatch(r"[0-9a-f]{40}", self.base_sha)
            or not re.fullmatch(r"[0-9a-f]{40}", self.candidate_tree)
            or not re.fullmatch(r"[0-9a-f]{32}", self.nonce)
            or not 1 <= len(self.test_paths) <= 64
            or tuple(sorted(set(self.test_paths))) != self.test_paths
        ):
            raise ValueError("execution identity is invalid")
        validate_test_manifest(self.test_paths)


@dataclass(frozen=True)
class TestExecutionRequest:
    """One bound, shell-free Red request for controller-declared test targets."""

    identity: TestExecutionIdentity
    worktree: Path
    argv: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _absolute(self.worktree):
            raise ValueError("execution request is invalid")
        validate_pytest_targets(self.argv, self.identity.test_paths)


@dataclass(frozen=True)
class OpenedTestAsset:
    """Host-attested opened-file identity and content hash, not a path-prefix grant."""

    path: Path
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class PreparedTestExecution:
    """One host-prepared projection and declared runtime for a bound invocation."""

    identity: TestExecutionIdentity
    candidate_assets: tuple[OpenedTestAsset, ...]
    runtime_assets: tuple[OpenedTestAsset, ...]
    runtime_fingerprint: str
    private_scratch: Path
    probe_paths: tuple[Path, ...]
    parent_net_namespace: str


@dataclass(frozen=True)
class TestExecutorPorts:
    """Explicit trusted backend lifecycle boundaries; this package supplies none."""

    nonce: Callable[[], str]
    prepare: Callable[[TestExecutionRequest], PreparedTestExecution]
    probe: Callable[[PreparedTestExecution], CommandResult]
    execute: Callable[[PreparedTestExecution], CommandResult]
    attest_assets: Callable[[PreparedTestExecution], bool]
    close_inputs: Callable[[PreparedTestExecution], None]
    stop_and_reap: Callable[[PreparedTestExecution], bool]
    cleanup: Callable[[PreparedTestExecution], bool]


class _ProtectedProof(BoundaryModel):
    auth: StrictBool
    source_auth: StrictBool
    controller: StrictBool
    git_admin: StrictBool
    sibling: StrictBool


class _ProbeProof(BoundaryModel):
    schema_version: StrictInt
    nonce: StrictStr
    candidate_tree: StrictStr
    runtime_fingerprint: StrictStr
    private_reads_denied: StrictBool
    candidate_writes_denied: StrictBool
    scratch_write_allowed: StrictBool
    network_isolated: StrictBool
    privileged_env_absent: StrictBool
    privileged_fds_absent: StrictBool
    descendants_denied: StrictBool
    protected_categories_denied: _ProtectedProof
    probe_manifest_digest: StrictStr


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate probe key")
        result[key] = value
    return result


def parse_test_execution_probe(
    output: str,
    identity: TestExecutionIdentity,
    runtime_fingerprint: str,
    *,
    protected_paths: TestProtectedPaths | None = None,
) -> bool:
    """Require strict, complete, duplicate-free proof bound to the host manifest."""
    if protected_paths is None or not isinstance(output, str):
        return False
    try:
        if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            return False
        data = _ProbeProof.model_validate(
            json.loads(output, object_pairs_hook=_unique_pairs)
        )
    except (ValueError, UnicodeError):
        return False
    flags = (
        data.private_reads_denied,
        data.candidate_writes_denied,
        data.scratch_write_allowed,
        data.network_isolated,
        data.privileged_env_absent,
        data.privileged_fds_absent,
        data.descendants_denied,
    )
    return (
        data.schema_version == 1
        and data.nonce == identity.nonce
        and data.candidate_tree == identity.candidate_tree
        and data.runtime_fingerprint == runtime_fingerprint
        and data.probe_manifest_digest == protected_paths.digest()
        and all(flags)
        and all(data.protected_categories_denied.model_dump().values())
    )


@dataclass(frozen=True, repr=False)
class TestExecutionPolicy:
    """Host-owned one-use execution lifecycle; no default confinement implementation."""

    _ports: TestExecutorPorts
    _protected_paths: TestProtectedPaths
    _used_nonces: set[str] = field(default_factory=set, init=False, repr=False)

    def fresh_nonce(self) -> str:
        """Ask the trusted host for a fresh executor nonce, separate from broker data."""
        try:
            value = self._ports.nonce()
        except (OSError, RuntimeError) as error:
            raise TestExecutionError("unavailable") from error
        if not re.fullmatch(r"[0-9a-f]{32}", value) or value in self._used_nonces:
            raise TestExecutionError("invalid_identity")
        return value

    def require_native_hook(self, operation: str) -> None:
        """Refuse native hook paths until an equivalent confined adapter exists."""
        del operation
        raise TestExecutionError("unsupported_hook")

    def _attest(self, prepared: PreparedTestExecution) -> None:
        assets = (*prepared.candidate_assets, *prepared.runtime_assets)
        if (
            not prepared.candidate_assets
            or not prepared.runtime_assets
            or any(
                not _absolute(asset.path)
                or asset.device < 0
                or asset.inode <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
                for asset in assets
            )
            or len({asset.path for asset in assets}) != len(assets)
            or self._ports.attest_assets(prepared) is not True
        ):
            raise TestExecutionError("asset_changed")

    def _run(
        self, request: TestExecutionRequest, prepared: PreparedTestExecution
    ) -> CommandResult:
        if prepared.identity != request.identity:
            raise TestExecutionError("identity_changed")
        if (
            prepared.probe_paths != self._protected_paths.paths()
            or not _absolute(prepared.private_scratch)
            or not prepared.parent_net_namespace
            or not re.fullmatch(r"[0-9a-f]{64}", prepared.runtime_fingerprint)
        ):
            raise TestExecutionError("invalid_probe")
        if any(
            not asset.path.is_relative_to(request.worktree)
            or "template" in asset.path.relative_to(request.worktree).parts
            for asset in prepared.candidate_assets
        ):
            raise TestExecutionError("asset_changed")
        self._attest(prepared)
        try:
            probe = self._ports.probe(prepared)
        except (
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            UnicodeError,
        ) as error:
            raise TestExecutionError("sandbox_or_probe_failed") from error
        if type(probe.returncode) is not int:
            raise TestExecutionError("invalid_probe")
        if probe.returncode != 0:
            raise TestExecutionError("sandbox_or_probe_failed")
        try:
            if any(
                not isinstance(text, str)
                or len(text.encode("utf-8")) > _MAX_OUTPUT_BYTES
                for text in (probe.stdout, probe.stderr)
            ):
                raise TestExecutionError("invalid_probe")
        except UnicodeError as error:
            raise TestExecutionError("invalid_probe") from error
        if not parse_test_execution_probe(
            probe.stdout,
            request.identity,
            prepared.runtime_fingerprint,
            protected_paths=self._protected_paths,
        ):
            raise TestExecutionError("invalid_probe")
        self._attest(prepared)
        try:
            self._ports.close_inputs(prepared)
        except (OSError, RuntimeError) as error:
            raise TestExecutionError("input_descriptor_failure") from error
        try:
            result = self._ports.execute(prepared)
        except subprocess.TimeoutExpired as error:
            raise TestExecutionError("timeout") from error
        except UnicodeError as error:
            raise TestExecutionError("output_invalid") from error
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise TestExecutionError("execution_failed") from error
        try:
            if (
                any(
                    not isinstance(text, str)
                    or len(text.encode("utf-8")) > _MAX_OUTPUT_BYTES
                    for text in (result.stdout, result.stderr)
                )
                or type(result.returncode) is not int
            ):
                raise TestExecutionError("output_invalid")
        except UnicodeError as error:
            raise TestExecutionError("output_invalid") from error
        return result

    def execute_red(self, request: TestExecutionRequest) -> CommandResult:
        """Return only after fresh proof, integrity, input close, reaping and cleanup."""
        if request.identity.nonce in self._used_nonces:
            raise TestExecutionError("invalid_identity")
        self._used_nonces.add(request.identity.nonce)
        try:
            prepared = self._ports.prepare(request)
        except TestExecutionError:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise TestExecutionError("unavailable") from error
        result: CommandResult | None = None
        try:
            try:
                result = self._run(request, prepared)
            except TestExecutionError:
                raise
            except (OSError, RuntimeError, ValueError) as error:
                raise TestExecutionError("asset_changed") from error
        finally:
            try:
                reaped = self._ports.stop_and_reap(prepared) is True
            except (OSError, RuntimeError):
                reaped = False
            try:
                if not reaped:
                    raise TestExecutionError("descendants_survived")
                if result is not None:
                    try:
                        self._attest(prepared)
                    except (OSError, RuntimeError, ValueError) as error:
                        raise TestExecutionError("asset_changed") from error
            finally:
                try:
                    cleaned = self._ports.cleanup(prepared) is True
                except (OSError, RuntimeError):
                    cleaned = False
                if not cleaned:
                    raise TestExecutionError("cleanup_failed")
        assert result is not None
        return result


def create_test_execution_policy(
    ports: TestExecutorPorts,
    *,
    protected_paths: TestProtectedPaths,
) -> TestExecutionPolicy:
    """Create a policy only from explicit trusted host ports and protected paths."""
    return TestExecutionPolicy(ports, protected_paths)


_REFERENCE_PROBE = r"""
import errno
import json
import os
import subprocess
import sys

settings = json.loads(sys.argv[1])

def denied_open(path: str, flags: int) -> bool:
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        return error.errno in (errno.EACCES, errno.EPERM)
    else:
        os.close(descriptor)
        return False

categories = {name: denied_open(path, os.O_RDONLY) for name, path in settings["protected"]}
private_denied = all(categories.values())
candidate_path = os.path.join(os.getcwd(), settings["test_path"])
candidate_denied = denied_open(candidate_path, os.O_WRONLY | os.O_CREAT)
scratch_path = os.path.join(settings["scratch"], "probe-write")
try:
    descriptor = os.open(scratch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    os.unlink(scratch_path)
    scratch_allowed = True
except OSError:
    scratch_allowed = False
try:
    network = (os.readlink("/proc/self/ns/net") != settings["parent_net"]
               and set(os.listdir("/sys/class/net")) <= {"lo"})
except OSError:
    network = False
privileged_environment = not any(
    key.startswith(("GITLAB_", "GIT_CONFIG", "AGENT_WORK_", "AWS_", "AZURE_"))
    or key in {"SSH_AUTH_SOCK", "GITHUB_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"}
    for key in os.environ
)
try:
    descriptors = os.listdir("/proc/self/fd")
    privileged_descriptors = all(value in {"0", "1", "2"} for value in descriptors)
except OSError:
    privileged_descriptors = False
payload = {
    "schema_version": 1,
    "nonce": settings["nonce"],
    "candidate_tree": settings["tree"],
    "runtime_fingerprint": settings["runtime"],
    "private_reads_denied": private_denied,
    "candidate_writes_denied": candidate_denied,
    "scratch_write_allowed": scratch_allowed,
    "network_isolated": network,
    "privileged_env_absent": privileged_environment,
    "privileged_fds_absent": privileged_descriptors,
    "protected_categories_denied": categories,
    "probe_manifest_digest": settings["manifest_digest"],
}
fields = ("private_reads_denied", "candidate_writes_denied", "scratch_write_allowed",
          "network_isolated", "privileged_env_absent", "privileged_fds_absent")
if len(sys.argv) > 2 and sys.argv[2] == "child":
    payload["descendants_denied"] = all(payload[name] is True for name in fields)
else:
    try:
        script = "SOURCE = " + repr(SOURCE) + "\nexec(SOURCE)"
        child = subprocess.run((sys.executable, "-I", "-c", script, sys.argv[1], "child"),
                               capture_output=True, text=True, timeout=120, check=False)
        child_proof = json.loads(child.stdout)
        payload["descendants_denied"] = (
            child.returncode == 0 and len(child.stdout.encode("utf-8")) <= 262144
            and child_proof.keys() == {*payload, "descendants_denied"}
            and child_proof["nonce"] == settings["nonce"]
            and child_proof["candidate_tree"] == settings["tree"]
            and child_proof["runtime_fingerprint"] == settings["runtime"]
            and child_proof["probe_manifest_digest"] == settings["manifest_digest"]
            and child_proof["protected_categories_denied"] == categories
            and all(child_proof[name] is True for name in (*fields, "descendants_denied"))
        )
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        payload["descendants_denied"] = False
sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
"""


def build_test_execution_probe(
    identity: TestExecutionIdentity,
    *,
    probe_paths: tuple[Path, ...],
    scratch: Path,
    parent_net_namespace: str,
    runtime_fingerprint: str,
    protected_paths: TestProtectedPaths,
) -> tuple[str, ...]:
    """Emit a fixed isolated-interpreter reference producer, not a sandbox launcher.

    All named paths are descriptor-opened without consuming private bytes. Child
    evidence comes from repeating the same observations. Missing files are unsafe.
    """
    if (
        probe_paths != protected_paths.paths()
        or not _absolute(scratch)
        or not parent_net_namespace
        or not re.fullmatch(r"[0-9a-f]{64}", runtime_fingerprint)
    ):
        raise TestExecutionError("invalid_probe")
    payload = json.dumps(
        {
            "nonce": identity.nonce,
            "tree": identity.candidate_tree,
            "runtime": runtime_fingerprint,
            "scratch": str(scratch),
            "parent_net": parent_net_namespace,
            "test_path": identity.test_paths[0],
            "protected": list(zip(_CATEGORIES, map(str, probe_paths), strict=True)),
            "manifest_digest": protected_paths.digest(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    script = "SOURCE = " + repr(_REFERENCE_PROBE) + "\nexec(SOURCE)"
    return (sys.executable, "-I", "-c", script, payload)
