---
title: Template contract
type: reference
summary: Look up template inputs, output, setup, and updates.
audience: integrator
tags: [template]
status: current
last_reviewed: 2026-09-24
related:
  - path: ../how-to/validate-template-changes.md
    why: Verifies the contract described here.
  - path: ../how-to/refresh-ci-image.md
    why: Maintains root validation images without changing generated-project portability.
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

Use this page to look up template inputs, output, setup, and update rules.

## Copier inputs

The upstream `copier.yaml` configuration defines these inputs.

| Input | Type | Default | Used for |
| --- | --- | --- | --- |
| `project_name` | string | `My Python Project` | Display name in generated files. |
| `project_slug` | string | Derived from `project_name` by lowercasing and replacing spaces and hyphens with underscores. | Package directory and distribution name. |
| `description` | string | `A Python project generated with copier` | Project metadata and generated README text. |

## Rendering rules

Copier uses the upstream `template/` directory as its payload. It
renders `.jinja` files and drops the extension. It also renders path names
with Jinja expressions. The guarded post-render task makes the relative
`.claude/skills -> ../.agents/skills` link. On a new copy, it drops the
`.claude/project.md` move marker. It keeps and reports old context at that
path. Copier copies all other files unchanged.

## Trusted post-render tasks

Pass `--trust` to run these tasks after rendering. Copier makes the output
directory before it runs the preflight.

| Order | Task | Effect |
| --- | --- | --- |
| 1 | Command preflight | Validates required commands. A failure prevents all later tasks. |
| 2 | `git init` | Creates a Git repository. |
| 3 | Frontend migration | Maintains the skills link, removes the new-copy project-context marker, and reports preserved legacy context. |
| 4 | Readiness diagnostics | Checks the Git identity resolved in the new repository and authenticated GitLab API access. Diagnostics do not prevent later setup tasks. |
| 5 | `uv sync` | Creates the virtual environment and installs dependencies. |
| 6 | `uv run pre-commit install` | Installs the repository's pre-commit hook. |

### Preflight contract

The command preflight checks this requirement:

| Requirement | Validation |
| --- | --- |
| Required commands | `git`, `uv`, and `glab` are available on `PATH`. |

| Preflight result | Rule |
| --- | --- |
| Missing commands | Report all missing commands together and exit unsuccessfully. Do not run `git init`, dependency synchronization, or hook installation. |
| UV tools | UV supplies the Python interpreter and the generated project's Python tools. Python, Ruff, pytest, tox, and pre-commit are not separate preflight requirements. |

After `git init`, readiness diagnostics have these rules:

| Check | Rule |
| --- | --- |
| Git identity | Run `git var GIT_AUTHOR_IDENT` in the generated repository. Identity can come from repository configuration or standard Git identity environment variables. Directory-scoped `direnv` is supported; global Git configuration is not required. |
| GitLab access | Make an authenticated `glab api user` request to `${GITLAB_HOST:-gitlab-master.nvidia.com}`. The request accepts environment credentials given to `glab`, such as a directory-scoped `GITLAB_TOKEN`; it does not use `glab auth status`. |
| Result | Diagnostics do not block dependency setup or hook installation. Fix them before committing or using GitLab. |

| Bash validation | Rule |
| --- | --- |
| Requirements | Bash and ShellCheck are needed for generated Bash validation, not Copier generation. |
| Destination | The command check runs after rendering. Remove or change a destination directory only when Copier needs an empty target. Rendered files are not an initialized project until the command preflight passes. |

## Generated capabilities

