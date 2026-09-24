# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for recoverable, producer-owned review handoffs."""

from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import build_parser
from agent_work.cli import main as cli_main
from agent_work.repository import CommandResult

_TREE = "a" * 40
_OTHER_TREE = "b" * 40
_JUNIT = ("py311:" + "c" * 64, "py314:" + "d" * 64)


@dataclass
class _State:
    objects: dict[str, bytes] = field(default_factory=dict)
    inputs: dict[Path, bytes] = field(default_factory=dict)
    writes: list[tuple[str, bytes]] = field(default_factory=list)
    tree: str = _TREE
    junit: tuple[str, ...] = _JUNIT
    project_id: int = 310967
    issue_iid: int = 202
    branch: str = "clanker/issue202-interactive-review-handoff"

    def observation(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "issue_iid": self.issue_iid,
            "branch": self.branch,
            "worktree": "/candidate",
            "candidate_tree": self.tree,
            "validation_state": "terminal-success",
            "junit_digests": self.junit,
        }

    def write_once(self, reference: str, payload: bytes) -> None:
        if reference in self.objects:
            raise FileExistsError(reference)
        self.objects[reference] = payload
        self.writes.append((reference, payload))


def _module() -> Any:
    return importlib.import_module("agent_work.review_handoff")


def _dependencies(module: Any, state: _State) -> Any:
    return module.ReviewHandoffDependencies(
        observe_candidate=state.observation,
        read_input=state.inputs.get,
        write_object=state.write_once,
        read_object=state.objects.get,
        list_objects=lambda prefix: tuple(
            key for key in state.objects if key.startswith(prefix + "/")
        ),
        nonce=lambda: "fixed-unpredictable-test-nonce",
        clock=lambda: 100.0,
    )


def _started(module: Any, state: _State) -> tuple[Any, Any]:
    dependencies = _dependencies(module, state)
    session = module.begin_review_handoff(
        worktree=Path("/candidate"), apply=True, dependencies=dependencies
    )
    return session, dependencies


def _submit(
    module: Any,
    state: _State,
    session: Any,
    dependencies: Any,
    *,
    role: str = "$tdd-green",
    version: int = 1,
    payload: bytes = b"complete correction ledger\n",
) -> Any:
    payload_path = Path("/scratch/receipt.txt")
    state.inputs[payload_path] = payload
    return module.submit_review_handoff(
        session_ref=session.session_ref,
        role=role,
        version=version,
        kind="correction-ledger",
        payload_path=payload_path,
        apply=True,
        dependencies=dependencies,
    )


def test_begin_binds_observed_candidate_and_successful_junit_evidence() -> None:
    module = _module()
    state = _State()

    session, dependencies = _started(module, state)
    manifest = module.review_handoff_manifest(
        session_ref=session.session_ref, dependencies=dependencies
    )

    assert manifest.project_id == 310967
    assert manifest.issue_iid == 202
    assert manifest.branch == state.branch
    assert manifest.candidate_tree == _TREE
    assert manifest.junit_digests == _JUNIT
    assert state.writes


def test_submit_retains_exact_complete_bytes_under_immutable_sha_reference() -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)
    receipt = b"complete review receipt\nline two\n"

    submitted = _submit(module, state, session, dependencies, payload=receipt)
    recovered = module.read_review_handoff(
        session_ref=session.session_ref,
        receipt_ref=submitted.receipt_ref,
        dependencies=_dependencies(module, state),
    )
    manifest = module.review_handoff_manifest(
        session_ref=session.session_ref, dependencies=_dependencies(module, state)
    )

    assert submitted.sha256 == hashlib.sha256(receipt).hexdigest()
    assert recovered == receipt
    assert submitted.receipt_ref in manifest.receipt_refs
    assert all(not reference.startswith("/candidate") for reference in state.objects)


