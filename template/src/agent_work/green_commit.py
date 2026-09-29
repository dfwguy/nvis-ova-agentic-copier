# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bind a native-hook Green commit to an accepted Red commit and worker branch."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .red_commit import RedCommitProof, red_commit_ref

_OID = re.compile(r"[0-9a-f]{40}\Z")


class GreenCommitError(RuntimeError):
    """The candidate or native-hook Green commit could not be proven."""


@dataclass(frozen=True)
class IsolatedGreenCommit:
    """Result of one isolated commit with its bounded timing summary."""

    commit_sha: str
    timing_status: str
    junit_timings: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class GreenCommitDependencies:
    """Injected Git observations and mutations for one Green transaction."""

    worker_state: Callable[[Path], tuple[str, str, str, str]]
    read_ref: Callable[[str], str | None]
    commit_identity: Callable[[str], tuple[str, str, str]]
    blob_oid: Callable[[str, str], str]
    isolated_commit: Callable[[str, str, str], IsolatedGreenCommit]
    cas_ref: Callable[[str, str, str | None], bool]


@dataclass(frozen=True)
class GreenCommitProof:
    """Verified local Green commit and optional timing evidence."""

    commit_sha: str
    red_sha: str
    tree_oid: str
    ref: str
    timing_status: str
    junit_timings: tuple[dict[str, object], ...]


def green_commit_ref(issue_iid: int, base_sha: str) -> str:
    """Name the one local Green commit ref for an issue and base."""
    return f"refs/agent-work/green-commits/{issue_iid}/{base_sha}"


def create_green_commit(
    *,
    issue_iid: int,
    worktree: Path,
    branch: str,
    base_sha: str,
    candidate_tree: str,
    red_proof: RedCommitProof,
    message: str,
    dependencies: GreenCommitDependencies,
) -> GreenCommitProof:
    """Commit the exact candidate through one native hook before moving the worker."""
    if (
        issue_iid <= 0
        or _OID.fullmatch(base_sha) is None
        or _OID.fullmatch(candidate_tree) is None
        or _OID.fullmatch(red_proof.commit_sha) is None
        or red_proof.base_sha != base_sha
        or red_proof.ref != red_commit_ref(issue_iid, base_sha)
        or _OID.fullmatch(red_proof.tree_oid) is None
        or branch != f"agent/issue-{issue_iid}"
        or not message.strip()
        or not red_proof.test_paths
    ):
        raise GreenCommitError("Green commit request is invalid")

    def verify_worker(expected_head: str) -> None:
        state = dependencies.worker_state(worktree)
        if state != (branch, expected_head, candidate_tree, candidate_tree):
            raise GreenCommitError("worker branch or candidate changed")

    def verify_commit(commit: str) -> None:
        if _OID.fullmatch(commit) is None or dependencies.commit_identity(commit) != (
            red_proof.commit_sha,
            candidate_tree,
            message,
        ):
            raise GreenCommitError("Green commit identity differs from candidate")
        if any(
            dependencies.blob_oid(commit, path)
            != dependencies.blob_oid(red_proof.commit_sha, path)
            for path in red_proof.test_paths
        ):
            raise GreenCommitError("accepted Red test blob changed")

    if dependencies.read_ref(red_proof.ref) != red_proof.commit_sha:
        raise GreenCommitError("accepted Red commit ref changed")
    if dependencies.commit_identity(red_proof.commit_sha)[:2] != (
        base_sha,
        red_proof.tree_oid,
    ):
        raise GreenCommitError("accepted Red commit identity changed")
    if any(
        _OID.fullmatch(dependencies.blob_oid(red_proof.commit_sha, path)) is None
        for path in red_proof.test_paths
    ):
        raise GreenCommitError("accepted Red test blob is invalid")
    if any(
        dependencies.blob_oid(candidate_tree, path)
        != dependencies.blob_oid(red_proof.commit_sha, path)
        for path in red_proof.test_paths
    ):
        raise GreenCommitError("candidate changes accepted Red tests")

    ref = green_commit_ref(issue_iid, base_sha)
    retained = dependencies.read_ref(ref)
    if retained is not None:
        verify_commit(retained)
        head = dependencies.worker_state(worktree)[1]
        if head not in {base_sha, retained}:
            raise GreenCommitError("worker head differs from retained Green commit")
        verify_worker(head)
        if dependencies.read_ref(red_proof.ref) != red_proof.commit_sha:
            raise GreenCommitError("accepted Red commit ref changed")
        if head == base_sha and not dependencies.cas_ref(
            f"refs/heads/{branch}", retained, base_sha
        ):
            raise GreenCommitError("worker branch changed before Green update")
        timing_status = "unknown"
        timings: tuple[dict[str, object], ...] = ()
        commit = retained
    else:
        verify_worker(base_sha)
        try:
            isolated = dependencies.isolated_commit(
                red_proof.commit_sha, candidate_tree, message
            )
        except RuntimeError as error:
            raise GreenCommitError("installed hook rejected Green commit") from error
        verify_worker(base_sha)
        commit = isolated.commit_sha
        verify_commit(commit)
        if isolated.timing_status not in {"complete", "unknown"}:
            raise GreenCommitError("Green timing status is invalid")
        if isolated.timing_status == "unknown" and isolated.junit_timings:
            raise GreenCommitError("unknown timing cannot carry case durations")
        if not dependencies.cas_ref(ref, commit, None):
            raise GreenCommitError("Green commit ref could not be bound")
        if dependencies.read_ref(ref) != commit:
            raise GreenCommitError("Green commit ref readback differs")
        verify_commit(commit)
        verify_worker(base_sha)
        if dependencies.read_ref(red_proof.ref) != red_proof.commit_sha:
            raise GreenCommitError("accepted Red commit ref changed")
        if not dependencies.cas_ref(f"refs/heads/{branch}", commit, base_sha):
            raise GreenCommitError("worker branch changed before Green update")
        timing_status = isolated.timing_status
        timings = isolated.junit_timings

    if dependencies.read_ref(ref) != commit:
        raise GreenCommitError("Green commit ref changed")
    verify_worker(commit)
    verify_commit(commit)
    return GreenCommitProof(
        commit, red_proof.commit_sha, candidate_tree, ref, timing_status, timings
    )
