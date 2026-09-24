---
name: agent-policy-writer
description: Edit approved agent-policy files in a coding diff; not content-only documentation or read-only review.
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

# Agent Policy Writer

Use this skill only when a coding diff changes agent instructions. Read and
follow [the role instructions](../../agents/agent-policy-writer.md). The Architect
starts it after the accepted Red checkpoint. It finishes before Green and the
Diátaxis Writer start together. Edit only AGENTS.md,
.agents/**, and declared portable template mirrors. Do not edit tests,
production code, or docs. Preserve exact mirrors and return a policy receipt.
