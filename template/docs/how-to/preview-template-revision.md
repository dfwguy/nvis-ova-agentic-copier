---
title: How to preview a template revision
type: how-to
summary: Compare a saved template commit with a new revision.
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

Use this before you update a generated project. It lists paths that changed
between the recorded template commit and a candidate revision.

Run the command from the generated project's root. Its default project root is
the current directory. There it reads `.copier-answers.yml` for the recorded
template origin and current revision.

```bash
uv run agent-work template preview --candidate-revision main
```

To run it elsewhere, give the generated project path:

```bash
uv run agent-work template preview --project-root /path/to/generated-project \
  --candidate-revision v2.4.0
```

The command prints both resolved commits and an outcome: `changed` or
`unchanged`. It also prints changed paths. Each is a safe relative path in the
upstream template repository.

This is a stateless, read-only comparison. It has no `--apply` option. It does
not write project or template state. It does not run Copier. It does not render
files, update the generated project, create a branch or worktree, or publish
anything.

The path list does not predict the rendered project's impact. It lists changes
between two upstream template commits. It is not Copier conflict or update
analysis. After you decide to update, follow the [update behavior](../reference/template-contract.md#update-behavior).

For script-friendly JSON output, add `--json`:

```bash
uv run agent-work template preview --candidate-revision main --json
```

If the preview fails, the normal error is redacted. Add `--debug` only when you
need the safe stage identifier. It locates the failed operation:

```bash
uv run agent-work template preview --candidate-revision main --debug
```

See the [template revision preview reference](../reference/template-revision-preview.md)
for command details, JSON format, and exit codes.
