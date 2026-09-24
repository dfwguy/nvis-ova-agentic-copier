# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for trusted publication of a prepared template update."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_work import template_comparison, template_update
from agent_work.cli import main
from agent_work.gitlab import GitLabMergeRequestPublisher
from agent_work.models import AgentWorkConfig, MergeRequestDraft
from agent_work.repository import CommandResult
from agent_work.template_update_publication import (
    TemplateUpdatePublicationReport,
    _DefaultDependencies,
    publish_ready_template_update,
)

_BASE = "a" * 40
_CURRENT = "b" * 40
_CANDIDATE = "c" * 40
_TREE = "d" * 40
_PUBLISHED = "e" * 40
_UPDATE_ID = "f" * 64
_BRANCH = "agent/template-update-ffffffffffffffff"
_PATHS = ("README.md", "src/widget.py")


def _expected_description() -> str:
    return (
        "Review the validated generated template update.\n\n"
        f"Current template revision: `{_CURRENT}`.\n"
        f"Candidate template revision: `{_CANDIDATE}`.\n"
        f"Validated tree: `{_TREE}`.\n"
        "Validation: the installed full hook passed with fresh py311 and "
        "py314 JUnit results.\n"
        f"Published commit: `{_PUBLISHED}`.\n\n"
        "Only a human may approve or merge."
    )


def _ready_record(tmp_path: Path) -> template_update.TemplateUpdateRecord:
    """Build one durable, changed READY update with a worker proposal."""
    return template_update.TemplateUpdateRecord(
        revision=5,
        update_id=_UPDATE_ID,
        state=template_update.TemplateUpdateState.READY,
        project_id=37,
        source="https://git.example.test/factory/template.git",
        current_commit=_CURRENT,
        candidate_commit=_CANDIDATE,
        checkout=Path("/generated/widget"),
        base_sha=_BASE,
        branch=_BRANCH,
        worktree=tmp_path / "prepared",
        changed_paths=_PATHS,
        completion=template_update.TemplateUpdateCompletion(
            workflow_lane=template_update.WorkflowLane.CONTENT,
            workflow_receipts=(),
            changed_paths=_PATHS,
            conflict_disposition="none",
            merge_request_draft=MergeRequestDraft(
                title="Update generated project from template",
                description="Review the validated generated update.",
            ),
            audit_receipt="worker proposal",
            capacity_receipt="worker proposal",
            candidate_tree=_TREE,
        ),
    )


def _ready_report(
    record: template_update.TemplateUpdateRecord,
) -> template_update.TemplateUpdateReport:
    """Use the existing update result as the publication input."""
    return template_update.TemplateUpdateReport(
        outcome="ready",
        state=template_update.TemplateUpdateState.READY,
        update_id=record.update_id,
        current_commit=record.current_commit,
        candidate_commit=record.candidate_commit,
        branch=record.branch,
        worktree=record.worktree,
        changed_paths=record.changed_paths,
        conflict_paths=(),
        recovery_stage=None,
        next_action="publish validated update",
        completion=record.completion,
    )


