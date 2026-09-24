# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Private, bounded, no-replace objects for interactive review context."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_OBJECT_BYTES = 1_048_576


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Open each absolute directory component without following links."""
    if not path.is_absolute():
        raise ValueError("object path must be absolute")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise ValueError("object path traversal is forbidden")
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_bounded_regular(path: Path, *, limit: int = _MAX_OBJECT_BYTES) -> bytes | None:
    """Read one ordinary file without following its path components."""
    try:
        parent = _open_directory(path.parent)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("object is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                return handle.read(limit + 1)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


@dataclass(frozen=True)
class AtomicObjectStore:
    """Private, bounded, no-replace objects scoped by an opaque session prefix."""

    root: Path

    def _path(self, reference: str) -> Path:
        parts = reference.split("/")
        if not parts or any(_PART.fullmatch(part) is None for part in parts):
            raise ValueError("invalid object reference")
        return self.root.joinpath(*parts)

    def read(self, reference: str) -> bytes | None:
        """Read one bounded object after no-follow path checks."""
        return read_bounded_regular(self._path(reference))

    def write_once(self, reference: str, payload: bytes) -> None:
        """Publish complete bytes atomically without replacing an object."""
        if len(payload) > _MAX_OBJECT_BYTES:
            raise ValueError("object exceeds bounded size")
        self._publish(reference, payload)

    def _publish(self, reference: str, payload: bytes) -> None:
        path = self._path(reference)
        root = _open_directory(self.root, create=True)
        try:
            if os.fstat(root).st_mode & 0o077:
                raise ValueError("object store is not private")
            parent = _open_directory(path.parent, create=True)
            try:
                if os.fstat(parent).st_mode & 0o077:
                    raise ValueError("object directory is not private")
                temporary = f".tmp-{os.urandom(16).hex()}"
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root,
                )
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.link(
                        temporary,
                        path.name,
                        src_dir_fd=root,
                        dst_dir_fd=parent,
                        follow_symlinks=False,
                    )
                    os.fsync(parent)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=root)
                    except FileNotFoundError:
                        pass
                    os.fsync(root)
            finally:
                os.close(parent)
        finally:
            os.close(root)

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        """Inventory one bounded session subtree, never old sessions."""
        try:
            root = _open_directory(self.root)
        except FileNotFoundError:
            return ()
        try:
            if os.fstat(root).st_mode & 0o077:
                raise ValueError("object store is not private")
            try:
                subtree = _open_directory(self._path(prefix))
            except FileNotFoundError:
                return ()
            try:
                objects: list[str] = []
                for parent, directories, files, current in os.fwalk(
                    ".", dir_fd=subtree, follow_symlinks=False
                ):
                    for name in (*directories, *files):
                        mode = os.stat(
                            name, dir_fd=current, follow_symlinks=False
                        ).st_mode
                        if stat.S_ISLNK(mode) or not (
                            stat.S_ISDIR(mode) or stat.S_ISREG(mode)
                        ):
                            raise ValueError("object store contains an unsafe entry")
                    for name in files:
                        suffix = Path(parent, name).as_posix().removeprefix("./")
                        objects.append(f"{prefix}/{suffix}")
                        if len(objects) > 10_000:
                            raise ValueError("session inventory exceeds bounded size")
                return tuple(objects)
            finally:
                os.close(subtree)
        finally:
            os.close(root)
