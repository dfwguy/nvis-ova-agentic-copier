# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Requirements approval is fresh host-verified evidence, never a cached grant."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work import runtime
from agent_work.domain import RunnerState, WorkflowPhase
from agent_work.ledger import JsonLedger, LedgerCorrupt
from agent_work.models import CommandReport, LedgerRecord

HOST = "gitlab.example.test"
PROJECT = 310967
TASK = 360
SOURCE = 389
AUTHOR = 65239
NOTE = 71979732
SERVICE = 144242
DESCRIPTION = "Observable approved scope fixture"
BODY = "Scope approved. I will reply to #360 to approve as well."
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


class _ScopeBound(Protocol):
    """Exact future evidence field on real reports and persisted records."""

    @property
    def scope_binding(self) -> runtime.ScopeApprovalBinding | None: ...


def scope_binding(
    value: CommandReport | LedgerRecord,
) -> runtime.ScopeApprovalBinding | None:
    """Read the planned field while preserving actual boundary parsing."""
    return cast(_ScopeBound, value).scope_binding


def _digest(text: str) -> str:
    """Hash fixture text without interpreting it as authority."""
    return hashlib.sha256(text.encode()).hexdigest()


def _scope_data() -> dict[str, object]:
    """Provide one fully identified, observable requirements revision."""
    return {
        "schema_version": 1,
        "host": HOST,
        "project_id": PROJECT,
        "issue_iid": TASK,
        "source_issue_iid": SOURCE,
        "scope_revision": 1,
        "source_description_sha256": _digest(DESCRIPTION),
        "baseline_sha": "a" * 40,
        "criteria": [
            {
                "id": "R360-IDENTITY",
                "behavior": "Bind the exact source",
                "source_ref": "issue:389",
                "constraints": ["No factory grant"],
                "required_evidence": ["Authenticated human event"],
            }
        ],
    }


def request() -> runtime.ScopeApprovalRequest:
    """Retain the actual request revision before the selected reply."""
    return runtime.ScopeApprovalRequest(
        scope=runtime.ScopeRevision.model_validate(_scope_data()),
        requested_revision=1,
        requested_description_sha256=_digest(DESCRIPTION),
        requested_at=NOW,
        approval_note_id=NOTE,
    )


def observation() -> runtime.ScopeSourceObservation:
    """Supply independent authenticated source and account facts."""
    return runtime.ScopeSourceObservation(
        host=HOST,
        project_id=PROJECT,
        issue_iid=SOURCE,
        description=DESCRIPTION,
        notes=(
            runtime.ScopeSourceNote(
                project_id=PROJECT,
                issue_iid=SOURCE,
                id=NOTE,
                body=BODY,
                author_id=AUTHOR,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
                system=False,
            ),
        ),
        authors=(
            runtime.ScopeSourceAuthor(
                id=AUTHOR,
                username="breese",
                bot=False,
                state="active",
                account_type=None,
            ),
        ),
        service_actor_id=SERVICE,
        complete=True,
        updated_at=NOW,
    )


def _decision(bound: runtime.ScopeApprovalRequest) -> runtime.HostScopeDecision:
    """In-process verifier affirms personhood and the reviewed request."""
    return runtime.HostScopeDecision(
        author_kind="human",
        decision="approve",
        author_id=AUTHOR,
        note_id=NOTE,
        reviewed_revision=bound.requested_revision,
        reviewed_description_sha256=bound.requested_description_sha256,
        reviewed_scope_sha256=runtime.canonical_scope_digest(bound.scope),
    )


class SourceFixture:
    """Controlled independent observations and trusted verifier callbacks."""

    def __init__(
        self, observations: tuple[runtime.ScopeSourceObservation, ...] | None = None
    ) -> None:
        self.observations = observations or (observation(), observation())
        self.reads = 0
        self.host_decision: runtime.HostScopeDecision | None = None

    def read(
        self, bound: runtime.ScopeApprovalRequest
    ) -> runtime.ScopeSourceObservation:
        del bound
        value = self.observations[min(self.reads, len(self.observations) - 1)]
        self.reads += 1
        return value

    def verify(
        self,
        bound: runtime.ScopeApprovalRequest,
        source: runtime.ScopeSourceObservation,
    ) -> runtime.HostScopeDecision:
        del bound, source
        return self.host_decision or _decision(request())