@pytest.mark.parametrize("invalid_state", ["running", "terminal-failure"])
def test_begin_rejects_candidate_without_successful_validation(
    invalid_state: str,
) -> None:
    module = _module()
    state = _State()
    dependencies = _dependencies(module, state)
    observation = state.observation()
    observation["validation_state"] = invalid_state
    dependencies = module.ReviewHandoffDependencies(
        observe_candidate=lambda: observation,
        read_input=state.inputs.get,
        write_object=state.write_once,
        read_object=state.objects.get,
        list_objects=lambda prefix: tuple(
            key for key in state.objects if key.startswith(prefix + "/")
        ),
        nonce=lambda: "fixed-unpredictable-test-nonce",
        clock=lambda: 100.0,
    )

    with pytest.raises(module.ReviewHandoffError):
        module.begin_review_handoff(
            worktree=Path("/candidate"), apply=True, dependencies=dependencies
        )
    assert state.writes == []


def test_begin_rejects_a_worktree_other_than_the_observed_candidate() -> None:
    module = _module()
    state = _State()

    with pytest.raises(module.ReviewHandoffError):
        module.begin_review_handoff(
            worktree=Path("/other"),
            apply=True,
            dependencies=_dependencies(module, state),
        )
    assert state.writes == []


@pytest.mark.parametrize("changed", ["tree", "junit", "project_id", "branch"])
def test_recovery_rejects_stale_candidate_evidence(changed: str) -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)
    _submit(module, state, session, dependencies)
    setattr(
        state,
        changed,
        {
            "tree": _OTHER_TREE,
            "junit": ("py311:" + "e" * 64, "py314:" + "f" * 64),
            "project_id": 1,
            "branch": "other-branch",
        }[changed],
    )

    with pytest.raises(module.ReviewHandoffError):
        module.review_handoff_manifest(
            session_ref=session.session_ref, dependencies=dependencies
        )


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_read_rejects_missing_or_tampered_receipt(damage: str) -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)
    submitted = _submit(module, state, session, dependencies)
    accepted_ref = next(key for key in state.objects if key.endswith("/accepted"))
    if damage == "missing":
        del state.objects[accepted_ref]
    else:
        state.objects[accepted_ref] = b"changed after acceptance"

    with pytest.raises(module.ReviewHandoffError):
        module.read_review_handoff(
            session_ref=session.session_ref,
            receipt_ref=submitted.receipt_ref,
            dependencies=dependencies,
        )


def test_one_rejection_allows_one_corrected_green_ledger() -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)

    with pytest.raises(module.ReviewHandoffError):
        _submit(module, state, session, dependencies, payload=b"x" * 1_048_577)
    accepted = _submit(
        module, state, session, dependencies, payload=b"corrected receipt"
    )
    with pytest.raises(module.ReviewHandoffError):
        _submit(module, state, session, dependencies, payload=b"extra request")

    assert (
        module.read_review_handoff(
            session_ref=session.session_ref,
            receipt_ref=accepted.receipt_ref,
            dependencies=dependencies,
        )
        == b"corrected receipt"
    )


