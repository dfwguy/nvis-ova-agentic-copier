# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for durable, uncommitted Red checkpoints."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.models import AgentWorkConfig, LedgerRecord
from agent_work.red_checkpoint import RedCheckpointError
from agent_work.repository import CommandResult, GitRepository, RepositoryError
from agent_work.worker import build_worker_prompt

BASE_SHA = "a" * 40
TREE_OID = "b" * 40
TEST_PATH = "tests/test_widget.py"
CHECKPOINT_REF = f"refs/agent-work/red-checkpoints/81/{BASE_SHA}"


def _checkpoint_module() -> Any:
    return importlib.import_module("agent_work.red_checkpoint")


def _record(tmp_path: Path, **changes: object) -> LedgerRecord:
    values: dict[str, Any] = {
        "project_id": 310967,
        "issue_iid": 81,
        "revision": 7,
        "state": RunnerState.EXECUTING,
        "phase": WorkflowPhase.WORKFLOW,
        "workflow_lane": WorkflowLane.UNKNOWN,
        "work_kind": WorkKind.ORDINARY,
        "source_branch": "agent/issue-81",
        "worktree": tmp_path / "worktree",
        "source_sha": BASE_SHA,
    }
    values.update(changes)
    return LedgerRecord(**values)


def _request(**changes: object) -> Any:
    checkpoint = _checkpoint_module()
    values: dict[str, object] = {
        "argv": ("uv", "run", "pytest", f"{TEST_PATH}::test_red"),
        "expected_failure": "assert 1 == 2",
        "test_paths": (TEST_PATH,),
    }
    values.update(changes)
    return checkpoint.RedCheckpointRequest(**values)


def _evidence(**changes: object) -> Any:
    checkpoint = _checkpoint_module()
    values: dict[str, object] = {
        "schema_version": 1,
        "project_id": 310967,
        "issue_iid": 81,
        "source_branch": "agent/issue-81",
        "base_sha": BASE_SHA,
        "tree_oid": TREE_OID,
        "ref": CHECKPOINT_REF,
        "test_paths": (TEST_PATH,),
        "argv": ("uv", "run", "pytest", f"{TEST_PATH}::test_red"),
        "exit_code": 1,
        "expected_failure": "assert 1 == 2",
        "output_sha256": hashlib.sha256(b"assert 1 == 2\n").hexdigest(),
    }
    values.update(changes)
    return checkpoint.RedCheckpointEvidence(**values)


