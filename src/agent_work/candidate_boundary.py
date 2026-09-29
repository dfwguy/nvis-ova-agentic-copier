# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Bounded, identity-bound reads of a controller-owned candidate snapshot."""

import os
import resource
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath


class CandidateBoundaryError(ValueError):
    """Raised when a candidate cannot be captured or read safely."""


_MAX_PATHS = 1024
_DEFAULT_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
GIT_READ_MAX_PATHS = 128
GIT_READ_MAX_FILE_BYTES = 2 * 1024 * 1024
GIT_READ_MAX_TOTAL_BYTES = 16 * 1024 * 1024
GIT_READ_MAX_CHUNK_BYTES = 128 * 1024


def validate_git_read_paths(paths: tuple[str, ...]) -> None:
    """Require canonical bounded changed paths before reading Git objects."""
    if (
        not paths
        or len(paths) > GIT_READ_MAX_PATHS
        or paths != tuple(sorted(set(paths)))
        or any(
            not path
            or str(PurePosixPath(path)) != path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\n" in path
            or "\r" in path
            for path in paths
        )
    ):
        raise CandidateBoundaryError("candidate changed paths are invalid")


def validate_git_read_size(size: int, total: int, *, per_blob: bool = True) -> int:
    """Enforce the shared per-blob and aggregate read budgets."""
    if (
        size < 0
        or (per_blob and size > GIT_READ_MAX_FILE_BYTES)
        or total + size > GIT_READ_MAX_TOTAL_BYTES
    ):
        raise CandidateBoundaryError("candidate content is oversize or binary")
    return total + size


@dataclass(frozen=True)
class CandidateSnapshot:
    """Immutable candidate content and the identity it was captured from."""

    root: Path
    base_tree: str
    tree_oid: str
    changed_paths: tuple[str, ...]
    _files: tuple[tuple[str, bytes, str], ...]
    _observed_digest: str
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES


def _paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    if len(paths) > _MAX_PATHS:
        raise CandidateBoundaryError("path count exceeds limit")
    normalized = tuple(sorted(set(paths)))
    if not normalized or any(
        not path
        or str(PurePosixPath(path)) != path
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        for path in normalized
    ):
        raise CandidateBoundaryError("path is malformed")
    return normalized


