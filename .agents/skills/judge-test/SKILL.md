---
name: judge-test
description: Perform a read-only review of test quality, coverage, and test practices.
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

# Test Judge

Use this skill to review tests. Read and follow
[the role](../../agents/judge-test.md) before acting. It works with Claude Code,
Codex, and Pi.
For feature work, check the accepted Red checkpoint and Red-to-Green diff.
For MR repair, read the source intent, target changes, and final rebased diff.
Check the final SHA/tree and fresh test proof. Do not require old Red receipts
or create a checkpoint. Return the result to Reconciler.

## Model selection

Use this skill's `metadata.model-tier`. The live Router resolves its
delegation mapping. Each frontend resolves that tier through its configured
model mapping. Do not hard-code a provider-specific model name here.
