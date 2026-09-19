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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from yaml.events import AliasEvent
from yaml.tokens import AliasToken, AnchorToken, TagToken

_MAX_BYTES = 64 * 1024
MAX_ISSUE_DESCRIPTION = 10_000
RESERVED_MARKER_PREFIX = "<!-- agent-work-upstream-finding:"
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
    matching_iid: int | None = None

    def as_json(self) -> str:
        """Serialize the safe preview as stable compact JSON."""
        payload: dict[str, int | str] = {
            "schema_version": self.schema_version,
            "command": self.command,
            "outcome": self.outcome,
            "project_fingerprint": self.project_fingerprint,
            "revision_fingerprint": self.revision_fingerprint,
            "semantic_finding_fingerprint": self.semantic_finding_fingerprint,
            "copied_path_count": self.copied_path_count,
            "evidence_link_count": self.evidence_link_count,
        }
        if self.matching_iid is not None:
            payload["matching_iid"] = self.matching_iid
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class PreparedIssueDraft:
    """Immutable structured evidence with a canonical derived issue payload."""

    preview: UpstreamFindingPreview
    expected_behavior: str
    observed_behavior: str
    validation_evidence: str
    priority: Literal["low", "medium", "high", "urgent"]
    severity: Literal["low", "medium", "high", "critical"]
    scope: Literal["generated-project", "template", "both"]
    copied_paths: tuple[str, ...]
    reproduction: tuple[str, ...]
    evidence_links: tuple[str, ...]
    marker: str = field(init=False)
    title: str = field(init=False)
    description: str = field(init=False)

    def __post_init__(self) -> None:
        """Validate structured evidence and derive the only postable payload."""
        if (
            self.priority not in {"low", "medium", "high", "urgent"}
            or self.severity not in {"low", "medium", "high", "critical"}
            or self.scope not in {"generated-project", "template", "both"}
        ):
            raise ValueError("issue draft is invalid")
        if (
            not self.copied_paths
            or len(self.copied_paths) > 100
            or not self.reproduction
            or len(self.reproduction) > 20
            or len(self.evidence_links) > 20
        ):
            raise ValueError("issue draft is invalid")
        object.__setattr__(
            self, "expected_behavior", _safe_text(self.expected_behavior, "text")
        )
        object.__setattr__(
            self, "observed_behavior", _safe_text(self.observed_behavior, "text")
        )
        object.__setattr__(
            self, "validation_evidence", _safe_text(self.validation_evidence, "text")
        )
        object.__setattr__(
            self,
            "copied_paths",
            tuple(sorted({_safe_path(item) for item in self.copied_paths})),
        )
        object.__setattr__(
            self,
            "reproduction",
            tuple(
                _safe_text(item, "reproduction", maximum=2_000)
                for item in self.reproduction
            ),
        )
        object.__setattr__(
            self,
            "evidence_links",
            tuple(_safe_link(item) for item in self.evidence_links),
        )
        semantic_fingerprint = _fingerprint(
            {
                "revision": self.preview.revision_fingerprint,
                "copied_paths": self.copied_paths,
                "reproduction": self.reproduction,
                "expected_behavior": self.expected_behavior,
                "observed_behavior": self.observed_behavior,
                "scope": self.scope,
            }
        )
        if (
            semantic_fingerprint != self.preview.semantic_finding_fingerprint
            or self.preview.copied_path_count != len(self.copied_paths)
            or self.preview.evidence_link_count != len(self.evidence_links)
        ):
            raise ValueError("issue draft is invalid")
        marker, title, description = _render_issue_payload(self)
        object.__setattr__(self, "marker", marker)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", description)
        validate_prepared_issue_draft(self)


