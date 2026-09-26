# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Reconcile confirmed slow test roots with one configured GitLab project."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .gitlab import reject_unsafe_issue_text

MAX_SLOW_ROOT_MANIFEST_BYTES = 262_144
_MAX_CASE_LENGTH = 512
_RESERVED_MARKER_NAMESPACE = "agent-slow-test-root:"
_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


class _Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Evidence(_Boundary):
    environment: str = Field(min_length=1, max_length=80)
    duration_ms: float = Field(gt=0, le=3_600_000)
    tree_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    junit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _Root(_Boundary):
    key: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=80)
    cases: tuple[str, ...] = Field(min_length=1, max_length=100)
    confirmed: bool
    evidence: tuple[_Evidence, ...] = Field(max_length=20)
    title: str = Field(min_length=1, max_length=255)
    problem: str = Field(min_length=1, max_length=5000)
    outcome: str = Field(min_length=1, max_length=5000)
    acceptance: tuple[str, ...] = Field(min_length=1, max_length=30)
    semantic_issue_iid: int | None = Field(default=None, gt=0)
    semantic_phrase: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def check_scope(self) -> _Root:
        if not _KEY.fullmatch(self.key) or self.kind not in {
            "shared-fixture",
            "ordinary-test",
        }:
            raise ValueError("root key or kind is invalid")
        if len(set(self.cases)) != len(self.cases) or any(
            not case.strip()
            or "::" not in case
            or len(case) > _MAX_CASE_LENGTH
            or any(unicodedata.category(character) == "Cc" for character in case)
            for case in self.cases
        ):
            raise ValueError("root cases are invalid")
        if (self.semantic_issue_iid is None) != (self.semantic_phrase is None):
            raise ValueError("semantic hint is incomplete")
        if self.confirmed and (
            len(self.evidence) < 2
            or not any(
                sum(item.environment == environment for item in self.evidence) >= 2
                for environment in {item.environment for item in self.evidence}
            )
        ):
            raise ValueError(
                "confirmed root requires two local repeats with provenance"
            )
        for value in (
            self.title,
            self.problem,
            self.outcome,
            *self.acceptance,
            *self.cases,
            self.semantic_phrase,
        ):
            if value is None:
                continue
            reject_unsafe_issue_text(value)
            if _RESERVED_MARKER_NAMESPACE in value.casefold():
                raise ValueError("reserved slow root marker in manifest text")
            if not value.strip():
                raise ValueError("root text is invalid")
        return self


class _Manifest(_Boundary):
    schema_version: Literal[1]
    roots: tuple[_Root, ...] = Field(min_length=1, max_length=100)


@dataclass(frozen=True)
class SlowRootItem:
    """One root's deduplicated disposition."""

    root_key: str
    action: str
    issue_iid: int | None


@dataclass(frozen=True)
class SlowRootReport:
    """All dispositions from one bounded inventory reconciliation."""

    applied: bool
    items: tuple[SlowRootItem, ...]


