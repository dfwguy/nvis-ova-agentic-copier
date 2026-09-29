# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Project-wide admission behavior for Judge timing observations."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from agent_work.cli import main
from agent_work.ledger import LockBusy, XdgProjectLock, project_state_path
from agent_work.repository import CommandResult
from agent_work.timing_admission import TimingAdmissionLease


def test_waiting_window_exposes_only_live_exact_project_request(tmp_path: Path) -> None:
    """Contention files and inherited environment cannot impersonate a live waiter."""
    from agent_work import timing_admission

    observe = getattr(timing_admission, "observe_waiting_interactive_validation", None)
    assert callable(observe), "live interactive validation observer is required"
    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    now = datetime(2026, 9, 27, tzinfo=UTC)
    elapsed = 0.0
    direct = timing_admission.project_timing_admission(
        host=host,
        project_id=project_id,
        environment=environment,
        wait_seconds=0.0,
        monotonic_clock=lambda: elapsed,
        sleeper=lambda _seconds: None,
    )
    direct.__enter__()
    observed: list[object] = []

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        request = observe(
            host=host,
            project_id=project_id,
            environment=environment,
            utc_clock=lambda: now,
        )
        assert request is not None
        assert request.host == host
        assert request.project_id == project_id
        assert request.request_id
        assert request.requested_at <= now < request.expires_at
        assert not timing_admission.mark_interactive_validation_yield(
            request, at=request.expires_at, issue_iid=12
        )
        observed.append(request)
        assert (
            observe(
                host=host,
                project_id=277,
                environment=environment,
                utc_clock=lambda: now,
            )
            is None
        )
        assert (
            observe(
                host=host,
                project_id=project_id,
                environment=environment,
                utc_clock=lambda: now + timedelta(hours=1),
            )
            is None
        )
        elapsed += seconds
        direct.__exit__(None, None, None)

    assert (
        timing_admission.run_project_timing_window(
            command=("true",),
            run_child=lambda _command, _environment: 0,
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.1,
            monotonic_clock=lambda: elapsed,
            sleeper=sleep,
            utc_clock=lambda: now,
        )
        == 0
    )
    assert observed
    assert (
        observe(
            host=host,
            project_id=project_id,
            environment={**environment, "AGENT_WORK_TIMING_NONCE": "forged"},
            utc_clock=lambda: now,
        )
        is None
    )


def test_unavailable_interactive_window_has_bounded_wait_and_clears_request(
    tmp_path: Path,
) -> None:
    """A timed-out waiter cannot keep yielding the controller indefinitely."""
    from agent_work import timing_admission

    observe = getattr(timing_admission, "observe_waiting_interactive_validation", None)
    assert callable(observe), "live interactive validation observer is required"
    environment = {"XDG_STATE_HOME": str(tmp_path)}
    now = datetime(2026, 9, 27, tzinfo=UTC)
    elapsed = 0.0
    admission = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: elapsed,
        "sleeper": lambda _seconds: None,
    }
    seen: list[str] = []

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        request = observe(
            host=admission["host"],
            project_id=admission["project_id"],
            environment=environment,
            utc_clock=lambda: now + timedelta(seconds=elapsed),
        )
        assert request is not None
        seen.append(request.request_id)
        elapsed += seconds

    with (
        timing_admission.project_timing_admission(**admission),
        pytest.raises(timing_admission.TimingAdmissionBusy),
    ):
        timing_admission.run_project_timing_window(
            command=("true",),
            run_child=lambda _command, _environment: pytest.fail(
                "unavailable window started validation"
            ),
            host="gitlab.example.test",
            project_id=276,
            environment=environment,
            wait_seconds=0.1,
            monotonic_clock=lambda: elapsed,
            sleeper=sleep,
            utc_clock=lambda: now + timedelta(seconds=elapsed),
        )

    assert seen and len(set(seen)) == 1
    assert elapsed == pytest.approx(0.1)
    assert (
        observe(
            host="gitlab.example.test",
            project_id=276,
            environment=environment,
            utc_clock=lambda: now + timedelta(seconds=elapsed),
        )
        is None
    )


