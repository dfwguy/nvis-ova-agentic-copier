# Template contract

**Diátaxis category: Reference.** This page is a factual lookup for the
template's public inputs, generated output, setup tasks, and update behavior.

## Copier inputs

The [`copier.yaml`](../../copier.yaml) configuration defines these values:

| Input | Type | Default | Used for |
| --- | --- | --- | --- |
| `project_name` | string | `My Python Project` | Display name in generated files. |
| `project_slug` | string | Derived from `project_name` by lowercasing and replacing spaces and hyphens with underscores. | Package directory and distribution name. |
| `description` | string | `A Python project generated with copier` | Project metadata and generated README text. |

## Rendering rules

The template payload is the [`template/`](../../template) directory. Copier
renders `.jinja` files and removes their extension. It also renders path names
that contain Jinja expressions. All other files are copied unchanged.

## Post-generation tasks

Copier runs these tasks only when the caller passes `--trust`:

| Order | Task | Effect |
| --- | --- | --- |
| 1 | `git init` | Creates a Git repository. |
| 2 | `uv sync` | Creates the virtual environment and installs dependencies. |
| 3 | `uv run pre-commit install` | Installs the repository's pre-commit hook. |

## Generated capabilities

| Area | Provided configuration |
| --- | --- |
| Python | Requires Python 3.11 or newer; tox tests Python 3.11 and 3.14. |
| Dependencies | UV-managed project, test, and development dependency groups. |
| Quality | Ruff linting and formatting, plus ty type checking. |
| Tests | pytest, coverage, Hypothesis, randomized test ordering, and purpose-built boundary mocking tools. |
| Hooks | YAML validation, Ruff, ty, and tox through pre-commit. |
| CI | GitLab jobs for Ruff checks, formatting, tox, YAML linting, and type checking. |
| Agent workflow | `CLAUDE.md`, `.claude/`, and `docs/diataxis.md` for architecture, TDD, documentation, review, and publication routing. |

## Update behavior

`uvx copier update --trust` reads `.copier-answers.yml` from the generated
project and performs a three-way update. The following generated files are
preserved when they already exist:

- `pyproject.toml`
- `README.md`
- `.gitlab-ci.yml`
- `.pre-commit-config.yaml`
- `.claude/project.md`
- `<project_slug>/__init__.py`

## Related pages

- Follow the [first-project tutorial](../tutorials/create-your-first-project.md).
- Use the [non-interactive creation guide](../how-to/create-a-project-non-interactively.md).
- Read [why the template is structured this way](../explanation/template-and-generated-project.md).
- Read [why the coding standards exist](../explanation/coding-standards.md).
