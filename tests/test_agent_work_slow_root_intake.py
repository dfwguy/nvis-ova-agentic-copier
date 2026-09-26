# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for confirmed slow root issue reconciliation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import build_parser, main
from agent_work.gitlab import GitLabError, GitLabIssuePublisher
from agent_work.repository import CommandResult
from agent_work.slow_root_intake import (
    SlowRootItem,
    SlowRootReport,
    reconcile_slow_roots,
)

PROJECT_ID = 310967


def _root(
    key: str = "fixture-gitlab-inventory",
    *,
    kind: str = "shared-fixture",
    confirmed: bool = True,
    durations: tuple[float, float] = (128.1, 132.7),
    semantic_issue_iid: int | None = None,
    semantic_phrase: str | None = None,
) -> dict[str, object]:
    root: dict[str, object] = {
        "key": key,
        "kind": kind,
        "cases": [
            "tests/test_agent_work_boundaries.py::test_first",
            "tests/test_agent_work_boundaries.py::test_second",
        ],
        "confirmed": confirmed,
        "evidence": [
            {
                "environment": "py311",
                "duration_ms": durations[0],
                "tree_sha": "a" * 40,
                "junit_sha256": "1" * 64,
            },
            {
                "environment": "py311",
                "duration_ms": durations[1],
                "tree_sha": "b" * 40,
                "junit_sha256": "2" * 64,
            },
        ],
        "title": "Reduce repeated GitLab inventory fixture time",
        "problem": "Two local repeats show the shared inventory fixture exceeds the timing budget.",
        "outcome": "Reuse the inventory fixture once per safe scope.",
        "acceptance": [
            "Both selected cases remain covered",
            "The local timing improves",
        ],
    }
    if semantic_issue_iid is not None:
        root["semantic_issue_iid"] = semantic_issue_iid
    if semantic_phrase is not None:
        root["semantic_phrase"] = semantic_phrase
    return root


def _manifest(*roots: dict[str, object]) -> bytes:
    return json.dumps({"schema_version": 1, "roots": roots}).encode()


def _issue(
    iid: int,
    *,
    state: str = "opened",
    description: str = "",
    title: str = "Existing issue",
    labels: tuple[str, ...] = ("existing-owner",),
) -> dict[str, object]:
    return {
        "project_id": PROJECT_ID,
        "iid": iid,
        "state": state,
        "title": title,
        "description": description,
        "labels": list(labels),
    }


def _reconcile(
    manifest: bytes,
    *,
    apply: bool = False,
    issues: tuple[dict[str, object], ...] = (),
    create_issue: Callable[..., dict[str, object]] | None = None,
    fetch_issue: Callable[[int], dict[str, object]] | None = None,
    list_issues: Callable[[], tuple[dict[str, object], ...]] | None = None,
) -> Any:
    return reconcile_slow_roots(
        manifest,
        project_id=PROJECT_ID,
        apply=apply,
        list_issues=list_issues or (lambda: issues),
        create_issue=create_issue or (lambda **_fields: pytest.fail("unexpected POST")),
        fetch_issue=fetch_issue or (lambda _iid: pytest.fail("unexpected issue GET")),
    )


def test_preview_groups_shared_fixture_cases_into_one_read_only_root() -> None:
    calls: list[str] = []

    def list_issues() -> tuple[dict[str, object], ...]:
        calls.append("list")
        return ()

    report = _reconcile(_manifest(_root()), list_issues=list_issues)

    assert report.applied is False
    assert [(item.root_key, item.action, item.issue_iid) for item in report.items] == [
        ("fixture-gitlab-inventory", "would-create", None)
    ]
    assert calls == ["list"]


def test_preview_accepts_confirmed_ordinary_test_root() -> None:
    report = _reconcile(_manifest(_root("ordinary-test-root", kind="ordinary-test")))

    assert [(item.root_key, item.action) for item in report.items] == [
        ("ordinary-test-root", "would-create")
    ]


