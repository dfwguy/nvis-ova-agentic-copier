# breese-copier

A [copier](https://copier.readthedocs.io/) template for generating new Python projects with UV, ruff, pytest, pre-commit, and GitLab CI/CD pre-configured.

## Usage

```bash
copier copy gh:breese/breese-copier my-project
```

## Template Variables

| Variable | Description | Default |
|---|---|---|
| `project_name` | Human-readable project name | `My Python Project` |
| `project_slug` | Python package name | auto-derived from `project_name` |
| `description` | Short project description | `A Python project generated with copier` |

## Post-Generation Tasks

After generating a project, copier automatically runs:

1. `git init` — initialize git repository
2. `uv sync` — create virtualenv and install dependencies
3. `uv run pre-commit install` — install pre-commit hooks

## Development

```bash
# Run tests
uv run tox                  # All tests across Python versions
uv run pytest               # Run pytest directly (faster iteration)

# Lint and format
uv run ruff check --fix
uv run ruff format
```
