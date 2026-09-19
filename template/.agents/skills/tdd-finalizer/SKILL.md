---
name: tdd-finalizer
description: Verify final receipts and exact evidence, then make the controlled interactive commit.
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

# Finalizer

Use this portable workflow skill for the finalizer phase. Read and
follow [the role instructions](../../agents/tdd-finalizer.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

For ordinary interactive Phase-6 publication, Finalizer may commit with
`--no-verify` only after mechanically matching the staged tree and JUnit
snapshots/digests/counts plus the Non-functional Reviewer receipt to the Judge
handoff. It never stages; the serial-worker
exception remains no-commit.

## Model selection

Request the `fast` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
