# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the recoverable CLI surface."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_work.cli import _render, build_parser, main
from agent_work.domain import CommandRequest, RunnerState, WorkflowPhase, WorkKind
from agent_work.gitlab import (
    MergeRequestPublicationReport,
    render_merge_request_description,
)
from agent_work.ledger import LockBusy, project_state_path
from agent_work.models import (
    CommandItemReport,
    CommandReport,
    ConditionEvidence,
    MergeRequestEvidence,
    PrerequisiteLabelProjectionReport,
    RecoveryCandidateReport,
    RecoverySummary,
)
from agent_work.repository import CommandResult
from agent_work.timing_admission import TimingAdmissionLease
from agent_work.upstream_finding import UpstreamFindingPreview
from agent_work.worker import WorkerResultBrokerClientRequest


def test_validation_window_preview_does_not_launch_child() -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def run_validation_window(command: tuple[str, ...], apply: bool) -> int:
        calls.append((command, apply))
        return 0

    output: list[str] = []
    exit_code = main(
        ["validation", "window", "--", "bash", "-c", "true"],
        run_validation_window=run_validation_window,
        write_stdout=output.append,
    )

    assert exit_code == 0
    assert calls == []
    assert output


@pytest.mark.parametrize("child_exit_code", (0, 1, 2))
def test_validation_window_apply_preserves_child_argv_and_normalizes_exit_code(
    child_exit_code: int,
) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []

    def run_validation_window(command: tuple[str, ...], apply: bool) -> int:
        calls.append((command, apply))
        return child_exit_code

    exit_code = main(
        ["validation", "window", "--apply", "--", "bash", "-c", "exit 7"],
        run_validation_window=run_validation_window,
        write_stdout=lambda _message: None,
    )

    assert exit_code == (0 if child_exit_code == 0 else 1)
    assert calls == [(("bash", "-c", "exit 7"), True)]


def test_validation_window_apply_json_reports_child_status() -> None:
    output: list[str] = []
    result = main(
        ["validation", "window", "--apply", "--json", "--", "bash", "-c", "exit 7"],
        run_validation_window=lambda _command, _apply: 7,
        write_stdout=output.append,
    )

    assert result == 1
    assert len(output) == 1
    assert json.loads(output[0]) == {
        "action": "window",
        "applied": True,
        "exit_code": 7,
    }


def test_validation_window_busy_json_reports_deferred_status() -> None:
    from agent_work.timing_admission import TimingAdmissionBusy

    output: list[str] = []
    result = main(
        ["validation", "window", "--apply", "--json", "--", "bash"],
        run_validation_window=lambda _command, _apply: (_ for _ in ()).throw(
            TimingAdmissionBusy("project timing admission busy")
        ),
        write_stdout=output.append,
        write_stderr=lambda _message: None,
    )

    assert result == 1
    assert json.loads(output[0]) == {
        "action": "window",
        "applied": True,
        "state": "deferred",
        "reason": "project_timing_admission_busy",
    }


def test_validation_window_help_and_missing_child_use_argparse_status() -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["validation", "window", "--help"])
    with pytest.raises(SystemExit) as missing_child_exit:
        main(["validation", "window", "--apply", "--"])

    assert help_exit.value.code == 0
    assert missing_child_exit.value.code == 2


@pytest.mark.parametrize(
    "identity",
    (
        {},
        {"GITLAB_HOST": "gitlab.example.test"},
        {"GITLAB_PROJECT_ID": "276"},
        {"GITLAB_HOST": "gitlab.example.test", "GITLAB_PROJECT_ID": "invalid"},
    ),
)
def test_applied_validation_without_numeric_project_identity_fails_before_hook(
    tmp_path: Path, identity: dict[str, str]
) -> None:
    commands: list[tuple[str, ...]] = []
    removed: list[Path] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        if command == ("git", "rev-parse", "--git-dir"):
            return CommandResult(0, str(tmp_path / ".git"), "")
        if command == ("git", "write-tree"):
            return CommandResult(0, "a" * 40, "")
        return CommandResult(0, "", "")

    errors: list[str] = []
    result = main(
        ["validation", "run", "--candidate-tree", "a" * 40, "--apply"],
        environment={"XDG_STATE_HOME": str(tmp_path), **identity},
        run_command=run,
        run_validation_hook=lambda command, _lease: run(command),
        read_validation_artifact=lambda _path: (
            b"<testsuite tests='1' failures='0' errors='0'/>"
        ),
        remove_validation_artifact=removed.append,
        write_stdout=lambda _message: None,
        write_stderr=errors.append,
    )

    assert result == 1
    assert ("git", "hook", "run", "pre-commit") not in commands
    assert removed == []
    assert list(tmp_path.rglob("*.json")) == []
    assert errors


def test_applied_validation_with_project_identity_runs_hook_under_shared_admission(
    tmp_path: Path,
) -> None:
    environment = {
        "XDG_STATE_HOME": str(tmp_path),
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "276",
    }
    commands: list[tuple[str, ...]] = []
    removed: list[Path] = []
    hook_leases: list[TimingAdmissionLease | None] = []

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        if command == ("git", "rev-parse", "--git-dir"):
            return CommandResult(0, str(tmp_path / ".git"), "")
        if command == ("git", "write-tree"):
            return CommandResult(0, "a" * 40, "")
        if command == (
            sys.executable,
            "scripts/check_licenses.py",
            "--root",
            ".",
            "--policy",
            ".license-policy.toml",
            "--candidate-tree",
            "a" * 40,
            "--json",
        ):
            return CommandResult(
                0,
                json.dumps(
                    {"schema_version": 1, "outcome": "compliant", "violations": []}
                ),
                "",
            )
        return CommandResult(0, "", "")

    def run_hook(
        command: tuple[str, ...], lease: TimingAdmissionLease | None
    ) -> CommandResult:
        hook_leases.append(lease)
        return run(command)

    result = main(
        ["validation", "run", "--candidate-tree", "a" * 40, "--apply"],
        environment=environment,
        run_command=run,
        run_validation_hook=run_hook,
        read_validation_artifact=lambda _path: (
            b"<testsuite tests='1' failures='0' errors='0'/>"
        ),
        remove_validation_artifact=removed.append,
        write_stdout=lambda _message: None,
    )

    project_root = project_state_path(
        host="gitlab.example.test", project_id=276, environment=environment
    )
    assert result == 0
    assert commands.count(("git", "hook", "run", "pre-commit")) == 1
    assert len(hook_leases) == 1
    assert isinstance(hook_leases[0], TimingAdmissionLease)
    assert removed == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]
    assert any(path.is_file() for path in project_root.rglob("*"))


def test_applied_validation_contention_returns_json_deferred_result() -> None:
    from agent_work.timing_admission import TimingAdmissionBusy

    output: list[str] = []
    result = main(
        ["validation", "run", "--candidate-tree", "a" * 40, "--apply", "--json"],
        validation_run=lambda **_kwargs: (_ for _ in ()).throw(
            TimingAdmissionBusy("project timing admission busy")
        ),
        write_stdout=output.append,
        write_stderr=lambda _message: None,
    )

    assert result == 1
    assert json.loads(output[0]) == {
        "candidate_tree": "a" * 40,
        "state": "deferred",
        "reason": "project_timing_admission_busy",
    }


def test_human_status_renders_one_concise_answer_line_for_each_item() -> None:
    def item(iid: int, state: RunnerState, summary: str) -> CommandItemReport:
        return CommandItemReport(
            issue_iid=iid,
            state=state,
            phase=WorkflowPhase.CODING_GREEN,
            work_kind=WorkKind.ORDINARY,
            attempt=1,
            retries_remaining=3,
            source_branch=f"agent/issue-{iid}",
            source_sha="a" * 40,
            worktree=Path(f"/state/worktrees/{iid}"),
            summary=summary,
            current_condition=(
                ConditionEvidence(code="failed_condition", detail="blocked\nunsafe")
                if state is RunnerState.FAILED
                else None
            ),
            next_action="Start a fresh whole workflow and repeat validation.",
        )

    rendered = _render(
        CommandReport(
            command="loop.status",
            applied=False,
            outcome="status",
            items=(
                item(12, RunnerState.EXECUTING, "changed=1 validated=passed"),
                item(13, RunnerState.FAILED, "changed=0 validated=failed"),
            ),
        ),
        as_json=False,
    )

    lines = rendered.splitlines()
    assert len(lines) == 2
    assert "issue=#12" in lines[0] and "running" in lines[0]
    assert "changed=1" in lines[0] and "validated=passed" in lines[0]
    assert "issue=#13" in lines[1] and "failed" in lines[1]
    assert "changed=0" in lines[1] and "validated=failed" in lines[1]
    assert "current=failed_condition:blocked unsafe" in lines[1]
    assert all("next=" in line for line in lines)


