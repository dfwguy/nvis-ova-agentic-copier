---
name: nonfunctional-review
description: Review the shared JUnit timing evidence before final validation.
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

# Non-functional Reviewer

Use this portable skill for non-functional review. Before you act, read and
follow [the role instructions](../../agents/nonfunctional-review.md). That file
defines the role. This skill works with Claude Code, Codex, and Pi.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router maps it to a
model. Each frontend uses its own configured map. Do not name a provider model
in this skill.

The TDD Judge delegates this review after the validation session's one hook
run. Use only the exact staged tree and immutable absolute JUnit snapshot
paths, digests, and counts it provides. Return the timing receipt to the
Judge, never to specialist review.
