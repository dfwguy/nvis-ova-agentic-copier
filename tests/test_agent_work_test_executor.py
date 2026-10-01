# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Host-owned isolated Red execution rejects unverifiable confinement and identity."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_work import runtime
from agent_work.repository import CommandResult

BASE = "a" * 40
TREE = "b" * 40
FINGERPRINT = "c" * 64
NONCE = "9" * 32
PATHS = ("tests/test_example.py",)
DENIAL_FIELDS = (
    "private_reads_denied",
    "candidate_writes_denied",
    "scratch_write_allowed",
    "network_isolated",
    "privileged_env_absent",
    "privileged_fds_absent",
    "descendants_denied",
)


CATEGORIES = ("auth", "source_auth", "controller", "git_admin", "sibling")


def protected_paths(
    root: Path = Path("/synthetic-protected"),
) -> runtime.TestProtectedPaths:
    """Host-owned synthetic manifest covers every required private category."""
    return runtime.TestProtectedPaths(
        auth=root / "auth.json",
        source_auth=root / "source-auth.json",
        controller=root / "controller.json",
        git_admin=root / "git-admin.json",
        sibling=root / "sibling.json",
    )


def protected_tuple(paths: runtime.TestProtectedPaths) -> tuple[Path, ...]:
    return (
        paths.auth,
        paths.source_auth,
        paths.controller,
        paths.git_admin,
        paths.sibling,
    )


