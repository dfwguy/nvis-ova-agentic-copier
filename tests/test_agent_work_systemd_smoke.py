# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the isolated user-systemd smoke activation."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from agent_work import systemd_smoke as smoke_module
from agent_work.cli import main
from agent_work.models import AgentWorkConfig
from agent_work.repository import CommandResult
from agent_work.systemd_smoke import (
    SystemdSmokeDependencies,
    SystemdSmokeError,
    SystemdSmokeReport,
    _prove_children,
    _read_rollouts,
    run_systemd_smoke,
)
from agent_work.worker import (
    WorkerResultBrokerClientRequest,
    submit_worker_result_to_broker,
)


@pytest.mark.parametrize(
    ("report_is_accepted", "expected_status"),
    [(True, 0), (False, 1)],
)
def test_smoke_wrapper_validates_one_report_and_exact_cli_arguments(
    project_root: Path,
    tmp_path: Path,
    *,
    report_is_accepted: bool,
    expected_status: int,
) -> None:
    """The wrapper runs the applied JSON smoke command and rejects a bad report."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git_environment = os.environ | {
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        ["git", "init", "--initial-branch", "main", "--template="],
        check=True,
        capture_output=True,
        cwd=checkout,
        env=git_environment,
        text=True,
    )
    (checkout / ".gitignore").write_text(".envrc\n")
    (checkout / ".envrc").write_text("# local\n")
    (checkout / "src").mkdir()
    shutil.copy2(
        project_root / "src" / "agent-work-git-credential.sh",
        checkout / "src" / "agent-work-git-credential.sh",
    )
    for arguments in (
        ("add", "--all"),
        (
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "initial",
        ),
        ("switch", "-c", "agent/work-base"),
        ("remote", "add", "origin", "https://gitlab.example.test/group/project.git"),
    ):
        subprocess.run(
            ["git", *arguments],
            check=True,
            capture_output=True,
            cwd=checkout,
            env=git_environment,
            text=True,
        )
    commands = tmp_path / "commands"
    commands.mkdir()
    report = json.dumps(
        {
            "schema_version": 1,
            "command": "loop.smoke",
            "applied": True,
            "outcome": "passed",
            "broker_accepted": report_is_accepted,
        },
        separators=(",", ":"),
    )
    uv_body = (
        'printf "%s\\n" "$@" > "$SMOKE_UV_ARGS"\n'
        'if [ "$AGENT_WORK_UV" != "$EXPECTED_UV" ]; then exit 73; fi\n'
        'if [ "$*" != "run agent-work loop smoke --apply --json" ]; then exit 74; fi\n'
        f"printf '%s\\n' '{report}'"
    )
    for name, body in (
        ("uv", uv_body),
        (
            "direnv",
            'shift 2\nAGENT_WORK_UV="$DIRENV_UV_OVERRIDE"\nexport AGENT_WORK_UV\nexec "$@"',
        ),
        ("agent", "exit 0"),
        ("glab", "exit 0"),
    ):
        command = commands / name
        command.write_text(f"#!/bin/sh\n{body}\n")
        command.chmod(0o755)
    environment = os.environ | {
        "AGENT_WORK_ENV_VERSION": "1",
        "AGENT_WORK_CHECKOUT": str(checkout),
        "AGENT_WORK_BASE_BRANCH": "agent/work-base",
        "AGENT_WORK_FACTORY_USERNAME": "factory-bot",
        "AGENT_WORK_UV": str(commands / "uv"),
        "EXPECTED_UV": str(commands / "uv"),
        "DIRENV_UV_OVERRIDE": "/untrusted/direnv-uv",
        "AGENT_WORK_AGENT": "codex",
        "AGENT_WORK_AGENT_PATH": str(commands / "agent"),
        "AGENT_WORK_DIRENV": str(commands / "direnv"),
        "AGENT_WORK_GLAB": str(commands / "glab"),
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "123",
        "AGENT_WORK_GIT": str(shutil.which("git")),
        "AGENT_WORK_JQ": str(shutil.which("jq")),
        "AGENT_WORK_FLOCK": str(shutil.which("flock")),
        "AGENT_WORK_TIMEOUT": str(shutil.which("timeout")),
        "SMOKE_UV_ARGS": str(tmp_path / "uv-args"),
    }

    result = subprocess.run(
        [
            "bash",
            str(project_root / "scripts" / "agent-work-systemd-run.sh"),
            "--smoke",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert (tmp_path / "uv-args").read_text().splitlines() == [
        "run",
        "agent-work",
        "loop",
        "smoke",
        "--apply",
        "--json",
    ]
    assert result.returncode == expected_status, result.stderr
    if report_is_accepted:
        assert result.stdout == f"{report}\n"
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "did not emit one passed loop.smoke JSON report" in result.stderr


def test_systemd_smoke_source_mirrors_template(project_root: Path) -> None:
    """The generated smoke implementation uses the tested root source bytes."""
    relative = Path("src/agent_work/systemd_smoke.py")
    assert (project_root / relative).read_bytes() == (
        project_root / "template" / relative
    ).read_bytes()


def test_checkout_broker_client_uses_configured_absolute_uv(
    project_root: Path, tmp_path: Path
) -> None:
    """The smoke child executes the service's configured UV, not a PATH lookup."""
    checkout = tmp_path / "checkout"
    client = checkout / "bin" / "agent-work-from-checkout"
    client.parent.mkdir(parents=True)
    shutil.copy2(project_root / "bin" / "agent-work-from-checkout", client)
    client.chmod(0o700)
    configured_uv = tmp_path / "configured-uv"
    configured_uv.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    configured_uv.chmod(0o700)
    path_uv = tmp_path / "uv"
    path_uv.write_text("#!/usr/bin/env bash\nexit 97\n")
    path_uv.chmod(0o700)

    result = subprocess.run(
        [str(client), "worker-result", "build", "--apply"],
        check=False,
        capture_output=True,
        env={
            "AGENT_WORK_UV": str(configured_uv),
            "PATH": f"{tmp_path}:/usr/bin",
            "HOME": str(tmp_path),
        },
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "run",
        "--project",
        str(checkout),
        "agent-work",
        "worker-result",
        "build",
        "--apply",
    ]


