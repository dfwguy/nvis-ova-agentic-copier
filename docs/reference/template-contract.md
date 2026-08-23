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

## Trusted post-render tasks

Copier runs these tasks only when the caller passes `--trust`. They run after
files are rendered; Copier cannot perform this preflight before creating the
destination directory.

| Order | Task | Effect |
| --- | --- | --- |
| 1 | Prerequisite preflight | Validates required commands, Git identity, and GitLab authentication. A failure prevents all later tasks. |
| 2 | `git init` | Creates a Git repository. |
| 3 | `uv sync` | Creates the virtual environment and installs dependencies. |
| 4 | `uv run pre-commit install` | Installs the repository's pre-commit hook. |

### Preflight contract

The prerequisite preflight validates all of the following:

| Requirement | Validation |
| --- | --- |
| Required commands | `git`, `uv`, `bash`, `shellcheck`, and `glab` are available on `PATH`. |
| Git commit identity | `git config --global --get user.name` and `git config --global --get user.email` each return a nonempty value. |
| GitLab CLI authentication | `glab auth status --hostname gitlab-master.nvidia.com` succeeds. |

The preflight reports all failed requirements together and exits unsuccessfully;
`git init`, dependency synchronization, and hook installation do not run.
UV supplies the Python interpreter and the generated project's Python tools,
so Python, Ruff, pytest, tox, and pre-commit are not separate preflight
requirements.

To resolve a failure, install every missing command, configure the Git identity
values, or run `glab auth login --hostname gitlab-master.nvidia.com` as
applicable. Then re-run the trusted Copier command. Since the check runs after
rendering, remove or choose another destination directory only when Copier
requires an empty target; do not treat the rendered files as a successfully
initialized project until the preflight has passed.

## Generated capabilities

| Area | Provided configuration |
| --- | --- |
| Python | Requires Python 3.11 or newer; tox tests Python 3.11 and 3.14. |
| Dependencies | UV-managed project, test, and development dependency groups. |
| Quality | Ruff linting and formatting, plus ty type checking. |
| Tests | pytest, coverage, Hypothesis, randomized test ordering, and purpose-built boundary mocking tools. |
| Hooks | YAML validation, trailing-whitespace and TOML parsing checks, Kingfisher secret scanning, Ruff, ty, and tox through pre-commit. |
| CI | GitLab jobs for Ruff checks, formatting, tox, YAML linting, Kingfisher secret scanning, and type checking. |
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
