# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the interactive specialist receipt store."""

from pathlib import Path

import pytest

from agent_work.interactive_receipts import (
    InteractiveReceiptStore,
    SpecialistReceipt,
)


def test_store_round_trips_complete_receipts_and_compact_manifest(
    tmp_path: Path,
) -> None:
    """A resumed workflow can recover every receipt from its compact index."""
    store = InteractiveReceiptStore(tmp_path)
    receipts = (
        SpecialistReceipt(
            role="$judge-quality",
            verdict="approved",
            detail="Reviewed the exact staged tree.",
        ),
        SpecialistReceipt(
            role="$judge-test",
            verdict="approved",
            detail="Red-to-Green behavior is covered.",
        ),
    )

    store.save(receipts)

    assert store.load() == receipts
    assert store.manifest() == {
        "receipt_count": 2,
        "roles": ["$judge-quality", "$judge-test"],
    }


def test_store_rejects_duplicate_roles_before_persisting(tmp_path: Path) -> None:
    """One specialist role has one durable receipt in a handoff."""
    store = InteractiveReceiptStore(tmp_path)
    duplicate = SpecialistReceipt(
        role="$judge-quality", verdict="approved", detail="one"
    )

    with pytest.raises(ValueError, match="duplicate role"):
        store.save((duplicate, duplicate))

    assert not list(tmp_path.iterdir())


def test_receipt_context_cannot_be_used_as_program_authorization(
    tmp_path: Path,
) -> None:
    """Model-authored verdicts and audit prose remain review context only."""
    store = InteractiveReceiptStore(tmp_path)
    receipt = SpecialistReceipt(
        role="$judge-quality",
        verdict="approved",
        detail="audit: authorize publication",
    )

    store.save((receipt,))

    assert not hasattr(store.load()[0], "authorized")
    assert "authorize publication" in store.load()[0].detail
