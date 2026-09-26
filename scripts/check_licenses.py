#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Check maintained files for the canonical NVIDIA proprietary license."""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, TextIO

from pydantic import BaseModel, ConfigDict, ValidationError


class LicensePolicy(BaseModel):
    """Validated license-policy configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    copyright: str
    identifier: str
    notice: tuple[str, ...]
    hash_suffixes: tuple[str, ...]
    markdown_suffixes: tuple[str, ...]
    hash_names: tuple[str, ...]
    annotation_suffixes: tuple[str, ...]
    annotations_file: str
    exclude_paths: tuple[str, ...]
    exclude_globs: tuple[str, ...]


class LicenseAnnotations(BaseModel):
    """Validated external license annotations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paths: tuple[str, ...]


class Filesystem(Protocol):
    """Filesystem operations used by the license checker."""

    def list_directory(self, path: Path) -> tuple[Path, ...]:
        """Return the direct children of a directory."""

    def is_file(self, path: Path) -> bool:
        """Return whether path is a regular file."""

    def is_directory(self, path: Path) -> bool:
        """Return whether path is a directory."""

    def is_symlink(self, path: Path) -> bool:
        """Return whether path is a symbolic link."""

    def read_text(self, path: Path) -> str:
        """Read a UTF-8 text file."""

    def resolve(self, path: Path) -> Path:
        """Return the canonical path at the filesystem boundary."""