def manifest_digest(paths: runtime.TestProtectedPaths) -> str:
    encoded = json.dumps(
        list(zip(CATEGORIES, map(str, protected_tuple(paths)), strict=True)),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def identity() -> runtime.TestExecutionIdentity:
    """Return a fixture controller identity, never an authority grant."""
    return runtime.TestExecutionIdentity(
        project_id=310967,
        issue_iid=387,
        ledger_revision=1,
        source_branch="agent/issue-387",
        base_sha=BASE,
        candidate_tree=TREE,
        test_paths=PATHS,
        nonce=NONCE,
    )


def proof(
    bound: runtime.TestExecutionIdentity,
    *,
    protected: runtime.TestProtectedPaths | None = None,
    changes: Mapping[str, object] | None = None,
    **overrides: object,
) -> str:
    """Build fake host evidence for the public strict parser."""
    manifest = protected or protected_paths()
    fields: dict[str, object] = {
        "schema_version": 1,
        "nonce": bound.nonce,
        "candidate_tree": bound.candidate_tree,
        "runtime_fingerprint": FINGERPRINT,
        **dict.fromkeys(DENIAL_FIELDS, True),
        "protected_categories_denied": dict.fromkeys(CATEGORIES, True),
        "probe_manifest_digest": manifest_digest(manifest),
    }
    if changes is not None:
        fields.update(changes)
    fields.update(overrides)
    return json.dumps(fields)


def valid_proof(
    bound: runtime.TestExecutionIdentity,
    *,
    protected: runtime.TestProtectedPaths | None = None,
) -> str:
    """Establish an accepted exact-bound control before changing one evidence field."""
    manifest = protected or protected_paths()
    text = proof(bound, protected=manifest)
    assert (
        runtime.parse_test_execution_probe(
            text, bound, FINGERPRINT, protected_paths=manifest
        )
        is True
    )
    return text


class FixtureHost:
    """Inject every execution lifecycle boundary without claiming live confinement."""

    def __init__(self, root: Path, *, failure: str = "") -> None:
        self.root = root
        self.failure = failure
        self.events: list[str] = []
        self.prepared_requests: list[runtime.TestExecutionRequest] = []
        self.attestations = 0
        self.inputs_closed = False
        self.reaped = False
        self.cleaned = False
        self.probe_result: CommandResult | None = None
        self.output = CommandResult(1, "FAILED example", "")
        self.asset_path = root / "candidate.py"
        self.asset_path.write_text("synthetic fixture source")
        self.runtime_path = root / "runtime.py"
        self.runtime_path.write_text("synthetic fixture runtime")

    def nonce(self) -> str:
        self.events.append("nonce")
        return NONCE

    def prepare(
        self, request: runtime.TestExecutionRequest
    ) -> runtime.PreparedTestExecution:
        self.events.append("prepare")
        self.prepared_requests.append(request)
        if self.failure == "prepare":
            raise runtime.TestExecutionError("unavailable")

        def asset(path: Path) -> runtime.OpenedTestAsset:
            observed = path.stat()
            return runtime.OpenedTestAsset(
                path=path,
                device=observed.st_dev,
                inode=observed.st_ino,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )

        prepared = runtime.PreparedTestExecution(
            identity=request.identity,
            candidate_assets=(asset(self.asset_path),),
            runtime_assets=(asset(self.runtime_path),),
            runtime_fingerprint=FINGERPRINT,
            private_scratch=self.root / "scratch",
            probe_paths=protected_tuple(protected_paths(self.root)),
            parent_net_namespace="parent-net",
        )
        if self.failure == "prepared-identity":
            return replace(prepared, identity=replace(request.identity, issue_iid=388))
        return prepared

    def probe(self, prepared: runtime.PreparedTestExecution) -> CommandResult:
        self.events.append("probe")
        if self.failure == "probe-exception":
            raise OSError("private stderr must not escape")
        if self.failure == "probe-unknown":
            return CommandResult(1, "", "unattributed host diagnostic")
        if self.failure.startswith("proof-"):
            valid_proof(prepared.identity, protected=protected_paths(self.root))
            return CommandResult(
                0,
                proof(
                    prepared.identity,
                    protected=protected_paths(self.root),
                    changes={self.failure.removeprefix("proof-"): False},
                ),
                "",
            )
        return self.probe_result or CommandResult(
            0, proof(prepared.identity, protected=protected_paths(self.root)), ""
        )

    def execute(self, prepared: runtime.PreparedTestExecution) -> CommandResult:
        assert self.inputs_closed
        self.events.append("execute")
        if self.failure == "timeout":
            raise subprocess.TimeoutExpired("isolated", 1)
        if self.failure == "execute-exception":
            raise OSError("private execution exception")
        if self.failure == "unicode":
            return CommandResult(1, "\ud800", "")
        if self.failure == "output-overflow":
            return CommandResult(1, "x" * 262145, "")
        if self.failure == "runtime-swap-after-payload":
            self.runtime_path.write_text("replacement runtime")
        return self.output

    def attest_assets(self, prepared: runtime.PreparedTestExecution) -> bool:
        self.attestations += 1
        self.events.append("attest")
        if self.failure == f"attest-{self.attestations}":
            return False
        if self.failure == "inode-swap-before-probe" and self.attestations == 1:
            self.asset_path.rename(self.root / "old-candidate")
            self.asset_path.write_text("synthetic fixture source")
        if self.failure == "symlink-swap-after-probe" and self.attestations == 2:
            self.asset_path.unlink()
            self.asset_path.symlink_to(self.runtime_path)
        for asset in (*prepared.candidate_assets, *prepared.runtime_assets):
            observed = asset.path.lstat()
            if (
                asset.path.is_symlink()
                or (observed.st_dev, observed.st_ino) != (asset.device, asset.inode)
                or hashlib.sha256(asset.path.read_bytes()).hexdigest() != asset.sha256
            ):
                return False
        return True

    def close_inputs(self, prepared: runtime.PreparedTestExecution) -> None:
        self.events.append("close")
        if self.failure == "close":
            raise OSError("private descriptor failure")
        self.inputs_closed = True

    def stop_and_reap(self, prepared: runtime.PreparedTestExecution) -> bool:
        self.events.append("reap")
        self.reaped = True
        return self.failure != "surviving-descendant"

    def cleanup(self, prepared: runtime.PreparedTestExecution) -> bool:
        self.events.append("cleanup")
        self.cleaned = True
        return self.failure != "cleanup"

    def ports(self) -> runtime.TestExecutorPorts:
        return runtime.TestExecutorPorts(
            nonce=self.nonce,
            prepare=self.prepare,
            probe=self.probe,
            execute=self.execute,
            attest_assets=self.attest_assets,
            close_inputs=self.close_inputs,
            stop_and_reap=self.stop_and_reap,
            cleanup=self.cleanup,
        )


def request(root: Path) -> runtime.TestExecutionRequest:
    """Return a bounded request matching the fixture manifest."""
    return runtime.TestExecutionRequest(
        identity=identity(), worktree=root, argv=("uv", "run", "pytest", *PATHS)
    )


def test_fake_host_accepts_nonzero_red_only_after_proof_assets_and_cleanup(
    tmp_path: Path,
) -> None:
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    result = policy.execute_red(request(tmp_path))
    assert result == CommandResult(1, "FAILED example", "")
    assert host.inputs_closed and host.reaped and host.cleaned
    assert host.attestations >= 3
    assert host.events.index("close") < host.events.index("execute")
    assert host.events.index("reap") < host.events.index("cleanup")


@pytest.mark.parametrize(
    "failure",
    [
        "prepare",
        "prepared-identity",
        "probe-exception",
        "probe-unknown",
        *["proof-" + field for field in DENIAL_FIELDS],
        "attest-1",
        "attest-2",
        "attest-3",
        "inode-swap-before-probe",
        "symlink-swap-after-probe",
        "runtime-swap-after-payload",
        "close",
        "timeout",
        "execute-exception",
        "unicode",
        "output-overflow",
        "surviving-descendant",
        "cleanup",
    ],
)
def test_every_unproven_lifecycle_or_asset_change_raises_instead_of_red_failure(
    tmp_path: Path, failure: str
) -> None:
    host = FixtureHost(tmp_path, failure=failure)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    with pytest.raises(runtime.TestExecutionError) as error:
        policy.execute_red(request(tmp_path))
    assert "private" not in str(error.value)
    assert error.value.reason in {
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
    }
    if failure.startswith("proof-"):
        assert error.value.reason == "invalid_probe"
        assert "execute" not in host.events
    if failure == "probe-unknown":
        assert error.value.reason == "sandbox_or_probe_failed"
        assert error.value.cause_proven is False
        assert "execute" not in host.events
    if "prepare" in host.events and failure != "prepare":
        assert host.reaped and host.cleaned


@pytest.mark.parametrize("operation", ["judge", "serial-green"])
def test_selected_policy_refuses_native_hooks_without_adapter(
    tmp_path: Path, operation: str
) -> None:
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    with pytest.raises(runtime.TestExecutionError) as error:
        policy.require_native_hook(operation)
    assert error.value.reason == "unsupported_hook"
    assert host.events == []


def test_completed_identity_cannot_replay_executor_context(tmp_path: Path) -> None:
    host = FixtureHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(host.root)
    )
    bound = request(tmp_path)
    policy.execute_red(bound)
    before = list(host.events)
    with pytest.raises(runtime.TestExecutionError):
        policy.execute_red(bound)
    assert host.events == before


