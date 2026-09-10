## What This Repo Is

A [copier](https://copier.readthedocs.io/) template for generating new Python projects. Running `copier copy` against this repo scaffolds a complete Python project with UV, ruff, pytest, pre-commit, and GitLab CI/CD pre-configured.

Key distinction: the `template/` directory is the **template output**, not the
source being developed here. The serial runner itself is importable production
code in `src/agent_work/`; the Copier contract tests live in `tests/`.

## Architecture

### Template Configuration (`copier.yaml`)
Three variables: `project_name`, `project_slug` (auto-derived via Jinja2 default), `description`.

Post-generation tasks validate prerequisites, initialize Git, establish the
Claude skills link, report readiness, sync dependencies, and install hooks.

### Template Content (`template/`)
Files that get copied/rendered into the generated project. Jinja2 rendering applies to:
- Files with `.jinja` extension — rendered and extension stripped (`pyproject.toml.jinja` → `pyproject.toml`)
- Directory/file names containing `{{ }}` — rendered as Jinja2

Template variables use `{{ variable }}` (no `cookiecutter.` prefix, unlike cookiecutter).

### Generated Python source (`template/src/{{ project_slug }}/`)
The Copier payload uses the standard Python `src/` layout. Generated importable
code lives below `src/<package>/`; generated tests live in `tests/` outside the
package. Python console commands are declared in `[project.scripts]` in the
generated `pyproject.toml`.

### Tests (`tests/`)
`test_template.py` validates template generation by calling `copier.run_copy()` with test answers and inspecting the output.

## Filesystem structure deviations

This repository is a Copier template, whose generated-project payload is under
`template/`, and it also ships the `src/agent_work/` runner package.

- **Language:** Python
- **Rule:** importable application code lives under `src/<package>/`.
- **Actual paths:** root runner code is at `src/agent_work/`; template source
  is under `template/`, and contract tests are under `tests/`. Generated
  Python code is at `src/<project_slug>/` and `src/agent_work/`; generated
  Python entry points use
  `[project.scripts]`, and generated Bash projects add a single `bin/<tool>`
  only when they introduce a Bash application.
- **Rationale:** the root runner validates the Copier payload's serial GitLab
  issue workflow while the repository continues to produce project files.

- **Language:** Bash
- **Rule:** user-facing Bash commands use one `bin/<tool>` entry point, with
  sourceable implementation in `src/` or `lib/` and tests outside production.
- **Actual paths:** the systemd-only wrapper and entry point is
  `scripts/agent-work-systemd-run.sh`, mirrored in `template/scripts/`; its
  service units are in `systemd/` and `template/systemd/`, and its behavior is
  tested from `tests/test_template.py`. Recoverable records, the project lock,
  and durable issue worktrees live below the XDG state directory.
- **Rationale:** this wrapper is a systemd integration boundary, not an
  interactive user CLI or sourceable library. Its stable `scripts/` path is
  referenced by the installed service unit.

## Template sync

Mirror shared workflow changes in `template/`. The project notes are the one
intentional difference: the template renders `.agents/project.md.jinja` with
the generated project's name, description, and package path.

## Change-lane classification

Use the coding workflow for changes to Python, Bash, tests, CI, configuration,
packaging, services, Copier tasks, Jinja control logic, executable fenced
commands, or any mixed or uncertain diff. Use the documentation and
agent-policy workflow only when the whole change is prose, navigation, front
matter, or non-executable portable instructions. Agent-policy changes are
non-trivial even when small.

## Documentation and agent-policy mirrors

Shared workflow instructions, skills, roles, guidance, and documentation have
byte-identical paths below the repository root and `template/`. The sole
rendered exception is root `.agents/project.md` versus
`template/.agents/project.md.jinja`. Smoke-render the Copier template after a
template documentation or policy change.

## Validation ownership

The coding workflow owns executable behavior and its tests. The documentation
workflow owns prose structure, Diátaxis metadata and navigation, local links,
mirror parity, and synchronization with configured behavior. Do not add tests
that merely lock down wording. Bash fenced snippets that operators execute are
interfaces and remain in the coding workflow.

## Testing source of truth

- Python behavior, versions, and the supported matrix: `pyproject.toml` and tox.
- CI behavior: `.gitlab-ci.yml`.
- Local validation: `.pre-commit-config.yaml`.
- Bash behavior: source scripts and ShellCheck configuration.
- Shared instructions: root and `template/` byte parity.
- Rendered project context: Copier smoke output.

## Pipeline Performance Budget

```yaml
pipeline_execution_preferred_seconds: 60
pipeline_execution_hard_limit_seconds: 120
```

Apply these limits to the longest individual job `duration`, not the pipeline
wall-clock span or any `queued_duration`. The jobs are the queue-free units of
repository execution; runner scheduling must not reject a change. These values
are human-controlled and require explicit human approval to change.

## Python Versions

- Minimum: **3.11**
- Tested: **3.11** and **3.14** (via tox)
- Avoid `3.12+` stdlib additions