@pytest.mark.parametrize(
    "selector",
    [
        "tests/test_slow.py::test_case[" + "x" * 512 + "]",
        "tests/test_slow.py::test_case\nInjected heading",
    ],
)
def test_manifest_rejects_unbounded_or_multiline_case_selector(selector: str) -> None:
    root = _root()
    root["cases"] = [selector]

    with pytest.raises(ValueError, match="case|manifest|invalid"):
        _reconcile(_manifest(root))


def test_manifest_keeps_valid_parametrized_case_selector() -> None:
    root = _root()
    root["cases"] = ["tests/test_slow.py::test_case[py311 - edge]"]

    report = _reconcile(_manifest(root))

    assert report.items[0].action == "would-create"


@pytest.mark.parametrize(
    "field", ["title", "problem", "outcome", "acceptance", "cases", "semantic_phrase"]
)
def test_manifest_rejects_reserved_slow_root_marker_in_caller_text(field: str) -> None:
    root = _root(semantic_issue_iid=255, semantic_phrase="expected scope")
    marker = "<!-- agent-slow-test-root:other-root:v1 -->"
    if field == "acceptance":
        root[field] = [marker]
    elif field == "cases":
        root[field] = [f"tests/test_slow.py::test_case[{marker}]"]
    else:
        root[field] = marker

    with pytest.raises(ValueError, match="marker|manifest|invalid"):
        _reconcile(_manifest(root))


@pytest.mark.parametrize("durations", [(101.0, 102.0), (205.5, 240.2)])
def test_changed_repeat_durations_reuse_the_same_exact_root_key(
    durations: tuple[float, float],
) -> None:
    marker = "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
    report = _reconcile(
        _manifest(_root(durations=durations)),
        issues=(_issue(41, description=f"Existing scope.\n{marker}"),),
    )

    assert [(item.action, item.issue_iid) for item in report.items] == [("reuse", 41)]


def test_unrelated_issue_with_null_description_does_not_block_preview() -> None:
    unrelated = _issue(42)
    unrelated["description"] = None

    report = _reconcile(_manifest(_root()), issues=(unrelated,))

    assert report.items[0].action == "would-create"


def test_shared_root_accepts_two_local_repeats_plus_other_environment() -> None:
    root = _root()
    evidence = cast("list[dict[str, object]]", root["evidence"])
    root["evidence"] = [
        *evidence,
        {
            "environment": "py314",
            "duration_ms": 145.0,
            "tree_sha": "c" * 40,
            "junit_sha256": "3" * 64,
        },
    ]

    report = _reconcile(_manifest(root))

    assert [(item.root_key, item.action) for item in report.items] == [
        ("fixture-gitlab-inventory", "would-create")
    ]


def test_closed_exact_root_is_covered_without_reopening_or_creating() -> None:
    marker = "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
    report = _reconcile(
        _manifest(_root()),
        apply=True,
        issues=(_issue(41, state="closed", description=marker),),
    )

    assert [(item.action, item.issue_iid) for item in report.items] == [("covered", 41)]


@pytest.mark.parametrize(
    ("hint_iid", "phrase", "issue_iid", "issue_state", "issue_body", "expected"),
    [
        (
            255,
            "GitLab inventory fixture",
            255,
            "opened",
            "GitLab inventory fixture",
            "reuse",
        ),
        (
            255,
            "GitLab inventory fixture",
            255,
            "closed",
            "GitLab inventory fixture",
            "covered",
        ),
    ],
)
def test_semantic_legacy_hint_reuses_only_verified_issue_scope_and_status(
    hint_iid: int,
    phrase: str,
    issue_iid: int,
    issue_state: str,
    issue_body: str,
    expected: str,
) -> None:
    report = _reconcile(
        _manifest(_root(semantic_issue_iid=hint_iid, semantic_phrase=phrase)),
        issues=(_issue(issue_iid, state=issue_state, description=issue_body),),
    )

    assert [(item.action, item.issue_iid) for item in report.items] == [
        (expected, issue_iid)
    ]


