# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Controller-owned Git-object candidate read contracts."""

import subprocess
from pathlib import Path
from typing import cast

import pytest

from agent_work.models import AgentWorkConfig
from agent_work.repository import (
    CommandResult,
    CommandTimeoutError,
    GitRepository,
    RepositoryError,
    run_subprocess_bytes,
)

BASE = "a" * 40
TREE = "b" * 40
OLD = "c" * 40
NEW = "d" * 40
REMOVED = "e" * 40
SOURCE = "f" * 40
UNRELATED = "9" * 40


def _repository(
    tmp_path: Path,
    *,
    oversize: bool = False,
    binary: bool = False,
    mode_only: bool = False,
    drift: bool = False,
    large: bool = False,
    crlf: bool = False,
    authored: bool = False,
) -> GitRepository:
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=81,
        checkout=tmp_path,
        state_root=tmp_path / "state",
        git_path=Path("/usr/bin/git"),
        glab_path=Path("/usr/bin/glab"),
        base_branch="agent/base",
    )
    outputs = {
        ("rev-parse", "HEAD"): BASE + "\n",
        ("write-tree",): TREE + "\n",
        ("rev-parse", f"{BASE}^{{tree}}"): BASE + "\n",
        (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            BASE,
            TREE,
        ): "docs/a.md\0docs/gone.md\0",
        ("ls-tree", "-z", BASE, "--", "docs/a.md"): f"100644 blob {OLD}\tdocs/a.md\0",
        ("ls-tree", "-z", TREE, "--", "docs/a.md"): f"100644 blob {NEW}\tdocs/a.md\0",
        (
            "ls-tree",
            "-z",
            BASE,
            "--",
            "docs/gone.md",
        ): f"100644 blob {REMOVED}\tdocs/gone.md\0",
        ("ls-tree", "-z", TREE, "--", "docs/gone.md"): "",
        ("cat-file", "-s", OLD): "7\n",
        ("cat-file", "-s", NEW): "7\n",
        ("cat-file", "-s", REMOVED): "8\n",
        ("cat-file", "blob", OLD): "before\n",
        ("cat-file", "blob", NEW): "after!\n",
        ("cat-file", "blob", REMOVED): "removed\n",
        ("status", "--porcelain=v1", "-z"): " M docs/a.md\0 D docs/gone.md\0",
        ("read-tree", BASE): "",
        ("add", "--", "docs/a.md", "docs/gone.md"): "",
    }
    if oversize:
        outputs[("cat-file", "-s", NEW)] = "2097153\n"
    if large:
        content = "x" * 188_414
        outputs[("cat-file", "-s", OLD)] = "188414\n"
        outputs[("cat-file", "-s", NEW)] = "188414\n"
        outputs[("cat-file", "blob", OLD)] = content
        outputs[("cat-file", "blob", NEW)] = content
    if mode_only:
        outputs[("diff", "--name-only", "-z", "--no-renames", BASE, TREE)] = (
            "docs/a.md\0"
        )
        outputs[("ls-tree", "-z", TREE, "--", "docs/a.md")] = (
            f"100755 blob {OLD}\tdocs/a.md\0"
        )
        outputs[("status", "--porcelain=v1", "-z")] = " M docs/a.md\0"
        outputs[("add", "--", "docs/a.md")] = ""
    if crlf:
        outputs[("cat-file", "-s", OLD)] = "11\n"
        outputs[("cat-file", "-s", NEW)] = "11\n"
        outputs[("cat-file", "blob", OLD)] = "old\r\nline\r\n"
        outputs[("cat-file", "blob", NEW)] = "new\r\nline\r\n"
    if authored:
        outputs[("rev-parse", "HEAD")] = SOURCE + "\n"
        outputs[("rev-parse", "-q", "--verify", f"{BASE}^{{commit}}")] = BASE + "\n"
        outputs[("rev-parse", "-q", "--verify", "MERGE_HEAD")] = ""
        outputs[("ls-files", "--unmerged")] = ""
        outputs[("diff", "--name-only", "--no-renames", BASE)] = "docs/a.md\n"
        outputs[("diff", "--name-only", "-z", "--no-renames", BASE, TREE)] = (
            "docs/a.md\0"
        )
        outputs[("ls-files", "--others", "--exclude-standard")] = ""
        outputs[("add", "--", "docs/a.md")] = ""
        outputs[("cat-file", "blob", OLD)] = "baseline\n"
        outputs[("cat-file", "-s", OLD)] = "9\n"
        outputs[("cat-file", "blob", NEW)] = "repaired\n"
        outputs[("cat-file", "-s", NEW)] = "9\n"
    head_reads = 0

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal head_reads
        args = command[3:]
        if args == ("rev-parse", "HEAD"):
            head_reads += 1
            if drift and head_reads > 1:
                return CommandResult(0, "f" * 40 + "\n", "")
        if authored and args == ("merge-base", "--is-ancestor", BASE, "HEAD"):
            return CommandResult(0, "", "")
        if authored and args == ("merge-base", "--is-ancestor", UNRELATED, "HEAD"):
            return CommandResult(1, "", "")
        if authored and args == ("rev-parse", "-q", "--verify", "MERGE_HEAD"):
            return CommandResult(1, "", "")
        if args not in outputs:
            raise AssertionError(f"unexpected Git command: {args}")
        return CommandResult(0, outputs[args], "")

    def read_blob(command: tuple[str, ...], **kwargs: object) -> bytes:
        args = command[3:]
        if oversize and args == ("cat-file", "blob", NEW):
            raise AssertionError("oversized blob must not be fetched")
        if binary and args == ("cat-file", "blob", NEW):
            return b"\xff" * cast(int, kwargs["max_output_bytes"])
        if args not in outputs:
            raise AssertionError(f"unexpected Git blob command: {args}")
        return outputs[args].encode()

    return GitRepository(config, run, read_git_blob=read_blob)


