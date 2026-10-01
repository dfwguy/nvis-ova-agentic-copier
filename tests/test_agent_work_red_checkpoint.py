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
import os
from collections.abc import Callable
from dataclasses import replace
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


def _preflight_success(*details: str) -> CommandResult:
    return CommandResult(
        0,
        json.dumps(
            {
                "residual_diagnostics": ["unresolved-attribute"] * len(details),
                "residual_details": list(details),
            }
        ),
        "",
    )


class FakeDependencies:
    """Injected trusted boundaries for one checkpoint transaction."""

    def __init__(self, tmp_path: Path) -> None:
        self.record = _record(tmp_path)
        self.head = BASE_SHA
        self.paths: tuple[str, ...] = (TEST_PATH,)
        self.command_result = CommandResult(1, "assert 1 == 2\n", "")
        self.after_run: Callable[[], None] | None = None
        self.after_preflight: Callable[[], None] | None = None
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

    def run_red_preflight(
        self, worktree: Path, paths: tuple[str, ...], tree: str
    ) -> CommandResult:
        self.calls.append(("preflight", (worktree, paths, tree)))
        if self.after_preflight is not None:
            self.after_preflight()
        return _preflight_success()

    def read_evidence(
        self, project_id: int, issue_iid: int, base_sha: str
    ) -> Any | None:
        self.calls.append(("read", (project_id, issue_iid, base_sha)))
        return self.retained

    def write_evidence(self, evidence: Any) -> None:
        self.calls.append(("write", evidence))
        self.retained = evidence

    def verify_test_assets(self, worktree: Path, paths: tuple[str, ...]) -> None:
        self.calls.append(("assets", (worktree, paths)))

    def value(self, *, with_preflight: bool = False) -> Any:
        checkpoint = _checkpoint_module()
        return checkpoint.RedCheckpointDependencies(
            load_record=self.load_record,
            current_head=self.current_head,
            changed_paths=self.changed_paths,
            run_red_command=self.run_red_command,
            snapshot_test_tree=self.snapshot_test_tree,
            expected_tree_oid=self.snapshot_result,
            verify_test_assets=self.verify_test_assets,
            read_evidence=self.read_evidence,
            write_evidence=self.write_evidence,
            **({"run_red_preflight": self.run_red_preflight} if with_preflight else {}),
        )


def test_red_checkpoint_apply_reruns_failure_then_snapshots_and_records(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        dependencies=dependencies.value(with_preflight=True),
    )

    assert evidence == _evidence()
    names = [name for name, _value in dependencies.calls]
    assert names.index("run") < names.index("snapshot") < names.index("preflight")
    assert names.index("preflight") < names.index("write")
    rechecks = names[names.index("preflight") + 1 : names.index("write")]
    assert {"head", "paths", "snapshot"} <= set(rechecks)


@pytest.mark.parametrize("preflight_exit", (0, 1))
def test_red_checkpoint_preflights_exact_test_tree_before_retaining_ref(
    tmp_path: Path, preflight_exit: int
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    def preflight(worktree: Path, paths: tuple[str, ...], tree: str) -> CommandResult:
        dependencies.calls.append(("preflight", (worktree, paths, tree)))
        return (
            _preflight_success()
            if not preflight_exit
            else CommandResult(1, "", "intrinsic test error")
        )

    injected = checkpoint.RedCheckpointDependencies(
        **{**vars(dependencies.value()), "run_red_preflight": preflight}
    )
    if preflight_exit:
        with pytest.raises(checkpoint.RedCheckpointError, match="preflight"):
            checkpoint.create_red_checkpoint(
                issue_iid=81, request=_request(), dependencies=injected
            )
        assert dependencies.retained is None
    else:
        evidence = checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=injected
        )
        assert evidence.tree_oid == TREE_OID

    names = [name for name, _value in dependencies.calls]
    assert dependencies.calls[names.index("preflight")] == (
        "preflight",
        (dependencies.record.worktree, (TEST_PATH,), TREE_OID),
    )
    assert names.index("snapshot") < names.index("preflight")
    if not preflight_exit:
        assert names.index("preflight") < names.index("write")


