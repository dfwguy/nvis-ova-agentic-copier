# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for local upstream-finding manifest previews."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work.upstream_finding import ManifestError, preview_manifest


def _manifest(**overrides: str) -> bytes:
    """Return one valid manifest, with selected YAML fragments replaced."""
    fields = {
        "project_id": "42",
        "paths": "  - src/widget.py\\n  - tests/test_widget.py",
        "reproduction": "  - Create a widget\\n  - Run the generated command",
        "priority": "high",
        "severity": "medium",
        "scope": "both",
        "links": "  - url: https://example.com/evidence\\n    visibility: public",
    } | overrides
    sections = [
        "schema_version: 1",
        "generated_project:",
        "  host: gitlab.example.com",
        f"  project_id: {fields['project_id']}",
        "  path_with_namespace: group/widget",
        "copier:",
        "  source: https://github.com/example/template.git",
        "  revision: 0123456789abcdef0123456789abcdef01234567",
        "finding:",
        "  copied_paths:",
        fields["paths"],
        "  reproduction:",
        fields["reproduction"],
        "  expected_behavior: Generated widget accepts its documented input.",
        "  observed_behavior: Generated widget rejects that input.",
        "  validation_evidence: Reproduced with the documented command.",
        f"  priority: {fields['priority']}",
        f"  severity: {fields['severity']}",
        f"  scope: {fields['scope']}",
        "  evidence_links:",
        fields["links"],
    ]
    return ("\\n".join(sections) + "\\n").replace("\\n", "\n").encode()


def test_preview_preserves_literal_backslash_n_in_plain_scalar() -> None:
    """A literal escape sequence cannot inject a new YAML key."""
    content = _manifest().replace(
        b"Generated widget accepts its documented input.",
        b"literal \\nordinary-text",
    )
    preview = preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert preview.outcome == "valid"


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("example..com", id="empty-label"),
        pytest.param("-bad.example", id="leading-hyphen-label"),
        pytest.param("bad-.example", id="trailing-hyphen-label"),
        pytest.param("a" * 64 + ".example", id="oversize-label"),
    ],
)
def test_preview_rejects_malformed_host_labels(host: str) -> None:
    """Project host labels follow conservative DNS syntax."""
    with pytest.raises(ManifestError) as raised:
        preview_manifest(
            Path("finding.yaml"),
            read_bytes=_reader(
                _manifest().replace(b"gitlab.example.com", host.encode()), []
            ),
        )
    assert "generated_project.host" in str(raised.value)


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("127.1", id="short-numeric"),
        pytest.param("2130706433", id="integer-numeric"),
        pytest.param("0x7f000001", id="hex-numeric"),
        pytest.param("local", id="single-label"),
        pytest.param("service.internal", id="internal-suffix"),
        pytest.param("home.arpa", id="home-arpa"),
    ],
)
def test_preview_rejects_legacy_numeric_and_internal_hosts(host: str) -> None:
    """Hosts that may resolve locally never cross the manifest boundary."""
    with pytest.raises(ManifestError):
        preview_manifest(
            Path("finding.yaml"),
            read_bytes=_reader(
                _manifest().replace(b"gitlab.example.com", host.encode()), []
            ),
        )


@pytest.mark.parametrize(
    "host",
    [
        pytest.param("0x7f.0.0.1", id="dotted-hex"),
        pytest.param("0177.0.0.1", id="dotted-octal"),
    ],
)
def test_preview_rejects_dotted_legacy_numeric_hosts(host: str) -> None:
    """Mixed-base numeric address spellings cannot bypass the host boundary."""
    with pytest.raises(ManifestError):
        preview_manifest(
            Path("finding.yaml"),
            read_bytes=_reader(
                _manifest().replace(b"gitlab.example.com", host.encode()), []
            ),
        )


def test_preview_sanitizes_unknown_key_locations() -> None:
    """Unknown key text never reaches a diagnostic."""
    content = _manifest() + b"glpat-secret\x1b[31m: value\n"
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert str(raised.value) == "yaml is invalid"
    assert "glpat-secret" not in str(raised.value)