def approved() -> runtime.ScopeApprovalBinding:
    """Establish an exact accepted public control before a negative mutation."""
    fixture = SourceFixture()
    result = runtime.capture_scope(
        request(), read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state == "approved" and result.reason == "approved"
    assert result.event is not None and result.event.note_id == NOTE
    assert result.scope_sha256 == runtime.canonical_scope_digest(result.request.scope)
    assert fixture.reads == 2
    return result


def _changed_source(field: str, value: object) -> runtime.ScopeSourceObservation:
    """Change exactly one public authenticated observation dimension."""
    source = observation()
    if field.startswith("note."):
        return source.model_copy(
            update={"notes": (source.notes[0].model_copy(update={field[5:]: value}),)}
        )
    if field.startswith("author."):
        return source.model_copy(
            update={
                "authors": (source.authors[0].model_copy(update={field[7:]: value}),)
            }
        )
    return source.model_copy(update={field: value})


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("host", "other.example.test", "identity_mismatch"),
        ("project_id", PROJECT + 1, "identity_mismatch"),
        ("issue_iid", SOURCE + 1, "identity_mismatch"),
        ("description", DESCRIPTION + " changed", "source_changed"),
        ("complete", False, "incomplete_source"),
        ("notes", (), "event_missing"),
        ("note.project_id", PROJECT + 1, "identity_mismatch"),
        ("note.issue_iid", SOURCE + 1, "identity_mismatch"),
        ("note.id", NOTE + 1, "event_missing"),
        ("note.author_id", AUTHOR + 1, "identity_mismatch"),
        ("note.body", BODY + " changed", "event_changed"),
        ("note.updated_at", NOW + timedelta(seconds=2), "event_changed"),
        ("author.bot", True, "non_human_author"),
        ("author.state", "blocked", "human_unverified"),
        ("author.state", None, "human_unverified"),
        ("author.account_type", "service_account", "non_human_author"),
        ("author.account_type", "bot", "non_human_author"),
        ("service_actor_id", AUTHOR, "non_human_author"),
    ],
)
def test_single_authenticated_source_mutation_never_reuses_approval(
    field: str, value: object, reason: str
) -> None:
    binding = approved()
    fixture = SourceFixture((_changed_source(field, value),) * 2)
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state != "approved" and result.reason == reason


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("author_kind", "agent", "non_human_author"),
        ("author_kind", "bot", "non_human_author"),
        ("author_kind", "service", "non_human_author"),
        ("author_kind", "unknown", "human_unverified"),
        ("decision", "unknown", "ambiguous_reply"),
        ("author_id", AUTHOR + 1, "identity_mismatch"),
        ("note_id", NOTE + 1, "identity_mismatch"),
        ("reviewed_revision", 2, "identity_mismatch"),
        ("reviewed_description_sha256", "b" * 64, "identity_mismatch"),
        ("reviewed_scope_sha256", "c" * 64, "scope_changed"),
    ],
)
def test_single_host_decision_mutation_refuses_even_nonbot_account(
    field: str, value: object, reason: str
) -> None:
    binding = approved()
    fixture = SourceFixture()
    fixture.host_decision = replace(_decision(binding.request), **{field: value})
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state != "approved" and result.reason == reason


@pytest.mark.parametrize("operation", ["capture", "verify"])
def test_missing_host_verifier_never_accepts_wording_or_cached_approved_claim(
    operation: str,
) -> None:
    binding = approved()
    fixture = SourceFixture()
    result = (
        runtime.capture_scope(
            binding.request, read_source=fixture.read, verify_event=None
        )
        if operation == "capture"
        else runtime.verify_scope(binding, read_source=fixture.read, verify_event=None)
    )
    assert result.state == "unknown" and result.reason == "human_verifier_unavailable"


