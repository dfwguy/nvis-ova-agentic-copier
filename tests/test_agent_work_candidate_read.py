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

from __future__ import annotations

import asyncio
import json
import subprocess
from base64 import b64decode, b64encode
from collections.abc import Callable
from copy import deepcopy
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from mcp import Client
from mcp.types import SERVER_INFO_META_KEY, CallToolResult, TextContent

from agent_work import mcp_server
from agent_work.models import AgentWorkConfig
from agent_work.repository import (
    CommandResult,
    CommandTimeoutError,
    GitRepository,
    RepositoryError,
    run_subprocess_bytes,
)
from agent_work.worker import WorkerProtocolError

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
        outputs[("rev-parse", f"{SOURCE}^{{tree}}")] = TREE + "\n"
        outputs[("rev-parse", f"{UNRELATED}^{{tree}}")] = TREE + "\n"
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
        if authored and args == ("merge-base", "--is-ancestor", BASE, SOURCE):
            return CommandResult(0, "", "")
        if authored and args == ("merge-base", "--is-ancestor", BASE, UNRELATED):
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


def test_committed_inventory_reads_36_paths_without_a_staged_index(
    tmp_path: Path,
) -> None:
    paths = tuple(f"docs/file-{number:02}.md" for number in range(36))
    calls: list[tuple[str, ...]] = []
    config = AgentWorkConfig(
        host="gitlab.example.com",
        project_id=81,
        checkout=tmp_path,
        state_root=tmp_path / "state",
        git_path=Path("/usr/bin/git"),
        glab_path=Path("/usr/bin/glab"),
        base_branch="agent/base",
    )

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        args = command[3:]
        calls.append(args)
        if args == ("rev-parse", "HEAD"):
            return CommandResult(0, SOURCE + "\n", "")
        if args == ("rev-parse", f"{SOURCE}^{{tree}}"):
            return CommandResult(0, TREE + "\n", "")
        if args == ("rev-parse", f"{BASE}^{{tree}}"):
            return CommandResult(0, BASE + "\n", "")
        if args == ("merge-base", "--is-ancestor", BASE, SOURCE):
            return CommandResult(0, "", "")
        if args == ("diff", "--name-only", "-z", "--no-renames", BASE, TREE):
            return CommandResult(0, "\0".join(paths) + "\0", "")
        if args == ("write-tree",):
            raise AssertionError("committed reads must not depend on the index")
        if len(args) == 5 and args[:2] == ("ls-tree", "-z"):
            tree, _, path = args[2:]
            if path not in paths:
                raise AssertionError(f"unexpected path: {path}")
            if tree == BASE:
                return CommandResult(0, "", "")
            if tree == TREE:
                return CommandResult(0, f"100644 blob {NEW}\t{path}\0", "")
        if args == ("cat-file", "-s", NEW):
            return CommandResult(0, "7\n", "")
        raise AssertionError(f"unexpected Git command: {args}")

    inventory = GitRepository(config, run).changed_candidate_paths(
        tmp_path, TREE, base_sha=BASE, candidate_sha=SOURCE
    )
    assert inventory["candidate_tree"] == TREE
    assert inventory["changed_paths"] == list(paths)
    assert len(inventory["files"]) == 36
    assert ("write-tree",) not in calls


def test_committed_read_rejects_different_commit_with_same_tree(
    tmp_path: Path,
) -> None:
    with pytest.raises(RepositoryError):
        _repository(tmp_path, authored=True).read_candidate_range(
            tmp_path,
            TREE,
            base_sha=BASE,
            candidate_sha=UNRELATED,
            path="docs/a.md",
            side="new",
            offset=0,
            length=1,
        )


