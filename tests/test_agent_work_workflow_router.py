# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior of the pure route over already verified workflow facts."""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work.domain import WorkflowLane, WorkflowPhase
from agent_work.models import WorkflowReceiptRecord
from agent_work.red_checkpoint import RedCheckpointEvidence, checkpoint_ref
from agent_work.validation import JudgeValidationReport

TREE = "a" * 40
NEXT_TREE = "b" * 40
BASE = "c" * 40


@pytest.fixture
def router_api() -> ModuleType:
    """Import the implementation only when a behavior test executes."""
    return _load_router()


def _load_router() -> ModuleType:
    return importlib.import_module("agent_work.workflow_router")


def _red() -> RedCheckpointEvidence:
    return RedCheckpointEvidence(
        project_id=310967,
        issue_iid=306,
        source_branch="clanker/issue306-architect-role-boundary",
        base_sha=BASE,
        tree_oid="d" * 40,
        ref=checkpoint_ref(306, BASE),
        test_paths=("tests/test_agent_work_workflow_router.py",),
        argv=("uv", "run", "pytest", "tests/test_agent_work_workflow_router.py"),
        exit_code=1,
        expected_failure="workflow router is missing",
        output_sha256="e" * 64,
    )


def _validation(tree: str = TREE) -> JudgeValidationReport:
    return JudgeValidationReport(
        candidate_tree=tree,
        applied=True,
        action="ran",
        state="terminal-success",
        terminal=True,
        returncode=0,
        diagnostic=None,
    )


def test_coding_red_then_policy_then_concurrent_green_and_docs(
    router_api: ModuleType,
) -> None:
    facts = router_api.WorkflowFacts(lane=WorkflowLane.CODING)
    assert router_api.route_workflow(facts).next_roles == ("$tdd-red",)

    facts = router_api.WorkflowFacts(lane=WorkflowLane.CODING, red_checkpoint=_red())
    route = router_api.route_workflow(facts)
    assert "$agent-policy-writer" in route.unresolved
    assert route.next_roles == ("$architect",)

    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        applicability=(
            router_api.RoleApplicability(
                role="$agent-policy-writer", required=False, reason="no policy paths"
            ),
        ),
    )
    route = router_api.route_workflow(facts)
    assert set(route.next_roles) == {"$tdd-green", "$diataxis-writer"}
    assert "$agent-policy-writer" in route.not_applicable_slots


def test_coding_validation_and_specialist_receipts_gate_finalizer(
    router_api: ModuleType,
) -> None:
    early = (
        router_api.VerifiedRoleReceipt("$tdd-green", TREE),
        router_api.VerifiedRoleReceipt("$diataxis-writer", TREE),
    )
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        receipts=early,
        applicability=(
            router_api.RoleApplicability(
                role="$agent-policy-writer", required=False, reason="no policy paths"
            ),
        ),
    )
    assert router_api.route_workflow(facts).next_roles == ("$tdd-judge",)

    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=early,
        applicability=facts.applicability,
    )
    assert router_api.route_workflow(facts).next_roles == ("$nonfunctional-review",)

    required_reviews = {
        "$diataxis-judge",
        "$judge-quality",
        "$judge-test",
        "$judge-cicd",
        "$tdd-document",
        "$filesystem-structure-judge",
        "$agent-policy-judge",
        "$license-checker",
    }
    with_nonfunctional = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=(
            *early,
            router_api.VerifiedRoleReceipt("$nonfunctional-review", TREE),
        ),
        applicability=facts.applicability,
    )
    pending = router_api.route_workflow(with_nonfunctional)
    assert required_reviews - {
        "$filesystem-structure-judge",
        "$agent-policy-judge",
    } <= set(pending.next_roles)
    assert {"$filesystem-structure-judge", "$agent-policy-judge"} <= set(
        pending.unresolved
    )

    conditional = (
        router_api.RoleApplicability("$agent-policy-writer", False, "no policy paths"),
        router_api.RoleApplicability("$cli-syntax-judge", False, "no CLI changes"),
        router_api.RoleApplicability("$consistency-judge", False, "no repeated facts"),
        router_api.RoleApplicability(
            "$temporality-judge", False, "no time-bound guidance"
        ),
        router_api.RoleApplicability(
            "$filesystem-structure-judge", True, "Python layout"
        ),
        router_api.RoleApplicability("$agent-policy-judge", True, "policy changed"),
    )
    ready = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=(
            *with_nonfunctional.receipts,
            *(router_api.VerifiedRoleReceipt(role, TREE) for role in required_reviews),
        ),
        applicability=conditional,
    )
    assert router_api.route_workflow(ready).next_roles == ("$tdd-finalizer",)


