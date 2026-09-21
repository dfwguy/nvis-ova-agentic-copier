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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work.cli import build_parser, main
from agent_work.domain import RunnerState, WorkflowLane, WorkflowPhase, WorkKind
from agent_work.models import AgentWorkConfig, LedgerRecord
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
        "workflow_lane": WorkflowLane.CODING,
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
        return self.command_result

    def snapshot_tests(self, base_sha: str, paths: tuple[str, ...], ref: str) -> str:
        self.calls.append(("snapshot", (base_sha, paths, ref)))
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
            snapshot_tests=self.snapshot_tests,
            read_evidence=self.read_evidence,
            write_evidence=self.write_evidence,
        )


def test_red_checkpoint_preview_validates_without_running_or_mutating(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    report = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        apply=False,
        dependencies=dependencies.value(),
    )

    assert report.applied is False
    assert report.action == "would-create"
    assert report.evidence is None
    assert [name for name, _value in dependencies.calls] == [
        "load",
        "head",
        "paths",
        "read",
    ]


def test_red_checkpoint_apply_reruns_failure_then_snapshots_and_records(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)

    report = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        apply=True,
        dependencies=dependencies.value(),
    )

    assert report.applied is True and report.action == "created"
    assert report.evidence == _evidence()
    assert [name for name, _value in dependencies.calls] == [
        "load",
        "head",
        "paths",
        "read",
        "run",
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
            apply=True,
            dependencies=dependencies.value(),
        )

    assert "snapshot" in [name for name, _value in dependencies.calls]
    assert "write" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_exact_replay_is_idempotent(tmp_path: Path) -> None:
    checkpoint = _checkpoint_module()
    dependencies = FakeDependencies(tmp_path)
    dependencies.retained = _evidence()

    report = checkpoint.create_red_checkpoint(
        issue_iid=81,
        request=_request(),
        apply=True,
        dependencies=dependencies.value(),
    )

    assert report.action == "existing" and report.evidence == dependencies.retained
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
            apply=True,
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
            apply=True,
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
            apply=True,
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
            apply=True,
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
            apply=True,
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
            apply=True,
            dependencies=dependencies.value(),
        )

    assert "snapshot" not in [name for name, _value in dependencies.calls]


def test_red_checkpoint_store_is_atomic_idempotent_and_fails_closed(
    tmp_path: Path,
) -> None:
    checkpoint = _checkpoint_module()
    store = checkpoint.RedCheckpointStore(tmp_path)
    evidence = _evidence()

    store.write(evidence)
    store.write(evidence)

    assert store.read(310967, 81, BASE_SHA) == evidence
    target = tmp_path / "red-checkpoints" / "310967" / "81" / f"{BASE_SHA}.json"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not tuple(target.parent.glob(".checkpoint-*"))

    with pytest.raises(checkpoint.RedCheckpointError, match="conflict"):
        store.write(evidence.model_copy(update={"expected_failure": "different"}))
    assert store.read(310967, 81, BASE_SHA) == evidence
    assert not tuple(target.parent.glob(".checkpoint-*"))

    target.write_bytes(b"not-json")
    with pytest.raises(checkpoint.RedCheckpointError, match="malformed"):
        store.read(310967, 81, BASE_SHA)


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


