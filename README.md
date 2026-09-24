<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# OVA Agentic Flow copier

A [Copier](https://copier.readthedocs.io/) template for Python projects.
Use it to start a UV-managed Python project. It adds tools and docs.
It adds a portable, human-gated workflow. Claude Code, Codex, and Pi use it.

## Quick start

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git,
and [`glab`](https://gitlab.com/gitlab-org/cli). Then create and test a project:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
cd my-project
uv run pytest
```

`--trust` lets Copier run the listed post-render tasks.
For a first run, see [Create your first project](docs/tutorials/create-your-first-project.md).
The [template contract](docs/reference/template-contract.md) lists inputs,
tasks, output, and updates.

## Set up the agent loop

From this repository checkout, ask Codex to guide the agent loop. The loop is
recoverable and serial. It uses GitLab.

```text
Set up and manage the recoverable serial GitLab agent loop for this repository.
Follow this repository's instructions and the linked loop setup guide. Check
the prerequisites and safeguards, and surface any human decisions or blockers.
```

The [loop setup guide](docs/how-to/setup-serial-gitlab-agent-loop.md) is the
source of truth. It covers setup and use.

## Learn more

The [documentation index](docs/index.md) serves tutorial, how-to, reference,
and explanation readers. Template contributors should
[validate template changes](docs/how-to/validate-template-changes.md).
Generated-project maintainers can
[preview a template revision](docs/how-to/preview-template-revision.md) before
they decide whether to update. [How to update a generated
project](docs/how-to/update-a-generated-project.md) explains how applying a
reviewed revision that changes files opens an MR for human review.
