# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for appending a reviewed commit to one published MR."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

from agent_work.cli import _published_commit_dependencies, build_parser, main
from agent_work.repository import CommandResult
from agent_work.review_handoff import (
    ReviewHandoffManifest,
    ReviewHandoffReceiptEvidence,
)

_PROJECT = 310967
_MR = 342
_ISSUE = 304
_BRANCH = "clanker/issue304-published-commit"
_ORIGIN = "https://gitlab.example.com/group/project.git"
_OLD = "a" * 40
_NEW = "b" * 40
_TREE = "c" * 40
_RED = "d" * 40
_RED_REF = "refs/agent-work/red/310"
_SESSION = "session/review310"
_MANIFEST_SHA = "e" * 64


@dataclass
class _World:
    project: dict[str, object] = field(
        default_factory=lambda: {
            "id": _PROJECT,
            "http_url_to_repo": _ORIGIN,
            "default_branch": "main",
        }
    )
    mrs: list[dict[str, object]] = field(
        default_factory=lambda: [
            {
                "iid": _MR,
                "project_id": _PROJECT,
                "source_project_id": _PROJECT,
                "target_project_id": _PROJECT,
                "state": "opened",
                "source_branch": _BRANCH,
                "target_branch": "main",
                "sha": _OLD,
            }
        ]
    )
    remote_sha: str = _OLD
    local: dict[str, object] = field(
        default_factory=lambda: {
            "branch": _BRANCH,
            "head_ref": f"refs/heads/{_BRANCH}",
            "head_sha": _OLD,
            "staged_tree": _TREE,
            "red_sha": _RED,
            "origin_url": _ORIGIN,
            "merge_in_progress": False,
            "unstaged_changes": False,
            "untracked_files": False,
        }
    )
    manifest: ReviewHandoffManifest = field(
        default_factory=lambda: ReviewHandoffManifest(
            session_ref=_SESSION,
            project_id=_PROJECT,
            issue_iid=_ISSUE,
            branch=_BRANCH,
            candidate_tree=_TREE,
            junit_digests=("py311:" + "1" * 64, "py314:" + "2" * 64),
            receipt_refs=("receipt/session310/tdd-green/1/" + "3" * 64,),
            receipts=(
                ReviewHandoffReceiptEvidence(
                    role="$tdd-green",
                    review_version=1,
                    kind="correction-ledger",
                    receipt_ref="receipt/session310/tdd-green/1/" + "3" * 64,
                    sha256="3" * 64,
                ),
            ),
            sha256=_MANIFEST_SHA,
        )
    )
    commands: list[tuple[object, ...]] = field(default_factory=list)


def _module() -> Any:
    return importlib.import_module("agent_work.published_commit")


def _request(module: Any, **changes: object) -> Any:
    values: dict[str, object] = {
        "project_id": _PROJECT,
        "mr_iid": _MR,
        "worktree": Path("/candidate"),
        "expected_source_sha": _OLD,
        "judge_tree": _TREE,
        "red_ref": _RED_REF,
        "red_sha": _RED,
        "review_session_id": _SESSION,
        "review_manifest_sha256": _MANIFEST_SHA,
        "commit_message": "Finish reviewed change",
    }
    values.update(changes)
    return module.PublishedCommitRequest(**values)


def _dependencies(module: Any, world: _World) -> Any:
    def create_commit(tree: str, parent: str, message: str) -> str:
        world.commands.append(("commit-tree", tree, parent, message))
        return _NEW

    def cas(ref: str, new: str, old: str) -> bool:
        world.commands.append(("update-ref", ref, new, old))
        if world.local["head_sha"] != old:
            return False
        world.local["head_sha"] = new
        return True

    def inspect(sha: str) -> dict[str, object]:
        world.commands.append(("inspect-commit", sha))
        return {
            "sha": sha,
            "parents": (_OLD,),
            "tree": _TREE,
            "message": "Finish reviewed change",
        }

    return module.PublishedCommitDependencies(
        read_project=lambda project_id: world.project,
        read_open_mrs=lambda project_id, branch: world.mrs,
        read_remote_source=lambda branch: world.remote_sha,
        observe_local=lambda worktree, red_ref: dict(world.local),
        read_review_manifest=lambda worktree, session_id: world.manifest,
        create_commit=create_commit,
        compare_and_swap_ref=cas,
        inspect_commit=inspect,
    )


def test_apply_appends_exact_judge_tree_as_one_child_of_published_source() -> None:
    module = _module()
    world = _World()

    report = module.append_published_commit(
        _request(module), apply=True, dependencies=_dependencies(module, world)
    )

    assert report.applied is True
    assert report.old_sha == _OLD
    assert report.new_sha == _NEW
    assert report.tree == _TREE
    assert report.project_id == _PROJECT
    assert report.mr_iid == _MR
    assert ("commit-tree", _TREE, _OLD, "Finish reviewed change") in world.commands
    assert ("update-ref", f"refs/heads/{_BRANCH}", _NEW, _OLD) in world.commands
    assert sum(call[0] == "commit-tree" for call in world.commands) == 1
    assert sum(call[0] == "update-ref" for call in world.commands) == 1
    assert ("inspect-commit", _NEW) in world.commands
    assert world.local["head_sha"] == _NEW


