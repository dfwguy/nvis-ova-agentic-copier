---
name: documentation-writer
description: Edit an approved documentation or agent-policy change without manufacturing behavior tests.
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

# Documentation Writer

Use this portable workflow skill for the documentation and agent-policy edit
phase. Read and follow [the role instructions](../../agents/documentation-writer.md)
before acting.

## Model selection

The live Architect delegates this phase and waits while the writer remains the
sole mutable-worktree owner. Use this skill's `metadata.model-tier`; the
Architect router resolves the delegation mapping.
