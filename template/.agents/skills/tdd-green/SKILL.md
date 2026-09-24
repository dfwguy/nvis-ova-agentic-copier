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

The live Architect delegates this work after each needed preparation receipt
or N/A and the accepted mode-specific Red checkpoint, beside the Diátaxis
Writer. It stays live while Green coordinates review feedback. Use this
skill's `metadata.model-tier`. The Architect router picks the mapping.

For reconciliation, Green gets the rechecked Red proof, source, target, and
conflict list. Keep new merge proof apart from the saved Red to source proof.
Finalizer commits the passed staged tree from the pending merge. Do not fold
another commit into it.

The workflow owner waits for Green and Diátaxis Writer to stop editing. In interactive work, Main
stages their combined candidate and returns the tree ID. In serial work,
Green asks the trusted boundary to stage while still live. The controller
scans the worktree, normalizes changed paths, stages only allowed files, and
returns the tree ID. Green's path list cannot grant access. Green and Judge
do not stage with Git or call the old validation CLI in a serial worker.
Judge asks the trusted boundary to validate that exact tree and read its
saved status. The result binds the unchanged tree to trusted JUnit counts,
hashes, time data, and the `$nonfunctional-review` child result through an
opaque proof ID. It does not give private JUnit paths to the worker.
Interactive Judge evidence includes immutable supported-version JUnit
snapshots, SHA-256 digests, counts, and the child receipt.

After a change, use the applicable staging boundary for a new tree. Get a
fresh Judge receipt before review or finalization. Each coding candidate needs
the full hook. Green uses fast format, lint, and type commands for repair.
The trusted validation session owns one costly tox or full-suite check. It
runs one hook.