def test_missing_and_stale_coding_evidence_cannot_reach_finalizer(
    router_api: ModuleType,
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=NEXT_TREE,
        validation=_validation(TREE),
        receipts=(router_api.VerifiedRoleReceipt("$judge-quality", TREE),),
    )
    route = router_api.route_workflow(facts)
    assert "$tdd-finalizer" not in route.next_roles
    assert "$judge-quality" in route.stale
    assert "$tdd-judge" in route.stale
    assert "$diataxis-writer" in route.missing


def test_unknown_applicability_is_unresolved_not_not_applicable(
    router_api: ModuleType,
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        applicability=(router_api.RoleApplicability("$agent-policy-writer", None),),
    )
    route = router_api.route_workflow(facts)
    assert "$agent-policy-writer" in route.unresolved
    assert "$agent-policy-writer" not in route.not_applicable_slots
    assert "$tdd-green" not in route.next_roles


def test_mandatory_role_cannot_be_marked_not_applicable(router_api: ModuleType) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        applicability=(
            router_api.RoleApplicability("$tdd-judge", False, "claimed unnecessary"),
        ),
    )
    route = router_api.route_workflow(facts)
    assert "$tdd-judge" not in route.not_applicable_slots
    assert "$tdd-finalizer" not in route.next_roles


def test_content_writes_and_reviews_without_red(router_api: ModuleType) -> None:
    facts = router_api.WorkflowFacts(lane=WorkflowLane.CONTENT)
    assert router_api.route_workflow(facts).next_roles == ("$documentation-writer",)

    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CONTENT,
        candidate_tree=TREE,
        receipts=(router_api.VerifiedRoleReceipt("$documentation-writer", TREE),),
    )
    route = router_api.route_workflow(facts)
    assert "$agent-policy-judge" in route.next_roles
    assert "$tdd-red" not in route.next_roles

    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CONTENT,
        candidate_tree=TREE,
        receipts=(
            router_api.VerifiedRoleReceipt("$documentation-writer", TREE),
            router_api.VerifiedRoleReceipt("$agent-policy-judge", TREE),
        ),
        applicability=(
            router_api.RoleApplicability(
                "$consistency-judge", False, "no repeated facts"
            ),
            router_api.RoleApplicability(
                "$temporality-judge", False, "timeless content"
            ),
        ),
    )
    assert router_api.route_workflow(facts).next_roles == ("$documentation-finalizer",)


def test_reconciliation_reuses_retained_red_without_new_red(
    router_api: ModuleType,
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        mode="reconciliation",
        red_checkpoint=_red(),
        applicability=(
            router_api.RoleApplicability(
                "$agent-policy-writer", False, "retained policy unchanged"
            ),
        ),
    )
    assert set(router_api.route_workflow(facts).next_roles) == {
        "$tdd-green",
        "$diataxis-writer",
    }

    missing = router_api.WorkflowFacts(lane=WorkflowLane.CODING, mode="reconciliation")
    route = router_api.route_workflow(missing)
    assert "$tdd-red" not in route.next_roles
    assert "$tdd-red" in route.missing


def test_content_reconciliation_requires_explicit_narrow_plan_decision(
    router_api: ModuleType,
) -> None:
    base_facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CONTENT,
        mode="reconciliation",
        candidate_tree=TREE,
        receipts=(router_api.VerifiedRoleReceipt("$documentation-writer", TREE),),
        applicability=(
            router_api.RoleApplicability(
                "$consistency-judge", False, "no copied facts"
            ),
            router_api.RoleApplicability("$temporality-judge", False, "no time claims"),
        ),
    )
    missing = router_api.route_workflow(base_facts)
    assert "$agent-policy-judge" in missing.unresolved
    assert missing.next_roles == ("$architect",)

    accepted = router_api.route_workflow(
        router_api.WorkflowFacts(
            lane=base_facts.lane,
            mode=base_facts.mode,
            candidate_tree=base_facts.candidate_tree,
            receipts=base_facts.receipts,
            applicability=(
                *base_facts.applicability,
                router_api.RoleApplicability(
                    "$agent-policy-judge", False, "Architect accepted narrow merge plan"
                ),
            ),
        )
    )
    assert accepted.next_roles == ("$documentation-finalizer",)
    assert "$agent-policy-judge" in accepted.not_applicable_slots