def test_preview_sanitizes_deeply_nested_yaml() -> None:
    """Deep malformed YAML never leaks parser recursion failures."""
    content = ("value:\n" * 500).encode()
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert str(raised.value) == "yaml is invalid"


def _reader(content: bytes, seen: list[Path]) -> Callable[[Path], bytes]:
    """Provide an explicit, recording filesystem boundary fake."""

    def read_bytes(path: Path) -> bytes:
        seen.append(path)
        return content

    return read_bytes


def test_preview_returns_a_safe_immutable_local_summary() -> None:
    seen: list[Path] = []
    preview = preview_manifest(
        Path("finding.yaml"), read_bytes=_reader(_manifest(), seen)
    )
    assert seen == [Path("finding.yaml")]
    assert (preview.schema_version, preview.command, preview.outcome) == (
        1,
        "upstream-finding.preview",
        "valid",
    )
    assert (preview.copied_path_count, preview.evidence_link_count) == (2, 1)
    for fingerprint in (
        preview.project_fingerprint,
        preview.revision_fingerprint,
        preview.semantic_finding_fingerprint,
    ):
        assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    with pytest.raises((AttributeError, TypeError)):
        field = "copied_path_count"
        setattr(preview, field, "template")


def test_preview_normalizes_paths_and_excludes_triage_metadata_from_semantics() -> None:
    first = preview_manifest(Path("one.yaml"), read_bytes=_reader(_manifest(), []))
    second = preview_manifest(
        Path("two.yaml"),
        read_bytes=_reader(
            _manifest(
                paths="  - tests/test_widget.py\\n  - src/widget.py\\n  - src/widget.py",
                priority="urgent",
                severity="critical",
                links="  - url: https://example.com/other\\n    visibility: public",
            ),
            [],
        ),
    )
    assert first.project_fingerprint == second.project_fingerprint
    assert first.revision_fingerprint == second.revision_fingerprint
    assert first.semantic_finding_fingerprint == second.semantic_finding_fingerprint
    assert second.copied_path_count == 2


@given(st.permutations(("src/widget.py", "tests/test_widget.py")))
def test_preview_path_order_and_duplicates_do_not_change_semantics(
    paths: tuple[str, str],
) -> None:
    """Canonical copied paths are sorted and deduplicated before fingerprinting."""
    baseline = preview_manifest(Path("one.yaml"), read_bytes=_reader(_manifest(), []))
    rendered_paths = "\n".join(f"  - {path}" for path in (*paths, paths[0]))
    preview = preview_manifest(
        Path("two.yaml"), read_bytes=_reader(_manifest(paths=rendered_paths), [])
    )
    assert preview.semantic_finding_fingerprint == baseline.semantic_finding_fingerprint