def test_interrupted_acceptance_preserves_one_recoverable_digest_receipt() -> None:
    module = _module()
    state = _State()
    session, original = _started(module, state)
    payload = b"complete ledger after a lost response"
    state.inputs[Path("/scratch/receipt.txt")] = payload

    def interrupted_write(reference: str, contents: bytes) -> None:
        state.write_once(reference, contents)
        if reference.startswith("receipt/") or reference.endswith("/accepted"):
            raise OSError("response lost after durable publication")

    dependencies = module.ReviewHandoffDependencies(
        observe_candidate=original.observe_candidate,
        read_input=original.read_input,
        write_object=interrupted_write,
        read_object=original.read_object,
        list_objects=original.list_objects,
        nonce=original.nonce,
        clock=original.clock,
    )
    with pytest.raises(OSError, match="response lost"):
        module.submit_review_handoff(
            session_ref=session.session_ref,
            role="$tdd-green",
            version=1,
            kind="correction-ledger",
            payload_path=Path("/scratch/receipt.txt"),
            apply=True,
            dependencies=dependencies,
        )

    manifest = module.review_handoff_manifest(
        session_ref=session.session_ref, dependencies=original
    )
    assert len(manifest.receipts) == 1
    assert manifest.receipts[0].sha256 == hashlib.sha256(payload).hexdigest()
    assert (
        module.read_review_handoff(
            session_ref=session.session_ref,
            receipt_ref=manifest.receipt_refs[0],
            dependencies=original,
        )
        == payload
    )
    with pytest.raises(module.ReviewHandoffError, match="already accepted"):
        _submit(module, state, session, original, payload=b"other ledger")

    before_state = _State()
    before_session, before_original = _started(module, before_state)

    def fail_before_publish(reference: str, contents: bytes) -> None:
        if reference.endswith("/accepted"):
            raise OSError("publication did not happen")
        before_state.write_once(reference, contents)

    before_dependencies = module.ReviewHandoffDependencies(
        observe_candidate=before_original.observe_candidate,
        read_input=before_original.read_input,
        write_object=fail_before_publish,
        read_object=before_original.read_object,
        list_objects=before_original.list_objects,
        nonce=before_original.nonce,
        clock=before_original.clock,
    )
    before_state.inputs[Path("/scratch/receipt.txt")] = b"retry after no publish"
    with pytest.raises(OSError, match="did not happen"):
        module.submit_review_handoff(
            session_ref=before_session.session_ref,
            role="$tdd-green",
            version=1,
            kind="correction-ledger",
            payload_path=Path("/scratch/receipt.txt"),
            apply=True,
            dependencies=before_dependencies,
        )
    assert not any(key.endswith("/accepted") for key in before_state.objects)
    _submit(module, before_state, before_session, before_original)

    rejected_state = _State()
    rejected_session, rejected_original = _started(module, rejected_state)

    def lost_rejection_response(reference: str, contents: bytes) -> None:
        rejected_state.write_once(reference, contents)
        if reference.endswith("/attempt/1"):
            raise OSError("rejection response lost")

    rejected_dependencies = module.ReviewHandoffDependencies(
        observe_candidate=rejected_original.observe_candidate,
        read_input=rejected_original.read_input,
        write_object=lost_rejection_response,
        read_object=rejected_original.read_object,
        list_objects=rejected_original.list_objects,
        nonce=rejected_original.nonce,
        clock=rejected_original.clock,
    )
    with pytest.raises(OSError, match="rejection response lost"):
        _submit(
            module,
            rejected_state,
            rejected_session,
            rejected_dependencies,
            payload=b"x" * 1_048_577,
        )
    corrected = _submit(
        module, rejected_state, rejected_session, rejected_original, payload=b"fixed"
    )
    assert (
        module.read_review_handoff(
            session_ref=rejected_session.session_ref,
            receipt_ref=corrected.receipt_ref,
            dependencies=rejected_original,
        )
        == b"fixed"
    )


def test_concurrent_distinct_ledgers_cannot_both_be_accepted() -> None:
    module = _module()
    state = _State()
    session, original = _started(module, state)
    first_path = Path("/scratch/first.txt")
    second_path = Path("/scratch/second.txt")
    state.inputs[first_path] = b"first ledger"
    state.inputs[second_path] = b"second ledger"
    interleaved = False

    def interleaved_write(reference: str, payload: bytes) -> None:
        nonlocal interleaved
        if not interleaved and (
            reference.startswith("receipt/") or reference.endswith("/accepted")
        ):
            interleaved = True
            module.submit_review_handoff(
                session_ref=session.session_ref,
                role="$tdd-green",
                version=1,
                kind="correction-ledger",
                payload_path=second_path,
                apply=True,
                dependencies=original,
            )
        state.write_once(reference, payload)

    dependencies = module.ReviewHandoffDependencies(
        observe_candidate=original.observe_candidate,
        read_input=original.read_input,
        write_object=interleaved_write,
        read_object=original.read_object,
        list_objects=original.list_objects,
        nonce=original.nonce,
        clock=original.clock,
    )
    with pytest.raises(module.ReviewHandoffError):
        module.submit_review_handoff(
            session_ref=session.session_ref,
            role="$tdd-green",
            version=1,
            kind="correction-ledger",
            payload_path=first_path,
            apply=True,
            dependencies=dependencies,
        )
    manifest = module.review_handoff_manifest(
        session_ref=session.session_ref, dependencies=original
    )
    assert len(manifest.receipt_refs) == 1
    assert manifest.receipts[0].sha256 == hashlib.sha256(b"second ledger").hexdigest()
    assert (
        len(
            [
                key
                for key in state.objects
                if key.startswith("receipt/") or key.endswith("/accepted")
            ]
        )
        == 1
    )


