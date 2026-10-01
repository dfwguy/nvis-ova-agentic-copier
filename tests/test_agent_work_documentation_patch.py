# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Exact host-approved documentation changes at the descriptor boundary."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_work import serial
from agent_work.domain import WorkflowPhase

OLD = "# Guide\n\nOld explanation.\n"
NEW = "# Guide\n\nNew explanation.\n"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def filesystem() -> serial.DocumentationPatchFilesystem:
    """Shared real primitive fixture for direct application and broker tests."""

    def open_descriptor(
        path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        return os.open(path, flags, mode, dir_fd=dir_fd)

    def stat_entry(
        path: str | Path, *, dir_fd: int | None = None, follow_symlinks: bool = False
    ) -> os.stat_result:
        return os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def replace_entry(
        source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int
    ) -> None:
        os.replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def unlink_entry(path: str, *, dir_fd: int) -> None:
        os.unlink(path, dir_fd=dir_fd)

    return serial.DocumentationPatchFilesystem(
        open=open_descriptor,
        stat=stat_entry,
        fstat=os.fstat,
        read=os.read,
        write=os.write,
        fchmod=os.fchmod,
        replace=replace_entry,
        unlink=unlink_entry,
        close=os.close,
    )


def grant(
    root: Path,
    paths: tuple[str, ...] = ("docs/guide.md",),
    *,
    original: str = OLD,
    replacement: str = NEW,
) -> serial.DocumentationPatchGrant:
    """Shared complete host envelope; all paths belong to the test checkout."""
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(original)
        target.chmod(0o640)
    members = tuple(
        serial.DocumentationPatchFileGrant(
            path=path,
            base_sha256=_digest(original),
            replacement_sha256=_digest(replacement),
            base_size_bytes=len(original.encode()),
            replacement_size_bytes=len(replacement.encode()),
        )
        for path in paths
    )
    return serial.DocumentationPatchGrant(
        project_id=310967,
        issue_iid=12,
        checkout=root,
        source_branch="agent/issue-12",
        source_sha="a" * 40,
        phase=WorkflowPhase.WORKFLOW,
        ledger_revision=1,
        groups=(members,),
    )


def request(
    bound: serial.DocumentationPatchGrant, *, replacement: str = NEW, apply: bool = True
) -> serial.DocumentationPatchRequest:
    """Shared request contains proposed content, never host authority."""
    return serial.DocumentationPatchRequest(
        patches=tuple(
            serial.DocumentationPatch(
                path=member.path,
                base_sha256=member.base_sha256,
                replacement_text=replacement,
            )
            for group in bound.groups
            for member in group
        ),
        apply=apply,
    )


def _apply(
    bound: serial.DocumentationPatchGrant,
    proposed: serial.DocumentationPatchRequest,
    *,
    ports: serial.DocumentationPatchFilesystem | None = None,
) -> serial.DocumentationPatchResult:
    return serial.apply_documentation_patch(
        bound, proposed, identity_check=lambda: True, filesystem=ports or filesystem()
    )


def _assert_refused(
    result: serial.DocumentationPatchResult, reason: str = "patch_refused"
) -> None:
    assert (result.outcome, result.reason, result.changed, result.quarantined) == (
        "refused",
        reason,
        (),
        False,
    )


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize(
    "paths", [("README.md",), ("docs/guide.md", "template/docs/guide.md")]
)
def test_exact_host_grant_preserves_modes_and_reports_only_actual_effects(
    tmp_path: Path, paths: tuple[str, ...], *, apply: bool
) -> None:
    bound = grant(tmp_path, paths)
    before = tuple((tmp_path / member.path).stat() for member in bound.groups[0])
    result = _apply(bound, request(bound, apply=apply))
    assert result.outcome == ("applied" if apply else "preview")
    assert result.reason is None and result.quarantined is False
    assert tuple(member.path for member in result.changed) == (
        tuple(member.path for member in bound.groups[0]) if apply else ()
    )
    for index, member in enumerate(bound.groups[0]):
        target = tmp_path / member.path
        assert target.read_text() == (NEW if apply else OLD)
        assert target.stat().st_mode == before[index].st_mode
        if not apply:
            assert target.stat().st_ino == before[index].st_ino
        else:
            assert result.changed[index].sha256 == _digest(NEW)


@pytest.mark.parametrize(
    "field",
    [
        "base_sha256",
        "actual_base",
        "replacement_sha256",
        "base_size_bytes",
        "replacement_size_bytes",
    ],
)
def test_each_host_digest_and_measured_size_is_independently_required(
    tmp_path: Path, field: str
) -> None:
    bound = grant(tmp_path / "control")
    assert _apply(bound, request(bound)).outcome == "applied"
    bound = grant(tmp_path / "subject")
    member = bound.groups[0][0]
    actual = OLD
    if field == "actual_base":
        actual = OLD.replace("Old", "New")
        assert len(actual.encode()) == member.base_size_bytes
        assert _digest(actual) != member.base_sha256
        (bound.checkout / member.path).write_text(actual)
        proposed = request(bound)
        assert proposed.patches[0].base_sha256 == member.base_sha256
        _assert_refused(_apply(bound, proposed))
        assert (bound.checkout / member.path).read_text() == actual
        assert tuple((bound.checkout / "docs").iterdir()) == (
            bound.checkout / member.path,
        )
        return
    if field == "base_sha256":
        member = replace(member, base_sha256="b" * 64)
    elif field == "replacement_sha256":
        member = replace(member, replacement_sha256="b" * 64)
    elif field == "base_size_bytes":
        member = replace(member, base_size_bytes=member.base_size_bytes + 1)
    else:
        member = replace(
            member, replacement_size_bytes=member.replacement_size_bytes + 1
        )
    mutated = replace(bound, groups=((member,),))
    _assert_refused(_apply(mutated, request(bound)))
    assert (bound.checkout / "docs/guide.md").read_text() == OLD


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "docs/AGENTS.md",
        "docs/.agents/notes.md",
        "docs/guide.md.jinja",
        "template/README.md.jinja",
        "src/code.py",
        "tests/test_case.py",
        "sibling.md",
        "../outside.md",
        "/outside.md",
        "docs/../guide.md",
        "docs\\guide.md",
    ],
)
def test_approved_hashes_cannot_reclassify_unsupported_paths(
    tmp_path: Path, path: str
) -> None:
    control = grant(tmp_path / "control")
    assert _apply(control, request(control)).outcome == "applied"
    bound = grant(tmp_path / "subject")
    target: Path | None = None
    if not path.startswith("/") and "\\" not in path:
        target = bound.checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(OLD)
        target.chmod(0o640)
        observed = target.lstat()
        assert observed.st_nlink == 1 and observed.st_mode & 0o111 == 0
        assert observed.st_size == bound.groups[0][0].base_size_bytes
        assert _digest(target.read_text()) == bound.groups[0][0].base_sha256
    member = replace(bound.groups[0][0], path=path)
    mutated = replace(bound, groups=((member,),))
    # Shape-invalid paths may be rejected by the closed request boundary.
    try:
        proposed = request(mutated)
    except ValidationError:
        assert (bound.checkout / "docs/guide.md").read_text() == OLD
        if target is not None:
            assert target.read_text() == OLD
        return
    _assert_refused(_apply(mutated, proposed))
    assert (bound.checkout / "docs/guide.md").read_text() == OLD

    if target is not None:
        assert target.read_text() == OLD


