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
| 1 | Command preflight | Validates required commands. A failure prevents all later tasks. |
| 2 | `git init` | Creates a Git repository. |
| 3 | Readiness diagnostics | Checks the Git identity resolved in the new repository and authenticated GitLab API access. Diagnostics do not prevent later setup tasks. |
| 4 | `uv sync` | Creates the virtual environment and installs dependencies. |
| 5 | `uv run pre-commit install` | Installs the repository's pre-commit hook. |

### Preflight contract

The command preflight validates all of the following:

| Requirement | Validation |
| --- | --- |
| Required commands | `git`, `uv`, and `glab` are available on `PATH`. |

The preflight reports all missing commands together and exits unsuccessfully;
`git init`, dependency synchronization, and hook installation do not run.
UV supplies the Python interpreter and the generated project's Python tools,
so Python, Ruff, pytest, tox, and pre-commit are not separate preflight
requirements.

After `git init`, readiness diagnostics run `git var GIT_AUTHOR_IDENT` in the
generated repository and make an authenticated `glab api user` request to
`${GITLAB_HOST:-gitlab-master.nvidia.com}`. Git identity may come from
repository configuration or the standard Git identity environment variables,
so a directory-scoped `direnv` configuration is supported; global Git
configuration is not required. The GitLab request accepts credentials supplied
to `glab` by the environment, such as a directory-scoped `GITLAB_TOKEN`; it
does not rely on `glab auth status`. These diagnostics do not block dependency
setup or hook installation, but must be resolved before committing or using
the generated GitLab workflow.

Bash and ShellCheck are required when running the generated Bash validation,
but are not Copier generation requirements. Since the command check runs after
rendering, remove or choose another destination directory only when Copier
requires an empty target; do not treat the rendered files as a successfully
initialized project until the command preflight has passed.

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