def test_committed_range_rechecks_exact_blob_bytes(tmp_path: Path) -> None:
    original = b"before\n"
    committed = b"after!\n"
    altered = b"aftxr!\n"

    def oid(data: bytes) -> str:
        return sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()

    old_oid = oid(original)
    new_oid = oid(committed)
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
        ("rev-parse", "HEAD"): SOURCE + "\n",
        ("rev-parse", f"{SOURCE}^{{tree}}"): TREE + "\n",
        ("rev-parse", f"{BASE}^{{tree}}"): BASE + "\n",
        ("ls-tree", "-z", BASE, "--", "docs/a.md"): (
            f"100644 blob {old_oid}\tdocs/a.md\0"
        ),
        ("ls-tree", "-z", TREE, "--", "docs/a.md"): (
            f"100644 blob {new_oid}\tdocs/a.md\0"
        ),
        ("cat-file", "-s", old_oid): f"{len(original)}\n",
        ("cat-file", "-s", new_oid): f"{len(committed)}\n",
    }

    def run(command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        args = command[3:]
        if args == ("write-tree",):
            raise AssertionError("committed reads must not depend on the index")
        if args == ("merge-base", "--is-ancestor", BASE, SOURCE):
            return CommandResult(0, "", "")
        if args not in outputs:
            raise AssertionError(f"unexpected Git command: {args}")
        return CommandResult(0, outputs[args], "")

    def read_blob(command: tuple[str, ...], **_kwargs: object) -> bytes:
        assert command[3:] == ("cat-file", "blob", new_oid)
        return altered

    with pytest.raises(RepositoryError, match="candidate"):
        GitRepository(config, run, read_git_blob=read_blob).read_candidate_range(
            tmp_path,
            TREE,
            base_sha=BASE,
            candidate_sha=SOURCE,
            path="docs/a.md",
            side="new",
            offset=0,
            length=1,
        )


IDENTITY: dict[str, object] = {
    "accepted": True,
    "candidate_tree": TREE,
    "base_sha": BASE,
    "base_tree": "c" * 40,
    "ledger_revision": 7,
    "source_branch": "agent/issue-81",
    "nonce": "private-controller-nonce",
    "issue_iid": 81,
    "project_id": 310967,
}


def _receipts(
    payloads: tuple[bytes, ...],
) -> tuple[
    dict[str, object], tuple[dict[str, object], ...], tuple[dict[str, object], ...]
]:
    files: list[dict[str, object]] = []
    selectors: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    for index, payload in enumerate(payloads):
        path = f"docs/file-{index:03}.md"
        oid = sha256(payload).hexdigest()[:40]
        files.append(
            {
                "path": path,
                "old_mode": None,
                "old_oid": None,
                "old_size": None,
                "new_mode": "100644" if index % 2 == 0 else "100755",
                "new_oid": oid,
                "new_size": len(payload),
            }
        )
        selector: dict[str, object] = {
            "path": path,
            "side": "new",
            "offset": 0,
            "length": len(payload),
        }
        selectors.append(selector)
        results.append(
            IDENTITY
            | selector
            | {
                "oid": oid,
                "size": len(payload),
                "data_base64": b64encode(payload).decode("ascii"),
            }
        )
    inventory = IDENTITY | {
        "changed_paths": [file["path"] for file in files],
        "files": files,
    }
    return inventory, tuple(selectors), tuple(results)


def _group(
    inventory: dict[str, object],
    selectors: tuple[dict[str, object], ...],
    results: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return mcp_server.group_candidate_read_results(
        inventory,
        selectors,
        results,
        candidate_tree=TREE,
    )


def _call(gateway: Any, arguments: dict[str, object]) -> Any:
    async def run() -> Any:
        async with Client(
            mcp_server.create_candidate_mcp_server(gateway, TREE)
        ) as client:
            return await client.call_tool("candidate_read", arguments)

    return asyncio.run(run())


@given(st.lists(st.binary(min_size=1, max_size=24), min_size=1, max_size=10))
def test_grouped_read_generated_lossless_order_context_and_first_occurrence(
    payloads: list[bytes],
) -> None:
    inventory, selectors, results = _receipts(tuple(payloads))
    before = deepcopy((inventory, selectors, results))
    grouped = _group(inventory, selectors, results)
    bindings = cast(list[dict[str, Any]], grouped["bindings"])
    contents = cast(list[dict[str, str]], grouped["contents"])
    unique = list(dict.fromkeys(payloads))
    assert [
        b64decode(item["data_base64"], validate=True) for item in contents
    ] == unique
    assert [
        b64decode(contents[binding["content_index"]]["data_base64"], validate=True)
        for binding in bindings
    ] == payloads
    assert bindings == [
        selector
        | {
            "request_index": index,
            "oid": result["oid"],
            "size": result["size"],
            "content_index": unique.index(payloads[index]),
        }
        for index, (selector, result) in enumerate(zip(selectors, results, strict=True))
    ]
    assert grouped["inventory"] == {
        "changed_paths": inventory["changed_paths"],
        "files": inventory["files"],
    }
    assert grouped == _group(inventory, selectors, results)
    assert (inventory, selectors, results) == before
    for first, left in enumerate(bindings):
        for second, right in enumerate(bindings):
            assert (left["content_index"] == right["content_index"]) == (
                payloads[first] == payloads[second]
            )


@given(
    st.binary(min_size=1, max_size=24),
    st.sampled_from(
        [
            "candidate_tree",
            "base_tree",
            "base_sha",
            "nonce",
            "issue_iid",
            "project_id",
            "ledger_revision",
            "source_branch",
            "path",
            "side",
            "offset",
            "length",
            "oid",
            "size",
        ]
    ),
)
def test_grouped_read_generated_any_inconsistent_member_rejects(
    payload: bytes,
    field: str,
) -> None:
    inventory, selectors, results = _receipts((payload, payload))
    changed = deepcopy(results)
    value = changed[1][field]
    changed[1][field] = value + 1 if type(value) is int else "invalid"
    with pytest.raises(ValueError):
        _group(inventory, selectors, changed)


@pytest.mark.parametrize("field", list(IDENTITY))
def test_grouped_read_missing_required_proof_rejects(field: str) -> None:
    inventory, selectors, results = _receipts((b"abc",))
    del results[0][field]
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


@pytest.mark.parametrize("encoded", ["!", "YQ==", "YWJjZA==", "YWJj\n"])
def test_grouped_read_malformed_or_wrong_length_bytes_reject(encoded: str) -> None:
    inventory, selectors, results = _receipts((b"abc",))
    results[0]["data_base64"] = encoded
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("new_mode", "120000"),
        ("new_oid", "f" * 40),
        ("new_size", 4),
        ("new_size", True),
        ("new_size", 2_097_153),
        ("path", "docs/other.md"),
    ],
)
def test_grouped_read_inventory_mismatch_rejects(field: str, value: object) -> None:
    inventory, selectors, results = _receipts((b"abc",))
    cast(list[dict[str, object]], inventory["files"])[0][field] = value
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