@pytest.mark.parametrize("fault", ["missing", "duplicate", "overlapping", "unequal"])
def test_group_membership_and_mirror_text_are_complete_before_any_write(
    tmp_path: Path, fault: str
) -> None:
    bound = grant(tmp_path / "control", ("docs/guide.md", "template/docs/guide.md"))
    assert _apply(bound, request(bound)).outcome == "applied"
    bound = grant(tmp_path / "subject", ("docs/guide.md", "template/docs/guide.md"))
    proposed = request(bound)
    if fault == "missing":
        proposed = proposed.model_copy(update={"patches": proposed.patches[:1]})
    elif fault == "duplicate":
        proposed = proposed.model_copy(
            update={"patches": proposed.patches + proposed.patches[:1]}
        )
    elif fault == "overlapping":
        bound = replace(bound, groups=bound.groups + bound.groups)
    else:
        other = NEW + "Other prose.\n"
        second = replace(
            bound.groups[0][1],
            replacement_sha256=_digest(other),
            replacement_size_bytes=len(other.encode()),
        )
        bound = replace(bound, groups=((bound.groups[0][0], second),))
        proposed = proposed.model_copy(
            update={
                "patches": (
                    proposed.patches[0],
                    proposed.patches[1].model_copy(update={"replacement_text": other}),
                )
            }
        )
    _assert_refused(_apply(bound, proposed))
    assert all(
        (bound.checkout / path).read_text() == OLD
        for path in ("docs/guide.md", "template/docs/guide.md")
    )


