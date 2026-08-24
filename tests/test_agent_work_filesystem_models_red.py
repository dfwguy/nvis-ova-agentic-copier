"""Red contracts for the Phase 2 filesystem work-item boundary."""

import hashlib
import json
import string
from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_work import models


def _envelope(**overrides: object) -> dict[str, object]:
    """Return one smallest valid producer-owned filesystem envelope."""
    return {
        "schema_version": 1,
        "source_id": "nightly-import",
        "repository": "copier",
        "title": "Add safe filesystem intake",
        "body": "Implement the bounded protocol.",
        "acceptance_criteria": ["Never execute producer data as a command."],
    } | overrides


@pytest.mark.parametrize(
    "field,value",
    (
        ("schema_version", 2),
        ("source_id", "../producer"),
        ("source_id", "producer/name"),
        ("source_id", "x" * 129),
        ("repository", "/outside"),
        ("repository", "x" * 129),
        ("title", ""),
        ("title", "x" * 513),
        ("body", "x" * 65_537),
        ("acceptance_criteria", []),
        ("acceptance_criteria", ["same", "same"]),
        ("acceptance_criteria", [""]),
        ("acceptance_criteria", ["x" * 2_049]),
        ("acceptance_criteria", [str(index) for index in range(65)]),
    ),
)
def test_filesystem_envelope_rejects_every_out_of_bounds_untrusted_field(
    field: str, value: object
) -> None:
    """Producer data has one deliberately small v1 shape and no coercions."""
    with pytest.raises(ValueError):
        models.FilesystemEnvelope.model_validate(_envelope(**{field: value}))


@pytest.mark.parametrize("extra", ({"unexpected": True}, {"mode": "inspect"}))
def test_filesystem_envelope_forbids_fields_outside_the_v1_producer_contract(
    extra: dict[str, object],
) -> None:
    """A producer cannot smuggle runner authority or future fields into v1."""
    with pytest.raises(ValueError):
        models.FilesystemEnvelope.model_validate(_envelope(**extra))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", "1"),
        ("schema_version", True),
        ("schema_version", 1.5),
        ("source_id", 1),
        ("repository", None),
        ("repository", 1),
        ("title", 1),
        ("body", None),
        ("body", 1),
        ("acceptance_criteria", "one criterion"),
        ("acceptance_criteria", ("one criterion",)),
        ("acceptance_criteria", ["one criterion", 1]),
    ),
)
def test_filesystem_envelope_rejects_wrong_json_types_without_coercion(
    field: str, value: object
) -> None:
    """The file boundary accepts only exact JSON-native v1 field types."""
    with pytest.raises(ValueError):
        models.FilesystemEnvelope.model_validate(_envelope(**{field: value}))


def test_filesystem_envelope_matches_json_schema_numeric_integer_semantics() -> None:
    """Draft 2020-12 treats the JSON number ``1.0`` as an integer equal to one."""
    envelope = models.FilesystemEnvelope.model_validate(_envelope(schema_version=1.0))

    assert envelope.schema_version == 1


@settings(max_examples=20, deadline=None)
@given(
    source_id=st.text(
        alphabet=string.ascii_letters + string.digits + "._-",
        min_size=1,
        max_size=128,
    ),
    repository=st.text(
        alphabet=string.ascii_letters + string.digits + "._-",
        min_size=1,
        max_size=128,
    ),
)
def test_filesystem_envelope_accepts_safe_bounded_aliases(
    source_id: str, repository: str
) -> None:
    """Alias validation is deterministic for the whole permitted character set."""
    envelope = models.FilesystemEnvelope.model_validate(
        _envelope(source_id=source_id, repository=repository)
    )

    assert envelope.source_id == source_id
    assert envelope.repository == repository


def test_work_item_from_file_uses_the_full_content_hash_as_immutable_identity() -> None:
    """Equivalent file work never relies on filename, mtime, or a truncated hash."""
    envelope = models.FilesystemEnvelope.model_validate(_envelope())
    raw = json.dumps(_envelope(), sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()

    item = models.WorkItem.from_file(
        envelope=envelope,
        content_sha256=digest,
        run_id="run-file-123",
        mode="publish",
    )

    assert item.work_item_id == f"filesystem:{digest}"
    assert item.source_type == "filesystem"
    assert item.source_id == "nightly-import"
    assert item.source_revision == digest
    assert item.repository == "copier"
    assert item.deduplication_key == digest
    assert item.branch == f"agent/file-{digest[:16]}"
    field = "branch"
    with pytest.raises(FrozenInstanceError):
        setattr(item, field, "agent/file-mutated")