@pytest.mark.parametrize("held_boundary", ["project", "timing"])
def test_late_lock_release_cannot_start_validation_after_shared_deadline(
    tmp_path: Path, held_boundary: str
) -> None:
    """A lock released at the deadline is too late to start the child."""
    from agent_work import timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    elapsed = 0.0
    now = datetime(2026, 9, 27, tzinfo=UTC)
    if held_boundary == "project":
        held: AbstractContextManager[object] = XdgProjectLock(root / "project.lock")
    else:
        held = timing_admission.project_timing_admission(
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: elapsed,
            sleeper=lambda _seconds: None,
        )
    held.__enter__()
    released = False

    def sleep(seconds: float) -> None:
        nonlocal elapsed, released
        elapsed += seconds
        if elapsed >= 0.1 and not released:
            held.__exit__(None, None, None)
            released = True

    try:
        with pytest.raises(timing_admission.TimingAdmissionBusy):
            timing_admission.run_project_timing_window(
                command=("validation",),
                run_child=lambda _command, _environment: pytest.fail(
                    "validation started after the shared deadline"
                ),
                host=host,
                project_id=project_id,
                environment=environment,
                wait_seconds=0.1,
                monotonic_clock=lambda: elapsed,
                sleeper=sleep,
                utc_clock=lambda: now + timedelta(seconds=elapsed),
            )
    finally:
        if not released:
            held.__exit__(None, None, None)
    state = timing_admission.read_validation_admission(
        host=host, project_id=project_id, environment=environment
    )
    assert state is not None and state["status"] == "unavailable"
    assert state["wait_elapsed_seconds"] == pytest.approx(0.1)


def test_unconsumed_yielded_admission_survives_a_second_window(tmp_path: Path) -> None:
    """A second request cannot erase the first validation's pending resume proof."""
    from agent_work import timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    now = datetime(2026, 9, 27, tzinfo=UTC)
    held = XdgProjectLock(root / "project.lock")
    held.__enter__()
    elapsed = 0.0
    yielded_request: timing_admission.InteractiveValidationRequest | None = None

    def release_after_yield(seconds: float) -> None:
        nonlocal elapsed, yielded_request
        yielded_request = timing_admission.observe_waiting_interactive_validation(
            host=host,
            project_id=project_id,
            environment=environment,
            utc_clock=lambda: now,
        )
        assert yielded_request is not None
        assert timing_admission.mark_interactive_validation_yield(
            yielded_request, at=now, issue_iid=12
        )
        elapsed += seconds
        held.__exit__(None, None, None)

    try:
        first_exit = timing_admission.run_project_timing_window(
            command=("validation",),
            run_child=lambda _command, _environment: 0,
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.1,
            monotonic_clock=lambda: elapsed,
            sleeper=release_after_yield,
            utc_clock=lambda: now,
        )
    finally:
        held.__exit__(None, None, None)
    assert first_exit == 0
    first = timing_admission.read_validation_admission(
        host=host, project_id=project_id, environment=environment
    )
    assert first is not None and first["status"] == "complete"
    with pytest.raises(timing_admission.TimingAdmissionBusy):
        timing_admission.run_project_timing_window(
            command=("second",),
            run_child=lambda _command, _environment: pytest.fail(
                "second validation erased pending resume evidence"
            ),
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: elapsed,
            sleeper=lambda _seconds: None,
            utc_clock=lambda: now,
        )
    preserved = timing_admission.read_validation_admission(
        host=host, project_id=project_id, environment=environment
    )
    assert preserved is not None and preserved["request_id"] == first["request_id"]
    assert timing_admission.mark_validation_admission_resumed(
        preserved, at=now + timedelta(seconds=1), issue_iid=12
    )
    assert (
        timing_admission.run_project_timing_window(
            command=("later",),
            run_child=lambda _command, _environment: 0,
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: elapsed,
            sleeper=lambda _seconds: None,
            utc_clock=lambda: now + timedelta(seconds=2),
        )
        == 0
    )


