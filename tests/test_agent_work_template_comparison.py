# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contract for safe, read-only Copier template comparisons."""

from __future__ import annotations

import re
import subprocess
import traceback
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from agent_work.template_comparison import (
    CommandResult,
    TemplateComparison,
    TemplateComparisonError,
    compare_template_revisions,
)

_SAFE_ORIGIN = "https://git.example.test/org/template.git"
_CURRENT_COMMIT = "a" * 40
_CANDIDATE_COMMIT = "b" * 40
_SECRET = "https://user:super-secret@git.example.test/org/template.git"


@dataclass(frozen=True)
class RecordedCommand:
    """A binary command invocation made by the comparison boundary."""

    argv: tuple[str, ...]
    timeout_seconds: float


class FakeRunner:
    """Injected command boundary with explicit Git protocol responses."""

    def __init__(self, *, diff_stdout: bytes = b"src/a.py\x00README.md\x00") -> None:
        self.calls: list[RecordedCommand] = []
        self.diff_stdout = diff_stdout

    def __call__(
        self, argv: tuple[str, ...], *, timeout_seconds: float
    ) -> CommandResult:
        self.calls.append(RecordedCommand(tuple(argv), timeout_seconds))
        if argv[-2:] == ("--verify", "refs/agent-work/current^{commit}"):
            return CommandResult(0, f"{_CURRENT_COMMIT}\n".encode(), b"")
        if argv[-2:] == ("--verify", "refs/agent-work/candidate^{commit}"):
            return CommandResult(0, f"{_CANDIDATE_COMMIT}\n".encode(), b"")
        if "diff" in argv:
            return CommandResult(0, self.diff_stdout, b"")
        return CommandResult(0, b"", b"")


@contextmanager
def private_repository() -> Iterator[Path]:
    """Supply a fake ephemeral bare-repository location without filesystem I/O."""

    yield Path("/private/repository")


def compare(
    read_text: Callable[[Path], str],
    runner: FakeRunner,
    *,
    candidate_revision: str = "candidate",
    repository: Callable[
        [], AbstractContextManager[Path, bool | None]
    ] = private_repository,
) -> TemplateComparison:
    """Exercise the public comparison API solely through injected boundaries."""

    return compare_template_revisions(
        Path(".copier-answers.yml"),
        candidate_revision,
        read_text=read_text,
        run_command=runner,
        private_repository=repository,
        timeout_seconds=7.5,
    )


def answers(origin: str = _SAFE_ORIGIN) -> str:
    """Return minimal Copier metadata plus an irrelevant ordinary answer."""

    return f"_src_path: {origin}\n_commit: {_CURRENT_COMMIT}\nproject_name: ignored\n"


@pytest.mark.parametrize(
    "origin",
    [
        "https://git.example.test/org/template.git",
        "ssh://git@git.example.test/org/template.git",
        "git@git.example.test:org/template.git",
    ],
)
def test_comparison_accepts_safe_origins_and_returns_immutable_sorted_paths(
    origin: str,
) -> None:
    runner = FakeRunner(diff_stdout=b"z.py\x00a.py\x00z.py\x00")

    result = compare(lambda _path: answers(origin), runner)

    assert result.paths == ("a.py", "z.py")
    attribute = "paths"
    with pytest.raises((AttributeError, TypeError)):
        setattr(result, attribute, ())


@pytest.mark.parametrize(
    "origin",
    [
        "file:///tmp/template",
        "/tmp/template",
        "../template",
        "ext::evil",
        "https://user:password@git.example.test/org/template.git",
        "https://git@git.example.test/org/template.git",
        "https://git.example.test/org/template.git -c core.hooksPath=x",
        "ssh://git@-oProxyCommand=evil/org/template.git",
        "git@-oProxyCommand=evil:org/template.git",
        "-uhttps://git.example.test/org/template.git",
        "git@git.example.test",
        "https://git.example.test",
    ],
)
def test_comparison_rejects_unsafe_or_malformed_origins_without_leaking_them(
    origin: str,
) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(origin), runner)

    assert str(raised.value) == "template comparison failed"
    assert origin not in str(raised.value)
    assert runner.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://[",
        "https://[not-a-host",
        "https://git.example.test:badport/org/template.git",
    ],
)
def test_malformed_https_origins_are_sanitized_without_traceback_disclosure(
    origin: str,
) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(origin), runner)

    assert str(raised.value) == "template comparison failed"
    assert origin not in "".join(traceback.format_exception(raised.value))
    assert runner.calls == []


