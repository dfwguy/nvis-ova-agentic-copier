# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only template comparison preview CLI."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from agent_work import template_comparison
from agent_work.cli import main
from agent_work.template_comparison import (
    CommandResult,
    TemplateComparison,
    TemplateComparisonError,
    compare_template_revisions,
)

_CURRENT_COMMIT = "a" * 40
_CANDIDATE_COMMIT = "b" * 40


@contextmanager
def _private_repository() -> Iterator[Path]:
    """Provide the private repository boundary without filesystem access."""

    yield Path("/private/template-preview.git")


def _answers(_path: Path) -> str:
    """Return Copier metadata from the injected text boundary."""

    return (
        "_src_path: https://git.example.test/org/template.git\n"
        f"_commit: {_CURRENT_COMMIT}\n"
    )


def _runner(argv: tuple[str, ...], *, timeout_seconds: float) -> CommandResult:
    """Return deterministic binary Git protocol results through the boundary."""

    if argv[-2:] == ("--verify", "refs/agent-work/current^{commit}"):
        return CommandResult(0, f"{_CURRENT_COMMIT}\n".encode(), b"")
    if argv[-2:] == ("--verify", "refs/agent-work/candidate^{commit}"):
        return CommandResult(0, f"{_CANDIDATE_COMMIT}\n".encode(), b"")
    if "diff" in argv:
        return CommandResult(0, b"src/a.py\x00README.md\x00", b"")
    return CommandResult(0, b"", b"")


def test_comparison_result_exposes_resolved_commits_and_safe_paths() -> None:
    result = compare_template_revisions(
        Path(".copier-answers.yml"),
        "candidate",
        read_text=_answers,
        run_command=_runner,
        private_repository=_private_repository,
        timeout_seconds=30,
    )

    assert result.current_commit == _CURRENT_COMMIT
    assert result.candidate_commit == _CANDIDATE_COMMIT
    assert result.paths == ("README.md", "src/a.py")


def test_comparison_error_has_a_closed_safe_stage_identifier() -> None:
    def malformed_current(
        argv: tuple[str, ...], *, timeout_seconds: float
    ) -> CommandResult:
        if argv[-2:] == ("--verify", "refs/agent-work/current^{commit}"):
            return CommandResult(0, b"not-a-commit\n", b"untrusted stderr")
        return _runner(argv, timeout_seconds=timeout_seconds)

    with pytest.raises(TemplateComparisonError) as raised:
        compare_template_revisions(
            Path(".copier-answers.yml"),
            "candidate",
            read_text=_answers,
            run_command=malformed_current,
            private_repository=_private_repository,
            timeout_seconds=30,
        )

    assert str(raised.value) == "template comparison failed"
    assert raised.value.stage == "resolve-current"


def test_comparison_error_replaces_an_unknown_stage_with_input() -> None:
    assert TemplateComparisonError(stage="untrusted-stage").stage == "input"


