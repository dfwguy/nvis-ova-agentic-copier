# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Safe, read-only comparison of two Copier template revisions."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

_ERROR_MESSAGE = "template comparison failed"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SCP_ORIGIN = re.compile(r"^git@(?P<host>[^:/@]+):(?P<path>[^/].*)$")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")
_SAFE_STAGES = frozenset(
    {
        "input",
        "answers",
        "temporary-repository",
        "init",
        "current-fetch",
        "resolve-current",
        "candidate-fetch",
        "resolve-candidate",
        "diff",
    }
)


class TemplateComparisonError(RuntimeError):
    """A comparison boundary rejected untrusted input or failed safely."""

    def __init__(self, *, stage: str = "input") -> None:
        """Create an error that never includes untrusted boundary data."""
        super().__init__(_ERROR_MESSAGE)
        self.stage = stage if stage in _SAFE_STAGES else "input"


@dataclass(frozen=True)
class CommandResult:
    """Binary output returned by the injected command boundary."""

    returncode: int
    stdout: bytes
    stderr: bytes


class BinaryCommandRunner(Protocol):
    """Run one binary argv command through the injected process boundary."""

    def __call__(
        self, argv: tuple[str, ...], *, timeout_seconds: float
    ) -> CommandResult:
        """Return the binary result for one command."""


@dataclass(frozen=True)
class TemplateComparison:
    """Deterministic changed paths between two resolved template commits."""

    current_commit: str
    candidate_commit: str
    paths: tuple[str, ...]
    source: str = ""


@contextmanager
def private_repository() -> Iterator[Path]:
    """Create one private ephemeral bare-repository location."""
    with tempfile.TemporaryDirectory(prefix="agent-work-template-preview-") as path:
        yield Path(path) / "template.git"