@pytest.mark.parametrize(
    "contents",
    [
        "[not, a, mapping]",
        "_src_path: https://git.example.test/org/template.git\n",
        f"_commit: {_CURRENT_COMMIT}\n",
        "_src_path: [wrong]\n_commit: [wrong]\n",
        "_src_path: https://git.example.test/org/template.git\n_commit: 42\n",
        "_src_path: [unterminated\n",
        "_src_path: https://git.example.test/org/template.git\n_commit: -c core.hooksPath=evil\n",
        "_src_path: https://git.example.test/org/template.git\n_commit: 'main branch'\n",
        "_src_path: https://git.example.test/org/template.git\n_commit: 'main;cat /secret'\n",
        "_src_path: https://git.example.test/org/template.git\n_commit: 'main\\u0000evil'\n",
    ],
)
def test_comparison_rejects_invalid_copier_metadata_without_running_commands(
    contents: str,
) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError, match="^template comparison failed$"):
        compare(lambda _path: contents, runner)

    assert runner.calls == []


def test_comparison_fetches_and_resolves_both_revisions_independently_with_fixed_argv() -> (
    None
):
    runner = FakeRunner()

    result = compare(lambda _path: answers(), runner)

    commands = [call.argv for call in runner.calls]
    assert commands == [
        ("git", "init", "--bare", "/private/repository"),
        (
            "git",
            "-C",
            "/private/repository",
            "fetch",
            "--no-tags",
            _SAFE_ORIGIN,
            f"+{_CURRENT_COMMIT}:refs/agent-work/current",
        ),
        (
            "git",
            "-C",
            "/private/repository",
            "rev-parse",
            "--verify",
            "refs/agent-work/current^{commit}",
        ),
        (
            "git",
            "-C",
            "/private/repository",
            "fetch",
            "--no-tags",
            _SAFE_ORIGIN,
            "+candidate:refs/agent-work/candidate",
        ),
        (
            "git",
            "-C",
            "/private/repository",
            "rev-parse",
            "--verify",
            "refs/agent-work/candidate^{commit}",
        ),
        (
            "git",
            "-C",
            "/private/repository",
            "diff",
            "--name-only",
            "-z",
            _CURRENT_COMMIT,
            _CANDIDATE_COMMIT,
            "--",
        ),
    ]
    assert {call.timeout_seconds for call in runner.calls} == {7.5}
    assert all(isinstance(call.argv, tuple) for call in runner.calls)
    assert result.paths == ("README.md", "src/a.py")


def test_comparison_rejects_a_non_answers_file_before_reading_it() -> None:
    runner = FakeRunner()
    reads: list[Path] = []

    def recording_read(path: Path) -> str:
        reads.append(path)
        return answers()

    with pytest.raises(TemplateComparisonError, match="^template comparison failed$"):
        compare_template_revisions(
            Path("untrusted.yml"),
            "candidate",
            read_text=recording_read,
            run_command=runner,
            private_repository=private_repository,
            timeout_seconds=7.5,
        )

    assert reads == []
    assert runner.calls == []


@pytest.mark.parametrize(
    "revision", ["", "-c", "HEAD;cat /secret", "refs/heads/main\x00evil", "bad ref"]
)
def test_comparison_rejects_invalid_candidate_revision_before_any_command(
    revision: str,
) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError, match="^template comparison failed$"):
        compare(lambda _path: answers(), runner, candidate_revision=revision)

    assert runner.calls == []


def test_comparison_rejects_invalid_utf8_diff_output_without_leaking_output() -> None:
    runner = FakeRunner(diff_stdout=b"safe.py\x00\xffsecret\x00")

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(), runner)

    assert str(raised.value) == "template comparison failed"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize("output", [b"", b"src/a.py"])
def test_comparison_accepts_no_changes_and_rejects_malformed_nul_output(
    output: bytes,
) -> None:
    runner = FakeRunner(diff_stdout=output)

    if output == b"":
        assert compare(lambda _path: answers(), runner).paths == ()
    else:
        with pytest.raises(
            TemplateComparisonError, match="^template comparison failed$"
        ):
            compare(lambda _path: answers(), runner)


@pytest.mark.parametrize(
    "resolved",
    [b"not-a-commit\n", b"A" * 40 + b"\n", b"a" * 39 + b"\n", b"a" * 40 + b"\nextra\n"],
)
def test_comparison_rejects_malformed_rev_parse_output_without_leaking_it(
    resolved: bytes,
) -> None:
    class MalformedRevisionRunner(FakeRunner):
        def __call__(
            self, argv: tuple[str, ...], *, timeout_seconds: float
        ) -> CommandResult:
            if "rev-parse" in argv:
                self.calls.append(RecordedCommand(argv, timeout_seconds))
                return CommandResult(0, resolved, b"revision-secret")
            return super().__call__(argv, timeout_seconds=timeout_seconds)

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(), MalformedRevisionRunner())

    assert str(raised.value) == "template comparison failed"
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/absolute",
        "../traverse",
        "a/../../b",
        r"windows\\path",
        "C:drive",
        "-option-spoof",
        "a\nspoof",
        "a\u202ebidi",
        "a\u200dformat",
        "a\u009bcontrol",
    ],
)
def test_comparison_rejects_unsafe_paths_from_git(path: str) -> None:
    runner = FakeRunner(diff_stdout=path.encode("utf-8") + b"\x00")

    with pytest.raises(TemplateComparisonError, match="^template comparison failed$"):
        compare(lambda _path: answers(), runner)


