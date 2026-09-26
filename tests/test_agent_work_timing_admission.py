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
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast

import pytest

from agent_work.cli import main
from agent_work.ledger import project_state_path
from agent_work.repository import CommandResult
from agent_work.timing_admission import TimingAdmissionLease


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

    arguments = {
        "host": "gitlab.example.test",
        "project_id": 276,
        "environment": {"XDG_STATE_HOME": str(tmp_path)},
        "wait_seconds": 0.0,
        "monotonic_clock": lambda: 0.0,
        "sleeper": lambda _seconds: None,
    }

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
        run_project_timing_window(command=("true",), run_child=child, **arguments) == 0
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

        nested_exit = main(
            ["validation", "run", "--candidate-tree", "a" * 40, "--apply"],
            environment=child_environment,
            run_command=run,
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
        **admission,
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
