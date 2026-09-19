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

A [Copier](https://copier.readthedocs.io/) template for new Python projects.
It creates a UV-managed Python project with development tooling, documentation,
and a portable human-gated workflow for Claude Code, Codex, and Pi.

## Quick start

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git,
and [`glab`](https://gitlab.com/gitlab-org/cli), then generate and test a
project:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
cd my-project
uv run pytest
```

`--trust` permits Copier to run this template's declared post-render tasks.
For a guided first run, see [Create your first
project](docs/tutorials/create-your-first-project.md). The [template
contract](docs/reference/template-contract.md) describes inputs, tasks, output,
and updates.

## Set up the agent loop

From this repository checkout, ask Codex to guide the setup and ongoing
management of the recoverable serial GitLab agent loop:

```text
Set up and manage the recoverable serial GitLab agent loop for this repository.
Follow this repository's instructions and the linked loop setup guide. Check
the prerequisites and safeguards, and surface any human decisions or blockers.
```

The [loop setup guide](docs/how-to/setup-serial-gitlab-agent-loop.md) is the
detailed procedure and source of truth for installation and operation.

## Learn more

The [documentation index](docs/index.md) organizes tutorials, how-to guides,
reference, and explanations. Template contributors should use [Validate
template changes](docs/how-to/validate-template-changes.md). Generated-project
maintainers can [preview a template revision](docs/how-to/preview-template-revision.md)
before deciding whether to update.
