---
name: agent-policy-judge
description: Review agent instructions for coherent routing, portability, synchronization, and authority boundaries.
metadata:
  model-tier: fast
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

# Agent Policy Judge

Use this portable skill for agent-policy review. It is read-only. Before acting,
read and follow [the role instructions](../../agents/agent-policy-judge.md).
Those instructions govern the review.

## Rebase review mode

For existing-MR repair, use both captured intents and the target-to-final diff.
Bind the review to the final rebased SHA/tree. Return findings to Reconciler.
Do not require a feature manifest, Architect plan, or Green handoff for this
mode. Check the same policy and human gates. Feature and serial rules remain.


## Model selection

Use this skill's `metadata.model-tier`. Its tier is `fast`. The live Router
router resolves the delegation mapping. It resolves it for delegation.
