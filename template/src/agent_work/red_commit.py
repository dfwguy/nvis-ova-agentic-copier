# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Proof for a real, isolated Git commit containing accepted Red tests."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .red_checkpoint import (
    RedCheckpointError,
    RedCheckpointRequest,
    validate_red_checkpoint_request,
)

_OID = re.compile(r"[0-9a-f]{40}\Z")


class RedCommitError(RuntimeError):
    """The candidate or retained Red commit could not be proven."""


@dataclass(frozen=True)
class RedCommitDependencies:
    """Injected Git observation and mutation boundary for one transaction."""

    current_head: Callable[[Path], str]
    current_index_tree: Callable[[Path], str]
    changed_paths: Callable[[str, str], tuple[str, ...]]
    blob_oid: Callable[[str, str], str]
    read_ref: Callable[[str], str | None]
    isolated_commit: Callable[[str, str, tuple[str, ...]], str]
    commit_identity: Callable[[str], tuple[str, str]]
    cas_ref: Callable[[str, str, str | None], bool]


@dataclass(frozen=True)
class RedCommitProof:
    """Exact local commit identity for one accepted Red candidate."""

    commit_sha: str
    base_sha: str
    tree_oid: str
    ref: str
    test_paths: tuple[str, ...]


def red_commit_ref(issue_iid: int, base_sha: str) -> str:
    """Name the one local Red commit ref for an issue and base."""
    return f"refs/agent-work/red-commits/{issue_iid}/{base_sha}"


def create_red_commit(
    *,
    issue_iid: int,
    worktree: Path,
    base_sha: str,
    candidate_tree: str,
    request: RedCheckpointRequest,
    dependencies: RedCommitDependencies,
    previous_head: str | None = None,
) -> RedCommitProof:
    """Bind only an unchanged accepted test tree to a verified real commit."""
    try:
        request = validate_red_checkpoint_request(request)
    except RedCheckpointError as error:
        raise RedCommitError("Red commit request is invalid") from error
    if (
        issue_iid <= 0
        or not _OID.fullmatch(base_sha)
        or not _OID.fullmatch(candidate_tree)
    ):
        raise RedCommitError("Red commit identity is invalid")

    head = dependencies.current_head(worktree)
    index = dependencies.current_index_tree(worktree)
    if head != (previous_head or base_sha):
        raise RedCommitError("worker head is not the accepted base")
    if dependencies.changed_paths(base_sha, candidate_tree) != request.test_paths:
        raise RedCommitError("candidate contains paths outside accepted tests")
    blobs = tuple(
        dependencies.blob_oid(candidate_tree, path) for path in request.test_paths
    )
    if not all(_OID.fullmatch(blob) for blob in blobs):
        raise RedCommitError("accepted test blob is invalid")

    ref = red_commit_ref(issue_iid, base_sha)

    def verify(commit: str) -> RedCommitProof:
        if not _OID.fullmatch(commit):
            raise RedCommitError("Red commit oid is invalid")
        if dependencies.commit_identity(commit) != (base_sha, candidate_tree):
            raise RedCommitError("Red commit parent or tree differs from candidate")
        if any(
            dependencies.blob_oid(commit, path) != blob
            for path, blob in zip(request.test_paths, blobs, strict=True)
        ):
            raise RedCommitError("accepted test blob changed in Red commit")
        return RedCommitProof(commit, base_sha, candidate_tree, ref, request.test_paths)

    retained = dependencies.read_ref(ref)
    if retained is not None:
        proof = verify(retained)
        if dependencies.read_ref(ref) != proof.commit_sha:
            raise RedCommitError("retained Red commit ref changed during recovery")
        return proof

    commit = dependencies.isolated_commit(base_sha, candidate_tree, request.test_paths)
    if (
        dependencies.current_head(worktree) != head
        or dependencies.current_index_tree(worktree) != index
    ):
        raise RedCommitError("worker head or index changed during Red commit")
    proof = verify(commit)
    if (
        not dependencies.cas_ref(ref, commit, None)
        and dependencies.read_ref(ref) != commit
    ):
        raise RedCommitError("Red commit ref was not bound to this commit")
    if dependencies.read_ref(ref) != commit:
        raise RedCommitError("Red commit ref readback differs from this commit")
    return verify(commit)