def test_grouped_read_same_oid_range_with_unequal_bytes_rejects() -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    results[1]["data_base64"] = b64encode(b"xyz").decode("ascii")
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


def test_grouped_read_different_offsets_of_same_oid_preserve_unequal_bytes() -> None:
    inventory, selectors, results = _receipts((b"abcxyz",))
    first = selectors[0] | {"length": 3}
    second = first | {"offset": 3}
    grouped = _group(
        inventory,
        (first, second),
        (
            results[0] | first | {"data_base64": "YWJj"},
            results[0] | second | {"data_base64": "eHl6"},
        ),
    )
    assert grouped["contents"] == [{"data_base64": "YWJj"}, {"data_base64": "eHl6"}]


def test_grouped_read_equal_bytes_different_oids_modes_and_utf8_slice_share() -> None:
    inventory, selectors, results = _receipts((b"\xc3", b"\xc3"))
    file = cast(list[dict[str, object]], inventory["files"])[1]
    file["new_oid"] = "f" * 40
    results[1]["oid"] = "f" * 40
    grouped = _group(inventory, selectors, results)
    assert grouped["contents"] == [{"data_base64": "ww=="}]
    assert [
        binding["content_index"]
        for binding in cast(list[dict[str, object]], grouped["bindings"])
    ] == [0, 0]


