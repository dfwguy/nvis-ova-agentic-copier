---
title: How to customize a generated project's workflow
type: how-to
summary: Change generated code and tools. Keep them compatible with template updates.
audience: practitioner
tags: [project-generation]
status: current
last_reviewed: 2026-08-27
related:
  - path: ../explanation/template-and-generated-project.md
    why: Explains why template and project changes have different owners.
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

# How to customize a generated project's workflow

Use this guide for a generated project. It applies when that project needs its
own conventions or tools after creation. Make the change in the generated
project to keep it compatible with the template. Keep the template repository
unchanged. Review the change before you commit it.

1. Change the generated project's configuration rather than this template
   repository. For example, adjust dependency groups or tool settings in
   `pyproject.toml`. Update the project's `README.md` to describe its domain.
   Keep each change tied to the project's needs.
2. Keep the project's checks aligned. Run the relevant commands after a change:

   ```bash
   uv run ruff check --fix
   uv run ruff format
   uv run ty check
   uv run tox
   ```

3. If you change the agent workflow or coding conventions, update the
   generated project's `AGENTS.md` and referenced `.agents/` guidance at the
   same time. Those files are the source of truth for contributors and agents
   working in that project.
4. Commit the customization through the generated project's required feature
   branch and merge-request flow.

Use the project's own branch for this work. Ask a reviewer to check changes
that affect shared workflow. Merge only through the project's normal review.
Keep the review scope clear. Record why the project needs the change.
Run the checks again after a reviewer asks for a revision.
Keep the result with the merge request. Use the same checks for each revision.

Do not expect `uvx copier update --trust` to overwrite the generated project's
`README.md`, `.gitlab-ci.yml`, or package initializer. The template deliberately
preserves these files, plus `pyproject.toml` and `.agents/project.md`, because
they contain project-specific package metadata and agent context. Update them
deliberately when changing the project layout. Check preserved files after an
update. Reapply a needed local change by hand. For the reasoning, see
[why the template separates source from generated output](../explanation/template-and-generated-project.md).
