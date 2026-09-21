---
name: tdd-green
description: Implement the minimum production change needed to make approved Red tests pass.
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

# Implementer

Use this portable skill for implementation. Read and follow
[the role instructions](../../agents/tdd-green.md) before acting. That file
defines the role. This skill works with Claude Code, Codex, and Pi.
Green owns the worktree. Reviewers do not edit it.

## Model selection

The live Architect delegates this work after each applicable preparation
receipt/N/A and the accepted mode-specific Red checkpoint. It waits while
Green owns all later worktree mutations and review feedback. Use this skill's
`metadata.model-tier`. The Architect router picks the mapping.

For normal reconciliation, Green receives the mechanically re-proved retained
Red checkpoint as the Red preparation receipt. In an approved `pre-checkpoint`
migration, it instead receives the exact N/A receipt stating that no authentic
Red exists, the cutoff proof, and the conflict manifest. In either case, it
receives exact source and target identities, preserves only approved source and
target intent, and records fresh three-way evidence. Finalization directly
commits the approved staged tree from the pending merge state. Do not compact a
passing commit.

Green records the exact source, target, tree, test result, and review result in
its handoff.

Green stages the implementation candidate tree. It records the tree before it
delegates the serial validation Judge. Specialist review needs the Judge's
validation-session receipt. That receipt has the unchanged tree OID, fixed
version JUnit snapshots, SHA-256 digests, counts, and the
`$nonfunctional-review` child receipt.

After a candidate-tree change, Green stages a new tree. It gets a fresh Judge
receipt before review or finalization. Each coding candidate needs the full
hook. Green uses fast format, lint, and type commands for repair. The validation
session owns one costly tox or full-suite check. It runs one hook.
