# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Fresh authenticated requirements evidence with explicit host verification."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, get_args

from .models import (
    LedgerRecord,
    ScopeApprovalBinding,
    ScopeApprovalEvent,
    ScopeApprovalRequest,
    ScopeReason,
    ScopeRevision,
    ScopeSourceAuthor,
    ScopeSourceNote,
    ScopeSourceObservation,
)


@dataclass(frozen=True)
class HostScopeDecision:
    """In-process host assessment, never an externally parsed approval grant."""

    author_kind: Literal["human", "agent", "bot", "service", "unknown"]
    decision: Literal["approve", "revoke", "unknown"]
    author_id: int | None = None
    note_id: int | None = None
    reviewed_revision: int | None = None
    reviewed_description_sha256: str | None = None
    reviewed_scope_sha256: str | None = None


class ScopeError(RuntimeError):
    """A closed safe evidence failure without external diagnostic text."""

    def __init__(self, reason: ScopeReason) -> None:
        self.reason: ScopeReason = (
            reason if reason in get_args(ScopeReason) else "invalid_input"
        )
        super().__init__(self.reason)


def canonical_scope_digest(scope: ScopeRevision) -> str:
    """Hash the exact ordered requirements projection, not an approval verdict."""
    return scope.canonical_digest()


def _event(note: ScopeSourceNote) -> ScopeApprovalEvent:
    return ScopeApprovalEvent(
        project_id=note.project_id,
        issue_iid=note.issue_iid,
        note_id=note.id,
        author_id=note.author_id,
        body_sha256=hashlib.sha256(note.body.encode("utf-8")).hexdigest(),
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def _source_note(
    request: ScopeApprovalRequest, source: ScopeSourceObservation, note_id: int
) -> tuple[ScopeSourceNote, ScopeSourceAuthor]:
    scope = request.scope
    if (source.host, source.project_id, source.issue_iid) != (
        scope.host,
        scope.project_id,
        scope.source_issue_iid,
    ):
        raise ScopeError("identity_mismatch")
    if not source.complete or len({n.id for n in source.notes}) != len(source.notes):
        raise ScopeError("incomplete_source")
    if (
        hashlib.sha256(source.description.encode("utf-8")).hexdigest()
        != scope.source_description_sha256
    ):
        raise ScopeError("source_changed")
    notes = tuple(n for n in source.notes if n.id == note_id)
    if len(notes) != 1:
        raise ScopeError("event_missing")
    note = notes[0]
    if (note.project_id, note.issue_iid) != (scope.project_id, scope.source_issue_iid):
        raise ScopeError("identity_mismatch")
    if note.system:
        raise ScopeError("non_human_author")
    if note.updated_at != note.created_at:
        raise ScopeError("event_changed")
    if note.created_at < request.requested_at:
        raise ScopeError("missing_requested_revision")
    if len({a.id for a in source.authors}) != len(source.authors):
        raise ScopeError("incomplete_source")
    authors = tuple(a for a in source.authors if a.id == note.author_id)
    if len(authors) != 1:
        raise ScopeError("identity_mismatch")
    author = authors[0]
    if (
        author.bot is True
        or author.id == source.service_actor_id
        or author.account_type in {"bot", "service", "service_account", "agent"}
    ):
        raise ScopeError("non_human_author")
    if author.bot is not False or author.state != "active":
        raise ScopeError("human_unverified")
    return note, author


def _result(
    request: ScopeApprovalRequest,
    reason: ScopeReason,
    event: ScopeApprovalEvent | None = None,
) -> ScopeApprovalBinding:
    state: Literal["approved", "unknown", "stale", "revoked"] = "unknown"
    if reason == "approved":
        state = "approved"
    elif reason in {"source_changed", "scope_changed", "stale_binding"}:
        state = "stale"
    elif reason == "revoked":
        state = "revoked"
    return ScopeApprovalBinding(
        request=request,
        event=event,
        state=state,
        reason=reason,
        scope_sha256=canonical_scope_digest(request.scope),
    )


def _verify(
    request: ScopeApprovalRequest,
    prior: ScopeApprovalBinding | None,
    *,
    read_source: Callable[[ScopeApprovalRequest], ScopeSourceObservation],
    verify_event: Callable[
        [ScopeApprovalRequest, ScopeSourceObservation], HostScopeDecision
    ]
    | None,
) -> ScopeApprovalBinding:
    event = prior.event if prior is not None else None
    try:
        before = read_source(request)
    except ScopeError as error:
        return _result(request, error.reason, event)
    except (OSError, RuntimeError, ValueError):
        return _result(request, "source_unavailable", event)
    decision: HostScopeDecision | None = None
    host_failed = False
    if verify_event is not None:
        try:
            decision = verify_event(request, before)
        except (OSError, RuntimeError, ValueError):
            host_failed = True
    try:
        after = read_source(request)
    except ScopeError as error:
        return _result(request, error.reason, event)
    except (OSError, RuntimeError, ValueError):
        return _result(request, "source_unavailable", event)
    try:
        first, first_author = _source_note(request, before, request.approval_note_id)
        last, last_author = _source_note(request, after, request.approval_note_id)
        first_event, last_event = _event(first), _event(last)
        if sorted(before.notes, key=lambda note: note.id) != sorted(
            after.notes, key=lambda note: note.id
        ):
            raise ScopeError("event_changed")
        if first_event != last_event or (event is not None and first_event != event):
            raise ScopeError("event_changed")
        if first_author != last_author and (
            first_author.bot,
            first_author.state,
            first_author.account_type,
        ) != (last_author.bot, last_author.state, last_author.account_type):
            raise ScopeError("human_unverified")
        if verify_event is None:
            raise ScopeError("human_verifier_unavailable")
        if host_failed or not isinstance(decision, HostScopeDecision):
            raise ScopeError("human_unverified")
        if decision.author_kind in {"agent", "bot", "service"}:
            raise ScopeError("non_human_author")
        if decision.author_kind != "human":
            raise ScopeError("human_unverified")
        if decision.decision not in {"approve", "revoke"}:
            raise ScopeError("ambiguous_reply")
        if (
            type(decision.author_id) is not int
            or type(decision.reviewed_revision) is not int
            or type(decision.note_id) is not int
            or decision.author_id != last.author_id
            or decision.reviewed_revision != request.requested_revision
            or decision.reviewed_description_sha256
            != request.requested_description_sha256
        ):
            raise ScopeError("identity_mismatch")
        if decision.reviewed_scope_sha256 != canonical_scope_digest(request.scope):
            raise ScopeError("scope_changed")
        if decision.decision == "revoke":
            if decision.note_id is None:
                raise ScopeError("identity_mismatch")
            withdrawal, _ = _source_note(request, after, decision.note_id)
            if withdrawal.author_id != decision.author_id:
                raise ScopeError("identity_mismatch")
            prior_withdrawal, _ = _source_note(request, before, decision.note_id)
            if _event(prior_withdrawal) != _event(withdrawal):
                raise ScopeError("event_changed")
            return _result(request, "revoked", _event(withdrawal))
        if decision.note_id != request.approval_note_id:
            raise ScopeError("identity_mismatch")
        return _result(request, "approved", last_event)
    except ScopeError as error:
        return _result(request, error.reason, event)
    except (UnicodeError, ValueError):
        return _result(request, "source_unavailable", event)


def capture_scope(
    request: ScopeApprovalRequest,
    *,
    read_source: Callable[[ScopeApprovalRequest], ScopeSourceObservation],
    verify_event: Callable[
        [ScopeApprovalRequest, ScopeSourceObservation], HostScopeDecision
    ]
    | None,
) -> ScopeApprovalBinding:
    """Observe source on both sides of explicit host verification without a gate."""
    return _verify(request, None, read_source=read_source, verify_event=verify_event)


def verify_scope(
    binding: ScopeApprovalBinding,
    *,
    read_source: Callable[[ScopeApprovalRequest], ScopeSourceObservation],
    verify_event: Callable[
        [ScopeApprovalRequest, ScopeSourceObservation], HostScopeDecision
    ]
    | None,
) -> ScopeApprovalBinding:
    """Freshly recheck evidence; an old terminal binding cannot recover authority."""
    if binding.state in {"stale", "revoked"}:
        return _result(
            binding.request,
            "revoked" if binding.state == "revoked" else "stale_binding",
            binding.event,
        )
    if binding.scope_sha256 != canonical_scope_digest(binding.request.scope):
        return _result(binding.request, "scope_changed", binding.event)
    return _verify(
        binding.request, binding, read_source=read_source, verify_event=verify_event
    )


def bind_scope_record(
    record: LedgerRecord, binding: ScopeApprovalBinding
) -> LedgerRecord:
    """Attach exact evidence to an existing workflow without changing its phase."""
    if (record.project_id, record.issue_iid) != (
        binding.request.scope.project_id,
        binding.request.scope.issue_iid,
    ):
        raise ScopeError("identity_mismatch")
    if binding.scope_sha256 != canonical_scope_digest(binding.request.scope):
        raise ScopeError("scope_changed")
    prior = record.scope_binding
    if prior is not None and prior.state in {"stale", "revoked"}:
        prior_note_ids = {prior.request.approval_note_id}
        if prior.event is not None:
            prior_note_ids.add(prior.event.note_id)
        if (
            binding.request.requested_revision <= prior.request.requested_revision
            or binding.request.requested_at <= prior.request.requested_at
            or binding.event is None
            or binding.event.note_id in prior_note_ids
        ):
            raise ScopeError("revoked" if prior.state == "revoked" else "stale_binding")
    try:
        return LedgerRecord.model_validate(
            record.model_dump() | {"scope_binding": binding.model_dump()}
        )
    except ValueError as error:
        raise ScopeError("invalid_record") from error
