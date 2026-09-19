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

Use this portable workflow skill for the implementation judge phase. Read and
follow [the role instructions](../../agents/tdd-judge.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

The serial Phase-4 Judge records the exact staged candidate tree, then invokes
exactly one mutation-gated `agent-work validation run --candidate-tree TREE
--apply`. That durable candidate-tree-keyed validation session executes one
fixed hook, persists the terminal exit, and uses its idempotent candidate
lock/session to prevent a duplicate hook run. The Judge does not invoke the
hook directly. If frontend output yields or its execution handle is lost, the
Judge uses the read-only `agent-work validation status --candidate-tree TREE
--wait-seconds 30` action for the same candidate until a persisted terminal
exit exists. Status never launches a hook. A running or missing exit
authorizes no tree verdict, artifact inspection, non-functional delegation, or
handoff. A terminal nonzero returns to Green. Only terminal zero permits the
unchanged-tree check and capture of immutable absolute supported-version JUnit
snapshots with SHA-256 digests and test counts, followed by delegation to
`$nonfunctional-review`. The Judge returns the combined handoff to Green
before the Phase-5 reviewer fanout.

## Model selection

Use this skill's `metadata.model-tier`; the live Architect router resolves its delegation mapping. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
