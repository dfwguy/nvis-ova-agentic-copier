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

In serial work, Green supplies the controller-staged candidate tree. Judge
requests the controller-owned, identity-bound `candidate commit` for that
exact tree while active. The transaction runs the installed hook once and
returns the Green SHA, committed tree, and bounded validation proof. Judge does
not call `git add`, `git write-tree`, the hook, `agent-work validation run`, or
`agent-work validation status`. A lost or incomplete result is no pass; use the
trusted status boundary for the same candidate or return to Green for safe
recovery. The candidate commit runs no separate prior validation hook. A
terminal nonzero
returns to Green. Only a verified Green commit permits Judge to check its
exact tree, retained Red parent/ref, clean state, and available Green hook
JUnit proof for Python 3.11 and 3.14. Bind it to the Green SHA, committed
tree, and opaque evidence identity. Use JUnit digests, counts, and the complete
bounded list of cases above 0.100 seconds. Do not require paired baseline
runs or phase deltas in ordinary serial coding. Baseline-relative regression
remains `UNKNOWN` and nonblocking. If the hook passes but a JUnit file is
missing or `OSError`-unreadable, use timing `UNKNOWN` and keep reviewing
without a timing pass. A valid readable JUnit file with an overflowing
bounded advisory case summary also has timing `UNKNOWN`. Malformed,
oversized, or invalid present JUnit data blocks review. A failed hook still
fails. Missing or mismatched commit identity
proof is no pass. The legacy staged-tree route still rejects malformed
validation proof. Do not request controller-private snapshot paths. Delegate
`$nonfunctional-review` with this source-bound evidence. Return the joined
handoff to Green before specialist review.

For existing-MR repair, use the role's rebase mode. Validate the final SHA/tree
with the unchanged full hook; create no commit and require no Red checkpoint.
Return native and timing results to Reconciler.

## Model selection

Use this skill's `metadata.model-tier`. The live Router maps it to a
model. Each frontend uses its configured map. Do not name a provider model in
this skill.