@pytest.mark.parametrize(
    "fault", ["executable", "symlink", "parent_symlink", "hardlink", "directory"]
)
def test_nonregular_or_aliased_targets_never_change_outside_files(
    tmp_path: Path, fault: str
) -> None:
    control = grant(tmp_path / "control")
    assert _apply(control, request(control)).outcome == "applied"
    bound = grant(tmp_path / "subject")
    target = bound.checkout / "docs/guide.md"
    outside = tmp_path / "outside.md"
    outside.write_text(OLD)
    if fault == "executable":
        target.chmod(0o740)
    elif fault == "symlink":
        target.unlink()
        target.symlink_to(outside)
    elif fault == "parent_symlink":
        target.unlink()
        target.parent.rmdir()
        directory = tmp_path / "outside"
        directory.mkdir()
        (directory / "guide.md").write_text(OLD)
        target.parent.symlink_to(directory, target_is_directory=True)
    elif fault == "hardlink":
        target.unlink()
        os.link(outside, target)
    else:
        target.unlink()
        target.mkdir()
    _assert_refused(_apply(bound, request(bound)))
    assert outside.read_text() == OLD


@pytest.mark.parametrize(
    "fault",
    ["short_write", "write_error", "first_replace", "second_replace", "cleanup"],
)
def test_descriptor_failures_report_honest_partial_members_without_rollback(
    tmp_path: Path, fault: str
) -> None:
    paths = ("docs/guide.md", "template/docs/guide.md")
    control = grant(tmp_path / "control", paths)
    assert _apply(control, request(control)).outcome == "applied"
    bound = grant(tmp_path / "subject", paths)
    ports = filesystem()
    replacements = 0

    def write(descriptor: int, content: bytes) -> int:
        if fault == "short_write":
            return 0
        if fault == "write_error":
            raise OSError("PRIVATE_FILE_CONTENT")
        return os.write(descriptor, content)

    def replace_entry(
        source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int
    ) -> None:
        nonlocal replacements
        replacements += 1
        if (fault == "first_replace" and replacements == 1) or (
            fault == "second_replace" and replacements == 2
        ):
            raise OSError("PRIVATE_FILE_CONTENT")
        os.replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def close(descriptor: int) -> None:
        os.close(descriptor)
        if fault == "cleanup":
            raise OSError("PRIVATE_FILE_CONTENT")

    result = _apply(
        bound,
        request(bound),
        ports=replace(ports, write=write, replace=replace_entry, close=close),
    )
    assert (
        result.outcome in ("refused", "failed_partial") and result.reason == "io_failed"
    )
    assert "PRIVATE_FILE_CONTENT" not in result.model_dump_json()
    actual = tuple(path for path in paths if (bound.checkout / path).read_text() == NEW)
    assert tuple(member.path for member in result.changed) == actual
    if actual:
        assert result.outcome == "failed_partial" and result.quarantined is True
    if fault == "cleanup":
        assert result.quarantined is True
    if fault == "second_replace":
        assert actual == paths[:1]