@pytest.mark.parametrize(
    ("content", "field"),
    [
        pytest.param(b"schema_version: '1'\n", "schema_version", id="quoted-schema"),
        pytest.param(
            _manifest(project_id="'42'"),
            "generated_project.project_id",
            id="quoted-project-id",
        ),
        pytest.param(_manifest(priority="HIGH"), "finding.priority", id="bad-priority"),
        pytest.param(
            _manifest(paths="  - ../outside.py"),
            "finding.copied_paths",
            id="path-traversal",
        ),
        pytest.param(
            _manifest(
                links="  - url: https://127.0.0.1/evidence\\n    visibility: public"
            ),
            "finding.evidence_links",
            id="private-link-address",
        ),
        pytest.param(
            _manifest(
                links="  - url: https://example.com/evidence?token=x\\n    visibility: public"
            ),
            "finding.evidence_links",
            id="credential-query-link",
        ),
        pytest.param(
            _manifest(
                links="  - url: https://user:pass@example.com/evidence\\n    visibility: public"
            ),
            "finding.evidence_links",
            id="userinfo-link",
        ),
        pytest.param(
            _manifest(
                links="  - url: https://example.com/evidence\\n    visibility: confidential"
            ),
            "finding.evidence_links",
            id="restricted-link",
        ),
        pytest.param(
            _manifest() + b"unknown_field: nope\\n", "<unknown>", id="unknown-key"
        ),
        pytest.param(
            _manifest().replace(b"high", b"glpat-very-secret-token"),
            "finding.priority",
            id="sensitive-priority",
        ),
        pytest.param(b"schema_version: 1\nvalue: &shared value\n", "yaml", id="anchor"),
        pytest.param(b"schema_version: !!int 1\n", "yaml", id="explicit-tag"),
        pytest.param(
            b"schema_version: 1\nvalue: &shared value\ncopy: *shared\n",
            "yaml",
            id="alias",
        ),
    ],
)
def test_preview_rejects_untrusted_input_with_sanitized_field_diagnostics(
    content: bytes, field: str
) -> None:
    secret = "glpat-very-secret-token"
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert field in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"[not, a, mapping]", id="non-mapping"),
        pytest.param(b"schema_version: 1\\nschema_version: 1\\n", id="duplicate-key"),
        pytest.param(
            b"schema_version: 1\\nvalue: !!python/object/apply:os.system ['echo nope']\\n",
            id="unsafe-python-tag",
        ),
        pytest.param(b"x" * (64 * 1024 + 1), id="oversize-document"),
    ],
)
def test_preview_rejects_unsafe_or_oversize_yaml_before_any_side_effect(
    content: bytes,
) -> None:
    with pytest.raises(ManifestError):
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))


@pytest.mark.parametrize(
    ("content", "field"),
    [
        pytest.param(
            _manifest().replace(b"gitlab.example.com", b"localhost"),
            "generated_project.host",
            id="loopback-host",
        ),
        pytest.param(
            _manifest().replace(b"gitlab.example.com", b"gitlab.example.com/path"),
            "generated_project.host",
            id="host-with-path",
        ),
        pytest.param(
            _manifest().replace(b"group/widget", b"group//widget"),
            "generated_project.path_with_namespace",
            id="empty-namespace-segment",
        ),
        pytest.param(
            _manifest().replace(b"group/widget", b"group/../widget"),
            "generated_project.path_with_namespace",
            id="namespace-traversal",
        ),
        pytest.param(
            _manifest().replace(b"project_id: 42", b"project_id: 0"),
            "generated_project.project_id",
            id="zero-project-id",
        ),
        pytest.param(
            _manifest().replace(
                b"https://github.com/example/template.git",
                b"ssh://user:pass@github.com/example/template.git",
            ),
            "copier.source",
            id="ssh-userinfo-source",
        ),
        pytest.param(
            _manifest().replace(
                b"https://github.com/example/template.git",
                b"https://user:pass@github.com/example/template.git",
            ),
            "copier.source",
            id="https-userinfo-source",
        ),
        pytest.param(
            _manifest().replace(
                b"https://github.com/example/template.git", b"file:///tmp/template"
            ),
            "copier.source",
            id="file-source",
        ),
        pytest.param(
            _manifest().replace(
                b"0123456789abcdef0123456789abcdef01234567",
                b"0123456789ABCDEF0123456789abcdef01234567",
            ),
            "copier.revision",
            id="uppercase-revision",
        ),
        pytest.param(
            _manifest().replace(
                b"0123456789abcdef0123456789abcdef01234567", b"0123456789abcdef"
            ),
            "copier.revision",
            id="short-revision",
        ),
        pytest.param(
            _manifest().replace(b"severity: medium", b"severity: severe"),
            "finding.severity",
            id="bad-severity",
        ),
        pytest.param(
            _manifest().replace(b"scope: both", b"scope: all"),
            "finding.scope",
            id="bad-scope",
        ),
        pytest.param(
            _manifest().replace(
                b"expected_behavior: Generated widget accepts its documented input.",
                b"expected_behavior: ''",
            ),
            "finding.expected_behavior",
            id="empty-expected-behavior",
        ),
        pytest.param(
            _manifest().replace(
                b"validation_evidence: Reproduced with the documented command.",
                b"validation_evidence: true",
            ),
            "finding.validation_evidence",
            id="nonstring-validation-evidence",
        ),
        pytest.param(
            _manifest().replace(b"  - Create a widget", b"  - true"),
            "finding.reproduction",
            id="nonstring-reproduction-step",
        ),
    ],
)
def test_preview_rejects_invalid_identity_copier_and_strict_scalar_boundaries(
    content: bytes, field: str
) -> None:
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert field in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/absolute.py", id="absolute"),
        pytest.param("../parent.py", id="parent-traversal"),
        pytest.param("src\\\\windows.py", id="backslash"),
        pytest.param("C:\\\\drive.py", id="drive-prefix"),
        pytest.param(".", id="current-directory"),
        pytest.param("", id="empty"),
        pytest.param("src/\\x00bad.py", id="nul-byte"),
    ],
)
def test_preview_rejects_every_unsafe_copied_path_shape(path: str) -> None:
    with pytest.raises(ManifestError) as raised:
        preview_manifest(
            Path("finding.yaml"), read_bytes=_reader(_manifest(paths=f"  - {path}"), [])
        )
    assert "finding.copied_paths" in str(raised.value)


