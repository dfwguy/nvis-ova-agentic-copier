---
title: How to update a generated project
type: how-to
summary: Update a Copier project with a reviewed template revision.
audience: practitioner
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-23
related:
  - path: preview-template-revision.md
    why: Compares the recorded template revision with the revision to apply.
  - path: ../reference/template-update.md
    why: Defines the update command, durable states, and result data.
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

# How to update a generated project

Use this guarded command to apply a reviewed template revision to a Copier
project.

1. Preview the revision. It compares template commits. It cannot predict
   rendered files or Copier conflicts.

   ```bash
   uv run agent-work template preview --candidate-revision REV
   ```

2. Check that the generated project's default-branch checkout is clean.
   Commit or resolve local work first. The command rejects another branch or
   a dirty checkout.

3. Run the update with the reviewed candidate revision:

   ```bash
   uv run agent-work template update --candidate-revision REV --apply
   ```

   Without `--apply`, the command plans only. It uses the resolved candidate
   commit, not a moving branch. It uses the factory project lock. It creates a
   kept worktree and branch in XDG state, then runs Copier there. Copier keeps
   project answers and later edits under its update rules.

4. Read the result. A changed update checks the generated project's needed
   workflow in the worktree and returns merge-request draft data. It does not
   commit, push, create a merge request, approve, merge, schedule work, or
   change issue state. Publishing and scheduling are outside this command.

5. Stop for `prepared`, `applying`, `workflow`, `conflict`, or an interrupted
   state. The command never replays an unclear state. It keeps the worktree
   and reports a safe recovery state, paths, and next action. Follow that
   action in the kept worktree. Do not discard, reset, or stash it.

Use the [template update reference](../reference/template-update.md) for the
full interface, states, and result data. Use the
[template reference](../reference/template-contract.md#update-behavior) for
files that Copier preserves.

## Handle legacy Claude files

If Copier reports `.claude/project.md`, move its project facts into
`.agents/project.md`. Remove the old file, then rerun Copier. Copier keeps
both files during this move. Agents can read both contexts, as `AGENTS.md`
requires, until the move ends.

After the update, confirm that Claude Code uses the canonical workflow tree:

```bash
test "$(readlink .claude/skills)" = ../.agents/skills
```

The guarded task keeps a correct link. For an older copied `.claude/skills`
directory, it replaces only a tree that matches `.agents/skills`. A different
directory or link stays intact. Keep or reconcile it, then rerun Copier. Do
not remove it blindly.

## Migrate a legacy flat Python package.

The template uses `src/<package>/`. Before you accept this update, move an old
top-level `<package>/` app with care. Do not leave both paths in the tree. The
generated filesystem gate rejects a split layout.

1. Start with a clean tree. Make a backup branch or tag before you change the
   package layout.
2. Create `src/`. Move all app files from `<package>/` to `src/<package>/`.
   Include user modules. Copier cannot find or move them.
3. Update `pyproject.toml` so Hatch builds `packages = ["src/<package>"]`.
   Keep or add Python console commands in `[project.scripts]`.
4. Run `uv run pytest` and `uv run tox`. Fix imports, package setup, and tests
   before you continue.
5. Update `.agents/project.md` if it names the old layout. Then use `--apply`.
   Copier keeps `pyproject.toml` and `.agents/project.md`. It does not replace
   this project work. Run the checks again.