def render_reserved_marker(preview: UpstreamFindingPreview) -> str:
    """Render the sole canonical marker used to identify an upstream finding."""
    fingerprints = (
        preview.project_fingerprint,
        preview.revision_fingerprint,
        preview.semantic_finding_fingerprint,
    )
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in fingerprints):
        raise ValueError("finding fingerprints are invalid")
    return (
        "<!-- agent-work-upstream-finding:v1 project="
        f"{fingerprints[0]} revision={fingerprints[1]} semantic={fingerprints[2]} -->"
    )


def render_issue_title(preview: UpstreamFindingPreview) -> str:
    """Render the one canonical title paired with a prepared finding."""
    return f"Upstream generated finding {preview.semantic_finding_fingerprint[:12]}"


def _render_issue_payload(draft: PreparedIssueDraft) -> tuple[str, str, str]:
    """Render the sole canonical title and body from validated structured state."""
    marker = render_reserved_marker(draft.preview)
    body = "\n".join(
        (
            "## Validated upstream finding",
            "",
            marker,
            "",
            "## Fingerprint provenance",
            f"- Project fingerprint: `{draft.preview.project_fingerprint}`",
            f"- Revision fingerprint: `{draft.preview.revision_fingerprint}`",
            f"- Semantic finding fingerprint: `{draft.preview.semantic_finding_fingerprint}`",
            "",
            "## Finding evidence",
            f"- Expected behavior: {draft.expected_behavior}",
            f"- Observed behavior: {draft.observed_behavior}",
            f"- Validation evidence: {draft.validation_evidence}",
            f"- Priority: {draft.priority}",
            f"- Severity: {draft.severity}",
            f"- Scope: {draft.scope}",
            "",
            "## Copied paths",
            *(f"- `{item}`" for item in draft.copied_paths),
            "",
            "## Reproduction",
            *(f"{index}. {item}" for index, item in enumerate(draft.reproduction, 1)),
            *(("", "## Public evidence links") if draft.evidence_links else ()),
            *(f"- {item}" for item in draft.evidence_links),
        )
    )
    return marker, render_issue_title(draft.preview), body


def validate_prepared_issue_draft(draft: PreparedIssueDraft) -> None:
    """Require the producer and direct callers to use one safe draft contract."""
    marker, expected_title, expected_description = _render_issue_payload(draft)
    if (
        draft.marker != marker
        or draft.title != expected_title
        or draft.description != expected_description
        or len(draft.description) > MAX_ISSUE_DESCRIPTION
        or draft.description.count(RESERVED_MARKER_PREFIX) != 1
        or draft.description.count(marker) != 1
        or _SECRET.search(draft.description)
        or any(
            unicodedata.category(character) in {"Cc", "Cf"} and character != "\n"
            for character in draft.description
        )
    ):
        raise ValueError("issue draft is invalid")


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
    return prepare_issue_draft(path, read_bytes=read_bytes).preview


def _preview_from_manifest(manifest: _Manifest) -> UpstreamFindingPreview:
    """Build the content-free preview from an already validated manifest."""
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


def prepare_issue_draft(
    path: Path, *, read_bytes: Callable[[Path], bytes]
) -> PreparedIssueDraft:
    """Read once and prepare one bounded, redacted upstream issue draft."""
    try:
        content = read_bytes(path)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ManifestError("manifest file could not be read") from None
    if not isinstance(content, bytes):
        raise ManifestError("manifest file could not be read")
    manifest = _parse(content)
    preview = _preview_from_manifest(manifest)
    finding = manifest.finding
    try:
        return PreparedIssueDraft(
            preview=preview,
            expected_behavior=finding.expected_behavior,
            observed_behavior=finding.observed_behavior,
            validation_evidence=finding.validation_evidence,
            priority=finding.priority,
            severity=finding.severity,
            scope=finding.scope,
            copied_paths=tuple(finding.copied_paths),
            reproduction=tuple(finding.reproduction),
            evidence_links=tuple(link.url for link in finding.evidence_links),
        )
    except ValueError:
        raise ManifestError("issue draft is invalid") from None
