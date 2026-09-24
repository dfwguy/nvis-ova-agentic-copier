---
title: Template update
type: reference
summary: Look up the guarded template update command and its recovery states.
audience: integrator
tags: [template]
status: current
last_reviewed: 2026-09-24
related:
  - path: ../how-to/update-a-generated-project.md
    why: Shows how to prepare for and recover from a guarded template update.
  - path: template-revision-preview.md
    why: Defines the read-only comparison used before an update.
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

# Template update

This page defines the guarded template update and its publication boundary.

## Command

```text
agent-work template update --candidate-revision REV [--project-root PATH] [--json] [--debug] [--apply]
```

`--candidate-revision REV` is required. `--project-root PATH` defaults to `.`.
`--json` prints machine-readable data. `--debug` prints a traceback for local
diagnosis. `--apply` permits the update workflow and a human-review merge
request. Without it, the command plans only.

The plan uses the existing metadata comparison and makes no changes. It does
not run Copier, create a worktree or branch, or change project state.

## Optional user timer

The timer is off by default. This repo names its units
`agentic-copier-template-update.service` and `.timer`. A new project uses its
slug in those names. Put `TEMPLATE_UPDATE_CANDIDATE_REVISION` and
absolute `AGENT_WORK_CHECKOUT` and `AGENT_WORK_DIRENV` paths in a private file.
Do not store a token there. Use
`%h/.config/agentic-copier-template-update/template-update.env` here. In a
new project, use
`%h/.config/<project-slug>-template-update/template-update.env`. The service
loads the checkout's ignored `.envrc` through `direnv`. It must set the GitLab
host, project ID, base branch, factory username, and absolute Git and glab
paths. Load the token from a secure store. The template source must be public
HTTPS or fetchable through `SSH_AUTH_SOCK`; a private HTTPS token is not passed
to the template fetch. The service then runs `bin/agent-work-from-checkout`.
It omits `--apply`, so
each timer run makes a plan. You must install and enable the timer. Add
`--apply` to a local unit only when you choose to apply the update.

## Apply preconditions and effects

With `--apply`, the project root must be clean and on its default branch. The
command binds `REV` to one commit before it changes files. It holds the project
lock and saves state before it acts.

The command creates a retained branch and worktree in XDG state. In that
worktree it runs:

```text
copier update --vcs-ref RESOLVED_SHA --trust --defaults --skip-tasks --conflict rej
```

Copier keeps project answers and later edits under its update rules. The
command does not choose a revision. If files change, it runs the project's
workflow. Then the controller checks the worktree, its tree, and its paths.
It runs the installed hook and reads fresh JUnit files. It checks the branch,
base commit, and remote target. Worker text cannot authorize a commit or MR.

Once checks pass, the controller commits the tree. It pushes the kept branch.
It opens or finds one MR for human review. It never approves or merges it. On
a retry, it checks the record, branch, commit, and MR. It will not open a second
MR for the same update.

## States and recovery

The command saves state before it acts. It does not replay an unclear state.
It checks the kept worktree, branch, remote commit, and MR before a retry.

On a conflict or failed check, the command keeps the worktree and state. It
also keeps them if a remote result is unclear. The result names the next step.
The change stays in recovery until it is safe to publish.
Do not discard, reset, or stash the retained work.

## Result data

Every result has `schema_version`, `command`, `outcome`, `state`,
`current_commit`, `candidate_commit`, `changed_paths`, and `next_action`.
Retained attempts also report the branch and worktree. A conflict reports its
`conflict_paths` when Copier identifies them. A recovery result reports its
`recovery_stage`. A published result has `merge_request_iid` and
`merge_request_url` from the verified merge request.
These fields show the safe next step. Worker output is not proof of a safe
publish step.

When Copier changes files, the command runs the project's workflow on the
diff. The retained workflow record contains one worker handoff:

- `workflow_lane` and worker-reported workflow receipts
- exact `changed_paths` and the resulting `candidate_tree`
- worker-reported audit and capacity text
- proposed merge request title and description

The worker may suggest review text. It cannot commit, push, open an MR, approve,
merge, or pick a revision. The controller makes the publish decision. The
normal published CLI result omits the worker handoff.

For a code change, the worker reports `junit_artifacts` for `py311` and
`py314`. These paths and hashes are handoff data. The controller runs the hook
on the exact staged tree. It checks fresh test results for both Python
versions. A prose change has no worker JUnit data. It still needs the
controller's full check.

## Successful result

For a changed update, success means the controller published one verified
committed branch and merge request for human review. An unchanged update has
no change to publish. The command does not approve, merge, choose a candidate
revision, or change issue state.
