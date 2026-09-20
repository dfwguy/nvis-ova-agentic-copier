---
name: architect
description: Design non-trivial changes before tests or implementation; use at the start of feature work or refactors.
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

# Architect

Use this portable workflow skill for the architect phase. Before acting, read
and follow [the role instructions](../../agents/architect.md). That file is
role-reference material. This directory is the shared skill entry point for
Claude Code, Codex, and Pi.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router resolves the
delegation mapping. Each frontend maps that tier through its configured model
mapping. Do not hard-code a provider-specific model name in this skill.