| Area | Provided configuration |
| --- | --- |
| Python | Requires Python 3.11 or newer. Local tox runs test Python 3.11 and 3.14; CI creates locked Python 3.11 and 3.14.3 environments. |
| Dependencies | UV-managed project, test, and development dependency groups. |
| Quality | Ruff linting and formatting, plus ty type checking. |
| Tests | pytest, coverage, Hypothesis, randomized test ordering, and purpose-built boundary mocking tools. |
| Hooks | YAML validation, trailing-whitespace and TOML parsing checks, Bash syntax and ShellCheck, Kingfisher secret scanning, Ruff, ty, and tox through pre-commit. |
| CI | Root CI builds three candidates from the current `Dockerfile`, `pyproject.toml`, and `uv.lock`. The `tooling` target installs only the locked four-tool `tooling` group and pinned Git, jq, and ShellCheck. It serves `ruff-check`, `ruff-format`, `yamllint`, `kingfisher`, `readability-check`, and `shellcheck`. Local development includes test, tooling, and typing dependencies, pre-commit, and tox. The `pytest-py311` and `pytest-py314` targets install pinned Git and jq, the locked project defaults, and the `test` group. Python 3.11 also installs the `typing` group with ty and textstat and serves `type-check` and `license-check`. Python 3.11 serves 17 pytest shards; Python 3.14 serves six. Each target also serves its matching coverage job. Candidate tags, caches, metadata, digest environment files, and image variables are target-specific. Tags use `validation-<target>:$CI_COMMIT_SHA-$CI_PIPELINE_ID`; caches use `validation-<target>-cache`. Build artifacts prove each target's metadata and immutable digest. Jobs perform no `apt-get`, `uv python install`, or `uv sync` bootstrap. `readability-check` uses `uv run --no-sync` with the checkout `src` directory on `PYTHONPATH`; it does not install dependencies at runtime. The tooling target alone contains the stable precompiled Kingfisher rule cache; the runtime scan contract does not change. The split is intended to reduce unrelated work, but it does not guarantee a run time. The 60-second queue-free preference and 120-second hard per-job limit remain measured thresholds. Each Python version's shards form one deterministic, exhaustive full-suite partition and publish separate JUnit and raw coverage files. `coverage-py311` combines exactly 17 files; `coverage-py314` combines exactly six. Both aggregation jobs measure only `src/agent_work`, enforce the 85% floor, and publish Cobertura. The root remains a simple DAG with no synthetic aggregate validation job or aggregate JUnit gate suite. Root jobs trust only the current `$CI_PROJECT_DIR` in their temporary container. The generated template remains registry-independent: its portable `validation (10 gates)` job still runs `uv sync --locked --all-groups` for both Python environments before `uv run --no-sync` validation. It retains the same aggregate gate suite and separate pytest reports. Local tox remains the supported-matrix runner. Both CI configurations run only for merge-request events and the default branch. |
| Agent workflow | `AGENTS.md` is the portable bootstrap; the live `$architect` router in `.agents/agents/architect.md` defines architecture, TDD, documentation, review, and final decisions. Skills define the procedure for each workflow stage, `$git-flow` owns publication, and `$work-item-runner` owns the serial exception. `.agents/` is the only workflow tree. `CLAUDE.md` imports `AGENTS.md`, a guarded task maintains the `.claude/skills` link to `.agents/skills`, and minimal Claude settings deny approval and merge commands. |

## Generated filesystem layout

| Area | Layout rule |
| --- | --- |
| Python | Generated Python projects use a `src` layout. Importable application code lives at `src/<project_slug>/`. Tests live outside the production package. Packaging metadata and Python command-line entry points belong in `pyproject.toml`. Python console commands use `[project.scripts]` and delegate into the installed package. |
| Bash | A generated project with Bash has one user-facing executable in `bin/<tool>`. Bash implementation and library files live in `src/` or `lib/`. Tests stay outside production code. Clearly labeled development and CI helpers are not public CLI entry points and do not belong in `bin/`. |
| Departure | The filesystem-structure gate checks these defaults before finalization. A project can depart only if `.agents/project.md` records the affected language, its standard rule, the actual source and entry-point paths, and the compatibility or operational rationale. The default generated project records the systemd wrapper's intentional `scripts/` path. |

## Update behavior

`uvx copier update --trust` uses `.copier-answers.yml` and performs a
three-way update. It keeps these generated files when they already exist:

- `pyproject.toml`
- `README.md`
- `.gitlab-ci.yml`
- `.pre-commit-config.yaml`
- `.agents/project.md`
- legacy `.claude/project.md`
- `src/<project_slug>/__init__.py`

`pyproject.toml` and `.agents/project.md` are project-owned. Copier also keeps
old `.claude/project.md` during the move. Sessions use it until its owner moves
its context to `.agents/project.md`, then removes the old file. An old
flat-package project must move its package path. Copier does not overwrite
project-owned files in either move.

The template owns the skills link. After an update, `.claude/skills` should be
the relative link `../.agents/skills`. Copier keeps an existing link. For an
old copy, it compares `.claude/skills` with `.agents/skills` using
`git diff --no-index`. It replaces only matching trees. A different directory
or link stays in place. The task reports the next step. Keep or align the local
path, then rerun Copier. Do not remove it blindly.

## Related pages

- [Preview a template revision](../how-to/preview-template-revision.md) before deciding to update.
- Look up the [template revision preview](template-revision-preview.md) command and output.
- [Validate template changes](../how-to/validate-template-changes.md) before opening a merge request.
- [Refresh the CI validation images](../how-to/refresh-ci-image.md) after changing their inputs.
- Read [why the agent workflow uses specialized roles](../explanation/agentic-workflow.md).
