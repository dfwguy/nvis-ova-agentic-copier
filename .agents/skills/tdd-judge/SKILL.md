---
name: tdd-judge
description: Run the full installed-hook suite and record JUnit evidence without editing files.
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

# Implementation Judge

Use this portable workflow skill for the implementation judge phase. Read
and follow [the role instructions](../../agents/tdd-judge.md) before acting.
That file is role-reference material. This is the shared skill entry point
for Claude Code, Codex, and Pi.
Judge does not edit.
It checks one tree. It gives one receipt.

The serial Phase-4 Judge records the exact staged candidate tree. It then
invokes exactly one mutation-gated `agent-work validation run --candidate-tree
TREE --apply`. The durable candidate-tree-keyed validation session runs one
fixed hook and saves its terminal exit. Its idempotent candidate lock/session
blocks a duplicate hook run. The Judge does not call the hook.
The session runs it.
It runs once.

If frontend output yields or its handle is lost, use read-only `agent-work
validation status --candidate-tree TREE --wait-seconds 30` for that candidate.
Use it until a persisted terminal exit exists. Status never starts a hook. A
running or missing exit gives no tree verdict, artifact check,
non-functional delegation, or handoff. A terminal nonzero returns to
Green. Only terminal zero permits the unchanged-tree check. Then get immutable
absolute supported-version JUnit snapshots with SHA-256 digests
and test counts. Delegate `$nonfunctional-review`. Return the combined handoff
to Green before the Phase-5 reviewer fanout.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router picks its
delegation mapping. Each frontend uses its configured model mapping. Do not
hard-code a provider-specific model name in this skill.
