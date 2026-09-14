<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Contributing

## Branch protection

`main` is protected — no direct commits. All changes go through a merge request.

## Merge requests

- Open an MR from a feature branch (e.g. `clanker/<slug>` for AI-driven changes).
- At least one approval is required, and the MR author and committers cannot self-approve.
- A Maintainer merges.

## AI contributions

The shared agent workflow is defined by [`AGENTS.md`](AGENTS.md) and
the [`git-flow` skill](.agents/skills/git-flow/SKILL.md). AI must never run `glab mr
approve` — approval is reserved for human reviewers.