def test_template_preview_json_uses_only_the_injected_comparison_boundaries() -> None:
    calls: list[tuple[Path, str, Callable[[Path], str], object, object, float]] = []
    output: list[str] = []

    def compare(
        answers_path: Path,
        candidate_revision: str,
        *,
        read_text: Callable[[Path], str],
        run_command: object,
        private_repository: object,
        timeout_seconds: float,
    ) -> TemplateComparison:
        calls.append(
            (
                answers_path,
                candidate_revision,
                read_text,
                run_command,
                private_repository,
                timeout_seconds,
            )
        )
        return TemplateComparison(
            current_commit=_CURRENT_COMMIT,
            candidate_commit=_CANDIDATE_COMMIT,
            paths=("README.md", "src/a.py"),
        )

    assert (
        main(
            [
                "template",
                "preview",
                "--candidate-revision",
                "candidate",
                "--project-root",
                "/workspace/project",
                "--json",
            ],
            dispatch=lambda *_args, **_kwargs: pytest.fail("dispatcher was used"),
            compare_template_revisions=compare,
            template_read_text=_answers,
            template_run_command=_runner,
            template_private_repository=_private_repository,
            write_stdout=output.append,
        )
        == 0
    )

    assert calls == [
        (
            Path("/workspace/project/.copier-answers.yml"),
            "candidate",
            _answers,
            _runner,
            _private_repository,
            30,
        )
    ]
    assert json.loads(output[0]) == {
        "schema_version": 1,
        "command": "template.preview",
        "outcome": "changed",
        "current_commit": _CURRENT_COMMIT,
        "candidate_commit": _CANDIDATE_COMMIT,
        "paths": ["README.md", "src/a.py"],
    }


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (
            ("README.md", "src/a.py"),
            (
                f"outcome=changed current_commit={_CURRENT_COMMIT} "
                f"candidate_commit={_CANDIDATE_COMMIT} paths=2\n"
                "path=README.md\npath=src/a.py"
            ),
        ),
        (
            (),
            (
                f"outcome=unchanged current_commit={_CURRENT_COMMIT} "
                f"candidate_commit={_CANDIDATE_COMMIT} paths=0"
            ),
        ),
    ],
)
def test_template_preview_text_is_stable_for_changed_and_unchanged_results(
    paths: tuple[str, ...], expected: str
) -> None:
    output: list[str] = []

    def compare(*_args: object, **_kwargs: object) -> TemplateComparison:
        return TemplateComparison(
            current_commit=_CURRENT_COMMIT,
            candidate_commit=_CANDIDATE_COMMIT,
            paths=paths,
        )

    assert (
        main(
            ["template", "preview", "--candidate-revision", "candidate"],
            dispatch=lambda *_args, **_kwargs: pytest.fail("dispatcher was used"),
            compare_template_revisions=compare,
            write_stdout=output.append,
        )
        == 0
    )

    assert output == [expected]


@pytest.mark.parametrize(
    ("debug", "expected"),
    [
        (False, "template comparison failed"),
        (True, "template comparison failed stage=resolve-candidate"),
    ],
)
def test_template_preview_failure_output_is_safe_in_normal_and_debug_modes(
    debug: bool, expected: str
) -> None:
    error_output: list[str] = []

    def compare(*_args: object, **_kwargs: object) -> TemplateComparison:
        raise TemplateComparisonError(stage="resolve-candidate")

    arguments = ["template", "preview", "--candidate-revision", "candidate"]
    if debug:
        arguments.append("--debug")
    assert (
        main(
            arguments,
            dispatch=lambda *_args, **_kwargs: pytest.fail("dispatcher was used"),
            compare_template_revisions=compare,
            write_stderr=error_output.append,
        )
        == 1
    )

    assert error_output == [expected]


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (
            ["template", "preview"],
            "the following arguments are required: --candidate-revision",
        ),
        (
            ["template", "preview", "--candidate-revision", "candidate", "--apply"],
            "unrecognized arguments: --apply",
        ),
    ],
)
def test_template_preview_requires_a_candidate_and_rejects_apply(
    arguments: list[str], expected_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)

    assert raised.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected_error in captured.err


def test_template_preview_help_bypasses_the_dispatcher(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            ["template", "preview", "--help"],
            dispatch=lambda *_args, **_kwargs: pytest.fail("dispatcher was used"),
        )

    assert raised.value.code == 0
    help_output = capsys.readouterr().out
    assert "--candidate-revision" in help_output
    assert "show a safe failure stage" in help_output


def test_binary_git_adapter_uses_a_fixed_argv_only_safe_process_boundary() -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_process(
        command: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return cast(
            subprocess.CompletedProcess[bytes],
            SimpleNamespace(
                returncode=7, stdout=b"binary\x00stdout", stderr=b"binary\xffstderr"
            ),
        )

    result = template_comparison.run_git_command(
        ("git", "fetch", "https://git.example.test/org/template.git"),
        timeout_seconds=17,
        git_executable=Path("/usr/bin/git"),
        run_process=run_process,
        environment={
            "HTTPS_PROXY": "http://proxy.example.test:8080",
            "NO_PROXY": "localhost",
            "SSL_CERT_FILE": "/certs/ca.pem",
            "SSH_AUTH_SOCK": "/run/user/1000/ssh.sock",
            "GIT_CONFIG_PARAMETERS": "-c unsafe.option=true",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "alias.fetch",
            "GIT_CONFIG_VALUE_0": "!unsafe-command",
            "GIT_DIR": "/attacker/repository",
            "GIT_WORK_TREE": "/attacker/worktree",
            "GIT_SSH_COMMAND": "ssh -o ProxyCommand=unsafe-command",
        },
    )

    assert result == CommandResult(7, b"binary\x00stdout", b"binary\xffstderr")
    assert calls == [
        (
            (
                "/usr/bin/git",
                "fetch",
                "https://git.example.test/org/template.git",
            ),
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "timeout": 17,
                "check": False,
                "shell": False,
                "env": {
                    "HTTPS_PROXY": "http://proxy.example.test:8080",
                    "NO_PROXY": "localhost",
                    "SSL_CERT_FILE": "/certs/ca.pem",
                    "SSH_AUTH_SOCK": "/run/user/1000/ssh.sock",
                    "GCM_INTERACTIVE": "never",
                    "GIT_ASKPASS": "/bin/false",
                    "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "SSH_ASKPASS": "/bin/false",
                },
            },
        )
    ]