def _dependencies(
    run_command: Callable[..., CommandResult],
    *,
    codex_home: Path = Path("/home/service/.codex"),
    temporary_directory: Path | None = None,
    read_rollout_metadata: Callable[[Path], Iterable[object]] | None = None,
) -> SystemdSmokeDependencies:
    checkout = (temporary_directory or Path("/srv")) / "agent-work"
    client = checkout / "bin" / "agent-work-from-checkout"
    if temporary_directory is not None:
        client.parent.mkdir(parents=True, exist_ok=True)
        client.write_text("#!/usr/bin/env bash\nexit 0\n")
        client.chmod(0o700)
    return SystemdSmokeDependencies(
        run_command=run_command,
        environment={
            "AGENT_WORK_AGENT": "codex",
            "AGENT_WORK_AGENT_PATH": "/opt/codex",
            "AGENT_WORK_GIT": str(shutil.which("git")),
            "AGENT_WORK_SECRET": "do-not-leak",
        },
        checkout=checkout,
        timeout_seconds=60,
        codex_home=codex_home,
        temporary_directory=temporary_directory,
        read_rollout_metadata=read_rollout_metadata,
    )


def _owner_codex_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create owner-only source credentials that a private smoke home may link."""
    owner_home = tmp_path / "owner-codex"
    owner_home.mkdir(mode=0o700)
    auth = owner_home / "auth.json"
    config = owner_home / "config.toml"
    auth.write_text('{"token":"unchanged"}\n')
    config.write_text("model = 'unchanged'\n")
    auth.chmod(0o600)
    config.chmod(0o600)
    return owner_home, auth, config


def _smoke_config(tmp_path: Path) -> AgentWorkConfig:
    """Return the minimal discovered configuration for a CLI smoke boundary."""
    return AgentWorkConfig(
        host="gitlab.example.test",
        project_id=1,
        checkout=tmp_path / "checkout",
        state_root=tmp_path / "state",
        git_path=tmp_path / "git",
        glab_path=tmp_path / "glab",
        base_branch="agent/work-base",
    )


def test_smoke_preview_has_a_stable_harmless_json_schema() -> None:
    """Preview reports its scope without opening a broker or subprocess."""
    stdout: list[str] = []

    assert main(["loop", "smoke", "--json"], write_stdout=stdout.append) == 0
    assert [json.loads(item) for item in stdout] == [
        {
            "schema_version": 1,
            "command": "loop.smoke",
            "applied": False,
            "outcome": "preview",
        }
    ]


def test_smoke_help_describes_preview_and_bounded_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Smoke help reflects its read-only default and bounded error output."""
    with pytest.raises(SystemExit) as error:
        main(["loop", "smoke", "--help"])

    help_text = capsys.readouterr().out
    assert error.value.code == 0
    assert "omit for a read-only preview" in help_text
    assert "smoke keeps bounded diagnostics" in " ".join(help_text.split())