class CommandRunner(Protocol):
    """Command boundary used to discover Git-maintained paths."""

    def __call__(
        self,
        command: Sequence[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
        encoding: str,
    ) -> subprocess.CompletedProcess[str]:
        """Run one command and return its completed result."""


class TreeReadError(OSError):
    """A requested candidate Git blob could not be read."""


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


@dataclass(frozen=True)
class LocalFilesystem:
    """Real local filesystem boundary."""

    def list_directory(self, path: Path) -> tuple[Path, ...]:
        """Return the direct children of a directory."""
        return tuple(path.iterdir())

    def is_file(self, path: Path) -> bool:
        """Return whether path is a regular file."""
        return path.is_file()

    def is_directory(self, path: Path) -> bool:
        """Return whether path is a directory."""
        return path.is_dir()

    def is_symlink(self, path: Path) -> bool:
        """Return whether path is a symbolic link."""
        return path.is_symlink()

    def read_text(self, path: Path) -> str:
        """Read a UTF-8 text file."""
        return path.read_text(encoding="utf-8")

    def resolve(self, path: Path) -> Path:
        """Return the canonical path."""
        return path.resolve()


@dataclass(frozen=True)
class TreeFilesystem:
    """Read only files and symlinks from one immutable Git tree."""

    root: Path
    entries: dict[Path, tuple[str, str]]
    contents: dict[str, str]
    run: CommandRunner

    @classmethod
    def load(cls, root: Path, tree: str, run: CommandRunner) -> TreeFilesystem:
        """Load the tree listing through the injected command runner."""
        listing = run(
            ("git", "-C", str(root), "ls-tree", "-r", "-z", tree),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if listing.returncode != 0:
            raise OSError("cannot read candidate Git tree")
        entries: dict[Path, tuple[str, str]] = {}
        for record in listing.stdout.split("\0"):
            if not record:
                continue
            metadata, separator, name = record.partition("\t")
            fields = metadata.split(" ")
            relative = Path(name)
            if (
                not separator
                or len(fields) != 3
                or fields[0] not in {"100644", "100755", "120000"}
                or fields[1] != "blob"
                or re.fullmatch(r"[0-9a-f]{40}", fields[2]) is None
                or relative.is_absolute()
                or not name
                or any(part in {".", ".."} for part in relative.parts)
                or relative in entries
            ):
                raise ValueError("invalid candidate Git tree listing")
            entries[relative] = (fields[0], fields[2])
        return cls(root, entries, {}, run)

    def _relative(self, path: Path) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as error:
            raise OSError("path is outside candidate Git tree") from error

    def list_directory(self, path: Path) -> tuple[Path, ...]:
        """Return direct children recorded in the tree."""
        relative = self._relative(path)
        return tuple(
            {
                path / item.parts[len(relative.parts)]
                for item in self.entries
                if len(item.parts) > len(relative.parts)
                and item.parts[: len(relative.parts)] == relative.parts
            }
        )

    def is_file(self, path: Path) -> bool:
        """Return whether the entry is a regular blob."""
        entry = self.entries.get(self._relative(path))
        return entry is not None and entry[0] != "120000"

    def is_directory(self, path: Path) -> bool:
        """Return whether the tree contains descendants of this path."""
        relative = self._relative(path)
        return relative == Path(".") or any(
            item.parts[: len(relative.parts)] == relative.parts
            and len(item.parts) > len(relative.parts)
            for item in self.entries
        )

    def is_symlink(self, path: Path) -> bool:
        """Return whether the tree entry is a symbolic link."""
        entry = self.entries.get(self._relative(path))
        return entry is not None and entry[0] == "120000"

    def read_text(self, path: Path) -> str:
        """Read a regular blob from the candidate tree."""
        entry = self.entries.get(self._relative(path))
        if entry is None or entry[0] == "120000":
            raise TreeReadError("candidate Git blob is unavailable")
        oid = entry[1]
        if oid not in self.contents:
            blob = self.run(
                ("git", "-C", str(self.root), "cat-file", "blob", oid),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if blob.returncode != 0:
                raise TreeReadError("cannot read candidate Git blob")
            self.contents[oid] = blob.stdout
        return self.contents[oid]

    def resolve(self, path: Path) -> Path:
        """Return a lexical absolute path within the tree root."""
        resolved = path if path.is_absolute() else self.root / path
        self._relative(resolved)
        return resolved


@dataclass(frozen=True)
class Finding:
    """One immutable license-policy violation."""

    path: Path
    message: str

    def diagnostic(self) -> str:
        """Render the finding for human-readable output."""
        return f"{self.path.as_posix()}: {self.message}"


@dataclass(frozen=True)
class CheckResult:
    """Immutable result of checking one repository root."""

    findings: tuple[Finding, ...]

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return deterministic human-readable diagnostics."""
        return tuple(finding.diagnostic() for finding in self.findings)


@dataclass(frozen=True)
class Output:
    """Injected command output streams."""

    stdout: TextIO
    stderr: TextIO


@dataclass(frozen=True)
class IgnoreRule:
    """One ordered rule loaded from the repository ignore file."""

    base: Path
    pattern: str
    escaped: tuple[bool, ...]
    negated: bool
    directory_only: bool
    anchored: bool


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def parse_arguments(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = _ArgumentParser(
        description="Check files for the canonical NVIDIA license header."
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--policy", type=Path, default=Path(".license-policy.toml"))
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--candidate-tree", type=str)
    parser.add_argument("--debug", "-d", action="store_true")
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(arguments)


def _load_policy(path: Path, filesystem: Filesystem) -> LicensePolicy:
    try:
        return LicensePolicy.model_validate(tomllib.loads(filesystem.read_text(path)))
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise ValueError(f"invalid license policy: {error}") from error


def _load_annotations(
    policy_path: Path,
    policy: LicensePolicy,
    filesystem: Filesystem,
) -> LicenseAnnotations:
    path = policy_path.parent / policy.annotations_file
    try:
        return LicenseAnnotations.model_validate(
            tomllib.loads(filesystem.read_text(path))
        )
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise ValueError(f"invalid license annotations: {error}") from error


def _normalized_name(path: Path) -> Path:
    return path.with_suffix("") if path.suffix == ".jinja" else path


def _header(policy: LicensePolicy, prefix: str, suffix: str = "") -> str:
    lines = (policy.copyright, policy.identifier, "", *policy.notice)
    return "\n".join(
        f"{prefix}{line}{suffix}" if suffix else f"{prefix}{line}".rstrip()
        for line in lines
    )


def _header_style(relative_path: Path, policy: LicensePolicy) -> tuple[str, str] | None:
    normalized = _normalized_name(relative_path)
    if normalized.suffix in policy.markdown_suffixes:
        return "<!-- ", " -->"
    if (
        normalized.suffix in policy.hash_suffixes
        or normalized.name in policy.hash_names
    ):
        return "# ", ""
    return None


def _is_excluded(relative_path: Path, policy: LicensePolicy) -> bool:
    value = relative_path.as_posix()
    named_directory_excluded = any(
        "/" not in excluded and excluded in relative_path.parts
        for excluded in policy.exclude_paths
    )
    rooted_path_excluded = any(
        value == excluded or value.startswith(f"{excluded}/")
        for excluded in policy.exclude_paths
    )
    return (
        named_directory_excluded
        or rooted_path_excluded
        or any(fnmatch.fnmatch(value, pattern) for pattern in policy.exclude_globs)
    )


def _is_annotated(relative_path: Path, annotations: LicenseAnnotations) -> bool:
    return relative_path.as_posix() in annotations.paths


def _repository_is_unavailable(stderr: str) -> bool:
    """Return whether Git cannot safely inspect the requested repository."""
    return any(
        marker in stderr.lower()
        for marker in ("not a git repository", "detected dubious ownership")
    )


def _load_maintained_paths(
    root: Path,
    run: CommandRunner,
) -> frozenset[Path] | None:
    result = run(
        (
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
        ),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return frozenset(Path(value) for value in result.stdout.split("\0") if value)
    if _repository_is_unavailable(result.stderr):
        return None
    raise OSError(result.stderr.strip() or "git ls-files failed")


def _load_ignore_rules(
    directory: Path,
    root: Path,
    filesystem: Filesystem,
) -> tuple[IgnoreRule, ...]:
    path = directory / ".gitignore"
    if filesystem.is_symlink(path) or not filesystem.is_file(path):
        return ()
    base = directory.relative_to(root)
    rules = []
    for raw_line in filesystem.read_text(path).splitlines():
        parsed = _parse_ignore_line(raw_line)
        if parsed is None:
            continue
        value, escaped, negated, directory_only, anchored = parsed
        rules.append(
            IgnoreRule(
                base,
                value,
                escaped,
                negated,
                directory_only,
                anchored,
            )
        )
    return tuple(rules)


def _parse_ignore_line(
    raw_line: str,
) -> tuple[str, tuple[bool, ...], bool, bool, bool] | None:
    if not raw_line or raw_line.startswith("#"):
        return None
    negated = raw_line.startswith("!")
    value = raw_line[1:] if negated else raw_line
    characters: list[tuple[str, bool]] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            characters.append((value[index + 1], True))
            index += 2
        else:
            characters.append((value[index], False))
            index += 1
    while characters and characters[-1] == (" ", False):
        characters.pop()
    if not characters:
        return None
    directory_only = characters[-1] == ("/", False)
    if directory_only:
        characters.pop()
    if not characters:
        return None
    anchored = characters[0] == ("/", False) or any(
        character == "/" and not escaped for character, escaped in characters
    )
    if characters and characters[0] == ("/", False):
        characters.pop(0)
    pattern = "".join(character for character, _escaped in characters)
    escaped = tuple(is_escaped for _character, is_escaped in characters)
    return pattern, escaped, negated, directory_only, anchored


def _split_pattern(
    pattern: str, escaped: tuple[bool, ...]
) -> tuple[tuple[str, tuple[bool, ...]], ...]:
    parts: list[tuple[str, tuple[bool, ...]]] = []
    characters: list[str] = []
    escape_flags: list[bool] = []
    for character, is_escaped in zip(pattern, escaped, strict=True):
        if character == "/" and not is_escaped:
            if characters:
                parts.append(("".join(characters), tuple(escape_flags)))
            characters = []
            escape_flags = []
        else:
            characters.append(character)
            escape_flags.append(is_escaped)
    if characters:
        parts.append(("".join(characters), tuple(escape_flags)))
    return tuple(parts)


def _character_class_pattern(
    pattern: str, escaped: tuple[bool, ...], start: int
) -> tuple[str, int] | None:
    closing = next(
        (
            index
            for index in range(start + 1, len(pattern))
            if pattern[index] == "]" and not escaped[index]
        ),
        None,
    )
    if closing is None:
        return None
    body = list(zip(pattern[start + 1 : closing], escaped[start + 1 : closing]))
    if not body:
        return None
    prefix = "^" if body[0] in {("!", False), ("^", False)} else ""
    if prefix:
        body.pop(0)
    value = "".join(
        re.escape(character)
        if is_escaped or character in {"\\", "]", "["}
        else "\\^"
        if character == "^"
        else character
        for character, is_escaped in body
    )
    return f"[{prefix}{value}]", closing + 1


def _matches_component(value: str, pattern: str, escaped: tuple[bool, ...]) -> bool:
    expression: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if escaped[index]:
            expression.append(re.escape(character))
            index += 1
        elif character == "*":
            expression.append(".*")
            index += 1
        elif character == "?":
            expression.append(".")
            index += 1
        elif character == "[" and (
            character_class := _character_class_pattern(pattern, escaped, index)
        ):
            rendered, index = character_class
            expression.append(rendered)
        else:
            expression.append(re.escape(character))
            index += 1
    try:
        return re.fullmatch("".join(expression), value) is not None
    except re.error:
        return False


def _matches_pattern_parts(
    path_parts: tuple[str, ...],
    pattern_parts: tuple[tuple[str, tuple[bool, ...]], ...],
) -> bool:
    if not pattern_parts:
        return not path_parts
    pattern, escaped = pattern_parts[0]
    if pattern == "**" and not any(escaped):
        if len(pattern_parts) == 1:
            return bool(path_parts)
        return any(
            _matches_pattern_parts(path_parts[offset:], pattern_parts[1:])
            for offset in range(len(path_parts) + 1)
        )
    return (
        bool(path_parts)
        and _matches_component(path_parts[0], pattern, escaped)
        and _matches_pattern_parts(path_parts[1:], pattern_parts[1:])
    )


def _matches_ignore_rule(
    relative_path: Path, rule: IgnoreRule, *, is_directory: bool
) -> bool:
    try:
        scoped_path = relative_path.relative_to(rule.base)
    except ValueError:
        return False
    if rule.directory_only and not is_directory:
        return False
    if rule.anchored:
        pattern_parts = _split_pattern(rule.pattern, rule.escaped)
        return _matches_pattern_parts(scoped_path.parts, pattern_parts)
    return any(
        _matches_component(part, rule.pattern, rule.escaped)
        for part in scoped_path.parts
    )


def _is_ignored(
    relative_path: Path, rules: tuple[IgnoreRule, ...], *, is_directory: bool
) -> bool:
    ignored = False
    for rule in rules:
        if _matches_ignore_rule(relative_path, rule, is_directory=is_directory):
            ignored = not rule.negated
    return ignored


def _allowed_header_offsets(content: str, relative_path: Path) -> tuple[int, ...]:
    offsets = [0]
    if (
        content.startswith("#!")
        and (newline := content.find("\n")) >= 0
        and content[2:newline].strip()
    ):
        offsets.append(newline + 1)
    normalized = _normalized_name(relative_path)
    if normalized.suffix in {".md", ".mdc"} and content.startswith("---\n"):
        closing = content.find("\n---\n", 4)
        if closing >= 0:
            offsets.append(closing + len("\n---\n"))
    return tuple(offsets)


def _has_header_at(content: str, expected: str, offset: int) -> bool:
    if not content.startswith(expected, offset):
        return False
    end = offset + len(expected)
    return end == len(content) or content[end] == "\n"


def _contains_header(content: str, expected: str) -> bool:
    offset = content.find(expected)
    while offset >= 0:
        if _has_header_at(content, expected, offset):
            return True
        offset = content.find(expected, offset + 1)
    return False


def _check_path(
    path: Path,
    relative_path: Path,
    policy: LicensePolicy,
    annotations: LicenseAnnotations,
    filesystem: Filesystem,
) -> str | None:
    annotated = _is_annotated(relative_path, annotations)
    if filesystem.is_symlink(path):
        return None if annotated else "external license annotation required for symlink"

    normalized = _normalized_name(relative_path)
    if normalized.suffix in policy.annotation_suffixes:
        return None if annotated else "external license annotation required"

    style = _header_style(relative_path, policy)
    if style is None:
        return "unsupported file type"

    try:
        content = filesystem.read_text(path)
    except TreeReadError:
        raise
    except (OSError, UnicodeDecodeError):
        return "unsupported file type"
    if annotated and content == "<!-- agent-workflow-legacy-project-guard -->\n":
        return None
    expected = _header(policy, *style)
    if any(
        _has_header_at(content, expected, offset)
        for offset in _allowed_header_offsets(content, relative_path)
    ):
        return None
    if _contains_header(content, expected):
        return "misplaced NVIDIA license header"
    if policy.copyright in content or policy.identifier in content:
        return "incomplete NVIDIA license header"
    return "missing NVIDIA license header"


def _candidate_paths(
    root: Path,
    policy: LicensePolicy,
    filesystem: Filesystem,
    logger: logging.Logger,
    ignore_rules: tuple[IgnoreRule, ...],
    maintained_paths: frozenset[Path] | None,
) -> tuple[Path, ...]:
    maintained_directories = (
        frozenset(
            parent
            for path in maintained_paths
            for parent in path.parents
            if parent != Path(".")
        )
        if maintained_paths is not None
        else frozenset()
    )

    def visit(
        directory: Path,
        inherited_ignore_rules: tuple[IgnoreRule, ...],
    ) -> tuple[Path, ...]:
        candidates: list[Path] = []
        active_ignore_rules = (
            inherited_ignore_rules + _load_ignore_rules(directory, root, filesystem)
            if maintained_paths is None and directory != root
            else inherited_ignore_rules
        )
        children = sorted(
            filesystem.list_directory(directory), key=lambda path: path.name
        )
        for child in children:
            relative_path = child.relative_to(root)
            if maintained_paths is not None and (
                relative_path not in maintained_paths
                and relative_path not in maintained_directories
            ):
                logger.debug("unmaintained path=%s", relative_path.as_posix())
                continue
            is_symlink = filesystem.is_symlink(child)
            is_directory = not is_symlink and filesystem.is_directory(child)
            if _is_excluded(relative_path, policy) or _is_ignored(
                relative_path,
                active_ignore_rules,
                is_directory=is_directory,
            ):
                logger.debug("excluded path=%s", relative_path.as_posix())
                continue
            if is_symlink:
                candidates.append(relative_path)
            elif is_directory:
                candidates.extend(visit(child, active_ignore_rules))
            elif filesystem.is_file(child):
                candidates.append(relative_path)
        return tuple(candidates)

    return visit(root, ignore_rules)


def check(
    root: Path,
    policy: LicensePolicy,
    annotations: LicenseAnnotations,
    *,
    filesystem: Filesystem,
    logger: logging.Logger,
    ignore_rules: tuple[IgnoreRule, ...] = (),
    maintained_paths: frozenset[Path] | None = None,
) -> CheckResult:
    """Return an immutable result for every policy violation below root."""
    findings = []
    for relative_path in _candidate_paths(
        root, policy, filesystem, logger, ignore_rules, maintained_paths
    ):
        logger.debug("classifying path=%s", relative_path.as_posix())
        diagnostic = _check_path(
            root / relative_path,
            relative_path,
            policy,
            annotations,
            filesystem,
        )
        if diagnostic:
            findings.append(Finding(relative_path, diagnostic))
    return CheckResult(tuple(findings))


def check_root(
    root: Path,
    policy: LicensePolicy,
    annotations: LicenseAnnotations,
    *,
    filesystem: Filesystem,
    logger: logging.Logger | None = None,
) -> tuple[str, ...]:
    """Return deterministic diagnostics for every policy violation below root."""
    local_logger = logger or logging.getLoggerClass()("license-check.null")
    if logger is None:
        local_logger.addHandler(logging.NullHandler())
    return check(
        root,
        policy,
        annotations,
        filesystem=filesystem,
        logger=local_logger,
    ).diagnostics


def _build_local_logger(
    root: Path,
    *,
    debug: bool,
    stream: TextIO | None,
) -> logging.Logger:
    logger = logging.getLoggerClass()(
        f"license-check:{root.as_posix()}", level=logging.DEBUG
    )
    logger.propagate = False
    if debug and stream is not None:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setLevel(logging.DEBUG)
        stream_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stream_handler)
    else:
        logger.addHandler(logging.NullHandler())
    return logger


def _emit_error(message: str, *, json_output: bool, output: Output) -> None:
    if json_output:
        json.dump(
            {"schema_version": 1, "outcome": "error", "error": message},
            output.stdout,
        )
        output.stdout.write("\n")
    else:
        output.stderr.write(f"{message}\n")


def _emit_result(result: CheckResult, *, json_output: bool, output: Output) -> None:
    if json_output:
        json.dump(
            {
                "schema_version": 1,
                "outcome": "violations" if result.findings else "compliant",
                "violations": [
                    {"path": finding.path.as_posix(), "message": finding.message}
                    for finding in result.findings
                ],
            },
            output.stdout,
        )
        output.stdout.write("\n")
    elif result.findings:
        output.stderr.write("\n".join(result.diagnostics) + "\n")


def main(
    arguments: Sequence[str] | None = None,
    *,
    filesystem: Filesystem | None = None,
    output: Output | None = None,
    logger_factory: Callable[[Path], logging.Logger] | None = None,
    run_command: CommandRunner | None = None,
) -> int:
    """Run the read-only license check and return its documented exit status."""
    command_output = output or Output(sys.stdout, sys.stderr)
    effective_arguments = tuple(sys.argv[1:] if arguments is None else arguments)
    json_output = "--json" in effective_arguments
    try:
        namespace = parse_arguments(effective_arguments)
    except ValueError as error:
        _emit_error(str(error), json_output=json_output, output=command_output)
        return 2
    if namespace.apply:
        _emit_error(
            "--apply is unsupported because the license checker is read-only",
            json_output=namespace.json_output,
            output=command_output,
        )
        return 2

    if (
        namespace.candidate_tree is not None
        and re.fullmatch(r"[0-9a-f]{40}", namespace.candidate_tree) is None
    ):
        _emit_error(
            "invalid candidate Git tree",
            json_output=namespace.json_output,
            output=command_output,
        )
        return 2
    runtime_exit = 1 if namespace.candidate_tree is not None else 2
    local_filesystem = filesystem or LocalFilesystem()
    try:
        root = local_filesystem.resolve(namespace.root or Path("."))
    except (OSError, RuntimeError, ValueError) as error:
        _emit_error(
            str(error),
            json_output=namespace.json_output,
            output=command_output,
        )
        return runtime_exit
    if not local_filesystem.is_directory(root):
        _emit_error(
            f"invalid root directory: {root}",
            json_output=namespace.json_output,
            output=command_output,
        )
        return runtime_exit
    try:
        active_filesystem: Filesystem = (
            TreeFilesystem.load(
                root, namespace.candidate_tree, run_command or _run_command
            )
            if namespace.candidate_tree is not None
            else local_filesystem
        )
        policy_argument = (
            root / namespace.policy
            if namespace.candidate_tree is not None
            and not namespace.policy.is_absolute()
            else namespace.policy
        )
        policy_path = active_filesystem.resolve(policy_argument)
        logger = (
            logger_factory(policy_path.parent)
            if logger_factory is not None
            else _build_local_logger(
                policy_path.parent,
                debug=namespace.debug,
                stream=None if namespace.json_output else command_output.stderr,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        _emit_error(
            str(error),
            json_output=namespace.json_output,
            output=command_output,
        )
        return runtime_exit
    logger.info("started root=%s", root)
    try:
        policy = _load_policy(policy_path, active_filesystem)
        annotations = _load_annotations(policy_path, policy, active_filesystem)
        maintained_paths = (
            frozenset(active_filesystem.entries)
            if isinstance(active_filesystem, TreeFilesystem)
            else _load_maintained_paths(root, run_command or _run_command)
        )
        ignore_rules = (
            _load_ignore_rules(root, root, active_filesystem)
            if maintained_paths is None
            else ()
        )
        result = check(
            root,
            policy,
            annotations,
            filesystem=active_filesystem,
            logger=logger,
            ignore_rules=ignore_rules,
            maintained_paths=maintained_paths,
        )
    except (OSError, RuntimeError, ValueError) as error:
        logger.error("failed error=%s", error)
        _emit_error(
            str(error),
            json_output=namespace.json_output,
            output=command_output,
        )
        return runtime_exit

    logger.info("completed violations=%d", len(result.findings))
    _emit_result(result, json_output=namespace.json_output, output=command_output)
    return 1 if result.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
