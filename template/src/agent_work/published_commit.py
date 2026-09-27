# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Append one reviewed tree to the exact published merge request source."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .review_handoff import ReviewHandoffManifest

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class PublishedCommitError(RuntimeError):
    """A published source or reviewed candidate failed a required check."""


class PublishedCommitInput(BaseModel):
    """Validated command-line fields at the external input boundary."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    project_id: int = Field(strict=True, gt=0)
    mr_iid: int = Field(strict=True, gt=0)
    worktree: Path
    expected_source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    judge_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    red_ref: str = Field(pattern=r"^refs/agent-work/red/[A-Za-z0-9/_-]+$")
    red_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    review_session_id: str = Field(pattern=r"^session/[A-Za-z0-9_-]+$")
    review_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_message: str = Field(min_length=1)


@dataclass(frozen=True)
class PublishedCommitRequest:
    """Explicit identities captured by the interactive Finalizer."""

    project_id: int
    mr_iid: int
    worktree: Path
    expected_source_sha: str
    judge_tree: str
    red_ref: str
    red_sha: str
    review_session_id: str
    review_manifest_sha256: str
    commit_message: str


@dataclass(frozen=True)
class PublishedCommitDependencies:
    """Injected GitLab, Git, and private review-handoff boundaries."""

    read_project: Callable[[int], dict[str, object]]
    read_open_mrs: Callable[[int, str], list[dict[str, object]]]
    read_remote_source: Callable[[str], str]
    observe_local: Callable[[Path, str], dict[str, object]]
    read_review_manifest: Callable[[Path, str], ReviewHandoffManifest]
    create_commit: Callable[[str, str, str], str]
    compare_and_swap_ref: Callable[[str, str, str], bool]
    inspect_commit: Callable[[str], dict[str, object]]


@dataclass(frozen=True)
class PublishedCommitReport:
    """Identity of the proposed or appended reviewed commit."""

    project_id: int
    mr_iid: int
    old_sha: str
    new_sha: str | None
    tree: str
    applied: bool


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise PublishedCommitError(reason)


def _valid_sha(value: str) -> bool:
    return _SHA.fullmatch(value) is not None


def _check_local(
    local: dict[str, object], request: PublishedCommitRequest, *, head: str
) -> None:
    branch = local.get("branch")
    _require(isinstance(branch, str) and bool(branch), "local branch is missing")
    _require(
        local.get("head_ref") == f"refs/heads/{branch}",
        "local HEAD is not the selected branch",
    )
    _require(local.get("head_sha") == head, "local branch changed")
    _require(local.get("red_sha") == request.red_sha, "Red checkpoint changed")
    _require(
        local.get("staged_tree") == request.judge_tree,
        "index differs from the Judge tree",
    )
    _require(local.get("merge_in_progress") is False, "merge state is not clean")
    _require(local.get("unstaged_changes") is False, "worktree is not clean")
    _require(local.get("untracked_files") is False, "untracked state is not clean")


def append_published_commit(
    request: PublishedCommitRequest,
    *,
    apply: bool,
    dependencies: PublishedCommitDependencies,
) -> PublishedCommitReport:
    """Verify exact published and reviewed identities, then append by local CAS."""
    _require(request.project_id > 0 and request.mr_iid > 0, "numeric IDs required")
    _require(request.worktree.is_absolute(), "worktree must be absolute")
    _require(
        all(
            _valid_sha(value)
            for value in (
                request.expected_source_sha,
                request.judge_tree,
                request.red_sha,
            )
        ),
        "commit and tree identities are malformed",
    )
    _require(
        _DIGEST.fullmatch(request.review_manifest_sha256) is not None,
        "review manifest digest is malformed",
    )
    _require(
        request.red_ref.startswith("refs/agent-work/red/")
        and ".." not in request.red_ref,
        "Red checkpoint ref is invalid",
    )
    _require(
        bool(request.commit_message.strip())
        and request.commit_message == request.commit_message.strip(),
        "commit message is empty or has outer whitespace",
    )

    project = dependencies.read_project(request.project_id)
    _require(project.get("id") == request.project_id, "project identity changed")
    local = dependencies.observe_local(request.worktree, request.red_ref)
    branch = local.get("branch")
    if not isinstance(branch, str) or not branch:
        raise PublishedCommitError("local branch is missing")
    _check_local(local, request, head=request.expected_source_sha)
    _require(
        local.get("origin_url") == project.get("http_url_to_repo"),
        "checkout origin differs from the numeric project",
    )
    target = project.get("default_branch")
    _require(isinstance(target, str) and bool(target), "default branch is missing")
    matches = dependencies.read_open_mrs(request.project_id, branch)
    _require(len(matches) == 1, "expected exactly one open merge request")
    mr = matches[0]
    _require(
        mr.get("iid") == request.mr_iid
        and mr.get("project_id") == request.project_id
        and mr.get("source_project_id") == request.project_id
        and mr.get("target_project_id") == request.project_id
        and mr.get("state") == "opened"
        and mr.get("source_branch") == branch
        and mr.get("target_branch") == target
        and mr.get("sha") == request.expected_source_sha,
        "merge request source identity changed",
    )
    _require(
        dependencies.read_remote_source(branch) == request.expected_source_sha,
        "remote source branch advanced",
    )
    manifest = dependencies.read_review_manifest(
        request.worktree, request.review_session_id
    )
    _require(
        manifest.session_ref == request.review_session_id
        and manifest.sha256 == request.review_manifest_sha256
        and manifest.project_id == request.project_id
        and type(manifest.issue_iid) is int
        and manifest.issue_iid > 0
        and manifest.branch == branch
        and manifest.candidate_tree == request.judge_tree
        and bool(manifest.junit_digests),
        "review manifest or successful Judge evidence changed",
    )
    _require(
        bool(manifest.receipts)
        and tuple(item.receipt_ref for item in manifest.receipts)
        == manifest.receipt_refs
        and all(
            item.role == "$tdd-green"
            and item.kind == "correction-ledger"
            and item.review_version > 0
            and _DIGEST.fullmatch(item.sha256) is not None
            for item in manifest.receipts
        ),
        "accepted Green review ledger is missing or malformed",
    )
    if not apply:
        return PublishedCommitReport(
            request.project_id,
            request.mr_iid,
            request.expected_source_sha,
            None,
            request.judge_tree,
            False,
        )

    new_sha = dependencies.create_commit(
        request.judge_tree, request.expected_source_sha, request.commit_message
    )
    _require(_valid_sha(new_sha), "new commit identity is malformed")
    commit = dependencies.inspect_commit(new_sha)
    _require(
        commit.get("sha") == new_sha
        and commit.get("parents") == (request.expected_source_sha,)
        and commit.get("tree") == request.judge_tree
        and commit.get("message") == request.commit_message,
        "new commit differs from reviewed tree or captured parent",
    )
    _require(
        dependencies.compare_and_swap_ref(
            f"refs/heads/{branch}", new_sha, request.expected_source_sha
        ),
        "local branch changed before commit could be appended",
    )
    after = dependencies.observe_local(request.worktree, request.red_ref)
    _check_local(after, request, head=new_sha)
    _require(after.get("branch") == branch, "local branch changed after append")
    _require(
        dependencies.inspect_commit(new_sha) == commit,
        "new commit changed after append",
    )
    return PublishedCommitReport(
        request.project_id,
        request.mr_iid,
        request.expected_source_sha,
        new_sha,
        request.judge_tree,
        True,
    )