@dataclass
class _PublicationHarness:
    """Record effects at injected program and external boundaries."""

    record: template_update.TemplateUpdateRecord
    observed_tree: str = _TREE
    validation_tree: str = _TREE
    junit_artifacts: tuple[str, ...] = ("py311", "py314")
    observed_mr_sha: str = _PUBLISHED
    observed_remote_sha: str | None = None
    remote_sha_after_push: str | None = None
    observed_paths: tuple[str, ...] = _PATHS
    observed_mr_title: str = "Update generated project from template"
    observed_mr_description: str | None = None
    observed_commit_tree: str = _TREE
    observed_commit_parent: str = _BASE
    target_ok: bool = True
    publish_request: tuple[object, ...] | None = None
    events: list[str] = field(default_factory=list)
    intent: object | None = None
    published_sha: str | None = None
    merge_request_iid: int | None = None

    @contextmanager
    def lock(self) -> Iterator[None]:
        self.events.append("lock")
        yield

    def project(self) -> template_update.TemplateUpdateProject:
        self.events.append("project")
        return template_update.TemplateUpdateProject(
            project_id=37,
            checkout=Path("/generated/widget"),
            default_branch="main",
            state_root=self.record.worktree.parent,
        )

    def load_record(self, update_id: str) -> template_update.TemplateUpdateRecord:
        self.events.append("record")
        assert update_id == _UPDATE_ID
        return self.record

    def observe_checkout(self) -> template_update.TemplateUpdateCheckout:
        self.events.append("checkout")
        return template_update.TemplateUpdateCheckout("main", _BASE, True)

    def observe_worktree(self, path: Path) -> template_update.TemplateUpdateWorktree:
        self.events.append("worktree")
        assert path == self.record.worktree
        if self.published_sha is not None:
            return template_update.TemplateUpdateWorktree(
                _BRANCH, self.published_sha, (), (), None
            )
        return template_update.TemplateUpdateWorktree(
            _BRANCH, _BASE, self.observed_paths, (), self.observed_tree
        )

    def validate_tree(self, *_args: object, **_kwargs: object) -> object:
        self.events.append("validate")
        return SimpleNamespace(
            tree=self.validation_tree,
            success=True,
            junit_artifacts=self.junit_artifacts,
        )

    def save_intent(self, intent: object, *_args: object) -> None:
        self.events.append("intent")
        self.intent = intent

    def load_intent(self, _update_id: str) -> object | None:
        self.events.append("load-intent")
        return self.intent

    def commit(self, *_args: object, **_kwargs: object) -> str:
        self.events.append("commit")
        self.published_sha = _PUBLISHED
        return _PUBLISHED

    def push(self, *_args: object, **_kwargs: object) -> None:
        self.events.append("push")
        if self.observed_remote_sha is None:
            self.observed_remote_sha = self.remote_sha_after_push or _PUBLISHED

    def observe_commit(self, *_args: object, **_kwargs: object) -> object:
        self.events.append("commit-readback")
        return SimpleNamespace(
            tree=self.observed_commit_tree, parent=self.observed_commit_parent
        )

    def observe_remote_sha(self, *_args: object, **_kwargs: object) -> str | None:
        self.events.append("remote-readback")
        return self.observed_remote_sha

    def verify_publication_target(self, *_args: object, **_kwargs: object) -> bool:
        self.events.append("target-readback")
        return self.target_ok

    def publish_merge_request(self, *args: object, **_kwargs: object) -> int:
        self.events.append("merge-request")
        self.publish_request = args
        self.merge_request_iid = 42
        return 42

    def observe_merge_request(self, *_args: object, **_kwargs: object) -> object:
        self.events.append("readback")
        return SimpleNamespace(
            iid=42,
            source_branch=_BRANCH,
            target_branch="main",
            sha=self.observed_mr_sha,
            title=self.observed_mr_title,
            description=self.observed_mr_description or _expected_description(),
            web_url="https://git.example.test/generated/widget/-/merge_requests/42",
        )

    def dependencies(self) -> object:
        return SimpleNamespace(
            project=self.project,
            lock=self.lock,
            load_record=self.load_record,
            observe_checkout=self.observe_checkout,
            observe_worktree=self.observe_worktree,
            validate_tree=self.validate_tree,
            load_intent=self.load_intent,
            save_intent=self.save_intent,
            commit=self.commit,
            observe_commit=self.observe_commit,
            push=self.push,
            observe_remote_sha=self.observe_remote_sha,
            verify_publication_target=self.verify_publication_target,
            publish_merge_request=self.publish_merge_request,
            observe_merge_request=self.observe_merge_request,
        )


def _publish(
    report: template_update.TemplateUpdateReport, harness: _PublicationHarness
) -> TemplateUpdatePublicationReport:
    return publish_ready_template_update(
        report, dependencies=cast(Any, harness.dependencies())
    )