@pytest.mark.parametrize(
    "issues",
    [
        (_issue(255, description="Different root"),),
        (_issue(256, description="GitLab inventory fixture"),),
    ],
)
def test_unverified_legacy_hint_does_not_claim_an_existing_issue(
    issues: tuple[dict[str, object], ...],
) -> None:
    with pytest.raises(ValueError, match="semantic|scope|hint|invalid"):
        _reconcile(
            _manifest(
                _root(
                    semantic_issue_iid=255,
                    semantic_phrase="GitLab inventory fixture",
                )
            ),
            issues=issues,
        )


def test_duplicate_exact_marker_is_rejected_before_any_create() -> None:
    marker = "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
    issues = (_issue(41, description=marker), _issue(42, description=marker))

    with pytest.raises(ValueError, match="ambiguous"):
        _reconcile(_manifest(_root()), apply=True, issues=issues)


def test_two_distinct_roots_cannot_claim_one_existing_issue() -> None:
    markers = (
        "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->\n"
        "<!-- agent-slow-test-root:other-root:v1 -->"
    )
    with pytest.raises(ValueError, match="ambiguous"):
        _reconcile(
            _manifest(_root(), _root("other-root")),
            apply=True,
            issues=(_issue(41, description=markers),),
        )


def test_unconfirmed_observation_is_diagnostic_and_never_posted() -> None:
    report = _reconcile(_manifest(_root(confirmed=False)), apply=True)

    assert [(item.action, item.issue_iid) for item in report.items] == [
        ("unconfirmed", None)
    ]


@pytest.mark.parametrize(
    "invalid_evidence",
    [
        [],
        [{"environment": "py311", "duration_ms": 130.0}],
        [
            {
                "environment": "py311",
                "duration_ms": 130.0,
                "tree_sha": "a" * 40,
                "junit_sha256": "1" * 64,
            },
            {
                "environment": "py314",
                "duration_ms": 132.0,
                "tree_sha": "b" * 40,
                "junit_sha256": "2" * 64,
            },
        ],
        [
            {
                "environment": "py311",
                "duration_ms": 130.0,
                "tree_sha": "not-a-sha",
                "junit_sha256": "1" * 64,
            },
            {
                "environment": "py311",
                "duration_ms": 132.0,
                "tree_sha": "b" * 40,
                "junit_sha256": "2" * 64,
            },
        ],
    ],
)
def test_confirmed_root_requires_two_local_repeats_with_provenance(
    invalid_evidence: list[dict[str, object]],
) -> None:
    root = _root()
    root["evidence"] = invalid_evidence

    with pytest.raises(ValueError, match="evidence|repeat|provenance|manifest"):
        _reconcile(_manifest(root))


def test_apply_rechecks_inventory_then_creates_one_issue_with_exact_marker() -> None:
    events: list[str] = []
    created: dict[str, object] = {}

    def list_issues() -> tuple[dict[str, object], ...]:
        events.append("list")
        return ()

    def create_issue(**fields: object) -> dict[str, object]:
        events.append("create")
        created.update(fields)
        return _issue(
            61,
            title=str(fields["title"]),
            description=str(fields["description"]),
            labels=cast("tuple[str, ...]", fields["labels"]),
        )

    def fetch_issue(iid: int) -> dict[str, object]:
        events.append("fetch")
        assert iid == 61
        return _issue(
            iid,
            title=str(created["title"]),
            description=str(created["description"]),
            labels=cast("tuple[str, ...]", created["labels"]),
        )

    report = _reconcile(
        _manifest(_root()),
        apply=True,
        list_issues=list_issues,
        create_issue=create_issue,
        fetch_issue=fetch_issue,
    )

    assert events == ["list", "list", "create", "fetch"]
    assert (
        str(created["description"]).count(
            "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
        )
        == 1
    )
    assert created["labels"] == ("agent-slow-test",)
    assert [(item.action, item.issue_iid) for item in report.items] == [("created", 61)]