def test_unavailable_admission_defers_new_window_until_reported(tmp_path: Path) -> None:
    """A failed wait remains reportable before a later window replaces it."""
    from agent_work import timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    root.mkdir(parents=True)
    now = datetime(2026, 9, 27, tzinfo=UTC)
    state_path = root / "interactive-validation-request.json"
    state_path.write_text(
        json.dumps(
            {
                "host": host,
                "project_id": project_id,
                "status": "unavailable",
                "request_id": "a" * 32,
                "request_at": now.isoformat(),
                "validation_finished_at": now.isoformat(),
                "wait_elapsed_seconds": 0.1,
            }
        ),
        encoding="utf-8",
    )
    arguments = {
        "command": ("validation",),
        "run_child": lambda _command, _environment: 0,
        "host": host,
        "project_id": project_id,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
        "utc_clock": lambda: now,
    }
    with pytest.raises(timing_admission.TimingAdmissionBusy):
        timing_admission.run_project_timing_window(**arguments)
    retained = timing_admission.read_validation_admission(
        host=host, project_id=project_id, environment=environment
    )
    assert retained is not None and retained["request_id"] == "a" * 32
    assert timing_admission.mark_validation_admission_resumed(
        retained, at=now + timedelta(seconds=1)
    )
    assert timing_admission.run_project_timing_window(**arguments) == 0


def test_malformed_completed_admission_is_explicit_and_preserved(
    tmp_path: Path,
) -> None:
    """Invalid retained timing evidence cannot be silently overwritten."""
    from agent_work import timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    root.mkdir(parents=True)
    path = root / "interactive-validation-request.json"
    payload = {
        "host": host,
        "project_id": project_id,
        "status": "complete",
        "request_id": "a" * 32,
        "issue_iid": 12,
        "request_at": "invalid-timestamp",
        "yield_at": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
        "validation_started_at": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
        "validation_finished_at": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
        "wait_elapsed_seconds": 0.1,
    }
    original = json.dumps(payload)
    path.write_text(original)

    with pytest.raises(timing_admission.ValidationAdmissionStateError):
        timing_admission.read_validation_admission(
            host=host, project_id=project_id, environment=environment
        )
    with pytest.raises(timing_admission.ValidationAdmissionStateError):
        timing_admission.run_project_timing_window(
            command=("validation",),
            run_child=lambda _command, _environment: pytest.fail(
                "invalid retained state started validation"
            ),
            host=host,
            project_id=project_id,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )
    assert path.read_text() == original


def test_stale_admission_reader_cannot_replace_consumed_resolution(
    tmp_path: Path,
) -> None:
    """Only the first exact request disposition may consume retained evidence."""
    from agent_work import timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    host = "gitlab.example.test"
    project_id = 276
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    root.mkdir(parents=True)
    now = datetime(2026, 9, 27, tzinfo=UTC)
    path = root / "interactive-validation-request.json"
    path.write_text(
        json.dumps(
            {
                "host": host,
                "project_id": project_id,
                "status": "complete",
                "request_id": "a" * 32,
                "issue_iid": 12,
                "request_at": now.isoformat(),
                "yield_at": now.isoformat(),
                "validation_started_at": now.isoformat(),
                "validation_finished_at": now.isoformat(),
                "wait_elapsed_seconds": 0.1,
            }
        )
    )
    stale = timing_admission.read_validation_admission(
        host=host, project_id=project_id, environment=environment
    )
    assert stale is not None
    assert timing_admission.mark_validation_admission_resolved(
        stale,
        at=now + timedelta(seconds=1),
        issue_iid=12,
        resolution="closed_without_resume",
    )
    consumed = path.read_text()

    assert not timing_admission.mark_validation_admission_resumed(
        stale, at=now + timedelta(seconds=2), issue_iid=12
    )
    assert not timing_admission.mark_validation_admission_resolved(
        stale,
        at=now + timedelta(seconds=3),
        issue_iid=12,
        resolution="ineligible_without_resume",
    )
    assert path.read_text() == consumed


