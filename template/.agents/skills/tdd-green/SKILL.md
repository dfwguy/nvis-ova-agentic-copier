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
Green owns production and test edits. Diátaxis Writer concurrently owns
`docs/**`, `template/docs/**`, `README.md`, and prose in
`template/README.md.jinja`. Reviewers do not edit files.

## Model selection

The live Architect delegates this work after policy preparation and the
accepted mode-specific Red checkpoint, beside the Diátaxis Writer. It waits
while Green coordinates review feedback. Use this skill's
`metadata.model-tier`. The Architect router picks the mapping.

For reconciliation, Green receives the mechanically re-proved retained Red
checkpoint as the Red preparation receipt. It also receives exact source,
target, and conflict manifest. It records fresh three-way evidence apart from
historical Red-to-source provenance. Finalization directly commits the approved
staged tree from the pending merge state. Do not compact a passing commit.

Main stages the combined candidate tree only after Green and Diátaxis Writer
stop editing. Green receives that tree before it delegates the serial
validation Judge. Specialist review needs the Judge's
validation-session receipt. That receipt has the unchanged tree OID, fixed
version JUnit snapshots, SHA-256 digests, counts, and the
`$nonfunctional-review` child receipt.

After a change, Main waits for both writers to stop. It stages the new tree.
Green gets a fresh Judge
receipt before review or finalization. Each coding candidate needs the full
hook. Green uses fast format, lint, and type commands for repair. The validation
session owns one costly tox or full-suite check. It runs one hook.
