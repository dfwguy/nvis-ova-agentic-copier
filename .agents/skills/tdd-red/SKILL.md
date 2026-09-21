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

## Model selection

The live Architect delegates this work with `$diataxis-writer` and conditional
`$agent-policy-writer`. It waits for every preparation receipt or N/A before
Green starts. Use this skill's `metadata.model-tier`. The live Architect router
maps it to a model.