@pytest.mark.parametrize("kill_mode", ("pid", "group"))
@pytest.mark.timeout(20)
def test_window_owner_death_keeps_project_lock_until_child_cleanup(
    tmp_path: Path,
    kill_mode: str,
) -> None:
    """The real supervisor keeps controller exclusion after the window dies."""
    host = "gitlab.example.test"
    project_id = 276
    environment = {"XDG_STATE_HOME": str(tmp_path)}
    root = project_state_path(host=host, project_id=project_id, environment=environment)
    started = tmp_path / "child-started"
    terminating = tmp_path / "child-terminating"
    release = tmp_path / "release-child"
    child_code = (
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "assert 'AGENT_WORK_PROJECT_LOCK_FD' not in os.environ\n"
        "def stop(_signum, _frame):\n"
        f"    Path({str(terminating)!r}).write_text('terminating')\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(started)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    parent_code = (
        "import sys, time\n"
        "from agent_work.timing_admission import run_project_timing_window, run_subprocess_child\n"
        "run_project_timing_window(\n"
        f"    command=(sys.executable, '-c', {child_code!r}),\n"
        "    run_child=run_subprocess_child,\n"
        f"    host={host!r}, project_id={project_id},\n"
        f"    environment={{'XDG_STATE_HOME': {str(tmp_path)!r}, 'PYTHONPATH': {source_path!r}}},\n"
        "    wait_seconds=0.0, monotonic_clock=time.monotonic, sleeper=time.sleep,\n"
        ")\n"
    )
    parent_environment = {
        **os.environ,
        "PYTHONPATH": source_path,
    }
    parent = subprocess.Popen(
        (sys.executable, "-c", parent_code),
        start_new_session=True,
        env=parent_environment,
    )

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5.0
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.exists()

    try:
        wait_for(started)
        if kill_mode == "group":
            os.killpg(parent.pid, signal.SIGTERM)
        else:
            parent.kill()
        parent.wait(timeout=5.0)
        wait_for(terminating)
        with pytest.raises(LockBusy), XdgProjectLock(root / "project.lock"):
            pytest.fail("controller acquired the project lock during child cleanup")
    finally:
        release.write_text("release")
        if started.exists() and not terminating.exists():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=5.0)

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with XdgProjectLock(root / "project.lock"):
                return
        except LockBusy:
            time.sleep(0.01)
    pytest.fail("project lock remained held after child cleanup")


def test_timing_admission_template_is_exact_portable_mirror() -> None:
    root = Path(__file__).resolve().parents[1]
    relative = Path("src/agent_work/timing_admission.py")
    assert (root / relative).read_bytes() == (root / "template" / relative).read_bytes()


def test_same_project_independent_openers_cannot_overlap_and_record_contention(
    tmp_path: Path,
) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
    )

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    now = 0.0

    def clock() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += max(seconds, 0.001)

    arguments = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.01,
        "monotonic_clock": clock,
        "sleeper": sleep,
    }
    with (
        project_timing_admission(**arguments),
        pytest.raises(TimingAdmissionBusy),
        project_timing_admission(**arguments),
    ):
        pytest.fail("the second opener entered the same project window")

    project_root = project_state_path(
        host="gitlab.example.test", project_id=276, environment=environment
    )
    observations = [
        path.read_text(errors="replace")
        for path in project_root.rglob("*")
        if path.is_file()
    ]
    assert any("contention" in item or "busy" in item for item in observations)


def test_successful_wait_records_contention(tmp_path: Path) -> None:
    from agent_work.timing_admission import project_timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    now = 0.0
    held = project_timing_admission(
        host="gitlab.example.test",
        project_id=276,
        environment=environment,
        wait_seconds=0.0,
        monotonic_clock=lambda: now,
        sleeper=lambda _seconds: None,
    )
    held.__enter__()

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds
        held.__exit__(None, None, None)

    with project_timing_admission(
        host="gitlab.example.test",
        project_id=276,
        environment=environment,
        wait_seconds=0.1,
        monotonic_clock=lambda: now,
        sleeper=sleep,
    ):
        pass

    project_root = project_state_path(
        host="gitlab.example.test", project_id=276, environment=environment
    )
    assert "contention" in (project_root / "timing-contention.json").read_text()