def test_recovery_inventory_v2_json_and_human_output_are_compact_and_sanitized() -> (
    None
):
    """Inventory output exposes actionable state without leaking multiline evidence."""
    candidate = RecoveryCandidateReport(
        issue_iid=12,
        classification="needs_human_decision",
        issue_state="opened",
        priority=0,
        recommendation="ignored legacy recommendation",
        active=True,
        human_action_required=True,
        blocked_cause="pipeline evidence disagrees",
        dependency_explanation="trusted prerequisite #9 remains open",
        next_action="Review issue #12 before selecting one resolution.",
    )
    report = CommandReport(
        schema_version=2,
        command="recovery.inventory",
        applied=False,
        outcome="inventory",
        recovery_summary=RecoverySummary(
            total=4,
            open=3,
            closed=1,
            active=2,
            blocked=1,
            dependency_gated=1,
            human_action=1,
            no_action=3,
        ),
        recovery_candidates=(candidate,),
    )

    payload = json.loads(_render(report, as_json=True))
    rendered = _render(report, as_json=False)

    assert payload["schema_version"] == 2
    assert payload["recovery_summary"]["human_action"] == 1
    assert payload["recovery_candidates"][0]["next_action"].startswith("Review")
    assert "human_action_required=true" in rendered
    assert "next_action=Review issue #12 before selecting one resolution." in rendered
    assert "\n" not in rendered and "recommendation=" not in rendered


def test_human_prerequisite_projection_labels_are_single_line() -> None:
    """GitLab-controlled display labels cannot split human command output."""
    report = CommandReport(
        command="loop.run",
        applied=False,
        outcome="idle",
        prerequisite_projections=(
            PrerequisiteLabelProjectionReport(
                issue_iid=12,
                current_labels=("customer::priority\nunsafe",),
                intended_labels=("agent::prereq_37\r\nunsafe",),
            ),
        ),
    )

    rendered = _render(report, as_json=False)

    assert "\n" not in rendered and "\r" not in rendered
    assert "current_labels=customer::priority unsafe" in rendered
    assert "intended_labels=agent::prereq_37 unsafe" in rendered


def test_worker_exit_evidence_is_projected_to_json_and_human_output() -> None:
    item = CommandItemReport.model_validate(
        {
            "issue_iid": 12,
            "state": "retry-wait",
            "phase": "coding-green",
            "work_kind": "ordinary",
            "attempt": 2,
            "retries_remaining": 2,
            "source_branch": "agent/issue-12",
            "worktree": "/tmp/issue-12",
            "worker_exit": {"returncode": -15, "diagnostic": "transport closed"},
        }
    )
    report = CommandReport(
        command="loop.run", applied=True, outcome="activation_complete", items=(item,)
    )

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        assert apply
        return report

    output: list[str] = []
    assert (
        main(
            ["loop", "run", "--apply", "--json"],
            dispatch=dispatch,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output[0])["items"][0]["worker_exit"] == {
        "returncode": -15,
        "diagnostic": "transport closed",
    }
    output.clear()
    assert (
        main(["loop", "run", "--apply"], dispatch=dispatch, write_stdout=output.append)
        == 0
    )
    assert "returncode=-15" in output[0]
    assert "worker_diagnostic=transport closed" in output[0]


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "run"],
        ["loop", "status"],
        ["loop", "retry", "--issue-iid", "12"],
        ["migration", "preview"],
        ["migration", "apply", "--plan-file", "plan.json"],
        ["recovery", "inventory", "--issue-iid", "12", "--json", "--apply"],
        [
            "recovery",
            "resolve",
            "--issue-iid",
            "12",
            "--choice",
            "retry",
            "--reason",
            "trusted prerequisite is closed",
        ],
    ],
)
def test_parser_exposes_the_nested_resource_action_contract(
    arguments: list[str],
) -> None:
    assert build_parser().parse_args(arguments)


def test_credential_rotate_parser_requires_noninteractive_target_inputs() -> None:
    parsed = build_parser().parse_args(
        [
            "credential",
            "rotate",
            "--environment-file",
            "/etc/agent-work.env",
            "--unit-name",
            "agent-work",
            "--apply",
            "--json",
            "--debug",
        ]
    )

    assert parsed.resource == "credential"
    assert parsed.action == "rotate"
    assert parsed.environment_file == Path("/etc/agent-work.env")
    assert parsed.unit_name == "agent-work"
    assert parsed.apply is True
    assert parsed.json is True
    assert parsed.debug is True


def test_bare_loop_run_is_a_read_only_preview() -> None:
    seen: list[bool] = []

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        seen.append(apply)
        return CommandReport(command="loop.run", applied=apply, outcome="idle")

    output: list[str] = []
    assert (
        main(["loop", "run", "--json"], dispatch=dispatch, write_stdout=output.append)
        == 0
    )
    assert seen == [False]
    assert json.loads(output[0])["applied"] is False


