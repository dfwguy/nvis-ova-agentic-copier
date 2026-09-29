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
run. In interactive work, use the exact staged tree and its available immutable
candidate JUnit snapshots, digests, and counts. A new or modified test strictly
above 1.000 seconds is the ordinary hard block; exactly 1.000 is allowed.
Send 0.100-second suggestions to a separate follow-up. Absent paired proof
makes baseline regression `UNKNOWN` without waiving the full-JUnit gate;
malformed or mismatched present proof is invalid. Controlled task-specific
hard budgets still apply. In serial work, require the
available Python 3.11 and 3.14 baseline and candidate timing proof bound to exact source
trees. Use only controller-owned source/run digests, counts, completeness,
candidate slow-case data, findings, and transcript outcome. The controller
keeps full inventories and exact-source repeats private. If the hook passes
but a fixed JUnit file is missing or raises `OSError`, set timing to `UNKNOWN`
and keep reviewing. Do not claim a timing pass. Reject other missing, partial, or mismatched
serial proof. Never request controller-private snapshot paths or treat
model prose as validation evidence. Return the timing receipt to the Judge,
never to specialist review.
