# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Validate and summarize strictly local upstream-finding manifests."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml.events import AliasEvent
from yaml.tokens import AliasToken, AnchorToken, TagToken

_MAX_BYTES = 64 * 1024
_SHA = re.compile(r"^[0-9a-f]{40}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_DRIVE = re.compile(r"^[A-Za-z]:")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|glpat-[^\s]+|eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+|(?:token|password|secret|api[_-]?key)\s*=|bearer\s+",
    re.IGNORECASE,
)


class ManifestError(RuntimeError):
    """A sanitized manifest-boundary error with no untrusted content."""


class _NoAliasLoader(yaml.SafeLoader):
    """Safe YAML loader which rejects aliases and duplicate mappings."""

    def compose_node(self, parent: object, index: object) -> yaml.Node:
        if self.check_event(AliasEvent):
            raise yaml.YAMLError("aliases are not allowed")
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[object, object]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise yaml.YAMLError("duplicate keys are not allowed")
        return super().construct_mapping(node, deep=deep)


class _BoundaryModel(BaseModel):
    """Strict immutable model for parsed manifest data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _normalized(value: str) -> str:
    """Return canonical whitespace-trimmed Unicode text."""
    return unicodedata.normalize("NFC", value).strip()


def _safe_text(value: str, field: str, *, maximum: int = 10_000) -> str:
    """Validate and normalize a non-secret text scalar."""
    normalized = _normalized(value)
    if (
        not normalized
        or len(normalized) > maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cf"} for character in normalized
        )
        or _SECRET.search(normalized)
    ):
        raise ValueError(f"{field} is invalid")
    return normalized


def _safe_host(value: str) -> str:
    """Accept a public bare DNS hostname."""
    host = _safe_text(value, "host", maximum=253).lower()
    if (
        host in {"local", "internal", "home.arpa"}
        or ".internal" in host
        or ".local" in host
    ):
        raise ValueError("host is invalid")
    numeric_component = r"(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)"
    if re.fullmatch(rf"{numeric_component}(?:\.{numeric_component}){{0,3}}", host):
        raise ValueError("host is invalid")
    labels = host.split(".")
    if (
        not _HOST.fullmatch(host)
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
        or host in {"localhost", "localhost.localdomain"}
    ):
        raise ValueError("host is invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not address.is_global:
        raise ValueError("host is invalid")
    return host


def _safe_path(value: str) -> str:
    """Accept a normalized relative repository path."""
    path = _safe_text(value, "path", maximum=512)
    segments = path.split("/")
    if (
        path.startswith(("/", "-"))
        or "\\" in path
        or _DRIVE.match(path)
        or any(segment in {"", ".", ".."} for segment in segments)
    ):
        raise ValueError("path is invalid")
    return path


def _safe_url_text(value: str, field: str) -> str:
    """Accept a bounded URL string without whitespace or malformed escapes."""
    url = _safe_text(value, field, maximum=2_000)
    if any(character.isspace() for character in url) or _INVALID_PERCENT_ESCAPE.search(
        url
    ):
        raise ValueError(f"{field} is invalid")
    return url


def _safe_source(value: str) -> str:
    """Accept only credential-free network Git origins."""
    source = _safe_url_text(value, "source")
    scp = re.fullmatch(r"git@([A-Za-z0-9.-]+):([^\s]+)", source)
    if scp:
        _safe_host(scp.group(1))
        if scp.group(2).startswith(("/", "-")) or ".." in scp.group(2).split("/"):
            raise ValueError("source is invalid")
        return source
    try:
        parsed = urlsplit(source)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError("source is invalid") from None
    if (
        parsed.scheme not in {"https", "ssh"}
        or host is None
        or parsed.username not in {None, "git"}
        or parsed.password is not None
        or not parsed.path.strip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source is invalid")
    _safe_host(host)
    return source


def _safe_link(value: str) -> str:
    """Accept a public, canonical HTTPS evidence URL."""
    url = _safe_url_text(value, "url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        raise ValueError("url is invalid") from None
    if (
        parsed.scheme != "https"
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("url is invalid")
    _safe_host(host)
    return url


class _GeneratedProject(_BoundaryModel):
    host: str
    project_id: int = Field(gt=0)
    path_with_namespace: str

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Validate project host."""
        return _safe_host(value)

    @field_validator("path_with_namespace")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Validate namespace path."""
        return _safe_path(value)


class _Copier(_BoundaryModel):
    source: str
    revision: str

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        """Validate Copier origin."""
        return _safe_source(value)

    @field_validator("revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        """Require an exact lowercase commit SHA."""
        if _SHA.fullmatch(value) is None:
            raise ValueError("revision is invalid")
        return value


class _EvidenceLink(_BoundaryModel):
    url: str
    visibility: Literal["public"]

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """Validate public evidence URL."""
        return _safe_link(value)


class _Finding(_BoundaryModel):
    copied_paths: list[str] = Field(min_length=1, max_length=100)
    reproduction: list[str] = Field(min_length=1, max_length=20)
    expected_behavior: str
    observed_behavior: str
    validation_evidence: str
    priority: Literal["low", "medium", "high", "urgent"]
    severity: Literal["low", "medium", "high", "critical"]
    scope: Literal["generated-project", "template", "both"]
    evidence_links: list[_EvidenceLink] = Field(max_length=20)

    @field_validator("copied_paths")
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        """Validate each copied path."""
        return [_safe_path(value) for value in values]

    @field_validator("reproduction")
    @classmethod
    def validate_reproduction(cls, values: list[str]) -> list[str]:
        """Validate ordered reproduction steps."""
        return [_safe_text(value, "reproduction", maximum=2_000) for value in values]

    @field_validator("expected_behavior", "observed_behavior", "validation_evidence")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """Validate finding prose."""
        return _safe_text(value, "text")


class _Manifest(_BoundaryModel):
    schema_version: Literal[1]
    generated_project: _GeneratedProject
    copier: _Copier
    finding: _Finding


@dataclass(frozen=True)
class UpstreamFindingPreview:
    """Safe immutable preview that intentionally omits manifest content."""

    schema_version: int
    command: str
    outcome: str
    project_fingerprint: str
    revision_fingerprint: str
    semantic_finding_fingerprint: str
    copied_path_count: int
    evidence_link_count: int

    def as_json(self) -> str:
        """Serialize the safe preview as stable compact JSON."""
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "command": self.command,
                "outcome": self.outcome,
                "project_fingerprint": self.project_fingerprint,
                "revision_fingerprint": self.revision_fingerprint,
                "semantic_finding_fingerprint": self.semantic_finding_fingerprint,
                "copied_path_count": self.copied_path_count,
                "evidence_link_count": self.evidence_link_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def _fingerprint(value: object) -> str:
    """Hash canonical JSON without serialization ambiguity."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _error_location(error: ValidationError) -> str:
    """Return the first safe Pydantic location without rejected data."""
    location = error.errors()[0]["loc"]
    allowed = {
        "schema_version",
        "generated_project",
        "copier",
        "finding",
        "host",
        "project_id",
        "path_with_namespace",
        "source",
        "revision",
        "copied_paths",
        "reproduction",
        "expected_behavior",
        "observed_behavior",
        "validation_evidence",
        "priority",
        "severity",
        "scope",
        "evidence_links",
        "url",
        "visibility",
    }
    return ".".join(
        str(part) if isinstance(part, int) or part in allowed else "<unknown>"
        for part in location
    )