def test_read_candidate_derives_deletion_and_exact_content_from_git(
    tmp_path: Path,
) -> None:
    evidence = _repository(tmp_path).read_candidate(
        tmp_path,
        TREE,
        base_sha=BASE,
        changed_paths=("docs/a.md", "docs/gone.md"),
    )
    assert evidence == {
        "candidate_tree": TREE,
        "base_tree": BASE,
        "changed_paths": ["docs/a.md", "docs/gone.md"],
        "files": [
            {
                "path": "docs/a.md",
                "old_mode": "100644",
                "new_mode": "100644",
                "old_oid": OLD,
                "new_oid": NEW,
                "old_content": "before\n",
                "new_content": "after!\n",
            },
            {
                "path": "docs/gone.md",
                "old_mode": "100644",
                "new_mode": None,
                "old_oid": REMOVED,
                "new_oid": None,
                "old_content": "removed\n",
                "new_content": None,
            },
        ],
    }


def test_read_candidate_preserves_crlf_bytes_from_git_blobs(tmp_path: Path) -> None:
    evidence = _repository(tmp_path, crlf=True).read_candidate(
        tmp_path, TREE, base_sha=BASE, changed_paths=("docs/a.md", "docs/gone.md")
    )
    files = cast(list[dict[str, str | None]], evidence["files"])
    assert files[0]["old_content"] == "old\r\nline\r\n"
    assert files[0]["new_content"] == "new\r\nline\r\n"


def test_read_candidate_uses_original_baseline_only_with_ancestor_allowance(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, authored=True)

    evidence = repository.read_candidate(
        tmp_path,
        TREE,
        base_sha=BASE,
        allow_base_ancestor=True,
        changed_paths=("docs/a.md",),
    )
    files = cast(list[dict[str, str | None]], evidence["files"])
    assert files[0]["old_content"] == "baseline\n"
    assert files[0]["new_content"] == "repaired\n"
    with pytest.raises(RepositoryError, match="candidate changed"):
        repository.read_candidate(
            tmp_path,
            TREE,
            base_sha=BASE,
            changed_paths=("docs/a.md",),
        )
    with pytest.raises(RepositoryError, match="candidate changed"):
        repository.read_candidate(
            tmp_path,
            TREE,
            base_sha=UNRELATED,
            allow_base_ancestor=True,
            changed_paths=("docs/a.md",),
        )


def test_raw_git_blob_runner_caps_output_and_timeout() -> None:
    class FakeOutput:
        def fileno(self) -> int:
            return 7

        def close(self) -> None:
            pass

    class FakeProcess:
        stdout = FakeOutput()

        def poll(self) -> None:
            return None

        def kill(self) -> None:
            pass

        def wait(self, *, timeout: float | None = None) -> int:
            del timeout
            return 0

    def open_process(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        return cast(subprocess.Popen[bytes], FakeProcess())

    with pytest.raises(RepositoryError, match="oversize"):
        run_subprocess_bytes(
            ("git", "cat-file", "blob", OLD),
            max_output_bytes=8,
            timeout_seconds=2,
            open_process=open_process,
            stream_readable=lambda _stream, _timeout: True,
            read_bytes=lambda _fd, _size: b"x" * 9,
            clock=lambda: 0,
        )
    with pytest.raises(CommandTimeoutError, match="timed out"):
        run_subprocess_bytes(
            ("git", "cat-file", "blob", OLD),
            max_output_bytes=8,
            timeout_seconds=2,
            open_process=open_process,
            stream_readable=lambda _stream, _timeout: False,
            read_bytes=lambda _fd, _size: b"",
            clock=lambda: 0,
        )


def test_read_candidate_rejects_oversize_blob_before_fetch(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="oversize"):
        _repository(tmp_path, oversize=True).read_candidate(
            tmp_path,
            TREE,
            base_sha=BASE,
            changed_paths=("docs/a.md", "docs/gone.md"),
        )


def test_read_candidate_accepts_existing_large_source_file(tmp_path: Path) -> None:
    evidence = _repository(tmp_path, large=True).read_candidate(
        tmp_path,
        TREE,
        base_sha=BASE,
        changed_paths=("docs/a.md", "docs/gone.md"),
    )
    files = cast(list[dict[str, str | None]], evidence["files"])
    old_content = files[0]["old_content"]
    new_content = files[0]["new_content"]
    assert old_content is not None and len(old_content) == 188_414
    assert new_content is not None and len(new_content) == 188_414


def test_read_candidate_shows_mode_only_change(tmp_path: Path) -> None:
    evidence = _repository(tmp_path, mode_only=True).read_candidate(
        tmp_path, TREE, base_sha=BASE, changed_paths=("docs/a.md",)
    )
    assert evidence["files"] == [
        {
            "path": "docs/a.md",
            "old_mode": "100644",
            "new_mode": "100755",
            "old_oid": OLD,
            "new_oid": OLD,
            "old_content": "before\n",
            "new_content": "before\n",
        }
    ]


def test_read_candidate_rejects_binary_blob(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="binary"):
        _repository(tmp_path, binary=True).read_candidate(
            tmp_path,
            TREE,
            base_sha=BASE,
            changed_paths=("docs/a.md", "docs/gone.md"),
        )


def test_read_candidate_rejects_head_drift_after_objects(tmp_path: Path) -> None:
    with pytest.raises(RepositoryError, match="changed"):
        _repository(tmp_path, drift=True).read_candidate(
            tmp_path,
            TREE,
            base_sha=BASE,
            changed_paths=("docs/a.md", "docs/gone.md"),
        )
