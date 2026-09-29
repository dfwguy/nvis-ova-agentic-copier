---
name: idle-recovery-adviser
description: Give a bounded, read-only create-child or defer advisory after genuine loop idleness.
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

# Idle Recovery Adviser

Read and follow [the role instructions](../../agents/idle-recovery-adviser.md).
Give advice only. The serial controller checks facts, saves intent, and creates
or finds the child.
