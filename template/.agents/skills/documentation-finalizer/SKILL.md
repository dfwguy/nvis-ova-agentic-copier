---
name: documentation-finalizer
description: Run the final read-only structural and synchronization gates for content-only changes.
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

# Documentation Finalizer

Use this portable skill after the `Documentation Writer` sends a full diff.
The diff has content only. This skill owns the final docs and policy review. Before you
act, read and follow [the role instructions](../../agents/documentation-finalizer.md).

## Model selection

Use this skill's `metadata.model-tier`. The live Architect router maps
delegation.
