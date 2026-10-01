# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Actual CLI and runtime store requirements evidence without factory authority."""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from agent_work import runtime
from agent_work.cli import main
from agent_work.domain import CommandRequest
from agent_work.gitlab import GitLabGateway
from agent_work.ledger import JsonLedger, LedgerError, LockBusy
from agent_work.models import CommandReport, LedgerRecord
from tests.test_agent_work_approved_scope import (
    AUTHOR,
    NOTE,
    NOW,
    PROJECT,
    SOURCE,
    TASK,
    SourceFixture,
    approved,
    record,
    request,
    scope_binding,
)
from tests.test_agent_work_scope_source import ApiFixture
from tests.test_agent_work_serial_runtime import FakeInvoker, FakeRepository


class ScopeRuntimeFixture:
    """Real caller chain with injected network, file, lock, and person boundaries."""

    def __init__(
        self, root: Path, *, verified: bool = True, existing: bool = True
    ) -> None:
        self.root = root
        self.api = ApiFixture(root)
        self.config = self.api.config
        self.ledger = JsonLedger(self.config.state_root, clock=lambda: NOW)
        self.initial: LedgerRecord | None = (
            self.ledger.persist(record(root)) if existing else None
        )
        self.plan = root / "scope.json"
        self.plan.write_text(request().model_dump_json(), encoding="utf-8")
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.locks: list[str] = []
        self.requests: list[CommandRequest] = []
        self.host = SourceFixture()
        self.repository = FakeRepository(self.config)
        self.log_path = root / "scope-runtime.log"
        self.logger = logging.getLogger(f"scope-fixture-{root.name}")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.handler = logging.FileHandler(self.log_path)
        self.logger.addHandler(self.handler)
        self.lock_failure = False
        self.on_lock: Callable[[], None] | None = None
        self.wired = runtime.RuntimeDependencies(
            config=self.config,
            ledger=self.ledger,
            gitlab=GitLabGateway(self.config, self.api.run),
            repository=self.repository,
            invoke=FakeInvoker(),
            lock=self.lock,
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
            sleep=lambda _delay: None,
            logger=self.logger,
            read_plan=self.read_plan,
            process_probe=lambda: "missing",
        )
        self.wired = replace(
            self.wired,
            scope_read_source=self.api.source,
            scope_verify_event=self.host.verify if verified else None,
        )

    @contextmanager
    def lock(self) -> Generator[None]:
        self.locks.append("enter")
        if self.lock_failure:
            raise LockBusy("scope lock unavailable")
        if self.on_lock is not None:
            self.on_lock()
        try:
            yield
        finally:
            self.locks.append("exit")

    def read_plan(self, path: Path) -> bytes:
        assert path == self.plan
        return path.read_bytes()

    def dispatch(self, command: CommandRequest, *, apply: bool) -> CommandReport:
        self.requests.append(command)
        return runtime.dispatch(command, apply=apply, dependencies=self.wired)

    def cli(
        self,
        operation: str,
        *,
        apply: bool = False,
        as_json: bool = True,
        debug: bool = False,
        issue: int = TASK,
    ) -> int:
        argv = ["scope", operation, "--issue", str(issue)]
        if operation == "capture":
            argv.extend(["--scope-file", str(self.plan)])
        if apply:
            argv.append("--apply")
        if as_json:
            argv.append("--json")
        if debug:
            argv.append("--debug")
        return main(
            argv,
            dispatch=self.dispatch,
            environment={},
            current_checkout=lambda: self.root,
            home_directory=lambda: self.root,
            run_command=self.api.run,
            write_stdout=self.stdout.append,
            write_stderr=self.stderr.append,
        )

    def close(self) -> None:
        self.handler.close()
        self.logger.removeHandler(self.handler)


def _caller_control(root: Path) -> ScopeRuntimeFixture:
    """Check actual CLI-to-runtime-to-authenticated-adapter positive control."""
    fixture = ScopeRuntimeFixture(root)
    assert fixture.cli("capture") == 0
    report = CommandReport.model_validate_json(fixture.stdout[-1])
    binding = scope_binding(report)
    assert binding is not None and binding.state == "approved"
    assert (
        fixture.requests[-1].command == "scope.capture"
        and fixture.requests[-1].plan_file == fixture.plan
    )
    assert fixture.ledger.load_read_only(TASK) == fixture.initial
    assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    assert fixture.api.calls and all(
        command[5] == "GET" for command in fixture.api.calls
    )
    fixture.stdout.clear()
    fixture.stderr.clear()
    return fixture


