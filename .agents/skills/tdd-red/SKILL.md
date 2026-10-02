---
name: tdd-red
description: Write failing behavior tests before implementation and stop at Red.
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

# Test Writer

Use this portable skill for test writing. Read and follow
[the role instructions](../../agents/tdd-red.md) before acting. That file
defines the role. This skill works with Claude Code, Codex, and Pi.

Existing-MR repair uses Reconciler and `$reconcile-merge-request`, not Red.
It needs no old checkpoint or original Red receipt to enter that route.
Preserve old refs and history; never create proof after the fact.

## Model selection

Router runs Red first and checks its evidence. For interactive work, Main
creates the local Red commit and ref. For serial work, the trusted tool creates
the tree ref. Policy work starts after that checkpoint, when needed. Green and
the Diátaxis Writer start together after policy work. Use this skill's
`metadata.model-tier`. The live Router maps the tier.