@pytest.mark.parametrize("change", ["description", "event", "account", "incomplete"])
def test_source_changes_between_independent_reads_refuse_capture(change: str) -> None:
    approved()
    field, value = {
        "description": ("description", "new scope"),
        "event": ("note.updated_at", NOW + timedelta(seconds=3)),
        "account": ("author.bot", True),
        "incomplete": ("complete", False),
    }[change]
    fixture = SourceFixture((observation(), _changed_source(field, value)))
    result = runtime.capture_scope(
        request(), read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state != "approved"
    assert result.reason in {
        "source_changed",
        "event_changed",
        "non_human_author",
        "incomplete_source",
    }
    assert fixture.reads == 2


def test_comment_only_updated_time_and_username_are_not_approval_authority() -> None:
    binding = approved()
    source = _changed_source("updated_at", NOW + timedelta(days=1))
    source = source.model_copy(
        update={
            "authors": (
                source.authors[0].model_copy(
                    update={"username": "factory-looking-name"}
                ),
            )
        }
    )
    fixture = SourceFixture((source,) * 2)
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state == "approved"
    fixture.host_decision = replace(_decision(binding.request), author_kind="service")
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state == "unknown" and result.reason == "non_human_author"


@pytest.mark.parametrize(
    "field", ["requested_revision", "requested_description_sha256", "requested_at"]
)
def test_request_binding_rejects_wrong_revision_hash_or_reply_before_request(
    field: str,
) -> None:
    binding = approved()
    payload = binding.request.model_dump(mode="json")
    payload[field] = {
        "requested_revision": 2,
        "requested_description_sha256": "f" * 64,
        "requested_at": (NOW + timedelta(days=1)).isoformat(),
    }[field]
    if field != "requested_at":
        with pytest.raises(ValidationError):
            runtime.ScopeApprovalRequest.model_validate(payload)
    else:
        fixture = SourceFixture()
        result = runtime.capture_scope(
            runtime.ScopeApprovalRequest.model_validate(payload),
            read_source=fixture.read,
            verify_event=fixture.verify,
        )
        assert (
            result.state == "unknown" and result.reason == "missing_requested_revision"
        )


@pytest.mark.parametrize("failure", ["read", "verify"])
def test_external_failure_is_unknown_without_raw_private_diagnostics(
    failure: str,
) -> None:
    binding = approved()
    fixture = SourceFixture()

    def fail_read(
        bound: runtime.ScopeApprovalRequest,
    ) -> runtime.ScopeSourceObservation:
        del bound
        raise OSError("synthetic-private-token")

    def fail_verify(
        bound: runtime.ScopeApprovalRequest, source: runtime.ScopeSourceObservation
    ) -> runtime.HostScopeDecision:
        del bound, source
        raise RuntimeError("synthetic-private-token")

    result = runtime.verify_scope(
        binding,
        read_source=fail_read if failure == "read" else fixture.read,
        verify_event=fail_verify if failure == "verify" else fixture.verify,
    )
    assert result.state == "unknown"
    assert result.reason == (
        "source_unavailable" if failure == "read" else "human_unverified"
    )
    assert "synthetic-private-token" not in result.model_dump_json()


@pytest.mark.parametrize("state", ["stale", "revoked"])
@given(revision=st.integers(min_value=1, max_value=100))
def test_terminal_binding_never_recovers_with_same_approval_event(
    state: str, revision: int
) -> None:
    binding = approved()
    binding = binding.model_copy(
        update={
            "state": state,
            "reason": "revoked" if state == "revoked" else "stale_binding",
        }
    )
    fixture = SourceFixture()
    fixture.host_decision = replace(
        _decision(binding.request), reviewed_revision=revision
    )
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state != "approved"
    assert result.reason in {"revoked", "stale_binding"}


def test_authenticated_withdrawal_prevents_cached_or_reloaded_reuse(
    tmp_path: Path,
) -> None:
    binding = approved()
    fixture = SourceFixture()
    fixture.host_decision = replace(_decision(binding.request), decision="revoke")
    revoked = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert revoked.state == "revoked" and revoked.reason == "revoked"
    reloaded = runtime.ScopeApprovalBinding.model_validate_json(
        revoked.model_dump_json()
    )
    fresh = SourceFixture()
    assert (
        runtime.verify_scope(
            reloaded, read_source=fresh.read, verify_event=fresh.verify
        ).state
        == "revoked"
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("project_id", True),
        ("issue_iid", 0),
        ("source_issue_iid", -1),
        ("scope_revision", True),
        ("scope_revision", 0),
        ("source_description_sha256", "bad"),
        ("baseline_sha", "bad"),
        ("extra", "claim"),
        ("host", "https://wrong/host"),
    ],
)
def test_strict_scope_boundary_rejects_single_invalid_field(
    field: str, value: object
) -> None:
    runtime.ScopeRevision.model_validate(_scope_data())
    data = _scope_data()
    data[field] = value
    with pytest.raises(ValidationError):
        runtime.ScopeRevision.model_validate(data)


