# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Host-bound documentation replacements through injected descriptor operations."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .domain import WorkflowPhase

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PatchReason = Literal[
    "invalid_request", "identity_changed", "patch_refused", "io_failed"
]


@dataclass(frozen=True)
class DocumentationPatchFileGrant:
    """One host-reviewed path, pair of digests, and measured byte lengths."""

    path: str
    base_sha256: str
    replacement_sha256: str
    base_size_bytes: int
    replacement_size_bytes: int


@dataclass(frozen=True)
class DocumentationPatchGrant:
    """Private host authority for complete, disjoint replacement groups."""

    project_id: int
    issue_iid: int
    checkout: Path
    source_branch: str
    source_sha: str
    phase: WorkflowPhase
    ledger_revision: int
    groups: tuple[tuple[DocumentationPatchFileGrant, ...], ...]


class _PatchBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DocumentationPatch(_PatchBoundary):
    """Worker-proposed replacement without host authority fields."""

    path: str
    base_sha256: Digest
    replacement_text: str


class DocumentationPatchRequest(_PatchBoundary):
    """Closed preview-by-default request for a complete grant."""

    patches: tuple[DocumentationPatch, ...]
    apply: bool = False


class DocumentationPatchMember(_PatchBoundary):
    """Public fact about one successfully replaced approved member."""

    path: str
    sha256: Digest


class DocumentationPatchResult(_PatchBoundary):
    """Safe result with no proposed contents or private host identity."""

    outcome: Literal["preview", "applied", "refused", "failed_partial"]
    changed: tuple[DocumentationPatchMember, ...] = ()
    reason: PatchReason | None = None
    quarantined: bool = False


class OpenDescriptor(Protocol):
    """Open one entry relative to an owned descriptor."""

    def __call__(
        self,
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int: ...


class StatEntry(Protocol):
    """Inspect a named entry without following a symlink."""

    def __call__(
        self,
        path: str | Path,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = False,
    ) -> os.stat_result: ...


class ReplaceEntry(Protocol):
    """Replace one owned temporary entry relative to pinned parents."""

    def __call__(
        self, source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int
    ) -> None: ...


class UnlinkEntry(Protocol):
    """Remove one owned temporary entry."""

    def __call__(self, path: str, *, dir_fd: int) -> None: ...


@dataclass(frozen=True)
class DocumentationPatchFilesystem:
    """Finite filesystem primitives supplied by the host composer."""

    open: OpenDescriptor
    stat: StatEntry
    fstat: Callable[[int], os.stat_result]
    read: Callable[[int, int], bytes]
    write: Callable[[int, bytes], int]
    fchmod: Callable[[int, int], None]
    replace: ReplaceEntry
    unlink: UnlinkEntry
    close: Callable[[int], None]


def _executable_regions(text: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if any(marker in text for marker in ("{{", "}}", "{%", "%}", "{#", "#}")):
        return None
    blocks: list[str] = []
    indented: list[str] = []
    current: list[str] | None = None
    fence = ""
    length = 0
    for line in text.splitlines(keepends=True):
        if line.startswith((" ", "\t")):
            indented.append(line)
        stripped = line.lstrip(" ")
        if stripped.startswith(">") and re.search(r"`{3,}|~{3,}", stripped):
            return None
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*?)(?:\r?\n)?$", line)
        if current is None:
            if marker:
                fence, length = marker[1][0], len(marker[1])
                if fence == "`" and "`" in marker[2]:
                    return None
                current = [line]
        else:
            current.append(line)
            if marker:
                if (
                    marker[1][0] != fence
                    or len(marker[1]) < length
                    or marker[2].strip()
                ):
                    return None
                blocks.append("".join(current))
                current = None
    if current is not None:
        return None
    return tuple(blocks), tuple(indented)


def documentation_patch_preserves_executable_regions(
    original: str, replacement: str
) -> bool:
    """Preserve exact ordered fences and every whitespace-indented line."""
    before = _executable_regions(original)
    return before is not None and before == _executable_regions(replacement)


def _eligible_path(path: str) -> bool:
    parts = path.split("/")
    return (
        bool(parts)
        and all(part not in {"", ".", "..", "AGENTS.md", ".agents"} for part in parts)
        and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in path)
        and "\\" not in path
        and not any(marker in path for marker in ("{{", "}}", "{%", "%}", "{#", "#}"))
        and path.endswith(".md")
        and (
            path == "README.md"
            or parts[0] == "docs"
            or parts[:2] == ["template", "docs"]
        )
    )