def _parse(content: bytes) -> _Manifest:
    """Decode, parse, and validate one bounded YAML manifest."""
    if len(content) > _MAX_BYTES:
        raise ManifestError("yaml is invalid")
    try:
        text = content.decode("utf-8")
        if any(
            isinstance(token, (AliasToken, AnchorToken, TagToken))
            for token in yaml.scan(text)
        ):
            raise yaml.YAMLError("anchors, aliases, and tags are not allowed")
        loaded = yaml.load(text, Loader=_NoAliasLoader)
    except (UnicodeDecodeError, yaml.YAMLError, TypeError, ValueError, RecursionError):
        raise ManifestError("yaml is invalid") from None
    if not isinstance(loaded, dict):
        raise ManifestError("yaml is invalid")
    try:
        return _Manifest.model_validate(loaded)
    except ValidationError as error:
        raise ManifestError(f"{_error_location(error)} is invalid") from None


def preview_manifest(
    path: Path, *, read_bytes: Callable[[Path], bytes]
) -> UpstreamFindingPreview:
    """Read through the injected boundary and return a safe local summary."""
    try:
        content = read_bytes(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ManifestError("manifest file could not be read") from None
    if not isinstance(content, bytes):
        raise ManifestError("manifest file could not be read")
    manifest = _parse(content)
    project = manifest.generated_project
    copier = manifest.copier
    finding = manifest.finding
    project_fingerprint = _fingerprint(
        {
            "host": project.host,
            "project_id": project.project_id,
            "path": project.path_with_namespace,
        }
    )
    revision_fingerprint = _fingerprint(
        {
            "project": project_fingerprint,
            "source": copier.source,
            "revision": copier.revision,
        }
    )
    paths = tuple(sorted(set(finding.copied_paths)))
    semantic_finding_fingerprint = _fingerprint(
        {
            "revision": revision_fingerprint,
            "copied_paths": paths,
            "reproduction": tuple(finding.reproduction),
            "expected_behavior": finding.expected_behavior,
            "observed_behavior": finding.observed_behavior,
            "scope": finding.scope,
        }
    )
    return UpstreamFindingPreview(
        schema_version=1,
        command="upstream-finding.preview",
        outcome="valid",
        project_fingerprint=project_fingerprint,
        revision_fingerprint=revision_fingerprint,
        semantic_finding_fingerprint=semantic_finding_fingerprint,
        copied_path_count=len(paths),
        evidence_link_count=len(finding.evidence_links),
    )
