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

Use this portable workflow skill for the filesystem judge phase. Read and
follow [the role instructions](../../agents/filesystem-structure-judge.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Use this skill's `metadata.model-tier`; the live Architect router resolves its delegation mapping. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
