---
name: findings-triage
description: File deduplicated GitLab Bug issues for actionable prepublication review findings.
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

# Findings Triage

Use this portable workflow skill after publication to process accumulated
actionable Minor findings from the selected workflow's prepublication review.
This role is independent of other post-publication work. Read and follow [the role
instructions](../../agents/findings-triage.md) before acting. That file is a
compatibility reference for Claude Code; this directory is the shared skill
entry point for Codex, Pi, and Claude Code.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
