# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Atomic XDG-local JSON ledger for recoverable agent work."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import IO

from pydantic import ValidationError

from .domain import ErrorCode
from .models import LedgerRecord


class LedgerError(RuntimeError):
    """A ledger operation could not preserve safe durable state."""


class LedgerCorrupt(LedgerError):
    """A record was preserved but could not be decoded safely."""


class LockBusy(LedgerError):
    """Another local segment owns the project lock."""


class XdgProjectLock:
    """Nonblocking advisory lock inside the project state directory."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None

    def __enter__(self) -> None:
        """Acquire the project lock without waiting."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise LockBusy("another segment owns the project lock") from error
        self._handle = handle

    def __exit__(self, *_args: object) -> bool:
        """Release the project lock."""
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False


def project_state_path(
    *, host: str, project_id: int, environment: Mapping[str, str]
) -> Path:
    """Return the XDG project-state directory without creating it."""
    base = Path(environment.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    host_hash = hashlib.sha256(host.encode()).hexdigest()[:12]
    return base / "agent-work" / "projects" / f"{project_id}-{host_hash}"


class JsonLedger:
    """Persist immutable issue records with atomic replacement."""

    def __init__(self, root: Path) -> None:
        self.state_root = root

    @property
    def records(self) -> Path:
        """Return the issue-record directory."""
        return self.state_root / "records"

    @staticmethod
    def _prepare(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    def persist(self, record: LedgerRecord) -> LedgerRecord:
        """Atomically persist a record and increment its revision."""
        self._prepare(self.records)
        updated = record.model_copy(update={"revision": record.revision + 1})
        target = self.records / f"{record.issue_iid}.json"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.records, prefix=".record-", text=True
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    updated.model_dump(mode="json"),
                    handle,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            temporary.replace(target)
            target.chmod(0o600)
            directory = os.open(self.records, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise LedgerError("ledger record could not be persisted") from error
        return updated

    def load(self, issue_iid: int) -> LedgerRecord | None:
        """Load one record, preserving corrupt bytes as recovery evidence."""
        target = self.records / f"{issue_iid}.json"
        if not target.exists():
            return None
        raw = target.read_bytes()
        try:
            return LedgerRecord.model_validate_json(raw)
        except (ValidationError, ValueError) as error:
            self._preserve_corrupt(issue_iid, raw)
            raise LedgerCorrupt("ledger record is corrupt") from error

    def list_records(self) -> tuple[LedgerRecord, ...]:
        """Return all valid records ordered by issue IID."""
        if not self.records.exists():
            return ()
        loaded = (
            self.load(int(path.stem))
            for path in sorted(self.records.glob("[0-9]*.json"))
        )
        return tuple(record for record in loaded if record is not None)

    def _preserve_corrupt(self, issue_iid: int, raw: bytes) -> None:
        recovery = self.state_root / "recovery"
        self._prepare(recovery)
        full_digest = hashlib.sha256(raw).hexdigest()
        target = recovery / f"{issue_iid}-{full_digest[:16]}.json"
        payload = {
            "schema_version": 1,
            "issue_iid": issue_iid,
            "error_code": ErrorCode.LEDGER_CORRUPT,
            "record_sha256": full_digest,
        }
        target.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        )
        target.chmod(0o600)