class _PatchRefusal(ValueError):
    def __init__(self, reason: PatchReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _PinnedDirectory:
    descriptor: int
    parent: int | None
    name: str
    identity: tuple[int, int]


@dataclass(frozen=True)
class _Member:
    grant: DocumentationPatchFileGrant
    parent: int
    name: str
    descriptor: int
    observed: os.stat_result
    replacement: bytes


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_nlink,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_nlink,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _read_bound(
    filesystem: DocumentationPatchFilesystem, descriptor: int, size: int
) -> bytes:
    result = bytearray()
    while len(result) < size + 1:
        part = filesystem.read(descriptor, size + 1 - len(result))
        if not part:
            break
        result.extend(part)
    if len(result) != size:
        raise _PatchRefusal("patch_refused")
    return bytes(result)


def _regular(observed: os.stat_result, size: int) -> bool:
    return (
        stat.S_ISREG(observed.st_mode)
        and not observed.st_mode & 0o111
        and observed.st_nlink == 1
        and observed.st_size == size
    )


def _grant_patches(
    grant: DocumentationPatchGrant, request: DocumentationPatchRequest
) -> tuple[tuple[DocumentationPatchFileGrant, DocumentationPatch, bytes], ...]:
    members = tuple(member for group in grant.groups for member in group)
    if not members or any(not group for group in grant.groups):
        raise _PatchRefusal("patch_refused")
    paths = tuple(member.path for member in members)
    proposed = {patch.path: patch for patch in request.patches}
    if (
        len(set(paths)) != len(paths)
        or len(proposed) != len(request.patches)
        or set(paths) != set(proposed)
    ):
        raise _PatchRefusal("patch_refused")
    result: list[tuple[DocumentationPatchFileGrant, DocumentationPatch, bytes]] = []
    for group in grant.groups:
        if (
            len(
                {
                    (
                        entry.base_sha256,
                        entry.replacement_sha256,
                        entry.base_size_bytes,
                        entry.replacement_size_bytes,
                    )
                    for entry in group
                }
            )
            != 1
        ):
            raise _PatchRefusal("patch_refused")
    for member in members:
        patch = proposed[member.path]
        content = patch.replacement_text.encode("utf-8")
        if (
            not _eligible_path(member.path)
            or re.fullmatch(r"[0-9a-f]{64}", member.base_sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", member.replacement_sha256) is None
            or type(member.base_size_bytes) is not int
            or member.base_size_bytes < 0
            or type(member.replacement_size_bytes) is not int
            or member.replacement_size_bytes < 0
            or patch.base_sha256 != member.base_sha256
            or len(content) != member.replacement_size_bytes
            or hashlib.sha256(content).hexdigest() != member.replacement_sha256
        ):
            raise _PatchRefusal("patch_refused")
        result.append((member, patch, content))
    return tuple(result)


class _PatchOperation:
    def __init__(
        self,
        filesystem: DocumentationPatchFilesystem,
        identity_check: Callable[[], bool],
    ) -> None:
        self.fs = filesystem
        self.identity_check = identity_check
        self.descriptors: list[int] = []
        self.directories: list[_PinnedDirectory] = []
        self.temporaries: list[tuple[int, str, tuple[int, int]]] = []
        self.changed: list[DocumentationPatchMember] = []

    def _identity(self) -> None:
        if not self.identity_check():
            raise _PatchRefusal("identity_changed")
        for directory in self.directories:
            observed = self.fs.fstat(directory.descriptor)
            named = self.fs.stat(
                directory.name, dir_fd=directory.parent, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(observed.st_mode)
                or (observed.st_dev, observed.st_ino) != directory.identity
                or (named.st_dev, named.st_ino) != directory.identity
            ):
                raise _PatchRefusal("patch_refused")

    def _directory(self, name: str | Path, parent: int | None) -> int:
        descriptor = self.fs.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        self.descriptors.append(descriptor)
        observed = self.fs.fstat(descriptor)
        named = self.fs.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode) or (observed.st_dev, observed.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise _PatchRefusal("patch_refused")
        self.directories.append(
            _PinnedDirectory(
                descriptor, parent, str(name), (observed.st_dev, observed.st_ino)
            )
        )
        return descriptor

    def _open_root(self, checkout: Path) -> int:
        if not checkout.is_absolute() or ".." in checkout.parts:
            raise _PatchRefusal("patch_refused")
        descriptor = self._directory(checkout.anchor, None)
        for component in checkout.parts[1:]:
            descriptor = self._directory(component, descriptor)
        return descriptor

    def _member(
        self,
        root: int,
        entry: DocumentationPatchFileGrant,
        patch: DocumentationPatch,
        replacement: bytes,
    ) -> _Member:
        parent = root
        for component in entry.path.split("/")[:-1]:
            parent = self._directory(component, parent)
        name = entry.path.split("/")[-1]
        descriptor = self.fs.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        self.descriptors.append(descriptor)
        observed = self.fs.fstat(descriptor)
        if not _regular(observed, entry.base_size_bytes):
            raise _PatchRefusal("patch_refused")
        content = _read_bound(self.fs, descriptor, entry.base_size_bytes)
        self._identity()
        if (
            not _same_file(observed, self.fs.fstat(descriptor))
            or not _same_file(
                observed, self.fs.stat(name, dir_fd=parent, follow_symlinks=False)
            )
            or hashlib.sha256(content).hexdigest() != entry.base_sha256
            or not documentation_patch_preserves_executable_regions(
                content.decode("utf-8"), patch.replacement_text
            )
        ):
            raise _PatchRefusal("patch_refused")
        return _Member(entry, parent, name, descriptor, observed, replacement)

    def _verify_member(self, member: _Member) -> None:
        self._identity()
        if not _same_file(
            member.observed, self.fs.fstat(member.descriptor)
        ) or not _same_file(
            member.observed,
            self.fs.stat(member.name, dir_fd=member.parent, follow_symlinks=False),
        ):
            raise _PatchRefusal("patch_refused")
        descriptor = self.fs.open(
            member.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=member.parent,
        )
        self.descriptors.append(descriptor)
        observed = self.fs.fstat(descriptor)
        content = _read_bound(self.fs, descriptor, member.grant.base_size_bytes)
        if (
            not _same_file(member.observed, observed)
            or not _same_file(observed, self.fs.fstat(descriptor))
            or hashlib.sha256(content).hexdigest() != member.grant.base_sha256
        ):
            raise _PatchRefusal("patch_refused")
        self._identity()

    def _replacement_matches(
        self, member: _Member, name: str, expected: os.stat_result
    ) -> bool:
        descriptor = self.fs.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=member.parent
        )
        self.descriptors.append(descriptor)
        observed = self.fs.fstat(descriptor)
        if not _regular(observed, member.grant.replacement_size_bytes) or (
            observed.st_dev,
            observed.st_ino,
            observed.st_mode,
        ) != (expected.st_dev, expected.st_ino, expected.st_mode):
            return False
        content = _read_bound(self.fs, descriptor, member.grant.replacement_size_bytes)
        named = self.fs.stat(name, dir_fd=member.parent, follow_symlinks=False)
        return (
            _same_file(observed, self.fs.fstat(descriptor))
            and _same_file(observed, named)
            and hashlib.sha256(content).hexdigest() == member.grant.replacement_sha256
        )

    def execute(
        self, grant: DocumentationPatchGrant, request: DocumentationPatchRequest
    ) -> DocumentationPatchResult:
        self._identity()
        proposals = _grant_patches(grant, request)
        root = self._open_root(grant.checkout)
        members = tuple(self._member(root, *proposal) for proposal in proposals)
        for member in members:
            self._verify_member(member)
        if not request.apply:
            return DocumentationPatchResult(outcome="preview")
        prepared: list[tuple[_Member, str, int]] = []
        for member in members:
            self._identity()
            name = f".documentation-patch-{secrets.token_hex(16)}"
            descriptor = self.fs.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=member.parent,
            )
            self.descriptors.append(descriptor)
            created = self.fs.fstat(descriptor)
            temporary_identity = (created.st_dev, created.st_ino)
            self.temporaries.append((member.parent, name, temporary_identity))
            self.fs.fchmod(descriptor, stat.S_IMODE(member.observed.st_mode))
            if self.fs.write(descriptor, member.replacement) != len(member.replacement):
                raise _PatchRefusal("io_failed")
            prepared.append((member, name, descriptor))
        for member, name, descriptor in prepared:
            self._verify_member(member)
            temporary = self.fs.fstat(descriptor)
            named = self.fs.stat(name, dir_fd=member.parent, follow_symlinks=False)
            if (
                not _regular(temporary, len(member.replacement))
                or not _same_file(temporary, named)
                or stat.S_IMODE(temporary.st_mode)
                != stat.S_IMODE(member.observed.st_mode)
                or not self._replacement_matches(member, name, temporary)
            ):
                raise _PatchRefusal("patch_refused")
            self._identity()
            try:
                self.fs.replace(
                    name,
                    member.name,
                    src_dir_fd=member.parent,
                    dst_dir_fd=member.parent,
                )
            except OSError:
                # A primitive may report failure after its rename took effect.
                if self._replacement_matches(member, member.name, temporary):
                    self.changed.append(
                        DocumentationPatchMember(
                            path=member.grant.path,
                            sha256=member.grant.replacement_sha256,
                        )
                    )
                    self.temporaries.remove(
                        (member.parent, name, (temporary.st_dev, temporary.st_ino))
                    )
                raise
            self.temporaries.remove(
                (member.parent, name, (temporary.st_dev, temporary.st_ino))
            )
            self.changed.append(
                DocumentationPatchMember(
                    path=member.grant.path, sha256=member.grant.replacement_sha256
                )
            )
            published = self.fs.stat(
                member.name, dir_fd=member.parent, follow_symlinks=False
            )
            if (
                temporary.st_dev,
                temporary.st_ino,
                temporary.st_mode,
                temporary.st_nlink,
                temporary.st_size,
            ) != (
                published.st_dev,
                published.st_ino,
                published.st_mode,
                published.st_nlink,
                published.st_size,
            ):
                raise _PatchRefusal("patch_refused")
            if not self._replacement_matches(member, member.name, temporary):
                raise _PatchRefusal("patch_refused")
            self._identity()
        return DocumentationPatchResult(outcome="applied", changed=tuple(self.changed))

    def cleanup(self) -> bool:
        failed = False
        for parent, name, identity in reversed(self.temporaries):
            try:
                observed = self.fs.stat(name, dir_fd=parent, follow_symlinks=False)
                if (observed.st_dev, observed.st_ino) != identity or not stat.S_ISREG(
                    observed.st_mode
                ):
                    failed = True
                    continue
                self.fs.unlink(name, dir_fd=parent)
            except OSError:
                failed = True
        for descriptor in reversed(self.descriptors):
            try:
                self.fs.close(descriptor)
            except OSError:
                failed = True
        return failed


