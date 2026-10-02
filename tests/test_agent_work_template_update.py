# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the recoverable Copier template-update command."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Never, cast

import pytest
from pydantic import ValidationError

from agent_work import template_update
from agent_work.cli import main
from agent_work.models import AgentWorkConfig
from agent_work.template_comparison import CommandResult, TemplateComparison

_CURRENT = "a" * 40
_CANDIDATE = "b" * 40
_BASE = "c" * 40
_SOURCE = "https://git.example.test/factory/template.git"


class _NoSideEffects:
    """Fail the test if a read-only plan crosses any mutation boundary."""

    def __getattr__(self, name: str) -> Never:
        pytest.fail(f"dry-run reached forbidden boundary: {name}")


def _comparison(_answers_path: Path, _candidate_revision: str) -> object:
    """Return the fully resolved comparison identity required by update."""

    return SimpleNamespace(
        source=_SOURCE,
        current_commit=_CURRENT,
        candidate_commit=_CANDIDATE,
        paths=("README.md", "src/widget.py"),
    )


def _report(*, outcome: str, state: str, changed_paths: tuple[str, ...]) -> object:
    """Build only the public report shape consumed by the CLI boundary."""

    return SimpleNamespace(
        outcome=outcome,
        state=state,
        update_id="f" * 64,
        current_commit=_CURRENT,
        candidate_commit=_CANDIDATE,
        branch=None,
        worktree=None,
        changed_paths=changed_paths,
        conflict_paths=(),
        recovery_stage=None,
        next_action="review plan",
        completion=None,
    )


def _completion(
    *,
    lane: str = "content",
    paths: tuple[str, ...],
    junit_artifacts: list[dict[str, object]] | None = None,
) -> bytes:
    """Return one bounded worker completion envelope for the supplied diff."""

    payload: dict[str, object] = {
        "workflow_lane": lane,
        "workflow_receipts": [
            f"{skill}: approved"
            for skill in template_update.WORKFLOW_RECEIPTS[
                template_update.WorkflowLane(lane)
            ]
        ],
        "changed_paths": list(paths),
        "conflict_disposition": "none",
        "merge_request_draft": {
            "title": "Update generated project from template",
            "description": "Validation is ready for human publication.",
        },
        "audit_receipt": "audit:complete",
        "capacity_receipt": "capacity:reserved",
        "candidate_tree": "e" * 40,
        "junit_artifacts": junit_artifacts or [],
    }
    return json.dumps(payload).encode()