@pytest.mark.parametrize("fault", ["identity", "inode", "mode", "link_count", "growth"])
def test_observable_pre_effect_drift_refuses_and_reads_a_finite_host_bound(
    tmp_path: Path, fault: str
) -> None:
    bound = grant(tmp_path / "control")
    assert _apply(bound, request(bound)).outcome == "applied"
    bound = grant(tmp_path / "subject")
    ports = filesystem()
    read_total = 0
    calls = 0
    target = bound.checkout / "docs/guide.md"

    def read(descriptor: int, count: int) -> bytes:
        nonlocal read_total, calls
        calls += 1
        data = os.read(descriptor, count)
        read_total += len(data)
        if calls == 1:
            if fault == "inode":
                swap = target.with_suffix(".swap")
                swap.write_text(OLD)
                swap.replace(target)
            elif fault == "mode":
                target.chmod(0o740)
            elif fault == "link_count":
                os.link(target, target.with_suffix(".alias"))
            elif fault == "growth":
                with target.open("ab") as handle:
                    handle.write(b"grown")
        return data

    checks = 0

    def identity_check() -> bool:
        nonlocal checks
        checks += 1
        return not (fault == "identity" and checks > 1)

    result = serial.apply_documentation_patch(
        bound,
        request(bound),
        identity_check=identity_check,
        filesystem=replace(ports, read=read),
    )
    _assert_refused(
        result, "identity_changed" if fault == "identity" else "patch_refused"
    )
    assert read_total <= bound.groups[0][0].base_size_bytes + 1
    assert target.read_text() != NEW


@pytest.mark.parametrize(
    "field", ["grant", "role", "root", "command", "create", "mode"]
)
def test_request_cannot_promote_worker_values_to_host_authority(field: str) -> None:
    valid = {
        "patches": [
            {
                "path": "docs/guide.md",
                "base_sha256": _digest(OLD),
                "replacement_text": NEW,
            }
        ],
        "apply": False,
    }
    assert (
        serial.DocumentationPatchRequest.model_validate_json(json.dumps(valid)).apply
        is False
    )
    with pytest.raises(ValidationError):
        serial.DocumentationPatchRequest.model_validate_json(
            json.dumps({**valid, field: "authority"})
        )


@settings(max_examples=8)
@given(
    prose=st.text(alphabet="abc XYZé", max_size=40),
    marker=st.sampled_from(("```", "~~~~")),
    body=st.sampled_from(("echo safe\n", "print('safe')\n")),
)
def test_generated_executable_regions_only_allow_prose_changes(
    tmp_path_factory: pytest.TempPathFactory, prose: str, marker: str, body: str
) -> None:
    root = tmp_path_factory.mktemp("regions")
    first = f"{marker}sh\n{body}{marker}\n"
    second = f"{marker}python\nprint('second')\n{marker}\n"
    original = f"Old prose\n{first}{second}    indented command\n"
    replacement = f"Updated prose: {prose}\n{first}{second}    indented command\n"
    bound = grant(root / "control", original=original, replacement=replacement)
    assert _apply(bound, request(bound, replacement=replacement)).outcome == "applied"
    for index, changed in enumerate(
        (
            replacement.replace(body, "different command\n"),
            replacement.replace(first + second, second + first),
            replacement.replace(first, ""),
            replacement.replace("    indented command\n", ""),
            replacement + "```sh\nextra\n```\n",
            replacement + "{{ unsafe }}",
            replacement + "```\nunclosed\n",
        )
    ):
        bound = grant(root / str(index), original=original, replacement=changed)
        _assert_refused(_apply(bound, request(bound, replacement=changed)))
        assert (bound.checkout / "docs/guide.md").read_text() == original


