# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Check selected Markdown prose against a Flesch-Kincaid grade limit."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, TextIO


class Filesystem(Protocol):
    """Filesystem operations used by the readability checker."""

    def is_file(self, path: Path) -> bool:
        """Return whether a path is a regular file."""

    def is_directory(self, path: Path) -> bool:
        """Return whether a path is a directory."""

    def read_text(self, path: Path) -> str:
        """Read UTF-8 text from a file."""

    def resolve(self, path: Path) -> Path:
        """Return a canonical filesystem path."""


class CommandRunner(Protocol):
    """Git command boundary used to discover selected paths."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        encoding: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and return its completed result."""


@dataclass(frozen=True)
class LocalFilesystem:
    """Real local filesystem boundary."""

    def is_file(self, path: Path) -> bool:
        """Return whether a path is a regular file."""
        return path.is_file()

    def is_directory(self, path: Path) -> bool:
        """Return whether a path is a directory."""
        return path.is_dir()

    def read_text(self, path: Path) -> str:
        """Read UTF-8 text from a file."""
        return path.read_text(encoding="utf-8")

    def resolve(self, path: Path) -> Path:
        """Return a canonical filesystem path."""
        return path.resolve()


@dataclass(frozen=True)
class Output:
    """Injected output streams."""

    stdout: TextIO
    stderr: TextIO


@dataclass(frozen=True)
class Finding:
    """One scored writing file."""

    path: Path
    grade: float


@dataclass(frozen=True)
class CheckResult:
    """Immutable result returned by a readability check."""

    findings: tuple[Finding, ...]
    max_grade: float

    @property
    def violations(self) -> tuple[Finding, ...]:
        """Return findings whose grade exceeds the configured limit."""
        return tuple(
            finding for finding in self.findings if finding.grade > self.max_grade
        )


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        add_help=False,
        description="Check selected Markdown prose without changing files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--root", type=Path, default=Path("."), help="repository root")
    parser.add_argument(
        "--scope",
        choices=("files", "changed", "all"),
        default="files",
        help="selection mode",
    )
    parser.add_argument("--paths", nargs="+", help="paths for files scope")
    parser.add_argument("--base-ref", help="Git base for changed scope")
    parser.add_argument("--max-grade", type=float, default=8.0, help="highest grade")
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="write JSON"
    )
    parser.add_argument(
        "--debug", "-d", action="store_true", help="show selection detail"
    )
    return parser


def parse_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    """Parse the checker command-line arguments."""
    return _parser().parse_args(arguments)


def _help() -> str:
    return _parser().format_help()


def _validate_arguments(options: argparse.Namespace) -> None:
    if not math.isfinite(options.max_grade):
        raise ValueError("--max-grade must be finite")
    if options.scope == "files" and not options.paths:
        raise ValueError("--paths is required with --scope files")
    if options.scope != "files" and options.paths:
        raise ValueError("--paths is only valid with --scope files")
    if options.scope == "changed" and not options.base_ref:
        raise ValueError("--base-ref is required with --scope changed")
    if options.scope != "changed" and options.base_ref:
        raise ValueError("--base-ref is only valid with --scope changed")


def _is_writing_path(path: Path) -> bool:
    return path.name.endswith((".md", ".md.jinja"))


