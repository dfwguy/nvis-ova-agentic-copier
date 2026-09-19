# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only writing readability checker."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from math import nan
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work import readability


def _run_checker(
    root: Path,
    *arguments: str,
    scores: Sequence[float] = (),
    command_output: str = "",
    command_status: int = 0,
) -> tuple[int, str, str, list[str], list[tuple[str, ...]]]:
    """Run the checker through its injected scoring and Git boundaries."""
    stdout, stderr = io.StringIO(), io.StringIO()
    scored_text: list[str] = []
    commands: list[tuple[str, ...]] = []
    pending_scores = iter(scores)

    def score_text(text: str) -> float:
        scored_text.append(text)
        return next(pending_scores)

    def run_command(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command, command_status, stdout=command_output, stderr="Git failed"
        )

    status = readability.main(
        ["--root", str(root), *arguments],
        filesystem=readability.LocalFilesystem(),
        run_command=run_command,
        score_text=score_text,
        output=readability.Output(stdout, stderr),
    )
    return status, stdout.getvalue(), stderr.getvalue(), scored_text, commands


def test_help_documents_each_read_only_scope_and_never_offers_apply(
    tmp_path: Path,
) -> None:
    """The public CLI names its scopes and stays explicitly read-only."""
    status, stdout, stderr, _scored, _commands = _run_checker(tmp_path, "--help")

    assert status == 0
    assert stderr == ""
    for option in (
        "--root",
        "--scope",
        "--paths",
        "--base-ref",
        "--max-grade",
        "--json",
        "--debug",
    ):
        assert option in stdout
    assert "--apply" not in stdout


@pytest.mark.parametrize(
    "arguments",
    (
        ("--scope", "files"),
        ("--scope", "changed"),
        ("--scope", "all", "--base-ref", "origin/main"),
        ("--scope", "files", "--base-ref", "origin/main", "--paths", "guide.md"),
        ("--scope", "changed", "--base-ref", "origin/main", "--paths", "guide.md"),
    ),
)
def test_invalid_scope_combinations_return_usage_error_json(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    """Every incompatible scope option remains inside the JSON error protocol."""
    status, stdout, stderr, _scored, _commands = _run_checker(
        tmp_path, *arguments, "--json"
    )

    assert status == 2
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["outcome"] == "error"
    assert payload["schema_version"] == 1
    assert payload["error"]


@pytest.mark.parametrize(("score", "expected_status"), ((8.0, 0), (8.01, 1)))
def test_grade_boundary_is_inclusive_at_eight(
    tmp_path: Path, score: float, expected_status: int
) -> None:
    """Grade eight passes and only a score above eight violates the policy."""
    (tmp_path / "guide.md").write_text("A short sentence.\n")

    status, stdout, stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "guide.md",
        scores=(score,),
    )

    assert status == expected_status
    assert stderr == ""
    assert scored == ["A short sentence."]
    assert "guide.md" in stdout
    assert str(score) in stdout


def test_files_scope_scores_the_entire_selected_file_not_only_changed_text(
    tmp_path: Path,
) -> None:
    """A difficult unchanged paragraph still fails a changed writing file."""
    (tmp_path / "guide.md").write_text(
        "Simple sentence.\n"
        "An extraordinarily incomprehensible institutionalization sentence remains.\n"
    )

    status, _stdout, _stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "guide.md",
        scores=(9.0,),
    )

    assert status == 1
    assert scored == [
        (
            "Simple sentence.\n"
            "An extraordinarily incomprehensible institutionalization sentence remains."
        )
    ]


def test_max_grade_changes_the_violation_threshold(tmp_path: Path) -> None:
    """A caller can set a stricter finite grade threshold."""
    (tmp_path / "guide.md").write_text("Readable prose.\n")

    status, _stdout, _stderr, _scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "guide.md",
        "--max-grade",
        "4",
        scores=(4.1,),
    )

    assert status == 1


def test_non_finite_max_grade_is_a_usage_error(tmp_path: Path) -> None:
    """A non-finite grade cannot disable the policy."""
    status, stdout, stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "guide.md",
        "--max-grade",
        str(nan),
        "--json",
    )

    assert status == 2
    assert stderr == ""
    assert scored == []
    assert json.loads(stdout)["outcome"] == "error"


def test_explicit_paths_ignore_deleted_and_non_writing_files(
    tmp_path: Path,
) -> None:
    """Only existing tracked Markdown and Markdown-Jinja files receive scores."""
    (tmp_path / "guide.md").write_text("Readable prose.\n")
    (tmp_path / "template.md.jinja").write_text("Rendered readable prose.\n")
    (tmp_path / "notes.txt").write_text("This must not be read.\n")

    status, _stdout, _stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "guide.md",
        "template.md.jinja",
        "notes.txt",
        "deleted.md",
        scores=(1.0, 2.0),
    )

    assert status == 0
    assert scored == ["Readable prose.", "Rendered readable prose."]


def test_paths_outside_root_are_operational_errors_without_scoring(
    tmp_path: Path,
) -> None:
    """Root-relative path selection cannot escape the requested repository root."""
    outside = tmp_path.parent / "outside.md"
    outside.write_text("Never score this text.\n")

    status, stdout, stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "../outside.md",
        "--json",
    )

    assert status == 2
    assert stderr == ""
    assert scored == []
    assert json.loads(stdout)["outcome"] == "error"