def run_git_command(
    argv: tuple[str, ...],
    *,
    timeout_seconds: float,
    git_executable: Path,
    run_process: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    environment: Mapping[str, str] = os.environ,
) -> CommandResult:
    """Run Git with a fixed binary argv and benign transport environment only."""
    allowed_environment = {
        key: environment[key]
        for key in ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSH_AUTH_SOCK")
        if key in environment
    }
    allowed_environment.update(
        {
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": "/bin/false",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "SSH_ASKPASS": "/bin/false",
        }
    )
    if not argv or argv[0] != "git":
        raise ValueError("invalid Git command")
    if not git_executable.is_absolute():
        raise ValueError("invalid Git executable")
    command = (str(git_executable), *argv[1:])
    result = run_process(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_seconds,
        check=False,
        shell=False,
        env=allowed_environment,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


class _CopierMetadata(BaseModel):
    """Strict metadata consumed from a Copier answers file."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    source_path: str
    commit: str


def _has_unsafe_text(value: str) -> bool:
    """Return whether text contains whitespace or control characters."""
    return not value or any(
        character.isspace() or ord(character) < 32 for character in value
    )


def _valid_origin(origin: str) -> bool:
    """Accept only credential-free HTTPS and Git SSH repository origins."""
    if _has_unsafe_text(origin) or any(character in origin for character in "?#"):
        return False
    if match := _SCP_ORIGIN.fullmatch(origin):
        return bool(
            match["host"]
            and not match["host"].startswith("-")
            and match["path"]
            and not match["path"].startswith("-")
        )
    try:
        parsed = urlparse(origin)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    valid_credentials = (
        parsed.username is None and parsed.password is None
        if parsed.scheme == "https"
        else parsed.username in {None, "git"} and parsed.password is None
    )
    return (
        parsed.scheme in {"https", "ssh"}
        and host is not None
        and not host.startswith("-")
        and valid_credentials
        and bool(parsed.path.strip("/"))
        and not parsed.path.startswith("//")
    )


def _valid_ref(revision: str) -> bool:
    """Accept a conservative Git fetch expression without option syntax."""
    return (
        bool(_REF.fullmatch(revision))
        and ".." not in revision
        and "//" not in revision
        and not revision.endswith(".")
        and "@{" not in revision
    )


def _load_metadata(contents: str) -> _CopierMetadata:
    """Parse the two comparison fields using safe YAML only."""
    try:
        loaded = yaml.safe_load(contents)
        if not isinstance(loaded, dict):
            raise TypeError
        metadata = _CopierMetadata(
            source_path=loaded.get("_src_path"), commit=loaded.get("_commit")
        )
    except (TypeError, ValueError, ValidationError, yaml.YAMLError):
        raise TemplateComparisonError from None
    if not _valid_origin(metadata.source_path) or not _valid_ref(metadata.commit):
        raise TemplateComparisonError
    return metadata


def _run(
    run_command: BinaryCommandRunner,
    argv: tuple[str, ...],
    timeout_seconds: float,
    stage: str,
) -> CommandResult:
    """Run one injected command and reject any nonzero result."""
    try:
        result = run_command(argv, timeout_seconds=timeout_seconds)
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        raise TemplateComparisonError(stage=stage) from None
    if result.returncode != 0:
        raise TemplateComparisonError(stage=stage)
    return result


def _resolved_commit(result: CommandResult, *, stage: str) -> str:
    """Return an exact lowercase SHA from rev-parse's binary output."""
    try:
        resolved = result.stdout.decode("ascii")
    except UnicodeDecodeError:
        raise TemplateComparisonError(stage=stage) from None
    if not resolved.endswith("\n") or resolved.count("\n") != 1:
        raise TemplateComparisonError(stage=stage)
    commit = resolved[:-1]
    if _SHA.fullmatch(commit) is None:
        raise TemplateComparisonError(stage=stage)
    return commit


def _safe_path(path: str) -> bool:
    """Return whether a diff path is a safe relative repository path."""
    segments = path.split("/")
    return (
        bool(path)
        and not path.startswith(("/", "-"))
        and "\\" not in path
        and _DRIVE_PATH.match(path) is None
        and all(segment not in {"", ".", ".."} for segment in segments)
        and all(
            unicodedata.category(character) not in {"Cc", "Cf"} for character in path
        )
    )


def _changed_paths(result: CommandResult) -> tuple[str, ...]:
    """Decode, validate, sort, and deduplicate NUL-delimited Git paths."""
    if not result.stdout:
        return ()
    if not result.stdout.endswith(b"\x00"):
        raise TemplateComparisonError(stage="diff")
    try:
        paths = result.stdout[:-1].decode("utf-8").split("\x00")
    except UnicodeDecodeError:
        raise TemplateComparisonError(stage="diff") from None
    if not all(_safe_path(path) for path in paths):
        raise TemplateComparisonError(stage="diff")
    return tuple(sorted(set(paths)))


def compare_template_revisions(
    answers_path: Path,
    candidate_revision: str,
    *,
    read_text: Callable[[Path], str],
    run_command: BinaryCommandRunner,
    private_repository: Callable[[], AbstractContextManager[Path]],
    timeout_seconds: float,
) -> TemplateComparison:
    """Compare a Copier template's recorded commit to a candidate revision."""
    if answers_path.name != ".copier-answers.yml" or not _valid_ref(candidate_revision):
        raise TemplateComparisonError(stage="input")
    try:
        try:
            metadata = _load_metadata(read_text(answers_path))
        except (
            OSError,
            RuntimeError,
            TimeoutError,
            UnicodeError,
            ValueError,
            subprocess.SubprocessError,
            TemplateComparisonError,
        ):
            raise TemplateComparisonError(stage="answers") from None
        with private_repository() as repository:
            location = str(repository)
            _run(
                run_command,
                ("git", "init", "--bare", location),
                timeout_seconds,
                "init",
            )
            _run(
                run_command,
                (
                    "git",
                    "-C",
                    location,
                    "fetch",
                    "--no-tags",
                    metadata.source_path,
                    f"+{metadata.commit}:refs/agent-work/current",
                ),
                timeout_seconds,
                "current-fetch",
            )
            current = _resolved_commit(
                _run(
                    run_command,
                    (
                        "git",
                        "-C",
                        location,
                        "rev-parse",
                        "--verify",
                        "refs/agent-work/current^{commit}",
                    ),
                    timeout_seconds,
                    "resolve-current",
                ),
                stage="resolve-current",
            )
            _run(
                run_command,
                (
                    "git",
                    "-C",
                    location,
                    "fetch",
                    "--no-tags",
                    metadata.source_path,
                    f"+{candidate_revision}:refs/agent-work/candidate",
                ),
                timeout_seconds,
                "candidate-fetch",
            )
            candidate = _resolved_commit(
                _run(
                    run_command,
                    (
                        "git",
                        "-C",
                        location,
                        "rev-parse",
                        "--verify",
                        "refs/agent-work/candidate^{commit}",
                    ),
                    timeout_seconds,
                    "resolve-candidate",
                ),
                stage="resolve-candidate",
            )
            changed = _run(
                run_command,
                (
                    "git",
                    "-C",
                    location,
                    "diff",
                    "--name-only",
                    "-z",
                    current,
                    candidate,
                    "--",
                ),
                timeout_seconds,
                "diff",
            )
            paths = _changed_paths(changed)
    except TemplateComparisonError:
        raise
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        TypeError,
        ValueError,
        subprocess.SubprocessError,
    ):
        raise TemplateComparisonError(stage="temporary-repository") from None
    return TemplateComparison(current, candidate, paths, metadata.source_path)