@pytest.mark.parametrize(
    ("content", "field"),
    [
        pytest.param(_manifest(paths=""), "finding.copied_paths", id="empty-paths"),
        pytest.param(
            _manifest(paths="\\n".join(f"  - src/{index}.py" for index in range(101))),
            "finding.copied_paths",
            id="too-many-paths",
        ),
        pytest.param(
            _manifest(reproduction=""), "finding.reproduction", id="empty-steps"
        ),
        pytest.param(
            _manifest(
                reproduction="\\n".join(f"  - step {index}" for index in range(21))
            ),
            "finding.reproduction",
            id="too-many-steps",
        ),
        pytest.param(_manifest(links=""), "finding.evidence_links", id="empty-links"),
        pytest.param(
            _manifest(
                links="\\n".join(
                    f"  - url: https://example.com/{index}\\n    visibility: public"
                    for index in range(21)
                )
            ),
            "finding.evidence_links",
            id="too-many-links",
        ),
        pytest.param(
            _manifest().replace(
                b"Generated widget accepts its documented input.", b"x" * 10_001
            ),
            "finding.expected_behavior",
            id="oversize-expected-behavior",
        ),
        pytest.param(
            _manifest().replace(
                b"Reproduced with the documented command.", b"x" * 10_001
            ),
            "finding.validation_evidence",
            id="oversize-validation-evidence",
        ),
    ],
)
def test_preview_enforces_collection_and_text_bounds(
    content: bytes, field: str
) -> None:
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert field in str(raised.value)


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("-----BEGIN PRIVATE KEY-----", id="private-key"),
        pytest.param("glpat-unsafe-token", id="gitlab-token"),
        pytest.param("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature", id="jwt"),
        pytest.param("token=unsafe", id="token-assignment"),
        pytest.param("password=unsafe", id="password-assignment"),
        pytest.param("secret=unsafe", id="secret-assignment"),
        pytest.param("api_key=unsafe", id="api-key-assignment"),
        pytest.param("Bearer unsafe", id="bearer"),
    ],
)
def test_preview_rejects_credentials_anywhere_without_reflecting_them(
    secret: str,
) -> None:
    content = _manifest().replace(
        b"Generated widget accepts its documented input.", secret.encode()
    )
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert "finding.expected_behavior" in str(raised.value)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "link",
    [
        pytest.param("http://example.com/evidence", id="insecure-scheme"),
        pytest.param("https://localhost/evidence", id="loopback-host"),
        pytest.param("https://169.254.1.1/evidence", id="link-local-address"),
        pytest.param("https://192.0.2.1/evidence", id="documentation-address"),
        pytest.param("https://example.com/evidence#fragment", id="fragment"),
        pytest.param("https://example.com/evidence?query=x", id="query"),
        pytest.param("https://example.com:not-a-port/evidence", id="malformed-port"),
        pytest.param("https://example.com/evidence path", id="raw-whitespace"),
        pytest.param("https://example.com/evidence%zz", id="malformed-percent"),
    ],
)
def test_preview_rejects_nonpublic_or_noncanonical_evidence_links(link: str) -> None:
    content = _manifest(links=f"  - url: {link}\\n    visibility: public")
    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=_reader(content, []))
    assert "finding.evidence_links" in str(raised.value)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "https://example.com:not-a-port/template.git", id="malformed-port"
        ),
        pytest.param("https://example.com/template path.git", id="raw-whitespace"),
        pytest.param("https://example.com/template%zz.git", id="malformed-percent"),
    ],
)
def test_preview_rejects_noncanonical_copier_sources(source: str) -> None:
    """Copier sources reject malformed ports, whitespace, and bad escapes."""
    with pytest.raises(ManifestError) as raised:
        preview_manifest(
            Path("finding.yaml"),
            read_bytes=_reader(
                _manifest().replace(
                    b"https://github.com/example/template.git", source.encode()
                ),
                [],
            ),
        )
    assert "copier.source" in str(raised.value)


