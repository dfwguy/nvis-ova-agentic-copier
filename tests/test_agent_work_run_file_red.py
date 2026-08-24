"""Red end-to-end contracts for bounded filesystem intake."""

import json
from pathlib import Path

import pytest

from agent_work.cli import main
from agent_work.filesystem import FileIntakeReport


class FakeFileRunner:
    """Injected public runner seam; it never touches a real repository."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"preview": not kwargs["apply"], "branch": "agent/file-a" * 16}


def test_run_file_preview_is_read_only_and_preserves_the_bounded_cli_contract(
    tmp_path: Path,
) -> None:
    """No `--apply` means a selected inbox file is only inspected and reported."""
    runner = FakeFileRunner()
    stdout: list[str] = []
    inbox_root = tmp_path / "inbox-root"

    exit_code = main(
        [
            "run-file",
            "--path",
            "item.json",
            "--inbox-root",
            str(inbox_root),
            "--repository",
            "copier",
            "--mode",
            "publish",
            "--json",
        ],
        run_file=runner,
        write_stdout=stdout.append,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert runner.calls == [
        {
            "path": Path("item.json"),
            "inbox_root": inbox_root,
            "repository": "copier",
            "mode": "publish",
            "apply": False,
            "timeout_seconds": 900,
        }
    ]
    assert json.loads(stdout[0])["preview"] is True


def test_run_file_apply_forwards_only_explicit_bounded_inputs(tmp_path: Path) -> None:
    """Applied execution remains one selected file and one caller-selected timeout."""
    runner = FakeFileRunner()
    inbox_root = tmp_path / "inbox-root"

    exit_code = main(
        [
            "run-file",
            "--path=item.json",
            "--inbox-root",
            str(inbox_root),
            "--repository",
            "copier",
            "--mode",
            "publish",
            "--apply",
            "--timeout-seconds",
            "23",
        ],
        run_file=runner,
        write_stdout=lambda _message: None,
        write_stderr=lambda _message: None,
    )

    assert exit_code == 0
    assert runner.calls[0]["apply"] is True
    assert runner.calls[0]["timeout_seconds"] == 23


@pytest.mark.parametrize(
    "path",
    ("/tmp/item.json", "nested/item.json", "../item.json", ".", "item.txt"),
)
def test_run_file_rejects_unsafe_or_unsupported_item_names_before_runner_invocation(
    tmp_path: Path, path: str
) -> None:
    """The CLI never lets a path escape the configured inbox component."""
    runner = FakeFileRunner()
    stderr: list[str] = []

    exit_code = main(
        [
            "run-file",
            "--path",
            path,
            "--inbox-root",
            str(tmp_path / "inbox-root"),
            "--repository",
            "copier",
            "--mode",
            "publish",
            "--apply",
        ],
        run_file=runner,
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 2
    assert runner.calls == []
    assert stderr == ["invalid command usage"]


def test_run_file_rejects_a_relative_inbox_root_before_runner_invocation() -> None:
    """The selected inbox is a host boundary and must never be cwd-relative."""
    runner = FakeFileRunner()
    stderr: list[str] = []

    exit_code = main(
        [
            "run-file",
            "--path",
            "item.json",
            "--inbox-root",
            "relative-inbox",
            "--repository",
            "copier",
            "--mode",
            "publish",
        ],
        run_file=runner,
        write_stdout=lambda _message: None,
        write_stderr=stderr.append,
    )

    assert exit_code == 2
    assert runner.calls == []
    assert stderr == ["invalid command usage"]


def test_run_file_runtime_error_is_a_safe_exit_one_without_a_traceback(
    tmp_path: Path,
) -> None:
    """Unexpected filesystem workflow failures must not escape the CLI boundary."""
    stdout: list[str] = []
    stderr: list[str] = []

    def failing_runner(**_kwargs: object) -> object:
        raise RuntimeError("producer-controlled details must stay private")

    exit_code = main(
        [
            "run-file",
            "--path",
            "item.json",
            "--inbox-root",
            str(tmp_path / "inbox-root"),
            "--repository",
            "copier",
            "--mode",
            "publish",
            "--apply",
            "--json",
        ],
        run_file=failing_runner,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert stdout == []
    assert stderr == ["agent-work run failed"]


def test_run_file_applied_quarantine_is_exit_one_with_a_machine_readable_report(
    tmp_path: Path,
) -> None:
    """Quarantine tells automation that an applied request did not reach publication."""
    stdout: list[str] = []
    stderr: list[str] = []

    exit_code = main(
        [
            "run-file",
            "--path",
            "item.json",
            "--inbox-root",
            str(tmp_path / "inbox-root"),
            "--repository",
            "copier",
            "--mode",
            "publish",
            "--apply",
            "--json",
        ],
        run_file=lambda **_kwargs: FileIntakeReport(
            preview=False,
            outcome="quarantined",
            quarantined=True,
        ),
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert exit_code == 1
    assert json.loads(stdout[0])["outcome"] == "quarantined"
    assert stderr == []


def test_run_file_help_explains_the_bounded_filesystem_intake_contract(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Operators can discover every safety boundary without reading implementation."""
    exit_code = main(["run-file", "--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "--path PATH" in captured.out
    assert "JSON basename in the inbox directory" in captured.out
    assert "--inbox-root INBOX_ROOT" in captured.out
    assert "absolute inbox layout root" in captured.out
    assert "--repository REPOSITORY" in captured.out
    assert "allowlisted repository alias" in captured.out
    assert "--mode {publish}" in captured.out
    assert "publish mode" in captured.out
    assert "--apply" in captured.out
    assert "enables mutation" in captured.out
    assert "read-only preview" in captured.out
    assert "--json" in captured.out
    assert "JSON report" in captured.out
    assert "--timeout-seconds TIMEOUT_SECONDS" in captured.out
    assert "default: 900" in captured.out


def test_top_level_help_briefly_describes_run_file_as_filesystem_intake(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The command index distinguishes controlled file intake from issue routing."""
    exit_code = main(["--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "run-file" in captured.out
    assert "filesystem intake" in captured.out
