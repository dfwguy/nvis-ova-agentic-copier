---
name: tdd-finalizer
description: Verify final receipts, retained Red history, and Green read-only.
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

Use this portable skill for finalization. Read and follow [the role
instructions](../../agents/tdd-finalizer.md) before acting. That file defines
the role. This skill works with Claude Code, Codex, and Pi.
Use the Judge handoff. Check each item. Keep the staged tree the same. Give a
clear receipt. Require the accepted Red checkpoint. Require Test Judge's
approval of the final Red-to-Green Git diff.

During interactive finalization, invoke `agent-work interactive-commit verify`
for the exact Green commit, tree, retained Red ancestry and ref, and clean
state. Never stage, commit, or compact. A failed call stops publication. A serial
worker never commits, stages, or runs `git write-tree`. It checks the
controller-owned exact-tree proof and opaque validation evidence instead of
private JUnit paths. Do not add a check or change the validated tree.

For coding reconciliation, Main creates the single ordered two-parent commit
from the Judge-approved staged tree and pending merge state. It consumes
`MERGE_HEAD`: captured old source is
first and exact target/`MERGE_HEAD` is second. Main supplies freshly fetched
ref proof; Finalizer checks it locally without remote or GitLab calls. It
postchecks cleared merge state, clean index and worktree, exact parents, tree,
message, and unchanged Red ref, without an intermediate passing commit or
sole-parent compaction.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router maps it to a
model. Each frontend uses its configured map. Do not name a provider model.
