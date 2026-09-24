---
name: consistency-judge
description: Check changed facts in interactive work against authoritative definitions and copies on one exact staged tree.
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

Use this read-only skill in interactive work for a changed fact with copies.
Also use it there when Architect cannot rule that out. In serial work, record
exactly `$consistency-judge: not applicable — trusted pre-finalization candidate tree/read boundary unavailable`.
Read [the role](../../agents/consistency-judge.md)
before review. The [review examples](references/eval-scenarios.md) show the
method. They are not tests or a source of truth.

## Model selection

Use this skill's `metadata.model-tier`. Architect maps the tier for each
frontend. Do not name a provider model here.
