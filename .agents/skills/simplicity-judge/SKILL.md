---
name: simplicity-judge
description: Check a plan for the smallest necessary change.
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


# Simplicity Judge

Read and follow [the role instructions](../../agents/simplicity-judge.md).
Use this read-only skill for one plan review. Return findings to Architect.

## Model selection

Use this skill's `metadata.model-tier`. Router maps it through the existing
frontend mapping and current user instructions.