def test_apply_uses_race_winner_from_second_inventory_without_post() -> None:
    marker = "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
    snapshots = iter(((), (_issue(61, description=marker),)))

    report = _reconcile(
        _manifest(_root()),
        apply=True,
        list_issues=lambda: next(snapshots),
    )

    assert [(item.action, item.issue_iid) for item in report.items] == [("reuse", 61)]


def test_apply_rechecks_race_winner_then_creates_only_unmatched_root() -> None:
    later_marker = "<!-- agent-slow-test-root:other-root:v1 -->"
    snapshots = iter(((), (_issue(62, description=later_marker),)))
    created: list[dict[str, object]] = []

    def create_issue(**fields: object) -> dict[str, object]:
        created.append(fields)
        return _issue(
            61,
            title=str(fields["title"]),
            description=str(fields["description"]),
            labels=cast("tuple[str, ...]", fields["labels"]),
        )

    def fetch_issue(iid: int) -> dict[str, object]:
        assert iid == 61
        fields = created[0]
        return _issue(
            iid,
            title=str(fields["title"]),
            description=str(fields["description"]),
            labels=cast("tuple[str, ...]", fields["labels"]),
        )

    report = _reconcile(
        _manifest(_root(), _root("other-root")),
        apply=True,
        list_issues=lambda: next(snapshots),
        create_issue=create_issue,
        fetch_issue=fetch_issue,
    )

    assert [(item.action, item.issue_iid) for item in report.items] == [
        ("created", 61),
        ("reuse", 62),
    ]
    assert len(created) == 1


def test_apply_rejects_two_unmatched_roots_before_post() -> None:
    scans = 0

    def list_issues() -> tuple[dict[str, object], ...]:
        nonlocal scans
        scans += 1
        return ()

    with pytest.raises(ValueError, match="one|unmatched|subset"):
        _reconcile(
            _manifest(_root(), _root("other-root")),
            apply=True,
            list_issues=list_issues,
        )
    assert scans == 2


def test_apply_reuses_prior_creation_when_manifest_is_resubmitted() -> None:
    issues: list[dict[str, object]] = []

    def create_issue(**fields: object) -> dict[str, object]:
        issue = _issue(
            len(issues) + 61,
            title=str(fields["title"]),
            description=str(fields["description"]),
            labels=cast("tuple[str, ...]", fields["labels"]),
        )
        issues.append(issue)
        return issue

    def fetch_issue(iid: int) -> dict[str, object]:
        return next(issue for issue in issues if issue["iid"] == iid)

    first = _reconcile(
        _manifest(_root()),
        apply=True,
        list_issues=lambda: tuple(issues),
        create_issue=create_issue,
        fetch_issue=fetch_issue,
    )
    second = _reconcile(
        _manifest(_root(), _root("other-root")),
        apply=True,
        list_issues=lambda: tuple(issues),
        create_issue=create_issue,
        fetch_issue=fetch_issue,
    )

    assert [(item.action, item.issue_iid) for item in first.items] == [("created", 61)]
    assert [(item.action, item.issue_iid) for item in second.items] == [
        ("reuse", 61),
        ("created", 62),
    ]


def test_apply_rejects_ambiguous_second_inventory_before_post() -> None:
    marker = "<!-- agent-slow-test-root:fixture-gitlab-inventory:v1 -->"
    snapshots = iter(
        ((), (_issue(61, description=marker), _issue(62, description=marker)))
    )

    with pytest.raises(ValueError, match="ambiguous"):
        _reconcile(
            _manifest(_root()),
            apply=True,
            list_issues=lambda: next(snapshots),
        )


