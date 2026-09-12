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

Use this portable workflow skill for the quality judge phase. Read and
follow [the role instructions](../../agents/judge-quality.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `most-capable` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
