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

Use this skill after publication. It handles actionable Minor findings from
the selected workflow's prepublication review. It is separate from other
post-publication work. Read and follow [the role instructions](../../agents/findings-triage.md) before you act.
That file supports Claude Code. This directory is the shared entry point for
Codex, Pi, and Claude Code.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router resolves the
delegation mapping. Each frontend maps that tier. Do not hard-code a
provider-specific model name here.