def test_red_checkpoint_corrected_preflight_retry_retains_once(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    outcomes = iter((1, 0))

    def preflight(
        _worktree: Path, _paths: tuple[str, ...], _tree: str
    ) -> CommandResult:
        return (
            CommandResult(1, "", "intrinsic test error")
            if next(outcomes)
            else _preflight_success()
        )

    injected = checkpoint.RedCheckpointDependencies(
        **{**vars(dependencies.value()), "run_red_preflight": preflight}
    )
    with pytest.raises(checkpoint.RedCheckpointError, match="preflight"):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=injected
        )
    assert dependencies.retained is None

    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81, request=_request(), dependencies=injected
    )

    assert dependencies.retained == evidence
    assert [name for name, _value in dependencies.calls].count("write") == 1


def test_red_checkpoint_requires_preflight_boundary_before_retaining_ref(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    missing = replace(dependencies.value(), run_red_preflight=None)

    with pytest.raises(checkpoint.RedCheckpointError, match="preflight"):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=missing
        )

    assert dependencies.retained is None


@pytest.mark.parametrize("mutation", ("head", "paths", "tree"))
def test_red_checkpoint_rejects_candidate_mutated_by_successful_preflight(
    tmp_path: Path, mutation: str
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    def mutate() -> None:
        if mutation == "head":
            dependencies.head = "c" * 40
        elif mutation == "paths":
            dependencies.paths = (TEST_PATH, "tests/test_added.py")
        else:
            dependencies.snapshot_result = "c" * 40

    dependencies.after_preflight = mutate
    with pytest.raises(checkpoint.RedCheckpointError, match="changed"):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(),
            dependencies=dependencies.value(with_preflight=True),
        )

    assert dependencies.retained is None


def test_red_checkpoint_retains_allowed_missing_api_details(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    detail = "tests/test_widget.py:4:agent_work.red_checkpoint.planned_api"
    injected = replace(
        dependencies.value(),
        run_red_preflight=lambda _worktree, _paths, _tree: _preflight_success(detail),
    )

    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81, request=_request(), dependencies=injected
    )

    assert evidence.residual_details == (detail,)
    assert dependencies.retained == evidence


def test_red_checkpoint_rejects_malformed_success_payload(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    injected = replace(
        dependencies.value(),
        run_red_preflight=lambda _worktree, _paths, _tree: CommandResult(0, "[]", ""),
    )

    with pytest.raises(checkpoint.RedCheckpointError, match="preflight"):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=injected
        )

    assert dependencies.retained is None


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
            dependencies=dependencies.value(with_preflight=True),
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
        if "ls-files" in command:
            return CommandResult(0, f"100644 {TREE_OID} 0\t{TEST_PATH}\0", "")
        if "write-tree" in command:
            return CommandResult(0, f"{TREE_OID}\n", "")
        if "rev-parse" in command and CHECKPOINT_REF in " ".join(command):
            return CommandResult(1, "", "")
        return CommandResult(0, "", "")

    asset = tmp_path / "worktree" / TEST_PATH
    asset.parent.mkdir(parents=True)
    asset.write_text("assert False\n")
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


@pytest.mark.parametrize("near_bound", (False, True))
def test_repository_checkpoint_ref_uses_one_cas_commit_and_exact_readback(
    tmp_path: Path, *, near_bound: bool
) -> None:
    """Injected Git responses prove the new ref protocol without a subprocess."""
    evidence = _evidence()
    if near_bound:
        payload = evidence.model_dump(mode="json", exclude_none=True)
        payload["residual_details"] = [""]
        raw = json.dumps(payload, separators=(",", ":")).encode()
        payload["residual_details"] = ["x" * (32767 - len(raw))]
        evidence = _checkpoint_module().RedCheckpointEvidence.model_validate(payload)
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
        evidence.model_dump_json(exclude_none=True),
    )
    assert commit_arguments is not None
    assert len(commit_arguments[5].encode()) + 1 <= 32768
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
        if "ls-files" in arguments:
            return CommandResult(0, f"100644 {TREE_OID} 0\t{TEST_PATH}\0", "")
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
    asset = worktree / TEST_PATH
    asset.parent.mkdir(parents=True)
    asset.write_text("assert False\n")
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
        retained_red_checkpoint=_evidence(),
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
        "residual_details": [],
        "architect_risk_intent": None,
    }


_RISK_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=100,
).filter(lambda value: bool(value.strip()))