def test_smoke_apply_runs_the_boundary_and_emits_accepted_json(tmp_path: Path) -> None:
    """Applied smoke delegates to the isolated boundary instead of fabricating success."""
    stdout: list[str] = []
    calls: list[SystemdSmokeDependencies] = []

    def run_smoke(*, dependencies: SystemdSmokeDependencies) -> SystemdSmokeReport:
        calls.append(dependencies)
        return SystemdSmokeReport(True, '{"kind":"systemd-smoke"}')

    assert (
        main(
            ["loop", "smoke", "--apply", "--json"],
            environment={"AGENT_WORK_AGENT": "codex"},
            discover_agent_work_config=lambda **_kwargs: _smoke_config(tmp_path),
            home_directory=lambda: tmp_path / "home",
            systemd_smoke_action=run_smoke,
            write_stdout=stdout.append,
        )
        == 0
    )
    assert len(calls) == 1
    assert calls[0].checkout == tmp_path / "checkout"
    assert [json.loads(item) for item in stdout] == [
        {
            "schema_version": 1,
            "command": "loop.smoke",
            "applied": True,
            "outcome": "passed",
            "broker_accepted": True,
        }
    ]


def test_smoke_apply_uses_configured_codex_home(tmp_path: Path) -> None:
    """The smoke boundary receives the installed service's configured Codex home."""
    calls: list[SystemdSmokeDependencies] = []

    def run_smoke(*, dependencies: SystemdSmokeDependencies) -> SystemdSmokeReport:
        calls.append(dependencies)
        return SystemdSmokeReport(True, "{}")

    assert (
        main(
            ["loop", "smoke", "--apply", "--json"],
            environment={
                "AGENT_WORK_AGENT": "codex",
                "CODEX_HOME": "/configured/service-codex",
            },
            discover_agent_work_config=lambda **_kwargs: _smoke_config(tmp_path),
            systemd_smoke_action=run_smoke,
            write_stdout=lambda _value: None,
        )
        == 0
    )
    assert calls[0].codex_home == Path("/configured/service-codex")


def test_smoke_apply_reports_boundary_failure_without_success_output(
    tmp_path: Path,
) -> None:
    """Boundary failures retain the CLI error contract and do not report pass."""
    stdout: list[str] = []
    stderr: list[str] = []

    def fail_smoke(*, dependencies: SystemdSmokeDependencies) -> SystemdSmokeReport:
        del dependencies
        raise SystemdSmokeError("smoke boundary failed")

    assert (
        main(
            ["loop", "smoke", "--apply", "--json"],
            environment={"AGENT_WORK_AGENT": "codex"},
            discover_agent_work_config=lambda **_kwargs: _smoke_config(tmp_path),
            systemd_smoke_action=fail_smoke,
            write_stdout=stdout.append,
            write_stderr=stderr.append,
        )
        == 1
    )
    assert stdout == []
    assert stderr == ["smoke boundary failed"]