class _Issue(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    project_id: int = Field(gt=0)
    iid: int = Field(gt=0)
    state: Literal["opened", "closed"]
    title: str
    description: str
    labels: list[str]

    @field_validator("description", mode="before")
    @classmethod
    def normalize_nullable_description(cls, value: object) -> object:
        """Treat GitLab's null description as an empty issue body."""
        return "" if value is None else value


def _issue(value: object, project_id: int) -> _Issue:
    try:
        issue = _Issue.model_validate(value)
    except ValidationError as error:
        raise ValueError("issue inventory is invalid") from error
    if issue.project_id != project_id:
        raise ValueError("issue inventory project is invalid")
    return issue


def _inventory(
    list_issues: Callable[[], tuple[dict[str, object], ...]], project_id: int
) -> tuple[_Issue, ...]:
    issues = list_issues()
    if not isinstance(issues, tuple) or len(issues) > 10_000:
        raise ValueError("issue inventory is incomplete")
    checked = tuple(_issue(value, project_id) for value in issues)
    if len({issue.iid for issue in checked}) != len(checked):
        raise ValueError("issue inventory has duplicate IID")
    return checked


def _marker(root: _Root) -> str:
    return f"<!-- agent-slow-test-root:{root.key}:v1 -->"


def _match(root: _Root, issues: tuple[_Issue, ...]) -> _Issue | None:
    marker = _marker(root)
    matches = [issue for issue in issues if marker in issue.description]
    if len(matches) > 1:
        raise ValueError("ambiguous exact root marker")
    if root.semantic_issue_iid is not None:
        hinted = [issue for issue in issues if issue.iid == root.semantic_issue_iid]
        if (
            len(hinted) != 1
            or root.semantic_phrase is None
            or root.semantic_phrase not in hinted[0].description
        ):
            raise ValueError("semantic hint does not verify issue scope")
        if matches and matches[0].iid != hinted[0].iid:
            raise ValueError("ambiguous semantic root scope")
        return hinted[0]
    return matches[0] if matches else None


def _description(root: _Root) -> str:
    acceptance = "\n".join(f"- {item}" for item in root.acceptance)
    cases = "\n".join(f"- {case}" for case in root.cases)
    return (
        f"## Problem\n\n{root.problem}\n\n## Outcome\n\n{root.outcome}"
        f"\n\n## Acceptance criteria\n\n{acceptance}"
        f"\n\n## Cases\n\n{cases}\n\n{_marker(root)}"
    )


def reconcile_slow_roots(
    manifest_bytes: bytes,
    *,
    project_id: int,
    apply: bool,
    list_issues: Callable[[], tuple[dict[str, object], ...]],
    create_issue: Callable[..., dict[str, object]],
    fetch_issue: Callable[[int], dict[str, object]],
) -> SlowRootReport:
    """Preview or create at most one verified issue per confirmed root."""
    if type(project_id) is not int or project_id <= 0 or not isinstance(apply, bool):
        raise ValueError("slow root configuration is invalid")
    if (
        not isinstance(manifest_bytes, bytes)
        or len(manifest_bytes) > MAX_SLOW_ROOT_MANIFEST_BYTES
    ):
        raise ValueError("slow root manifest is invalid")
    try:
        manifest = _Manifest.model_validate_json(manifest_bytes)
    except (ValidationError, ValueError) as error:
        raise ValueError("slow root manifest or evidence is invalid") from error
    if len({root.key for root in manifest.roots}) != len(manifest.roots):
        raise ValueError("slow root manifest has duplicate keys")
    first = _inventory(list_issues, project_id)
    second = _inventory(list_issues, project_id) if apply else first
    matches: dict[str, _Issue | None] = {}
    claimed: set[int] = set()
    for root in manifest.roots:
        if not root.confirmed:
            continue
        prior = _match(root, first)
        current = _match(root, second)
        if prior is not None and (
            current is None or prior.iid != current.iid or prior.state != current.state
        ):
            raise ValueError("slow root inventory became stale")
        if current is not None:
            if current.iid in claimed:
                raise ValueError("ambiguous slow root issue scope")
            claimed.add(current.iid)
        matches[root.key] = current
    if apply and sum(current is None for current in matches.values()) > 1:
        raise ValueError(
            "apply accepts one unmatched confirmed root; submit a manifest subset and rerun"
        )
    items: list[SlowRootItem] = []
    for root in manifest.roots:
        if not root.confirmed:
            items.append(SlowRootItem(root.key, "unconfirmed", None))
            continue
        current = matches[root.key]
        if current is not None:
            items.append(
                SlowRootItem(
                    root.key,
                    "reuse" if current.state == "opened" else "covered",
                    current.iid,
                )
            )
            continue
        if not apply:
            items.append(SlowRootItem(root.key, "would-create", None))
            continue
        title, description, labels = (
            root.title,
            _description(root),
            ("agent-slow-test",),
        )
        created = _issue(
            create_issue(
                project_id=project_id,
                title=title,
                description=description,
                labels=labels,
            ),
            project_id,
        )
        iid = created.iid
        readback = _issue(fetch_issue(iid), project_id)
        if (
            readback.iid != iid
            or readback.title != title
            or readback.description != description
            or tuple(readback.labels) != labels
            or readback.state != "opened"
        ):
            raise ValueError(
                "slow root issue read-back mismatch; creation may have completed, rerun to inspect marker"
            )
        items.append(SlowRootItem(root.key, "created", iid))
    return SlowRootReport(applied=apply, items=tuple(items))
