---
name: tdd-red
description: Write failing behavior tests before implementation and stop at Red.
metadata:
  model-tier: balanced
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Test Writer

Use this portable skill for test writing. Read and follow
[the role instructions](../../agents/tdd-red.md) before acting. That file
defines the role. This skill works with Claude Code, Codex, and Pi.

Reconciliation reuses and mechanically re-proves an exact retained authentic
Red checkpoint as the Red preparation receipt; it never delegates a fresh Test
Writer or writes Red evidence retrospectively.

For an approved pre-checkpoint migration, this skill is N/A. Its receipt states
that no authentic Red exists. It does not create a retrospective checkpoint or
claim a historical failing test.

## Model selection

The live Architect runs this work with `$diataxis-writer` and conditional
`$agent-policy-writer`. It waits for each needed preparation receipt or N/A. It
accepts Red's exact evidence. For interactive work, Main creates the local Red
commit and ref. For serial work, the trusted executable creates the tree ref.
Green starts after that checkpoint. Use this skill's
`metadata.model-tier`. The live Architect router maps the tier.