def test_smoke_rejects_a_non_codex_installed_profile(tmp_path: Path) -> None:
    """The configured factory agent must match the Codex proof command."""
    owner_home, _, _ = _owner_codex_home(tmp_path)
    dependencies = _dependencies(
        lambda *_args, **_kwargs: pytest.fail("smoke must not launch"),
        codex_home=owner_home,
        temporary_directory=tmp_path,
    )
    with pytest.raises(SystemdSmokeError, match="AGENT_WORK_AGENT=codex"):
        run_systemd_smoke(
            dependencies=replace(
                dependencies,
                environment={**dependencies.environment, "AGENT_WORK_AGENT": "pi"},
            )
        )


def test_smoke_cleans_private_root_when_workspace_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copied policy setup failure never leaves a private credential home."""
    owner_home, _, _ = _owner_codex_home(tmp_path)
    (tmp_path / "agent-work" / ".agents").mkdir(parents=True)

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr(smoke_module.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        run_systemd_smoke(
            dependencies=_dependencies(
                lambda *_args, **_kwargs: pytest.fail("smoke must not launch"),
                codex_home=owner_home,
                temporary_directory=tmp_path,
            )
        )

    assert list(tmp_path.glob("agent-work-smoke-*")) == []


def test_smoke_closes_its_broker_after_malformed_jsonl_stdout(tmp_path: Path) -> None:
    """Malformed machine output cannot bypass real broker closure."""
    owner_home, _, _ = _owner_codex_home(tmp_path)
    with pytest.raises(SystemdSmokeError, match="missing machine child evidence"):
        run_systemd_smoke(
            dependencies=_dependencies(
                lambda *_args, **_kwargs: CommandResult(
                    0, "{malformed\n", "delegation diagnostic"
                ),
                codex_home=owner_home,
                temporary_directory=tmp_path,
            )
        )

    assert list(tmp_path.glob("agent-work-smoke-*")) == []


def test_smoke_reports_private_cleanup_after_a_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cleanup diagnostics augment, rather than replace, the smoke failure."""
    owner_home, _, _ = _owner_codex_home(tmp_path)

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    with pytest.raises(
        SystemdSmokeError,
        match="missing machine child evidence.*private Codex home cleanup failed",
    ):
        run_systemd_smoke(
            dependencies=_dependencies(
                lambda *_args, **_kwargs: CommandResult(0, "{}", ""),
                codex_home=owner_home,
                temporary_directory=tmp_path,
            )
        )


def test_smoke_rejects_zero_exit_without_machine_child_events_and_redacts_stderr(
    tmp_path: Path,
) -> None:
    """A successful producer exit and prose admission never substitute for child evidence."""

    def run(_command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            json.dumps(
                {
                    "events": [],
                    "message": "collab spawn failed but I completed the request",
                }
            ),
            "router: collab spawn failed: no thread with id root; token=do-not-leak",
        )

    with pytest.raises(
        SystemdSmokeError, match="missing machine child evidence"
    ) as error:
        owner_home, _, _ = _owner_codex_home(tmp_path)
        run_systemd_smoke(
            dependencies=_dependencies(
                run, codex_home=owner_home, temporary_directory=tmp_path
            )
        )

    assert "collab spawn failed" in str(error.value)
    assert "do-not-leak" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_smoke_failure_diagnostic_is_redacted_and_bounded(tmp_path: Path) -> None:
    """A child stderr cannot expose a secret or exceed the operator diagnostic budget."""
    owner_home, _, _ = _owner_codex_home(tmp_path)
    child_stderr = "credential=abc123 pwd=pass " + "x" * 20_000
    dependencies = _dependencies(
        lambda *_args, **_kwargs: CommandResult(0, "{}", child_stderr),
        codex_home=owner_home,
        temporary_directory=tmp_path,
    )

    with pytest.raises(SystemdSmokeError) as error:
        run_systemd_smoke(
            dependencies=replace(
                dependencies,
                environment={
                    **dependencies.environment,
                    "SECRET_TOKEN": "abc",
                    "CREDENTIAL": "abc123",
                    "PASSWORD": "pass",
                },
            )
        )

    assert "abc123" not in str(error.value)
    assert "pass" not in str(error.value)
    assert "[REDACTED]123" not in str(error.value)
    assert "[REDACTED]" in str(error.value)
    assert len(str(error.value)) <= 4096


