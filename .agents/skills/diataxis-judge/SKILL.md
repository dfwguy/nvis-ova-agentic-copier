---
name: diataxis-judge
description: Review documentation for Diátaxis fit, technical accuracy, and index coverage.
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

# Documentation Judge

Use this portable skill for the comparison-based documentation review stage.
Before acting, read and follow [the role instructions](../../agents/diataxis-judge.md).
That file is role-reference material. This directory is the shared skill entry
point for Claude Code, Codex, and Pi.

## Model selection

Use this skill's `metadata.model-tier`. The live Router resolves the
delegation mapping. Each frontend maps that tier with its own model mapping.
Do not hard-code a provider-specific model name in this skill.