def test_repository_snapshots_declared_tests_with_a_temporary_index_and_local_ref(
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

    tree = GitRepository(_config(tmp_path), run).snapshot_tests(
        tmp_path / "worktree", BASE_SHA, (TEST_PATH,), CHECKPOINT_REF
    )

    assert tree == TREE_OID
    rendered = [command[3:] for command, _kwargs in commands]
    assert ("read-tree", BASE_SHA) in rendered
    assert ("add", "--", TEST_PATH) in rendered
    assert ("write-tree",) in rendered
    assert any(
        command[:2] == ("update-ref", CHECKPOINT_REF) and TREE_OID in command
        for command in rendered
    )
    assert all(
        "GIT_INDEX_FILE" in kwargs["environment"]
        for command, kwargs in commands
        if command[3:4] in {("read-tree",), ("add",), ("write-tree",)}
    )
    assert not any(
        action in command for command in rendered for action in ("commit", "push")
    )


def test_repository_rejects_a_checkpoint_ref_bound_to_another_tree(
    tmp_path: Path,
) -> None:
    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if "write-tree" in command:
            return CommandResult(0, f"{TREE_OID}\n", "")
        if "rev-parse" in command and CHECKPOINT_REF in " ".join(command):
            return CommandResult(0, f"{'c' * 40}\n", "")
        return CommandResult(0, "", "")

    with pytest.raises(RepositoryError, match="checkpoint ref"):
        GitRepository(_config(tmp_path), run).snapshot_tests(
            tmp_path / "worktree", BASE_SHA, (TEST_PATH,), CHECKPOINT_REF
        )


def test_checkpoint_ref_never_changes_one_commit_final_publication_history(
    tmp_path: Path,
) -> None:
    publication_sha = "d" * 40
    commands: list[tuple[str, ...]] = []
    committed = False

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal committed
        arguments = command[3:]
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
    repository.snapshot_tests(worktree, BASE_SHA, (TEST_PATH,), CHECKPOINT_REF)
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


def test_red_checkpoint_cli_preview_and_apply_are_injected_and_json_bounded() -> None:
    checkpoint = _checkpoint_module()
    calls: list[tuple[int, Any, bool]] = []
    output: list[str] = []

    def create(*, issue_iid: int, request: Any, apply: bool) -> Any:
        calls.append((issue_iid, request, apply))
        return SimpleNamespace(
            applied=apply,
            action="created" if apply else "would-create",
            evidence=_evidence() if apply else None,
        )

    arguments = [
        "red-checkpoint",
        "create",
        "--issue-iid",
        "81",
        "--evidence-file",
        "red.json",
        "--json",
    ]
    assert (
        main(
            arguments,
            read_bytes=lambda _path: _evidence_file(),
            create_red_checkpoint=create,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [(81, _request(), False)]
    assert json.loads(output.pop()) == {"action": "would-create", "applied": False}

    assert (
        main(
            [*arguments, "--apply"],
            read_bytes=lambda _path: _evidence_file(),
            create_red_checkpoint=create,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls[-1] == (81, _request(), True)
    payload = json.loads(output.pop())
    assert payload["tree_oid"] == TREE_OID
    assert payload["ref"] == CHECKPOINT_REF
    assert "output" not in payload
    assert checkpoint.RedCheckpointRequest.model_validate_json(_evidence_file())


def test_red_checkpoint_cli_help_errors_and_boundary_failure_exit_codes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        build_parser().parse_args(["red-checkpoint", "create", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--evidence-file" in help_text
    assert "without --apply" in help_text
    assert "sanitized checkpoint diagnostics" in help_text

    with pytest.raises(SystemExit) as invalid_exit:
        build_parser().parse_args(["red-checkpoint", "create"])
    assert invalid_exit.value.code == 2

    checkpoint = _checkpoint_module()
    errors: list[str] = []

    def fail(**_kwargs: object) -> Any:
        raise checkpoint.RedCheckpointError("red checkpoint rejected")

    assert (
        main(
            [
                "red-checkpoint",
                "create",
                "--issue-iid",
                "81",
                "--evidence-file",
                "red.json",
            ],
            read_bytes=lambda _path: _evidence_file(),
            create_red_checkpoint=fail,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["red checkpoint rejected"]


def test_red_checkpoint_cli_debug_never_exposes_rejected_evidence() -> None:
    secret = "DO-NOT-PRINT-THIS-MARKER"
    errors: list[str] = []

    exit_code = main(
        [
            "red-checkpoint",
            "create",
            "--issue-iid",
            "81",
            "--evidence-file",
            "red.json",
            "--debug",
        ],
        read_bytes=lambda _path: json.dumps(
            {
                "argv": ["unsafe", secret],
                "expected_failure": secret,
                "test_paths": [TEST_PATH],
            }
        ).encode(),
        write_stderr=errors.append,
    )

    assert exit_code == 1
    assert errors
    assert secret not in "\n".join(errors)


def test_red_checkpoint_cli_rejects_oversized_raw_evidence_before_parsing() -> None:
    errors: list[str] = []

    assert (
        main(
            [
                "red-checkpoint",
                "create",
                "--issue-iid",
                "81",
                "--evidence-file",
                "red.json",
            ],
            read_bytes=lambda _path: b"x" * 32_769,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["red checkpoint evidence file exceeds the input bound"]


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