@pytest.mark.parametrize(
    "change", ["duplicate", "unknown", "empty-id", "surrogate", "oversized"]
)
def test_criterion_boundary_rejects_ambiguous_or_unbounded_definition(
    change: str,
) -> None:
    bound = request()
    data = bound.scope.model_dump(mode="json")
    criteria = [criterion.model_dump(mode="json") for criterion in bound.scope.criteria]
    if change == "duplicate":
        criteria *= 2
    else:
        criteria[0].update(
            {"unknown": "grant"}
            if change == "unknown"
            else {"id": ""}
            if change == "empty-id"
            else {"behavior": "\ud800"}
            if change == "surrogate"
            else {"behavior": "x" * 1_000_001}
        )
    data["criteria"] = criteria
    with pytest.raises(ValidationError):
        runtime.ScopeRevision.model_validate(data)


@given(
    revision=st.integers(min_value=1, max_value=1_000),
    behavior=st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
        min_size=1,
        max_size=40,
    ).filter(lambda s: bool(s.strip())),
)
def test_canonical_digest_is_stable_across_boundary_roundtrip_and_binds_revision(
    revision: int, behavior: str
) -> None:
    data = _scope_data()
    data["scope_revision"] = revision
    criterion = runtime.ScopeCriterion(
        id="R360-IDENTITY", behavior=behavior, source_ref="issue:389"
    )
    data["criteria"] = [criterion.model_dump(mode="json")]
    scope = runtime.ScopeRevision.model_validate(data)
    value = runtime.canonical_scope_digest(scope)
    assert len(value) == 64 and value == runtime.canonical_scope_digest(
        runtime.ScopeRevision.model_validate_json(scope.model_dump_json())
    )
    other = scope.model_copy(update={"scope_revision": revision + 1})
    assert runtime.canonical_scope_digest(other) != value


def test_canonical_digest_binds_order_constraints_evidence_and_baseline() -> None:
    bound = request().scope
    original = runtime.canonical_scope_digest(bound)
    for changes in (
        {"baseline_sha": "b" * 40},
        {
            "criteria": (
                bound.criteria[0].model_copy(update={"constraints": ("Changed",)}),
            )
        },
        {
            "criteria": (
                bound.criteria[0].model_copy(
                    update={"required_evidence": ("Changed",)}
                ),
            )
        },
    ):
        assert (
            runtime.canonical_scope_digest(bound.model_copy(update=changes)) != original
        )
    second = bound.criteria[0].model_copy(update={"id": "R360-HUMAN"})
    pair = bound.model_copy(update={"criteria": (bound.criteria[0], second)})
    assert runtime.canonical_scope_digest(pair) != runtime.canonical_scope_digest(
        pair.model_copy(update={"criteria": tuple(reversed(pair.criteria))})
    )


def record(root: Path) -> LedgerRecord:
    """An already existing workflow, without changing its lifecycle."""
    return LedgerRecord(
        project_id=PROJECT,
        issue_iid=TASK,
        state=RunnerState.BLOCKED,
        phase=WorkflowPhase.CODING_GREEN,
        source_sha="a" * 40,
        source_branch="agent/issue-360",
        worktree=root,
    )


@pytest.mark.parametrize("mismatch", ["project", "issue", "digest"])
def test_bind_and_real_ledger_reload_validate_exact_scope_identity(
    tmp_path: Path, mismatch: str
) -> None:
    binding = approved()
    ledger = JsonLedger(tmp_path / "state", clock=lambda: NOW)
    original = ledger.persist(record(tmp_path))
    bound = runtime.bind_scope_record(original, binding)
    persisted = ledger.persist(bound)
    loaded = ledger.load_read_only(TASK)
    assert loaded is not None and scope_binding(loaded) == binding
    assert loaded.state == original.state and loaded.phase == original.phase
    changes: Mapping[str, object] = (
        {"project_id": PROJECT + 1}
        if mismatch == "project"
        else {"issue_iid": TASK + 1}
        if mismatch == "issue"
        else {}
    )
    if mismatch == "digest":
        binding = binding.model_copy(update={"scope_sha256": "f" * 64})
    with pytest.raises(runtime.ScopeError):
        runtime.bind_scope_record(original.model_copy(update=changes), binding)
    target = ledger.records / f"{TASK}.json"
    data = persisted.model_dump(mode="json")
    if mismatch == "project":
        data["project_id"] = PROJECT + 1
    elif mismatch == "issue":
        data["issue_iid"] = TASK + 1
    else:
        data["scope_binding"] = binding.model_dump(mode="json")
        assert data["project_id"] == PROJECT and data["issue_iid"] == TASK
        assert (
            data["scope_binding"] != persisted.model_dump(mode="json")["scope_binding"]
        )
    target.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(LedgerCorrupt):
        ledger.load_read_only(TASK)