@pytest.mark.parametrize("operation", ["preview", "apply", "inspect"])
def test_real_scope_cli_only_applied_capture_binds_existing_record_without_lifecycle_authority(
    tmp_path: Path, operation: str
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        initial = fixture.initial
        assert initial is not None
        if operation == "inspect":
            fixture.ledger.persist(runtime.bind_scope_record(initial, approved()))
        before = fixture.ledger.load_read_only(TASK)
        assert before is not None
        raw_before = (fixture.ledger.records / f"{TASK}.json").read_bytes()
        status = fixture.cli(
            "inspect" if operation == "inspect" else "capture",
            apply=operation == "apply",
        )
        assert status == 0
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        binding = scope_binding(report)
        assert binding is not None and binding.state == "approved"
        after = fixture.ledger.load_read_only(TASK)
        assert after is not None
        assert after.state == before.state and after.phase == before.phase
        assert (
            after.merge_request_iid == before.merge_request_iid
            and after.publication_sha == before.publication_sha
        )
        if operation == "apply":
            assert (
                scope_binding(after) is not None
                and after.revision == before.revision + 1
            )
            assert fixture.locks == ["enter", "exit"]
        else:
            assert (fixture.ledger.records / f"{TASK}.json").read_bytes() == raw_before
        assert all(command[5] == "GET" for command in fixture.api.calls)
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize("operation", ["capture", "inspect"])
@pytest.mark.parametrize("as_json", [False, True])
def test_missing_host_verifier_is_unknown_in_actual_caller_and_never_persists(
    tmp_path: Path, operation: str, *, as_json: bool
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        initial = fixture.initial
        assert initial is not None
        if operation == "inspect":
            fixture.ledger.persist(runtime.bind_scope_record(initial, approved()))
        fixture.wired = replace(fixture.wired, scope_verify_event=None)
        raw = (fixture.ledger.records / f"{TASK}.json").read_bytes()
        assert (
            fixture.cli(operation, apply=operation == "capture", as_json=as_json) == 1
        )
        if as_json:
            report = CommandReport.model_validate_json(fixture.stdout[-1])
            binding = scope_binding(report)
            assert binding is not None and binding.state == "unknown"
            assert binding.reason == "human_verifier_unavailable"
        else:
            assert fixture.stdout == [] and fixture.stderr
        assert (fixture.ledger.records / f"{TASK}.json").read_bytes() == raw
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize("operation", ["capture", "inspect"])
def test_actual_scope_caller_never_initializes_missing_workflow_record(
    tmp_path: Path, operation: str
) -> None:
    approved()
    fixture = ScopeRuntimeFixture(tmp_path, existing=False)
    try:
        assert fixture.cli(operation, apply=operation == "capture") == 1
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        assert scope_binding(report) is None
        assert report.outcome == "unknown" and report.stop_reason == "invalid_record"
        assert not fixture.ledger.records.exists()
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("identity", "identity_mismatch"),
        ("file", "invalid_input"),
        ("api", "source_unavailable"),
    ],
)
def test_applied_scope_negative_input_never_partially_updates_existing_workflow(
    tmp_path: Path, failure: str, reason: str
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        raw = (fixture.ledger.records / f"{TASK}.json").read_bytes()
        if failure == "identity":
            bound = request()
            wrong = bound.scope.model_copy(update={"issue_iid": TASK + 1})
            fixture.plan.write_text(
                bound.model_copy(update={"scope": wrong}).model_dump_json(),
                encoding="utf-8",
            )
        elif failure == "file":
            fixture.plan.write_text("synthetic-private-token", encoding="utf-8")
        else:
            fixture.api.failure = "nonzero"
        assert fixture.cli("capture", apply=True) == 1
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        binding = scope_binding(report)
        if failure == "file":
            assert binding is None
            assert report.outcome == "unknown" and report.stop_reason == reason
        else:
            assert binding is not None and binding.state != "approved"
            assert binding.reason == reason
        assert (fixture.ledger.records / f"{TASK}.json").read_bytes() == raw
        assert "synthetic-private-token" not in "".join(fixture.stdout + fixture.stderr)
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize("failure", ["lock", "persist"])
def test_applied_scope_lock_or_persistence_failure_retains_original_record(
    tmp_path: Path, failure: str
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        raw = (fixture.ledger.records / f"{TASK}.json").read_bytes()
        if failure == "lock":
            fixture.lock_failure = True
        else:

            class FailingLedger(JsonLedger):
                def persist(self, record: LedgerRecord) -> LedgerRecord:
                    del record
                    raise LedgerError("synthetic-private-token")

            fixture.wired = replace(
                fixture.wired,
                ledger=FailingLedger(fixture.config.state_root, clock=lambda: NOW),
            )
        assert fixture.cli("capture", apply=True) in {1, 75}
        assert (fixture.ledger.records / f"{TASK}.json").read_bytes() == raw
        assert "synthetic-private-token" not in "".join(fixture.stdout + fixture.stderr)
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize("debug", [False, True])
def test_scope_failure_uses_safe_local_logs_and_no_raw_debug_traceback(
    tmp_path: Path, *, debug: bool
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        fixture.api.failure = "nonzero"
        assert fixture.cli("capture", as_json=False, debug=debug) == 1
        assert fixture.stdout == [] and fixture.stderr
        fixture.handler.flush()
        text = fixture.log_path.read_text()
        assert "synthetic-private-token" not in text + "".join(fixture.stderr)
        assert text
        assert fixture.ledger.load_read_only(TASK) == fixture.initial
    finally:
        fixture.close()


@pytest.mark.parametrize(
    "argv",
    [
        ["scope", "capture", "--help"],
        ["scope", "inspect", "--help"],
        ["scope", "capture", "--issue", "0", "--scope-file", "missing"],
        ["scope", "capture", "--issue", "360", "--human-approved"],
    ],
)
def test_scope_cli_help_and_unsupported_authority_selectors_never_dispatch(
    argv: list[str],
) -> None:
    approved()
    calls: list[CommandRequest] = []

    def dispatch(command: CommandRequest, *, apply: bool) -> CommandReport:
        del apply
        calls.append(command)
        raise AssertionError("Invalid or help-only command cannot dispatch")

    with pytest.raises(SystemExit) as error:
        main(argv, dispatch=dispatch, environment={})
    assert error.value.code == (0 if "--help" in argv else 2)
    assert calls == []


def _candidate_control(
    fixture: ScopeRuntimeFixture, freshness: str, *, task: int = TASK
) -> runtime.ScopeApprovalBinding:
    """Prove every candidate is independently approved before terminal history."""
    bound = request()
    new_revision = freshness in {"revision-only", "both"}
    new_event = freshness in {"event-only", "both"}
    scope = bound.scope.model_copy(
        update={"scope_revision": 2 if new_revision else 1, "issue_iid": task}
    )
    note_id = NOTE + 1 if new_event else NOTE
    requested_at = NOW + timedelta(microseconds=500_000) if freshness != "old" else NOW
    bound = runtime.ScopeApprovalRequest(
        scope=scope,
        requested_revision=scope.scope_revision,
        requested_description_sha256=scope.source_description_sha256,
        requested_at=requested_at,
        approval_note_id=note_id,
    )
    notes_endpoint = f"projects/{PROJECT}/issues/{SOURCE}/notes"
    notes = fixture.api.responses[notes_endpoint]
    assert isinstance(notes, list) and isinstance(notes[0], dict)
    event_at = NOW + timedelta(seconds=2 if new_event else 1)
    fixture.api.responses[notes_endpoint] = [
        {
            **notes[0],
            "id": note_id,
            "created_at": event_at.isoformat(),
            "updated_at": event_at.isoformat(),
        }
    ]
    fixture.host.host_decision = runtime.HostScopeDecision(
        author_kind="human",
        decision="approve",
        author_id=AUTHOR,
        note_id=note_id,
        reviewed_revision=scope.scope_revision,
        reviewed_description_sha256=scope.source_description_sha256,
        reviewed_scope_sha256=runtime.canonical_scope_digest(scope),
    )
    fixture.plan.write_text(bound.model_dump_json(), encoding="utf-8")
    result = runtime.capture_scope(
        bound, read_source=fixture.api.source, verify_event=fixture.host.verify
    )
    assert result.state == "approved" and result.reason == "approved"
    assert result.event is not None and result.event.note_id == note_id
    assert result.scope_sha256 == runtime.canonical_scope_digest(scope)
    return result


def _store_binding(
    fixture: ScopeRuntimeFixture, binding: runtime.ScopeApprovalBinding
) -> LedgerRecord:
    """Persist validated evidence into the existing matching workflow."""
    initial = fixture.initial
    assert initial is not None
    data = initial.model_dump(mode="json")
    data["scope_binding"] = binding.model_dump(mode="json")
    stored = fixture.ledger.persist(LedgerRecord.model_validate(data))
    assert fixture.ledger.load_read_only(stored.issue_iid) == stored
    return stored


@pytest.mark.parametrize("state", ["stale", "revoked"])
@pytest.mark.parametrize("freshness", ["old", "revision-only", "event-only", "both"])
@pytest.mark.parametrize("apply", [False, True])
def test_actual_capture_preserves_terminal_history_until_request_revision_and_event_are_both_fresh(
    tmp_path: Path, state: str, freshness: str, *, apply: bool
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        prior = approved().model_copy(
            update={
                "state": state,
                "reason": "revoked" if state == "revoked" else "stale_binding",
            }
        )
        candidate = _candidate_control(fixture, freshness)
        stored = _store_binding(fixture, prior)
        raw = (fixture.ledger.records / f"{TASK}.json").read_bytes()
        reason = "revoked" if state == "revoked" else "stale_binding"
        if freshness == "both":
            bound_record = runtime.bind_scope_record(stored, candidate)
            assert scope_binding(bound_record) == candidate
        else:
            with pytest.raises(runtime.ScopeError) as error:
                runtime.bind_scope_record(stored, candidate)
            assert error.value.reason == reason
        fixture.stdout.clear()
        fixture.stderr.clear()
        fixture.locks.clear()
        result = fixture.cli("capture", apply=apply)
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        reported = scope_binding(report)
        assert reported is not None
        if freshness == "both":
            assert result == 0 and reported.state == "approved"
            assert report.applied is apply
            after = fixture.ledger.load_read_only(TASK)
            assert after is not None
            assert scope_binding(after) == (candidate if apply else prior)
            assert after.state == stored.state and after.phase == stored.phase
        else:
            assert result == 1 and report.applied is False
            assert reported.state != "approved" and reported.reason == reason
            assert (fixture.ledger.records / f"{TASK}.json").read_bytes() == raw
            assert not (fixture.ledger.records / f"{TASK + 1}.json").exists()
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
        assert all(command[5] == "GET" for command in fixture.api.calls)
    finally:
        fixture.close()


@pytest.mark.parametrize(
    ("operation", "when"),
    [
        ("capture-preview", "initial"),
        ("capture-apply", "initial"),
        ("inspect", "initial"),
        ("capture-apply", "locked-reload"),
    ],
)
def test_actual_scope_selector_refuses_other_valid_task_record_before_external_authority(
    tmp_path: Path, operation: str, when: str
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        original_binding = approved()
        matching = _store_binding(fixture, original_binding)
        fixture.stdout.clear()
        fixture.stderr.clear()
        assert fixture.cli("inspect") == 0
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        control = scope_binding(report)
        assert control is not None and control.state == "approved"
        other_binding = _candidate_control(fixture, "old", task=TASK + 1)
        other_data = matching.model_dump(mode="json")
        other_data["issue_iid"] = TASK + 1
        other_data["scope_binding"] = other_binding.model_dump(mode="json")
        other = LedgerRecord.model_validate(other_data)
        fixture.ledger.persist(other)
        assert fixture.ledger.load_read_only(TASK + 1) is not None
        fixture.stdout.clear()
        fixture.stderr.clear()
        assert fixture.cli("capture", issue=TASK + 1) == 0
        other_report = CommandReport.model_validate_json(fixture.stdout[-1])
        other_control = scope_binding(other_report)
        assert other_control is not None and other_control.state == "approved"
        assert other_control.request.scope.issue_iid == TASK + 1
        assert fixture.cli("inspect", issue=TASK + 1) == 0
        target = fixture.ledger.records / f"{TASK}.json"
        replacement = other.model_dump_json().encode()
        if operation == "inspect":
            # The selected stored record is valid on its own, including its bound request.
            fixture.plan.write_text(
                other_binding.request.model_dump_json(), encoding="utf-8"
            )
        if when == "initial":
            target.write_bytes(replacement)
        else:
            # Apply loads a matching record, then sees a valid different task under the lock.
            fixture.plan.write_text(request().model_dump_json(), encoding="utf-8")
            fixture.host.host_decision = None

            def substitute_record() -> None:
                target.write_bytes(replacement)

            fixture.on_lock = substitute_record
        raw_before = target.read_bytes()
        other_before = (fixture.ledger.records / f"{TASK + 1}.json").read_bytes()
        fixture.api.calls.clear()
        fixture.stdout.clear()
        fixture.stderr.clear()
        host_calls: list[int] = []

        def verify(
            bound: runtime.ScopeApprovalRequest, source: runtime.ScopeSourceObservation
        ) -> runtime.HostScopeDecision:
            host_calls.append(bound.scope.issue_iid)
            return fixture.host.verify(bound, source)

        fixture.wired = replace(fixture.wired, scope_verify_event=verify)
        status = fixture.cli(
            "inspect" if operation == "inspect" else "capture",
            apply=operation == "capture-apply",
        )
        assert status == 1
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        assert report.outcome == "unknown" and report.stop_reason == "identity_mismatch"
        binding = scope_binding(report)
        assert binding is None or binding.state != "approved"
        assert fixture.api.calls == [] and host_calls == []
        assert target.read_bytes() == (
            replacement if when == "locked-reload" else raw_before
        )
        assert (
            fixture.ledger.records / f"{TASK + 1}.json"
        ).read_bytes() == other_before
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()


@pytest.mark.parametrize("context", ["host", "project", "bound-task"])
def test_actual_inspect_refuses_mismatched_configured_or_bound_identity_before_source(
    tmp_path: Path, context: str
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        _store_binding(fixture, approved())
        fixture.stdout.clear()
        assert fixture.cli("inspect") == 0
        control = CommandReport.model_validate_json(fixture.stdout[-1])
        binding = scope_binding(control)
        assert binding is not None and binding.state == "approved"
        if context == "host":
            fixture.wired = replace(
                fixture.wired,
                config=fixture.config.model_copy(update={"host": "other.example.test"}),
            )
        elif context == "project":
            fixture.wired = replace(
                fixture.wired,
                config=fixture.config.model_copy(update={"project_id": PROJECT + 1}),
            )
        else:
            # A standalone-valid task361 record and binding are placed under selector360.
            other = _candidate_control(fixture, "old", task=TASK + 1)
            data = record(tmp_path).model_dump(mode="json")
            data.update(
                {"issue_iid": TASK + 1, "scope_binding": other.model_dump(mode="json")}
            )
            valid = LedgerRecord.model_validate(data)
            assert runtime.bind_scope_record(valid, other).issue_iid == TASK + 1
            fixture.ledger.persist(valid)
            assert fixture.cli("inspect", issue=TASK + 1) == 0
            fixture.stdout.clear()
            (fixture.ledger.records / f"{TASK}.json").write_text(
                valid.model_dump_json(), encoding="utf-8"
            )
        target = fixture.ledger.records / f"{TASK}.json"
        raw = target.read_bytes()
        fixture.api.calls.clear()
        fixture.stdout.clear()
        fixture.stderr.clear()
        assert fixture.cli("inspect") == 1
        report = CommandReport.model_validate_json(fixture.stdout[-1])
        assert report.outcome == "unknown" and report.stop_reason == "identity_mismatch"
        reported = scope_binding(report)
        assert reported is None or reported.state != "approved"
        assert fixture.api.calls == [] and target.read_bytes() == raw
        assert fixture.locks == []
    finally:
        fixture.close()


def test_inspect_apply_is_harmless_compatibility_with_same_readonly_report(
    tmp_path: Path,
) -> None:
    fixture = _caller_control(tmp_path)
    try:
        _store_binding(fixture, approved())
        target = fixture.ledger.records / f"{TASK}.json"
        before = target.read_bytes()
        assert fixture.cli("inspect") == 0
        ordinary = CommandReport.model_validate_json(fixture.stdout[-1])
        binding = scope_binding(ordinary)
        assert binding is not None and binding.state == "approved"
        fixture.stdout.clear()
        fixture.stderr.clear()
        assert fixture.cli("inspect", apply=True) == 0
        compatible = CommandReport.model_validate_json(fixture.stdout[-1])
        assert compatible == ordinary and compatible.applied is False
        assert target.read_bytes() == before and fixture.locks == []
        assert fixture.repository.prepares == 0 and fixture.repository.pushes == 0
    finally:
        fixture.close()
