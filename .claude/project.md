## What This Repo Is

A [copier](https://copier.readthedocs.io/) template for generating new Python projects. Running `copier copy` against this repo scaffolds a complete Python project with UV, ruff, pytest, pre-commit, and GitLab CI/CD pre-configured.

Key distinction: the `template/` directory is the **template output**, not the source being developed here. The source code lives in `tests/`.

## Architecture

### Template Configuration (`copier.yaml`)
Three variables: `project_name`, `project_slug` (auto-derived via Jinja2 default), `description`.

Post-generation tasks (replaces cookiecutter hooks): `git init`, `uv sync`, `uv run pre-commit install`.

### Template Content (`template/`)
Files that get copied/rendered into the generated project. Jinja2 rendering applies to:
- Files with `.jinja` extension — rendered and extension stripped (`pyproject.toml.jinja` → `pyproject.toml`)
- Directory/file names containing `{{ }}` — rendered as Jinja2

Template variables use `{{ variable }}` (no `cookiecutter.` prefix, unlike cookiecutter).

### Tests (`tests/`)
`test_template.py` validates template generation by calling `copier.run_copy()` with test answers and inspecting the output.

## File Sync Notes

`CLAUDE.md` (root) and `template/CLAUDE.md` (template) are separate files. `template/CLAUDE.md` is what gets copied into generated projects.

## Python Versions

- Minimum: **3.11**
- Tested: **3.11** and **3.14** (via tox)
- Avoid `3.12+` stdlib additions
