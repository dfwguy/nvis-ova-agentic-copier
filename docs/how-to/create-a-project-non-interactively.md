---
title: How to create a project non-interactively
type: how-to
summary: Make a project.
audience: practitioner
tags: [project-generation]
status: current
last_reviewed: 2026-08-27
related:
  - path: ../tutorials/create-your-first-project.md
    why: Introduces the interactive path before automation.
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

# How to create a project non-interactively

Use this guide to run Copier from a script. Give the command every answer. Run
it where you can create the target directory. Keep the answers in your script.
Then each run uses the same project details.

Pass values with `--data` and give Copier a target directory:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git order-service \
  --data project_name="Order Service" \
  --data project_slug="order_service" \
  --data description="Order ingestion pipeline"
```

The final positional value is the target directory. In this example, Copier
creates `order-service`. Quote values that contain spaces. Use a unique target
directory for each CI job. Do not reuse a directory with files that the job
must keep.

Keep `--trust` because the template has post-generation tasks. When Copier
finishes, check the generated project:

```bash
cd order-service
uv run pytest
```

Run the check from the generated project's root. Fix a failed check there
before treating the generated project as ready. Keep the command output with
the CI job when the project is generated in automation.

Choose an importable Python identifier for `project_slug`. It names the package
directory and distribution. See the [template reference](../reference/template-contract.md#copier-inputs)
for the full input contract.