@pytest.mark.parametrize(
    "failure",
    [
        lambda: OSError(_SECRET),
        lambda: RuntimeError(_SECRET),
        lambda: subprocess.TimeoutExpired(("git",), 7.5, output=_SECRET),
    ],
)
def test_comparison_sanitizes_read_and_process_start_failures(
    failure: Callable[[], BaseException],
) -> None:
    def failing_read(_path: Path) -> str:
        raise failure()

    with pytest.raises(TemplateComparisonError) as raised:
        compare(failing_read, FakeRunner())

    assert str(raised.value) == "template comparison failed"
    assert "super-secret" not in str(raised.value)
    assert "super-secret" not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("failure_index", range(6))
def test_comparison_sanitizes_nonzero_command_stderr_at_every_protocol_step(
    failure_index: int,
) -> None:
    class FailingRunner(FakeRunner):
        def __call__(
            self, argv: tuple[str, ...], *, timeout_seconds: float
        ) -> CommandResult:
            result = super().__call__(argv, timeout_seconds=timeout_seconds)
            if len(self.calls) == failure_index + 1:
                return CommandResult(128, b"output-secret", b"stderr-secret")
            return result

    runner = FailingRunner()
    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(), runner)

    assert str(raised.value) == "template comparison failed"
    assert "secret" not in str(raised.value)
    assert len(runner.calls) == failure_index + 1


def test_comparison_sanitizes_timeout_and_cleanup_failure() -> None:
    class TimeoutRunner(FakeRunner):
        def __call__(
            self, argv: tuple[str, ...], *, timeout_seconds: float
        ) -> CommandResult:
            raise subprocess.TimeoutExpired(
                ("git",), timeout_seconds, output=b"timeout-secret"
            )

    @contextmanager
    def failing_cleanup() -> Iterator[Path]:
        yield Path("/private/repository")
        raise OSError("cleanup-secret")

    for runner, repository in (
        (TimeoutRunner(), private_repository),
        (FakeRunner(), failing_cleanup),
    ):
        with pytest.raises(TemplateComparisonError) as raised:
            compare(lambda _path: answers(), runner, repository=repository)
        assert str(raised.value) == "template comparison failed"
        assert "secret" not in str(raised.value)
        assert "secret" not in "".join(traceback.format_exception(raised.value))


@given(st.text().map(lambda value: f"-{value}"))
def test_invalid_candidate_revisions_never_echo_untrusted_input(value: str) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(), runner, candidate_revision=value)

    assert str(raised.value) == "template comparison failed"
    assert value not in str(raised.value)


@given(st.text().map(lambda value: f"-{value}"))
def test_invalid_origins_never_echo_untrusted_input(value: str) -> None:
    runner = FakeRunner()

    with pytest.raises(TemplateComparisonError) as raised:
        compare(lambda _path: answers(value), runner)

    assert str(raised.value) == "template comparison failed"
    assert value not in str(raised.value)


@given(
    st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd")), min_size=1
        )
    )
)
def test_valid_diff_paths_are_deterministically_sorted_and_deduplicated(
    paths: list[str],
) -> None:
    assume(paths)
    runner = FakeRunner(diff_stdout="\x00".join(paths).encode() + b"\x00")

    result = compare(lambda _path: answers(), runner)

    assert result.paths == tuple(sorted(set(paths)))


def test_root_and_template_comparison_modules_and_yaml_dependencies_are_identical() -> (
    None
):
    root = Path(__file__).parents[1]
    root_module = root / "src/agent_work/template_comparison.py"
    template_module = root / "template/src/agent_work/template_comparison.py"
    root_dependency = re.search(
        r'^\s*"(PyYAML[^\"]*)",\s*$',
        (root / "pyproject.toml").read_text(),
        re.MULTILINE,
    )
    template_dependency = re.search(
        r'^\s*"(PyYAML[^\"]*)",\s*$',
        (root / "template/pyproject.toml.jinja").read_text(),
        re.MULTILINE,
    )

    assert root_module.read_bytes() == template_module.read_bytes()
    assert root_dependency is not None
    assert template_dependency is not None
    assert root_dependency.group(1) == template_dependency.group(1)