def test_preview_fingerprints_change_only_for_meaningful_identity_revision_or_semantics() -> (
    None
):
    baseline = preview_manifest(
        Path("baseline.yaml"), read_bytes=_reader(_manifest(), [])
    )
    project_changed = preview_manifest(
        Path("project.yaml"),
        read_bytes=_reader(
            _manifest().replace(b"project_id: 42", b"project_id: 43"), []
        ),
    )
    revision_changed = preview_manifest(
        Path("revision.yaml"),
        read_bytes=_reader(
            _manifest().replace(
                b"0123456789abcdef0123456789abcdef01234567",
                b"1123456789abcdef0123456789abcdef01234567",
            ),
            [],
        ),
    )
    source_changed = preview_manifest(
        Path("source.yaml"),
        read_bytes=_reader(
            _manifest().replace(
                b"https://github.com/example/template.git",
                b"https://github.com/example/other-template.git",
            ),
            [],
        ),
    )
    semantic_changed = preview_manifest(
        Path("semantic.yaml"),
        read_bytes=_reader(
            _manifest().replace(
                b"Generated widget rejects that input.",
                b"Generated widget exits unexpectedly.",
            ),
            [],
        ),
    )
    reordered_steps = preview_manifest(
        Path("steps.yaml"),
        read_bytes=_reader(
            _manifest(
                reproduction="  - Run the generated command\\n  - Create a widget"
            ),
            [],
        ),
    )
    assert baseline.project_fingerprint != project_changed.project_fingerprint
    assert baseline.revision_fingerprint != project_changed.revision_fingerprint
    assert baseline.revision_fingerprint != revision_changed.revision_fingerprint
    assert baseline.revision_fingerprint != source_changed.revision_fingerprint
    assert (
        baseline.semantic_finding_fingerprint
        != project_changed.semantic_finding_fingerprint
    )
    assert (
        baseline.semantic_finding_fingerprint
        != revision_changed.semantic_finding_fingerprint
    )
    assert (
        baseline.semantic_finding_fingerprint
        != source_changed.semantic_finding_fingerprint
    )
    assert (
        baseline.semantic_finding_fingerprint
        != semantic_changed.semantic_finding_fingerprint
    )
    assert (
        baseline.semantic_finding_fingerprint
        != reordered_steps.semantic_finding_fingerprint
    )


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"\\xff", id="invalid-utf8"),
        pytest.param(_manifest(), id="reader-error"),
    ],
)
def test_preview_wraps_reader_and_utf8_failures_in_sanitized_manifest_errors(
    content: bytes,
) -> None:
    def unreadable(_path: Path) -> bytes:
        if content == _manifest():
            raise PermissionError("secret path is unreadable")
        return content

    with pytest.raises(ManifestError) as raised:
        preview_manifest(Path("finding.yaml"), read_bytes=unreadable)
    assert "secret path" not in str(raised.value)
