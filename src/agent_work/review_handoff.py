# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Immutable producer-owned handoffs for one validated review candidate."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_MAX_INPUT = 1_048_576
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")


class ReviewHandoffError(RuntimeError):
    """A review request or retained object failed a trusted check."""


@dataclass(frozen=True)
class ReviewHandoffDependencies:
    """Injected observation, bounded I/O, immutable storage, and entropy."""

    observe_candidate: Callable[[], dict[str, object]]
    read_input: Callable[[Path], bytes | None]
    write_object: Callable[[str, bytes], None]
    read_object: Callable[[str], bytes | None]
    list_objects: Callable[[str], tuple[str, ...]]
    nonce: Callable[[], str]
    clock: Callable[[], float]


class _Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: int = Field(strict=True, gt=0)
    issue_iid: int | None = Field(default=None, strict=True, gt=0)
    branch: str = Field(strict=True, min_length=1, max_length=255)
    worktree: str = Field(strict=True, min_length=1, max_length=4096)
    candidate_tree: str = Field(strict=True, pattern=r"^[0-9a-f]{40}$")
    validation_state: str = Field(strict=True)
    junit_digests: tuple[str, ...]


class _Session(_Candidate):
    session_ref: str = Field(strict=True)
    created_at: float


@dataclass(frozen=True)
class ReviewHandoffSession:
    """Compact reference returned to the producer that opened a session."""

    session_ref: str
    applied: bool


@dataclass(frozen=True)
class ReviewHandoffReceipt:
    """Compact accepted receipt reference and digest."""

    receipt_ref: str
    sha256: str
    applied: bool


@dataclass(frozen=True)
class ReviewHandoffManifest:
    """Trusted candidate identity and immutable receipt references."""

    session_ref: str
    project_id: int
    issue_iid: int | None
    branch: str
    candidate_tree: str
    junit_digests: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    receipts: tuple[ReviewHandoffReceiptEvidence, ...]
    sha256: str


@dataclass(frozen=True)
class ReviewHandoffReceiptEvidence:
    """Structured immutable receipt metadata for Finalizer comparison."""

    role: str
    review_version: int
    kind: str
    receipt_ref: str
    sha256: str


def _encode(value: BaseModel) -> bytes:
    return value.model_dump_json().encode()


def _decode(model: type[BaseModel], raw: bytes | None) -> BaseModel:
    if raw is None or len(raw) > _MAX_INPUT:
        raise ReviewHandoffError("retained review object is missing or oversized")
    try:
        return model.model_validate_json(raw)
    except ValidationError as error:
        raise ReviewHandoffError("retained review object is malformed") from error


def _candidate(dependencies: ReviewHandoffDependencies) -> _Candidate:
    try:
        candidate = _Candidate.model_validate(dependencies.observe_candidate())
    except ValidationError as error:
        raise ReviewHandoffError("candidate observation is malformed") from error
    if candidate.validation_state != "terminal-success" or not candidate.junit_digests:
        raise ReviewHandoffError(
            "candidate lacks successful validation and JUnit evidence"
        )
    if any(
        re.fullmatch(r"py3(?:11|14):[0-9a-f]{64}", digest) is None
        for digest in candidate.junit_digests
    ):
        raise ReviewHandoffError("candidate JUnit evidence is malformed")
    if not Path(candidate.worktree).is_absolute():
        raise ReviewHandoffError("candidate worktree must be absolute")
    return candidate


def _session(session_ref: str, dependencies: ReviewHandoffDependencies) -> _Session:
    if not session_ref.startswith("session/") or _ID.fullmatch(session_ref[8:]) is None:
        raise ReviewHandoffError("invalid session reference")
    session = _decode(_Session, dependencies.read_object(session_ref))
    assert isinstance(session, _Session)
    if session.session_ref != session_ref:
        raise ReviewHandoffError("session identity mismatch")
    observed = _candidate(dependencies)
    for field in (
        "project_id",
        "issue_iid",
        "branch",
        "worktree",
        "candidate_tree",
        "junit_digests",
    ):
        if getattr(session, field) != getattr(observed, field):
            raise ReviewHandoffError(f"candidate {field} changed since session began")
    return session


