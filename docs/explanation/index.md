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

Use these pages to learn why the template and agent loop work this way.
They explain key choices, limits, and trade-offs. Read the page that fits
your question. Then use its links to learn more.

<!-- framing: hand-written, 2-4 sentences, EDITABLE -->

<!-- BEGIN GENERATED -->
### Template Architecture
- [Why the template and generated project are separate](template-and-generated-project.md) — Learn why templates and projects differ.
- [Why this repository uses Diátaxis](why-diataxis.md) — Learn why docs separate reader needs.
- [Why the coding standards exist](coding-standards.md) — Learn why projects use these coding standards.

### Agent Workflow
- [Recoverable serial agent-loop architecture](serial-agent-loop-recovery-design.md) — Learn how the loop recovers unattended work.
- [Why the agent workflow uses specialized roles](agentic-workflow.md) — Learn why agent tools share one workflow.
- [Why the agent loop is serial and GitLab-native](serial-gitlab-agent-loop.md) — Learn why the loop uses GitLab and local state.
<!-- END GENERATED -->
