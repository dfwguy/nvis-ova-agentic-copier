---
name: judge-quality
description: Perform a read-only quality review and classify findings by severity.
metadata:
  model-tier: most-capable
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

# Quality Judge

Use this skill for the quality review stage. Read and follow [the role
instructions](../../agents/judge-quality.md) before acting. That file is
role-reference material. This directory is the shared skill entry point for
Claude Code, Codex, and Pi.

## Rebase review mode

Reconciler invokes this review for existing-MR repair. Use the captured original
feature intent and target changes. Review the target-to-final committed diff
at the final rebased SHA and tree. Do not use an empty worktree-to-HEAD diff.
Require no Architect, Red checkpoint, or Green stage for this mode.
Return all findings to Reconciler. It owns repairs and renewed proof.
The feature and serial rules below keep their existing scope.


## Model selection

Use this skill's `metadata.model-tier`. The live Router resolves its
delegation mapping. Each frontend resolves that tier through its configured
model mapping. Do not hard-code a provider-specific model name here.
