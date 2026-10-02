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

Use this skill's `metadata.model-tier`. The live Router maps it to a
model. Each frontend uses its own configured map. Do not name a provider model
in this skill.

The TDD Judge delegates this review after the Green commit's one hook
run. In interactive work, use the exact staged tree and its available immutable
candidate JUnit snapshots, digests, and counts. A new or modified test strictly
above 1.000 seconds is the ordinary hard block; exactly 1.000 is allowed.
Send 0.100-second suggestions to a separate follow-up. Absent paired proof
makes baseline regression `UNKNOWN` without waiving the full-JUnit gate;
malformed or mismatched present proof is invalid. Controlled task-specific
hard budgets still apply. In serial work, require the available Python 3.11
and 3.14 Green hook JUnit digests, counts, and complete bounded case list
above 0.100 seconds. Bind them to the Green SHA and tree. If the serial Green
hook passes but a JUnit file is missing or `OSError`-unreadable, use timing
`UNKNOWN` and keep reviewing without a timing pass. A valid readable JUnit
file with an overflowing bounded advisory case summary also has timing
`UNKNOWN`. Malformed, oversized, or invalid present JUnit data blocks review.
A failed hook or mismatched commit
identity proof still fails. The legacy staged-tree route still rejects
malformed proof. Never request controller-private snapshot paths or treat
model prose as validation evidence. Return the timing receipt to the Judge,
never to specialist review.
