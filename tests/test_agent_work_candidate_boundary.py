# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Behavior contracts for the controller-owned candidate boundary."""

import errno
import os
import resource
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_work.candidate_boundary import (
    CandidateBoundaryError,
    CandidateSnapshot,
    capture_candidate,
    read_candidate,
    read_candidate_file,
)


def test_capture_candidate_keeps_supplied_metadata_and_changed_paths(
    tmp_path: Path,
) -> None:
    """A snapshot retains supplied metadata and captured content."""
    (tmp_path / "README.md").write_text("candidate\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")

    snapshot = capture_candidate(
        root=tmp_path,
        base_tree="0" * 40,
        observed_paths=("README.md", "src/app.py"),
        tree_oid="1" * 40,
        staged_paths=("README.md", "src/app.py"),
    )

    assert isinstance(snapshot, CandidateSnapshot)
    assert snapshot.tree_oid == "1" * 40
    assert snapshot.base_tree == "0" * 40
    assert snapshot.changed_paths == ("README.md", "src/app.py")
    assert read_candidate(snapshot) == (
        b"\x00\x00\x00\x09README.md"
        b"\x00\x00\x00\x00\x00\x00\x00\x0acandidate\n"
        b"\x00\x00\x00\x0asrc/app.py"
        b"\x00\x00\x00\x00\x00\x00\x00\x0avalue = 1\n"
    )
    assert read_candidate_file(snapshot, "src/app.py") == b"value = 1\n"


def test_read_candidate_serialization_distinguishes_file_boundaries(
    tmp_path: Path,
) -> None:
    single_root = tmp_path / "single"
    single_root.mkdir()
    (single_root / "a").write_bytes(b"x\0b\0y")
    split_root = tmp_path / "split"
    split_root.mkdir()
    (split_root / "a").write_bytes(b"x\0")
    (split_root / "b").write_bytes(b"y")

    single = capture_candidate(
        root=single_root,
        base_tree="0" * 40,
        observed_paths=("a",),
        tree_oid="1" * 40,
        staged_paths=("a",),
    )
    split = capture_candidate(
        root=split_root,
        base_tree="0" * 40,
        observed_paths=("a", "b"),
        tree_oid="1" * 40,
        staged_paths=("a", "b"),
    )

    assert read_candidate(single) != read_candidate(split)


def test_capture_candidate_rejects_symlinked_root_before_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "app.py").write_text("outside\n", encoding="utf-8")
    root = tmp_path / "candidate"
    root.symlink_to(outside, target_is_directory=True)
    original_scandir = os.scandir
    outside_stat = outside.stat()

    def reject_outside_enumeration(path: int | str | bytes | os.PathLike[str]):
        if isinstance(path, int):
            inspected = os.fstat(path)
            is_outside = (inspected.st_dev, inspected.st_ino) == (
                outside_stat.st_dev,
                outside_stat.st_ino,
            )
        else:
            is_outside = Path(os.fsdecode(path)).resolve() == outside
        if is_outside:
            pytest.fail("symlinked root was enumerated")
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", reject_outside_enumeration)

    with pytest.raises(CandidateBoundaryError):
        capture_candidate(
            root=root,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("app.py",),
        )


