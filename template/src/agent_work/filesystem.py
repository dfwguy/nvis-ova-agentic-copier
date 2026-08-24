"""Controlled, one-file filesystem intake for normalized agent work."""

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .adapters import AdapterFailure
from .models import FilesystemEnvelope, WorkItem, validate_alias

_DIRECTORIES = (
    "inbox",
    "processing",
    "awaiting-human",
    "done",
    "failed",
    "quarantine",
    ".dedupe",
)
_MAX_BYTES = 262_144


class FileIntakeFailure(AdapterFailure):
    """A safe operational blocker at the filesystem producer boundary."""


@dataclass(frozen=True)
class RepositoryTarget:
    """A trusted checkout selected only by its configured alias."""

    alias: str
    checkout: Path
    host: str
    project_id: int


class InboxOperations(Protocol):
    """The complete injected filesystem boundary for one inbox attempt."""

    def address(self, path: Path, inbox_root: Path) -> tuple[Path, Path]:
        """Resolve and validate the selected producer-addressable item."""

    def validate_layout(self, root: Path, item: Path, checkout: Path) -> None:
        """Validate controlled directories, item safety, and checkout separation."""

    def read_request(self, item: Path) -> bytes:
        """Read at most the configured producer request boundary."""

    def ensure_dedupe(self, root: Path) -> None:
        """Ensure the runner-owned deduplication directory is safe and durable."""

    def reserve_deduplication(self, root: Path, digest: str) -> bool:
        """Create a durable raw-content reservation without replacing another run."""

    def claim(self, item: Path, processing: Path) -> Path:
        """Atomically transfer ownership from inbox to processing."""

    def release_claim(self, claimed: Path) -> None:
        """Durably remove an owned processing claim after finalization."""

    def destination(self, root: Path, directory: str, digest: str, run_id: str) -> Path:
        """Return one terminal bundle location under a validated root."""


@dataclass(frozen=True)
class LocalInboxOperations:
    """Concrete local inbox adapter with every operating-system seam injected."""

    device_id: Callable[[Path], int] = lambda path: path.stat().st_dev
    read_syscall: Callable[[int, int], bytes] = os.read
    write_syscall: Callable[[int, bytes], int] = os.write
    atomic_replace: Callable[[Path, Path], None] = os.replace
    fsync_directory: Callable[[Path], None] = lambda path: _fsync_directory(path)

    def address(self, path: Path, inbox_root: Path) -> tuple[Path, Path]:
        """Resolve one direct producer item beneath an absolute safe inbox root."""
        return _validate_address(path, inbox_root)

    def validate_layout(self, root: Path, item: Path, checkout: Path) -> None:
        """Validate the controlled filesystem topology with injected device checks."""
        _validate_layout(root, item, checkout, self.device_id)

    def read_request(self, item: Path) -> bytes:
        """Read one bounded request through the injected read syscall."""
        return _bounded_read_request(item, _MAX_BYTES + 1, self.read_syscall)

    def ensure_dedupe(self, root: Path) -> None:
        """Create the runner-owned dedupe directory through the local adapter."""
        _ensure_dedupe_directory(root)

    def reserve_deduplication(self, root: Path, digest: str) -> bool:
        """Reserve an exact raw digest through the injected write syscall."""
        return _reserve_deduplication(root, digest, self.write_syscall)

    def claim(self, item: Path, processing: Path) -> Path:
        """Atomically claim one selected inbox item."""
        return _claim(item, processing, self.atomic_replace, self.fsync_directory)

    def release_claim(self, claimed: Path) -> None:
        """Durably release the processing evidence after terminal persistence."""
        _durable_unlink(claimed)

    def destination(self, root: Path, directory: str, digest: str, run_id: str) -> Path:
        """Construct one controlled terminal destination from safe components."""
        return root / directory / f"{digest}-{run_id}"