def _inside_root(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _selected_paths(
    options: argparse.Namespace,
    root: Path,
    filesystem: Filesystem,
    run_command: CommandRunner,
) -> tuple[Path, ...]:
    if options.scope == "files":
        selected = tuple(Path(value) for value in options.paths)
    else:
        command = (
            (
                "git",
                "-C",
                str(root),
                "diff",
                "--name-only",
                "-z",
                options.base_ref,
                "--",
            )
            if options.scope == "changed"
            else ("git", "-C", str(root), "ls-files", "-z")
        )
        result = run_command(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode:
            raise OSError(result.stderr.strip() or "Git path discovery failed")
        selected = tuple(Path(value) for value in result.stdout.split("\0") if value)
    resolved: list[Path] = []
    for relative in selected:
        candidate = filesystem.resolve(root / relative)
        if not _inside_root(root, candidate):
            raise ValueError(f"path is outside --root: {relative}")
        if filesystem.is_file(candidate) and _is_writing_path(relative):
            resolved.append(candidate.relative_to(root))
    return tuple(sorted(set(resolved), key=lambda path: path.as_posix()))


def extract_prose(markdown: str) -> str:
    """Return human prose after removing non-prose Markdown structures."""
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, count=1, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines: list[str] = []
    in_fence = False
    in_table = False
    source_lines = text.splitlines()
    for index, line in enumerate(source_lines):
        if re.match(r"^ {0,3}(```|~~~)", line):
            in_fence = not in_fence
            continue
        if in_fence or line.startswith(("    ", "\t")) or line.lstrip().startswith("|"):
            continue
        next_line = source_lines[index + 1] if index + 1 < len(source_lines) else ""
        is_table_header = "|" in line and bool(
            re.fullmatch(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*", next_line)
        )
        if is_table_header or (in_table and "|" in line):
            in_table = True
            continue
        if in_table:
            in_table = False
        line = re.sub(r"`[^`]*`", "", line)
        line = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"^\s{0,3}(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+|>\s?)", "", line)
        line = re.sub(r"([*_~])\1?", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _render(result: CheckResult, *, json_output: bool) -> str:
    if json_output:
        payload = {
            "schema_version": 1,
            "outcome": "violations" if result.violations else "compliant",
            "files": [
                {"path": finding.path.as_posix(), "grade": finding.grade}
                for finding in result.findings
            ],
        }
        return f"{json.dumps(payload, sort_keys=True)}\n"
    return "".join(
        f"{finding.path.as_posix()}: grade {finding.grade}\n"
        for finding in result.findings
    )


def _error(message: str, *, json_output: bool, output: Output) -> int:
    if json_output:
        output.stdout.write(
            json.dumps({"schema_version": 1, "outcome": "error", "error": message})
            + "\n"
        )
    else:
        output.stderr.write(f"error: {message}\n")
    return 2


def main(
    arguments: Sequence[str] | None = None,
    *,
    filesystem: Filesystem | None = None,
    run_command: CommandRunner | None = None,
    score_text: Callable[[str], float] | None = None,
    output: Output | None = None,
) -> int:
    """Run the read-only checker and return its documented exit status."""
    command_output = output or Output(sys.stdout, sys.stderr)
    raw_arguments = tuple(arguments or ())
    if "--help" in raw_arguments or "-h" in raw_arguments:
        command_output.stdout.write(_help())
        return 0
    try:
        options = parse_arguments(raw_arguments)
        _validate_arguments(options)
        active_filesystem = filesystem or LocalFilesystem()
        root = active_filesystem.resolve(options.root)
        if not active_filesystem.is_directory(root):
            raise ValueError(f"--root is not a directory: {options.root}")
        active_runner = run_command or _run_command
        paths = _selected_paths(options, root, active_filesystem, active_runner)
        active_scorer = score_text or _score_text
        findings = tuple(
            Finding(path, active_scorer(prose))
            for path in paths
            if (prose := extract_prose(active_filesystem.read_text(root / path)))
        )
        result = CheckResult(findings, options.max_grade)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        json_output = "--json" in raw_arguments
        return _error(str(error), json_output=json_output, output=command_output)
    if options.debug and not options.json_output:
        command_output.stderr.write(f"debug: selected {len(paths)} writing files\n")
    command_output.stdout.write(_render(result, json_output=options.json_output))
    return 1 if result.violations else 0


def _run_command(
    command: Sequence[str],
    *,
    check: bool,
    capture_output: bool,
    text: bool,
    encoding: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=capture_output,
        text=text,
        encoding=encoding,
    )


def _score_text(text: str) -> float:
    from textstat import textstat

    return float(textstat.flesch_kincaid_grade(text))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