class FakeDependencies:
    """Injected trusted boundaries for one checkpoint transaction."""

    def __init__(self, tmp_path: Path) -> None:
        self.record = _record(tmp_path)
        self.head = BASE_SHA
        self.paths: tuple[str, ...] = (TEST_PATH,)
        self.command_result = CommandResult(1, "assert 1 == 2\n", "")
        self.after_run: Callable[[], None] | None = None
        self.retained: Any | None = None
        self.snapshot_result = TREE_OID
        self.calls: list[tuple[str, object]] = []

    def load_record(self, issue_iid: int) -> LedgerRecord | None:
        self.calls.append(("load", issue_iid))
        return self.record

    def current_head(self, worktree: Path) -> str:
        self.calls.append(("head", worktree))
        return self.head

    def changed_paths(self, worktree: Path) -> tuple[str, ...]:
        self.calls.append(("paths", worktree))
        return self.paths

    def run_red_command(self, worktree: Path, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(("run", (worktree, argv)))
        if self.after_run is not None:
            self.after_run()
        return self.command_result

    def snapshot_test_tree(self, base_sha: str, paths: tuple[str, ...]) -> str:
        self.calls.append(("snapshot", (base_sha, paths)))
        return self.snapshot_result

    def read_evidence(
        self, project_id: int, issue_iid: int, base_sha: str
    ) -> Any | None:
        self.calls.append(("read", (project_id, issue_iid, base_sha)))
        return self.retained

    def write_evidence(self, evidence: Any) -> None:
        self.calls.append(("write", evidence))
        self.retained = evidence

    def value(self) -> Any:
        checkpoint = _checkpoint_module()
        return checkpoint.RedCheckpointDependencies(
            load_record=self.load_record,
            current_head=self.current_head,
            changed_paths=self.changed_paths,
            run_red_command=self.run_red_command,
            snapshot_test_tree=self.snapshot_test_tree,
            expected_tree_oid=self.snapshot_result,
            read_evidence=self.read_evidence,
            write_evidence=self.write_evidence,
        )


def test_red_checkpoint_apply_reruns_failure_then_snapshots_and_records(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        dependencies=dependencies.value(),
    )

    assert evidence == _evidence()
    assert [name for name, _value in dependencies.calls] == [
        "load",
        "head",
        "paths",
        "read",
        "run",
        "head",
        "paths",
        "snapshot",
        "write",
    ]


def test_red_checkpoint_rejects_malformed_snapshot_tree_before_evidence_write(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.snapshot_result = "not-a-tree-oid"

    with pytest.raises(checkpoint.RedCheckpointError, match="snapshot tree"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(),
            dependencies=dependencies.value(),
        )

    assert "snapshot" in [name for name, _value in dependencies.calls]
    assert "write" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_rejects_test_tree_changed_by_red_before_ref_write(
    tmp_path: Path,
) -> None:
    """The atomic ref is not published when Red changes the candidate tree."""
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    dependencies.after_run = lambda: setattr(dependencies, "snapshot_result", "c" * 40)
    with pytest.raises(checkpoint.RedCheckpointError, match="candidate tree changed"):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=dependencies.value()
        )

    assert "write" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_rejects_undeclared_path_added_by_red_before_ref_write(
    tmp_path: Path,
) -> None:
    """The transaction rechecks all changed paths after running Red."""
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.after_run = lambda: setattr(
        dependencies, "paths", (TEST_PATH, "tests/test_added.py")
    )

    with pytest.raises(checkpoint.RedCheckpointError, match="paths changed"):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=dependencies.value()
        )

    assert dependencies.retained is None
    assert "write" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_exact_replay_is_idempotent(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.retained = _evidence()

    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        dependencies=dependencies.value(),
    )

    assert evidence == dependencies.retained
    assert "run" not in [name for name, _value in dependencies.calls]
    assert "write" not in [name for name, _value in dependencies.calls]


