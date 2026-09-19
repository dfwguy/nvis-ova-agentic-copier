---
title: How to preview a template revision
type: how-to
summary: Compare a generated project's recorded template commit with an upstream candidate before updating.
audience: practitioner
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-19
related:
  - path: ../reference/template-revision-preview.md
    why: Defines the preview command, output, and exit status.
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

# How to preview a template revision

Use this before updating a generated project when you want to see which paths
changed in the upstream template repository between the project's recorded
template commit and a candidate revision.

Run the command from the generated project's root. Its default project root is
the current directory, where it reads `.copier-answers.yml` for the recorded
template origin and current revision.

```bash
uv run agent-work template preview --candidate-revision main
```

To run it from another directory, name the generated project explicitly:

```bash
uv run agent-work template preview --project-root /path/to/generated-project \
  --candidate-revision v2.4.0
```

The command prints the resolved current and candidate commits, whether they are
`changed` or `unchanged`, and any changed paths. Those paths are safe relative
paths in the upstream template repository.

This is a stateless, read-only comparison. It has no `--apply` option and does
not write project or template state. It does not run Copier, render files,
update the generated project, create a branch or worktree, or publish anything.

Do not treat the path list as a prediction of rendered-project impact. It is a
repository change list between two upstream template commits, not Copier
conflict or update analysis. Use [Update a generated project](update-a-generated-project.md)
only after you decide to perform the separate Copier update.

For JSON output suitable for a script, add `--json`:

```bash
uv run agent-work template preview --candidate-revision main --json
```

If the preview fails, the normal error is redacted. Add `--debug` only when you
need the safe stage identifier that locates the failed operation:

```bash
uv run agent-work template preview --candidate-revision main --debug
```

See the [template revision preview reference](../reference/template-revision-preview.md)
for the complete command contract, output schema, and exit statuses.