def _rollout(
    session_id: str, task_name: str, parent_thread_id: str
) -> dict[str, object]:
    """Build one private child rollout metadata record from the Codex boundary."""
    return {
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "source": {
                "subagent": {
                    "thread_spawn": {
                        "parent_thread_id": parent_thread_id,
                        "agent_path": f"/root/{task_name}",
                    }
                }
            },
        },
    }


def test_child_receipts_follow_required_task_order_and_reject_duplicates() -> None:
    """Task names, rather than random session IDs, bind the result ordering."""
    assert _prove_children(
        (
            _rollout("balanced", "architect_proposal_balanced", "root"),
            _rollout("z-fast-one", "architect_proposal_fast_one", "root"),
            _rollout("a-fast-two", "architect_proposal_fast_two", "root"),
        ),
        "root",
    ) == ("balanced", "z-fast-one", "a-fast-two")
    with pytest.raises(SystemdSmokeError, match="duplicate required child task"):
        _prove_children(
            (
                _rollout("balanced", "architect_proposal_balanced", "root"),
                _rollout("one", "architect_proposal_fast_one", "root"),
                _rollout("two", "architect_proposal_fast_one", "root"),
            ),
            "root",
        )
    with pytest.raises(SystemdSmokeError, match="unexpected direct child"):
        _prove_children(
            (
                _rollout("balanced", "architect_proposal_balanced", "root"),
                _rollout("one", "architect_proposal_fast_one", "root"),
                _rollout("two", "architect_proposal_fast_two", "root"),
                _rollout("extra", "unapproved_child", "root"),
            ),
            "root",
        )
    with pytest.raises(SystemdSmokeError, match="duplicate direct child session"):
        _prove_children(
            (
                _rollout("same", "architect_proposal_balanced", "root"),
                _rollout("same", "architect_proposal_fast_one", "root"),
                _rollout("two", "architect_proposal_fast_two", "root"),
            ),
            "root",
        )


def test_rollout_scan_rejects_incomplete_file_inventory(tmp_path: Path) -> None:
    """Evidence limits fail closed rather than hiding a later direct child."""
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    for index in range(33):
        (sessions / f"{index}.jsonl").write_text("{}\n")

    with pytest.raises(SystemdSmokeError, match="rollout scan is incomplete"):
        tuple(_read_rollouts(tmp_path / "codex"))


def test_rollout_scan_reads_only_first_metadata_lines_from_large_valid_sessions(
    tmp_path: Path,
) -> None:
    """Large private rollout tails do not prevent bounded child identity extraction."""
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    metadata = (
        {"type": "session_meta", "payload": {"id": "root"}},
        _rollout("balanced", "architect_proposal_balanced", "root"),
        _rollout("fast-one", "architect_proposal_fast_one", "root"),
        _rollout("fast-two", "architect_proposal_fast_two", "root"),
    )
    valid_tail = json.dumps({"type": "response", "payload": "x" * 70_000}) + "\n"
    for index, record in enumerate(metadata):
        (sessions / f"{index}.jsonl").write_text(json.dumps(record) + "\n" + valid_tail)

    records = tuple(_read_rollouts(tmp_path / "codex"))
    assert len(records) == len(metadata)
    assert all(record in records for record in metadata)