@pytest.mark.parametrize(
    ("state", "terminal", "returncode"),
    (("terminal-failure", True, 1), ("running", False, None)),
)
def test_matching_tree_unsuccessful_validation_never_advances_reviews(
    router_api: ModuleType,
    state: Literal["terminal-failure", "running"],
    *,
    terminal: bool,
    returncode: int | None,
) -> None:
    validation = JudgeValidationReport(
        candidate_tree=TREE,
        applied=True,
        action="ran",
        state=state,
        terminal=terminal,
        returncode=returncode,
        diagnostic=None,
    )
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=validation,
        receipts=(
            router_api.VerifiedRoleReceipt("$tdd-green", TREE),
            router_api.VerifiedRoleReceipt("$diataxis-writer", TREE),
        ),
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", False, "no policy"),
        ),
    )
    route = router_api.route_workflow(facts)
    assert route.next_roles == ("$tdd-judge",)
    assert "$tdd-finalizer" not in route.next_roles


@given(
    role=st.sampled_from(
        ("$tdd-green", "$diataxis-writer", "$nonfunctional-review", "$judge-quality")
    ),
    defect=st.sampled_from(("missing", "duplicate", "stale")),
)
def test_finalizer_never_follows_incomplete_or_conflicting_receipts(
    role: str, defect: str
) -> None:
    router_api = _load_router()
    roles = (
        "$tdd-green",
        "$diataxis-writer",
        "$nonfunctional-review",
        "$diataxis-judge",
        "$judge-quality",
        "$judge-test",
        "$judge-cicd",
        "$tdd-document",
        "$license-checker",
    )
    receipts = tuple(router_api.VerifiedRoleReceipt(item, TREE) for item in roles)
    if defect == "missing":
        receipts = tuple(item for item in receipts if item.role != role)
    else:
        receipts = (
            *receipts,
            router_api.VerifiedRoleReceipt(
                role, NEXT_TREE if defect == "stale" else TREE
            ),
        )
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=receipts,
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", False, "no policy"),
            router_api.RoleApplicability(
                "$filesystem-structure-judge", False, "no layout"
            ),
            router_api.RoleApplicability("$agent-policy-judge", False, "no policy"),
            router_api.RoleApplicability("$cli-syntax-judge", False, "no CLI"),
            router_api.RoleApplicability("$consistency-judge", False, "no copies"),
            router_api.RoleApplicability(
                "$temporality-judge", False, "no time guidance"
            ),
        ),
    )
    assert "$tdd-finalizer" not in router_api.route_workflow(facts).next_roles


def test_narrative_receipt_cannot_substitute_for_verified_evidence(
    router_api: ModuleType,
) -> None:
    narrative = WorkflowReceiptRecord(
        phase=WorkflowPhase.CODING_GREEN,
        verdict="pass",
        detail="$tdd-green completed for candidate tree " + TREE,
    )
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        receipts=(narrative,),
        applicability=(
            router_api.RoleApplicability(
                "$agent-policy-writer", False, "no policy paths"
            ),
        ),
    )
    route = router_api.route_workflow(facts)
    assert "$tdd-green" in route.missing
    assert "$tdd-judge" not in route.next_roles


@pytest.mark.parametrize("extra_tree", (TREE, NEXT_TREE))
def test_duplicate_or_stale_review_receipt_blocks_finalizer(
    router_api: ModuleType, extra_tree: str
) -> None:
    roles = (
        "$tdd-green",
        "$diataxis-writer",
        "$nonfunctional-review",
        "$diataxis-judge",
        "$judge-quality",
        "$judge-test",
        "$judge-cicd",
        "$tdd-document",
        "$license-checker",
    )
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=(
            *(router_api.VerifiedRoleReceipt(role, TREE) for role in roles),
            router_api.VerifiedRoleReceipt("$judge-quality", extra_tree),
        ),
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", False, "no policy"),
            router_api.RoleApplicability(
                "$filesystem-structure-judge", False, "no layout"
            ),
            router_api.RoleApplicability("$agent-policy-judge", False, "no policy"),
            router_api.RoleApplicability("$cli-syntax-judge", False, "no CLI"),
            router_api.RoleApplicability("$consistency-judge", False, "no copies"),
            router_api.RoleApplicability(
                "$temporality-judge", False, "no time guidance"
            ),
        ),
    )
    route = router_api.route_workflow(facts)
    assert "$tdd-finalizer" not in route.next_roles
    assert "$judge-quality" in route.stale or "$judge-quality" in route.unresolved


@pytest.mark.parametrize("mode", ("ordinary", "reconciliation"))
@pytest.mark.parametrize(
    ("lane", "expected_roles"),
    (
        (WorkflowLane.CODING, ("$tdd-green", "$diataxis-writer")),
        (WorkflowLane.CONTENT, ("$documentation-writer",)),
    ),
)
def test_missing_compact_tree_routes_to_ordered_writers(
    router_api: ModuleType,
    mode: str,
    lane: WorkflowLane,
    expected_roles: tuple[str, ...],
) -> None:
    facts = router_api.WorkflowFacts(
        lane=lane,
        mode=mode,
        candidate_tree=None,
        red_checkpoint=_red(),
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", False, "no policy"),
        ),
    )
    assert router_api.route_workflow(facts).next_roles == expected_roles