@settings(max_examples=8)
@given(
    name=st.text(alphabet="abcdef", min_size=1, max_size=12),
    text=st.text(alphabet="abc XYZé", min_size=1, max_size=30),
    delta=st.integers(min_value=1, max_value=4),
)
def test_generated_path_group_digest_and_size_bindings(
    tmp_path_factory: pytest.TempPathFactory, name: str, text: str, delta: int
) -> None:
    root = tmp_path_factory.mktemp("bindings")
    paths = (f"docs/{name}.md", f"template/docs/{name}.md")
    replacement = f"# Guide\n\nExplanation: {text}\n"
    bound = grant(root / "control", paths, replacement=replacement)
    assert _apply(bound, request(bound, replacement=replacement)).outcome == "applied"
    bound = grant(root / "subject", paths, replacement=replacement)
    proposed = request(bound, replacement=replacement)
    for field in ("base_size_bytes", "replacement_size_bytes"):
        member = bound.groups[0][0]
        member = (
            replace(member, base_size_bytes=member.base_size_bytes + delta)
            if field == "base_size_bytes"
            else replace(
                member, replacement_size_bytes=member.replacement_size_bytes + delta
            )
        )
        _assert_refused(
            _apply(replace(bound, groups=((member, bound.groups[0][1]),)), proposed)
        )
    outside = root / f"{name}.md"
    outside.write_text(OLD)
    outside.chmod(0o640)
    observed = outside.lstat()
    assert observed.st_nlink == 1 and observed.st_mode & 0o111 == 0
    assert observed.st_size == bound.groups[0][0].base_size_bytes
    assert _digest(outside.read_text()) == bound.groups[0][0].base_sha256
    invalid_member = replace(bound.groups[0][0], path=f"../{name}.md")
    invalid_grant = replace(bound, groups=((invalid_member, bound.groups[0][1]),))
    try:
        invalid_request = request(invalid_grant, replacement=replacement)
    except ValidationError:
        pass
    else:
        _assert_refused(_apply(invalid_grant, invalid_request))
    assert outside.read_text() == OLD
    _assert_refused(
        _apply(bound, proposed.model_copy(update={"patches": proposed.patches[:1]}))
    )
    changed = proposed.patches[0].model_copy(
        update={"replacement_text": replacement + "x"}
    )
    _assert_refused(
        _apply(
            bound,
            proposed.model_copy(update={"patches": (changed, proposed.patches[1])}),
        )
    )
    assert all((bound.checkout / path).read_text() == OLD for path in paths)


@pytest.mark.parametrize(
    "fault",
    [
        "uppercase_hash",
        "short_hash",
        "nonhex_hash",
        "integer_apply",
        "string_apply",
        "nested_extra",
    ],
)
def test_closed_request_rejects_each_independent_syntax_fault(fault: str) -> None:
    patch: dict[str, object] = {
        "path": "docs/guide.md",
        "base_sha256": _digest(OLD),
        "replacement_text": NEW,
    }
    payload: dict[str, object] = {"patches": [patch], "apply": False}
    assert (
        serial.DocumentationPatchRequest.model_validate_json(json.dumps(payload)).apply
        is False
    )
    if fault == "uppercase_hash":
        patch["base_sha256"] = _digest(OLD).upper()
    elif fault == "short_hash":
        patch["base_sha256"] = _digest(OLD)[:-1]
    elif fault == "nonhex_hash":
        patch["base_sha256"] = "z" * 64
    elif fault == "integer_apply":
        payload["apply"] = 1
    elif fault == "string_apply":
        payload["apply"] = "true"
    else:
        patch["grant"] = "host authority"
    with pytest.raises(ValidationError):
        serial.DocumentationPatchRequest.model_validate_json(json.dumps(payload))
