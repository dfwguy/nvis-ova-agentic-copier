---
name: filesystem-structure-judge
description: Verify changed Python and Bash files follow the documented filesystem layout.
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

# Filesystem Judge

Use this portable skill for the filesystem review stage. Before acting, read and
follow [the role instructions](../../agents/filesystem-structure-judge.md).
That file is role-reference material. This directory is the shared skill entry
point for Claude Code, Codex, and Pi.

## Rebase review mode

Reconciler invokes this review for existing-MR repair. Use the captured original
feature intent and target changes. Review the target-to-final committed diff
at the final rebased SHA and tree. Do not use an empty worktree-to-HEAD diff.
Require no Architect, Red checkpoint, or Green stage for this mode.
Return all findings to Reconciler. It owns repairs and renewed proof.
The feature and serial rules below keep their existing scope.


## Model selection

Use this skill's `metadata.model-tier`. The live Router resolves the
delegation mapping. Each frontend maps that tier with its own model mapping.
Do not hard-code a provider-specific model name in this skill.