def test_repeated_contention_events_are_distinguishable(tmp_path: Path) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
    )

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    admission = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    project_root = project_state_path(
        host="gitlab.example.test", project_id=276, environment=environment
    )
    observed: list[str] = []
    with project_timing_admission(**admission):
        for event_id in ("first", "second"):
            with (
                pytest.raises(TimingAdmissionBusy),
                project_timing_admission(
                    host="gitlab.example.test",
                    project_id=276,
                    environment=environment,
                    wait_seconds=0.0,
                    monotonic_clock=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                    new_event_id=lambda event_id=event_id: event_id,
                ),
            ):
                pytest.fail("busy opener entered")
            observed.append(
                json.loads((project_root / "timing-contention.json").read_text())[
                    "event_id"
                ]
            )
    assert observed == ["first", "second"]


def test_inherited_lease_keeps_lock_after_owner_context_closes(tmp_path: Path) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
    )

    arguments = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": {"XDG_STATE_HOME": str(tmp_path)},
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    with project_timing_admission(**arguments) as lease:
        assert lease is not None
        child_copy = os.dup(lease.fd)
    try:
        with pytest.raises(TimingAdmissionBusy), project_timing_admission(**arguments):
            pytest.fail("another opener entered while child lease survived")
    finally:
        os.close(child_copy)
    with project_timing_admission(**arguments):
        pass


def test_session_scan_includes_distinct_process_groups(tmp_path: Path) -> None:
    from agent_work.timing_admission import _session_members

    for pid, group, session, state in (
        (11, 11, 11, "S"),
        (12, 12, 11, "S"),
        (13, 13, 13, "S"),
        (14, 14, 11, "Z"),
    ):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} (worker) {state} 1 {group} {session} 0")

    assert set(_session_members(11, proc_root=tmp_path)) == {11, 12}


@pytest.mark.parametrize(("exited", "expected"), ((False, 1), (True, 0)))
def test_supervisor_stops_child_before_return_on_owner_death_or_exit(
    exited: bool, expected: int
) -> None:
    from agent_work.timing_admission import _supervise_child

    events: list[str] = []

    class Child:
        pid = 11

        def poll(self) -> int | None:
            return 0 if exited else None

        def wait(self) -> int:
            events.append("wait")
            return 0

    result = _supervise_child(
        Child(),
        parent_closed=lambda: events.append("eof") or True,
        stop_child=lambda _child: events.append("stop"),
    )

    assert result == expected
    assert events[-1] == "stop"


def test_supervisor_keeps_lease_until_other_process_group_stops() -> None:
    from agent_work.timing_admission import _stop_child_session

    events: list[tuple[str, int]] = []
    live = {11, 12}
    now = 0.0

    class Child:
        pid = 11

        def poll(self) -> int | None:
            return None

        def wait(self) -> int:
            assert not live
            events.append(("wait", 11))
            return 0

    def send(pid: int, signum: int) -> None:
        events.append(("term" if signum == signal.SIGTERM else "kill", pid))
        if pid == 11 or signum == signal.SIGKILL:
            live.remove(pid)

    def sleep(_seconds: float) -> None:
        nonlocal now
        now = 30.0

    _stop_child_session(
        Child(),
        members=lambda _sid: tuple(sorted(live)),
        send_signal=send,
        monotonic_clock=lambda: now,
        sleeper=sleep,
    )

    assert events == [("term", 11), ("term", 12), ("kill", 12), ("wait", 11)]


def test_nested_window_requires_direct_lease(tmp_path: Path) -> None:
    from agent_work.timing_admission import run_project_timing_window

    def child(_command: tuple[str, ...], environment: Mapping[str, str]) -> int:
        with pytest.raises(RuntimeError, match="direct project lease"):
            run_project_timing_window(
                command=("true",),
                run_child=lambda _nested, _environment: 0,
                host="gitlab.example.test",
                project_id=276,
                environment=environment,
                wait_seconds=0.0,
                monotonic_clock=lambda: 0.0,
                sleeper=lambda _seconds: None,
            )
        return 0

    assert (
        run_project_timing_window(
            command=("true",),
            run_child=child,
            host="gitlab.example.test",
            project_id=276,
            environment={"XDG_STATE_HOME": str(tmp_path)},
            wait_seconds=0.0,
            monotonic_clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )
        == 0
    )


