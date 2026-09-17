# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the serial-runtime pytest shard plugin."""

from __future__ import annotations

import importlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

pytest_plugins = ("pytester",)


def _partition_nodeids(
    nodeids: tuple[str, ...], shard_index: int | str, shard_count: int | str
) -> tuple[str, ...]:
    module = importlib.import_module("tests.helpers.serial_runtime_shard")
    return module.partition_nodeids(nodeids, shard_index, shard_count)


def test_partition_nodeids_is_deterministic_and_exhaustive() -> None:
    """Two partitions deterministically assign every collected node exactly once."""
    nodeids = tuple(
        f"tests/test_agent_work_serial_runtime.py::test_case_{index}"
        for index in range(17)
    )

    first_partition = _partition_nodeids(nodeids, 0, 2)
    second_partition = _partition_nodeids(nodeids, 1, 2)

    assert first_partition == _partition_nodeids(nodeids, 0, 2)
    assert second_partition == _partition_nodeids(nodeids, 1, 2)
    assert set(first_partition).isdisjoint(second_partition)
    assert set(first_partition) | set(second_partition) == set(nodeids)


@given(
    nodeids=st.lists(st.text(min_size=1), unique=True).map(tuple),
    shard_count=st.integers(min_value=2, max_value=8),
)
def test_partition_nodeids_property_is_exhaustive_and_disjoint(
    nodeids: tuple[str, ...], shard_count: int
) -> None:
    """Every valid partition set assigns each distinct node ID once."""
    partitions = tuple(
        _partition_nodeids(nodeids, shard_index, shard_count)
        for shard_index in range(shard_count)
    )

    assert set().union(*map(set, partitions)) == set(nodeids)
    assert sum(map(len, partitions)) == len(nodeids)


@pytest.mark.parametrize(
    ("shard_index", "shard_count"),
    ((-1, 2), (2, 2), (0, 0), (0, 1), ("0", 2), (0, "2"), (True, 2), (0, True)),
)
def test_partition_nodeids_rejects_invalid_shard_configuration(
    shard_index: int | str, shard_count: int | str
) -> None:
    """Partition selection accepts only a valid member of a multi-shard set."""
    with pytest.raises(ValueError):
        _ = _partition_nodeids(("test::one",), shard_index, shard_count)


def test_plugin_collects_complementary_partitions(pytester: pytest.Pytester) -> None:
    """Plugin options collect each serial-runtime test exactly once."""
    pytester.makepyfile(
        """
        def test_alpha(): pass
        def test_bravo(): pass
        def test_charlie(): pass
        def test_delta(): pass
        """
    )
    common_arguments = (
        "--collect-only",
        "-q",
        "-p",
        "tests.helpers.serial_runtime_shard",
        "--serial-runtime-shard-count=2",
    )
    first = pytester.runpytest(*common_arguments, "--serial-runtime-shard-index=0")
    second = pytester.runpytest(*common_arguments, "--serial-runtime-shard-index=1")
    assert first.ret == pytest.ExitCode.OK
    assert second.ret == pytest.ExitCode.OK
    first_partition = {
        line for line in first.outlines if line.startswith("test_") and "::" in line
    }
    second_partition = {
        line for line in second.outlines if line.startswith("test_") and "::" in line
    }
    assert first_partition.isdisjoint(second_partition)
    assert first_partition | second_partition == {
        "test_plugin_collects_complementary_partitions.py::test_alpha",
        "test_plugin_collects_complementary_partitions.py::test_bravo",
        "test_plugin_collects_complementary_partitions.py::test_charlie",
        "test_plugin_collects_complementary_partitions.py::test_delta",
    }


def test_plugin_rejects_an_incomplete_shard_configuration(
    pytester: pytest.Pytester,
) -> None:
    """Plugin requires an explicit index and count together."""
    pytester.makepyfile("def test_example(): pass")
    result = pytester.runpytest(
        "--collect-only",
        "-p",
        "tests.helpers.serial_runtime_shard",
        "--serial-runtime-shard-index=0",
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