def test_template_preview_main_composes_default_git_boundaries_for_an_injected_comparison() -> (
    None
):
    calls: list[tuple[object, object]] = []
    output: list[str] = []

    def compare(
        answers_path: Path,
        candidate_revision: str,
        *,
        read_text: Callable[[Path], str],
        run_command: object,
        private_repository: object,
        timeout_seconds: float,
    ) -> TemplateComparison:
        assert read_text is not _answers
        assert timeout_seconds == 30
        calls.append((run_command, private_repository))
        return TemplateComparison(
            current_commit=_CURRENT_COMMIT,
            candidate_commit=_CANDIDATE_COMMIT,
            paths=(),
        )

    assert (
        main(
            ["template", "preview", "--candidate-revision", "candidate"],
            dispatch=lambda *_args, **_kwargs: pytest.fail("dispatcher was used"),
            compare_template_revisions=compare,
            find_executable=lambda name: "/trusted/git" if name == "git" else None,
            environment={"HTTPS_PROXY": "http://proxy"},
            write_stdout=output.append,
        )
        == 0
    )

    assert len(calls) == 1
    assert isinstance(calls[0][0], partial)
    assert calls[0][0].func is template_comparison.run_git_command
    assert calls[0][0].keywords == {
        "git_executable": Path("/trusted/git"),
        "environment": {"HTTPS_PROXY": "http://proxy"},
    }
    assert calls[0][1] is template_comparison.private_repository
    assert output == [
        (
            f"outcome=unchanged current_commit={_CURRENT_COMMIT} "
            f"candidate_commit={_CANDIDATE_COMMIT} paths=0"
        )
    ]


@pytest.mark.parametrize("resolver", [lambda _name: None, lambda _name: "git"])
def test_template_preview_rejects_untrusted_git_resolution(
    resolver: Callable[[str], str | None],
) -> None:
    errors: list[str] = []
    assert (
        main(
            ["template", "preview", "--candidate-revision", "candidate"],
            find_executable=resolver,
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == ["template comparison failed"]


def test_template_preview_injected_runner_bypasses_git_resolution() -> None:
    output: list[str] = []

    def exploding_resolver(_name: str) -> str | None:
        pytest.fail("resolver was used")

    def compare(*_args: object, **_kwargs: object) -> TemplateComparison:
        return TemplateComparison(_CURRENT_COMMIT, _CANDIDATE_COMMIT, ())

    assert (
        main(
            ["template", "preview", "--candidate-revision", "candidate"],
            compare_template_revisions=compare,
            template_run_command=_runner,
            find_executable=exploding_resolver,
            write_stdout=output.append,
        )
        == 0
    )
    assert output


@pytest.mark.parametrize(
    ("resolver", "debug", "expected"),
    [
        (lambda _name: None, False, "template comparison failed"),
        (lambda _name: "git", True, "template comparison failed stage=input"),
        (
            lambda _name: (_ for _ in ()).throw(OSError("secret")),
            True,
            "template comparison failed stage=input",
        ),
    ],
)
def test_template_preview_redacts_git_resolution_failures(
    resolver: Callable[[str], str | None], debug: bool, expected: str
) -> None:
    errors: list[str] = []
    arguments = ["template", "preview", "--candidate-revision", "candidate"]
    if debug:
        arguments.append("--debug")
    assert (
        main(
            arguments,
            find_executable=resolver,
            compare_template_revisions=lambda *_args, **_kwargs: pytest.fail(
                "compare used"
            ),
            write_stderr=errors.append,
        )
        == 1
    )
    assert errors == [expected]
