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

Use this portable workflow skill for the implementer phase. Read and
follow [the role instructions](../../agents/tdd-green.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

The live Architect delegates this phase after every applicable phase-2 receipt/N/A and waits
while Green owns all subsequent worktree mutations and review feedback. Use this
skill's `metadata.model-tier`; the Architect router resolves the mapping.

Green stages and captures the Phase-3 candidate tree before delegating the
serial Phase-4 Judge. The Judge's Git-hook receipt, unchanged tree OID,
supported-version immutable JUnit snapshots, SHA-256 digests and counts, and
its `$nonfunctional-review` child receipt are prerequisites for the Phase-5
review fanout.

After any candidate-tree change, Green stages a new tree and obtains a fresh
Phase-4 Judge receipt before dependent review or finalization. Every coding
candidate requires the full hook. Green's fast formatter, lint, and type commands repair;
the Judge owns the one expensive tox/full-suite verification.