def test_rollout_scan_rejects_oversized_first_metadata_line(tmp_path: Path) -> None:
    """The metadata byte cap applies before an oversized line is parsed."""
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "root.jsonl").write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"id": "root", "extra": "x" * 270_000}}
        )
        + "\n"
    )

    with pytest.raises(SystemdSmokeError, match="rollout scan is incomplete"):
        tuple(_read_rollouts(tmp_path / "codex"))


def test_rollout_scan_rejects_nonmetadata_first_line(tmp_path: Path) -> None:
    """A later valid record cannot substitute for first-line session metadata."""
    sessions = tmp_path / "codex" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "root.jsonl").write_text(
        '{"type":"response"}\n{"type":"session_meta","payload":{"id":"root"}}\n'
    )

    with pytest.raises(SystemdSmokeError, match="rollout scan is incomplete"):
        tuple(_read_rollouts(tmp_path / "codex"))


def test_smoke_submits_to_the_live_broker_only_after_controller_child_gate(
    tmp_path: Path,
) -> None:
    """The synthetic identity is accepted only after three distinct children are observed."""
    calls: list[tuple[str, ...]] = []
    owner_home, auth, config = _owner_codex_home(tmp_path)
    private_home: Path | None = None

    def run(
        command: tuple[str, ...], *, environment: dict[str, str], **_kwargs: object
    ) -> CommandResult:
        nonlocal private_home
        calls.append(command)
        assert environment["AGENT_WORK_UV"] == str(configured_uv)
        assert str(tmp_path / "agent-work/bin/agent-work-from-checkout") in str(
            _kwargs["stdin"]
        )
        assert "must read its named exact skill" in str(_kwargs["stdin"])
        assert "neither delegate, mutate, nor build or submit" in str(_kwargs["stdin"])
        workspace = Path(command[command.index("-C") + 1])
        git_path = shutil.which("git")
        assert git_path is not None
        git_probe = subprocess.run(
            [git_path, "-C", str(workspace), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert git_probe.returncode == 0 and git_probe.stdout.strip() == "true"
        codex_home = Path(environment["CODEX_HOME"])
        private_home = codex_home
        assert stat.S_IMODE(codex_home.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(codex_home.stat().st_mode) == 0o700
        assert (codex_home / "auth.json").is_symlink()
        assert (codex_home / "auth.json").resolve() == auth
        assert (codex_home / "config.toml").is_symlink()
        assert (codex_home / "config.toml").resolve() == config
        assert (
            Path(environment["UV_CACHE_DIR"])
            == codex_home.parent / "workspace" / "uv-cache"
        )
        assert (
            Path(environment["XDG_CACHE_HOME"])
            == codex_home.parent / "workspace" / "xdg-cache"
        )
        assert stat.S_IMODE(Path(environment["UV_CACHE_DIR"]).stat().st_mode) == 0o700
        assert stat.S_IMODE(Path(environment["XDG_CACHE_HOME"]).stat().st_mode) == 0o700
        sessions = codex_home / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "root.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": "root"}}) + "\n"
        )
        for session_id, task_name in (
            ("balanced-1", "architect_proposal_balanced"),
            ("fast-1", "architect_proposal_fast_one"),
            ("fast-2", "architect_proposal_fast_two"),
        ):
            (sessions / f"{session_id}.jsonl").write_text(
                json.dumps(_rollout(session_id, task_name, "root")) + "\n"
            )
        receipt = submit_worker_result_to_broker(
            Path(environment["AGENT_WORK_SMOKE_BROKER_SOCKET"]),
            WorkerResultBrokerClientRequest(
                nonce=environment["AGENT_WORK_SMOKE_BROKER_NONCE"], issue_iid=251
            ),
            b'{"kind":"systemd-smoke","session_ids":["balanced-1","fast-1","fast-2"]}',
            timeout_seconds=60,
        )
        assert receipt["accepted"] is True
        return CommandResult(
            0,
            json.dumps({"events": [{"type": "thread.started", "thread_id": "root"}]}),
            "",
        )

    configured_uv = tmp_path / "configured-uv"
    configured_uv.write_text("#!/usr/bin/env bash\nexit 0\n")
    configured_uv.chmod(0o700)
    dependencies = _dependencies(
        run, codex_home=owner_home, temporary_directory=tmp_path
    )
    report = run_systemd_smoke(
        dependencies=replace(
            dependencies,
            environment={
                **dependencies.environment,
                "AGENT_WORK_UV": str(configured_uv),
            },
        )
    )

    assert report.broker_accepted is True
    assert json.loads(report.broker_artifact) == {
        "kind": "systemd-smoke",
        "session_ids": ["balanced-1", "fast-1", "fast-2"],
    }
    assert private_home is not None
    assert calls[0][:4] == (
        "/opt/codex",
        "exec",
        "-C",
        str(private_home.parent / "workspace"),
    )
    assert calls[0][4:11] == (
        "--sandbox",
        "workspace-write",
        "-c",
        "sandbox_workspace_write.network_access=true",
        "--enable",
        "multi_agent",
        "--add-dir",
    )
    assert "--ephemeral" not in calls[0]
    assert not private_home.parent.exists()
    assert auth.read_text() == '{"token":"unchanged"}\n'
    assert config.read_text() == "model = 'unchanged'\n"
    assert stat.S_IMODE(os.stat(owner_home).st_mode) == 0o700


