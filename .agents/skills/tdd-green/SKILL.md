---
name: tdd-green
description: Implement the minimum production change needed to make approved Red tests pass.
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

# Implementer

Use this portable workflow skill for the implementation stage. Before acting,
read and follow [the role instructions](../../agents/tdd-green.md). That file is
role-reference material. This directory is the shared skill entry point for
Claude Code, Codex, and Pi.

Existing-MR rebases use `$reconcile-merge-request` and Reconciler, not Green.

## Model selection

Use this skill's `metadata.model-tier`. The live Router resolves the
delegation mapping. Each frontend maps that tier through its configured model
mapping. Do not hard-code a provider-specific model name in this skill.