@pytest.mark.parametrize("field", DENIAL_FIELDS)
@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_public_probe_parser_requires_each_strict_true_field(
    field: str, value: object
) -> None:
    bound = identity()
    valid_proof(bound)
    assert (
        runtime.parse_test_execution_probe(
            proof(bound, changes={field: value}),
            bound,
            FINGERPRINT,
            protected_paths=protected_paths(),
        )
        is False
    )


@pytest.mark.parametrize("bad_output", ["", "not-json", "{}", "\ud800", "x" * 262145])
def test_malformed_oversized_or_unicode_probe_is_never_authority(
    bad_output: str,
) -> None:
    valid_proof(identity())
    assert (
        runtime.parse_test_execution_probe(
            bad_output, identity(), FINGERPRINT, protected_paths=protected_paths()
        )
        is False
    )


@pytest.mark.parametrize(
    "defect",
    ["extra", "trailing", "duplicate", "missing", "nonce", "tree", "runtime", "schema"],
)
def test_probe_envelope_rejects_ambiguous_or_unbound_identity(defect: str) -> None:
    bound = identity()
    text = valid_proof(bound)
    if defect == "extra":
        text = proof(bound, arbitrary_grant=True)
    elif defect == "trailing":
        text += "{}"
    elif defect == "duplicate":
        text = text.replace(
            '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
        )
    elif defect == "missing":
        payload = json.loads(text)
        del payload["private_reads_denied"]
        text = json.dumps(payload)
    elif defect == "nonce":
        text = proof(bound, nonce="8" * 32)
    elif defect == "tree":
        text = proof(bound, candidate_tree="d" * 40)
    elif defect == "runtime":
        text = proof(bound, runtime_fingerprint="d" * 64)
    else:
        text = proof(bound, schema_version=True)
    assert (
        runtime.parse_test_execution_probe(
            text, bound, FINGERPRINT, protected_paths=protected_paths()
        )
        is False
    )