def test_ready_template_update_validates_exact_tree_before_publication(
    tmp_path: Path,
) -> None:
    """Only program-owned validation of the observed candidate permits effects."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record)

    result = _publish(_ready_report(record), harness)

    assert result.outcome == "published"
    assert harness.intent is not None
    assert harness.events.index("validate") < harness.events.index("intent")
    assert harness.events.index("intent") < harness.events.index("commit")
    assert harness.events.index("commit") < harness.events.index("push")
    assert harness.events.index("push") < harness.events.index("merge-request")
    assert harness.events.index("merge-request") < harness.events.index("readback")


@pytest.mark.parametrize("observed_tree", [None, "0" * 40])
def test_ready_template_update_refuses_drift_before_remote_effects(
    tmp_path: Path, observed_tree: str | None
) -> None:
    """Deletion or a changed candidate tree cannot authorize publication."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record, observed_tree=observed_tree or "")

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "validate" not in harness.events
    assert "commit" not in harness.events
    assert "push" not in harness.events
    assert "merge-request" not in harness.events


@pytest.mark.parametrize(
    ("validation_tree", "junit_artifacts"),
    [
        ("0" * 40, ("py311", "py314")),
        (_TREE, ("py311",)),
        (_TREE, ()),
    ],
)
def test_ready_template_update_requires_current_program_owned_validation(
    tmp_path: Path, validation_tree: str, junit_artifacts: tuple[str, ...]
) -> None:
    """A worker receipt cannot replace exact supported-version validation."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(
        record,
        validation_tree=validation_tree,
        junit_artifacts=junit_artifacts,
    )

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "validate" in harness.events
    assert "commit" not in harness.events
    assert "push" not in harness.events
    assert "merge-request" not in harness.events


def test_published_template_update_replay_reads_back_one_merge_request(
    tmp_path: Path,
) -> None:
    """A repeated activation confirms durable publication without a new push."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record)
    report = _ready_report(record)

    first = _publish(report, harness)
    assert first.outcome == "published"
    harness.events.clear()
    second = _publish(report, harness)

    assert second.outcome == "published"
    assert "commit" not in harness.events
    assert "push" not in harness.events
    assert "merge-request" not in harness.events
    assert "readback" in harness.events


def test_published_template_update_replay_rejects_mr_sha_drift(
    tmp_path: Path,
) -> None:
    """A mismatched server readback cannot be mistaken for completed work."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record)
    report = _ready_report(record)
    assert _publish(report, harness).outcome == "published"
    harness.events.clear()
    harness.observed_mr_sha = "0" * 40

    result = _publish(report, harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "commit" not in harness.events
    assert "push" not in harness.events
    assert "merge-request" not in harness.events


@pytest.mark.parametrize("drift", ["observed_commit_tree", "observed_commit_parent"])
def test_template_update_refuses_wrong_committed_tree_or_parent(
    tmp_path: Path, drift: str
) -> None:
    """A commit must retain the validated tree on the exact prepared base."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record)
    setattr(harness, drift, "0" * 40)

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "commit-readback" in harness.events
    assert "push" not in harness.events


def test_template_update_refuses_wrong_remote_branch_sha(tmp_path: Path) -> None:
    """A push response cannot stand in for remote branch SHA readback."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record, observed_remote_sha="0" * 40)

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "remote-readback" in harness.events
    assert "merge-request" not in harness.events


def test_template_update_refuses_wrong_sha_after_push(tmp_path: Path) -> None:
    """The server must report the pushed SHA before an MR can be opened."""
    harness = _PublicationHarness(
        _ready_record(tmp_path), remote_sha_after_push="0" * 40
    )

    result = _publish(_ready_report(harness.record), harness)

    assert result.outcome == "needs-recovery"
    assert "push" in harness.events
    assert "merge-request" not in harness.events


def test_template_update_uses_final_worker_changed_paths(tmp_path: Path) -> None:
    """Copier paths may be a subset of the worker's final validated tree."""
    record = _ready_record(tmp_path)
    final_paths = (*_PATHS, "tests/test_widget.py")
    assert record.completion is not None
    record = record.model_copy(
        update={
            "completion": record.completion.model_copy(
                update={"changed_paths": final_paths}
            )
        }
    )
    harness = _PublicationHarness(record, observed_paths=final_paths)

    result = _publish(_ready_report(record), harness)

    assert result.outcome == "published"
    assert harness.intent is not None
    assert cast(Any, harness.intent).changed_paths == final_paths