@dataclass(frozen=True)
class FilesystemDependencies:
    """All impure filesystem intake boundaries, supplied by the CLI."""

    resolve_repository: Callable[[str], RepositoryTarget | None]
    repository_lock: Callable[[RepositoryTarget], AbstractContextManager[None]]
    run_normalized: Callable[[WorkItem, RepositoryTarget, int], Mapping[str, object]]
    finalize_bundle: Callable[..., None]
    new_run_id: Callable[[], str]
    inbox: InboxOperations
    inbox_lock: Callable[[Path], AbstractContextManager[None]] = lambda _root: (
        nullcontext()
    )


@dataclass(frozen=True)
class FileIntakeReport:
    """Serializable outcome of examining or consuming one inbox item."""

    preview: bool
    outcome: str | None = None
    destination: Path | None = None
    duplicate: bool = False
    quarantined: bool = False
    validation_error: str | None = None
    source: str | None = None
    repository: str | None = None
    sha256: str | None = None
    branch: str | None = None
    result: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the stable CLI report representation."""
        return {
            "preview": self.preview,
            "outcome": self.outcome,
            "destination": str(self.destination) if self.destination else None,
            "duplicate": self.duplicate,
            "quarantined": self.quarantined,
            "validation_error": self.validation_error,
            "source": self.source,
            "repository": self.repository,
            "sha256": self.sha256,
            "branch": self.branch,
            "result": dict(self.result) if self.result is not None else None,
        }


@dataclass(frozen=True)
class FileIntake:
    """Validate, claim, run, and terminally bundle one producer request."""

    dependencies: FilesystemDependencies

    def run(
        self,
        *,
        path: Path,
        inbox_root: Path,
        repository: str,
        mode: Literal["publish"],
        apply: bool,
        timeout_seconds: int,
    ) -> FileIntakeReport:
        """Preview or atomically consume one direct child of the trusted inbox."""
        try:
            return self._run(
                path=path,
                inbox_root=inbox_root,
                repository=repository,
                mode=mode,
                apply=apply,
                timeout_seconds=timeout_seconds,
            )
        except FileIntakeFailure:
            raise
        except (ValueError, RuntimeError) as error:
            raise FileIntakeFailure("filesystem intake failed") from error

    def _run(
        self,
        *,
        path: Path,
        inbox_root: Path,
        repository: str,
        mode: Literal["publish"],
        apply: bool,
        timeout_seconds: int,
    ) -> FileIntakeReport:
        """Perform the bounded intake after converting unsafe OS errors."""
        root, item = self.dependencies.inbox.address(path, inbox_root)
        target = self.dependencies.resolve_repository(repository)
        if target is None or target.alias != repository:
            raise FileIntakeFailure("repository is not allowlisted")
        _validate_target(target)
        self.dependencies.inbox.validate_layout(root, item, target.checkout)
        if not apply:
            raw, envelope, error = _read_envelope(
                self.dependencies.inbox.read_request(item)
            )
            digest = hashlib.sha256(raw).hexdigest()
            return _report(
                preview=True,
                outcome="preview",
                envelope=envelope,
                repository=repository,
                digest=digest,
                validation_error=error or _repository_error(envelope, repository),
            )
        run_id = self.dependencies.new_run_id()
        with (
            self.dependencies.inbox_lock(root),
            self.dependencies.repository_lock(target),
        ):
            self.dependencies.inbox.validate_layout(root, item, target.checkout)
            claimed = self.dependencies.inbox.claim(
                item, root / "processing" / item.name
            )
            raw, envelope, error = _read_envelope(
                self.dependencies.inbox.read_request(claimed)
            )
            digest = hashlib.sha256(raw).hexdigest()
            validation_error = error or _repository_error(envelope, repository)
            if validation_error is not None:
                return self._finalize(
                    root=root,
                    directory="quarantine",
                    digest=digest,
                    run_id=run_id,
                    request=raw,
                    intake={
                        "outcome": "quarantined",
                        "validation_error": validation_error,
                        "content_sha256": digest,
                    },
                    agent={},
                    claimed=claimed,
                    envelope=envelope,
                    repository=repository,
                    quarantined=True,
                )
            self.dependencies.inbox.ensure_dedupe(root)
            reserved = self.dependencies.inbox.reserve_deduplication(root, digest)
            if not reserved:
                return self._finalize(
                    root=root,
                    directory="done",
                    digest=digest,
                    run_id=run_id,
                    request=raw,
                    intake={"outcome": "duplicate", "content_sha256": digest},
                    agent={},
                    claimed=claimed,
                    envelope=envelope,
                    repository=repository,
                    duplicate=True,
                )
            if envelope is None:
                raise FileIntakeFailure("filesystem intake failed")
            work_item = WorkItem.from_file(
                envelope=envelope, content_sha256=digest, run_id=run_id, mode=mode
            )
            try:
                agent = dict(
                    self.dependencies.run_normalized(work_item, target, timeout_seconds)
                )
            except (OSError, RuntimeError, ValueError):
                self._finalize(
                    root=root,
                    directory="failed",
                    digest=digest,
                    run_id=run_id,
                    request=raw,
                    intake={"outcome": "failed", "content_sha256": digest},
                    agent={},
                    claimed=claimed,
                    envelope=envelope,
                    repository=repository,
                )
                raise
            outcome = str(agent.get("outcome", "failed"))
            directory = "awaiting-human" if outcome == "awaiting_human" else "failed"
            return self._finalize(
                root=root,
                directory=directory,
                digest=digest,
                run_id=run_id,
                request=raw,
                intake={"outcome": outcome, "content_sha256": digest},
                agent=agent,
                claimed=claimed,
                envelope=envelope,
                repository=repository,
                branch=work_item.branch,
            )

    def _finalize(
        self,
        *,
        root: Path,
        directory: str,
        digest: str,
        run_id: str,
        request: bytes,
        intake: Mapping[str, object],
        agent: Mapping[str, object],
        claimed: Path,
        envelope: FilesystemEnvelope | None,
        repository: str,
        branch: str | None = None,
        duplicate: bool = False,
        quarantined: bool = False,
    ) -> FileIntakeReport:
        """Write a terminal bundle, retaining the claim when writing fails."""
        destination = self.dependencies.inbox.destination(
            root, directory, digest, run_id
        )
        self.dependencies.finalize_bundle(
            destination=destination,
            request=request,
            intake_result=intake,
            agent_result=agent,
        )
        self.dependencies.inbox.release_claim(claimed)
        raw_validation_error = intake.get("validation_error")
        validation_error = (
            raw_validation_error if isinstance(raw_validation_error, str) else None
        )
        return _report(
            preview=False,
            outcome=str(intake["outcome"]),
            envelope=envelope,
            repository=repository,
            digest=digest,
            destination=destination,
            duplicate=duplicate,
            quarantined=quarantined,
            validation_error=validation_error,
            branch=branch,
            result=agent,
        )


def _validate_address(path: Path, inbox_root: Path) -> tuple[Path, Path]:
    """Resolve only an absolute no-symlink inbox root and direct JSON child."""
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or path.name in {"", ".", ".."}
        or path.suffix != ".json"
    ):
        raise RuntimeError("invalid inbox item path")
    if not inbox_root.is_absolute():
        raise RuntimeError("inbox root must be absolute")
    if not _lexically_normal_absolute(inbox_root):
        raise RuntimeError("inbox root must be lexically normalized")
    _no_symlink_components(inbox_root)
    return inbox_root, inbox_root / "inbox" / path.name


def _no_symlink_components(path: Path) -> None:
    """Reject a path containing a symlink without resolving through it."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise RuntimeError("controlled path must not contain symlinks")


