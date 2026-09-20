---
title: Why this repository uses Diátaxis
type: explanation
summary: Learn why docs separate reader needs.
audience: maintainer
tags: [template-architecture]
status: current
last_reviewed: 2026-09-06
related:
  - path: coding-standards.md
    why: Explains the conventions that apply this documentation structure.
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

# Why this repository uses Diátaxis

This page explains why the repository organizes documentation by reader need, instead of putting every topic in one guide.

Each type has one clear job. This helps readers find the right page fast. It
also keeps a page from trying to do too much.

[Diátaxis](https://diataxis.fr/) is a documentation framework with four reader needs.

People use documentation for different goals. A new user wants a guided success. An active user wants a reliable path to a task. A maintainer needs facts. A contributor needs context and trade-offs. One page rarely serves all these goals well.

Diátaxis gives each goal a home:

- **Tutorials** help a learner gain confidence through a complete lesson.
- **How-to guides** help a reader accomplish a particular task.
- **Reference** provides precise facts without a narrative detour.
- **Explanation** supplies context and rationale behind a design.

This split helps a Copier template because reader needs change with the task. A team creating a project needs a tutorial or how-to. A team updating one needs the Copier contract. A maintainer assessing the template or its standards needs an explanation. Classifying a page first keeps each path short. It also lets pages link instead of repeat content.

This does not ban cross-links or require artificial isolation. A how-to should link to the reference it needs. An explanation can point to a tutorial that makes its idea concrete. The page's main job is the boundary. A task guide should not become an API catalog. A reference should not hide facts in a long lesson.

The repository makes this practical. Its workflow assigns documentation writing and read-only documentation review with normal development checks. The writer chooses the affected quadrant. The judge checks category fit, technical accuracy, completeness, and links. Documentation is a reviewed part of a change, not a final add-on.

For the contributor process, see the [Diátaxis documentation process](../../DIATAXIS.md). Browse the current quadrants at the [documentation index](../index.md). For another Explanation page, see [why the coding standards exist](coding-standards.md).