def _risk_intent(
    scenario: str = "Restart after Red", invariant: str = "Keep identity"
) -> Any:
    checkpoint = _checkpoint_module()
    return checkpoint.ArchitectRiskIntent(
        risks=(
            checkpoint.ArchitectRiskCase(
                kind="state", scenario=scenario, invariant=invariant
            ),
            checkpoint.ArchitectRiskCase(
                kind="protocol",
                scenario="Replay result",
                invariant="Reject stale nonce",
            ),
        )
    )


@given(scenario=_RISK_TEXT, invariant=_RISK_TEXT)
def test_architect_risk_intent_roundtrip_and_request_validation_are_deterministic(
    scenario: str, invariant: str
) -> None:
    checkpoint = _checkpoint_module()
    intent = _risk_intent(scenario, invariant)
    encoded = intent.model_dump_json()
    assert checkpoint.ArchitectRiskIntent.model_validate_json(encoded) == intent
    assert intent.model_dump_json() == encoded
    request = _request(architect_risk_intent=intent)
    validated = checkpoint.validate_red_checkpoint_request(request)
    assert validated == request
    assert validated.architect_risk_intent == intent
    assert checkpoint.validate_red_checkpoint_request(validated) == validated


@pytest.mark.parametrize(
    "changes",
    (
        {"kind": "approval"},
        {"scenario": ""},
        {"scenario": " \n\t"},
        {"invariant": "\x00"},
        {"scenario": "restart\x00now"},
        {"invariant": " "},
        {"project_id": 310967},
        {"approved": True},
        {"scenario": 12},
    ),
)
def test_architect_risk_case_rejects_invalid_or_authoritative_fields(
    changes: dict[str, object],
) -> None:
    checkpoint = _checkpoint_module()
    with pytest.raises(ValueError):
        checkpoint.ArchitectRiskCase.model_validate(
            {"kind": "state", "scenario": "Restart", "invariant": "Keep identity"}
            | changes
        )


@pytest.mark.parametrize(
    "payload",
    (
        {"risks": []},
        {
            "risks": [
                {"kind": "state", "scenario": "Restart", "invariant": "Keep identity"}
            ],
            "nonce": "private",
        },
    ),
    ids=("empty-risks", "private-field-with-valid-risk"),
)
def test_architect_risk_intent_requires_nonempty_closed_shape(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _checkpoint_module().ArchitectRiskIntent.model_validate(payload)


@pytest.mark.parametrize(
    "scenario",
    ("x" * 4096, "😀" * 1024),
    ids=("long-ascii-within-budget", "unicode-within-budget"),
)
def test_architect_risk_intent_uses_aggregate_budget_without_individual_text_cap(
    scenario: str,
) -> None:
    intent = _risk_intent(scenario)
    assert intent.risks[0].scenario == scenario
    assert _evidence(architect_risk_intent=intent).architect_risk_intent == intent


def test_architect_risk_intent_allows_many_small_cases_within_evidence_budget() -> None:
    checkpoint = _checkpoint_module()
    case = checkpoint.ArchitectRiskCase(kind="state", scenario="s", invariant="i")
    intent = checkpoint.ArchitectRiskIntent(risks=(case,) * 129)
    assert (
        len(_evidence(architect_risk_intent=intent).architect_risk_intent.risks) == 129
    )


@pytest.mark.parametrize(
    "scenario", ("x" * 32768, "😀" * 8192), ids=("ascii-overflow", "unicode-overflow")
)
def test_architect_risk_intent_rejects_utf8_aggregate_overflow(scenario: str) -> None:
    with pytest.raises(ValueError):
        _risk_intent(scenario)


def test_red_checkpoint_full_evidence_bound_accounts_for_nested_intent_before_write(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    intent = _risk_intent("x" * 32500)
    assert len(intent.model_dump_json().encode()) <= 32768
    payload = _evidence().model_dump(mode="json") | {
        "architect_risk_intent": intent.model_dump(mode="json")
    }
    assert (
        len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        > 32768
    )
    dependencies = FakeDependencies(tmp_path)
    with pytest.raises(RedCheckpointError):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=_request(architect_risk_intent=intent),
            dependencies=dependencies.value(with_preflight=True),
        )
    assert not any(name == "write" for name, _value in dependencies.calls)


def test_red_checkpoint_captures_intent_and_exact_retry_does_not_rewrite(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    request = _request(architect_risk_intent=_risk_intent())
    evidence = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=request,
        dependencies=dependencies.value(with_preflight=True),
    )
    assert evidence.architect_risk_intent == request.architect_risk_intent
    dependencies.calls.clear()
    assert (
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=request,
            dependencies=dependencies.value(with_preflight=True),
        )
        == evidence
    )
    assert not any(name in {"run", "write"} for name, _value in dependencies.calls)