@pytest.mark.parametrize("outside_exists", [False, True])
def test_capture_candidate_does_not_probe_swapped_root_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outside_exists: bool
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    (root / "app.py").write_text("inside\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    if outside_exists:
        (outside / "app.py").write_text("outside\n", encoding="utf-8")
    original_scandir = os.scandir
    original_is_file = Path.is_file
    swapped = False
    outside_probes = 0

    @contextmanager
    def swap_after_enumeration(
        directory_fd: int,
    ) -> Iterator[Iterator[os.DirEntry[str]]]:
        nonlocal swapped
        with original_scandir(directory_fd) as entries:
            yield entries
        if not swapped:
            root.rename(tmp_path / "original")
            root.symlink_to(outside, target_is_directory=True)
            swapped = True

    def record_outside_probe(path: Path) -> bool:
        nonlocal outside_probes
        if path == root / "app.py" and root.is_symlink():
            outside_probes += 1
        return original_is_file(path)

    monkeypatch.setattr(os, "scandir", swap_after_enumeration)
    monkeypatch.setattr(Path, "is_file", record_outside_probe)

    with pytest.raises(
        CandidateBoundaryError, match="candidate file is missing or unsafe: app.py"
    ):
        capture_candidate(
            root=root,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("app.py",),
        )

    assert swapped and outside_probes == 0


def test_capture_candidate_limits_reads_to_remaining_aggregate_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_bytes(b"x" * 9)
    original_read = os.read
    requested_sizes: list[int] = []

    def record_read(fd: int, size: int) -> bytes:
        requested_sizes.append(size)
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", record_read)

    with pytest.raises(CandidateBoundaryError, match="aggregate"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("app.py",),
            max_file_bytes=1024 * 1024,
            max_total_bytes=1,
        )

    assert requested_sizes and max(requested_sizes) <= 2


def test_capture_candidate_bounds_empty_directory_enumeration(tmp_path: Path) -> None:
    (tmp_path / "app.py").touch()
    for index in range(1025):
        (tmp_path / f"directory-{index:04}").mkdir()

    with pytest.raises(CandidateBoundaryError, match="path count"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("app.py",),
        )


def test_capture_candidate_deep_chain_uses_linear_open_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    directory = root
    depth = 256
    for _ in range(depth):
        directory /= "d"
        directory.mkdir()
    (directory / "leaf.txt").write_bytes(b"leaf\n")
    leaf_path = "/".join(("d",) * depth + ("leaf.txt",))

    original_open = os.open
    open_count = 0

    def count_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal open_count
        open_count += 1
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", count_open)
    snapshot = capture_candidate(
        root=root,
        base_tree="0" * 40,
        observed_paths=(leaf_path,),
        tree_oid="1" * 40,
        staged_paths=(leaf_path,),
    )
    capture_open_count = open_count
    monkeypatch.setattr(os, "open", original_open)

    assert snapshot.changed_paths == (leaf_path,)
    assert read_candidate_file(snapshot, leaf_path) == b"leaf\n"
    assert capture_open_count <= 8 * (depth + 1)


def test_capture_candidate_deep_comb_uses_bounded_open_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    directory = root
    paths: list[str] = []
    for _ in range(64):
        branch = directory / "branch"
        branch.mkdir()
        (branch / "leaf.txt").write_bytes(b"leaf\n")
        paths.append((branch / "leaf.txt").relative_to(root).as_posix())
        directory /= "next"
        directory.mkdir()
    (directory / "leaf.txt").write_bytes(b"leaf\n")
    paths.append((directory / "leaf.txt").relative_to(root).as_posix())
    observed_paths = tuple(sorted(paths))

    original_open = os.open
    open_count = 0

    def count_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal open_count
        open_count += 1
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", count_open)
    snapshot = capture_candidate(
        root=root,
        base_tree="0" * 40,
        observed_paths=observed_paths,
        tree_oid="1" * 40,
        staged_paths=observed_paths,
    )
    capture_open_count = open_count
    monkeypatch.setattr(os, "open", original_open)

    assert snapshot.changed_paths == observed_paths
    assert read_candidate_file(snapshot, paths[0]) == b"leaf\n"
    assert read_candidate_file(snapshot, paths[-1]) == b"leaf\n"
    assert capture_open_count <= 3500


def test_capture_candidate_branching_walk_respects_low_fd_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    directory = root
    paths: list[str] = []
    for _ in range(24):
        branch = directory / "branch"
        branch.mkdir()
        (branch / "leaf.txt").write_bytes(b"leaf\n")
        paths.append((branch / "leaf.txt").relative_to(root).as_posix())
        directory /= "next"
        directory.mkdir()
    observed_paths = tuple(sorted(paths))

    original_getrlimit = resource.getrlimit
    original_open = os.open
    original_dup = os.dup
    original_close = os.close
    live: set[int] = set()
    peak_live = 0

    def low_limit(limit: int) -> tuple[int, int]:
        if limit == resource.RLIMIT_NOFILE:
            return (12, 12)
        return original_getrlimit(limit)

    def track_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal peak_live
        if len(live) >= 12:
            raise OSError(errno.EMFILE, "simulated descriptor limit")
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        live.add(fd)
        peak_live = max(peak_live, len(live))
        return fd

    def track_dup(fd: int) -> int:
        nonlocal peak_live
        if len(live) >= 12:
            raise OSError(errno.EMFILE, "simulated descriptor limit")
        duplicated = original_dup(fd)
        live.add(duplicated)
        peak_live = max(peak_live, len(live))
        return duplicated

    def track_close(fd: int) -> None:
        original_close(fd)
        live.discard(fd)

    monkeypatch.setattr(resource, "getrlimit", low_limit)
    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    monkeypatch.setattr(os, "close", track_close)
    snapshot = capture_candidate(
        root=root,
        base_tree="0" * 40,
        observed_paths=observed_paths,
        tree_oid="1" * 40,
        staged_paths=observed_paths,
    )
    monkeypatch.setattr(os, "open", original_open)
    monkeypatch.setattr(os, "dup", original_dup)
    monkeypatch.setattr(os, "close", original_close)

    assert snapshot.changed_paths == observed_paths
    assert read_candidate_file(snapshot, paths[-1]) == b"leaf\n"
    assert peak_live <= 12
    assert live == set()


@pytest.mark.parametrize("failure_site", ("open", "scandir"))
def test_capture_candidate_closes_descriptors_after_nested_directory_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    root = tmp_path / "candidate"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_bytes(b"leaf\n")
    nested_stat = nested.stat()
    original_open = os.open
    original_dup = os.dup
    original_close = os.close
    original_scandir = os.scandir
    live: set[int] = set()

    def track_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if failure_site == "open" and path == "b" and dir_fd is not None:
            raise OSError(errno.EIO, "simulated nested open failure")
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        live.add(fd)
        return fd

    def track_dup(fd: int) -> int:
        duplicated = original_dup(fd)
        live.add(duplicated)
        return duplicated

    def track_close(fd: int) -> None:
        original_close(fd)
        live.discard(fd)

    def fail_nested_scandir(path: int | str | bytes | os.PathLike[str]):
        if isinstance(path, int):
            scanned = os.fstat(path)
            if failure_site == "scandir" and (
                scanned.st_dev,
                scanned.st_ino,
            ) == (nested_stat.st_dev, nested_stat.st_ino):
                raise OSError(errno.EIO, "simulated nested scan failure")
        return original_scandir(path)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "dup", track_dup)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "scandir", fail_nested_scandir)

    with pytest.raises(
        CandidateBoundaryError, match="candidate root is missing or unsafe"
    ):
        capture_candidate(
            root=root,
            base_tree="0" * 40,
            observed_paths=("a/b/leaf.txt",),
            tree_oid="1" * 40,
            staged_paths=("a/b/leaf.txt",),
        )

    assert live == set()