def test_missing_historical_request_cannot_be_inferred_from_latest_note() -> None:
    binding = approved()
    data = binding.model_dump(mode="json")
    data.pop("request")
    with pytest.raises(ValidationError):
        runtime.ScopeApprovalBinding.model_validate(data)


@pytest.mark.parametrize(
    "change", ["criteria", "baseline", "binding-digest", "request-issue"]
)
def test_changed_reviewed_projection_cannot_inherit_original_host_decision(
    change: str,
) -> None:
    binding = approved()
    scope = binding.request.scope
    if change == "criteria":
        scope = scope.model_copy(
            update={
                "criteria": (
                    scope.criteria[0].model_copy(
                        update={"behavior": "Changed observable behavior"}
                    ),
                )
            }
        )
    elif change == "baseline":
        scope = scope.model_copy(update={"baseline_sha": "b" * 40})
    elif change == "request-issue":
        scope = scope.model_copy(update={"issue_iid": TASK + 1})
    mutated = (
        binding.model_copy(update={"scope_sha256": "f" * 64})
        if change == "binding-digest"
        else binding.model_copy(
            update={"request": binding.request.model_copy(update={"scope": scope})}
        )
    )
    fixture = SourceFixture()
    result = runtime.verify_scope(
        mutated, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state != "approved"
    assert result.reason == "scope_changed"


def test_new_scope_requires_new_retained_request_and_distinct_verified_event() -> None:
    binding = approved()
    stale_source = _changed_source("description", DESCRIPTION + " new revision")
    fixture = SourceFixture((stale_source,) * 2)
    stale = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert stale.state == "stale" and stale.reason == "source_changed"
    new_scope = binding.request.scope.model_copy(
        update={
            "scope_revision": 2,
            "source_description_sha256": _digest(stale_source.description),
        }
    )
    new_request = runtime.ScopeApprovalRequest(
        scope=new_scope,
        requested_revision=2,
        requested_description_sha256=_digest(stale_source.description),
        requested_at=NOW + timedelta(seconds=3),
        approval_note_id=NOTE + 1,
    )
    fresh_note = stale_source.notes[0].model_copy(
        update={
            "id": NOTE + 1,
            "created_at": NOW + timedelta(seconds=4),
            "updated_at": NOW + timedelta(seconds=4),
        }
    )
    fixture = SourceFixture(
        (stale_source.model_copy(update={"notes": (fresh_note,)}),) * 2
    )
    fixture.host_decision = replace(_decision(new_request), note_id=NOTE + 1)
    fresh = runtime.capture_scope(
        new_request, read_source=fixture.read, verify_event=fixture.verify
    )
    assert fresh.state == "approved" and fresh.event is not None
    assert binding.event is not None
    assert fresh.event.note_id != binding.event.note_id


def test_authenticated_later_withdrawal_is_observed_without_rewriting_approval_note() -> (
    None
):
    binding = approved()
    source = observation()
    withdrawal = source.notes[0].model_copy(
        update={
            "id": NOTE + 1,
            "body": "I withdraw this scope approval.",
            "created_at": NOW + timedelta(seconds=5),
            "updated_at": NOW + timedelta(seconds=5),
        }
    )
    fixture = SourceFixture(
        (source.model_copy(update={"notes": (*source.notes, withdrawal)}),) * 2
    )
    fixture.host_decision = replace(
        _decision(binding.request), decision="revoke", note_id=NOTE + 1
    )
    result = runtime.verify_scope(
        binding, read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state == "revoked" and result.reason == "revoked"


@pytest.mark.parametrize("meaning", ["conditional", "unrelated"])
def test_conditional_or_unrelated_reply_requires_host_meaning_not_magic_text(
    meaning: str,
) -> None:
    approved()
    source = _changed_source(
        "note.body",
        "Scope approved if another decision is made."
        if meaning == "conditional"
        else "Scope approved is a quoted example.",
    )
    fixture = SourceFixture((source,) * 2)
    fixture.host_decision = replace(_decision(request()), decision="unknown")
    result = runtime.capture_scope(
        request(), read_source=fixture.read, verify_event=fixture.verify
    )
    assert result.state == "unknown" and result.reason == "ambiguous_reply"