@pytest.mark.parametrize(
    ("request_changes", "dependency_changes", "message"),
    (
        ({"argv": ("pytest", TEST_PATH)}, {}, "uv run pytest"),
        ({"argv": ("uv", "run", "pytest", "-q")}, {}, "test path"),
        ({"test_paths": ("src/widget.py",)}, {}, "tests/"),
        ({}, {"head": "c" * 40}, "head"),
        ({}, {"paths": (TEST_PATH, "src/widget.py")}, "test paths"),
        ({}, {"command_result": CommandResult(0, "passed", "")}, "must fail"),
        (
            {},
            {"command_result": CommandResult(1, "different failure", "")},
            "expected failure",
        ),
    ),
)
def test_red_checkpoint_rejects_invalid_or_unproven_evidence_before_storage(
    tmp_path: Path,
    request_changes: dict[str, object],
    dependency_changes: dict[str, object],
    message: str,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    for name, value in dependency_changes.items():
        setattr(dependencies, name, value)

    with pytest.raises(checkpoint.RedCheckpointError, match=message):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(**request_changes),
            dependencies=dependencies.value(),
        )

    assert "write" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_rejects_wrong_state_phase_or_identity(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.record = _record(tmp_path, state=RunnerState.BLOCKED)

    with pytest.raises(checkpoint.RedCheckpointError, match="executing workflow"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(),
            dependencies=dependencies.value(),
        )

    assert dependencies.calls == [("load", 81)]


def test_red_checkpoint_rejects_conflicting_retained_ref_or_record(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.retained = _evidence(tree_oid="c" * 40)

    with pytest.raises(checkpoint.RedCheckpointError, match="conflict"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(),
            dependencies=dependencies.value(),
        )

    assert "run" not in [name for name, _value in dependencies.calls]


@pytest.mark.parametrize(
    ("request_changes", "message"),
    (
        ({"argv": ("uv", "run", "pytest", *(["-q"] * 126))}, "128"),
        (
            {"argv": ("uv", "run", "pytest", "x" * 4_097)},
            "argv entries",
        ),
        ({"expected_failure": "x" * 2_049}, "failure marker"),
        (
            {"test_paths": tuple(f"tests/test_{index}.py" for index in range(65))},
            "64",
        ),
    ),
)
def test_red_checkpoint_rejects_every_request_size_boundary_before_side_effects(
    tmp_path: Path, request_changes: dict[str, object], message: str
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    with pytest.raises(checkpoint.RedCheckpointError, match=message):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(**request_changes),
            dependencies=dependencies.value(),
        )

    assert dependencies.calls == []


@pytest.mark.parametrize(
    "paths",
    (
        ("tests/../secret.py",),
        ("/tests/test_absolute.py",),
        (TEST_PATH, TEST_PATH),
        ("tests/test_z.py", "tests/test_a.py"),
        ("tests/test_not_python.txt",),
        (f"tests/{'x' * 506}.py",),
    ),
)
def test_red_checkpoint_rejects_non_normalized_test_path_adversaries(
    tmp_path: Path, paths: tuple[str, ...]
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    with pytest.raises(checkpoint.RedCheckpointError, match="test paths"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(argv=("uv", "run", "pytest", *paths), test_paths=paths),
            dependencies=dependencies.value(),
        )

    assert dependencies.calls == []


@given(
    st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz_",
            min_size=1,
            max_size=20,
        ),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
def test_red_checkpoint_accepts_sorted_unique_normalized_test_paths(
    stems: list[str],
) -> None:
    checkpoint = _checkpoint_module()
    paths = tuple(sorted(f"tests/test_{stem}.py" for stem in stems))

    validated = checkpoint.validate_red_checkpoint_request(
        _request(argv=("uv", "run", "pytest", *paths), test_paths=paths)
    )

    assert validated.test_paths == paths


def test_red_checkpoint_preflights_total_evidence_size_before_git_snapshot(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.command_result = CommandResult(255, "expected\n", "")
    paths = tuple(f"tests/{'x' * 480}_{index:02d}.py" for index in range(64))
    dependencies.paths = paths
    request = _request(
        argv=("uv", "run", "pytest", *paths),
        expected_failure="expected",
        test_paths=paths,
    )

    with pytest.raises(checkpoint.RedCheckpointError, match="durable bound"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=request,
            dependencies=dependencies.value(),
        )

    assert "snapshot" not in [name for name, _value in dependencies.calls]


def _config(tmp_path: Path) -> AgentWorkConfig:
    return AgentWorkConfig(
        host="gitlab.example.com",
        project_id=310967,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "bin" / "git",
        glab_path=tmp_path / "bin" / "glab",
        base_branch="agent/work-base",
    )


def test_repository_snapshots_declared_tests_with_a_temporary_index(
    tmp_path: Path,
) -> None:
    commands: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def run(command: tuple[str, ...], **kwargs: Any) -> CommandResult:
        commands.append((command, kwargs))
        if "write-tree" in command:
            return CommandResult(0, f"{TREE_OID}\n", "")
        if "rev-parse" in command and CHECKPOINT_REF in " ".join(command):
            return CommandResult(1, "", "")
        return CommandResult(0, "", "")

    tree = GitRepository(_config(tmp_path), run).snapshot_test_tree(
        tmp_path / "worktree", BASE_SHA, (TEST_PATH,)
    )

    assert tree == TREE_OID
    rendered = [command[3:] for command, _kwargs in commands]
    assert ("read-tree", BASE_SHA) in rendered
    assert ("add", "--", TEST_PATH) in rendered
    assert ("write-tree",) in rendered
    assert all(
        "GIT_INDEX_FILE" in kwargs["environment"]
        for command, kwargs in commands
        if command[3:4] in {("read-tree",), ("add",), ("write-tree",)}
    )
    assert not any(
        action in command for command in rendered for action in ("commit", "push")
    )


def test_repository_checkpoint_ref_uses_one_cas_commit_and_exact_readback(
    tmp_path: Path,
) -> None:
    """Injected Git responses prove the new ref protocol without a subprocess."""
    evidence = _evidence()
    commit_oid = "c" * 40
    commands: list[tuple[str, ...]] = []
    commit_arguments: tuple[str, ...] | None = None

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        nonlocal commit_arguments
        commands.append(command[3:])
        arguments = command[3:]
        if arguments[:1] == ("commit-tree",):
            commit_arguments = arguments
            return CommandResult(0, f"{commit_oid}\n", "")
        if arguments[:1] == ("update-ref",):
            return CommandResult(0, "", "")
        if arguments[:1] == ("rev-parse",) and "--verify" in arguments:
            return CommandResult(0, f"{commit_oid}\n", "")
        if arguments[:2] == ("cat-file", "-t"):
            return CommandResult(0, "commit\n", "")
        if arguments[:2] == ("cat-file", "-s"):
            return CommandResult(0, "512\n", "")
        if arguments[:3] == ("show", "-s", "--format=%B"):
            assert commit_arguments is not None
            return CommandResult(0, commit_arguments[5], "")
        if arguments[:3] == ("show", "-s", "--format=%P"):
            assert commit_arguments is not None
            return CommandResult(0, f"{commit_arguments[3]}\n", "")
        if arguments[:1] == ("rev-parse",) and arguments[-1].endswith("^{tree}"):
            assert commit_arguments is not None
            return CommandResult(0, f"{commit_arguments[1]}\n", "")
        raise AssertionError(arguments)

    repository = GitRepository(_config(tmp_path), run)
    repository.write_red_checkpoint_ref(tmp_path / "worktree", evidence)
    assert (
        repository.read_red_checkpoint_ref(tmp_path / "worktree", 310967, 81, BASE_SHA)
        == evidence
    )
    assert commit_arguments == (
        "commit-tree",
        TREE_OID,
        "-p",
        BASE_SHA,
        "-m",
        evidence.model_dump_json(),
    )
    assert ("update-ref", CHECKPOINT_REF, commit_oid, "") in commands


@pytest.mark.parametrize(
    ("scenario", "message"),
    (
        ("missing", None),
        ("legacy", None),
        ("oversized", "exceeds the durable bound"),
        ("malformed", "malformed"),
    ),
)
def test_repository_checkpoint_ref_reader_fails_closed_with_injected_git(
    tmp_path: Path, scenario: str, message: str | None
) -> None:
    """Missing and legacy refs fall through; corrupt commit evidence is rejected."""
    evidence = _evidence()

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        arguments = command[3:]
        if arguments[:1] == ("rev-parse",) and "--verify" in arguments:
            return (
                CommandResult(1, "", "")
                if scenario == "missing"
                else CommandResult(0, "c" * 40 + "\n", "")
            )
        if arguments[:2] == ("cat-file", "-t"):
            return CommandResult(
                0, "tree\n" if scenario == "legacy" else "commit\n", ""
            )
        if arguments[:2] == ("cat-file", "-s"):
            return CommandResult(
                0, "65537\n" if scenario == "oversized" else "512\n", ""
            )
        if arguments[:3] == ("show", "-s", "--format=%B"):
            return CommandResult(
                0,
                "not json" if scenario == "malformed" else evidence.model_dump_json(),
                "",
            )
        if arguments[:3] == ("show", "-s", "--format=%P"):
            return CommandResult(0, f"{BASE_SHA}\n", "")
        if arguments[:1] == ("rev-parse",):
            return CommandResult(0, f"{TREE_OID}\n", "")
        raise AssertionError(arguments)

    reader = GitRepository(_config(tmp_path), run)
    if message is None:
        assert (
            reader.read_red_checkpoint_ref(tmp_path / "worktree", 310967, 81, BASE_SHA)
            is None
        )
    else:
        with pytest.raises(RedCheckpointError, match=message):
            reader.read_red_checkpoint_ref(tmp_path / "worktree", 310967, 81, BASE_SHA)


def test_repository_checkpoint_ref_cas_failure_leaves_no_visible_authority(
    tmp_path: Path,
) -> None:
    """A failed compare-and-swap does not report a retained checkpoint."""
    evidence = _evidence()

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        arguments = command[3:]
        if arguments[:1] == ("commit-tree",):
            return CommandResult(0, "c" * 40 + "\n", "")
        if arguments[:1] == ("update-ref",):
            return CommandResult(1, "", "conflict")
        raise AssertionError(arguments)

    with pytest.raises(RepositoryError, match="ref could not be created"):
        GitRepository(_config(tmp_path), run).write_red_checkpoint_ref(
            tmp_path / "worktree", evidence
        )


def test_checkpoint_ref_never_changes_one_commit_final_publication_history(
    tmp_path: Path,
) -> None:
    publication_sha = "d" * 40
    commands: list[tuple[str, ...]] = []
    committed = False
    transport_credential_config = (
        "-c",
        "credential.helper=",
        "-c",
        'credential.helper=!exec "$AGENT_WORK_GIT_CREDENTIAL_HELPER"',
        "-c",
        "credential.useHttpPath=true",
        "-c",
        "core.askPass=/bin/false",
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal committed
        arguments = command[3:]
        if arguments[:1] == ("-c",):
            assert arguments[:8] == transport_credential_config
            arguments = arguments[8:]
            assert arguments[:1] == ("push",)
        commands.append(arguments)
        if arguments == ("write-tree",):
            return CommandResult(0, f"{TREE_OID}\n", "")
        if CHECKPOINT_REF in " ".join(arguments) and arguments[:1] in {
            ("rev-parse",),
            ("show-ref",),
        }:
            return CommandResult(1, "", "")
        if arguments == ("branch", "--show-current"):
            return CommandResult(0, "agent/issue-81\n", "")
        if arguments == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if arguments == ("status", "--porcelain"):
            return CommandResult(
                0, "" if committed else " M tests/test_widget.py\n", ""
            )
        if arguments == ("rev-parse", "HEAD"):
            return CommandResult(
                0, f"{publication_sha if committed else BASE_SHA}\n", ""
            )
        if arguments[:1] == ("commit",):
            committed = True
            return CommandResult(0, "", "")
        if arguments[:4] == ("rev-list", "--parents", "-n", "1"):
            return CommandResult(0, f"{publication_sha} {BASE_SHA}\n", "")
        if arguments[:1] == ("push",):
            return CommandResult(0, "", "")
        if arguments == ("rev-parse", "origin/agent/issue-81"):
            return CommandResult(0, f"{publication_sha}\n", "")
        return CommandResult(0, "", "")

    repository = GitRepository(
        _config(tmp_path),
        run,
        service_environment={"GITLAB_TOKEN": "dedicated-token"},
    )
    worktree = tmp_path / "worktree"
    repository.snapshot_test_tree(worktree, BASE_SHA, (TEST_PATH,))
    prepared = repository.prepare_commit(
        worktree,
        "agent/issue-81",
        81,
        expected_head=BASE_SHA,
        validated_paths=(TEST_PATH,),
    )
    repository.push_prepared(worktree, "agent/issue-81", prepared)

    commits = [command for command in commands if command[:1] == ("commit",)]
    pushes = [command for command in commands if command[:1] == ("push",)]
    assert commits == [("commit", "-m", "Resolve issue #81")]
    assert pushes == [
        ("push", "origin", f"{publication_sha}:refs/heads/agent/issue-81")
    ]
    assert CHECKPOINT_REF not in " ".join(pushes[0])


def _evidence_file() -> bytes:
    return json.dumps(
        {
            "argv": ["uv", "run", "pytest", f"{TEST_PATH}::test_red"],
            "expected_failure": "assert 1 == 2",
            "test_paths": [TEST_PATH],
        }
    ).encode()


def test_recovery_prompt_exposes_retained_checkpoint_without_publication_authority() -> (
    None
):
    prompt = build_worker_prompt(
        {
            "phase": "workflow",
            "state": "executing",
            "source_branch": "agent/issue-81",
            "source_sha": BASE_SHA,
            "red_checkpoint": _evidence().model_dump(mode="json"),
        },
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=1800,
    )

    assert "retained Red checkpoint" in prompt
    assert TREE_OID in prompt and CHECKPOINT_REF in prompt
    assert "restore" in prompt.casefold()
    assert "Do not commit, push, approve, or merge" in prompt


def test_retained_red_provenance_contract_preserves_complete_identity() -> None:
    """Feedback repair must carry every retained Red identity field unchanged."""
    checkpoint = _evidence()
    identity = checkpoint.model_dump(mode="json")
    assert identity == {
        "schema_version": 1,
        "project_id": 310967,
        "issue_iid": 81,
        "source_branch": "agent/issue-81",
        "base_sha": BASE_SHA,
        "tree_oid": "b" * 40,
        "ref": CHECKPOINT_REF,
        "test_paths": [TEST_PATH],
        "argv": ["uv", "run", "pytest", f"{TEST_PATH}::test_red"],
        "exit_code": 1,
        "expected_failure": "assert 1 == 2",
        "output_sha256": hashlib.sha256(b"assert 1 == 2\n").hexdigest(),
    }