@pytest.mark.parametrize("side", ["old", "new"])
def test_grouped_read_present_empty_and_absent_inventory_do_not_fabricate_reads(
    side: str,
) -> None:
    inventory, selectors, results = _receipts((b"abc", b""))
    files = cast(list[dict[str, object]], inventory["files"])
    empty_file = files[1]
    if side == "old":
        for suffix in ("mode", "oid", "size"):
            empty_file[f"old_{suffix}"] = empty_file[f"new_{suffix}"]
            empty_file[f"new_{suffix}"] = None
    grouped = _group(inventory, selectors[:1], results[:1])
    assert grouped["inventory"] == {
        "changed_paths": inventory["changed_paths"],
        "files": files,
    }
    assert len(cast(list[object], grouped["bindings"])) == 1
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


@pytest.mark.parametrize(
    "mutation", ["absent", "outside", "not_accepted", "extra", "count"]
)
def test_grouped_read_closed_inventory_and_result_shape_rejects(mutation: str) -> None:
    inventory, selectors, results = _receipts((b"abc",))
    if mutation == "absent":
        selectors = (selectors[0] | {"side": "old"},)
        results = (results[0] | {"side": "old"},)
    elif mutation == "outside":
        inventory["changed_paths"] = []
        inventory["files"] = []
    elif mutation == "not_accepted":
        results[0]["accepted"] = False
    elif mutation == "extra":
        results[0]["secret"] = "private detail"
    else:
        results = ()
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


def test_candidate_mcp_batch_equal_bytes_preserves_independent_calls_and_duplicates() -> (
    None
):
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    calls: list[dict[str, object] | None] = []

    def gateway(
        action: str, tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        assert action == "read" and tree == TREE
        calls.append(selector)
        if selector == {"mode": "changed_paths"}:
            return inventory
        return results[selectors.index(cast(dict[str, object], selector))]

    reads = (*selectors, selectors[0])
    result = _call(gateway, {"reads": list(reads)})
    assert result.is_error is False, (
        "grouped candidate_read must accept independently authorized ranges"
    )
    assert calls == [{"mode": "changed_paths"}, *reads]
    public = result.structured_content
    assert public["contents"] == [{"data_base64": "YWJj"}]
    assert [binding["request_index"] for binding in public["bindings"]] == [0, 1, 2]
    assert public["inventory"] == {
        "changed_paths": inventory["changed_paths"],
        "files": inventory["files"],
    }
    assert public["candidate_tree"] == TREE and public["base_sha"] == BASE
    assert (
        public["ledger_revision"] == 7 and public["source_branch"] == "agent/issue-81"
    )
    assert all(
        key not in json.dumps(result.model_dump())
        for key in ("nonce", "issue_iid", "project_id", "private-controller-nonce")
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"reads": []},
        {"reads": "invalid"},
        {"reads": [{"path": "../a", "side": "new", "offset": 0, "length": 1}]},
        {"reads": [{"path": "docs/a", "side": "new", "offset": True, "length": 1}]},
        {"reads": [{"path": "docs/a", "side": "new", "offset": 0, "length": True}]},
        {"reads": [{"path": "docs/a", "side": "new", "offset": -1, "length": 1}]},
        {"reads": [{"path": "docs/a", "side": "new", "offset": 0, "length": 131073}]},
        {
            "reads": [
                {
                    "path": "docs/a",
                    "side": "new",
                    "offset": 0,
                    "length": 1,
                    "nonce": "secret",
                }
            ]
        },
        {"reads": [], "path": "docs/a", "side": "new", "offset": 0, "length": 1},
    ],
)
def test_candidate_mcp_batch_invalid_members_prevalidate_before_gateway(
    invalid: dict[str, object],
) -> None:
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return {}

    result = _call(gateway, invalid)
    assert result.is_error is True
    assert calls == []