def _validate_layout(
    root: Path, item: Path, checkout: Path, device_id: Callable[[Path], int]
) -> None:
    """Prove controlled paths are regular, separate, and atomically movable."""
    if not root.is_dir() or root.is_symlink():
        raise FileIntakeFailure("inbox root is unsafe")
    _no_symlink_components(root)
    controlled = tuple(root / name for name in _DIRECTORIES if name != ".dedupe")
    if any(
        not directory.is_dir() or directory.is_symlink() for directory in controlled
    ):
        raise FileIntakeFailure("controlled directories are unsafe")
    if not checkout.is_absolute() or checkout.is_symlink() or not checkout.is_dir():
        raise FileIntakeFailure("repository checkout is unsafe")
    _no_symlink_components(checkout)
    try:
        root.relative_to(checkout)
        raise FileIntakeFailure("repository and inbox overlap")
    except ValueError:
        try:
            checkout.relative_to(root)
            raise FileIntakeFailure("repository and inbox overlap")
        except ValueError:
            pass
    if any(device_id(directory) != device_id(root) for directory in controlled):
        raise FileIntakeFailure("controlled directories must share a device")
    dedupe = root / ".dedupe"
    if dedupe.exists() and (
        not dedupe.is_dir()
        or dedupe.is_symlink()
        or device_id(dedupe) != device_id(root)
    ):
        raise FileIntakeFailure("deduplication directory is unsafe")
    info = item.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or item.is_symlink():
        raise FileIntakeFailure("inbox item must be a single-link regular file")


