<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
## What This Repo Is

A [copier](https://copier.readthedocs.io/) template that makes Python projects.
`copier copy` adds UV, ruff, pytest, pre-commit, and GitLab CI/CD.

`template/` is output, not this repository's source. The serial runner is
importable production code in `src/agent_work/`. Copier contract tests are in
`tests/`.

## Factory objective and project context

This repository owns the primary objective of a running, self-improving
factory. It allows merge requests that improve that factory. This file owns
that context. `AGENTS.md` owns portable standing authority, safeguards, and
human approval and merge boundaries.

For the local recovery scenario in issue #158 and merge request !236, reject
the proposed 25-file pre-checkpoint migration. It serves a retained snapshot,
not a state the current workflow can recreate after merge. Reimplement any
still-needed behavior under the current workflow with authentic Red evidence.
Do not fabricate a Red checkpoint or weaken recovery safeguards to carry the
old snapshot forward.

## Architecture

### Template Configuration (`copier.yaml`)

The configuration has `project_name`, `project_slug`, and `description`.
Jinja2 derives `project_slug` by default. Post-generation tasks check
prerequisites. They start Git. They link Claude skills. They report readiness.
They sync dependencies. They install hooks.

### Template Content (`template/`)

Files here are copied or rendered for the generated project.

- `.jinja` files are rendered and lose that extension. For example,
  `pyproject.toml.jinja` becomes `pyproject.toml`.
- Directory and file names containing `{{ }}` are rendered as Jinja2.
- Template variables use `{{ variable }}`. They have no `cookiecutter.` prefix.

### Generated Python source (`template/src/{{ project_slug }}/`)

The Copier payload uses the standard Python `src/` layout. Importable code is
in `src/<package>/`. Generated tests are in `tests/`, outside the package.
Python console commands are in generated `pyproject.toml` under
`[project.scripts]`.

### Tests (`tests/`)

`test_template.py` calls `copier.run_copy()` with test answers. It checks the
output.

## Filesystem structure deviations

This Copier template has its payload in `template/`. It also ships the
`src/agent_work/` runner package.

- **Language:** Python
- **Rule:** importable app code lives under `src/<package>/`.
- **Actual paths:** root runner code is `src/agent_work/`. Template source is
  `template/`. Contract tests are `tests/`. Generated Python code is in
  `src/<project_slug>/` and `src/agent_work/`. Its entry points use
  `[project.scripts]`. A generated Bash app adds one `bin/<tool>`.
- **Rationale:** the root runner validates the Copier payload's serial GitLab
  issue workflow. This repository makes project files.

- **Language:** Python
- **Rule:** Python commands live below `src/<package>/`. Installed commands
  use `[project.scripts]`.
- **Actual paths:** the read-only checker is `scripts/check_licenses.py`. Its
  mirror is `template/scripts/check_licenses.py`. `tests/test_licenses.py` is
  its source behavior contract. Root and template pre-commit and CI run it.
- **Rationale:** it validates files before package installation. It is a
  read-only development/CI helper, not a user-facing app. Ephemeral diagnostics
  make no writes. This is a narrow local class, not a global FileHandler policy
  change.

- **Language:** Bash
- **Rule:** a user-facing Bash command uses one `bin/<tool>` entry point. Its
  sourceable code is in `src/` or `lib/`. Tests stay outside production.
- **Actual paths:** the systemd-only wrapper and entry point is
  `scripts/agent-work-systemd-run.sh`. Its mirror is in `template/scripts/`.
  Its service units are in `systemd/` and `template/systemd/`. Its behavior is
  tested by `tests/test_agent_work_systemd_smoke.py`. Recoverable records are below the XDG state directory.
  The project lock is there. Durable issue worktrees are too.
- **Rationale:** this is a systemd boundary, not an interactive CLI or
  sourceable library. The installed service unit uses its stable `scripts/`
  path.

## Template sync

Mirror portable shared policy changes in `template/`. Root `.agents/project.md`
is the context exception. It records this repository's owned context.
`template/.agents/project.md.jinja` renders a new project's first context with
its name, description, and package path.

## Change-lane classification

Use coding for Python, Bash, tests, CI, config, packaging, services, and Copier
tasks. Use coding for Jinja control logic and executable fenced commands. Use it
for a mixed or unsure diff. Use documentation and agent-policy only for prose,
navigation, front matter, or non-executable portable instructions.
Agent-policy changes are non-trivial, even when small.

## Documentation and agent-policy mirrors

Portable shared policy is template-owned. It includes `AGENTS.md`, instructions, skills, roles,
guidance, and docs. Root and `template/` copies are byte-identical. Root
`.agents/project.md` and `template/.agents/project.md.jinja` are the rendered
context exception. Rendered `.agents/project.md` is project-owned. Copier keeps
it through `_skip_if_exists`. It is not a portable policy mirror. Smoke-render
Copier after a template doc or policy change.

## Validation ownership

Coding owns executable behavior and tests. Documentation owns prose structure.
It owns Diátaxis metadata and navigation. It owns local links. It owns mirror
parity. It owns sync with configured behavior. Do not add tests that lock
wording. Only Bash fences operators execute are interfaces. They stay in coding.

### Copier-local maintenance synchronization exception

This repository may keep a maintenance sync check for declared root/template
mirrors. It may check only byte identity. It may check copy sync. It may check
render sync. It must never assert doc or agent-policy content. This is local,
not portable policy.

## Testing source of truth

- Python behavior, versions, and the supported matrix: `pyproject.toml` and tox.
- CI behavior: `.gitlab-ci.yml`.
- Local validation: `.pre-commit-config.yaml`.
- Bash behavior: source scripts and ShellCheck configuration.
- Shared instructions: root and `template/` byte parity.
- Copier maintenance synchronization: the content-agnostic exception above.
- Rendered project context: Copier smoke output.

## Pipeline Performance Budget

```yaml
pipeline_execution_preferred_seconds: 60
pipeline_execution_hard_limit_seconds: 120
```

Apply these limits to the longest job `duration`. Do not use the pipeline
wall-clock span. Do not use `queued_duration`. Jobs are queue-free execution
units. Runner scheduling must not reject a change. Humans control these values.
A change needs explicit human approval.

## Python Versions

- Minimum: **3.11**
- Tested: **3.11** and **3.14** (via tox)
- Avoid `3.12+` stdlib additions