@pytest.mark.parametrize("failure", ["refusal", "budget", "stale", "bytes", "private"])
def test_candidate_mcp_batch_later_member_failure_returns_no_partial_result(
    failure: str,
) -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        if selector == {"mode": "changed_paths"}:
            return inventory
        if selector == selectors[0]:
            return results[0]
        if failure in {"refusal", "budget"}:
            raise WorkerProtocolError(
                "candidate_broker", f"request refused: {failure} private detail"
            )
        field, value = {
            "stale": ("base_tree", "f" * 40),
            "bytes": ("data_base64", "eHl6"),
            "private": ("nonce", "other-private"),
        }[failure]
        return results[1] | {field: value}

    result = _call(gateway, {"reads": list(selectors)})
    assert calls == [{"mode": "changed_paths"}, *selectors]
    assert result.is_error is True
    assert result.structured_content is None
    assert [item.text for item in result.content] == ["candidate_broker: read failed"]


def test_candidate_mcp_batch_more_than_inventory_path_limit_and_repeated_bytes_allowed() -> (
    None
):
    inventory, selectors, results = _receipts((b"x" * 131072,))
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return inventory if selector == {"mode": "changed_paths"} else results[0]

    result = _call(gateway, {"reads": [selectors[0]] * 129})
    assert result.is_error is False
    assert len(calls) == 130
    assert len(result.structured_content["bindings"]) == 129
    assert len(result.structured_content["contents"]) == 1


def test_candidate_mcp_batch_mandatory_binding_oversize_refuses_before_gateway() -> (
    None
):
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return {}

    selector = {"path": "docs/" + "a" * 500, "side": "new", "offset": 0, "length": 1}
    result = _call(gateway, {"reads": [selector] * 2000})
    assert result.is_error is True
    assert calls == []


def test_candidate_mcp_batch_complete_result_bound_then_smaller_fresh_call_succeeds() -> (
    None
):
    payloads = tuple(bytes([index]) * 131072 for index in range(6))
    inventory, selectors, results = _receipts(payloads)
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return (
            inventory
            if selector == {"mode": "changed_paths"}
            else results[selectors.index(cast(dict[str, object], selector))]
        )

    oversized = _call(gateway, {"reads": list(selectors)})
    assert oversized.is_error is True
    assert oversized.structured_content is None
    assert calls == [{"mode": "changed_paths"}, *selectors]
    smaller = _call(gateway, {"reads": list(selectors[:1])})
    assert smaller.is_error is False
    assert calls[-2:] == [{"mode": "changed_paths"}, selectors[0]]
    assert (
        len(json.dumps(smaller.structured_content, ensure_ascii=True).encode())
        <= 900000
    )


@pytest.mark.parametrize("field", ["commit_sha", "committed_tree"])
def test_grouped_read_available_commit_proof_mismatch_or_omission_rejects(
    field: str,
) -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    proof = {"commit_sha": "e" * 40, "committed_tree": TREE}
    inventory.update(proof)
    for result in results:
        result.update(proof)
    results[1][field] = "f" * 40
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)
    del results[1][field]
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


def test_grouped_read_valid_commit_proof_survives_helper_and_public_batch() -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    proof = {"commit_sha": "e" * 40, "committed_tree": TREE}
    inventory.update(proof)
    for result in results:
        result.update(proof)
    grouped = _group(inventory, selectors, results)
    assert {field: grouped[field] for field in proof} == proof
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return (
            inventory
            if selector == {"mode": "changed_paths"}
            else results[selectors.index(cast(dict[str, object], selector))]
        )

    response = _call(gateway, {"reads": list(selectors)})
    assert response.is_error is False
    assert calls == [{"mode": "changed_paths"}, *selectors]
    assert {field: response.structured_content[field] for field in proof} == proof
    assert response.structured_content["contents"] == [{"data_base64": "YWJj"}]


def test_grouped_read_deleted_and_mode_only_old_new_bindings_retain_inventory() -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    files = cast(list[dict[str, object]], inventory["files"])
    for file in files:
        for suffix in ("mode", "oid", "size"):
            file[f"old_{suffix}"] = file[f"new_{suffix}"]
    files[1]["old_mode"] = "100644"
    for suffix in ("mode", "oid", "size"):
        files[0][f"new_{suffix}"] = None
    old = selectors[0] | {"side": "old"}
    mode_old = selectors[1] | {"side": "old"}
    grouped = _group(
        inventory,
        (old, mode_old, selectors[1]),
        (
            results[0] | old,
            results[1] | mode_old,
            results[1],
        ),
    )
    assert grouped["inventory"] == {
        "changed_paths": inventory["changed_paths"],
        "files": files,
    }
    assert grouped["contents"] == [{"data_base64": "YWJj"}]
    assert [
        binding["side"]
        for binding in cast(list[dict[str, object]], grouped["bindings"])
    ] == ["old", "old", "new"]


