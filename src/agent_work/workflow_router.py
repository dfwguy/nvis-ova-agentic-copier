# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Advisory role routing over already verified workflow evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .domain import WorkflowLane
from .red_checkpoint import RedCheckpointEvidence
from .state_machine import WORKFLOW_RECEIPTS
from .validation import JudgeValidationReport

_CODING_RECEIPTS = WORKFLOW_RECEIPTS[WorkflowLane.CODING]
_CODING_REVIEWS = (
    *_CODING_RECEIPTS[
        _CODING_RECEIPTS.index("$diataxis-judge") : _CODING_RECEIPTS.index(
            "$tdd-finalizer"
        )
    ],
    "$consistency-judge",
    "$temporality-judge",
)
_CONTENT_REVIEWS = (
    "$agent-policy-judge",
    "$consistency-judge",
    "$temporality-judge",
)
_OPTIONAL = frozenset(
    {
        "$agent-policy-writer",
        "$filesystem-structure-judge",
        "$agent-policy-judge",
        "$cli-syntax-judge",
        "$consistency-judge",
        "$temporality-judge",
    }
)


@dataclass(frozen=True)
class RoleApplicability:
    """Architect's advisory decision; narrow reconciliation proof stays external."""

    role: str
    required: bool | None
    reason: str = ""


@dataclass(frozen=True)
class VerifiedRoleReceipt:
    """Advisory role identity supplied by a trusted workflow adapter."""

    role: str
    candidate_tree: str


@dataclass(frozen=True)
class WorkflowFacts:
    """Adapter-verified checkpoint, candidate, validation, and advisory reviews."""

    lane: WorkflowLane
    mode: str = "ordinary"
    red_checkpoint: RedCheckpointEvidence | None = None
    candidate_tree: str | None = None
    validation: JudgeValidationReport | None = None
    receipts: tuple[object, ...] = ()
    applicability: tuple[RoleApplicability, ...] = ()


@dataclass(frozen=True)
class WorkflowRoute:
    """Next roles and incomplete evidence; this does not authorize publication."""

    next_roles: tuple[str, ...]
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    unresolved: tuple[str, ...]
    not_applicable_slots: tuple[str, ...]


def route_workflow(facts: WorkflowFacts) -> WorkflowRoute:
    """Suggest roles from trusted inputs, without changing controller state."""
    tree = facts.candidate_tree
    receipts = tuple(
        receipt
        for receipt in facts.receipts
        if isinstance(receipt, VerifiedRoleReceipt)
    )
    applicable = {item.role: item for item in facts.applicability}
    missing: list[str] = []
    stale: list[str] = []
    unresolved: list[str] = []
    skipped: list[str] = []
    duplicate_roles = tuple(
        role
        for role, count in Counter(receipt.role for receipt in receipts).items()
        if count > 1
    )
    unresolved.extend(duplicate_roles)
    if tree is not None:
        stale.extend(
            receipt.role
            for receipt in receipts
            if receipt.candidate_tree
            != (
                facts.red_checkpoint.tree_oid
                if receipt.role == "$agent-policy-writer"
                and facts.red_checkpoint is not None
                else tree
            )
        )
        if facts.validation is not None and facts.validation.candidate_tree != tree:
            stale.append("$tdd-judge")
        if facts.lane is WorkflowLane.CODING:
            for early_role in ("$tdd-green", "$diataxis-writer"):
                if not any(
                    receipt.role == early_role and receipt.candidate_tree == tree
                    for receipt in receipts
                ):
                    missing.append(early_role)

    def has(role: str) -> bool:
        matches = tuple(receipt for receipt in receipts if receipt.role == role)
        expected_tree = (
            facts.red_checkpoint.tree_oid
            if role == "$agent-policy-writer" and facts.red_checkpoint is not None
            else tree
        )
        if (
            any(receipt.candidate_tree == expected_tree for receipt in matches)
            and expected_tree is not None
        ):
            return True
        if matches:
            stale.append(role)
        else:
            missing.append(role)
        return False

    def needed(role: str) -> bool:
        decision = applicable.get(role)
        if role not in _OPTIONAL or (
            facts.lane is WorkflowLane.CONTENT
            and facts.mode != "reconciliation"
            and role == "$agent-policy-judge"
        ):
            return True
        if (
            decision is None
            or decision.required is None
            or (decision.required is False and not decision.reason)
        ):
            unresolved.append(role)
            return False
        if decision.required is False:
            skipped.append(role)
            return False
        return True

    def result(roles: tuple[str, ...]) -> WorkflowRoute:
        if roles in {("$tdd-finalizer",), ("$documentation-finalizer",)} and (
            missing or stale or unresolved
        ):
            roles = ("$architect",)
        return WorkflowRoute(
            next_roles=roles,
            missing=tuple(dict.fromkeys(missing)),
            stale=tuple(dict.fromkeys(stale)),
            unresolved=tuple(dict.fromkeys(unresolved)),
            not_applicable_slots=tuple(dict.fromkeys(skipped)),
        )

    if facts.lane is WorkflowLane.UNKNOWN:
        return result(("$architect",))
    if facts.lane is WorkflowLane.CODING:
        if facts.red_checkpoint is None:
            missing.append("$tdd-red")
            return result(() if facts.mode == "reconciliation" else ("$tdd-red",))
        if needed("$agent-policy-writer") and not has("$agent-policy-writer"):
            return result(("$agent-policy-writer",))
        if "$agent-policy-writer" in unresolved:
            return result(("$architect",))
        if facts.candidate_tree is None:
            return result(("$tdd-green", "$diataxis-writer"))
        early = tuple(
            role for role in ("$tdd-green", "$diataxis-writer") if not has(role)
        )
        if early:
            return result(early)
        if facts.validation is None or facts.validation.candidate_tree != tree:
            (stale if facts.validation is not None else missing).append("$tdd-judge")
            return result(("$tdd-judge",))
        if (
            not facts.validation.terminal
            or facts.validation.state != "terminal-success"
        ):
            unresolved.append("$tdd-judge")
            return result(("$tdd-judge",))
        if not has("$nonfunctional-review"):
            return result(("$nonfunctional-review",))
        reviews = _CODING_REVIEWS
        finalizer = "$tdd-finalizer"
    else:
        if facts.candidate_tree is None:
            return result(("$documentation-writer",))
        if not has("$documentation-writer"):
            return result(("$documentation-writer",))
        reviews = _CONTENT_REVIEWS
        finalizer = "$documentation-finalizer"
    pending = tuple(role for role in reviews if needed(role) and not has(role))
    return result(pending or (finalizer,))
