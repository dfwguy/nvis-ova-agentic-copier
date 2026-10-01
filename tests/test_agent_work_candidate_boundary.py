# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Preserved canonical Git-object path and content read limits."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work.candidate_boundary import (
    GIT_READ_MAX_CHUNK_BYTES,
    GIT_READ_MAX_FILE_BYTES,
    GIT_READ_MAX_PATHS,
    GIT_READ_MAX_TOTAL_BYTES,
    CandidateBoundaryError,
    validate_git_read_paths,
    validate_git_read_size,
)


@pytest.mark.parametrize(
    "paths",
    (
        ("README.md",),
        (".agents/project.md", "src/app.py", "template/a b.py"),
        tuple(f"src/{index:03}.py" for index in range(GIT_READ_MAX_PATHS)),
    ),
)
def test_git_read_accepts_canonical_sorted_paths(paths: tuple[str, ...]) -> None:
    assert validate_git_read_paths(paths) is None


@pytest.mark.parametrize(
    "paths",
    (
        (),
        ("",),
        ("b", "a"),
        ("a", "a"),
        ("/absolute",),
        ("../outside",),
        ("src/../outside",),
        ("./src/app.py",),
        ("src//app.py",),
        ("src/",),
        ("src/./app.py",),
        ("src/app\n.py",),
        ("src/app\r.py",),
        tuple(f"src/{index:03}.py" for index in range(GIT_READ_MAX_PATHS + 1)),
    ),
)
def test_git_read_rejects_noncanonical_or_over_budget_paths(
    paths: tuple[str, ...],
) -> None:
    with pytest.raises(
        CandidateBoundaryError, match="candidate changed paths are invalid"
    ):
        validate_git_read_paths(paths)


@pytest.mark.parametrize(
    ("size", "total", "per_blob", "expected"),
    (
        (0, 0, True, 0),
        (GIT_READ_MAX_CHUNK_BYTES, 0, True, GIT_READ_MAX_CHUNK_BYTES),
        (GIT_READ_MAX_FILE_BYTES, 0, True, GIT_READ_MAX_FILE_BYTES),
        (0, GIT_READ_MAX_TOTAL_BYTES, True, GIT_READ_MAX_TOTAL_BYTES),
        (1, GIT_READ_MAX_TOTAL_BYTES - 1, True, GIT_READ_MAX_TOTAL_BYTES),
        (GIT_READ_MAX_FILE_BYTES + 1, 0, False, GIT_READ_MAX_FILE_BYTES + 1),
        (GIT_READ_MAX_TOTAL_BYTES, 0, False, GIT_READ_MAX_TOTAL_BYTES),
    ),
)
def test_git_read_accumulates_bytes_with_exact_limits(
    size: int, total: int, *, per_blob: bool, expected: int
) -> None:
    assert validate_git_read_size(size, total, per_blob=per_blob) == expected


@pytest.mark.parametrize(
    ("size", "total", "per_blob"),
    (
        (-1, 0, True),
        (-1, 0, False),
        (GIT_READ_MAX_FILE_BYTES + 1, 0, True),
        (1, GIT_READ_MAX_TOTAL_BYTES, True),
        (GIT_READ_MAX_TOTAL_BYTES + 1, 0, False),
        (GIT_READ_MAX_FILE_BYTES, GIT_READ_MAX_TOTAL_BYTES, False),
    ),
)
def test_git_read_rejects_negative_or_over_budget_content(
    size: int, total: int, *, per_blob: bool
) -> None:
    with pytest.raises(
        CandidateBoundaryError, match="candidate content is oversize or binary"
    ):
        validate_git_read_size(size, total, per_blob=per_blob)


_CANONICAL_PATHS = st.sets(
    st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=12
        ),
        min_size=1,
        max_size=4,
    ).map("/".join),
    min_size=2,
    max_size=16,
).map(lambda paths: tuple(sorted(paths)))