def test_candidate_mcp_batch_later_invalid_member_prevalidates_before_inventory() -> (
    None
):
    _inventory, selectors, _results = _receipts((b"abc",))
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return {}

    result = _call(gateway, {"reads": [selectors[0], selectors[0] | {"length": False}]})
    assert result.is_error is True
    assert calls == []


def test_candidate_mcp_batch_inventory_and_bindings_count_toward_response_bound() -> (
    None
):
    inventory, selectors, results = _receipts(
        tuple(bytes([index]) * 131072 for index in range(5))
    )
    files = cast(list[dict[str, object]], inventory["files"])
    for index in range(123):
        files.append(
            {
                "path": f"extra/{index:03}/" + "x" * 490,
                "old_mode": None,
                "old_oid": None,
                "old_size": None,
                "new_mode": "100644",
                "new_oid": "d" * 40,
                "new_size": 1,
            }
        )
    files.sort(key=lambda item: cast(str, item["path"]))
    inventory["changed_paths"] = [item["path"] for item in files]
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selector)
        return (
            inventory
            if selector == {"mode": "changed_paths"}
            else results[selectors.index(cast(dict[str, object], selector))]
        )

    result = _call(gateway, {"reads": list(selectors)})
    assert sum(len(cast(str, item["data_base64"])) for item in results) < 900000
    assert result.is_error is True
    assert result.structured_content is None
    assert calls == [{"mode": "changed_paths"}, *selectors]


def test_grouped_read_same_oid_aliases_require_same_full_object_size() -> None:
    inventory, selectors, results = _receipts((b"abc", b"abc"))
    files = cast(list[dict[str, object]], inventory["files"])
    files[1]["new_size"] = 4
    results[1]["size"] = 4
    with pytest.raises(ValueError):
        _group(inventory, selectors, results)


def _expected_batch_payload(
    inventory: dict[str, object],
    selectors: tuple[dict[str, object], ...],
    results: tuple[dict[str, object], ...],
) -> dict[str, object]:
    contents: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    seen: list[bytes] = []
    for index, (selector, result) in enumerate(zip(selectors, results, strict=True)):
        payload = b64decode(cast(str, result["data_base64"]), validate=True)
        if payload not in seen:
            seen.append(payload)
            contents.append({"data_base64": b64encode(payload).decode("ascii")})
        bindings.append(
            selector
            | {
                "request_index": index,
                "oid": result["oid"],
                "size": result["size"],
                "content_index": seen.index(payload),
            }
        )
    return {
        key: inventory[key]
        for key in (
            "accepted",
            "candidate_tree",
            "base_sha",
            "base_tree",
            "ledger_revision",
            "source_branch",
        )
    } | {
        "inventory": {
            "changed_paths": inventory["changed_paths"],
            "files": inventory["files"],
        },
        "bindings": bindings,
        "contents": contents,
    }


def _expected_tool_result(
    payload: dict[str, object], *, stamp: dict[str, Any] | None = None
) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(text=json.dumps(payload, ensure_ascii=True))],
        structured_content=payload,
        _meta=None if stamp is None else {SERVER_INFO_META_KEY: stamp},
    )


def _tool_result_body_size(result: CallToolResult) -> int:
    return len(result.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))


def _fixture_gateway(
    inventory: dict[str, object],
    selectors: tuple[dict[str, object], ...],
    results: tuple[dict[str, object], ...],
    calls: list[dict[str, object] | None],
) -> Callable[[str, str, dict[str, object] | None], dict[str, object]]:
    def gateway(
        action: str, tree: str, selector: dict[str, object] | None
    ) -> dict[str, object]:
        assert action == "read" and tree == TREE
        calls.append(selector)
        return (
            inventory
            if selector == {"mode": "changed_paths"}
            else results[selectors.index(cast(dict[str, object], selector))]
        )

    return gateway