def apply_documentation_patch(
    grant: DocumentationPatchGrant,
    request: DocumentationPatchRequest,
    *,
    identity_check: Callable[[], bool],
    filesystem: DocumentationPatchFilesystem,
) -> DocumentationPatchResult:
    """Preview or replace the complete host grant; never roll back partial writes."""
    operation = _PatchOperation(filesystem, identity_check)
    try:
        validated = DocumentationPatchRequest.model_validate_json(
            request.model_dump_json()
        )
        result = operation.execute(grant, validated)
    except _PatchRefusal as error:
        result = DocumentationPatchResult(
            outcome="failed_partial" if operation.changed else "refused",
            changed=tuple(operation.changed),
            reason=error.reason,
            quarantined=bool(operation.changed),
        )
    except (ValidationError, UnicodeError, TypeError, ValueError):
        result = DocumentationPatchResult(outcome="refused", reason="invalid_request")
    except OSError as error:
        reason: PatchReason = (
            "patch_refused"
            if isinstance(error, (FileNotFoundError, NotADirectoryError))
            or error.errno == errno.ELOOP
            else "io_failed"
        )
        result = DocumentationPatchResult(
            outcome="failed_partial" if operation.changed else "refused",
            changed=tuple(operation.changed),
            reason=reason,
            quarantined=bool(operation.changed),
        )
    finally:
        cleanup_failed = operation.cleanup()
    if cleanup_failed:
        return DocumentationPatchResult(
            outcome="failed_partial" if operation.changed else "refused",
            changed=tuple(operation.changed),
            reason="io_failed",
            quarantined=True,
        )
    return result
