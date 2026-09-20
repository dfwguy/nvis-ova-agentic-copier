---
name: pipeline-performance-judge
description: Own post-publication GitLab pipeline reconciliation, timing diagnosis, and loop-ready feedback.
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

# Pipeline Performance Judge

Use this portable workflow skill once for a complete, bounded reconciliation
after a merge request is published. This agent alone owns live GitLab pipeline
observation, internal waiting, timing and failure diagnosis, and any
deduplicated feedback issue for the serial work loop. No other workflow role
polls or interprets the live pipeline. Run this bounded work once. This work
has one owner. Its scope is set.

Before you act, read and follow [the role instructions](../../agents/pipeline-performance-judge.md).
That file is a compatibility reference for Claude Code. This directory is the
shared skill entry point for Codex, Pi, and Claude Code.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router resolves its
delegation mapping. Each frontend resolves that tier through its configured
model mapping. Do not hard-code a provider-specific model name in this skill.