def _call_with_server_stamp(
    gateway: Callable[[str, str, dict[str, object] | None], dict[str, object]],
    arguments: dict[str, object],
) -> tuple[CallToolResult, dict[str, Any]]:
    async def run() -> tuple[CallToolResult, dict[str, Any]]:
        async with Client(
            mcp_server.create_candidate_mcp_server(gateway, TREE)
        ) as client:
            assert client.server_info is not None
            stamp = client.server_info.model_dump(
                by_alias=True, mode="json", exclude_none=True
            )
            response = await client.call_tool("candidate_read", arguments)
            assert response.meta is not None
            assert response.meta[SERVER_INFO_META_KEY] == stamp
            return response, stamp

    return asyncio.run(run())


def test_candidate_mcp_batch_near_complete_body_limit_preserves_all_verified_reads() -> (
    None
):
    inventory, selectors, results = _receipts(
        (b"a" * 131072, b"b" * 131072, b"c" * 74000)
    )
    expected = _expected_batch_payload(inventory, selectors, results)
    calls: list[dict[str, object] | None] = []
    response, stamp = _call_with_server_stamp(
        _fixture_gateway(inventory, selectors, results, calls),
        {"reads": list(selectors)},
    )
    expected_result = _expected_tool_result(expected, stamp=stamp)
    assert 895000 < _tool_result_body_size(expected_result) <= 900000
    assert response.is_error is False, (
        "complete-body bounded batch must accept a valid near-limit response"
    )
    assert response.structured_content == expected
    assert response.result_type == "complete"
    assert calls == [{"mode": "changed_paths"}, *selectors]
    assert _tool_result_body_size(response) == _tool_result_body_size(expected_result)
    assert 895000 < _tool_result_body_size(response) <= 900000
    rendered = response.model_dump_json(by_alias=True, exclude_none=True)
    assert all(
        field not in rendered
        for field in ("nonce", "issue_iid", "project_id", "private-controller-nonce")
    )


def test_candidate_mcp_batch_complete_body_overflow_rejects_then_smaller_fresh_call_succeeds() -> (
    None
):
    inventory, selectors, results = _receipts(
        (b"a" * 131072, b"b" * 131072, b"c" * 76000)
    )
    expected = _expected_batch_payload(inventory, selectors, results)
    calls: list[dict[str, object] | None] = []
    gateway = _fixture_gateway(inventory, selectors, results, calls)
    response, stamp = _call_with_server_stamp(gateway, {"reads": list(selectors)})
    assert len(json.dumps(expected, ensure_ascii=True).encode("utf-8")) < 900000
    assert _tool_result_body_size(_expected_tool_result(expected, stamp=stamp)) > 900000
    assert calls == [{"mode": "changed_paths"}, *selectors]
    assert response.is_error is True
    assert response.structured_content is None
    assert [item.model_dump()["text"] for item in response.content] == [
        "candidate_broker: read failed"
    ]
    assert _tool_result_body_size(response) <= 900000
    smaller, smaller_stamp = _call_with_server_stamp(
        gateway, {"reads": list(selectors[:1])}
    )
    assert smaller_stamp == stamp
    assert smaller.is_error is False
    assert smaller.structured_content == _expected_batch_payload(
        inventory, selectors[:1], results[:1]
    )
    assert calls[-2:] == [{"mode": "changed_paths"}, selectors[0]]
    assert _tool_result_body_size(smaller) <= 900000