@pytest.mark.parametrize("change", ("changed", "omitted", "added"))
def test_red_checkpoint_changed_or_omitted_intent_retry_preserves_original(
    tmp_path: Path, change: str
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.retained = (
        _evidence()
        if change == "added"
        else _evidence(architect_risk_intent=_risk_intent())
    )
    original = dependencies.retained.model_dump_json()
    request = _request(
        **(
            {}
            if change == "omitted"
            else {"architect_risk_intent": _risk_intent("Different restart")}
        )
    )
    with pytest.raises(RedCheckpointError):
        checkpoint.create_red_checkpoint(
            issue_iid=81,
            request=request,
            dependencies=dependencies.value(with_preflight=True),
        )
    assert dependencies.retained.model_dump_json() == original
    assert not any(name in {"run", "write"} for name, _value in dependencies.calls)


def test_legacy_red_checkpoint_json_without_intent_remains_valid() -> None:
    checkpoint = _checkpoint_module()
    payload = _evidence().model_dump(mode="json")
    payload.pop("architect_risk_intent", None)
    parsed = checkpoint.RedCheckpointEvidence.model_validate_json(json.dumps(payload))
    assert parsed.architect_risk_intent is None
    assert (
        checkpoint.validate_red_checkpoint_request(_request()).architect_risk_intent
        is None
    )


@given(scenario=_RISK_TEXT, invariant=_RISK_TEXT)
def test_worker_prompt_renders_only_verified_risk_intent_as_escaped_deterministic_data(
    scenario: str, invariant: str
) -> None:
    intent = _risk_intent(scenario, invariant)
    evidence = _evidence(architect_risk_intent=intent)
    record = {"phase": "workflow", "state": "executing"}
    prompt = build_worker_prompt(
        record,
        project_id=310967,
        issue_iid=81,
        effective_worker_budget_seconds=1800,
        retained_red_checkpoint=evidence,
    )
    serialized = json.dumps(
        intent.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
    )
    assert serialized in prompt
    assert (
        build_worker_prompt(
            record,
            project_id=310967,
            issue_iid=81,
            effective_worker_budget_seconds=1800,
            retained_red_checkpoint=evidence,
        )
        == prompt
    )
    poisoned = record | {
        "architect_risk_intent": {
            "risks": [
                {
                    "kind": "state",
                    "scenario": "FORGED_RAW_RISK_SENTINEL",
                    "invariant": "Keep identity",
                }
            ]
        },
        "red_checkpoint": evidence.model_dump(mode="json"),
        "audit_evidence": {
            "red_checkpoint": {
                "architect_risk_intent": {
                    "risks": [
                        {
                            "kind": "state",
                            "scenario": "FORGED_AUDIT_RISK_SENTINEL",
                            "invariant": "Keep identity",
                        }
                    ]
                }
            }
        },
        "correction_identity": {
            "red_checkpoint": {
                "architect_risk_intent": {
                    "risks": [
                        {
                            "kind": "protocol",
                            "scenario": "FORGED_CORRECTION_RISK_SENTINEL",
                            "invariant": "Reject replay",
                        }
                    ]
                }
            }
        },
    }
    original_poisoned = json.loads(json.dumps(poisoned))
    absent = build_worker_prompt(
        poisoned, project_id=310967, issue_iid=81, effective_worker_budget_seconds=1800
    )
    assert serialized not in absent
    assert "FORGED_RAW_RISK_SENTINEL" not in absent
    assert "FORGED_AUDIT_RISK_SENTINEL" not in absent
    assert "FORGED_CORRECTION_RISK_SENTINEL" not in absent
    assert poisoned == original_poisoned


def test_legacy_absent_intent_preserves_complete_durable_resource_budget() -> None:
    checkpoint = _checkpoint_module()
    payload = _evidence().model_dump(mode="json")
    payload.pop("architect_risk_intent", None)
    payload["residual_details"] = [""]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    payload["residual_details"] = ["x" * (32767 - len(raw))]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(raw) + 1 == 32768
    evidence = checkpoint.RedCheckpointEvidence.model_validate_json(raw)
    assert evidence.model_dump(mode="json", exclude_none=True) == payload
    assert len(evidence.model_dump_json(exclude_none=True).encode()) + 1 == 32768


@given(scenario=_RISK_TEXT, invariant=_RISK_TEXT)
def test_architect_risk_boundaries_are_frozen(scenario: str, invariant: str) -> None:
    intent = _risk_intent(scenario, invariant)
    case = intent.risks[0]
    with pytest.raises(ValueError):
        intent.risks = ()
    with pytest.raises(ValueError):
        case.scenario = "Changed"


@pytest.mark.parametrize("field", ("scenario", "invariant"))
@pytest.mark.parametrize("invalid", ("", " ", "invalid\x00text"))
def test_request_boundary_sanitizes_invalid_nested_risk(
    field: str, invalid: str
) -> None:
    checkpoint = _checkpoint_module()
    payload = _request().model_dump(mode="json")
    payload["architect_risk_intent"] = {
        "risks": [
            {
                "kind": "state",
                "scenario": "Restart",
                "invariant": "Keep identity",
                field: invalid,
            }
        ]
    }
    with pytest.raises(RedCheckpointError):
        checkpoint.validate_red_checkpoint_request(
            checkpoint.RedCheckpointRequest.model_construct(**payload)
        )


@pytest.mark.parametrize(
    "extra",
    (
        "tests/test_undeclared.py",
        "src/agent_work/runtime.py",
        "template/tests/test_x.py",
        "../sibling/tests/test_x.py",
        "--collect-only",
        "-k",
        "--",
        "@targets",
        f"{TEST_PATH}::",
        f"{TEST_PATH}::::test_red",
        f"{TEST_PATH}::test_red\n",
        f"{TEST_PATH}::test_red\\escape",
    ),
)
def test_checkpoint_target_authority_rejects_every_extra_unbound_argument(
    extra: str,
) -> None:
    checkpoint = _checkpoint_module()
    with pytest.raises(RedCheckpointError):
        checkpoint.validate_red_checkpoint_request(
            _request(argv=("uv", "run", "pytest", TEST_PATH, extra))
        )


@given(st.sampled_from(("\\", "\x00", "\x01", "\t", "\n", "\r", "\x7f")))
def test_checkpoint_manifest_rejects_generated_controls_and_alternate_separators(
    control: str,
) -> None:
    checkpoint = _checkpoint_module()
    path = f"tests/test_bad{control}asset.py"
    with pytest.raises(RedCheckpointError):
        checkpoint.validate_red_checkpoint_request(
            _request(argv=("uv", "run", "pytest", path), test_paths=(path,))
        )


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20))
def test_checkpoint_generated_undeclared_target_never_expands_authority(
    stem: str,
) -> None:
    checkpoint = _checkpoint_module()
    with pytest.raises(RedCheckpointError):
        checkpoint.validate_red_checkpoint_request(
            _request(argv=("uv", "run", "pytest", TEST_PATH, f"tests/other_{stem}.py"))
        )


