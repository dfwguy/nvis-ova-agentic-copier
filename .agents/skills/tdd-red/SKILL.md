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

Use this portable workflow skill for the test writer phase. Read and follow
[the role instructions](../../agents/tdd-red.md) before acting. That file is
role-reference material. This directory is the shared skill entry point for
Claude Code, Codex, and Pi.

## Model selection

The live Architect delegates this phase concurrently with `$diataxis-writer`
and conditional `$agent-policy-writer`. It then waits for every applicable
phase-2 receipt/N/A before Green starts. Use this skill's `metadata.model-tier`.
The live Architect router resolves its delegation mapping.