def test_model_verdict_cannot_change_trusted_candidate_identity() -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)
    submitted = _submit(
        module,
        state,
        session,
        dependencies,
        payload=b'{"verdict":"approved","candidate_tree":"' + b"b" * 40 + b'"}',
    )

    manifest = module.review_handoff_manifest(
        session_ref=session.session_ref, dependencies=dependencies
    )
    assert submitted.receipt_ref in manifest.receipt_refs
    assert manifest.candidate_tree == _TREE


def test_specialist_role_cannot_submit_directly() -> None:
    module = _module()
    state = _State()
    session, dependencies = _started(module, state)
    with pytest.raises(module.ReviewHandoffError):
        _submit(module, state, session, dependencies, role="$judge-quality")


@pytest.mark.parametrize("action", ["begin", "submit", "manifest", "read"])
def test_cli_exposes_review_handoff_actions(action: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["review-handoff", action, "--help"])
    assert error.value.code == 0


def test_cli_persists_private_session_and_reads_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    git_directory = tmp_path / "gitdir"
    git_directory.mkdir()
    shared_index = git_directory / "index"
    shared_index.write_bytes(b"staged index snapshot")
    shared_objects = git_directory / "objects"
    shared_objects.mkdir()
    state = tmp_path / "state"
    validation_root = (
        state
        / "agent-work-validation"
        / hashlib.sha256(str(git_directory).encode()).hexdigest()
    )
    artifact_directory = validation_root / _TREE
    artifact_directory.mkdir(parents=True)
    artifacts = []
    for environment in ("py311", "py314"):
        path = artifact_directory / f"test-results-{environment}.xml"
        contents = f"<{environment}/>".encode()
        path.write_bytes(contents)
        artifacts.append(
            {
                "environment": environment,
                "path": str(path),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "test_count": 1,
            }
        )
    (validation_root / f"{_TREE}.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "candidate_tree": _TREE,
                "state": "terminal-success",
                "returncode": 0,
                "junit_artifacts": artifacts,
            }
        )
    )

    def run_git(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        operation = command[3]
        if operation == "write-tree":
            git_environment = kwargs.get("environment")
            if not isinstance(git_environment, dict):
                return CommandResult(128, "", "shared index.lock is read-only")
            values = cast("dict[str, object]", git_environment)
            private_index = values.get("GIT_INDEX_FILE")
            private_objects = values.get("GIT_OBJECT_DIRECTORY")
            shared_alternate = values.get("GIT_ALTERNATE_OBJECT_DIRECTORIES")
            if (
                not isinstance(private_index, str)
                or not isinstance(private_objects, str)
                or not isinstance(shared_alternate, str)
            ):
                return CommandResult(128, "", "shared index.lock is read-only")
            assert Path(private_index).read_bytes() == b"staged index snapshot"
            assert Path(private_objects).is_dir()
            assert Path(private_index) != shared_index
            assert Path(private_objects) != shared_objects
            assert Path(private_index).parent == Path(private_objects).parent
            assert Path(private_index).parent != git_directory
            assert Path(private_index).parent.stat().st_mode & 0o077 == 0
            assert shared_alternate == str(shared_objects)
        if operation == "rev-parse" and command[4:6] == ("--git-path", "index"):
            return CommandResult(0, str(shared_index), "")
        if operation == "rev-parse" and command[4:6] == ("--git-path", "objects"):
            return CommandResult(0, str(shared_objects), "")
        values = {
            "rev-parse": str(git_directory),
            "write-tree": _TREE,
            "symbolic-ref": "clanker/issue202-review-handoff",
            "remote": "https://gitlab-master.nvidia.com/nvis-ova/nvis-ova-agentic-copier.git",
        }
        return CommandResult(0, values[operation], "")

    environment = {
        "XDG_STATE_HOME": str(state),
        "GITLAB_PROJECT_ID": "310967",
        "GITLAB_HOST": "gitlab-master.nvidia.com",
    }
    output: list[str] = []
    errors: list[str] = []

    def invoke(*arguments: str) -> dict[str, Any]:
        output.clear()
        assert (
            cli_main(
                ["review-handoff", *arguments, "--json"],
                environment=environment,
                run_command=run_git,
                write_stdout=output.append,
                write_stderr=errors.append,
            )
            == 0
        ), errors
        return json.loads(output[0])

    def foreign_git(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        if command[2] == str(candidate) and command[3] == "remote":
            return CommandResult(
                0, "https://gitlab-master.nvidia.com/nvis-ova/other.git", ""
            )
        return run_git(command, **kwargs)

    assert (
        cli_main(
            ["review-handoff", "begin", "--worktree", str(candidate), "--apply"],
            environment=environment,
            run_command=foreign_git,
            write_stdout=output.append,
            write_stderr=errors.append,
        )
        == 1
    )
    assert not (state / "agent-work-review-handoff").exists()
    output.clear()
    errors.clear()

    preview = invoke("begin", "--worktree", str(candidate), "--issue-iid", "202")
    store = state / "agent-work-review-handoff"
    assert not store.exists()
    begun = invoke(
        "begin", "--worktree", str(candidate), "--issue-iid", "202", "--apply"
    )
    receipt_file = tmp_path / "ledger.txt"
    receipt_file.write_bytes(b"full correction ledger\n")
    (tmp_path / "nested").mkdir()
    assert (
        cli_main(
            [
                "review-handoff",
                "submit",
                "--worktree",
                str(candidate),
                "--session-id",
                begun["session_id"],
                "--role",
                "$tdd-green",
                "--review-version",
                "1",
                "--kind",
                "correction-ledger",
                "--input-file",
                "nested/../ledger.txt",
                "--apply",
            ],
            environment=environment,
            run_command=run_git,
            write_stdout=output.append,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["object path traversal is forbidden"]
    output.clear()
    errors.clear()
    submitted = invoke(
        "submit",
        "--worktree",
        str(candidate),
        "--session-id",
        begun["session_id"],
        "--role",
        "$tdd-green",
        "--review-version",
        "1",
        "--kind",
        "correction-ledger",
        "--input-file",
        receipt_file.name,
        "--apply",
    )
    manifest = invoke(
        "manifest", "--worktree", str(candidate), "--session-id", begun["session_id"]
    )
    recovered = invoke(
        "read",
        "--worktree",
        str(candidate),
        "--session-id",
        begun["session_id"],
        "--receipt-id",
        submitted["receipt_id"],
        "--expected-manifest-sha256",
        manifest["sha256"],
    )

    assert preview["applied"] is False
    assert manifest["receipts"][0]["role"] == "$tdd-green"
    assert manifest["receipts"][0]["sha256"] == submitted["sha256"]
    assert recovered["content_base64"] == "ZnVsbCBjb3JyZWN0aW9uIGxlZGdlcgo="
    output.clear()
    assert (
        cli_main(
            [
                "review-handoff",
                "read",
                "--worktree",
                str(candidate),
                "--session-id",
                begun["session_id"],
                "--receipt-id",
                submitted["receipt_id"],
            ],
            environment=environment,
            run_command=run_git,
            write_stdout=output.append,
            write_stderr=errors.append,
        )
        == 0
    )
    assert output == ["full correction ledger\n"]
    assert all(
        path.stat().st_mode & 0o077 == 0 for path in store.rglob("*") if path.is_file()
    )
    assert shared_index.read_bytes() == b"staged index snapshot"
    assert not (git_directory / "index.lock").exists()
    assert not list(shared_objects.iterdir())
    (artifact_directory / "test-results-py311.xml").write_bytes(b"changed")
    assert (
        cli_main(
            [
                "review-handoff",
                "manifest",
                "--worktree",
                str(candidate),
                "--session-id",
                begun["session_id"],
                "--json",
            ],
            environment=environment,
            run_command=run_git,
            write_stdout=output.append,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors[-1] == "Judge evidence is malformed or stale"