@given(
    st.permutations((TEST_PATH, f"{TEST_PATH}::TestWidget::test_red[param]", TEST_PATH))
)
def test_checkpoint_target_permutations_and_duplicates_preserve_manifest(
    targets: list[str],
) -> None:
    checkpoint = _checkpoint_module()
    result = checkpoint.validate_red_checkpoint_request(
        _request(argv=("uv", "run", "pytest", *targets))
    )
    assert {target.split("::", 1)[0] for target in result.argv[3:]} == set(
        result.test_paths
    )


@pytest.mark.parametrize(
    "stage",
    ("missing", "initial", "after_run", "after_snapshot", "after_preflight", "replay"),
)
def test_checkpoint_refuses_unverified_assets_without_retained_evidence(
    tmp_path: Path, stage: str
) -> None:
    checkpoint = _checkpoint_module()
    fake = FakeDependencies(tmp_path)
    bad_asset = stage in {"initial", "replay"}
    if stage == "replay":
        fake.retained = _evidence()

    def invalidate() -> None:
        nonlocal bad_asset
        bad_asset = True

    def verify(_worktree: Path, _paths: tuple[str, ...]) -> None:
        if bad_asset:
            raise RepositoryError("test asset topology is invalid")

    fake.after_run = invalidate if stage == "after_run" else None
    fake.after_preflight = invalidate if stage == "after_preflight" else None

    def snapshot(base: str, paths: tuple[str, ...]) -> str:
        tree = fake.snapshot_test_tree(base, paths)
        if stage == "after_snapshot":
            invalidate()
        return tree

    dependencies = replace(
        fake.value(with_preflight=True),
        verify_test_assets=None if stage == "missing" else verify,
        snapshot_test_tree=snapshot,
    )
    with pytest.raises(RedCheckpointError):
        checkpoint.create_red_checkpoint(
            issue_iid=81, request=_request(), dependencies=dependencies
        )
    names = [name for name, _ in fake.calls]
    assert "write" not in names
    if stage in {"missing", "initial", "replay"}:
        assert "run" not in names
    if stage == "after_run":
        assert "snapshot" not in names
    if stage == "after_snapshot":
        assert "preflight" not in names
    if stage == "after_preflight":
        assert "preflight" in names


