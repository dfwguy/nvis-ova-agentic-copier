---
title: Your first project
type: tutorial
summary: Make a new project, then check that its tools are ready.
audience: new-user
tags: [project-generation]
status: current
last_reviewed: 2026-09-06
related:
  - path: ../how-to/create-a-project-non-interactively.md
    why: Provides the automation-oriented alternative to this lesson.
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

# Your first project

This lesson creates a working Python project. It then checks that its tools are
ready.

## Before you begin

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git,
and [`glab`](https://gitlab.com/gitlab-org/cli). Copier needs each command.
Copier creates the project. UV, Git, and glab support Copier's setup and
readiness checks.

Before making a commit, ensure Git can resolve an identity. Set it in the
generated repository or through directory-scoped environment variables, such
as `direnv`. A global Git identity is not required. Before using the GitLab
workflow, ensure `glab` has credentials for
`${GITLAB_HOST:-gitlab-master.nvidia.com}`. A directory-scoped `GITLAB_TOKEN`
is supported.

Bash and [ShellCheck](https://www.shellcheck.net/) are only needed for the
generated Bash validation. They do not prevent project generation.

You do not need Python separately. You do not need Ruff or pytest separately.
You do not need the other project tools separately. UV provides the project
interpreter and tools.

## Generate a project

Run Copier through `uvx`. Give the new project a destination directory:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
```

Answer the prompts with a project name, an importable package name, and a
short description. `--trust` is deliberate. The template runs declared tasks
after rendering. The command preflight checks `git`, `uv`, and `glab`. Copier
runs `git init` only after it passes. It then
runs `git var GIT_AUTHOR_IDENT` in the new repository. It also makes an
authenticated `glab api user` request to
`${GITLAB_HOST:-gitlab-master.nvidia.com}`. These readiness checks do not block
`uv sync` or `uv run pre-commit install`. Review those tasks in
[`copier.yaml`](../../copier.yaml) before trusting a template revision.

The check runs after rendering. Copier has made the destination folder. Each
diagnostic runs before that action. If it reports a missing command, install it
and run the command again. Fix a Git identity or GitLab access issue before
that action.
Use a new destination directory if Copier does not accept the old one. An old
destination directory may be accepted if it is empty.

## Inspect the generated project

Move into the new directory and run its fast test suite:

```bash
cd my-project
uv run pytest
```

You now have a package directory. Its name comes from your chosen slug. You
also have a starter test and an installed virtual environment. The project
includes Ruff, ty, tox, pre-commit, GitLab CI, and the agent workflow under
`.agents/`.
`CLAUDE.md` is an adapter to `AGENTS.md`. Claude Code reaches the same skills
through the relative `.claude/skills -> ../.agents/skills` link. Do not create
or replace that link yourself.

## Run the full verification matrix

Run every configured Python-version test environment:

```bash
uv run tox
```

Use this command before publishing substantive changes. It verifies the test
matrix. Generated GitLab CI uses the same matrix.

## What you learned

You generated a project. You allowed its declared setup tasks. You checked its
fast local test path and full test matrix. For a precise list of generated
content, see the [template reference](../reference/template-contract.md). For
a command-oriented variation, see [create a project non-interactively](../how-to/create-a-project-non-interactively.md).