@settings(max_examples=20, deadline=None)
@given(
    project=st.integers(min_value=1, max_value=1000000),
    issue=st.integers(min_value=1, max_value=1000000),
    revision=st.integers(min_value=1, max_value=1000000),
    nonce=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
)
def test_public_identity_bound_probe_property(
    project: int, issue: int, revision: int, nonce: str
) -> None:
    bound = replace(
        identity(),
        project_id=project,
        issue_iid=issue,
        ledger_revision=revision,
        nonce=nonce,
    )
    assert (
        runtime.parse_test_execution_probe(
            proof(bound), bound, FINGERPRINT, protected_paths=protected_paths()
        )
        is True
    )
    changed = replace(bound, nonce=("0" if nonce[0] != "0" else "1") + nonce[1:])
    assert (
        runtime.parse_test_execution_probe(
            proof(bound), changed, FINGERPRINT, protected_paths=protected_paths()
        )
        is False
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": 0},
        {"issue_iid": 0},
        {"ledger_revision": -1},
        {"source_branch": ""},
        {"base_sha": "bad"},
        {"candidate_tree": "bad"},
        {"test_paths": ("../outside.py",)},
        {"test_paths": ("template/tests/a.py",)},
        {"nonce": "broker-nonce"},
    ],
)
def test_identity_rejects_unbound_or_escaped_manifest(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        replace(identity(), **changes)


@pytest.mark.parametrize(
    "argv",
    [
        ("bash", "-c", "true"),
        ("uv", "run", "pytest", "../outside.py"),
        ("uv", "run", "pytest", "other.py"),
        ("uv", "run", "pytest", "--", "/outside.py"),
    ],
)
def test_execution_request_cannot_supply_arbitrary_payload_or_unbound_targets(
    tmp_path: Path, argv: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        runtime.TestExecutionRequest(identity=identity(), worktree=tmp_path, argv=argv)


@pytest.mark.parametrize(
    "operation",
    [
        *["accessible-" + category for category in CATEGORIES],
        "eacces",
        "eperm",
        "missing",
        "directory",
        "host-network",
        "extra-interface",
        "inherited-env",
        "inherited-fd",
        "candidate-write",
        "scratch-denied",
        "unsafe-child",
        "failed-child",
        "malformed-child",
    ],
)
def test_actual_reference_producer_truthfully_observes_synthetic_boundaries(
    tmp_path: Path, operation: str
) -> None:
    manifest = protected_paths(tmp_path)
    for outside in protected_tuple(manifest):
        outside.write_text("synthetic sentinel; contents must never be consumed")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    bound = identity()
    command = runtime.build_test_execution_probe(
        bound,
        probe_paths=protected_tuple(manifest),
        protected_paths=manifest,
        scratch=scratch,
        parent_net_namespace="parent-net",
        runtime_fingerprint=FINGERPRINT,
    )
    assert command[:3] == (sys.executable, "-I", "-c")
    original_open = os.open
    descriptor_attempts: list[str] = []
    children: list[tuple[str, ...]] = []
    child_active = False
    candidate_attempts: list[str] = []
    scratch_attempts: list[str] = []

    def open_descriptor(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        normalized = Path(os.fsdecode(path))
        if normalized in protected_tuple(manifest):
            descriptor_attempts.append(str(normalized))
            category = CATEGORIES[protected_tuple(manifest).index(normalized)]
            if operation == "accessible-" + category or (
                operation == "unsafe-child" and child_active
            ):
                return original_open(normalized, flags, mode)
            code = {
                "eperm": errno.EPERM,
                "missing": errno.ENOENT,
                "directory": errno.EISDIR,
            }.get(operation, errno.EACCES)
            raise OSError(code, "synthetic descriptor boundary")
        if normalized.is_relative_to(scratch) and flags & os.O_CREAT:
            scratch_attempts.append(str(normalized))
            if operation == "scratch-denied":
                raise PermissionError(errno.EACCES, "synthetic denied scratch")
            return original_open(normalized, flags, mode)
        if flags & os.O_CREAT:
            candidate_attempts.append(str(normalized))
            if operation == "candidate-write":
                assert normalized.is_relative_to(tmp_path)
                normalized.parent.mkdir(parents=True, exist_ok=True)
                return original_open(normalized, flags, mode)
        raise PermissionError(errno.EACCES, "synthetic denied write")

    def prohibit_bytes(*args: object, **kwargs: object) -> object:
        raise AssertionError("Producer must not consume private descriptor bytes")

    def list_boundary(path: str | bytes | os.PathLike[str]) -> list[str]:
        text = os.fsdecode(path)
        if "class/net" in text:
            return ["lo", "eth0"] if operation == "extra-interface" else ["lo"]
        if text.endswith("/fd"):
            return (
                ["0", "1", "2", "17"]
                if operation == "inherited-fd"
                else ["0", "1", "2"]
            )
        return []

    def read_link(
        path: str | bytes | os.PathLike[str], *, dir_fd: int | None = None
    ) -> str:
        return "parent-net" if operation == "host-network" else "isolated-net"

    def execute_script(argv: tuple[str, ...]) -> str:
        index = argv.index("-c") + 1
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["probe", *argv[index + 1 :]]),
            redirect_stdout(output),
        ):
            exec(compile(argv[index], "fixture-reference-producer", "exec"), {})  # noqa: S102 - Only the emitted repository producer runs under fake OS/process boundaries.
        return output.getvalue()

    def child_boundary(
        args: tuple[str, ...] | list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal child_active
        argv = tuple(args)
        children.append(argv)
        if operation == "failed-child":
            return subprocess.CompletedProcess(argv, 1, "", "synthetic failed child")
        if operation == "malformed-child":
            return subprocess.CompletedProcess(argv, 0, "malformed-child-output", "")
        child_active = True
        try:
            output = execute_script(argv)
        finally:
            child_active = False
        return subprocess.CompletedProcess(argv, 0, output, "")

    environment = (
        {"GITLAB_TOKEN": "synthetic-broker-secret"}
        if operation == "inherited-env"
        else {}
    )
    with (
        patch.object(os, "open", open_descriptor),
        patch.object(os, "read", prohibit_bytes),
        patch("builtins.open", prohibit_bytes),
        patch("io.open", prohibit_bytes),
        patch.object(os, "listdir", list_boundary),
        patch.object(os, "readlink", read_link),
        patch.object(os, "getcwd", lambda: str(tmp_path)),
        patch.dict(os.environ, environment, clear=True),
        patch.object(subprocess, "run", child_boundary),
    ):
        output = execute_script(command)
    payload = json.loads(output)
    assert descriptor_attempts
    assert children
    assert payload["probe_manifest_digest"] == manifest_digest(manifest)
    expected_categories = dict.fromkeys(CATEGORIES, True)
    if operation.startswith("accessible-"):
        expected_categories[operation.removeprefix("accessible-")] = False
    elif operation in {"missing", "directory"}:
        expected_categories = dict.fromkeys(CATEGORIES, False)
    assert payload["protected_categories_denied"] == expected_categories
    assert "synthetic-broker-secret" not in output
    if operation in {"eacces", "eperm"}:
        assert payload["private_reads_denied"] is True
        assert payload["descendants_denied"] is True
        assert (
            runtime.parse_test_execution_probe(
                output, bound, FINGERPRINT, protected_paths=manifest
            )
            is True
        )
    elif operation in {
        *["accessible-" + category for category in CATEGORIES],
        "missing",
        "directory",
    }:
        assert payload["private_reads_denied"] is False
        assert payload["descendants_denied"] is False
        assert (
            runtime.parse_test_execution_probe(
                output, bound, FINGERPRINT, protected_paths=manifest
            )
            is False
        )
    elif operation in {
        "candidate-write",
        "scratch-denied",
        "unsafe-child",
        "failed-child",
        "malformed-child",
    }:
        assert candidate_attempts and scratch_attempts
        field = {
            "candidate-write": "candidate_writes_denied",
            "scratch-denied": "scratch_write_allowed",
            "unsafe-child": "descendants_denied",
            "failed-child": "descendants_denied",
            "malformed-child": "descendants_denied",
        }[operation]
        assert payload[field] is False
        if operation in {"unsafe-child", "failed-child", "malformed-child"}:
            assert payload["private_reads_denied"] is True
            assert payload["candidate_writes_denied"] is True
            assert payload["scratch_write_allowed"] is True
        assert (
            runtime.parse_test_execution_probe(
                output, bound, FINGERPRINT, protected_paths=manifest
            )
            is False
        )
    else:
        field = {
            "host-network": "network_isolated",
            "extra-interface": "network_isolated",
            "inherited-env": "privileged_env_absent",
            "inherited-fd": "privileged_fds_absent",
        }[operation]
        assert payload[field] is False
        assert (
            runtime.parse_test_execution_probe(
                output, bound, FINGERPRINT, protected_paths=manifest
            )
            is False
        )


@pytest.mark.parametrize("category", CATEGORIES)
@pytest.mark.parametrize("defect", ["false", "missing", "integer"])
def test_each_host_required_category_is_independently_required_in_probe(
    category: str, defect: str
) -> None:
    bound = identity()
    valid_proof(bound)
    categories: dict[str, object] = dict.fromkeys(CATEGORIES, True)
    if defect == "missing":
        del categories[category]
    else:
        categories[category] = False if defect == "false" else 1
    assert (
        runtime.parse_test_execution_probe(
            proof(bound, protected_categories_denied=categories),
            bound,
            FINGERPRINT,
            protected_paths=protected_paths(),
        )
        is False
    )


@pytest.mark.parametrize(
    "defect", ["extra-category", "no-coverage", "digest", "duplicate-category"]
)
def test_aggregate_true_cannot_replace_complete_unique_host_manifest(
    defect: str,
) -> None:
    bound = identity()
    text = valid_proof(bound)
    if defect == "extra-category":
        text = proof(
            bound,
            protected_categories_denied={
                **dict.fromkeys(CATEGORIES, True),
                "other": True,
            },
        )
    elif defect == "no-coverage":
        text = proof(bound, protected_categories_denied={})
    elif defect == "digest":
        text = proof(bound, probe_manifest_digest="d" * 64)
    else:
        text = text.replace('"auth": true', '"auth": true, "auth": true')
    assert (
        runtime.parse_test_execution_probe(
            text, bound, FINGERPRINT, protected_paths=protected_paths()
        )
        is False
    )


@pytest.mark.parametrize("defect", ["empty", "missing", "duplicate", "mismatch"])
def test_prepared_probe_paths_must_exactly_match_host_owned_manifest(
    tmp_path: Path, defect: str
) -> None:
    class IncompleteHost(FixtureHost):
        def prepare(
            self, request: runtime.TestExecutionRequest
        ) -> runtime.PreparedTestExecution:
            prepared = super().prepare(request)
            paths = prepared.probe_paths
            changed = {
                "empty": (),
                "missing": paths[1:],
                "duplicate": (*paths[:-1], paths[0]),
                "mismatch": (*paths[:-1], tmp_path / "different.json"),
            }[defect]
            return replace(prepared, probe_paths=changed)

    host = IncompleteHost(tmp_path)
    policy = runtime.create_test_execution_policy(
        host.ports(), protected_paths=protected_paths(tmp_path)
    )
    with pytest.raises(runtime.TestExecutionError):
        policy.execute_red(request(tmp_path))
    assert "probe" not in host.events and "execute" not in host.events
    assert host.reaped and host.cleaned


@pytest.mark.parametrize("category", CATEGORIES)
def test_builder_refuses_missing_required_category_before_emitting_any_script(
    tmp_path: Path, category: str
) -> None:
    manifest = protected_paths(tmp_path)
    selected = tuple(
        path
        for name, path in zip(CATEGORIES, protected_tuple(manifest), strict=True)
        if name != category
    )
    with pytest.raises((runtime.TestExecutionError, ValueError)):
        runtime.build_test_execution_probe(
            identity(),
            probe_paths=selected,
            scratch=tmp_path / "scratch",
            parent_net_namespace="parent",
            runtime_fingerprint=FINGERPRINT,
            protected_paths=manifest,
        )


@pytest.mark.parametrize("defect", ["relative", "duplicate", "alias"])
def test_host_protected_manifest_rejects_relative_or_ambiguous_private_paths(
    defect: str,
) -> None:
    manifest = protected_paths()
    value = (
        Path("relative")
        if defect == "relative"
        else manifest.auth
        if defect == "duplicate"
        else Path("/synthetic-protected/../synthetic-protected/source-auth.json")
    )
    with pytest.raises(ValueError):
        replace(manifest, source_auth=value)


@settings(max_examples=20, deadline=None)
@given(
    category=st.sampled_from(CATEGORIES),
    value=st.one_of(st.integers(), st.text(max_size=20), st.none()),
)
def test_generated_nonboolean_category_evidence_cannot_grant_execution(
    category: str, value: object
) -> None:
    bound = identity()
    valid_proof(bound)
    categories: dict[str, object] = dict.fromkeys(CATEGORIES, True)
    categories[category] = value
    assert (
        runtime.parse_test_execution_probe(
            proof(bound, protected_categories_denied=categories),
            bound,
            FINGERPRINT,
            protected_paths=protected_paths(),
        )
        is False
    )