@pytest.mark.parametrize("mode", ("ordinary", "reconciliation"))
@pytest.mark.parametrize(
    ("policy_tree", "expected_roles", "expected_stale"),
    (
        ("d" * 40, ("$tdd-judge",), ()),
        (TREE, ("$agent-policy-writer",), ("$agent-policy-writer",)),
    ),
)
def test_compact_tree_policy_receipt_uses_retained_red_identity(
    router_api: ModuleType,
    mode: str,
    policy_tree: str,
    expected_roles: tuple[str, ...],
    expected_stale: tuple[str, ...],
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        mode=mode,
        candidate_tree=TREE,
        red_checkpoint=_red(),
        receipts=(
            router_api.VerifiedRoleReceipt("$agent-policy-writer", policy_tree),
            router_api.VerifiedRoleReceipt("$tdd-green", TREE),
            router_api.VerifiedRoleReceipt("$diataxis-writer", TREE),
        ),
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", True, "policy paths"),
        ),
    )
    route = router_api.route_workflow(facts)
    assert route.next_roles == expected_roles
    assert route.stale == expected_stale


@pytest.mark.parametrize("mode", ("ordinary", "reconciliation"))
def test_compact_tree_content_reviews_keep_order(
    router_api: ModuleType, mode: str
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CONTENT,
        mode=mode,
        candidate_tree=TREE,
        receipts=(router_api.VerifiedRoleReceipt("$documentation-writer", TREE),),
        applicability=(
            router_api.RoleApplicability("$agent-policy-judge", True, "policy paths"),
            router_api.RoleApplicability("$consistency-judge", True, "copied facts"),
            router_api.RoleApplicability("$temporality-judge", True, "time claims"),
        ),
    )
    route = router_api.route_workflow(facts)
    assert route.next_roles == (
        "$agent-policy-judge",
        "$consistency-judge",
        "$temporality-judge",
    )
    assert route.missing == route.next_roles


@pytest.mark.parametrize("mode", ("ordinary", "reconciliation"))
def test_compact_tree_coding_reviews_keep_order(
    router_api: ModuleType, mode: str
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CODING,
        mode=mode,
        red_checkpoint=_red(),
        candidate_tree=TREE,
        validation=_validation(),
        receipts=tuple(
            router_api.VerifiedRoleReceipt(role, TREE)
            for role in ("$tdd-green", "$diataxis-writer", "$nonfunctional-review")
        ),
        applicability=(
            router_api.RoleApplicability("$agent-policy-writer", False, "no policy"),
            *(
                router_api.RoleApplicability(role, True, "applicable changed paths")
                for role in (
                    "$filesystem-structure-judge",
                    "$agent-policy-judge",
                    "$cli-syntax-judge",
                    "$consistency-judge",
                    "$temporality-judge",
                )
            ),
        ),
    )
    route = router_api.route_workflow(facts)
    assert route.next_roles == (
        "$diataxis-judge",
        "$judge-quality",
        "$judge-test",
        "$judge-cicd",
        "$tdd-document",
        "$cli-syntax-judge",
        "$filesystem-structure-judge",
        "$agent-policy-judge",
        "$license-checker",
        "$consistency-judge",
        "$temporality-judge",
    )
    assert route.missing == route.next_roles


@pytest.mark.parametrize("mode", ("ordinary", "reconciliation"))
@pytest.mark.parametrize("receipt_tree", (TREE, NEXT_TREE))
def test_compact_tree_content_finalizer_requires_matching_reviews(
    router_api: ModuleType, mode: str, receipt_tree: str
) -> None:
    facts = router_api.WorkflowFacts(
        lane=WorkflowLane.CONTENT,
        mode=mode,
        candidate_tree=TREE,
        receipts=(
            router_api.VerifiedRoleReceipt("$documentation-writer", TREE),
            router_api.VerifiedRoleReceipt("$agent-policy-judge", receipt_tree),
        ),
        applicability=(
            router_api.RoleApplicability("$agent-policy-judge", True, "policy paths"),
            router_api.RoleApplicability(
                "$consistency-judge", False, "no copied facts"
            ),
            router_api.RoleApplicability("$temporality-judge", False, "no time claims"),
        ),
    )
    route = router_api.route_workflow(facts)
    if receipt_tree == TREE:
        assert route.next_roles == ("$documentation-finalizer",)
        assert route.stale == ()
    else:
        assert route.next_roles == ("$agent-policy-judge",)
        assert route.stale == ("$agent-policy-judge",)