def test_smoke_git_init_failure_stops_before_codex_and_cleans_private_root(
    tmp_path: Path,
) -> None:
    """A failed isolated Git init cannot launch Codex or retain private files."""
    owner_home, _, _ = _owner_codex_home(tmp_path)
    commands: list[tuple[str, ...]] = []

    def fail_git(command: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(command)
        assert kwargs["timeout_seconds"] == 10
        git_environment = kwargs["environment"]
        assert isinstance(git_environment, dict)
        git_environment = cast(dict[str, str], git_environment)
        assert set(git_environment) == {
            "HOME",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_TERMINAL_PROMPT",
            "GCM_INTERACTIVE",
        }
        assert Path(git_environment["HOME"]).name == "workspace"
        assert git_environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert git_environment["GIT_CONFIG_NOSYSTEM"] == "1"
        return CommandResult(1, "", "operator Git config must not leak")

    dependencies = replace(
        _dependencies(
            lambda *_args, **_kwargs: pytest.fail("Codex must not launch"),
            codex_home=owner_home,
            temporary_directory=tmp_path,
        ),
        run_git_command=fail_git,
    )
    with pytest.raises(
        SystemdSmokeError, match="private smoke Git initialization failed"
    ):
        run_systemd_smoke(dependencies=dependencies)

    assert len(commands) == 1
    assert commands[0][0] == dependencies.environment["AGENT_WORK_GIT"]
    assert "--template=" in commands[0]
    assert list(tmp_path.glob("agent-work-smoke-*")) == []


def test_smoke_rejects_unrelated_parent_rollout_before_broker_acceptance(
    tmp_path: Path,
) -> None:
    """A child rollout must be bound to the producer root before a broker can accept it."""

    def run(_command: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(
            0,
            json.dumps({"events": [{"type": "thread.started", "thread_id": "root"}]}),
            "",
        )

    with pytest.raises(SystemdSmokeError, match="parent-bound child evidence"):
        owner_home, _, _ = _owner_codex_home(tmp_path)
        run_systemd_smoke(
            dependencies=_dependencies(
                run,
                codex_home=owner_home,
                temporary_directory=tmp_path,
                read_rollout_metadata=lambda _root: (
                    _rollout("balanced-1", "architect_proposal_balanced", "other-root"),
                    _rollout("fast-1", "architect_proposal_fast_one", "other-root"),
                    _rollout("fast-2", "architect_proposal_fast_two", "other-root"),
                ),
            )
        )
