---
name: consistency-judge
description: Check changed facts against authoritative definitions and copies on one exact candidate tree.
metadata:
  model-tier: most-capable
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

# Consistency Judge

Use this read-only skill when a changed fact has copies or Router cannot
rule that out. In serial work, require a live controller-owned `candidate read`
for the exact candidate. Get the controller's canonical inventory, including
each changed path's old/new mode, blob ID, and size. Account for both sides of
every path: confirm absence where a side does not exist, and read the full
content of every present side. Use exact Git ranges by path, side, offset, and
length, each no larger than 128 KiB. Bind each result to repo, issue, base
commit/tree, candidate tree, path, side, blob ID, offset, and length. Compare
the ranges with the full side size; request missing ranges and reject gaps,
unreadable sides, or mismatched evidence. Account for mode-only changes even
when content is unchanged. Repeated reads need no hidden cursor, coverage
bitmap, or completion call. Git proves returned bytes and tree identity; the
judge owns reading and assessing every changed side. Delivery alone does not
prove comprehension. Restaging voids prior read proof. If the boundary is
unavailable in the selected mode,
record exactly `$consistency-judge: not applicable — trusted pre-finalization candidate tree/read boundary unavailable`
with mode and capability evidence. A missing, rejected, failed, or stale read
from an expected boundary blocks finalization.
Read [the role](../../agents/consistency-judge.md)
before review. The [review examples](references/eval-scenarios.md) show the
method. They are not tests or a source of truth.
Check matching behavior for dependency, tool, hook, tox, CI, lockfile, and
Jinja changes. Policy mirrors must match byte for byte. Inspect a Copier
render when a template change can alter output.

For existing-MR repair, use the role's rebase review mode. Bind review to the
final rebased SHA/tree and both intents; require no historical Red checkpoint.

## Model selection

Use this skill's `metadata.model-tier`. Router maps the tier for each
frontend. Do not name a provider model here.