def begin_review_handoff(
    *, worktree: Path, apply: bool, dependencies: ReviewHandoffDependencies
) -> ReviewHandoffSession:
    """Open one immutable session bound to the current successful Judge candidate."""
    observed = _candidate(dependencies)
    if worktree != Path(observed.worktree):
        raise ReviewHandoffError("requested worktree differs from observed candidate")
    nonce = dependencies.nonce()
    if _ID.fullmatch(nonce) is None:
        raise ReviewHandoffError("session nonce is malformed")
    session_ref = f"session/{nonce}"
    session = _Session(
        **observed.model_dump(),
        session_ref=session_ref,
        created_at=dependencies.clock(),
    )
    if apply:
        try:
            dependencies.write_object(session_ref, _encode(session))
        except FileExistsError as error:
            raise ReviewHandoffError("session already exists") from error
    return ReviewHandoffSession(session_ref=session_ref, applied=apply)


def _slot(session_ref: str, role: str, version: int) -> str:
    normalized_role = role.removeprefix("$")
    if _ID.fullmatch(normalized_role) is None or version < 1 or version > 1_000_000:
        raise ReviewHandoffError("invalid role or review version")
    return f"slot/{session_ref[8:]}/{normalized_role}/{version}"


def _attempt_count(prefix: str, dependencies: ReviewHandoffDependencies) -> int:
    attempts = set()
    for reference in dependencies.list_objects(prefix):
        if reference == prefix + "/accepted":
            continue
        if reference not in {prefix + "/attempt/1", prefix + "/attempt/2"}:
            raise ReviewHandoffError("review slot contains an unexpected object")
        if dependencies.read_object(reference) != b"rejected":
            raise ReviewHandoffError("review rejection evidence is malformed")
        attempts.add(reference)
    if prefix + "/attempt/2" in attempts and prefix + "/attempt/1" not in attempts:
        raise ReviewHandoffError("review rejection sequence is incomplete")
    return len(attempts)


def submit_review_handoff(
    *,
    session_ref: str,
    role: str,
    version: int,
    payload_path: Path,
    apply: bool,
    dependencies: ReviewHandoffDependencies,
    kind: str = "correction-ledger",
) -> ReviewHandoffReceipt:
    """Accept one bounded Green ledger, with one correction after rejection."""
    if role != "$tdd-green" or kind != "correction-ledger":
        raise ReviewHandoffError("only Green may submit a correction ledger")
    _session(session_ref, dependencies)
    prefix = _slot(session_ref, role, version)
    if dependencies.read_object(prefix + "/accepted") is not None:
        raise ReviewHandoffError("review slot already accepted")
    attempts = _attempt_count(prefix, dependencies)
    if attempts >= 2:
        raise ReviewHandoffError("review correction budget exhausted")
    payload = dependencies.read_input(payload_path)
    invalid = payload is None or not 0 < len(payload) <= _MAX_INPUT
    if invalid:
        if apply:
            try:
                dependencies.write_object(
                    f"{prefix}/attempt/{attempts + 1}", b"rejected"
                )
            except FileExistsError as error:
                raise ReviewHandoffError("review slot changed concurrently") from error
        raise ReviewHandoffError("review payload must contain at most 1048576 bytes")
    assert payload is not None
    sha256 = hashlib.sha256(payload).hexdigest()
    receipt_ref = (
        f"receipt/{session_ref[8:]}/{role.removeprefix('$')}/{version}/{sha256}"
    )
    if apply:
        try:
            # One no-replace object is the commit point. The external ref carries
            # its digest; the physical slot path prevents competing digests.
            dependencies.write_object(prefix + "/accepted", payload)
        except FileExistsError as error:
            raise ReviewHandoffError("review slot changed concurrently") from error
    return ReviewHandoffReceipt(receipt_ref=receipt_ref, sha256=sha256, applied=apply)


