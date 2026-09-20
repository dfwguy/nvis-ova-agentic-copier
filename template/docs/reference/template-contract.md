---
title: Template contract
type: reference
summary: Look up template inputs, generated files, setup tasks, and update behavior.
audience: integrator
tags: [template]
status: current
last_reviewed: 2026-09-20
related:
  - path: ../tutorials/create-your-first-project.md
    why: Uses the generated project behavior defined here.
  - path: ../how-to/create-a-project-non-interactively.md
    why: Supplies these inputs without interactive prompts.
  - path: ../how-to/customize-a-generated-project.md
    why: Applies the ownership boundaries described here.
  - path: ../how-to/update-a-generated-project.md
    why: Uses the update behavior defined here.
  - path: ../how-to/validate-template-changes.md
    why: Verifies the contract described here.
  - path: ../how-to/refresh-ci-image.md
    why: Maintains root validation images without changing generated-project portability.
  - path: ../explanation/template-and-generated-project.md
    why: Explains the template boundary behind this contract.
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Template contract

This page is a factual lookup for the
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
that contain Jinja expressions. The guarded post-render task establishes the
relative `.claude/skills -> ../.agents/skills` compatibility link and removes
its transient `.claude/project.md` migration marker on new copies. Existing
legacy context at that path is preserved and reported. All remaining files are
copied unchanged.

## Trusted post-render tasks

Copier runs these tasks only when the caller passes `--trust`. They run after
files are rendered; Copier cannot perform this preflight before creating the
destination directory.

| Order | Task | Effect |
| --- | --- | --- |
| 1 | Command preflight | Validates required commands. A failure prevents all later tasks. |
| 2 | `git init` | Creates a Git repository. |
| 3 | Frontend migration | Maintains the skills link, removes the new-copy project-context marker, and reports preserved legacy context. |
| 4 | Readiness diagnostics | Checks the Git identity resolved in the new repository and authenticated GitLab API access. Diagnostics do not prevent later setup tasks. |
| 5 | `uv sync` | Creates the virtual environment and installs dependencies. |
| 6 | `uv run pre-commit install` | Installs the repository's pre-commit hook. |

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
| Python | Requires Python 3.11 or newer. Local tox runs test Python 3.11 and 3.14; CI creates locked Python 3.11 and 3.14.3 environments. |
| Dependencies | UV-managed project, test, and development dependency groups. |
| Quality | Ruff linting and formatting, plus ty type checking. |
| Tests | pytest, coverage, Hypothesis, randomized test ordering, and purpose-built boundary mocking tools. |
| Hooks | YAML validation, trailing-whitespace and TOML parsing checks, Bash syntax and ShellCheck, Kingfisher secret scanning, Ruff, ty, and tox through pre-commit. |
| CI | Root CI builds and pushes `$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID` from the current `Dockerfile`, `pyproject.toml`, and `uv.lock` in every merge-request and default-branch pipeline. Root validation retains the image-build job's BuildKit metadata and digest as build evidence, then its nine native jobs—`ruff-check`, `ruff-format`, `pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`, `license-check`, and `shellcheck`—wait for and run in that pipeline-specific candidate. The image supplies the interpreter, system tools, and locked dependencies, so root jobs retain `uv run --no-sync` and perform no `apt-get`, `uv python install`, or `uv sync` bootstrap. The image build uses registry-backed cache within the 60-second preferred and 120-second hard per-job duration budget. Each root pytest job runs four deterministic concurrent shards: one complete `tests/test_agent_work_serial_runtime.py` shard, one complete `tests/test_template.py` shard, and two exhaustive, non-overlapping partitions of the remaining suite. The remaining-suite shards use `tests.helpers.serial_runtime_shard` with indices 0 and 1 of 2 and ignore both dedicated files. The job waits for every shard PID and publishes a JUnit report for each before combining their coverage data. The combined data measures only maintained `src/agent_work` source, enforces the existing 85% floor, and publishes one per-version Cobertura report. A failure in any shard or in coverage aggregation, floor enforcement, or report generation fails its named pytest job. There is no synthetic aggregate root validation job or aggregate JUnit gate suite; each check is investigated through its named job. In each configuration, shared `.base_uv` setup adds only the current `$CI_PROJECT_DIR` checkout to Git's safe-directory list. It runs in the ephemeral validation container, with no wildcard path or host Git configuration. The generated template remains registry-independent: its portable `validation (9 gates)` job unconditionally runs `uv sync --locked --all-groups` from its checkout's `pyproject.toml` and `uv.lock` for both Python environments before `uv run --no-sync` validation. It retains its aggregate gate suite and separate pytest reports. Local tox remains the supported-matrix runner outside CI. Both configurations run pipelines only for merge-request events and the default branch; feature-branch pushes without an MR do not start validation. |
| Agent workflow | `AGENTS.md` is the portable bootstrap; the live `$architect` router in `.agents/agents/architect.md` defines architecture, TDD, documentation, review, and final decisions. Skills own phase procedure, `$git-flow` owns publication, and `$work-item-runner` owns the serial exception. `.agents/` is the only workflow tree. `CLAUDE.md` imports `AGENTS.md`, a guarded task maintains the `.claude/skills` link to `.agents/skills`, and minimal Claude settings deny approval and merge commands. |

## Generated filesystem layout

Generated Python projects use a `src` layout. Importable application code lives
at `src/<project_slug>/`; tests live outside the production package. Packaging
metadata and Python command-line entry points belong in `pyproject.toml`.
Python console commands are declared in `[project.scripts]` and delegate into
the installed package.

When a generated project includes Bash, it has one user-facing executable in
`bin/<tool>`. Bash implementation or library files live in a separate `src/`
or `lib/` directory, and tests remain outside production code. Clearly labeled
development and CI helpers are not public CLI entry points and do not belong in
`bin/`.

The filesystem-structure gate reviews these defaults before finalization. A
project may depart from them only when `.agents/project.md` records the affected
language, the standard rule being departed from, the actual source and
entry-point paths, and the compatibility or operational rationale. The default
generated project records the systemd wrapper's intentional `scripts/` path.

## Update behavior

`uvx copier update --trust` reads `.copier-answers.yml` from the generated
project and performs a three-way update. The following generated files are
preserved when they already exist:

- `pyproject.toml`
- `README.md`
- `.gitlab-ci.yml`
- `.pre-commit-config.yaml`
- `.agents/project.md`
- legacy `.claude/project.md`
- `src/<project_slug>/__init__.py`

`pyproject.toml` and `.agents/project.md` are project-specific and remain
preserved. The legacy `.claude/project.md` is also kept so an existing
project's context is never discarded during the move: sessions read it until
the owner reconciles it into `.agents/project.md` and removes the old file. A
legacy flat-package project must likewise update its own package path. Copier
does not overwrite project-owned files to perform either migration.

The compatibility link is template-owned: after an update,
`.claude/skills` should be the relative link `../.agents/skills`. Copier keeps
that link when it already exists. For an older copied directory, it compares
`.claude/skills` and `.agents/skills` with `git diff --no-index` and replaces
only identical trees. A different directory or link remains intact while the
task reports how to continue. Preserve or reconcile the local path, then rerun
Copier; do not remove it blindly.

## Related pages

- Follow the [first-project tutorial](../tutorials/create-your-first-project.md).
- Use the [non-interactive creation guide](../how-to/create-a-project-non-interactively.md).
- Read [why the template is structured this way](../explanation/template-and-generated-project.md).
- Read [why the agent workflow uses specialized roles](../explanation/agentic-workflow.md).
- Read [why the coding standards exist](../explanation/coding-standards.md).