def test_capture_candidate_rejects_intermediate_directory_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "candidate"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (nested / "leaf.txt").write_bytes(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_leaf = outside / "leaf.txt"
    outside_leaf.write_bytes(b"outside\n")
    outside_stat = outside.stat()
    outside_leaf_stat = outside_leaf.stat()
    original_open = os.open
    original_scandir = os.scandir
    original_read = os.read
    swapped = False

    def swap_before_child_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "b" and dir_fd is not None and not swapped:
            nested.rename(tmp_path / "original")
            nested.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def reject_outside_scan(path: int | str | bytes | os.PathLike[str]):
        if isinstance(path, int):
            inspected = os.fstat(path)
            is_outside = (inspected.st_dev, inspected.st_ino) == (
                outside_stat.st_dev,
                outside_stat.st_ino,
            )
        else:
            is_outside = Path(os.fsdecode(path)).resolve() == outside
        if is_outside:
            pytest.fail("outside directory was enumerated")
        return original_scandir(path)

    def reject_outside_read(fd: int, size: int) -> bytes:
        inspected = os.fstat(fd)
        if (inspected.st_dev, inspected.st_ino) == (
            outside_leaf_stat.st_dev,
            outside_leaf_stat.st_ino,
        ):
            pytest.fail("outside file was read")
        return original_read(fd, size)

    monkeypatch.setattr(os, "open", swap_before_child_open)
    monkeypatch.setattr(os, "scandir", reject_outside_scan)
    monkeypatch.setattr(os, "read", reject_outside_read)

    with pytest.raises(
        CandidateBoundaryError, match="candidate root is missing or unsafe"
    ):
        capture_candidate(
            root=root,
            base_tree="0" * 40,
            observed_paths=("a/b/leaf.txt",),
            tree_oid="1" * 40,
            staged_paths=("a/b/leaf.txt",),
        )

    assert swapped


def test_capture_candidate_rejects_mismatched_staged_paths(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(CandidateBoundaryError, match="changed paths"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("other.py",),
        )


def test_capture_candidate_rejects_symlink_swap_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "candidate"
    root.mkdir()
    candidate = root / "app.py"
    candidate.write_text("safe\n", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside root\n", encoding="utf-8")
    original_open = os.open
    swapped = False

    def swap_on_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "app.py" and dir_fd is not None and not swapped:
            candidate.unlink()
            candidate.symlink_to(secret)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swap_on_open)

    with pytest.raises(CandidateBoundaryError):
        capture_candidate(
            root=root,
            base_tree="0" * 40,
            observed_paths=("app.py",),
            tree_oid="1" * 40,
            staged_paths=("app.py",),
        )

    assert swapped


def test_capture_candidate_rejects_oversize_without_unbounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "large.bin").write_bytes(b"x" * 9)

    def reject_read_bytes(_path: Path) -> bytes:
        pytest.fail("unbounded Path.read_bytes was called")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    with pytest.raises(CandidateBoundaryError, match="oversize"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("large.bin",),
            tree_oid="1" * 40,
            staged_paths=("large.bin",),
            max_file_bytes=8,
        )


def test_capture_candidate_rejects_aggregate_oversize(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"abcd")
    (tmp_path / "b.txt").write_bytes(b"efgh")

    with pytest.raises(CandidateBoundaryError, match="aggregate"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("a.txt", "b.txt"),
            tree_oid="1" * 40,
            staged_paths=("a.txt", "b.txt"),
            max_file_bytes=4,
            max_total_bytes=7,
        )


def test_capture_candidate_rejects_too_many_paths(tmp_path: Path) -> None:
    paths = tuple(f"{index:04}.txt" for index in range(1025))
    for path in paths:
        (tmp_path / path).touch()

    with pytest.raises(CandidateBoundaryError, match="path count"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=paths,
            tree_oid="1" * 40,
            staged_paths=paths,
        )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda root: (root / "src" / "app.py").write_text("value = 2\n"), "drift"),
        (lambda root: (root / "extra.txt").write_text("unexpected\n"), "changed paths"),
    ],
)
def test_capture_candidate_rejects_worktree_drift(
    tmp_path: Path, mutator, message: str
) -> None:
    """A snapshot cannot be read after its controller-owned tree changes."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = capture_candidate(
        root=tmp_path,
        base_tree="0" * 40,
        observed_paths=("src/app.py",),
        tree_oid="1" * 40,
        staged_paths=("src/app.py",),
    )

    mutator(tmp_path)

    with pytest.raises(CandidateBoundaryError, match=message):
        read_candidate_file(snapshot, "src/app.py")


def test_capture_candidate_rejects_unbounded_or_malformed_requests(
    tmp_path: Path,
) -> None:
    """Malformed paths and oversized files never cross the read boundary."""
    (tmp_path / "large.bin").write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(CandidateBoundaryError, match="oversize"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("large.bin",),
            tree_oid="1" * 40,
            staged_paths=("large.bin",),
            max_file_bytes=1024 * 1024,
        )

    with pytest.raises(CandidateBoundaryError, match="path"):
        capture_candidate(
            root=tmp_path,
            base_tree="0" * 40,
            observed_paths=("../secret",),
            tree_oid="1" * 40,
            staged_paths=("../secret",),
        )
