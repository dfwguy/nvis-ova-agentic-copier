---
title: Create your first project
type: tutorial
summary: Generate a project and verify its development tooling is ready.
audience: new-user
tags: [project-generation]
status: current
last_reviewed: 2026-08-27
related:
  - path: ../how-to/create-a-project-non-interactively.md
    why: Provides the automation-oriented alternative to this lesson.
  - path: ../reference/template-contract.md
    why: Lists the generated files and setup behavior used here.
---

# Create your first project

This lesson creates a working Python project
from the template and confirms that its development tooling is ready.

## Before you begin

Install [UV](https://docs.astral.sh/uv/getting-started/installation/), Git,
and [`glab`](https://gitlab.com/gitlab-org/cli). Copier requires all three
commands to generate a project.

Before making a commit, ensure Git can resolve an identity in the generated
project. You may configure it in the repository or through directory-scoped
environment variables (for example, `direnv`); a global Git identity is not
required. Before using the GitLab workflow, ensure `glab` has credentials for
`${GITLAB_HOST:-gitlab-master.nvidia.com}`. A directory-scoped `GITLAB_TOKEN`
is supported.

Bash and [ShellCheck](https://www.shellcheck.net/) are only needed when you
run the generated Bash validation; they do not prevent project generation.

You do not need to install Python, Ruff, pytest, or the other project tools
separately. UV provides the project interpreter and tools.

## Generate a project

Run Copier through `uvx`, giving the new project a destination directory:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
```

Answer the prompts with a project name, an importable package name, and a
short description. `--trust` is deliberate: the template runs its declared
tasks after rendering. The command preflight checks that `git`, `uv`, and
`glab` are available. Only when it passes does Copier run `git init`. It then
runs `git var GIT_AUTHOR_IDENT` in the new repository and makes an
authenticated `glab api user` request to
`${GITLAB_HOST:-gitlab-master.nvidia.com}`; those readiness diagnostics do not
block `uv sync` or `uv run pre-commit install`. Review those tasks in
[`copier.yaml`](../../copier.yaml) before trusting a template revision.

The check is a post-render task, so Copier has already created the destination
directory when it runs. If it reports a missing command, install it and run
the command again. Correct a Git identity or GitLab-access diagnostic before
performing its corresponding action. Use a new or empty destination directory
if Copier does not accept the existing one.

## Inspect the generated project

Move into the new directory and run its fast test suite:

```bash
cd my-project
uv run pytest
```

You now have a package directory named from your chosen slug, a starter test,
and an installed virtual environment. The project also includes Ruff, ty,
tox, pre-commit, GitLab CI, and the agent workflow under `.claude/`.

## Run the full verification matrix

Run every configured Python-version test environment:

```bash
uv run tox
```

Use this command before publishing substantive changes. It verifies the same
test matrix that the generated GitLab CI configuration runs.

## What you learned

You generated a project, allowed its explicitly declared setup tasks, and
verified both its fast local test path and full test matrix. For a precise list
of what was generated, see the [template reference](../reference/template-contract.md).
For a command-oriented variation, see [create a project non-interactively](../how-to/create-a-project-non-interactively.md).
