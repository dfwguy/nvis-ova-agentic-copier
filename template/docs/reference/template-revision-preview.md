---
title: Template revision preview
type: reference
summary: Compare the recorded template revision with a candidate revision.
audience: integrator
tags: [template]
status: current
last_reviewed: 2026-09-19
related:
  - path: ../how-to/preview-template-revision.md
    why: Shows how to compare a generated project's recorded template revision.
  - path: template-update.md
    why: Defines the guarded command that can apply a reviewed revision.
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

# Template revision preview

## Command

```text
agent-work template preview --candidate-revision REV [--project-root PATH] [--json] [--debug]
```

`--candidate-revision REV` is required. `--project-root PATH` defaults to `.`.
The command reads `<project-root>/.copier-answers.yml`. The recorded template
origin locates the template. The current revision is compared with the
candidate revision, which does not need to be newer.

This command is stateless and read-only. It has no `--apply` option. It writes
no project or template state. It does not execute Copier. It does not render or
update a generated project. It does not perform conflict recovery, create a
branch or worktree, or run publication automation.

## Output

Text output has this header, followed by one line for each changed path:

```text
outcome=<changed|unchanged> current_commit=<sha> candidate_commit=<sha> paths=<count>
path=<safe-path>
```

`current_commit` and `candidate_commit` are resolved 40-character lowercase
Git commit IDs. `outcome` is `changed` when `paths` is nonempty. Otherwise, it
is `unchanged`. Each path is a safe relative path in the upstream template
repository.

With `--json`, the command emits this compact object:

```json
{
  "schema_version": 1,
  "command": "template.preview",
  "outcome": "changed",
  "current_commit": "<40-char lowercase SHA>",
  "candidate_commit": "<40-char lowercase SHA>",
  "paths": ["<safe upstream repo path>"]
}
```

The `outcome` value is either `changed` or `unchanged`. When it is
`unchanged`, `paths` is an empty array. The object always contains every key
shown above.

The paths identify repository changes between upstream template commits. They
do not predict the rendered-project impact and are not Copier conflict or
update analysis.

## Failures and exit status

Ordinary failures are redacted. `--debug` adds only a safe stage identifier.
It does not expose untrusted input, repository details, or credentials.

| Exit status | Meaning |
| --- | --- |
| `0` | The comparison completed. Both `changed` and `unchanged` are successful outcomes. |
| `1` | The comparison could not complete. The error is redacted. |
| `2` | The command arguments or usage were invalid. |
