# Create your first project

**Diátaxis category: Tutorial.** This lesson creates a working Python project
from the template and confirms that its development tooling is ready.

## Before you begin

Install [UV](https://docs.astral.sh/uv/getting-started/installation/) and Git.
Install and authenticate [`glab`](https://gitlab.com/gitlab-org/cli) as well if
you will use the generated project's GitLab workflow.

## Generate a project

Run Copier through `uvx`, giving the new project a destination directory:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
```

Answer the prompts with a project name, an importable package name, and a
short description. `--trust` is deliberate: the template runs `git init`,
`uv sync`, and `uv run pre-commit install` after rendering. Review those tasks
in [`copier.yaml`](../../copier.yaml) before trusting a template revision.

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