@pytest.mark.parametrize("mismatch", ["description", "title", "labels", "state", "iid"])
def test_apply_rejects_created_issue_readback_mismatch(mismatch: str) -> None:
    created: dict[str, object] = {}

    def create_issue(**fields: object) -> dict[str, object]:
        created.update(fields)
        return _issue(
            61,
            title=str(fields["title"]),
            description=str(fields["description"]),
            labels=cast("tuple[str, ...]", fields["labels"]),
        )

    def fetch_issue(iid: int) -> dict[str, object]:
        readback = _issue(
            iid,
            title=str(created["title"]),
            description=str(created["description"]),
            labels=cast("tuple[str, ...]", created["labels"]),
        )
        readback[mismatch] = {
            "description": "altered",
            "title": "altered",
            "labels": ["altered"],
            "state": "closed",
            "iid": 62,
        }[mismatch]
        return readback

    with pytest.raises(ValueError, match="read.back|mismatch|invalid"):
        _reconcile(
            _manifest(_root()),
            apply=True,
            create_issue=create_issue,
            fetch_issue=fetch_issue,
        )


@pytest.mark.parametrize("apply", [False, True])
def test_cli_reconcile_requires_manifest_and_defaults_to_preview(
    *, apply: bool
) -> None:
    arguments = [
        "slow-root",
        "reconcile",
        "--manifest-file",
        "slow-roots.json",
        "--json",
    ]
    if apply:
        arguments.append("--apply")

    parsed = build_parser().parse_args(arguments)

    assert parsed.resource == "slow-root"
    assert parsed.action == "reconcile"
    assert parsed.manifest_file.name == "slow-roots.json"
    assert parsed.json is True
    assert parsed.apply is apply


