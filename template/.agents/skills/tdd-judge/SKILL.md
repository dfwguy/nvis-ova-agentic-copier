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

The serial Phase-4 Judge starts one `git hook run pre-commit` command and
waits for that same command/session to reach terminal exit code 0 before it
records the unchanged passed tree OID and immutable absolute
supported-version JUnit snapshots with SHA-256 digests and test counts, then
delegates `$nonfunctional-review` as its read-only child. It returns their
combined handoff to Green before the Phase-5 reviewer fanout; it does not
duplicate individual hook commands or rerun a suite. Yielded or still-running
output has no validation meaning and never authorizes a handoff.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