@pytest.mark.parametrize("stream", ("stdout", "stderr"))
def test_checkpoint_asset_checks_preserve_exact_failure_stream(
    tmp_path: Path, stream: str
) -> None:
    fake = FakeDependencies(tmp_path)
    fake.command_result = CommandResult(
        1,
        "assert 1 == 2\n" if stream == "stdout" else "",
        "assert 1 == 2\n" if stream == "stderr" else "",
    )
    evidence = _checkpoint_module().create_red_checkpoint(
        issue_iid=81, request=_request(), dependencies=fake.value(with_preflight=True)
    )
    assert evidence.output_sha256 == hashlib.sha256(b"assert 1 == 2\n").hexdigest()
    assert evidence.residual_details == ()
    assert sum(name == "assets" for name, _ in fake.calls) >= 4


@pytest.mark.parametrize(
    "defect", ("leaf_link", "ancestor_link", "directory", "fifo", "missing_parent")
)
def test_repository_asset_verifier_rejects_untrusted_topology(
    tmp_path: Path, defect: str
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    target = worktree / TEST_PATH
    external = tmp_path / "external"
    external.mkdir()
    (external / target.name).write_text("assert False\n")
    if defect == "ancestor_link":
        (worktree / "tests").symlink_to(external, target_is_directory=True)
    elif defect != "missing_parent":
        target.parent.mkdir()
        if defect == "leaf_link":
            target.symlink_to(external / target.name)
        elif defect == "directory":
            target.mkdir()
        else:
            os.mkfifo(target)
    repository: Any = GitRepository(
        _config(tmp_path),
        lambda *_args, **_kwargs: pytest.fail(
            "asset verification must not execute Git"
        ),
    )
    with pytest.raises(RepositoryError):
        repository.verify_test_assets(worktree, (TEST_PATH,))


@pytest.mark.parametrize("deleted", (False, True))
def test_repository_asset_verifier_accepts_regular_or_deleted_leaf_below_real_directories(
    tmp_path: Path, *, deleted: bool
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    if not deleted:
        target.write_text("assert False\n")
    repository: Any = GitRepository(
        _config(tmp_path),
        lambda *_args, **_kwargs: pytest.fail(
            "asset verification must not execute Git"
        ),
    )
    assert repository.verify_test_assets(tmp_path / "worktree", (TEST_PATH,)) is None


@pytest.mark.parametrize("mode", ("100644", "100755", "120000", "160000"))
def test_repository_snapshot_requires_regular_staged_test_modes(
    tmp_path: Path, mode: str
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")
    commands: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        commands.append(command)
        if "ls-files" in command:
            return CommandResult(0, f"{mode} {TREE_OID} 0\t{TEST_PATH}\0", "")
        if "write-tree" in command:
            return CommandResult(0, TREE_OID, "")
        return CommandResult(0, "", "")

    repository = GitRepository(_config(tmp_path), run)
    if mode in {"120000", "160000"}:
        with pytest.raises(RepositoryError):
            repository.snapshot_test_tree(tmp_path / "worktree", BASE_SHA, (TEST_PATH,))
    else:
        assert (
            repository.snapshot_test_tree(tmp_path / "worktree", BASE_SHA, (TEST_PATH,))
            == TREE_OID
        )
        assert any("ls-files" in command for command in commands)


@pytest.mark.parametrize("swap_at", ("add", "write-tree"))
def test_repository_snapshot_rechecks_topology_after_actual_git_boundary(
    tmp_path: Path, swap_at: str
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")
    external = tmp_path / "external.py"
    external.write_text("private fixture\n")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        if swap_at in command:
            target.unlink()
            target.symlink_to(external)
        if "ls-files" in command:
            return CommandResult(0, f"100644 {TREE_OID} 0\t{TEST_PATH}\0", "")
        return CommandResult(0, TREE_OID if "write-tree" in command else "", "")

    with pytest.raises(RepositoryError):
        GitRepository(_config(tmp_path), run).snapshot_test_tree(
            tmp_path / "worktree", BASE_SHA, (TEST_PATH,)
        )


def test_repository_generic_candidate_capture_preserves_content_paths(
    tmp_path: Path,
) -> None:
    target = tmp_path / "worktree" / "docs" / "reference.md"
    target.parent.mkdir(parents=True)
    target.write_text("Reference\n")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        if "ls-files" in command:
            return CommandResult(0, f"100644 {TREE_OID} 0\tdocs/reference.md\0", "")
        return CommandResult(0, TREE_OID if "write-tree" in command else "", "")

    assert (
        GitRepository(_config(tmp_path), run).red_candidate_tree(
            tmp_path / "worktree", BASE_SHA, ("docs/reference.md",)
        )
        == TREE_OID
    )


def test_repository_snapshot_rejects_staged_mode_swap_during_tree_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")
    mode = "100644"

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        nonlocal mode
        if "ls-files" in command:
            return CommandResult(0, f"{mode} {TREE_OID} 0\t{TEST_PATH}\0", "")
        if "write-tree" in command:
            mode = "120000"
            return CommandResult(0, TREE_OID, "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError):
        GitRepository(_config(tmp_path), run).snapshot_test_tree(
            tmp_path / "worktree", BASE_SHA, (TEST_PATH,)
        )


def test_repository_snapshot_preserves_declared_test_deletion(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    (worktree / "tests").mkdir(parents=True)

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        return CommandResult(0, TREE_OID if "write-tree" in command else "", "")

    assert (
        GitRepository(_config(tmp_path), run).snapshot_test_tree(
            worktree, BASE_SHA, (TEST_PATH,)
        )
        == TREE_OID
    )


def test_repository_snapshot_refuses_surviving_test_missing_from_staged_manifest(
    tmp_path: Path,
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        return CommandResult(0, TREE_OID if "write-tree" in command else "", "")

    with pytest.raises(RepositoryError):
        GitRepository(_config(tmp_path), run).snapshot_test_tree(
            tmp_path / "worktree", BASE_SHA, (TEST_PATH,)
        )


@pytest.mark.parametrize(
    "entry",
    (
        "",
        "not a stage record\0",
        f"100644 {TREE_OID} 1\t{TEST_PATH}\0",
        f"100644 {TREE_OID} 0\t{TEST_PATH}\0" * 2,
        f"100644 not-an-oid 0\t{TEST_PATH}\0",
    ),
)
def test_repository_snapshot_rejects_missing_or_ambiguous_surviving_asset_stage_records(
    tmp_path: Path, entry: str
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")

    def run(command: tuple[str, ...], **_kwargs: Any) -> CommandResult:
        if "ls-files" in command:
            return CommandResult(0, entry, "")
        return CommandResult(0, TREE_OID if "write-tree" in command else "", "")

    with pytest.raises(RepositoryError):
        GitRepository(_config(tmp_path), run).snapshot_test_tree(
            tmp_path / "worktree", BASE_SHA, (TEST_PATH,)
        )


def test_repository_asset_verifier_uses_injected_nonfollowing_filesystem_observation(
    tmp_path: Path,
) -> None:
    target = tmp_path / "worktree" / TEST_PATH
    target.parent.mkdir(parents=True)
    target.write_text("assert False\n")
    observations: list[Path] = []

    def observe(path: Path) -> os.stat_result:
        observations.append(path)
        return path.lstat()

    constructor: Any = GitRepository
    repository = constructor(
        _config(tmp_path),
        lambda *_args, **_kwargs: pytest.fail("asset checks cannot execute Git"),
        asset_lstat=observe,
    )
    repository.verify_test_assets(tmp_path / "worktree", (TEST_PATH,))
    assert target in observations
    assert target.parent in observations
