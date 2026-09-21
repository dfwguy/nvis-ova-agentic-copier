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

Use this portable workflow skill for finalization. Read and follow [the role
instructions](../../agents/tdd-finalizer.md) before acting. That file is role
reference material. This directory is the shared entry point for Claude Code,
Codex, and Pi.
Use the Judge handoff. Check each item. Keep the staged tree the same. Give a
clear receipt.

In ordinary interactive Phase 6 publication, Finalizer may commit with
`--no-verify` only after it mechanically matches the staged tree and separate
JUnit snapshots, digests, counts, and Non-functional Reviewer receipt to the
Judge handoff. It never stages. The serial-worker exception remains no-commit.
Do not add a new check. Do not change the tree.

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router picks its
delegation mapping. Each frontend uses its configured model mapping. Do not
hard-code a provider-specific model name.