def test_different_project_ids_can_hold_timing_admission_together(
    tmp_path: Path,
) -> None:
    from agent_work.timing_admission import project_timing_admission

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    base = {
        "host": "gitlab.example.test",
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    with (
        project_timing_admission(project_id=276, **base),
        project_timing_admission(project_id=277, **base),
    ):
        pass


def test_failed_timing_window_releases_admission_for_next_opener(
    tmp_path: Path,
) -> None:
    from agent_work.timing_admission import project_timing_admission

    arguments = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": {"XDG_STATE_HOME": str(tmp_path)},
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    with (
        pytest.raises(RuntimeError, match="child failed"),
        project_timing_admission(**arguments),
    ):
        raise RuntimeError("child failed")

    with project_timing_admission(**arguments):
        pass


def test_window_grant_cannot_skip_another_project_lock(tmp_path: Path) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
        run_project_timing_window,
    )

    environment = {"XDG_STATE_HOME": str(tmp_path)}

    def admission(project_id: int, chosen_environment: Mapping[str, str]):
        return project_timing_admission(
            host="gitlab.example.test",
            project_id=project_id,
            environment=chosen_environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )

    def child(_command: tuple[str, ...], grant: Mapping[str, str]) -> int:
        with (
            admission(277, environment),
            pytest.raises(TimingAdmissionBusy),
            admission(277, grant),
        ):
            pytest.fail("project 276 grant skipped project 277 lock")
        return 0

    assert (
        run_project_timing_window(
            command=("true",),
            run_child=child,
            host="gitlab.example.test",
            project_id=276,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )
        == 0
    )


def test_window_keeps_project_admission_through_nested_validation_and_later_work(
    tmp_path: Path,
) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
        run_project_timing_window,
    )

    environment = {
        "XDG_STATE_HOME": str(tmp_path),
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "276",
    }
    admission = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    observed: list[str] = []
    removed: list[Path] = []
    hook_leases: list[TimingAdmissionLease | None] = []

    def independent_admission() -> AbstractContextManager[None]:
        return project_timing_admission(
            host="gitlab.example.test",
            project_id=276,
            environment=environment,
            wait_seconds=0.0,
            monotonic_clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )

    def child(command: tuple[str, ...], child_environment: Mapping[str, str]) -> int:
        assert command == ("bash", "-c", "true")
        with pytest.raises(TimingAdmissionBusy), independent_admission():
            pytest.fail("another timing window entered before nested validation")
        observed.append("blocked-before")

        def run(
            validation_command: tuple[str, ...], **_kwargs: object
        ) -> CommandResult:
            if validation_command == ("git", "rev-parse", "--git-dir"):
                return CommandResult(0, str(tmp_path / ".git"), "")
            if validation_command == ("git", "write-tree"):
                return CommandResult(0, "a" * 40, "")
            if validation_command == (
                sys.executable,
                "scripts/check_licenses.py",
                "--root",
                ".",
                "--policy",
                ".license-policy.toml",
                "--candidate-tree",
                "a" * 40,
                "--json",
            ):
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "schema_version": 1,
                            "outcome": "compliant",
                            "violations": [],
                        }
                    ),
                    "",
                )
            if validation_command == ("git", "hook", "run", "pre-commit"):
                observed.append("nested-hook")
            return CommandResult(0, "", "")

        nested_exit = cast(Callable[..., int], main)(
            ["validation", "run", "--candidate-tree", "a" * 40, "--apply"],
            environment=child_environment,
            run_command=run,
            candidate_gate=lambda _tree: CommandResult(0, "", ""),
            run_validation_hook=lambda command, lease: (
                hook_leases.append(lease) or run(command)
            ),
            read_validation_artifact=lambda _path: (
                b"<testsuite tests='1' failures='0' errors='0'/>"
            ),
            remove_validation_artifact=removed.append,
            write_stdout=lambda _message: None,
        )
        assert nested_exit == 0
        with pytest.raises(TimingAdmissionBusy), independent_admission():
            pytest.fail("another timing window entered after nested validation")
        observed.append("blocked-after")
        return 0

    result = run_project_timing_window(
        command=("bash", "-c", "true"),
        run_child=child,
        host="gitlab.example.test",
        project_id=276,
        environment=environment,
        wait_seconds=0.0,
        monotonic_clock=lambda: 0.0,
        sleeper=lambda _seconds: None,
    )

    with project_timing_admission(**admission):
        observed.append("released")
    assert result == 0
    assert observed == ["blocked-before", "nested-hook", "blocked-after", "released"]
    assert hook_leases == [None]
    assert removed == [
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
        Path("test-results-py311.xml"),
        Path("test-results-py314.xml"),
    ]


