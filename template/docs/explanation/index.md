---
title: Explanation
type: index
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

Read this material for the reasoning behind the agent-loop design.

<!-- framing: hand-written, 2-4 sentences, EDITABLE -->

<!-- BEGIN GENERATED -->
### Agent workflow
- [Recoverable serial agent-loop architecture](serial-agent-loop-recovery-design.md) — Understand the recoverable state machine for unattended agent work.
- [Why the agent workflow uses specialized roles](agentic-workflow.md) — Understand how explicit skill routing keeps every supported agent frontend on the same workflow.
- [Why the agent loop is serial and GitLab-native](serial-gitlab-agent-loop.md) — Understand why bounded workflow sessions use GitLab projection and local durable state.
<!-- END GENERATED -->