def review_handoff_manifest(
    *, session_ref: str, dependencies: ReviewHandoffDependencies
) -> ReviewHandoffManifest:
    """Verify the current candidate and all accepted immutable receipts."""
    session = _session(session_ref, dependencies)
    prefix = f"slot/{session_ref[8:]}/"
    receipt_refs: list[str] = []
    receipts: list[ReviewHandoffReceiptEvidence] = []
    objects = dependencies.list_objects(prefix.removesuffix("/"))
    for reference in sorted(objects):
        if not reference.startswith(prefix):
            raise ReviewHandoffError("session inventory escaped its prefix")
        parts = reference.split("/")
        if len(parts) == 6 and parts[4] == "attempt":
            slot = (
                _slot(session_ref, "$tdd-green", int(parts[3]))
                if parts[3].isdigit()
                else ""
            )
            if (
                not slot
                or reference
                not in {
                    slot + "/attempt/1",
                    slot + "/attempt/2",
                }
                or dependencies.read_object(reference) != b"rejected"
            ):
                raise ReviewHandoffError("session rejection evidence is malformed")
            continue
        if len(parts) != 5 or parts[2] != "tdd-green" or parts[4] != "accepted":
            raise ReviewHandoffError("session inventory contains an unexpected object")
        try:
            version = int(parts[3])
        except ValueError as error:
            raise ReviewHandoffError("receipt version is malformed") from error
        if reference != _slot(session_ref, "$tdd-green", version) + "/accepted":
            raise ReviewHandoffError("receipt slot identity mismatch")
        _attempt_count(_slot(session_ref, "$tdd-green", version), dependencies)
        payload = dependencies.read_object(reference)
        if payload is None or not 0 < len(payload) <= _MAX_INPUT:
            raise ReviewHandoffError("receipt payload is missing or oversized")
        digest = hashlib.sha256(payload).hexdigest()
        receipt_ref = f"receipt/{session_ref[8:]}/tdd-green/{version}/{digest}"
        receipt_refs.append(receipt_ref)
        receipts.append(
            ReviewHandoffReceiptEvidence(
                role="$tdd-green",
                review_version=version,
                kind="correction-ledger",
                receipt_ref=receipt_ref,
                sha256=digest,
            )
        )
    canonical = json.dumps(
        {
            "session_ref": session_ref,
            "project_id": session.project_id,
            "issue_iid": session.issue_iid,
            "branch": session.branch,
            "candidate_tree": session.candidate_tree,
            "junit_digests": session.junit_digests,
            "receipts": [
                [
                    item.role,
                    item.review_version,
                    item.kind,
                    item.receipt_ref,
                    item.sha256,
                ]
                for item in receipts
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ReviewHandoffManifest(
        session_ref=session_ref,
        project_id=session.project_id,
        issue_iid=session.issue_iid,
        branch=session.branch,
        candidate_tree=session.candidate_tree,
        junit_digests=session.junit_digests,
        receipt_refs=tuple(receipt_refs),
        receipts=tuple(receipts),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _verify_receipt(
    session_ref: str, receipt_ref: str, dependencies: ReviewHandoffDependencies
) -> bytes:
    parts = receipt_ref.split("/")
    if len(parts) != 5 or parts[:3] != ["receipt", session_ref[8:], "tdd-green"]:
        raise ReviewHandoffError("receipt reference is malformed")
    try:
        version = int(parts[3])
    except ValueError as error:
        raise ReviewHandoffError("receipt version is malformed") from error
    if _HEX.fullmatch(parts[4]) is None:
        raise ReviewHandoffError("receipt digest is malformed")
    prefix = _slot(session_ref, "$tdd-green", version)
    if receipt_ref != f"receipt/{session_ref[8:]}/tdd-green/{version}/{parts[4]}":
        raise ReviewHandoffError("receipt reference is malformed")
    payload = dependencies.read_object(prefix + "/accepted")
    if (
        payload is None
        or not 0 < len(payload) <= _MAX_INPUT
        or hashlib.sha256(payload).hexdigest() != parts[4]
    ):
        raise ReviewHandoffError("receipt payload is missing or tampered")
    return payload


def read_review_handoff(
    *,
    session_ref: str,
    receipt_ref: str,
    dependencies: ReviewHandoffDependencies,
    expected_manifest_sha256: str | None = None,
) -> bytes:
    """Return exact retained bytes after fresh identity and digest checks."""
    manifest = review_handoff_manifest(
        session_ref=session_ref, dependencies=dependencies
    )
    if (
        expected_manifest_sha256 is not None
        and manifest.sha256 != expected_manifest_sha256
    ):
        raise ReviewHandoffError("review manifest changed since the saved handoff")
    if receipt_ref not in manifest.receipt_refs:
        raise ReviewHandoffError("receipt is not in the current session manifest")
    return _verify_receipt(session_ref, receipt_ref, dependencies)