def test_issue_and_merge_request_have_distinct_ids() -> None:
    module = _module()
    world = _World()

    report = module.append_published_commit(
        _request(module), apply=False, dependencies=_dependencies(module, world)
    )

    assert world.manifest.issue_iid == _ISSUE
    assert world.manifest.issue_iid != report.mr_iid


def _cli_arguments(worktree: Path) -> list[str]:
    return [
        "published-commit",
        "append",
        "--project-id",
        str(_PROJECT),
        "--mr-iid",
        str(_MR),
        "--worktree",
        str(worktree),
        "--expected-source-sha",
        _OLD,
        "--judge-tree",
        _TREE,
        "--red-ref",
        _RED_REF,
        "--red-sha",
        _RED,
        "--review-session-id",
        _SESSION,
        "--review-manifest-sha256",
        _MANIFEST_SHA,
        "--commit-message",
        "Finish reviewed change",
        "--json",
    ]


def test_append_command_is_dry_run_by_default() -> None:
    arguments = build_parser().parse_args(_cli_arguments(Path("/candidate")))

    assert arguments.resource == "published-commit"
    assert arguments.action == "append"
    assert arguments.apply is False
    assert arguments.json is True


@pytest.mark.parametrize("apply", [False, True])
def test_cli_dispatch_respects_apply_at_mutating_boundary(
    tmp_path: Path, *, apply: bool
) -> None:
    module = _module()
    world = _World()
    output: list[str] = []
    errors: list[str] = []
    arguments = _cli_arguments(tmp_path)
    if apply:
        arguments.append("--apply")

    result = main(
        arguments,
        build_published_commit_dependencies=lambda **_kwargs: _dependencies(
            module, world
        ),
        write_stdout=output.append,
        write_stderr=errors.append,
    )

    assert result == 0
    assert not errors
    assert json.loads(output[0])["applied"] is apply
    assert sum(call[0] == "commit-tree" for call in world.commands) == int(apply)
    assert sum(call[0] == "update-ref" for call in world.commands) == int(apply)


def test_cli_debug_reraises_failure_and_default_keeps_bounded_error(
    tmp_path: Path,
) -> None:
    module = _module()
    output: list[str] = []
    errors: list[str] = []

    def fail(**_kwargs: object) -> Any:
        raise module.PublishedCommitError("candidate boundary failed")

    assert (
        main(
            _cli_arguments(tmp_path),
            build_published_commit_dependencies=fail,
            write_stdout=output.append,
            write_stderr=errors.append,
        )
        == 1
    )
    assert not output
    assert errors == ["candidate boundary failed"]
    with pytest.raises(module.PublishedCommitError, match="candidate boundary failed"):
        main(
            [*_cli_arguments(tmp_path), "--debug"],
            build_published_commit_dependencies=fail,
            write_stdout=output.append,
            write_stderr=errors.append,
        )


@pytest.mark.parametrize("inventory", ["one", "duplicate", "truncated"])
def test_live_boundary_searches_and_exactly_matches_slash_branch(
    tmp_path: Path, inventory: str
) -> None:
    items = [{"name": _BRANCH, "commit": {"id": _OLD}}]
    if inventory == "duplicate":
        items.append(dict(items[0]))
    elif inventory == "truncated":
        items.extend(
            {"name": f"other/issue304-{index}", "commit": {"id": _NEW}}
            for index in range(99)
        )
    called: list[tuple[str, ...]] = []

    def run_command(command: tuple[str, ...], **_options: object) -> CommandResult:
        called.append(command)
        if command[:2] == ("glab", "api"):
            return CommandResult(0, json.dumps(items), "")
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, _ORIGIN, "")
        if command[-2:] == ("rev-parse", "--absolute-git-dir"):
            return CommandResult(0, str(tmp_path / ".git"), "")
        raise AssertionError(command)

    dependencies = _published_commit_dependencies(
        worktree=tmp_path,
        environment={
            "GITLAB_HOST": "gitlab.example.com",
            "GITLAB_PROJECT_ID": str(_PROJECT),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        },
        run_command=run_command,
    )

    if inventory == "one":
        assert dependencies.read_remote_source(_BRANCH) == _OLD
    else:
        with pytest.raises(_module().PublishedCommitError):
            dependencies.read_remote_source(_BRANCH)
    assert any(
        command[-1]
        == (
            f"projects/{_PROJECT}/repository/branches"
            "?search=clanker%2Fissue304-published-commit&per_page=100"
        )
        for command in called
    )


def test_preview_verifies_authority_without_creating_or_moving_commit() -> None:
    module = _module()
    world = _World()

    report = module.append_published_commit(
        _request(module), apply=False, dependencies=_dependencies(module, world)
    )

    assert report.applied is False
    assert world.local["head_sha"] == _OLD
    assert not any(call[0] in {"commit-tree", "update-ref"} for call in world.commands)


