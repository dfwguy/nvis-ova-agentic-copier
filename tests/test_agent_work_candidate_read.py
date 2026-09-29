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
from base64 import b64decode
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
    invalid_utf8: bool = False,
    git_calls: list[tuple[str, ...]] | None = None,
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
        ("ls-tree", "-z", BASE, "--", "docs/missing.md"): "",
        ("ls-tree", "-z", TREE, "--", "docs/missing.md"): "",
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
        if git_calls is not None:
            git_calls.append(args)
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
            return b"after!\0"
        if invalid_utf8 and args == ("cat-file", "blob", NEW):
            return b"after!\xff"
        if args not in outputs:
            raise AssertionError(f"unexpected Git blob command: {args}")
        return outputs[args].encode()

    return GitRepository(config, run, read_git_blob=read_blob)


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


def test_changed_candidate_paths_inventory_exposes_exact_git_side_metadata(
    tmp_path: Path,
) -> None:
    inventory = _repository(tmp_path).changed_candidate_paths(
        tmp_path, TREE, base_sha=BASE
    )
    assert inventory["candidate_tree"] == TREE
    assert inventory["base_tree"] == BASE
    assert inventory["changed_paths"] == ["docs/a.md", "docs/gone.md"]
    assert inventory["files"] == [
        {
            "path": "docs/a.md",
            "old_mode": "100644",
            "old_oid": OLD,
            "old_size": 7,
            "new_mode": "100644",
            "new_oid": NEW,
            "new_size": 7,
        },
        {
            "path": "docs/gone.md",
            "old_mode": "100644",
            "old_oid": REMOVED,
            "old_size": 8,
            "new_mode": None,
            "new_oid": None,
            "new_size": None,
        },
    ]


@pytest.mark.parametrize(
    ("side", "offset", "length", "expected"),
    [
        ("new", 4, 3, b"r!\n"),
        ("old", 0, 3, b"bef"),
        ("new", 0, 7, b"after!\n"),
    ],
)
def test_candidate_range_read_is_independent_and_exact(
    tmp_path: Path, side: str, offset: int, length: int, expected: bytes
) -> None:
    read = _repository(tmp_path).read_candidate_range(
        tmp_path,
        TREE,
        base_sha=BASE,
        path="docs/a.md",
        side=side,
        offset=offset,
        length=length,
    )
    assert read["candidate_tree"] == TREE
    assert read["path"] == "docs/a.md"
    assert read["side"] == side
    assert read["offset"] == offset
    assert read["length"] == length
    assert read["oid"] == (OLD if side == "old" else NEW)
    assert b64decode(read["data_base64"], validate=True) == expected


def test_repeated_range_reads_do_not_scan_candidate_inventory(
    tmp_path: Path,
) -> None:
    git_calls: list[tuple[str, ...]] = []
    repository = _repository(tmp_path, git_calls=git_calls)
    for offset, expected in ((0, b"aft"), (3, b"er!")):
        read = repository.read_candidate_range(
            tmp_path,
            TREE,
            base_sha=BASE,
            path="docs/a.md",
            side="new",
            offset=offset,
            length=3,
        )
        assert b64decode(read["data_base64"], validate=True) == expected
    assert not any(args[:2] == ("diff", "--name-only") for args in git_calls)
    assert not any("docs/gone.md" in args for args in git_calls)


@pytest.mark.parametrize(
    ("path", "side", "offset", "length"),
    [
        ("docs/missing.md", "new", 0, 1),
        ("docs/gone.md", "new", 0, 1),
        ("docs/a.md", "other", 0, 1),
        ("docs/a.md", "new", True, 1),
        ("docs/a.md", "new", 0, False),
        ("docs/a.md", "new", -1, 1),
        ("docs/a.md", "new", 0, 0),
        ("docs/a.md", "new", 6, 2),
        ("docs/a.md", "new", 0, 131073),
    ],
)
def test_candidate_range_read_rejects_invalid_path_side_or_range(
    tmp_path: Path, path: str, side: str, offset: int, length: int
) -> None:
    with pytest.raises(RepositoryError):
        _repository(tmp_path).read_candidate_range(
            tmp_path,
            TREE,
            base_sha=BASE,
            path=path,
            side=side,
            offset=offset,
            length=length,
        )


def test_stateless_inventory_rejects_oversize_and_head_drift(tmp_path: Path) -> None:
    for option in ("oversize", "drift"):
        with pytest.raises(RepositoryError):
            _repository(
                tmp_path, oversize=option == "oversize", drift=option == "drift"
            ).changed_candidate_paths(tmp_path, TREE, base_sha=BASE)


def test_stateless_range_keeps_deletion_and_large_file_bounds(tmp_path: Path) -> None:
    deleted = _repository(tmp_path).read_candidate_range(
        tmp_path,
        TREE,
        base_sha=BASE,
        path="docs/gone.md",
        side="old",
        offset=0,
        length=8,
    )
    assert deleted["oid"] == REMOVED
    assert b64decode(deleted["data_base64"], validate=True) == b"removed\n"
    large = _repository(tmp_path, large=True).read_candidate_range(
        tmp_path,
        TREE,
        base_sha=BASE,
        path="docs/a.md",
        side="new",
        offset=188_413,
        length=1,
    )
    assert large["size"] == 188_414
    assert b64decode(large["data_base64"], validate=True) == b"x"


@pytest.mark.parametrize("option", ["oversize", "binary", "invalid_utf8", "head_drift"])
def test_stateless_range_rejects_unreadable_or_changed_side(
    tmp_path: Path, option: str
) -> None:
    with pytest.raises(RepositoryError):
        _repository(
            tmp_path,
            oversize=option == "oversize",
            binary=option == "binary",
            invalid_utf8=option == "invalid_utf8",
            drift=option == "head_drift",
        ).read_candidate_range(
            tmp_path,
            TREE,
            base_sha=BASE,
            path="docs/a.md",
            side="new",
            offset=0,
            length=1,
        )