def test_template_update_requires_created_mr_text_readback(tmp_path: Path) -> None:
    """Initial MR readback must match the controller's exact review text."""
    harness = _PublicationHarness(
        _ready_record(tmp_path), observed_mr_description="unexpected body"
    )

    result = _publish(_ready_report(harness.record), harness)

    assert result.outcome == "needs-recovery"
    assert "readback" in harness.events


def test_template_update_replay_preserves_human_mr_text_edits(tmp_path: Path) -> None:
    """A later human edit must not trigger an MR rewrite or false recovery."""
    harness = _PublicationHarness(_ready_record(tmp_path))
    report = _ready_report(harness.record)
    assert _publish(report, harness).outcome == "published"
    harness.events.clear()
    harness.observed_mr_title = "Human-edited title"
    harness.observed_mr_description = "Human-edited description"

    result = _publish(report, harness)

    assert result.outcome == "published"
    assert "merge-request" not in harness.events


def test_template_update_refuses_unbound_publication_target(tmp_path: Path) -> None:
    """A changed Git remote cannot receive a generated project update."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record, target_ok=False)

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "target-readback" in harness.events
    assert "push" not in harness.events
    assert "merge-request" not in harness.events


def test_template_update_refuses_existing_wrong_remote_sha_before_push(
    tmp_path: Path,
) -> None:
    """An occupied source branch must not be advanced or overwritten."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record, observed_remote_sha="0" * 40)

    result = _publish(_ready_report(record), harness)

    assert result.outcome in {"needs-recovery", "conflict"}
    assert "push" not in harness.events
    assert "merge-request" not in harness.events


def test_template_update_mr_uses_controller_revision_and_validation_identity(
    tmp_path: Path,
) -> None:
    """The MR receives verified revision and tree details, not worker text."""
    record = _ready_record(tmp_path)
    harness = _PublicationHarness(record)

    assert _publish(_ready_report(record), harness).outcome == "published"

    assert harness.publish_request is not None
    assert _CURRENT in harness.publish_request
    assert _CANDIDATE in harness.publish_request
    assert _TREE in harness.publish_request


def test_template_update_cli_does_not_print_worker_prose_after_publication(
    tmp_path: Path,
) -> None:
    """Published status must not echo worker-authored audit or MR text."""
    record = _ready_record(tmp_path)
    assert record.completion is not None
    completion = record.completion.model_copy(
        update={"audit_receipt": "SECRET_SENTINEL"}
    )
    report = _ready_report(record)
    report = replace(report, completion=completion)
    output: list[str] = []

    status = main(
        [
            "template",
            "update",
            "--candidate-revision",
            "candidate",
            "--apply",
            "--json",
        ],
        run_template_update=lambda *_args, **_kwargs: report,
        publish_template_update=lambda *_args, **_kwargs: SimpleNamespace(
            outcome="published",
            state="published",
            next_action="review merge request",
            merge_request_iid=42,
            merge_request_url="https://git.example.test/merge_requests/42",
        ),
        write_stdout=output.append,
    )

    assert status == 0
    assert "SECRET_SENTINEL" not in output[0]
    payload = json.loads(output[0])
    assert payload["merge_request_iid"] == 42
    assert "completion" not in payload


