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

Read these pages when you need the reasoning behind the template and its agent
workflow. They explain design choices, boundaries, and trade-offs.

<!-- framing: hand-written, 2-4 sentences, EDITABLE -->

<!-- BEGIN GENERATED -->
### Template architecture
- [Template and generated project](template-and-generated-project.md) — Understand the boundary between this Copier template and its generated output.
- [Why Diátaxis](why-diataxis.md) — Understand why the documentation separates reader needs.
- [Why coding standards exist](coding-standards.md) — Understand the rationale for the generated development conventions.

### Agent workflow
- [Recoverable serial agent-loop architecture](serial-agent-loop-recovery-design.md) — Understand the recoverable state machine for unattended agent work.
- [Why the agent workflow uses specialized roles](agentic-workflow.md) — Understand how explicit skill routing keeps every supported agent frontend on the same workflow.
- [Why the agent loop is serial and GitLab-native](serial-gitlab-agent-loop.md) — Understand why bounded workflow sessions use GitLab projection and local durable state.
<!-- END GENERATED -->