def test_candidate_mcp_batch_text_escaping_alone_pushes_complete_body_over_limit() -> (
    None
):
    inventory, selectors, results = _receipts((b"a" * 65536,))
    path = "docs/" + '"' * 300
    selectors[0]["path"] = path
    results[0]["path"] = path
    cast(list[dict[str, object]], inventory["files"])[0]["path"] = path
    inventory["changed_paths"] = [path]
    reads = selectors * 350
    returned = results * 350
    minimum: dict[str, object] = {
        "bindings": [
            selector | {"request_index": index} for index, selector in enumerate(reads)
        ]
    }
    expected = _expected_batch_payload(inventory, reads, returned)
    calls: list[dict[str, object] | None] = []
    response, stamp = _call_with_server_stamp(
        _fixture_gateway(inventory, selectors, results, calls), {"reads": list(reads)}
    )
    assert _tool_result_body_size(_expected_tool_result(minimum, stamp=stamp)) < 900000
    structured_bytes = len(json.dumps(expected, ensure_ascii=True).encode("utf-8"))
    assert 2 * structured_bytes < 900000
    assert _tool_result_body_size(_expected_tool_result(expected, stamp=stamp)) > 900000
    assert calls == [{"mode": "changed_paths"}, *reads]
    assert response.is_error is True
    assert response.structured_content is None
    assert [item.model_dump()["text"] for item in response.content] == [
        "candidate_broker: read failed"
    ]


def test_candidate_mcp_batch_complete_minimum_overflow_prevents_all_gateway_calls() -> (
    None
):
    selector: dict[str, object] = {
        "path": "docs/" + "a" * 400,
        "side": "new",
        "offset": 0,
        "length": 1,
    }
    reads = (selector,) * 1000
    minimum: dict[str, object] = {
        "bindings": [
            item | {"request_index": index} for index, item in enumerate(reads)
        ]
    }
    calls: list[object] = []

    def gateway(
        _action: str, _tree: str, selection: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(selection)
        return {}

    response, stamp = _call_with_server_stamp(gateway, {"reads": list(reads)})
    assert len(json.dumps(minimum, ensure_ascii=True).encode("utf-8")) < 900000
    assert _tool_result_body_size(_expected_tool_result(minimum, stamp=stamp)) > 900000
    assert response.is_error is True
    assert response.structured_content is None
    assert calls == []


def test_candidate_mcp_legacy_dictionary_limit_preserved_above_new_batch_body_limit() -> (
    None
):
    inventory, selectors, results = _receipts((b"abc",))
    legacy = results[0] | {"padding": "x" * 450000}
    public = {
        key: value
        for key, value in legacy.items()
        if key not in {"nonce", "issue_iid", "project_id"}
    }
    calls: list[dict[str, object] | None] = []
    response, stamp = _call_with_server_stamp(
        _fixture_gateway(inventory, selectors, (legacy,), calls), selectors[0]
    )
    assert len(json.dumps(public, ensure_ascii=True).encode("utf-8")) < 900000
    assert _tool_result_body_size(_expected_tool_result(public, stamp=stamp)) > 900000
    assert response.is_error is False
    assert response.structured_content == public
    assert calls == [selectors[0]]
    assert _tool_result_body_size(response) > 900000


def test_candidate_mcp_batch_sdk_metadata_alone_pushes_complete_body_over_limit() -> (
    None
):
    inventory, selectors, results = _receipts(
        (b"a" * 131072, b"b" * 131072, b"c" * 74120)
    )
    expected = _expected_batch_payload(inventory, selectors, results)
    calls: list[dict[str, object] | None] = []
    response, stamp = _call_with_server_stamp(
        _fixture_gateway(inventory, selectors, results, calls),
        {"reads": list(selectors)},
    )
    bare = _expected_tool_result(expected)
    stamped = _expected_tool_result(expected, stamp=stamp)
    assert _tool_result_body_size(bare) <= 900000 < _tool_result_body_size(stamped)
    assert stamped.model_dump(by_alias=True, exclude_none=True)["_meta"] == {
        SERVER_INFO_META_KEY: stamp
    }
    assert calls == [{"mode": "changed_paths"}, *selectors]
    assert response.is_error is True
    assert response.structured_content is None
    assert [item.model_dump()["text"] for item in response.content] == [
        "candidate_broker: read failed"
    ]
    assert _tool_result_body_size(response) <= 900000