def _validate_target(target: RepositoryTarget) -> None:
    """Validate the trusted allowlist projection before opening task data."""
    if (
        not _safe_bare_host(target.host)
        or target.project_id <= 0
        or not _lexically_normal_absolute(target.checkout)
    ):
        raise FileIntakeFailure("repository allowlist entry is unsafe")
    try:
        validate_alias(target.alias)
    except ValueError as error:
        raise FileIntakeFailure("repository allowlist entry is unsafe") from error


def _lexically_normal_absolute(path: Path) -> bool:
    """Return whether an absolute path has no lexical traversal components."""
    return path.is_absolute() and all(part not in {".", ".."} for part in path.parts)


def _safe_bare_host(value: str) -> bool:
    """Return whether a configured GitLab host is a bare hostname."""
    return (
        bool(value) and "://" not in value and "/" not in value and not value.isspace()
    )


def _read_envelope(raw: bytes) -> tuple[bytes, FilesystemEnvelope | None, str | None]:
    """Read bounded raw bytes and validate UTF-8 JSON at the producer boundary."""
    if len(raw) > _MAX_BYTES:
        return raw, None, "request exceeds 262144 bytes"
    try:
        value = json.loads(raw.decode("utf-8"))
        return raw, FilesystemEnvelope.model_validate(value), None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        return raw, None, str(error)


