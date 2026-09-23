---
title: Template update
type: reference
summary: Look up the guarded template update command and its recovery states.
audience: integrator
tags: [template]
status: current
last_reviewed: 2026-09-23
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

This page defines the guarded template update command.

## Command

```text
agent-work template update --candidate-revision REV [--project-root PATH] [--json] [--debug] [--apply]
```

`--candidate-revision REV` is required. `--project-root PATH` defaults to `.`.
`--json` prints machine-readable data. `--debug` prints a traceback for local
diagnosis. `--apply` permits an update. Without it, the command plans only.

The plan uses the existing metadata comparison and makes no changes. It does
not run Copier, create a worktree or branch, or change project state.

## Apply preconditions and effects

With `--apply`, the project root must be a clean generated checkout on its
configured default branch. The command resolves `REV` to one exact commit
before it changes anything. It uses the factory project lock and saves durable
state before each side effect.

The command creates a retained branch and worktree in XDG state. In that
worktree it runs:

```text
copier update --vcs-ref RESOLVED_SHA --trust --defaults --skip-tasks --conflict rej
```

Copier keeps project answers and later edits under its update rules. The
command never chooses a candidate revision.

## States and recovery

The command saves `planned`, `prepared`, `applying`, `workflow`, `ready`,
`conflict`, or `needs-recovery` before its matching side effect. It never
replays a `prepared`, `applying`, `workflow`, `conflict`, or `needs-recovery`
record on its own.

On a Copier conflict or stop, the command keeps the worktree. Its safe result
names the recovery state, kept paths, and next action. Follow that action in
the kept worktree. Do not discard, reset, or stash it.

## Result data

Every result has `schema_version`, `command`, `outcome`, `state`,
`current_commit`, `candidate_commit`, `changed_paths`, and `next_action`.
Retained attempts also report the branch and worktree. A conflict reports its
`conflict_paths` when Copier identifies them. A recovery result reports its
`recovery_stage`. These fields keep the evidence needed to continue safely
instead of replaying the attempt.

When Copier changes files, the command asks the generated project to route the
actual diff through its workflow. A ready result then includes one completion:

- `workflow_lane` and its required workflow receipts
- exact `changed_paths` and the resulting `candidate_tree`
- an `audit_receipt` and `capacity_receipt`
- `merge_request_draft` metadata with a non-empty `title` and `description`

The worker supplies draft metadata only. It cannot commit, push, create or
update a merge request, approve, merge, schedule work, choose a revision, or
change issue state.

For a coding-lane completion, `junit_artifacts` must contain immutable,
hash-verified snapshots for both `py311` and `py314`. Each snapshot records its
absolute path, SHA-256 digest, and test counts. The snapshots must match the
candidate tree and contain no failures, errors, or skips. A content-lane
completion has no JUnit artifacts.

## Successful result

For a changed update, the command runs the generated project's needed workflow
in the worktree. It checks exact changed paths and the completion evidence.
Success returns merge-request draft data for human publication.

The command does not commit, push, create a merge request, approve, merge,
schedule work, choose a candidate revision, or change issue state. Publishing
and scheduling are separate work.