@dataclass
class _ApplyHarness:
    """DI-only system-boundary harness for template-update behavior contracts."""

    state_root: Path
    checkout: template_update.TemplateUpdateCheckout = field(
        default_factory=lambda: template_update.TemplateUpdateCheckout(
            branch="main", head=_BASE, clean=True
        )
    )
    worktree: template_update.TemplateUpdateWorktree = field(
        default_factory=lambda: template_update.TemplateUpdateWorktree(
            branch="agent/template-update-37",
            head=_BASE,
            changed_paths=("README.md", "src/widget.py"),
            conflict_paths=(),
            candidate_tree="e" * 40,
        )
    )
    copier_result: CommandResult = field(
        default_factory=lambda: CommandResult(0, b"", b"")
    )
    copier_error: BaseException | None = None
    worker_result: bytes = field(
        default_factory=lambda: _completion(paths=("README.md", "src/widget.py"))
    )
    loaded_record: template_update.TemplateUpdateRecord | None = None
    artifacts: dict[Path, bytes] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    copier_calls: list[tuple[tuple[str, ...], Path, float]] = field(
        default_factory=list
    )
    worker_requests: list[template_update.TemplateUpdateWorkflowRequest] = field(
        default_factory=list
    )
    observed_worktrees: list[template_update.TemplateUpdateWorktree] = field(
        default_factory=list
    )

    def dependencies(self) -> template_update.TemplateUpdateDependencies:
        """Wire every effect behind the specified injected controller ports."""

        return template_update.TemplateUpdateDependencies(
            project=lambda: template_update.TemplateUpdateProject(
                project_id=37,
                checkout=Path("/generated/widget"),
                default_branch="main",
                state_root=self.state_root,
            ),
            observe_checkout=lambda: self.checkout,
            lock=self._lock,
            load_record=lambda _update_id: self.loaded_record,
            save_record=self._save,
            create_worktree=self._create_worktree,
            run_copier=self._run_copier,
            observe_worktree=self._observe_worktree,
            invoke_workflow=self._invoke_workflow,
            read_artifact=self.artifacts.get,
            state_root=self.state_root,
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.events.append("lock:entered")
        yield
        self.events.append("lock:released")

    def _save(
        self,
        record: template_update.TemplateUpdateRecord,
        expected: template_update.TemplateUpdateRecord | None,
    ) -> None:
        self.events.append(f"save:{record.state.value}")
        assert self.loaded_record is expected
        self.loaded_record = record

    def _create_worktree(self, branch: str, base: str, destination: Path) -> Path:
        self.events.append("worktree:create")
        assert base == _BASE
        assert branch.startswith("agent/template-update-")
        assert destination.is_relative_to(self.state_root / "template-updates")
        return destination

    def _run_copier(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float
    ) -> CommandResult:
        self.events.append("copier:run")
        self.copier_calls.append((argv, cwd, timeout_seconds))
        if self.copier_error is not None:
            raise self.copier_error
        return self.copier_result

    def _invoke_workflow(
        self, request: template_update.TemplateUpdateWorkflowRequest
    ) -> bytes:
        self.events.append("worker:run")
        self.worker_requests.append(request)
        return self.worker_result

    def _observe_worktree(self, _path: Path) -> template_update.TemplateUpdateWorktree:
        """Return each recorded post-boundary observation in order."""

        if self.observed_worktrees:
            return self.observed_worktrees.pop(0)
        return self.worktree


def _apply(harness: _ApplyHarness) -> template_update.TemplateUpdateReport:
    """Run one apply attempt against the reusable fake system boundary."""

    return template_update.execute_template_update(
        candidate_revision="candidate",
        project_root=Path("/generated/widget"),
        apply=True,
        compare=lambda *_args, **_kwargs: TemplateComparison(
            source=_SOURCE,
            current_commit=_CURRENT,
            candidate_commit=_CANDIDATE,
            paths=("README.md", "src/widget.py"),
        ),
        dependencies=harness.dependencies(),
    )


@pytest.mark.parametrize(
    "status",
    (
        "R  old.py\0new.py\0",
        " C  old.py\0new.py\0",
        "RM old.py\0new.py\0",
        "CR old.py\0new.py\0",
        "MR old.py\0new.py\0",
        "MC old.py\0new.py\0",
    ),
)
def test_default_observer_preserves_rename_and_copy_destinations(status: str) -> None:
    """The update observer keeps porcelain continuation paths intact."""

    def run_process(argv: tuple[str, ...], **_kwargs: object) -> object:
        if argv[-3:] == ("status", "--porcelain", "-z"):
            return SimpleNamespace(returncode=0, stdout=status.encode(), stderr=b"")
        if argv[-2:] == ("rev-parse", "HEAD"):
            return SimpleNamespace(returncode=0, stdout=_BASE.encode(), stderr=b"")
        if argv[-2:] == ("write-tree",):
            return SimpleNamespace(returncode=0, stdout=("e" * 40).encode(), stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    dependencies = template_update.default_dependencies(
        project=template_update.TemplateUpdateProject(
            project_id=37,
            checkout=Path("/generated/widget"),
            default_branch="main",
            state_root=Path("/state"),
        ),
        run_process=cast(
            Callable[..., subprocess.CompletedProcess[bytes]], run_process
        ),
    )

    assert dependencies.observe_worktree(Path("/worktree")).changed_paths == (
        "new.py",
        "old.py",
    )


def test_template_update_help_describes_dry_run_and_apply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The new CLI endpoint makes its safe default and mutation gate explicit."""

    with pytest.raises(SystemExit) as raised:
        main(["template", "update", "--help"])

    assert raised.value.code == 0
    help_output = capsys.readouterr().out
    assert "--candidate-revision" in help_output
    assert "--project-root" in help_output
    assert "--apply" in help_output
    assert "dry-run" in help_output.lower()


@pytest.mark.parametrize(
    ("arguments", "expected"),
    (
        (["template", "update"], "--candidate-revision"),
        (
            ["template", "update", "--candidate-revision", "candidate", "--unknown"],
            "unrecognized arguments: --unknown",
        ),
    ),
)
def test_template_update_rejects_invalid_cli_input_with_exit_two(
    arguments: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Argument errors never start planning or mutation."""

    with pytest.raises(SystemExit) as raised:
        main(arguments)

    assert raised.value.code == 2
    assert expected in capsys.readouterr().err


def test_template_update_cli_defaults_to_a_json_read_only_plan() -> None:
    """Omitting --apply plans from the resolved comparator identity only."""

    calls: list[tuple[str, Path, bool, object, object]] = []
    output: list[str] = []

    def run_template_update(
        candidate_revision: str,
        project_root: Path,
        *,
        apply: bool,
        compare: object,
        dependencies: object,
    ) -> object:
        calls.append((candidate_revision, project_root, apply, compare, dependencies))
        return _report(
            outcome="planned",
            state="planned",
            changed_paths=("README.md", "src/widget.py"),
        )

    assert (
        main(
            [
                "template",
                "update",
                "--candidate-revision",
                "candidate",
                "--project-root",
                "/generated/widget",
                "--json",
            ],
            run_template_update=run_template_update,
            write_stdout=output.append,
        )
        == 0
    )

    assert len(calls) == 1
    candidate, root, apply, _compare, _dependencies = calls[0]
    assert (candidate, root, apply) == ("candidate", Path("/generated/widget"), False)
    assert json.loads(output[0]) == {
        "schema_version": 1,
        "command": "template.update",
        "outcome": "planned",
        "state": "planned",
        "current_commit": _CURRENT,
        "candidate_commit": _CANDIDATE,
        "changed_paths": ["README.md", "src/widget.py"],
        "next_action": "review plan",
    }


def test_template_update_cli_preview_skips_project_discovery(
    tmp_path: Path,
) -> None:
    """A dry-run plan needs no GitLab project configuration."""

    discovered: list[tuple[Path, object, object]] = []
    output: list[str] = []

    def discover(
        *, current_directory: Path, environment: object, run_command: object
    ) -> AgentWorkConfig:
        discovered.append((current_directory, environment, run_command))
        return AgentWorkConfig(
            host="git.example.test",
            project_id=37,
            checkout=Path("/generated/widget"),
            state_root=tmp_path,
            git_path=Path("/usr/bin/git"),
            glab_path=Path("/usr/bin/glab"),
            base_branch="agent/factory",
        )

    assert (
        main(
            [
                "template",
                "update",
                "--candidate-revision",
                "candidate",
                "--project-root",
                "/generated/widget",
                "--json",
            ],
            discover_agent_work_config=discover,
            compare_template_revisions=lambda *_args, **_kwargs: TemplateComparison(
                source=_SOURCE,
                current_commit=_CURRENT,
                candidate_commit=_CANDIDATE,
                paths=("README.md",),
            ),
            write_stdout=output.append,
        )
        == 0
    )

    assert discovered == []
    assert json.loads(output[0])["outcome"] == "planned"


def test_template_update_cli_passes_apply_only_when_explicit() -> None:
    """The CLI does not let presentation flags change the mutation gate."""

    received: list[bool] = []

    def run_template_update(*_args: object, **kwargs: object) -> object:
        apply = kwargs["apply"]
        assert isinstance(apply, bool)
        received.append(apply)
        return _report(outcome="ready", state="ready", changed_paths=())

    assert (
        main(
            ["template", "update", "--candidate-revision", "candidate", "--apply"],
            run_template_update=run_template_update,
            write_stdout=lambda _value: None,
        )
        == 0
    )

    assert received == [True]


def test_execute_template_update_dry_run_never_observes_or_mutates_checkout() -> None:
    """Planning is isolated from locks, records, Git, Copier, and workers."""

    comparison_calls: list[tuple[Path, str]] = []

    def compare(answers_path: Path, candidate_revision: str) -> object:
        comparison_calls.append((answers_path, candidate_revision))
        return _comparison(answers_path, candidate_revision)

    report = template_update.execute_template_update(
        candidate_revision="candidate",
        project_root=Path("/generated/widget"),
        apply=False,
        compare=compare,
        dependencies=_NoSideEffects(),
    )

    assert comparison_calls == [
        (Path("/generated/widget/.copier-answers.yml"), "candidate")
    ]
    assert report.outcome == "planned"
    assert report.state is template_update.TemplateUpdateState.PLANNED
    assert report.current_commit == _CURRENT
    assert report.candidate_commit == _CANDIDATE
    assert report.changed_paths == ("README.md", "src/widget.py")
    assert report.branch is None
    assert report.worktree is None


def test_template_update_record_is_strict_immutable_and_has_only_known_states() -> None:
    """Durable update state cannot silently absorb schema drift or be mutated."""

    record = template_update.TemplateUpdateRecord.model_validate(
        {
            "schema_version": 1,
            "revision": 0,
            "update_id": "d" * 64,
            "state": "planned",
            "project_id": 37,
            "source": _SOURCE,
            "current_commit": _CURRENT,
            "candidate_commit": _CANDIDATE,
            "checkout": "/generated/widget",
            "base_sha": _BASE,
            "branch": "agent/template-update-37",
            "worktree": "/state/template-updates/37/worktree",
            "changed_paths": [],
            "conflict_paths": [],
            "recovery_stage": None,
            "completion": None,
        }
    )

    assert record.state is template_update.TemplateUpdateState.PLANNED
    with pytest.raises((TypeError, ValidationError)):
        record.state = template_update.TemplateUpdateState.READY
    with pytest.raises(ValidationError):
        template_update.TemplateUpdateRecord.model_validate(
            record.model_dump() | {"untrusted": "accepted"}
        )
    with pytest.raises(ValidationError):
        template_update.TemplateUpdateRecord.model_validate(
            record.model_dump() | {"state": "replay-applying"}
        )


def test_apply_persists_each_non_replayable_state_before_its_boundary(
    tmp_path: Path,
) -> None:
    """A crash cannot leave worktree, Copier, or worker effects unrecorded."""

    harness = _ApplyHarness(tmp_path)

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.READY
    assert harness.events == [
        "lock:entered",
        "save:planned",
        "save:prepared",
        "worktree:create",
        "save:applying",
        "copier:run",
        "save:workflow",
        "worker:run",
        "save:ready",
        "lock:released",
    ]


def test_apply_uses_one_project_lock_and_exact_resolved_copier_update_argv(
    tmp_path: Path,
) -> None:
    """Copier receives only a resolved SHA in its dedicated update worktree."""

    harness = _ApplyHarness(tmp_path)

    _apply(harness)

    assert harness.events.count("lock:entered") == 1
    assert harness.events.count("lock:released") == 1
    assert len(harness.copier_calls) == 1
    argv, cwd, timeout_seconds = harness.copier_calls[0]
    assert argv == (
        "copier",
        "update",
        "--vcs-ref",
        _CANDIDATE,
        "--trust",
        "--defaults",
        "--skip-tasks",
        "--conflict",
        "rej",
    )
    assert cwd.is_relative_to(tmp_path / "template-updates")
    assert 0 < timeout_seconds <= 300


def test_apply_no_change_is_ready_without_invoking_generated_workflow(
    tmp_path: Path,
) -> None:
    """A clean no-op Copier update has no generated-project workflow cost."""

    harness = _ApplyHarness(tmp_path)
    harness.worktree = template_update.TemplateUpdateWorktree(
        branch="agent/template-update-37",
        head=_BASE,
        changed_paths=(),
        conflict_paths=(),
        candidate_tree=None,
    )

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.READY
    assert harness.worker_requests == []
    assert "worker:run" not in harness.events


def test_conflict_or_nonzero_copier_result_retains_evidence_without_workflow(
    tmp_path: Path,
) -> None:
    """Copier conflicts preserve the worktree and give recovery instead of reset."""

    harness = _ApplyHarness(tmp_path)
    harness.copier_result = CommandResult(1, b"", b"untrusted copier output")
    harness.worktree = template_update.TemplateUpdateWorktree(
        branch="agent/template-update-37",
        head=_BASE,
        changed_paths=("README.md", "src/widget.py"),
        conflict_paths=("src/widget.py.rej",),
        candidate_tree=None,
    )

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.CONFLICT
    assert report.outcome == "conflict"
    assert report.changed_paths == ("README.md", "src/widget.py")
    assert report.conflict_paths == ("src/widget.py.rej",)
    assert report.worktree is not None
    assert "worker:run" not in harness.events
    assert harness.events[-2:] == ["save:conflict", "lock:released"]


@pytest.mark.parametrize(
    "terminal_state",
    (
        template_update.TemplateUpdateState.PREPARED,
        template_update.TemplateUpdateState.APPLYING,
        template_update.TemplateUpdateState.WORKFLOW,
        template_update.TemplateUpdateState.CONFLICT,
        template_update.TemplateUpdateState.NEEDS_RECOVERY,
    ),
)
def test_reentry_for_ambiguous_or_recovery_state_never_replays_effects(
    tmp_path: Path, terminal_state: template_update.TemplateUpdateState
) -> None:
    """Ambiguous apply state is durable evidence, not permission to replay."""

    harness = _ApplyHarness(tmp_path)
    seed = template_update.TemplateUpdateRecord.model_validate(
        {
            "schema_version": 1,
            "revision": 2,
            "update_id": "d" * 64,
            "state": terminal_state.value,
            "project_id": 37,
            "source": _SOURCE,
            "current_commit": _CURRENT,
            "candidate_commit": _CANDIDATE,
            "checkout": "/generated/widget",
            "base_sha": _BASE,
            "branch": "agent/template-update-37",
            "worktree": str(tmp_path / "template-updates/37/worktree"),
            "changed_paths": ["README.md"],
            "conflict_paths": [],
            "recovery_stage": "copier",
            "completion": None,
        }
    )
    harness.loaded_record = seed

    report = _apply(harness)

    assert report.state in {
        terminal_state,
        template_update.TemplateUpdateState.NEEDS_RECOVERY,
    }
    assert harness.copier_calls == []
    assert harness.worker_requests == []
    assert "worktree:create" not in harness.events


@pytest.mark.parametrize(
    "worker_result",
    (
        b"not-json",
        _completion(paths=("README.md",)),
        _completion(paths=("README.md", "src/widget.py"))
        + b"\n"
        + _completion(paths=("README.md", "src/widget.py")),
        b"x" * 65_537,
    ),
)
def test_invalid_or_nonmatching_worker_completion_never_reaches_ready(
    tmp_path: Path, worker_result: bytes
) -> None:
    """The controller, rather than an agent, validates a bounded exact completion."""

    harness = _ApplyHarness(tmp_path)
    harness.worker_result = worker_result

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
    assert report.outcome == "needs-recovery"
    assert harness.events[-2:] == ["save:needs-recovery", "lock:released"]


def test_content_completion_can_be_ready_without_junit_but_coding_requires_it(
    tmp_path: Path,
) -> None:
    """Only coding completion needs current immutable evidence for both versions."""

    content = _ApplyHarness(tmp_path / "content")
    assert _apply(content).state is template_update.TemplateUpdateState.READY

    coding = _ApplyHarness(tmp_path / "coding")
    coding.worker_result = _completion(
        lane="coding", paths=("README.md", "src/widget.py"), junit_artifacts=[]
    )
    assert _apply(coding).state is template_update.TemplateUpdateState.NEEDS_RECOVERY


@pytest.mark.parametrize(
    "xml_size", [0, 1_048_577, 4_194_305, 6_291_456, 33_554_432, 33_554_433]
)
def test_coding_completion_with_current_two_version_junit_evidence_is_ready(
    tmp_path: Path,
    xml_size: int,
) -> None:
    """Coding handoff needs exact candidate-tree JUnit proof for both versions."""

    harness = _ApplyHarness(tmp_path)
    junit_artifacts: list[dict[str, object]] = []
    for environment in ("py311", "py314"):
        path = (tmp_path / environment / "junit.xml").resolve()
        contents = (
            b'<?xml version="1.0"?><testsuite tests="2" failures="0" '
            b'errors="0" skipped="0"></testsuite>'
        )
        if xml_size:
            contents += b" " * (xml_size - len(contents))
        harness.artifacts[path] = contents
        junit_artifacts.append(
            {
                "environment": environment,
                "path": str(path),
                "sha256": sha256(contents).hexdigest(),
                "test_count": 2,
                "failures": 0,
                "errors": 0,
                "skips": 0,
            }
        )
    harness.worker_result = _completion(
        lane="coding",
        paths=("README.md", "src/widget.py"),
        junit_artifacts=junit_artifacts,
    )

    report = _apply(harness)

    if xml_size > 33_554_432:
        assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
        assert report.completion is None
        return
    assert report.state is template_update.TemplateUpdateState.READY
    assert report.completion is not None
    assert report.completion.candidate_tree == harness.worktree.candidate_tree


def test_worker_changed_paths_after_completion_persist_needs_recovery(
    tmp_path: Path,
) -> None:
    """A worker cannot return a completion for a candidate it has changed."""

    harness = _ApplyHarness(tmp_path)
    harness.observed_worktrees = [
        harness.worktree,
        template_update.TemplateUpdateWorktree(
            branch=harness.worktree.branch,
            head=harness.worktree.head,
            changed_paths=("README.md",),
            conflict_paths=(),
            candidate_tree="f" * 40,
        ),
    ]

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
    assert report.completion is None


def test_coding_completion_rejects_duplicate_junit_environment(
    tmp_path: Path,
) -> None:
    """Two labels for one supported environment cannot forge matrix evidence."""

    harness = _ApplyHarness(tmp_path)
    path = (tmp_path / "py311" / "junit.xml").resolve()
    contents = b'<testsuite tests="1" failures="0" errors="0" skipped="0" />'
    harness.artifacts[path] = contents
    evidence: dict[str, object] = {
        "environment": "py311",
        "path": str(path),
        "sha256": sha256(contents).hexdigest(),
        "test_count": 1,
        "failures": 0,
        "errors": 0,
        "skips": 0,
    }
    harness.worker_result = _completion(
        lane="coding",
        paths=("README.md", "src/widget.py"),
        junit_artifacts=[evidence, evidence],
    )

    assert _apply(harness).state is template_update.TemplateUpdateState.NEEDS_RECOVERY


@pytest.mark.parametrize(
    "checkout",
    (
        template_update.TemplateUpdateCheckout(
            branch="feature", head=_BASE, clean=True
        ),
        template_update.TemplateUpdateCheckout(
            branch="main", head="not-a-sha", clean=True
        ),
        template_update.TemplateUpdateCheckout(branch="main", head=_BASE, clean=False),
    ),
)
def test_wrong_or_dirty_checkout_fails_closed_before_worktree_creation(
    tmp_path: Path, checkout: template_update.TemplateUpdateCheckout
) -> None:
    """Apply refuses a checkout that does not exactly match its configured base."""

    harness = _ApplyHarness(tmp_path)
    harness.checkout = checkout

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
    assert "worktree:create" not in harness.events
    assert harness.copier_calls == []


@pytest.mark.parametrize("failure", (TimeoutError("timeout"), OSError("interrupted")))
def test_copier_boundary_failure_persists_needs_recovery_without_replay(
    tmp_path: Path, failure: BaseException
) -> None:
    """An interrupted Copier boundary remains recoverable evidence, never retry work."""

    harness = _ApplyHarness(tmp_path)
    harness.copier_error = failure

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
    assert harness.events[-2:] == ["save:needs-recovery", "lock:released"]
    first_attempts = len(harness.copier_calls)
    _ = _apply(harness)
    assert len(harness.copier_calls) == first_attempts


def test_identity_mismatch_in_an_existing_record_fails_closed_without_effects(
    tmp_path: Path,
) -> None:
    """A record from another candidate cannot be repurposed by a later request."""

    harness = _ApplyHarness(tmp_path)
    harness.loaded_record = template_update.TemplateUpdateRecord.model_validate(
        {
            "schema_version": 1,
            "revision": 3,
            "update_id": "d" * 64,
            "state": "prepared",
            "project_id": 37,
            "source": _SOURCE,
            "current_commit": _CURRENT,
            "candidate_commit": "f" * 40,
            "checkout": "/generated/widget",
            "base_sha": _BASE,
            "branch": "agent/template-update-37",
            "worktree": str(tmp_path / "template-updates/37/worktree"),
            "changed_paths": ["README.md"],
            "conflict_paths": [],
            "recovery_stage": None,
            "completion": None,
        }
    )

    report = _apply(harness)

    assert report.state is template_update.TemplateUpdateState.NEEDS_RECOVERY
    assert harness.copier_calls == []
    assert harness.worker_requests == []


def test_update_module_stays_out_of_commit_push_and_publication_boundaries() -> None:
    """Template update prepares a human handoff and never owns publication."""

    injected_ports = set(
        _ApplyHarness(Path("/state")).dependencies().__dataclass_fields__
    )
    assert (
        not {
            "commit",
            "push",
            "publish",
            "create_merge_request",
            "approve",
            "merge",
        }
        & injected_ports
    )

    source = Path(template_update.__file__).read_text(encoding="utf-8")

    for forbidden in ("git commit", "git push", "merge-request publish", "glab api"):
        assert forbidden not in source


def test_root_and_template_template_update_modules_remain_byte_identical() -> None:
    """Generated projects receive the same update safety boundary as the root."""

    root = Path(__file__).parents[1]
    assert (root / "src/agent_work/template_update.py").read_bytes() == (
        root / "template/src/agent_work/template_update.py"
    ).read_bytes()


@pytest.mark.parametrize(
    "size", [1_048_577, 4_194_305, 6_291_456, 33_554_432, 33_554_433, 33_554_434]
)
def test_default_completion_artifact_reader_preserves_cap_and_stops_at_sentinel(
    tmp_path: Path, size: int
) -> None:
    path = tmp_path / "junit.xml"
    raw = b"<testsuite tests='1' failures='0' errors='0'/>"
    raw += b" " * (size - len(raw))
    path.write_bytes(raw)
    dependencies = template_update.default_dependencies(
        project=template_update.TemplateUpdateProject(
            project_id=37,
            checkout=tmp_path,
            default_branch="main",
            state_root=tmp_path / "state",
        ),
        run_process=lambda *_args, **_kwargs: pytest.fail(
            "reader must not launch process"
        ),
    )
    actual = dependencies.read_artifact(path)
    assert actual is not None and len(actual) == min(size, 33_554_433)
    assert sha256(actual).hexdigest() == sha256(raw[:33_554_433]).hexdigest()
    assert dependencies.read_artifact(tmp_path / "missing.xml") is None
