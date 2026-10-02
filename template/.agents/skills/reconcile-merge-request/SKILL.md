---
name: reconcile-merge-request
description: Safely update one open GitLab merge request after it conflicts with its latest target.
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

# Reconcile Merge Request

Use this skill to repair one existing open GitLab merge request by rebasing
its source onto the current target. Read and follow
[the Reconciler role](../../agents/reconciler.md) before acting.
Reconciler owns repair, final-result validation, reviews, and the same-MR
handoff through `$git-flow`. This route needs no Architect, Red, or Green stage.
Only humans may approve or merge the request.

## Model selection

Use this skill's `metadata.model-tier`. Router maps that tier through the
active frontend. Do not hard-code a provider model.