def test_changed_scope_uses_the_requested_base_and_nul_safe_git_file_list(
    tmp_path: Path,
) -> None:
    """Changed selection asks Git for names against the exact supplied base."""
    (tmp_path / "one.md").write_text("One readable sentence.\n")
    (tmp_path / "two.md.jinja").write_text("Two readable sentences.\n")

    status, _stdout, _stderr, scored, commands = _run_checker(
        tmp_path,
        "--scope",
        "changed",
        "--base-ref",
        "origin/main",
        scores=(2.0, 3.0),
        command_output="one.md\x00two.md.jinja\x00deleted.md\x00",
    )

    assert status == 0
    assert scored == ["One readable sentence.", "Two readable sentences."]
    assert len(commands) == 1
    assert "diff" in commands[0]
    assert "--name-only" in commands[0]
    assert "-z" in commands[0]
    assert "origin/main" in commands[0]


def test_git_discovery_failure_is_an_operational_error(tmp_path: Path) -> None:
    """A failed Git query cannot become an empty passing selection."""
    status, stdout, stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "changed",
        "--base-ref",
        "origin/main",
        "--json",
        command_status=1,
    )

    assert status == 2
    assert stderr == ""
    assert scored == []
    assert json.loads(stdout)["outcome"] == "error"


def test_all_scope_uses_nul_safe_tracked_file_discovery(tmp_path: Path) -> None:
    """All selection discovers tracked paths and filters them to writing files."""
    (tmp_path / "guide.md").write_text("Readable guide.\n")
    (tmp_path / "source.py").write_text("not prose\n")

    status, _stdout, _stderr, scored, commands = _run_checker(
        tmp_path,
        "--scope",
        "all",
        scores=(4.0,),
        command_output="guide.md\x00source.py\x00",
    )

    assert status == 0
    assert scored == ["Readable guide."]
    assert len(commands) == 1
    assert "ls-files" in commands[0]
    assert "-z" in commands[0]


def test_empty_or_non_prose_files_pass_without_calling_the_scorer(
    tmp_path: Path,
) -> None:
    """Files that yield no human prose pass and never invoke textstat."""
    (tmp_path / "generated.md").write_text(
        "---\ntitle: Generated\n---\n"
        "<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->\n"
        "```python\nprint('ignored')\n```\n"
        "    indented_code()\n"
        "| heading | value |\n| --- | --- |\n| cell | cell |\n"
    )

    status, _stdout, _stderr, scored, _commands = _run_checker(
        tmp_path,
        "--scope",
        "files",
        "--paths",
        "generated.md",
    )

    assert status == 0
    assert scored == []


def test_extract_prose_removes_markup_and_keeps_human_readable_words() -> None:
    """Extraction excludes structural Markdown while preserving ordinary prose."""
    prose = readability.extract_prose(
        "---\ntitle: Ignore metadata\n---\n"
        "<!-- private license comment -->\n"
        "# A heading with `inline_code`\n"
        "Read [the guide](https://example.test/docs) before continuing.\n"
        "Visit https://example.test/ignored directly.\n"
        "```bash\necho ignored\n```\n"
        "    ignored_indented_code\n"
        "| ignored | table |\n| --- | --- |\n| ignored | row |\n"
        "Final readable prose remains.\n"
    )

    assert prose == (
        "A heading with\n"
        "Read the guide before continuing.\n"
        "Visit directly.\n"
        "Final readable prose remains."
    )


@given(st.text(alphabet=st.characters(blacklist_characters="\n`"), min_size=1))
def test_extract_prose_excludes_arbitrary_fenced_content(markdown: str) -> None:
    """Arbitrary fenced content cannot enter the prose sent to the scorer."""
    marker = f"FENCE:{markdown}:END"
    prose = readability.extract_prose(f"Before.\n```text\n{marker}\n```\nAfter.\n")

    assert marker not in prose
    assert prose == "Before.\nAfter."


def test_json_and_human_diagnostics_are_deterministic_and_include_scores(
    tmp_path: Path,
) -> None:
    """Repeated runs produce stable per-file grade diagnostics in both formats."""
    (tmp_path / "b.md").write_text("Second sentence.\n")
    (tmp_path / "a.md").write_text("First sentence.\n")
    arguments = ("--scope", "files", "--paths", "b.md", "a.md")

    first = _run_checker(tmp_path, *arguments, scores=(9.0, 7.0))
    second = _run_checker(tmp_path, *arguments, scores=(9.0, 7.0))
    json_status, json_stdout, json_stderr, _scored, _commands = _run_checker(
        tmp_path, *arguments, "--json", scores=(9.0, 7.0)
    )

    assert first[:3] == second[:3]
    assert first[0] == 1
    assert "a.md" in first[1]
    assert "b.md" in first[1]
    assert "7.0" in first[1]
    assert "9.0" in first[1]
    assert json_status == 1
    assert json_stderr == ""
    payload = json.loads(json_stdout)
    assert payload["schema_version"] == 1
    assert payload["outcome"] == "violations"
    assert "a.md" in json_stdout
    assert "b.md" in json_stdout
    assert "9.0" in json_stdout
