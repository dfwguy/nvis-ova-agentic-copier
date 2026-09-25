---
name: consistency-judge
description: Check changed facts against authoritative definitions and copies on one exact candidate tree.
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

# Consistency Judge

Use this read-only skill when a changed fact has copies or Architect cannot
rule that out. In serial work, require a live controller-owned `candidate read`
for the exact candidate. A paged read needs all old/new side bytes and a
successful `candidate read --complete` before applicability or review.
Restaging voids that coverage. If the boundary is unavailable in the selected mode,
record exactly `$consistency-judge: not applicable — trusted pre-finalization candidate tree/read boundary unavailable`
with mode and capability evidence. A missing, rejected, failed, or stale read
from an expected boundary blocks finalization.
Read [the role](../../agents/consistency-judge.md)
before review. The [review examples](references/eval-scenarios.md) show the
method. They are not tests or a source of truth.

## Model selection

Use this skill's `metadata.model-tier`. Architect maps the tier for each
frontend. Do not name a provider model here.
