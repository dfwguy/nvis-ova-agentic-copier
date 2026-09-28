---
title: How to update a generated project
type: how-to
summary: Apply a reviewed template revision and inspect its human-review merge request.
audience: practitioner
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-28
related:
  - path: preview-template-revision.md
    why: Compares the recorded template revision with the revision to apply.
  - path: ../reference/template-update.md
    why: Defines the update command, durable states, and result data.
  - path: setup-serial-gitlab-agent-loop.md
    why: Shows the user-bus check used to supervise an optional template-update timer.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the candidate MCP host primitive and its limits.
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
project and publish a change for human review.

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

4. Read the result. For a changed update, the controller checks the final
   worktree and runs its own exact-tree validation, then commits the tree and verifies
   one merge request. Open that request for human review. Only a human may
   approve or merge it. An unchanged update has no merge request to publish.

5. Follow the reported recovery action if Copier, validation, or publication
   stops. The command keeps the worktree and evidence. It does not guess
   whether an uncertain remote write succeeded or create another merge
   request. Check the retained branch and request named by the result. Do not
   discard, reset, or stash the work.

## Add MCP to an updated project

Copier keeps an existing project's `pyproject.toml`. If that preserved file
lacks `mcp>=2,<3`, add the dependency before wiring a host adapter. Have the
project owner review and approve the dependency, update `pyproject.toml` and
`uv.lock`, and review both file changes. The ordinary factory loop remains
usable without an adapter. The
[serial-loop reference](../reference/serial-gitlab-agent-loop.md#candidate-preview-mcp-host-primitive)
defines the host primitive and its limits.

## Set up an optional scheduled preview

The separate template-update user timer is opt-in. Install its generated
service and timer only after you choose the candidate revision and the
checkout to inspect. This repository's unit names are
`agentic-copier-template-update.service` and
`agentic-copier-template-update.timer`; generated projects use their project
slug in place of `agentic-copier`. Keep the candidate revision in the private
environment file at `%h/.config/agentic-copier-template-update/template-update.env`
for this repository, or `%h/.config/<project-slug>-template-update/template-update.env`
for a generated project; set
`TEMPLATE_UPDATE_CANDIDATE_REVISION` to the revision you reviewed. The service
also reads absolute `AGENT_WORK_CHECKOUT` and `AGENT_WORK_DIRENV` paths from
that file. Do not put a token in the file or unit. The service uses `direnv`
in the active checkout, then calls its `bin/agent-work-from-checkout`. Set up
that checkout's ignored `.envrc` to load `GITLAB_HOST`, `GITLAB_PROJECT_ID`,
`AGENT_WORK_BASE_BRANCH`, `AGENT_WORK_FACTORY_USERNAME`, and absolute
`AGENT_WORK_GIT` and `AGENT_WORK_GLAB` paths. Load `GITLAB_TOKEN` from your
secure store. Restrict the env file to your user. The template source must be
public HTTPS or fetchable through
the SSH agent in `SSH_AUTH_SOCK`; this command does not pass a token to the
template fetch. If the source cannot be fetched, the preview stops safely.
Its default invocation omits `--apply`, so a timer activation only previews the
update. Review the preview before explicitly enabling apply in your local
service configuration. The generated project does not install or enable these
units for you.

Before running `systemctl --user` or `journalctl --user`, confirm that the
current user's runtime directory exists, is owned by that user, and contains
the user-bus socket. Then inspect the template-update service and timer through
that bus. Keep the main serial issue-loop units separate from this optional
timer. The [serial-loop setup guide](setup-serial-gitlab-agent-loop.md#establish-a-validated-user-bus-boundary)
shows the user-bus check.

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