def test_supervised_hook_timeout_retains_lease_until_cleanup_finishes(
    tmp_path: Path,
) -> None:
    from agent_work.timing_admission import (
        TimingAdmissionBusy,
        project_timing_admission,
        run_supervised_command,
    )

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    admission = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }
    copied_read = -1

    class FakeSupervisor:
        returncode = 1
        calls = 0

        def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
            self.calls += 1
            if self.calls == 1:
                assert timeout == 1.0
                raise subprocess.TimeoutExpired(("hook",), 1.0)
            assert timeout is None
            assert os.read(copied_read, 1) == b""
            with (
                pytest.raises(TimingAdmissionBusy),
                project_timing_admission(
                    host="gitlab.example.test",
                    project_id=276,
                    environment=environment,
                    wait_seconds=0.0,
                    monotonic_clock=lambda: 0.0,
                    sleeper=lambda _seconds: None,
                ),
            ):
                pytest.fail("hook cleanup released the project lease")
            return "", ""

        def wait(self) -> int:
            assert self.calls == 2
            return self.returncode

    supervisor = FakeSupervisor()

    def spawn(command: tuple[str, ...], **kwargs: object) -> subprocess.Popen[str]:
        nonlocal copied_read
        assert command[3] == "--supervise"
        assert int(command[5]) in cast(tuple[int, ...], kwargs["pass_fds"])
        copied_read = os.dup(int(command[4]))
        return cast(subprocess.Popen[str], supervisor)

    try:
        with project_timing_admission(**admission) as lease:
            assert isinstance(lease, TimingAdmissionLease)
            with pytest.raises(subprocess.TimeoutExpired):
                run_supervised_command(
                    ("hook",),
                    lease=lease,
                    environment=environment,
                    timeout_seconds=1.0,
                    spawn_supervisor=spawn,
                )
    finally:
        if copied_read >= 0:
            os.close(copied_read)
    with project_timing_admission(**admission):
        pass


def test_supervised_hook_returns_captured_diagnostics(tmp_path: Path) -> None:
    from agent_work.timing_admission import (
        project_timing_admission,
        run_supervised_command,
    )

    environment = {"XDG_STATE_HOME": str(tmp_path)}
    admission = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": environment,
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }

    class FakeSupervisor:
        returncode = 7

        def communicate(self, *, timeout: float | None = None) -> tuple[str, str]:
            assert timeout is None
            return "hook output", "hook failure"

        def wait(self) -> int:
            return self.returncode

    def spawn(command: tuple[str, ...], **kwargs: object) -> subprocess.Popen[str]:
        assert command[-1] == "hook"
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        return cast(subprocess.Popen[str], FakeSupervisor())

    with project_timing_admission(**admission) as lease:
        assert isinstance(lease, TimingAdmissionLease)
        result = run_supervised_command(
            ("hook",),
            lease=lease,
            environment=environment,
            spawn_supervisor=spawn,
        )

    assert (result.returncode, result.stdout, result.stderr) == (
        7,
        "hook output",
        "hook failure",
    )
