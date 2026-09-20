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

Use this portable workflow skill for the implementer phase. Read and follow
[the role instructions](../../agents/tdd-green.md) before acting. That file is
role-reference material. This directory is the shared skill entry point for
Claude Code, Codex, and Pi.

## Model selection

The live Architect delegates this phase after each applicable `phase-2
receipt/N/A`. It waits while Green owns all later worktree mutations and review
feedback. Use this skill's `metadata.model-tier`. The Architect router picks
the mapping.

Green stages and captures the `Phase-3 candidate tree` before it delegates the
`serial Phase-4 Judge`. The Judge's Git-hook receipt is needed for the
`Phase-5 review fanout`. That receipt has the `unchanged tree OID`,
`supported-version immutable JUnit snapshots`, `SHA-256 digests`, counts, and
its `$nonfunctional-review` child receipt.

After any `candidate-tree change`, Green stages a new tree. It gets a fresh
`Phase-4 Judge receipt` before dependent review or finalization. Each coding
candidate needs the full hook. Green's fast formatter, lint, and type commands
repair. The Judge owns one expensive `tox/full-suite` verification.
