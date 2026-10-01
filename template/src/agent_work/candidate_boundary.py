# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Shared bounds and validators for controller-owned Git candidate reads."""

from pathlib import PurePosixPath


class CandidateBoundaryError(ValueError):
    """Raised when Git candidate paths or read sizes exceed safe bounds."""


GIT_READ_MAX_PATHS = 128
GIT_READ_MAX_FILE_BYTES = 2 * 1024 * 1024
GIT_READ_MAX_TOTAL_BYTES = 16 * 1024 * 1024
GIT_READ_MAX_CHUNK_BYTES = 128 * 1024


def validate_git_read_paths(paths: tuple[str, ...]) -> None:
    """Require canonical bounded changed paths before reading Git objects."""
    if (
        not paths
        or len(paths) > GIT_READ_MAX_PATHS
        or paths != tuple(sorted(set(paths)))
        or any(
            not path
            or str(PurePosixPath(path)) != path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\n" in path
            or "\r" in path
            for path in paths
        )
    ):
        raise CandidateBoundaryError("candidate changed paths are invalid")


def validate_git_read_size(size: int, total: int, *, per_blob: bool = True) -> int:
    """Enforce the shared per-blob and aggregate read budgets."""
    if (
        size < 0
        or (per_blob and size > GIT_READ_MAX_FILE_BYTES)
        or total + size > GIT_READ_MAX_TOTAL_BYTES
    ):
        raise CandidateBoundaryError("candidate content is oversize or binary")
    return total + size
