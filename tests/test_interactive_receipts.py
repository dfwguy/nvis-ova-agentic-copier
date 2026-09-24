# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the immutable interactive object store."""

from pathlib import Path

import pytest

from agent_work.interactive_receipts import AtomicObjectStore


def test_atomic_objects_are_immutable_and_session_inventory_is_scoped(
    tmp_path: Path,
) -> None:
    store = AtomicObjectStore(tmp_path / "private")
    older = "slot/older/tdd-green/1/accepted"
    current = "slot/current/tdd-green/1/accepted"
    store.write_once(older, b"older")
    store.write_once(current, b"current")

    with pytest.raises(FileExistsError):
        store.write_once(current, b"replacement")
    assert store.read(current) == b"current"
    assert store.list_prefix("slot/current") == (current,)


def test_atomic_store_rejects_symlinked_parent(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)

    with pytest.raises(OSError):
        AtomicObjectStore(alias / "private").write_once("session/first", b"x")
    assert not (destination / "private").exists()