@pytest.mark.parametrize(
    "change",
    (
        "wrong-project",
        "wrong-origin",
        "missing-mr",
        "duplicate-mr",
        "foreign-mr",
        "wrong-target",
        "wrong-mr-sha",
        "remote-advanced",
        "wrong-local-head",
        "wrong-local-branch",
        "wrong-red-ref",
        "wrong-staged-tree",
        "unstaged-changes",
        "untracked-files",
        "merge-in-progress",
        "missing-merge-flag",
        "missing-unstaged-flag",
        "missing-untracked-flag",
        "stale-manifest",
        "wrong-manifest-tree",
        "missing-junit",
        "missing-green-receipt",
    ),
)
def test_rejected_precondition_preserves_local_checkout(change: str) -> None:
    module = _module()
    world = _World()
    if change == "wrong-project":
        world.project["id"] = _PROJECT + 1
    elif change == "wrong-origin":
        world.local["origin_url"] = "https://gitlab.example.com/other/repo.git"
    elif change == "missing-mr":
        world.mrs.clear()
    elif change == "duplicate-mr":
        world.mrs.append(dict(world.mrs[0]))
    elif change == "foreign-mr":
        world.mrs[0]["source_project_id"] = _PROJECT + 1
    elif change == "wrong-target":
        world.mrs[0]["target_branch"] = "release"
    elif change == "wrong-mr-sha":
        world.mrs[0]["sha"] = _NEW
    elif change == "remote-advanced":
        world.remote_sha = _NEW
    elif change == "wrong-local-head":
        world.local["head_sha"] = _NEW
    elif change == "wrong-local-branch":
        world.local["branch"] = "clanker/other"
    elif change == "wrong-red-ref":
        world.local["red_sha"] = _NEW
    elif change == "wrong-staged-tree":
        world.local["staged_tree"] = _NEW
    elif change == "unstaged-changes":
        world.local["unstaged_changes"] = True
    elif change == "untracked-files":
        world.local["untracked_files"] = True
    elif change == "merge-in-progress":
        world.local["merge_in_progress"] = True
    elif change == "missing-merge-flag":
        world.local.pop("merge_in_progress")
    elif change == "missing-unstaged-flag":
        world.local.pop("unstaged_changes")
    elif change == "missing-untracked-flag":
        world.local.pop("untracked_files")
    elif change == "stale-manifest":
        world.manifest = _manifest(sha256="f" * 64)
    elif change == "wrong-manifest-tree":
        world.manifest = _manifest(candidate_tree=_NEW)
    elif change == "missing-junit":
        world.manifest = _manifest(junit_digests=())
    elif change == "missing-green-receipt":
        world.manifest = _manifest(receipt_refs=())
    before = dict(world.local)

    with pytest.raises(module.PublishedCommitError):
        module.append_published_commit(
            _request(module), apply=True, dependencies=_dependencies(module, world)
        )

    assert world.local == before
    assert not any(call[0] in {"commit-tree", "update-ref"} for call in world.commands)


def _manifest(**changes: object) -> ReviewHandoffManifest:
    world = _World()
    fields = vars(world.manifest) | changes
    return ReviewHandoffManifest(**cast("dict[str, Any]", fields))


def test_compare_and_swap_conflict_does_not_move_published_branch() -> None:
    module = _module()
    world = _World()
    dependencies = _dependencies(module, world)
    original_cas = dependencies.compare_and_swap_ref

    def changed_during_commit(ref: str, new: str, old: str) -> bool:
        world.local["head_sha"] = "f" * 40
        return original_cas(ref, new, old)

    dependencies = module.PublishedCommitDependencies(
        **(vars(dependencies) | {"compare_and_swap_ref": changed_during_commit})
    )
    with pytest.raises(module.PublishedCommitError):
        module.append_published_commit(
            _request(module), apply=True, dependencies=dependencies
        )
    assert world.local["head_sha"] == "f" * 40
    assert ("update-ref", f"refs/heads/{_BRANCH}", _NEW, _OLD) in world.commands


@pytest.mark.parametrize("bad_field", ["parents", "tree", "message"])
def test_new_commit_must_match_reviewed_parent_tree_and_message(bad_field: str) -> None:
    module = _module()
    world = _World()
    dependencies = _dependencies(module, world)

    def inspect(sha: str) -> dict[str, object]:
        values: dict[str, object] = {
            "sha": sha,
            "parents": (_OLD,),
            "tree": _TREE,
            "message": "Finish reviewed change",
        }
        values[bad_field] = {"parents": (_NEW,), "tree": _NEW, "message": "other"}[
            bad_field
        ]
        return values

    dependencies = module.PublishedCommitDependencies(
        **(vars(dependencies) | {"inspect_commit": inspect})
    )
    with pytest.raises(module.PublishedCommitError):
        module.append_published_commit(
            _request(module), apply=True, dependencies=dependencies
        )
    assert world.local["head_sha"] == _OLD