def _bounded_read_request(
    item: Path, limit: int, read_syscall: Callable[[int, int], bytes]
) -> bytes:
    """Read no more than the explicit limit through a no-follow descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(item, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileIntakeFailure("inbox item must be a single-link regular file")
        chunks = bytearray(read_syscall(descriptor, limit))
        expected = min(limit, info.st_size)
        while len(chunks) < expected:
            chunk = read_syscall(descriptor, expected - len(chunks))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _claim(
    item: Path,
    processing: Path,
    atomic_replace: Callable[[Path, Path], None],
    fsync_directory: Callable[[Path], None],
) -> Path:
    """Atomically hand ownership from inbox to processing."""
    if processing.exists():
        raise FileIntakeFailure("processing item already exists")
    atomic_replace(item, processing)
    fsync_directory(item.parent)
    fsync_directory(processing.parent)
    return processing


def _reserve_deduplication(
    root: Path, digest: str, write_syscall: Callable[[int, bytes], int]
) -> bool:
    """Create the durable raw-content reservation exactly once without scanning."""
    reservation = root / ".dedupe" / digest
    try:
        descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        content = f"{digest}\n".encode()
        offset = 0
        while offset < len(content):
            written = write_syscall(descriptor, content[offset:])
            if written <= 0:
                raise FileIntakeFailure(
                    "deduplication reservation write made no progress"
                )
            offset += written
        os.fsync(descriptor)
    except (OSError, FileIntakeFailure) as error:
        try:
            os.close(descriptor)
        finally:
            _invalidate_reservation(reservation)
        if isinstance(error, FileIntakeFailure):
            raise
        raise FileIntakeFailure(
            "deduplication reservation could not be written"
        ) from error
    os.close(descriptor)
    _fsync_directory(reservation.parent)
    return True


def _invalidate_reservation(reservation: Path) -> None:
    """Remove an incomplete create-only reservation before it can block future work."""
    try:
        reservation.unlink()
    except OSError as error:
        raise FileIntakeFailure(
            "incomplete deduplication reservation could not be removed"
        ) from error
    _fsync_directory(reservation.parent)


def durable_finalize_bundle(
    *,
    destination: Path,
    request: bytes,
    intake_result: Mapping[str, object],
    agent_result: Mapping[str, object],
    fsync_path: Callable[[Path], None] | None = None,
    atomic_replace: Callable[[Path, Path], None] | None = None,
) -> None:
    """Durably expose one complete terminal bundle through one atomic rename."""
    staging = destination.with_name(f".{destination.name}.staging")
    flush = fsync_path or _fsync_path
    replace = atomic_replace or os.replace
    if destination.exists() or staging.exists():
        raise FileIntakeFailure("terminal bundle already exists")
    staging.mkdir(mode=0o700)
    _write_bundle_file(staging / "request.json", request, flush)
    _write_bundle_file(
        staging / "intake-result.json",
        json.dumps(intake_result, sort_keys=True).encode(),
        flush,
    )
    _write_bundle_file(
        staging / "agent-result.json",
        json.dumps(agent_result, sort_keys=True).encode(),
        flush,
    )
    flush(staging)
    replace(staging, destination)
    flush(destination)
    flush(destination.parent)


def _write_bundle_file(
    path: Path, content: bytes, fsync_path: Callable[[Path], None]
) -> None:
    """Create and flush one non-replaceable result file in the private staging dir."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("bundle file write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_path(path)


def _fsync_path(path: Path) -> None:
    """Flush a file or directory by name at the durable persistence boundary."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_dedupe_directory(root: Path) -> None:
    """Create the runner-owned create-only reservation directory in applied mode."""
    directory = root / ".dedupe"
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise FileIntakeFailure("deduplication directory is unsafe")
        return
    directory.mkdir(mode=0o700)
    _fsync_directory(root)


def _repository_error(
    envelope: FilesystemEnvelope | None, repository: str
) -> str | None:
    """Return the safe mismatch error shared by preview and applied execution."""
    if envelope is None:
        return None
    if envelope.repository != repository:
        return "repository does not match selected repository"
    return None


def _report(
    *,
    preview: bool,
    outcome: str,
    envelope: FilesystemEnvelope | None,
    repository: str,
    digest: str,
    destination: Path | None = None,
    duplicate: bool = False,
    quarantined: bool = False,
    validation_error: str | None = None,
    branch: str | None = None,
    result: Mapping[str, object] | None = None,
) -> FileIntakeReport:
    """Build the public report without exposing producer-owned authority."""
    return FileIntakeReport(
        preview=preview,
        outcome=outcome,
        destination=destination,
        duplicate=duplicate,
        quarantined=quarantined,
        validation_error=validation_error,
        source=envelope.source_id if envelope else None,
        repository=repository,
        sha256=digest,
        branch=branch,
        result=result,
    )


def _durable_unlink(path: Path) -> None:
    """Remove a processing claim and durably record that terminal ownership moved."""
    path.unlink()
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry change before reporting a completed handoff."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