def test_validation_run_cli_forwards_preview_and_renders_bounded_json() -> None:
    calls: list[tuple[str, bool, int | None]] = []

    def validation_run(
        *, candidate_tree: str, apply: bool, wait_seconds: int | None = None
    ) -> SimpleNamespace:
        calls.append((candidate_tree, apply, wait_seconds))
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=apply,
            action="preview",
            state="preview",
            terminal=False,
            exit_code=None,
            diagnostic=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    output: list[str] = []
    assert (
        main(
            ["validation", "run", "--candidate-tree", "a" * 40, "--json"],
            validation_run=validation_run,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [("a" * 40, False, None)]
    assert json.loads(output[0]) == {
        "candidate_tree": "a" * 40,
        "applied": False,
        "action": "preview",
        "state": "preview",
        "terminal": False,
        "exit_code": None,
        "diagnostic": None,
        "hook_failures": [],
        "junit_artifacts": [],
        "failed_tests": [],
        "failed_junit_digests": [],
        "elapsed_seconds": None,
        "junit_timings": [],
    }


def test_validation_cli_renders_complete_over_100ms_case_evidence() -> None:
    """The trusted JSON response gives reviewers more than the top three."""
    timing = SimpleNamespace(
        environment="py311",
        total_seconds=6.100001,
        slowest_cases=(
            SimpleNamespace(name="test_one", seconds=3),
            SimpleNamespace(name="test_two", seconds=2),
            SimpleNamespace(name="test_three", seconds=1),
        ),
        timing_complete=True,
        over_100ms_cases=(
            SimpleNamespace(name="test_one", seconds=3),
            SimpleNamespace(name="test_two", seconds=2),
            SimpleNamespace(name="test_three", seconds=1),
            SimpleNamespace(name="test_four", seconds=0.100001),
        ),
    )

    def validation_run(
        *, candidate_tree: str, wait_seconds: int | None = None
    ) -> SimpleNamespace:
        del wait_seconds
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=False,
            action="status",
            state="terminal-success",
            terminal=True,
            exit_code=0,
            diagnostic=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=6.100001,
            junit_timings=(timing,),
        )

    output: list[str] = []
    assert (
        main(
            ["validation", "status", "--candidate-tree", "a" * 40, "--json"],
            validation_status=validation_run,
            write_stdout=output.append,
        )
        == 0
    )
    rendered = json.loads(output[0])["junit_timings"][0]
    assert rendered["timing_complete"] is True
    assert [case["name"] for case in rendered["over_100ms_cases"]] == [
        "test_one",
        "test_two",
        "test_three",
        "test_four",
    ]


def test_candidate_cli_previews_and_forwards_live_identity(
    tmp_path: Path,
) -> None:
    """Preview is read-only; apply forwards identity and reports Judge failure."""
    calls: list[tuple[Path, str, int, str, str | None]] = []
    tree = "b" * 40
    nonce = "0123456789abcdef" * 2
    socket_path = tmp_path / "candidate.sock"

    def request(
        path: Path,
        identity: WorkerResultBrokerClientRequest,
        *,
        action: str,
        candidate_tree: str | None,
    ) -> dict[str, object]:
        calls.append(
            (
                path,
                identity.nonce,
                identity.issue_iid,
                action,
                candidate_tree,
            )
        )
        return {
            "candidate_tree": tree,
            "state": (
                "staged"
                if action == "stage"
                else "read"
                if action == "read"
                else "terminal-failure"
            ),
            "failed_tests": ["tests.test_example::test_failure"],
            **(
                {
                    "changed_paths": ["docs/guide.md"],
                    "files": [
                        {
                            "path": "docs/guide.md",
                            "old_mode": "100644",
                            "new_mode": "100644",
                            "old_content": "before\n",
                            "new_content": "after\n",
                        }
                    ],
                }
                if action == "read"
                else {}
            ),
        }

    output: list[str] = []
    assert (
        main(
            ["candidate", "stage", "--issue-iid", "81", "--json"],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop()) == {
        "action": "stage",
        "applied": False,
        "candidate_tree": None,
        "issue_iid": 81,
        "state": "preview",
    }
    assert calls == []
    common = [
        "--issue-iid",
        "81",
        "--candidate-socket",
        str(socket_path),
        "--broker-nonce",
        nonce,
        "--apply",
    ]
    assert (
        main(
            ["candidate", "stage", *common],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    assert output.pop().startswith(f"candidate_tree={tree} state=staged")
    assert (
        main(
            ["candidate", "read", *common, "--candidate-tree", tree, "--json"],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop())["state"] == "read"
    assert (
        main(
            ["candidate", "read", *common, "--candidate-tree", tree],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    human = output.pop()
    assert "Candidate read" in human
    assert "Path: docs/guide.md" in human
    assert 'Old content: "before\\n"' in human
    assert 'New content: "after\\n"' in human
    assert not human.lstrip().startswith("{")
    assert (
        main(
            ["candidate", "validate", *common, "--candidate-tree", tree, "--json"],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 1
    )
    assert json.loads(output.pop())["failed_tests"] == [
        "tests.test_example::test_failure"
    ]
    assert calls == [
        (socket_path, nonce, 81, "stage", None),
        (socket_path, nonce, 81, "read", tree),
        (socket_path, nonce, 81, "read", tree),
        (socket_path, nonce, 81, "validate", tree),
    ]


def test_candidate_read_apply_requires_live_identity() -> None:
    errors: list[str] = []
    assert (
        main(
            [
                "candidate",
                "read",
                "--issue-iid",
                "81",
                "--candidate-tree",
                "b" * 40,
                "--apply",
            ],
            write_stderr=errors.append,
        )
        == 2
    )
    assert errors == ["--apply requires --candidate-socket and --broker-nonce"]


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize(
    ("flags", "mode", "chunk_fields"),
    [
        ([], "plain", {}),
        (["--manifest"], "manifest", {}),
        (["--complete"], "complete", {}),
        (
            [
                "--path",
                "docs/guide.md",
                "--side",
                "old",
                "--offset",
                "3",
                "--length",
                "5",
            ],
            "chunk",
            {"path": "docs/guide.md", "side": "old", "offset": 3, "length": 5},
        ),
    ],
)
def test_candidate_read_preview_shows_exact_selector_without_request(
    flags: list[str], mode: str, chunk_fields: dict[str, object], *, as_json: bool
) -> None:
    output: list[str] = []

    def unexpected_request(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("dry-run preview must not contact the candidate broker")

    args = [
        "candidate",
        "read",
        "--issue-iid",
        "81",
        "--candidate-tree",
        "b" * 40,
        *flags,
        *(("--json",) if as_json else ()),
    ]
    assert (
        main(args, candidate_request=unexpected_request, write_stdout=output.append)
        == 0
    )
    assert len(output) == 1
    if as_json:
        preview = json.loads(output[0])
        assert preview["read_mode"] == mode
        assert {key: preview[key] for key in chunk_fields} == chunk_fields
    else:
        assert f"read_mode={mode}" in output[0]
        for key, value in chunk_fields.items():
            expected = json.dumps(value) if key == "path" else value
            assert f"{key}={expected}" in output[0]


def test_candidate_read_cli_forwards_paged_selectors_and_rejects_invalid_shape(
    tmp_path: Path,
) -> None:
    tree = "b" * 40
    selectors: list[dict[str, object]] = []

    def request(
        _path: Path,
        _identity: WorkerResultBrokerClientRequest,
        *,
        action: str,
        candidate_tree: str | None,
        selector: dict[str, object],
    ) -> dict[str, object]:
        assert action == "read" and candidate_tree == tree
        selectors.append(selector)
        return {"candidate_tree": tree, "state": "read-complete"}

    common = [
        "candidate",
        "read",
        "--issue-iid",
        "81",
        "--candidate-tree",
        tree,
        "--candidate-socket",
        str(tmp_path / "candidate.sock"),
        "--broker-nonce",
        "a" * 32,
        "--apply",
        "--json",
    ]
    output: list[str] = []
    for flags in (
        ["--manifest"],
        ["--path", "docs/guide.md", "--side", "old", "--offset", "0", "--length", "5"],
        ["--complete"],
    ):
        assert (
            main(
                [*common, *flags], candidate_request=request, write_stdout=output.append
            )
            == 0
        )
        output.pop()
    assert selectors == [
        {"mode": "manifest"},
        {
            "mode": "chunk",
            "path": "docs/guide.md",
            "side": "old",
            "offset": 0,
            "length": 5,
        },
        {"mode": "complete"},
    ]
    errors: list[str] = []
    assert main([*common, "--manifest", "--complete"], write_stderr=errors.append) == 2
    assert errors == ["candidate read selector is invalid"]


def test_candidate_read_human_page_and_manifest_are_labeled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = "b" * 40

    def request(
        _path: Path,
        _identity: WorkerResultBrokerClientRequest,
        *,
        action: str,
        candidate_tree: str | None,
        selector: dict[str, object],
    ) -> dict[str, object]:
        assert action == "read" and candidate_tree == tree
        if selector["mode"] == "manifest":
            return {
                "candidate_tree": tree,
                "paged": True,
                "files": [
                    {
                        "path": "docs/guide.md",
                        "old_mode": "100644",
                        "old_oid": "c" * 40,
                        "old_size": 6,
                        "old_sha256": "d" * 64,
                        "new_mode": None,
                    }
                ],
            }
        return {
            "candidate_tree": tree,
            "path": "docs/guide.md",
            "side": "old",
            "offset": 0,
            "length": 6,
            "data_base64": "YmVmb3Jl",
            "sha256": "d" * 64,
        }

    common = [
        "candidate",
        "read",
        "--issue-iid",
        "81",
        "--candidate-tree",
        tree,
        "--candidate-socket",
        str(tmp_path / "candidate.sock"),
        "--broker-nonce",
        "a" * 32,
        "--apply",
    ]
    output: list[str] = []
    assert (
        main(
            [*common, "--manifest"],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    manifest = output.pop()
    assert "Paged manifest:" in manifest
    assert "Old bytes: 6" in manifest
    assert (
        main(
            [
                *common,
                "--path",
                "docs/guide.md",
                "--side",
                "old",
                "--offset",
                "0",
                "--length",
                "6",
            ],
            candidate_request=request,
            write_stdout=output.append,
        )
        == 0
    )
    page = output.pop()
    assert "Bytes (base64): YmVmb3Jl" in page
    assert "Whole-side SHA-256:" in page
    with pytest.raises(SystemExit) as help_exit:
        main(["candidate", "read", "--help"])
    assert help_exit.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "exclusive with --complete and chunk flags" in help_text
    assert "chunk byte count" in help_text


def test_validation_status_cli_never_treats_running_as_success() -> None:
    calls: list[tuple[str, int | None]] = []

    def validation_status(
        *, candidate_tree: str, wait_seconds: int | None = None
    ) -> SimpleNamespace:
        calls.append((candidate_tree, wait_seconds))
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=False,
            action="status",
            state="running",
            terminal=False,
            exit_code=None,
            diagnostic=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    output: list[str] = []
    assert (
        main(
            [
                "validation",
                "status",
                "--candidate-tree",
                "a" * 40,
                "--wait-seconds",
                "3",
                "--apply",
                "--json",
            ],
            validation_status=validation_status,
            write_stdout=output.append,
        )
        == 1
    )
    assert calls == [("a" * 40, 3)]
    assert json.loads(output[0])["terminal"] is False
    assert json.loads(output[0])["exit_code"] is None
    assert json.loads(output[0])["junit_artifacts"] == []


def test_validation_run_cli_forwards_apply_and_requires_terminal_success() -> None:
    calls: list[tuple[str, bool]] = []

    def validation_run(
        *, candidate_tree: str, apply: bool, **_kwargs: object
    ) -> SimpleNamespace:
        calls.append((candidate_tree, apply))
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=True,
            action="ran",
            state="terminal-success",
            terminal=True,
            exit_code=0,
            diagnostic=None,
            hook_failures=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=1.0,
            junit_timings=(),
            junit_artifacts=(
                SimpleNamespace(
                    environment="py311",
                    path=Path("/snapshots") / candidate_tree / "test-results-py311.xml",
                    sha256="a" * 64,
                    test_count=3,
                ),
                SimpleNamespace(
                    environment="py314",
                    path=Path("/snapshots") / candidate_tree / "test-results-py314.xml",
                    sha256="b" * 64,
                    test_count=5,
                ),
            ),
        )

    output: list[str] = []
    assert (
        main(
            ["validation", "run", "--candidate-tree", "a" * 40, "--apply"],
            validation_run=validation_run,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [("a" * 40, True)]
    assert "junit_artifacts=" in output[0]
    assert f"/snapshots/{'a' * 40}/test-results-py311.xml" in output[0]
    assert "py311" in output[0]

    json_output: list[str] = []
    assert (
        main(
            [
                "validation",
                "run",
                "--candidate-tree",
                "a" * 40,
                "--apply",
                "--json",
            ],
            validation_run=validation_run,
            write_stdout=json_output.append,
        )
        == 0
    )
    assert json.loads(json_output[0])["junit_artifacts"] == [
        {
            "environment": "py311",
            "path": f"/snapshots/{'a' * 40}/test-results-py311.xml",
            "sha256": "a" * 64,
            "test_count": 3,
        },
        {
            "environment": "py314",
            "path": f"/snapshots/{'a' * 40}/test-results-py314.xml",
            "sha256": "b" * 64,
            "test_count": 5,
        },
    ]


@pytest.mark.parametrize(
    ("checker_outcome", "checker_returncode", "hook_count"),
    (
        ("violations", 1, 0),
        ("compliant", 0, 1),
    ),
)
def test_validation_default_cli_runs_exact_license_preflight_before_hook(
    tmp_path: Path,
    checker_outcome: str,
    checker_returncode: int,
    hook_count: int,
) -> None:
    class HookReached(Exception):
        """Stop after the preflight ordering boundary has been observed."""

    tree = "a" * 40
    calls: list[tuple[str, ...]] = []
    checker_command = (
        sys.executable,
        "scripts/check_licenses.py",
        "--root",
        ".",
        "--policy",
        ".license-policy.toml",
        "--candidate-tree",
        tree,
        "--json",
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        if command == ("git", "rev-parse", "--git-dir"):
            return CommandResult(0, str(tmp_path / "git"), "")
        if command == ("git", "write-tree"):
            return CommandResult(0, tree, "")
        if command == checker_command:
            violations = (
                [{"path": "bad.py", "message": "missing NVIDIA license header"}]
                if checker_outcome == "violations"
                else []
            )
            return CommandResult(
                checker_returncode,
                json.dumps(
                    {
                        "schema_version": 1,
                        "outcome": checker_outcome,
                        "violations": violations,
                    }
                ),
                "",
            )
        assert command == ("git", "hook", "run", "pre-commit")
        raise HookReached

    def run_hook(command: tuple[str, ...], _lease: object) -> CommandResult:
        return run(command)

    output: list[str] = []
    removed: list[Path] = []
    assert not (tmp_path / ".venv").exists()

    def invoke() -> int:
        return main(
            ["validation", "run", "--candidate-tree", tree, "--apply", "--json"],
            run_command=run,
            run_validation_hook=run_hook,
            environment={
                "XDG_STATE_HOME": str(tmp_path / "state"),
                "GITLAB_HOST": "git.example.test",
                "GITLAB_PROJECT_ID": "37",
            },
            write_stdout=output.append,
            remove_validation_artifact=removed.append,
        )

    if hook_count:
        with pytest.raises(HookReached):
            invoke()
        assert output == []
    else:
        assert invoke() == 1
        assert json.loads(output[0])["state"] == "preflight-failure"
    assert calls.count(checker_command) == 1
    assert calls.count(("git", "hook", "run", "pre-commit")) == hook_count
    assert removed == (
        [Path("test-results-py311.xml"), Path("test-results-py314.xml")]
        if hook_count
        else []
    )
    if hook_count:
        assert calls.index(checker_command) < calls.index(
            ("git", "hook", "run", "pre-commit")
        )
    else:
        assert not tuple((tmp_path / "state").rglob("*.json"))


@pytest.mark.parametrize("action", ("run", "status"))
@pytest.mark.parametrize("as_json", (False, True), ids=("human", "json"))
def test_validation_cli_projects_terminal_failure_diagnostic(
    *, action: str, as_json: bool
) -> None:
    diagnostic = (
        "git hook run pre-commit failed with exit code 5; "
        "JUnit py311 failures=2 errors=1"
    )

    def validation_action(
        *,
        candidate_tree: str,
        apply: bool = False,
        wait_seconds: int | None = None,
    ) -> SimpleNamespace:
        del wait_seconds
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=apply,
            action="ran" if apply else "status",
            state="terminal-failure",
            terminal=True,
            exit_code=5,
            diagnostic=diagnostic,
            hook_failures=(
                SimpleNamespace(
                    hook_id="ruff",
                    diagnostic_lines=("src/agent_work/example.py:7:3: F401",),
                ),
            ),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    output: list[str] = []
    arguments = ["validation", action, "--candidate-tree", "a" * 40]
    if action == "run":
        arguments.append("--apply")
    if as_json:
        arguments.append("--json")

    assert (
        main(
            arguments,
            validation_run=validation_action,
            validation_status=validation_action,
            write_stdout=output.append,
        )
        == 1
    )
    if as_json:
        assert json.loads(output[0])["diagnostic"] == diagnostic
        assert json.loads(output[0])["hook_failures"] == [
            {
                "hook_id": "ruff",
                "diagnostic_lines": ["src/agent_work/example.py:7:3: F401"],
            }
        ]
        assert json.loads(output[0])["junit_artifacts"] == []
    else:
        assert f"diagnostic={diagnostic}" in output[0]
        assert "failed_hook=ruff" in output[0]
        assert "src/agent_work/example.py:7:3: F401" in output[0]


def test_validation_cli_injects_distinct_typed_run_and_status_actions() -> None:
    calls: list[str] = []

    def validation_run(*, candidate_tree: str, apply: bool) -> SimpleNamespace:
        calls.append(f"run:{candidate_tree}:{apply}")
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=apply,
            action="preview",
            state="preview",
            terminal=False,
            exit_code=None,
            diagnostic=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    def validation_status(
        *, candidate_tree: str, wait_seconds: int | None
    ) -> SimpleNamespace:
        calls.append(f"status:{candidate_tree}:{wait_seconds}")
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=False,
            action="status",
            state="terminal-success",
            terminal=True,
            exit_code=0,
            diagnostic=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    assert (
        main(
            ["validation", "run", "--candidate-tree", "a" * 40],
            validation_run=validation_run,
            validation_status=validation_status,
        )
        == 0
    )
    assert (
        main(
            ["validation", "status", "--candidate-tree", "a" * 40],
            validation_run=validation_run,
            validation_status=validation_status,
        )
        == 0
    )
    assert calls == [f"run:{'a' * 40}:False", f"status:{'a' * 40}:None"]


def test_validation_cli_help_and_argument_errors_use_argparse_statuses() -> None:
    with pytest.raises(SystemExit) as help_exit:
        main(["validation", "run", "--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as invalid_exit:
        main(["validation", "run", "--candidate-tree", "not-a-tree"])
    assert invalid_exit.value.code == 2


@pytest.mark.parametrize("action", ("run", "status"))
def test_validation_cli_json_projects_bounded_artifact_failure(action: str) -> None:
    report = SimpleNamespace(
        candidate_tree="a" * 40,
        applied=action == "run",
        action="ran" if action == "run" else "status",
        state="terminal-artifact-failure",
        terminal=True,
        exit_code=0,
        diagnostic=None,
        artifact_failure=SimpleNamespace(
            stage="post_hook_success_evidence_read",
            reason="invalid_count",
            environment="py311",
            path=Path("test-results-py311.xml"),
        ),
        hook_failures=(),
        junit_artifacts=(),
        failed_tests=(),
        failed_junit_digests=(),
        elapsed_seconds=0.1,
        junit_timings=(),
    )
    output: list[str] = []
    arguments = ["validation", action, "--candidate-tree", "a" * 40, "--json"]
    if action == "run":
        arguments.append("--apply")

    exit_code = main(
        arguments,
        validation_run=lambda **_kwargs: report,
        validation_status=lambda **_kwargs: report,
        write_stdout=output.append,
    )

    assert exit_code == 1
    assert json.loads(output[0])["artifact_failure"] == {
        "stage": "post_hook_success_evidence_read",
        "reason": "invalid_count",
        "environment": "py311",
        "path": "test-results-py311.xml",
    }


def test_validation_retry_cli_supports_help_json_dry_run_and_apply(
    capsys: pytest.CaptureFixture[str],
) -> None:
    candidate = "a" * 40
    calls: list[tuple[str, bool]] = []
    output: list[str] = []

    def retry(*, candidate_tree: str, apply: bool) -> SimpleNamespace:
        calls.append((candidate_tree, apply))
        return SimpleNamespace(
            candidate_tree=candidate_tree,
            applied=apply,
            action="retried" if apply else "retry_preview",
            state="terminal-success" if apply else "preview",
            terminal=apply,
            exit_code=0 if apply else None,
            diagnostic=None,
            artifact_failure=None,
            hook_failures=(),
            junit_artifacts=(),
            failed_tests=(),
            failed_junit_digests=(),
            elapsed_seconds=None,
            junit_timings=(),
        )

    with pytest.raises(SystemExit) as help_exit:
        main(["validation", "retry", "--help"], validation_retry=retry)
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "Without --apply, preview" in help_text
    assert "current staged tree" in help_text
    with pytest.raises(SystemExit) as malformed_exit:
        main(
            ["validation", "retry", "--candidate-tree", "bad"],
            validation_retry=retry,
        )
    assert malformed_exit.value.code == 2

    assert (
        main(
            ["validation", "retry", "--candidate-tree", candidate, "--json"],
            validation_retry=retry,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output[-1])["action"] == "retry_preview"
    assert (
        main(
            ["validation", "retry", "--candidate-tree", candidate, "--apply", "--json"],
            validation_retry=retry,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output[-1])["action"] == "retried"
    assert calls == [(candidate, False), (candidate, True)]


def test_merge_request_publish_cli_passes_structured_fields_to_the_injected_boundary() -> (
    None
):
    calls: list[Mapping[str, object]] = []

    def publish_merge_request(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> MergeRequestPublicationReport:
        kwargs = {
            "source_branch": source_branch,
            "target_branch": target_branch,
            "title": title,
            "problem": problem,
            "outcome": outcome,
            "validation": validation,
            "impact": impact,
            "human_action": human_action,
            "tracking": tracking,
            "apply": apply,
            "issue_iid": issue_iid,
        }
        calls.append(kwargs)
        return MergeRequestPublicationReport(
            apply,
            "would-create",
            None,
            render_merge_request_description(
                problem=problem,
                outcome=outcome,
                validation=validation,
                impact=impact,
                human_action=human_action,
                tracking=tracking,
                issue_iid=issue_iid,
            ),
        )

    output: list[str] = []
    assert (
        main(
            [
                "merge-request",
                "publish",
                "--json",
                "--source-branch",
                "clanker/structured-mr-descriptions",
                "--target-branch",
                "main",
                "--title",
                "Readable request",
                "--problem",
                "Bodies escaped newlines.",
                "--outcome",
                "Publish Markdown.",
                "--validation",
                "uv run pytest",
                "--validation",
                "manual dry run",
                "--impact",
                "Review quality suffers.",
                "--human-action",
                "Review it.",
                "--tracking",
                "Relates to #123",
            ],
            publish_merge_request=publish_merge_request,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [
        {
            "source_branch": "clanker/structured-mr-descriptions",
            "target_branch": "main",
            "title": "Readable request",
            "problem": "Bodies escaped newlines.",
            "outcome": "Publish Markdown.",
            "validation": ("uv run pytest", "manual dry run"),
            "impact": "Review quality suffers.",
            "human_action": "Review it.",
            "tracking": "Relates to #123",
            "apply": False,
            "issue_iid": None,
        }
    ]
    assert json.loads(output[0]) == {
        "applied": False,
        "action": "would-create",
        "description": (
            "## Problem\nBodies escaped newlines.\n\n"
            "## Impact\nReview quality suffers.\n\n"
            "## Outcome\nPublish Markdown.\n\n"
            "## Human action\nReview it.\n\n"
            "## Validation\n- uv run pytest\n- manual dry run\n\n"
            "## Tracking\nRelates to #123"
        ),
    }


def test_merge_request_publish_cli_help_and_argument_errors_use_argparse_statuses() -> (
    None
):
    with pytest.raises(SystemExit) as help_exit:
        main(["merge-request", "publish", "--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as invalid_exit:
        main(["merge-request", "publish", "--source-branch", "topic"])
    assert invalid_exit.value.code == 2


def test_merge_request_preview_forwards_issue_iid_and_renders_one_canonical_close() -> (
    None
):
    calls: list[int | None] = []

    def publisher(
        *, issue_iid: int | None = None, **_kwargs: object
    ) -> MergeRequestPublicationReport:
        calls.append(issue_iid)
        return MergeRequestPublicationReport(
            False, "would-create", None, "Body\n\nCloses #123"
        )

    output: list[str] = []
    assert (
        main(
            [
                "merge-request",
                "publish",
                "--json",
                "--source-branch",
                "topic",
                "--target-branch",
                "main",
                "--title",
                "Title",
                "--problem",
                "Problem",
                "--outcome",
                "Outcome",
                "--validation",
                "pytest",
                "--issue-iid",
                "123",
            ],
            publish_merge_request=publisher,
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [123]
    assert json.loads(output[0])["description"].splitlines()[-1] == "Closes #123"


@pytest.mark.parametrize("value", ("0", "-1", "not-a-number"))
def test_merge_request_publish_cli_rejects_malformed_issue_iid(value: str) -> None:
    with pytest.raises(SystemExit) as invalid_exit:
        main(
            [
                "merge-request",
                "publish",
                "--source-branch",
                "topic",
                "--target-branch",
                "main",
                "--title",
                "Title",
                "--problem",
                "Problem",
                "--outcome",
                "Outcome",
                "--validation",
                "pytest",
                "--issue-iid",
                value,
            ]
        )
    assert invalid_exit.value.code == 2


def test_merge_request_publish_cli_returns_one_for_a_publication_boundary_failure() -> (
    None
):
    def failing_publish_merge_request(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> MergeRequestPublicationReport:
        del source_branch, target_branch, title, problem, outcome, validation, impact
        del human_action, tracking, apply, issue_iid
        raise RuntimeError("GitLab did not return the requested merge request")

    errors: list[str] = []
    assert (
        main(
            [
                "merge-request",
                "publish",
                "--source-branch",
                "clanker/structured-mr-descriptions",
                "--target-branch",
                "main",
                "--title",
                "Readable request",
                "--problem",
                "Bodies escaped newlines.",
                "--outcome",
                "Publish Markdown.",
                "--validation",
                "uv run pytest",
            ],
            publish_merge_request=failing_publish_merge_request,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["GitLab did not return the requested merge request"]


def test_merge_request_publish_cli_text_preview_includes_rendered_description() -> None:
    output: list[str] = []

    def publish_merge_request(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> MergeRequestPublicationReport:
        del source_branch, target_branch, title
        return MergeRequestPublicationReport(
            apply,
            "would-create",
            None,
            render_merge_request_description(
                problem=problem,
                outcome=outcome,
                validation=validation,
                impact=impact,
                human_action=human_action,
                tracking=tracking,
                issue_iid=issue_iid,
            ),
        )

    assert (
        main(
            [
                "merge-request",
                "publish",
                "--source-branch",
                "clanker/structured-mr-descriptions",
                "--target-branch",
                "main",
                "--title",
                "Readable request",
                "--problem",
                "Bodies escaped newlines.",
                "--outcome",
                "Publish Markdown.",
                "--validation",
                "uv run pytest",
            ],
            publish_merge_request=publish_merge_request,
            write_stdout=output.append,
        )
        == 0
    )
    assert "## Problem\nBodies escaped newlines." in output[0]


def test_merge_request_publish_cli_apply_passes_mutation_opt_in_to_boundary() -> None:
    calls: list[bool] = []

    def publish_merge_request(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> MergeRequestPublicationReport:
        del source_branch, target_branch, title, problem, outcome, validation, impact
        del human_action, tracking, issue_iid
        calls.append(apply)
        evidence = MergeRequestEvidence.model_validate(
            {
                "iid": 1,
                "web_url": "https://gitlab.example.com/mr/1",
                "state": "opened",
                "source_project_id": 310967,
                "target_project_id": 310967,
                "source_branch": "clanker/structured-mr-descriptions",
                "target_branch": "main",
                "sha": "a" * 40,
                "has_conflicts": False,
                "description": "description",
                "title": "Readable request",
                "draft": False,
            }
        )
        return MergeRequestPublicationReport(True, "create", evidence, "description")

    assert (
        main(
            [
                "merge-request",
                "publish",
                "--source-branch",
                "clanker/structured-mr-descriptions",
                "--target-branch",
                "main",
                "--title",
                "Readable request",
                "--problem",
                "Bodies escaped newlines.",
                "--outcome",
                "Publish Markdown.",
                "--validation",
                "uv run pytest",
                "--apply",
            ],
            publish_merge_request=publish_merge_request,
            write_stdout=lambda _value: None,
        )
        == 0
    )
    assert calls == [True]


@pytest.mark.parametrize(
    ("requested_apply", "returned_apply"), [(False, True), (True, False)]
)
def test_merge_request_publish_cli_rejects_mismatched_apply_result(
    requested_apply: bool,
    returned_apply: bool,
) -> None:
    evidence = MergeRequestEvidence.model_validate(
        {
            "iid": 1,
            "web_url": "https://gitlab.example.com/mr/1",
            "state": "opened",
            "source_project_id": 310967,
            "target_project_id": 310967,
            "source_branch": "topic",
            "target_branch": "main",
            "sha": "a" * 40,
            "has_conflicts": False,
            "description": "body",
            "title": "Title",
            "draft": False,
        }
    )

    def publish_merge_request(
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        problem: str,
        outcome: str,
        validation: tuple[str, ...],
        impact: str | None,
        human_action: str | None,
        tracking: str | None,
        apply: bool,
        issue_iid: int | None = None,
    ) -> MergeRequestPublicationReport:
        del source_branch, target_branch, title, problem, outcome, validation, impact
        del human_action, tracking, apply, issue_iid
        return MergeRequestPublicationReport(
            returned_apply, "create", evidence if returned_apply else None, "body"
        )

    errors: list[str] = []
    arguments = [
        "merge-request",
        "publish",
        "--source-branch",
        "topic",
        "--target-branch",
        "main",
        "--title",
        "Title",
        "--problem",
        "Problem",
        "--outcome",
        "Outcome",
        "--validation",
        "pytest",
    ]
    if requested_apply:
        arguments.append("--apply")
    assert (
        main(
            arguments,
            publish_merge_request=publish_merge_request,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["publication result did not match requested apply state"]


def test_merge_request_publish_cli_default_dry_run_composes_numeric_port_without_mutation() -> (
    None
):
    commands: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(command)
        return CommandResult(0, "[]", "")

    output: list[str] = []
    assert (
        main(
            [
                "merge-request",
                "publish",
                "--json",
                "--source-branch",
                "clanker/structured-mr-descriptions",
                "--target-branch",
                "main",
                "--title",
                "Readable request",
                "--problem",
                "Bodies escaped newlines.",
                "--outcome",
                "Publish Markdown.",
                "--validation",
                "uv run pytest",
                "--tracking",
                "Relates to #123",
                "--issue-iid",
                "123",
            ],
            environment={
                "GITLAB_HOST": "gitlab.example.com",
                "GITLAB_PROJECT_ID": "310967",
            },
            find_executable=lambda _name: "/dedicated/bin/glab",
            run_command=run_command,
            write_stdout=output.append,
        )
        == 0
    )
    assert len(commands) == 1
    assert commands[0][commands[0].index("-X") + 2] == "projects/310967/merge_requests"
    assert commands[0][commands[0].index("-X") + 1] == "GET"
    assert "state=opened" in commands[0]
    assert "source_branch=clanker/structured-mr-descriptions" in commands[0]
    assert not any("POST" in command or "PUT" in command for command in commands)
    assert json.loads(output[0]) == {
        "applied": False,
        "action": "would-create",
        "description": (
            "## Problem\nBodies escaped newlines.\n\n"
            "## Outcome\nPublish Markdown.\n\n"
            "## Validation\n- uv run pytest\n\n"
            "## Tracking\nRelates to #123\n\nCloses #123"
        ),
    }


def test_merge_request_publish_cli_forwards_the_injected_one_second_sleeper() -> None:
    description = render_merge_request_description(
        problem="Problem",
        outcome="Outcome",
        validation=("pytest",),
        issue_iid=123,
    )
    evidence = {
        "iid": 88,
        "web_url": "https://gitlab.example.com/mr/88",
        "state": "opened",
        "source_project_id": 310967,
        "target_project_id": 310967,
        "source_branch": "topic",
        "target_branch": "main",
        "sha": "a" * 40,
        "has_conflicts": False,
        "description": description,
        "title": "Title",
        "draft": False,
    }
    responses: list[object] = [
        [],
        evidence,
        evidence,
        [],
        [{"project_id": 310967, "iid": 123}],
    ]
    delays: list[float] = []

    def run_command(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        del command
        return CommandResult(0, json.dumps(responses.pop(0)), "")

    assert (
        main(
            [
                "merge-request",
                "publish",
                "--source-branch",
                "topic",
                "--target-branch",
                "main",
                "--title",
                "Title",
                "--problem",
                "Problem",
                "--outcome",
                "Outcome",
                "--validation",
                "pytest",
                "--issue-iid",
                "123",
                "--apply",
            ],
            environment={
                "GITLAB_HOST": "gitlab.example.com",
                "GITLAB_PROJECT_ID": "310967",
            },
            find_executable=lambda _name: "/dedicated/bin/glab",
            run_command=run_command,
            sleeper=delays.append,
            write_stdout=lambda _value: None,
        )
        == 0
    )
    assert delays == [1.0]
    assert responses == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "canary"],
        ["loop", "canary", "--json"],
        ["loop", "canary", "--apply", "--plan-file", "canary-plan.json"],
        ["loop", "recover", "--issue-iid", "76"],
        [
            "loop",
            "recover",
            "--issue-iid",
            "76",
            "--apply",
            "--plan-file",
            "recover-76.json",
            "--max-segments",
            "2",
            "--max-activation-seconds",
            "120",
        ],
    ],
)
def test_operational_plan_commands_expose_the_plan_bound_cli_contract(
    arguments: list[str],
) -> None:
    """Canary and recovery keep preview harmless and bind apply to a plan file."""
    assert build_parser().parse_args(arguments)


def test_recover_help_explains_the_bounded_activation_defaults(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Recovery help documents both reviewed execution bounds and their defaults."""
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["loop", "recover", "--help"])

    help_text = " ".join(capsys.readouterr().out.split())

    assert "maximum recovery segments per applied activation (default: 16)" in help_text
    assert "maximum seconds for one applied activation (default: 10800)" in help_text


@pytest.mark.parametrize(
    "arguments",
    [
        ["loop", "canary", "--apply"],
        ["loop", "recover", "--issue-iid", "76", "--apply"],
    ],
)
def test_operational_apply_requires_a_reviewed_plan_before_dispatch(
    arguments: list[str],
) -> None:
    """An apply invocation without its preview plan is CLI misuse, never a mutation."""
    dispatched = False
    errors: list[str] = []

    def dispatch_must_not_run(_request: object, *, apply: bool) -> CommandReport:
        nonlocal dispatched
        del apply
        dispatched = True
        raise AssertionError("planless apply must be rejected before dispatch")

    assert (
        main(
            arguments,
            dispatch=dispatch_must_not_run,
            write_stderr=errors.append,
        )
        == 2
    )
    assert not dispatched
    assert errors and "plan-file" in errors[0]


def test_canary_default_preview_and_recover_apply_preserve_exact_request_boundary() -> (
    None
):
    """The CLI supplies a read-only canary and one exact recovery target to DI."""
    seen: list[tuple[CommandRequest, bool]] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        typed = cast(CommandRequest, request)
        seen.append((typed, apply))
        return CommandReport(
            command=typed.command,
            applied=apply,
            outcome="checkpoint",
        )

    preview: list[str] = []
    assert (
        main(
            ["loop", "canary", "--json"],
            dispatch=dispatch,
            write_stdout=preview.append,
        )
        == 0
    )
    assert json.loads(preview[0])["applied"] is False
    assert seen[0] == (CommandRequest(command="loop.canary"), False)

    assert (
        main(
            [
                "loop",
                "recover",
                "--issue-iid",
                "76",
                "--apply",
                "--plan-file",
                "recover-76.json",
                "--max-segments",
                "3",
                "--max-activation-seconds",
                "180",
            ],
            dispatch=dispatch,
        )
        == 0
    )
    assert seen[1] == (
        CommandRequest(
            command="loop.recover",
            issue_iid=76,
            plan_file=Path("recover-76.json"),
            max_segments=3,
            max_activation_seconds=180,
        ),
        True,
    )


@pytest.mark.parametrize("action", ["canary", "recover"])
def test_operational_plan_command_help_describes_preview_and_plan_bound_apply(
    action: str, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["loop", action]
    if action == "recover":
        arguments.extend(["--issue-iid", "76"])
    arguments.append("--help")

    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(arguments)

    help_text = " ".join(capsys.readouterr().out.split()).lower()
    assert raised.value.code == 0
    assert "preview" in help_text
    assert "plan-file" in help_text
    assert "apply" in help_text


def test_apply_flag_reaches_the_single_dispatch_boundary() -> None:
    seen: list[tuple[CommandRequest, bool]] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        seen.append((cast(CommandRequest, request), apply))
        return CommandReport(
            command="loop.run", applied=apply, outcome="segment_complete"
        )

    assert main(["loop", "run", "--apply"], dispatch=dispatch) == 0
    assert seen[0][1] is True
    assert seen[0][0].max_activation_seconds == 10_800


def test_loop_run_exposes_bounded_activation_controls() -> None:
    defaults = build_parser().parse_args(["loop", "run"])
    seen: list[CommandRequest] = []

    def dispatch(request: object, *, apply: bool) -> CommandReport:
        assert apply
        seen.append(cast(CommandRequest, request))
        return CommandReport(
            command="loop.run",
            applied=True,
            outcome="activation_complete",
            segments_run=3,
            stop_reason="segment_limit",
        )

    output: list[str] = []
    result = main(
        [
            "loop",
            "run",
            "--apply",
            "--json",
            "--max-segments",
            "3",
            "--max-activation-seconds",
            "120",
        ],
        dispatch=dispatch,
        write_stdout=output.append,
    )

    assert result == 0
    assert defaults.max_segments == 16
    assert defaults.max_activation_seconds == 10_800
    assert seen[0].max_segments == 3
    assert seen[0].max_activation_seconds == 120
    assert json.loads(output[0]) == {
        "schema_version": 1,
        "command": "loop.run",
        "applied": True,
        "outcome": "activation_complete",
        "items": [],
        "actions": [],
        "segments_run": 3,
        "stop_reason": "segment_limit",
    }


def test_loop_run_help_documents_preview_apply_and_default_bounds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", "--help"])

    captured = capsys.readouterr()
    help_text = " ".join(captured.out.split())
    assert raised.value.code == 0
    assert "Preview the next safe action without mutation" in help_text
    assert "omit for a read-only preview" in help_text
    assert (
        "maximum persisted workflow attempts in one applied activation (default: 16)"
        in help_text
    )
    assert "maximum seconds for one applied activation (default: 10800)" in help_text
    assert "--timeout-seconds" not in help_text
    assert captured.err == ""


@pytest.mark.parametrize(
    ("flag", "sentinel"),
    [
        ("--max-segments", 17),
        ("--max-activation-seconds", 3601),
    ],
)
def test_loop_run_help_interpolates_the_current_activation_limit_default(
    flag: str, sentinel: int
) -> None:
    parser = build_parser()
    resources = cast(
        Any,
        next(action for action in parser._actions if getattr(action, "choices", None)),
    )
    loop = resources.choices["loop"]
    loop_actions = next(
        action for action in loop._actions if getattr(action, "choices", None)
    )
    run = loop_actions.choices["run"]
    activation_limit = next(
        action for action in run._actions if flag in action.option_strings
    )
    activation_limit.default = sentinel

    help_text = " ".join(run.format_help().split())

    assert f"(default: {sentinel})" in help_text


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--max-segments", "0"),
        ("--max-segments", "-1"),
        ("--max-segments", "not-an-integer"),
        ("--max-activation-seconds", "0"),
        ("--max-activation-seconds", "-1"),
        ("--max-activation-seconds", "not-an-integer"),
    ],
)
def test_loop_run_rejects_invalid_activation_bounds(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", flag, value])

    assert raised.value.code == 2


def test_loop_run_rejects_the_removed_per_session_timeout_option() -> None:
    with pytest.raises(SystemExit) as raised:
        build_parser().parse_args(["loop", "run", "--timeout-seconds", "1800"])

    assert raised.value.code == 2


def test_command_request_defaults_to_one_three_hour_activation_budget() -> None:
    request = CommandRequest(command="loop.run")

    assert request.max_activation_seconds == 10_800
    assert not hasattr(request, "timeout_seconds")


def test_human_output_reports_the_final_activation_checkpoint() -> None:
    def item(state: RunnerState, phase: WorkflowPhase) -> CommandItemReport:
        return CommandItemReport(
            issue_iid=12,
            state=state,
            phase=phase,
            work_kind=WorkKind.ORDINARY,
            attempt=1,
            retries_remaining=3,
            source_branch="agent/issue-12",
            worktree=Path("/tmp/issue-12"),
            next_action=f"Continue from {phase}.",
        )

    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        assert apply
        return CommandReport(
            command="loop.run",
            applied=True,
            outcome="activation_complete",
            items=(
                item(RunnerState.EXECUTING, WorkflowPhase.CODING_RED_DOCS),
                item(RunnerState.AWAITING_HUMAN, WorkflowPhase.POST_PUBLICATION),
            ),
            segments_run=2,
            stop_reason="awaiting_human",
        )

    output: list[str] = []
    assert (
        main(
            ["loop", "run", "--apply"],
            dispatch=dispatch,
            write_stdout=output.append,
        )
        == 0
    )
    assert output == [
        (
            "activation_complete stop_reason=awaiting_human issue=#12 "
            "state=awaiting-human phase=post-publication "
            "next_action=Continue from post-publication."
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("ledger unavailable"),
        ValueError("migration plan invalid"),
        OSError("migration plan unreadable"),
    ],
)
def test_unrecordable_boundary_failure_returns_one(failure: Exception) -> None:
    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        del apply
        raise failure

    errors: list[str] = []
    assert main(["loop", "status"], dispatch=dispatch, write_stderr=errors.append) == 1
    assert errors == [str(failure)]


def test_invalid_cli_input_returns_two() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["loop", "retry"])
    assert raised.value.code == 2


def test_recovery_resolve_apply_requires_the_exact_reviewed_plan_file() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(
            [
                "recovery",
                "resolve",
                "--issue-iid",
                "12",
                "--choice",
                "retry",
                "--reason",
                "trusted prerequisite is closed",
                "--apply",
            ]
        )

    assert raised.value.code == 2


def test_loop_retry_apply_requires_the_exact_reviewed_plan_file() -> None:
    """The retry command must not dispatch an unreviewed activation."""
    errors: list[str] = []

    result = main(
        ["loop", "retry", "--issue-iid", "12", "--apply"],
        dispatch=lambda _request, *, apply: pytest.fail(
            f"unexpected dispatch with apply={apply}"
        ),
        write_stderr=errors.append,
    )

    assert result == 2
    assert errors == ["--apply requires --plan-file"]


def test_lock_contention_returns_temporary_exit_75() -> None:
    def dispatch(_request: object, *, apply: bool) -> CommandReport:
        del apply
        raise LockBusy("another segment owns the project lock")

    errors: list[str] = []
    assert (
        main(["loop", "run", "--apply"], dispatch=dispatch, write_stderr=errors.append)
        == 75
    )
    assert errors == ["another segment owns the project lock"]


def test_upstream_finding_preview_is_local_json_and_bypasses_runtime_dispatch() -> None:
    output: list[str] = []
    seen_paths: list[Path] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for a local preview")

    def preview_manifest(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        seen_paths.append(path)
        assert callable(read_bytes)
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--json",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=preview_manifest,
            preview_upstream=lambda preview, **_kwargs: preview,
            read_bytes=lambda _path: b"unused",
            write_stdout=output.append,
        )
        == 0
    )
    assert seen_paths == [Path("finding.yaml")]
    rendered = json.loads(output[0])
    assert set(rendered) == {
        "schema_version",
        "command",
        "outcome",
        "project_fingerprint",
        "revision_fingerprint",
        "semantic_finding_fingerprint",
        "copied_path_count",
        "evidence_link_count",
    }
    assert rendered["semantic_finding_fingerprint"] == "c" * 64
    assert "src/widget.py" not in output[0] and "high" not in output[0]


def test_upstream_finding_preview_help_and_harmless_apply_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        build_parser().parse_args(["upstream-finding", "preview", "--help"])
    help_text = capsys.readouterr().out
    assert help_exit.value.code == 0
    assert (
        "read-only" in help_text.lower()
        and "GitLab" in help_text
        and "write" in help_text.lower()
    )
    parsed = build_parser().parse_args(
        [
            "upstream-finding",
            "preview",
            "--manifest-file",
            "finding.yaml",
            "--upstream-host",
            "upstream.example.com",
            "--upstream-project-id",
            "77",
            "--upstream-credential-file",
            "/dedicated/token",
            "--apply",
        ]
    )
    assert parsed.apply is True


def test_upstream_finding_preview_apply_remains_local_and_bypasses_dispatch() -> None:
    """The consistency flag cannot turn the local preview into an operation."""
    output: list[str] = []
    read_paths: list[Path] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for a local preview")

    def read_bytes(path: Path) -> bytes:
        read_paths.append(path)
        return b"manifest bytes"

    def preview_manifest(
        path: Path, *, read_bytes: Callable[[Path], bytes]
    ) -> UpstreamFindingPreview:
        assert read_bytes(path) == b"manifest bytes"
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--apply",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=preview_manifest,
            preview_upstream=lambda preview, **_kwargs: preview,
            read_bytes=read_bytes,
            write_stdout=output.append,
        )
        == 0
    )
    assert read_paths == [Path("finding.yaml")]
    assert output == [
        "outcome=valid project_fingerprint="
        + "a" * 64
        + " revision_fingerprint="
        + "b" * 64
        + " semantic_finding_fingerprint="
        + "c" * 64
        + " paths=1 evidence_links=1"
    ]


def test_upstream_finding_preview_renders_safe_human_summary_and_sanitizes_errors() -> (
    None
):
    output: list[str] = []
    errors: list[str] = []

    def preview_success(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=2,
            evidence_link_count=1,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=preview_success,
            preview_upstream=lambda preview, **_kwargs: preview,
            write_stdout=output.append,
        )
        == 0
    )
    assert "valid" in output[0]
    assert "a" * 64 in output[0]
    assert "paths=2" in output[0] and "evidence_links=1" in output[0]
    assert all(
        value not in output[0] for value in ("src/widget.py", "high", "medium", "both")
    )

    def preview_failure(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        from agent_work.upstream_finding import ManifestError

        raise ManifestError("finding.validation_evidence is invalid")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=preview_failure,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["finding.validation_evidence is invalid"]


def test_upstream_finding_preview_requires_a_manifest_file_and_returns_one_for_read_failure() -> (
    None
):
    with pytest.raises(SystemExit) as missing_file:
        build_parser().parse_args(["upstream-finding", "preview"])
    assert missing_file.value.code == 2

    errors: list[str] = []

    def unreadable(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        from agent_work.upstream_finding import ManifestError

        raise ManifestError("manifest file could not be read")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            preview_manifest=unreadable,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["manifest file could not be read"]


def test_upstream_finding_preview_requires_explicit_upstream_boundary_and_apply_stays_read_only() -> (
    None
):
    """Preview takes no generated-project identity and never reaches dispatch."""
    with pytest.raises(SystemExit) as missing_upstream:
        build_parser().parse_args(
            ["upstream-finding", "preview", "--manifest-file", "finding.yaml"]
        )
    assert missing_upstream.value.code == 2

    output: list[str] = []
    calls: list[tuple[str, int, Path]] = []

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for an upstream preview")

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def upstream_preview(
        preview: UpstreamFindingPreview,
        *,
        host: str,
        project_id: int,
        credential_file: Path,
        read_credential: object,
        run_command: object,
        environment: object,
    ) -> UpstreamFindingPreview:
        assert preview.outcome == "valid"
        assert callable(read_credential) and callable(run_command)
        assert isinstance(environment, dict)
        calls.append((host, project_id, credential_file))
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="match",
            project_fingerprint=preview.project_fingerprint,
            revision_fingerprint=preview.revision_fingerprint,
            semantic_finding_fingerprint=preview.semantic_finding_fingerprint,
            copied_path_count=preview.copied_path_count,
            evidence_link_count=preview.evidence_link_count,
            matching_iid=23,
        )

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
                "--apply",
                "--json",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=local_preview,
            preview_upstream=upstream_preview,
            read_bytes=lambda _path: b"unused",
            read_credential=lambda _path: b"dedicated-token",
            run_command=lambda *_args, **_kwargs: CommandResult(0, "", ""),
            environment={"GITLAB_TOKEN": "generated-token"},
            write_stdout=output.append,
        )
        == 0
    )
    assert calls == [("upstream.example.com", 77, Path("/dedicated/token"))]
    rendered = json.loads(output[0])
    assert rendered["outcome"] == "match"
    assert rendered["matching_iid"] == 23


def test_upstream_finding_preview_composes_the_default_read_only_boundary() -> None:
    """The CLI wires only the dedicated token into its GET-only reader."""
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    credential_paths: list[Path] = []
    output: list[str] = []

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def run(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        calls.append((command, kwargs))
        response = (
            '{"id":77,"path_with_namespace":"group/project","issues_enabled":true,"permissions":'
            '{"project_access":{"access_level":20},"group_access":null}}'
            if command[-1] == "projects/77"
            else "[]"
        )
        return CommandResult(0, response, "")

    def read_credential(path: Path) -> bytes:
        credential_paths.append(path)
        return b"dedicated-token"

    def dispatch_must_not_run(*_args: object, **_kwargs: object) -> CommandReport:
        raise AssertionError("runtime dispatch must not run for an upstream preview")

    assert (
        main(
            [
                "upstream-finding",
                "preview",
                "--manifest-file",
                "finding.yaml",
                "--upstream-host",
                "upstream.example.com",
                "--upstream-project-id",
                "77",
                "--upstream-credential-file",
                "/dedicated/token",
            ],
            dispatch=dispatch_must_not_run,
            preview_manifest=local_preview,
            read_credential=read_credential,
            run_command=run,
            environment={"GITLAB_TOKEN": "generated-token"},
            find_executable=lambda name: "/opt/bin/glab" if name == "glab" else None,
            write_stdout=output.append,
        )
        == 0
    )
    assert credential_paths == [Path("/dedicated/token")]
    assert "outcome=would-create" in output[0]
    assert [command[-1] for command, _kwargs in calls] == [
        "projects/77",
        "projects/77/issues?state=opened&per_page=100&page=1",
    ]
    for command, kwargs in calls:
        assert command[1:6] == (
            "api",
            "--hostname",
            "upstream.example.com",
            "-X",
            "GET",
        )
        assert kwargs["environment"] == {"GITLAB_TOKEN": "dedicated-token"}


def test_upstream_finding_preview_maps_sanitized_reader_failure_to_one_and_hides_absent_iid() -> (
    None
):
    """The read boundary has distinct syntax, operational, and success exits."""
    from agent_work.upstream_gitlab import UpstreamGitLabError

    def local_preview(path: Path, *, read_bytes: object) -> UpstreamFindingPreview:
        del path, read_bytes
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="valid",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    def failure(*_args: object, **_kwargs: object) -> UpstreamFindingPreview:
        raise UpstreamGitLabError("upstream access failed")

    arguments = [
        "upstream-finding",
        "preview",
        "--manifest-file",
        "finding.yaml",
        "--upstream-host",
        "upstream.example.com",
        "--upstream-project-id",
        "77",
        "--upstream-credential-file",
        "/dedicated/token",
    ]
    errors: list[str] = []
    assert (
        main(
            arguments,
            preview_manifest=local_preview,
            preview_upstream=failure,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["upstream access failed"]
    with pytest.raises(UpstreamGitLabError):
        main(
            [*arguments, "--debug"],
            preview_manifest=local_preview,
            preview_upstream=failure,
        )

    def would_create(*_args: object, **_kwargs: object) -> UpstreamFindingPreview:
        return UpstreamFindingPreview(
            schema_version=1,
            command="upstream-finding.preview",
            outcome="would-create",
            project_fingerprint="a" * 64,
            revision_fingerprint="b" * 64,
            semantic_finding_fingerprint="c" * 64,
            copied_path_count=1,
            evidence_link_count=0,
        )

    output: list[str] = []
    assert (
        main(
            arguments,
            preview_manifest=local_preview,
            preview_upstream=would_create,
            write_stdout=output.append,
        )
        == 0
    )
    assert "outcome=would-create" in output[0]
    assert "matching_iid" not in output[0]
