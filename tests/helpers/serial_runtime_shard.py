# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Deterministically partition serial-runtime pytest collection by node ID."""

from __future__ import annotations

from hashlib import sha256

import pytest


def partition_nodeids(
    nodeids: tuple[str, ...], shard_index: object, shard_count: object
) -> tuple[str, ...]:
    """Return the stable, exhaustive node-ID partition for one shard."""
    if (
        isinstance(shard_index, bool)
        or isinstance(shard_count, bool)
        or not isinstance(shard_index, int)
        or not isinstance(shard_count, int)
        or shard_count < 2
        or not 0 <= shard_index < shard_count
    ):
        raise ValueError("shard index must identify one member of a multi-shard set")
    return tuple(
        nodeid
        for nodeid in sorted(nodeids)
        if int.from_bytes(sha256(nodeid.encode()).digest(), "big") % shard_count
        == shard_index
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register explicit serial-runtime partition selection options."""
    group = parser.getgroup("serial-runtime-shard")
    group.addoption("--serial-runtime-shard-index", type=int)
    group.addoption("--serial-runtime-shard-count", type=int)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Keep only this plugin's stable collection partition when configured."""
    shard_index = config.getoption("serial_runtime_shard_index")
    shard_count = config.getoption("serial_runtime_shard_count")
    if shard_index is None and shard_count is None:
        return
    if shard_index is None or shard_count is None:
        raise pytest.UsageError("both serial-runtime shard options are required")
    selected = set(
        partition_nodeids(
            tuple(item.nodeid for item in items), shard_index, shard_count
        )
    )
    deselected = [item for item in items if item.nodeid not in selected]
    config.hook.pytest_deselected(items=deselected)
    items[:] = [item for item in items if item.nodeid in selected]