@given(paths=_CANONICAL_PATHS)
def test_generated_canonical_git_paths_are_accepted(paths: tuple[str, ...]) -> None:
    assert validate_git_read_paths(paths) is None


@pytest.mark.parametrize(
    "defect",
    (
        "duplicate",
        "order",
        "absolute",
        "parent",
        "dot",
        "separator",
        "newline",
        "carriage",
    ),
)
@given(paths=_CANONICAL_PATHS)
def test_generated_git_paths_reject_independent_invalid_perturbations(
    defect: str, paths: tuple[str, ...]
) -> None:
    if defect == "duplicate":
        invalid = tuple(sorted((*paths, paths[0])))
    elif defect == "order":
        invalid = tuple(reversed(paths))
    else:
        changed_path = {
            "absolute": "/" + paths[0],
            "parent": "../" + paths[0],
            "dot": "./" + paths[0],
            "separator": "src//" + paths[0],
            "newline": paths[0] + "\n",
            "carriage": paths[0] + "\r",
        }[defect]
        invalid = tuple(sorted((changed_path, *paths[1:])))
    with pytest.raises(
        CandidateBoundaryError, match="candidate changed paths are invalid"
    ):
        validate_git_read_paths(invalid)


@given(
    sizes=st.lists(
        st.integers(min_value=0, max_value=GIT_READ_MAX_FILE_BYTES), max_size=8
    )
)
def test_generated_git_blob_accounting_equals_cumulative_bytes(
    sizes: list[int],
) -> None:
    total = 0
    for index, size in enumerate(sizes):
        total = validate_git_read_size(size, total)
        assert total == sum(sizes[: index + 1])


@given(
    total=st.integers(min_value=0, max_value=GIT_READ_MAX_TOTAL_BYTES),
    size=st.integers(min_value=0, max_value=GIT_READ_MAX_FILE_BYTES),
)
def test_generated_git_aggregate_budget_accepts_exact_sum_or_rejects_overflow(
    total: int, size: int
) -> None:
    if total + size <= GIT_READ_MAX_TOTAL_BYTES:
        assert validate_git_read_size(size, total) == total + size
    else:
        with pytest.raises(
            CandidateBoundaryError, match="candidate content is oversize or binary"
        ):
            validate_git_read_size(size, total)


@given(
    size=st.integers(
        min_value=GIT_READ_MAX_FILE_BYTES + 1, max_value=GIT_READ_MAX_TOTAL_BYTES
    ),
    prior_bytes=st.integers(min_value=0, max_value=GIT_READ_MAX_TOTAL_BYTES),
)
def test_generated_large_git_reads_require_aggregate_mode(
    size: int, prior_bytes: int
) -> None:
    total = min(prior_bytes, GIT_READ_MAX_TOTAL_BYTES - size)
    with pytest.raises(
        CandidateBoundaryError, match="candidate content is oversize or binary"
    ):
        validate_git_read_size(size, total)
    assert validate_git_read_size(size, total, per_blob=False) == total + size


@given(
    total=st.integers(min_value=0, max_value=GIT_READ_MAX_TOTAL_BYTES),
    overflow=st.integers(min_value=1, max_value=GIT_READ_MAX_FILE_BYTES),
    per_blob=st.booleans(),
)
def test_generated_git_aggregate_overflow_is_rejected_in_both_modes(
    total: int, overflow: int, *, per_blob: bool
) -> None:
    size = GIT_READ_MAX_TOTAL_BYTES - total + overflow
    with pytest.raises(
        CandidateBoundaryError, match="candidate content is oversize or binary"
    ):
        validate_git_read_size(size, total, per_blob=per_blob)


@given(
    size=st.integers(min_value=-GIT_READ_MAX_TOTAL_BYTES, max_value=-1),
    per_blob=st.booleans(),
)
def test_generated_negative_git_read_sizes_are_rejected(
    size: int, *, per_blob: bool
) -> None:
    with pytest.raises(
        CandidateBoundaryError, match="candidate content is oversize or binary"
    ):
        validate_git_read_size(size, 0, per_blob=per_blob)