def test_template_update_mr_inventory_includes_closed_source_branches() -> None:
    """A closed same-branch request cannot be hidden by an opened-only query."""
    calls: list[tuple[str, ...]] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(
            0,
            json.dumps(
                [
                    {
                        "iid": 7,
                        "web_url": "https://git.example.test/merge_requests/7",
                        "state": "closed",
                        "source_project_id": 37,
                        "target_project_id": 37,
                        "source_branch": _BRANCH,
                        "target_branch": "main",
                        "sha": _PUBLISHED,
                    }
                ]
            ),
            "",
        )

    publisher = GitLabMergeRequestPublisher(
        host="git.example.test",
        project_id=37,
        glab_path="/bin/glab",
        run_command=run,
    )

    inventory = publisher.list_any_merge_requests(project_id=37, source_branch=_BRANCH)
    assert len(inventory) == 1
    assert inventory[0].state == "closed"
    assert any("state=all" in call for call in calls)


def test_template_update_default_validator_persists_exact_junit_snapshots(
    tmp_path: Path,
) -> None:
    """Injected Git/hook process and durable Judge evidence bind the same tree."""
    worktree = tmp_path / _UPDATE_ID
    worktree.mkdir()
    calls: list[tuple[str, ...]] = []

    def process(
        command: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[-1] == "write-tree":
            return subprocess.CompletedProcess(command, 0, (_TREE + "\n").encode(), b"")
        if command[-3:] == ("hook", "run", "pre-commit"):
            for environment in ("py311", "py314"):
                (worktree / f"test-results-{environment}.xml").write_text(
                    '<testsuite tests="1" failures="0" errors="0" skipped="0"/>'
                )
            return subprocess.CompletedProcess(command, 0, b"", b"")
        if command[-2:] == ("diff", "--quiet"):
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(command)

    config = AgentWorkConfig(
        host="git.example.test",
        project_id=37,
        checkout=tmp_path,
        state_root=tmp_path / "state",
        git_path=Path("/usr/bin/git"),
        glab_path=Path("/usr/bin/glab"),
        base_branch="agent/factory",
    )
    dependencies = _DefaultDependencies(
        config,
        cast(Any, object()),
        {},
        lambda *_args, **_kwargs: CommandResult(0, "", ""),
        process,
    )

    evidence = dependencies.validate_tree(worktree, _TREE)
    assert evidence.success
    assert evidence.junit_artifacts == ("py311", "py314")
    assert sum(call[-3:] == ("hook", "run", "pre-commit") for call in calls) == 1
    assert dependencies.validate_tree(worktree, _TREE).success
    assert sum(call[-3:] == ("hook", "run", "pre-commit") for call in calls) == 1

    snapshot = (
        config.state_root
        / "template-updates"
        / "validations"
        / _UPDATE_ID
        / _TREE
        / "test-results-py311.xml"
    )
    snapshot.chmod(0o644)
    snapshot.write_text("tampered")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        dependencies.validate_tree(worktree, _TREE)


def test_template_update_cli_composes_real_comparator_ports() -> None:
    """The update route supplies the comparator's required trusted ports."""
    seen: list[str] = []

    def compare(
        answers_path: Path,
        candidate_revision: str,
        *,
        read_text: Callable[[Path], str],
        run_command: template_comparison.BinaryCommandRunner,
        private_repository: Callable[[], AbstractContextManager[Path]],
        timeout_seconds: float,
    ) -> template_comparison.TemplateComparison:
        assert answers_path.name == ".copier-answers.yml"
        assert candidate_revision == "candidate"
        assert callable(read_text)
        assert callable(run_command)
        assert callable(private_repository)
        assert timeout_seconds > 0
        seen.append("compare")
        return template_comparison.TemplateComparison(_CURRENT, _CANDIDATE, ())

    def run_update(
        candidate: str, root: Path, *, compare: Callable[..., object], **_kwargs: object
    ) -> template_update.TemplateUpdateReport:
        assert callable(compare)
        comparison = compare(root / ".copier-answers.yml", candidate)
        assert comparison is not None
        return template_update.TemplateUpdateReport(
            "planned",
            template_update.TemplateUpdateState.PLANNED,
            "",
            _CURRENT,
            _CANDIDATE,
            None,
            None,
            (),
            (),
            None,
            "review plan",
            None,
        )

    status = main(
        ["template", "update", "--candidate-revision", "candidate", "--json"],
        run_template_update=run_update,
        compare_template_revisions=compare,
        template_run_command=lambda *_args, **_kwargs: (
            template_comparison.CommandResult(0, b"", b"")
        ),
        write_stdout=lambda _value: None,
    )

    assert status == 0
    assert seen == ["compare"]


def test_template_update_preview_needs_no_gitlab_project_discovery(
    tmp_path: Path,
) -> None:
    """A no-apply plan can read Copier metadata without GitLab setup."""
    (tmp_path / ".copier-answers.yml").write_text(
        "_src_path: https://github.com/octocat/Hello-World.git\n_commit: master\n"
    )
    output: list[str] = []

    def compare(
        answers_path: Path,
        candidate_revision: str,
        *,
        read_text: Callable[[Path], str],
        run_command: template_comparison.BinaryCommandRunner,
        private_repository: Callable[[], AbstractContextManager[Path]],
        timeout_seconds: float,
    ) -> template_comparison.TemplateComparison:
        assert "_src_path:" in read_text(answers_path)
        assert candidate_revision == "master"
        assert callable(run_command)
        assert callable(private_repository)
        assert timeout_seconds > 0
        return template_comparison.TemplateComparison(_CURRENT, _CANDIDATE, ())

    def discovery(
        *, current_directory: Path, environment: object, run_command: object
    ) -> AgentWorkConfig:
        assert current_directory == tmp_path
        assert environment is not None and run_command is not None
        raise RuntimeError("preview crossed GitLab project discovery")

    status = main(
        [
            "template",
            "update",
            "--candidate-revision",
            "master",
            "--project-root",
            str(tmp_path),
            "--json",
        ],
        compare_template_revisions=compare,
        discover_agent_work_config=discovery,
        write_stdout=output.append,
        write_stderr=lambda _value: None,
    )

    assert status == 0
    assert json.loads(output[0])["outcome"] == "planned"


@pytest.mark.parametrize(
    ("apply", "changed_paths", "should_publish"),
    [
        (False, _PATHS, False),
        (True, (), False),
        (True, _PATHS, True),
    ],
)
def test_template_update_cli_only_publishes_changed_applied_ready_update(
    tmp_path: Path,
    *,
    apply: bool,
    changed_paths: tuple[str, ...],
    should_publish: bool,
) -> None:
    """Preview and no-change apply have no publication boundary effects."""
    record = _ready_record(tmp_path)
    report = template_update.TemplateUpdateReport(
        outcome="ready" if apply else "planned",
        state=(
            template_update.TemplateUpdateState.READY
            if apply
            else template_update.TemplateUpdateState.PLANNED
        ),
        update_id=record.update_id,
        current_commit=record.current_commit,
        candidate_commit=record.candidate_commit,
        branch=record.branch if apply else None,
        worktree=record.worktree if apply else None,
        changed_paths=changed_paths,
        conflict_paths=(),
        recovery_stage=None,
        next_action="review plan",
        completion=record.completion if changed_paths and apply else None,
    )
    publications: list[object] = []

    def publish_ready(value: object, **_kwargs: object) -> object:
        publications.append(value)
        return SimpleNamespace(outcome="published", state="published")

    status = main(
        [
            "template",
            "update",
            "--candidate-revision",
            "candidate",
            "--json",
            *(["--apply"] if apply else []),
        ],
        run_template_update=lambda *_args, **_kwargs: report,
        publish_template_update=publish_ready,
        write_stdout=lambda _value: None,
    )

    assert status == 0
    assert publications == ([report] if should_publish else [])