def test_cli_reconcile_help_exits_successfully(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(["slow-root", "reconcile", "--help"])

    assert result.value.code == 0
    assert "--manifest-file" in capsys.readouterr().out


@pytest.mark.parametrize("apply", [False, True])
def test_cli_json_dispatches_manifest_with_explicit_apply_flag(*, apply: bool) -> None:
    manifest = _manifest(_root())
    calls: list[tuple[bytes, int, bool]] = []
    output: list[str] = []

    def action(
        manifest_bytes: bytes, *, project_id: int, apply: bool
    ) -> SlowRootReport:
        calls.append((manifest_bytes, project_id, apply))
        return SlowRootReport(
            applied=apply,
            items=(
                SlowRootItem(
                    root_key="fixture-gitlab-inventory",
                    action="created" if apply else "would-create",
                    issue_iid=61 if apply else None,
                ),
            ),
        )

    arguments = ["slow-root", "reconcile", "--manifest-file", "roots.json", "--json"]
    if apply:
        arguments.append("--apply")

    result = main(
        arguments,
        slow_root_reconcile_action=action,
        slow_root_manifest_reader=lambda _path: manifest,
        environment={
            "GITLAB_HOST": "gitlab-master.nvidia.com",
            "GITLAB_PROJECT_ID": "310967",
        },
        write_stdout=output.append,
        write_stderr=lambda message: pytest.fail(message),
    )

    assert result == 0
    assert calls == [(manifest, PROJECT_ID, apply)]
    payload = json.loads(output.pop())
    assert payload["applied"] is apply
    assert payload["items"][0]["action"] == ("created" if apply else "would-create")


def test_cli_invalid_manifest_returns_error_without_success_output() -> None:
    output: list[str] = []
    errors: list[str] = []

    def action(
        _manifest_bytes: bytes, *, project_id: int, apply: bool
    ) -> SlowRootReport:
        raise ValueError("slow root manifest is invalid")

    result = main(
        ["slow-root", "reconcile", "--manifest-file", "roots.json", "--json"],
        slow_root_reconcile_action=action,
        slow_root_manifest_reader=lambda _path: b"invalid",
        environment={
            "GITLAB_HOST": "gitlab-master.nvidia.com",
            "GITLAB_PROJECT_ID": "310967",
        },
        write_stdout=output.append,
        write_stderr=errors.append,
    )

    assert result == 1
    assert output == []
    assert errors == ["slow root manifest is invalid"]


def test_cli_rejects_oversized_manifest_before_reconciliation(tmp_path: Path) -> None:
    manifest = tmp_path / "oversized.json"
    manifest.write_bytes(b"x" * 262_145)
    errors: list[str] = []

    def action(
        _manifest_bytes: bytes, *, project_id: int, apply: bool
    ) -> SlowRootReport:
        pytest.fail("oversized manifest reached reconciliation")

    result = main(
        ["slow-root", "reconcile", "--manifest-file", str(manifest), "--json"],
        slow_root_reconcile_action=action,
        environment={"GITLAB_HOST": "gitlab.example", "GITLAB_PROJECT_ID": "310967"},
        write_stdout=lambda message: pytest.fail(message),
        write_stderr=errors.append,
    )

    assert result == 1
    assert len(errors) == 1
    assert "manifest" in errors[0]


def test_cli_limits_bytes_requested_at_manifest_filesystem_boundary(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "oversized.json"
    requested: list[tuple[Path, int]] = []

    def read_bounded(path: Path, *, limit: int) -> bytes:
        requested.append((path, limit))
        return b"x" * (limit + 1)

    result = main(
        ["slow-root", "reconcile", "--manifest-file", str(manifest), "--json"],
        slow_root_reconcile_action=lambda *_args, **_kwargs: pytest.fail(
            "oversized manifest reached reconciliation"
        ),
        read_bounded_regular_file=read_bounded,
        environment={"GITLAB_HOST": "gitlab.example", "GITLAB_PROJECT_ID": "310967"},
        write_stdout=lambda message: pytest.fail(message),
        write_stderr=lambda _message: None,
    )

    assert result == 1
    assert requested == [(manifest.resolve(), 262_144)]


def test_numeric_project_inventory_reads_all_pages_including_closed_issues() -> None:
    calls: list[tuple[str, ...]] = []
    pages: list[object] = [
        [_issue(iid) for iid in range(1, 101)],
        [_issue(101, state="closed")],
    ]

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(command)
        return CommandResult(0, json.dumps(pages.pop(0)), "")

    port = GitLabIssuePublisher(
        host="gitlab.example",
        project_id=PROJECT_ID,
        glab_path="/bin/glab",
        run_command=run,
    )
    inventory = port.list_all_issues(project_id=PROJECT_ID)

    assert len(inventory) == 101
    assert inventory[-1]["state"] == "closed"
    assert (
        "state=all" in calls[0] and "per_page=100" in calls[0] and "page=1" in calls[0]
    )
    assert (
        "state=all" in calls[1] and "per_page=100" in calls[1] and "page=2" in calls[1]
    )


def test_numeric_project_inventory_rejects_duplicate_iid_across_pages() -> None:
    pages: list[object] = [
        [_issue(iid) for iid in range(1, 101)],
        [_issue(100)],
    ]

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        _ = command
        return CommandResult(0, json.dumps(pages.pop(0)), "")

    port = GitLabIssuePublisher(
        host="gitlab.example",
        project_id=PROJECT_ID,
        glab_path="/bin/glab",
        run_command=run,
    )
    with pytest.raises(GitLabError, match="duplicate"):
        port.list_all_issues(project_id=PROJECT_ID)


def test_numeric_project_inventory_rejects_wrong_project() -> None:
    wrong_issue = _issue(1)
    wrong_issue["project_id"] = PROJECT_ID + 1

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        _ = command
        return CommandResult(0, json.dumps([wrong_issue]), "")

    port = GitLabIssuePublisher(
        host="gitlab.example",
        project_id=PROJECT_ID,
        glab_path="/bin/glab",
        run_command=run,
    )
    with pytest.raises(GitLabError, match="wrong-project"):
        port.list_all_issues(project_id=PROJECT_ID)


def test_numeric_project_inventory_rejects_page_cap_exhaustion() -> None:
    page = 0

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        _ = command
        nonlocal page
        page += 1
        return CommandResult(
            0,
            json.dumps(
                [_issue(iid) for iid in range((page - 1) * 100 + 1, page * 100 + 1)]
            ),
            "",
        )

    port = GitLabIssuePublisher(
        host="gitlab.example",
        project_id=PROJECT_ID,
        glab_path="/bin/glab",
        run_command=run,
    )
    with pytest.raises(GitLabError, match="page limit"):
        port.list_all_issues(project_id=PROJECT_ID)
    assert page == 100
