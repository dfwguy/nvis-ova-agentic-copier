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
invokes the controller-owned, identity-bound durable validation and status
boundary for that exact tree while active. The controller runs one
mutation-gated session and returns a bounded program-owned result. Judge does
not call `git add`, `git write-tree`, the hook, `agent-work validation run`, or
`agent-work validation status`. A lost or incomplete result is no pass; use the
trusted status boundary for the same candidate or return to Green for safe
recovery. The
durable tree-keyed session prevents duplicate hook runs. A terminal nonzero
returns to Green. Only terminal zero permits Judge to check the unchanged tree
and, in serial work, require available controller-owned baseline and candidate
JUnit timing proof for Python 3.11 and 3.14, bound to their exact source trees and
opaque evidence identities. Use bounded run digests, counts, completeness,
candidate slow-case data, findings, and transcript outcome. Full inventories
and exact-source repeats stay private. If the hook passes but a fixed JUnit
file is missing or raises `OSError`, set timing to `UNKNOWN` and keep reviewing.
Do not claim a timing pass. Other missing, partial, or mismatched serial proof is
no pass. Do not request controller-private snapshot paths. Delegate
`$nonfunctional-review` with this source-bound evidence. Return the joined
handoff to Green before specialist review.

For reconciliation, distinguish retained Red-to-source provenance from fresh
three-way merge proof for the joined tree. While the merge is pending, run
`agent-work validation run --candidate-tree <staged-tree> --apply`. It checks
the staged tree without a commit. Main later commits the approved two-parent
merge. Finalizer verifies it read-only.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router maps it to a
model. Each frontend uses its configured map. Do not name a provider model in
this skill.
