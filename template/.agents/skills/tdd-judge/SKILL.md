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

Use this portable skill for validation. Read and follow
[the role instructions](../../agents/tdd-judge.md) before acting. That file
defines the role. This skill works with Claude Code, Codex, and Pi.
Judge does not edit.
It checks one tree. It gives one receipt.

The serial validation Judge records the exact staged candidate tree. It invokes
one mutation-gated `agent-work validation run --candidate-tree TREE --apply`.
The durable tree-keyed session runs one fixed hook and saves its terminal exit.
Its idempotent lock and session block a duplicate hook run. Judge does not call
the hook.
The session runs it.
It runs once.

If output yields or its handle is lost, use read-only `agent-work validation
status --candidate-tree TREE --wait-seconds 30`. Use it until a saved terminal
exit exists. Status never starts a hook. A running or missing exit gives no
tree verdict, artifact check, child review, or handoff. A terminal nonzero goes
back to Green. Only terminal zero permits the unchanged-tree check. Then get
fixed-version JUnit snapshots with absolute paths, SHA-256 digests, and counts.
Delegate `$nonfunctional-review`. Return the combined handoff to Green before
specialist review.

For normal reconciliation, distinguish retained Red-to-source provenance from
fresh three-way reconciliation evidence for the exact joined candidate. For an
approved pre-checkpoint migration, assess current tests and behavior plus the
exact migration evidence; do not claim Red-to-Green provenance.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router maps it to a
model. Each frontend uses its configured map. Do not name a provider model in
this skill.