def _validate_identity(value: str, name: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise CandidateBoundaryError(f"{name} is malformed")


def _read_file(root: Path, path: str, limit: int, limit_error: str) -> bytes:
    """Read a regular file without following any component symlink."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        directory_fd = os.open(root, directory_flags)
        try:
            parts = PurePosixPath(path).parts
            for part in parts[:-1]:
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
            try:
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    raise CandidateBoundaryError(f"candidate file is unsafe: {path}")
                content = bytearray()
                while chunk := os.read(
                    file_fd, min(_READ_CHUNK_BYTES, limit - len(content) + 1)
                ):
                    content.extend(chunk)
                    if len(content) > limit:
                        raise CandidateBoundaryError(limit_error)
                return bytes(content)
            finally:
                os.close(file_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise CandidateBoundaryError(
            f"candidate file is missing or unsafe: {path}"
        ) from exc


def _read_files(
    root: Path, paths: tuple[str, ...], max_file_bytes: int, max_total_bytes: int
) -> tuple[tuple[str, bytes, str], ...]:
    files: list[tuple[str, bytes, str]] = []
    total_bytes = 0
    for path in paths:
        remaining_total = max_total_bytes - total_bytes
        limit = min(max_file_bytes, remaining_total)
        limit_error = (
            "candidate aggregate is oversize"
            if remaining_total < max_file_bytes
            else "candidate file is oversize"
        )
        content = _read_file(root, path, limit, limit_error)
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            raise CandidateBoundaryError("candidate aggregate is oversize")
        files.append((path, content, sha256(content).hexdigest()))
    return tuple(files)


def _directory_fd(root_fd: int, parts: tuple[str, ...]) -> int:
    """Open a directory below an already verified root descriptor."""
    directory_fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
    except OSError:
        os.close(directory_fd)
        raise
    return directory_fd


def _root_files(root: Path) -> tuple[str, ...]:
    """Enumerate bounded regular files through no-follow directory descriptors."""
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        open_fds = {root_fd}
        try:
            soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            max_open = (
                _MAX_PATHS
                if soft_limit == resource.RLIM_INFINITY
                else max(2, min(_MAX_PATHS, soft_limit - 32))
            )
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            files: list[str] = []
            pending: list[tuple[str, ...]] = []
            ancestors: list[tuple[tuple[str, ...], int, list[str]]] = []
            entry_count = 0
            parts: tuple[str, ...] = ()
            directory_fd = root_fd
            while True:
                children: list[str] = []
                with os.scandir(directory_fd) as entries:
                    for entry in entries:
                        entry_count += 1
                        if entry_count > _MAX_PATHS:
                            raise CandidateBoundaryError("path count exceeds limit")
                        path_parts = (*parts, entry.name)
                        if entry.is_dir(follow_symlinks=False):
                            children.append(entry.name)
                        elif entry.is_file(follow_symlinks=False):
                            files.append("/".join(path_parts))
                        else:
                            raise CandidateBoundaryError(
                                "candidate root contains unsafe entry"
                            )
                if children:
                    child = children.pop()
                    retain_parent = bool(children) and len(open_fds) < max_open
                    if retain_parent:
                        ancestors.append((parts, directory_fd, children))
                    else:
                        pending.extend((*parts, name) for name in children)
                    child_fd = os.open(child, directory_flags, dir_fd=directory_fd)
                    open_fds.add(child_fd)
                    if not retain_parent and directory_fd != root_fd:
                        os.close(directory_fd)
                        open_fds.remove(directory_fd)
                    parts = (*parts, child)
                    directory_fd = child_fd
                    continue
                if directory_fd != root_fd:
                    os.close(directory_fd)
                    open_fds.remove(directory_fd)
                if ancestors:
                    parent_parts, parent_fd, siblings = ancestors[-1]
                    child = siblings.pop()
                    if not siblings:
                        ancestors.pop()
                    child_fd = os.open(child, directory_flags, dir_fd=parent_fd)
                    open_fds.add(child_fd)
                    if not siblings and parent_fd != root_fd:
                        os.close(parent_fd)
                        open_fds.remove(parent_fd)
                    parts = (*parent_parts, child)
                    directory_fd = child_fd
                elif pending:
                    parts = pending.pop()
                    directory_fd = _directory_fd(root_fd, parts)
                    open_fds.add(directory_fd)
                else:
                    return tuple(sorted(files))
        finally:
            for descriptor in open_fds:
                os.close(descriptor)
    except OSError as exc:
        raise CandidateBoundaryError("candidate root is missing or unsafe") from exc


def _digest(files: tuple[tuple[str, bytes, str], ...]) -> str:
    packed = b"".join(
        path.encode() + b"\0" + digest.encode() + b"\0" for path, _, digest in files
    )
    return sha256(packed).hexdigest()


def capture_candidate(
    *,
    root: Path,
    base_tree: str,
    observed_paths: tuple[str, ...],
    tree_oid: str,
    staged_paths: tuple[str, ...],
    max_file_bytes: int = 1024 * 1024,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> CandidateSnapshot:
    """Capture exactly the controller-observed candidate paths."""
    _validate_identity(base_tree, "base tree")
    _validate_identity(tree_oid, "tree")
    if (
        max_file_bytes < 0
        or max_total_bytes < 0
        or _paths(observed_paths) != _paths(staged_paths)
    ):
        raise CandidateBoundaryError("changed paths do not match")
    paths = _paths(observed_paths)
    if _root_files(root) != paths:
        raise CandidateBoundaryError("changed paths do not match candidate root")
    files = _read_files(root, paths, max_file_bytes, max_total_bytes)
    return CandidateSnapshot(
        root=root,
        base_tree=base_tree,
        tree_oid=tree_oid,
        changed_paths=paths,
        _files=files,
        _observed_digest=_digest(files),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )


def read_candidate(snapshot: CandidateSnapshot) -> bytes:
    """Return the serialized immutable snapshot after rechecking its root."""
    current = _read_files(
        snapshot.root,
        snapshot.changed_paths,
        snapshot.max_file_bytes,
        snapshot.max_total_bytes,
    )
    if _root_files(snapshot.root) != snapshot.changed_paths:
        raise CandidateBoundaryError("candidate changed paths drift detected")
    if _digest(current) != snapshot._observed_digest:
        raise CandidateBoundaryError("candidate drift detected")
    return b"".join(
        len(path_bytes).to_bytes(4, "big")
        + path_bytes
        + len(content).to_bytes(8, "big")
        + content
        for path, content, _ in snapshot._files
        for path_bytes in (path.encode(),)
    )


def read_candidate_file(snapshot: CandidateSnapshot, path: str) -> bytes:
    """Read one bounded file only when the complete candidate is unchanged."""
    normalized = _paths((path,))[0]
    read_candidate(snapshot)
    for stored_path, content, _ in snapshot._files:
        if stored_path == normalized:
            return content
    raise CandidateBoundaryError("path is not part of candidate")
