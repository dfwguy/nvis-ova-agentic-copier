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

import os
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
