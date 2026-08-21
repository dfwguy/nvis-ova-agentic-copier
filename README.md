# OVA Agentic Flow copier

A [Copier](https://copier.readthedocs.io/) template that scaffolds new Python projects with a modern toolchain (UV, Ruff, ty, pytest, tox, pre-commit, GitLab CI) **and** a multi-agent Claude Code workflow wired in from day one.

If you're starting a new Python project on the team, this is the starting point. It saves you a day of boilerplate setup and gives you a TDD-enforced workflow with quality and CI gates pre-configured.

## What you get

Running this template produces a Python project that is ready to develop in immediately:

- **[UV](https://docs.astral.sh/uv/)** for dependency management and virtualenv (`uv sync`, `uv run …`).
- **[Ruff](https://docs.astral.sh/ruff/)** for linting and formatting, configured with `select = ["ALL"]` and curated exceptions.
- **[ty](https://github.com/astral-sh/ty)** for type checking (Astral's fast type checker).
- **[pytest](https://docs.pytest.org/)** with `pytest-cov`, `pytest-mock`, `pytest-subprocess`, `pytest-timeout`, and `requests-mock`.
- **[tox](https://tox.wiki/)** with `tox-uv`, configured to run tests against **Python 3.11 and 3.14**.
- **[pre-commit](https://pre-commit.com/)** hooks: ruff (check + format), [yamllint](https://yamllint.readthedocs.io/), ty, tox.
- **GitLab CI** ([`.gitlab-ci.yml`](template/.gitlab-ci.yml)) with jobs for `ruff_check`, `ruff_format`, `pytest` (tox), `yamllint`, and `type_check`. Code quality and JUnit reports are wired into GitLab's UI.
- **Claude Code agentic workflow** — `.claude/agents/`, `.claude/commands/`, and a `CLAUDE.md` that routes feature work through architect → red → green → judges → finalizer (see [Agentic workflow](#agentic-workflow) below).
- **Coding conventions** baked into `CLAUDE.md`: pathlib over `os`, dataclasses, type annotations, docstrings on public APIs, functional style, `pytest.mark.parametrize` over duplication, no internal mocking, and safe-by-default state-mutating scripts (no changes without an explicit `--apply` flag).

## Prerequisites

- **UV** — install per [UV installation docs](https://docs.astral.sh/uv/getting-started/installation/). Everything here runs through `uv` and `uvx` — including Copier itself, which we invoke as `uvx copier` rather than installing globally. UV also fetches Python interpreters on demand and runs the post-generation `uv sync`.
- **Git** — `git init` is run automatically after generation.
- **[`glab`](https://gitlab.com/gitlab-org/cli)** — required by the `/judge-cicd` agent to inspect GitLab pipeline health. Install via your package manager (e.g. `brew install glab`) and authenticate with `glab auth login`.

## Quick start

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project
cd my-project
uv run pytest
```

`--trust` is required because [`copier.yaml`](copier.yaml) declares `_tasks` (`git init`, `uv sync`, `uv run pre-commit install`) that execute on your machine after rendering. Copier refuses to run them without explicit trust.

You'll be prompted for three values (see [Template variables](#template-variables)). After the prompts, Copier runs the [post-generation tasks](#post-generation-tasks) and you're ready to commit.

To regenerate a project later when this template gets new features, run `uvx copier update --trust` from inside the generated project — see [Updating an existing project](#updating-an-existing-project).

## Template variables

Defined in [`copier.yaml`](copier.yaml). Copier prompts for these interactively; you can also pass them via `--data key=value`.

| Variable       | Description                                          | Default                                                |
|----------------|------------------------------------------------------|--------------------------------------------------------|
| `project_name` | Human-readable project name                          | `My Python Project`                                    |
| `project_slug` | Python package name (importable)                     | auto-derived from `project_name` (lowercased, `_`-joined) |
| `description`  | One-line project description (used in `pyproject.toml`) | `A Python project generated with copier`            |

Non-interactive example:

```bash
uvx copier copy --trust ssh://git@gitlab-master.nvidia.com:12051/nvis-ova/nvis-ova-agentic-copier.git my-project \
  --data project_name="Order Service" \
  --data description="Order ingestion pipeline"
```

## Post-generation tasks

After rendering, Copier runs (configured in [`copier.yaml`](copier.yaml) under `_tasks`):

1. `git init` — initialize a git repository.
2. `uv sync` — create the venv and install dependencies.
3. `uv run pre-commit install` — install the git pre-commit hook.

You can re-run any of these manually if a step fails.

## Generated project layout

```
my-project/
├── .claude/
│   ├── agents/            # Claude Code agent definitions (architect, tdd-*, judge-*)
│   ├── commands/          # Slash-command wrappers for the agents above
│   ├── project.md         # Project-specific Claude context (loaded by CLAUDE.md)
│   └── settings.json
├── .gitlab-ci.yml         # GitLab CI pipeline (ruff, pytest+tox, yamllint, ty)
├── .pre-commit-config.yaml
├── CLAUDE.md              # Conventions + agentic workflow instructions
├── README.md              # Generated stub — fill in for your project
├── my_project/
│   └── __init__.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   └── helpers/
└── pyproject.toml         # ruff, pytest, coverage, tox, dependency groups
```

## Agentic workflow

The generated project ships with a Claude Code multi-agent workflow that enforces test-driven development and quality/CI gates **for Python code**. All agents are defined under `.claude/agents/` and exposed as slash commands under `.claude/commands/`.

The workflow is Python-specific: it assumes `uv`, `ruff`, `ty`, `pytest`, and `tox`, and the `/judge-quality` checklist enforces Python conventions (pathlib, dependency inversion, type annotations, docstrings, `pytest.mark.parametrize`, etc.). It is not designed for non-Python projects.

> Background reading: [Claude Code overview](https://docs.claude.com/en/docs/claude-code/overview), [subagents](https://docs.claude.com/en/docs/claude-code/sub-agents), [slash commands](https://docs.claude.com/en/docs/claude-code/slash-commands).

### Git-flow lifecycle

Every change — TDD or trivial — wraps in the same lifecycle, documented in [`.claude/git-flow.md`](template/.claude/git-flow.md) and `@`-loaded from `CLAUDE.md` so the main agent always has it in context:

1. **Branch** — from a clean, up-to-date `main` (a clean working tree is required to start), the main agent creates `claude/<slug>` from the task description.
2. **Work** — either the TDD agents (below) or, for ≤~10-line trivial edits, a direct edit + commit.
3. **Publish** — `git push` + `glab mr create --push --target-branch main --yes` (combined into one command when no MR exists yet; just `git push` if one does). Runs **in-sandbox** over the HTTPS proxy, one approval per remote command. Branch protection makes the MR mandatory — there is no direct-to-`main` path, even for a one-line fix.
4. **Stay until it merges** — do **not** return to `main` when the MR opens. Remain on the branch until a human reviewer merges the MR **and** the post-merge pipeline on `main` is green (AI never approves or merges), then `git checkout main` + `git pull` and delete the merged local branch.

The TDD agents below own the middle "work" phase only.

### Routing (TDD work phase)

```
New feature or non-trivial change
        │
        ▼
   (git-flow Step 1: ensure feature branch)
        │
        ▼
   /architect          ← design first; skippable only for trivial single-function changes
        │
        ▼
   /tdd-red            ← Test Writer: write and confirm failing tests
        │
        ▼
   /tdd-green          ← Implementer: minimal code to pass tests
        │
        ▼
   /tdd-judge          ← runs ruff + non-tox pre-commit checks
        │
        ├──────────────┬──────────────┬──────────────┐   (run in parallel after tdd-judge)
        ▼              ▼              ▼              ▼
  /judge-quality   /judge-test    /tdd-document  /judge-cicd
   pure code       pure test      syncs README   checks .gitlab-ci.yml
   review          review (tests  to user-facing sync + pipeline
                   adhere to best changes        health via glab
                   practices)
        │              │              │              │
        └──────┬───────┴──────────────┴──────────────┘
               ▼
        /tdd-finalizer  ← always runs; verifies README, runs tox once across supported versions,
                          and commits locally. Rejects to /tdd-document if README is stale.
        │
        ▼
   (git-flow Steps 3–4: push + MR, then stay on the branch until it merges)
```

### Agents

| Command           | Role                                                                                                  | Does **not**                                |
|-------------------|-------------------------------------------------------------------------------------------------------|---------------------------------------------|
| `/architect`      | Designs modules, data structures, dependency boundaries                                               | Write code or tests                         |
| `/tdd-red`        | Writes failing tests; stops at Red                                                                    | Touch implementation                        |
| `/tdd-green`      | Minimal implementation to reach Green                                                                 | Refactor or add untested features           |
| `/tdd-judge`      | Runs ruff + non-tox pre-commit checks                                                                  | Fix issues — rejects and routes back        |
| `/judge-quality`  | Pure code review: design, clarity, conventions; tags findings **Major** / **Minor** / **Existing**    | Run any tooling                             |
| `/judge-test`     | Pure test-quality review (Sonnet): meaningful assertions, parametrize, hypothesis, order-independence, no internal mocking; tags **Major** / **Minor** / **Existing** | Run any tooling; review production code     |
| `/tdd-document`   | Updates `README.md` and other user-facing docs to reflect command/CLI changes                         | Touch code, tests, or agent definitions     |
| `/judge-cicd`     | Verifies `.gitlab-ci.yml` is in sync with the change and checks pipeline health via [`glab`](https://gitlab.com/gitlab-org/cli) | Edit CI config or retry pipelines           |
| `/tdd-finalizer`  | Verifies README sync, runs tox across all Python versions, commits locally; the orchestrator then follows [`git-flow.md`](template/.claude/git-flow.md) Steps 3–4 (push + MR via [`glab`](https://gitlab.com/gitlab-org/cli); stay on the branch until it merges) | Fix issues — rejects and routes back        |

**Two non-negotiable rules:**

1. Never write implementation before a failing test exists.
2. Never merge without all validators approving.

### Quality finding severity

The two quality reviewers — `/judge-quality` (production code) and `/judge-test` (tests) — classify every finding as one of:

- **Major** — defect in this change. The judge returns `Overall: Reject` and the orchestrator reroutes automatically: `/judge-quality` to `/tdd-green`, `/judge-test` to `/tdd-red` (test defects belong to the Test Writer).
- **Minor** — non-blocking style or clarity nit caused or kept by this change. The main agent appends each Minor finding to `FIXME.md` and stages it so `/tdd-finalizer` commits it alongside the change.
- **Existing** — pre-existing issue in surrounding code or tests that this change did not introduce. Surfaced in the report but never written to `FIXME.md`.

**Minor and Existing findings never block the merge.** `FIXME.md` (deferred quality findings) and its companion `TODO.md` (user-directed follow-ups) are checked into the repo so deferred work stays visible. After `/tdd-finalizer` reports the MR, the user is asked whether any `FIXME.md` entries should be elevated to `TODO.md`.

### Using the workflow

In a Claude Code session opened against the generated project:

```
/architect add a CSV ingestion module that streams rows into the existing pipeline
```

Claude designs the module, then you proceed with `/tdd-red`, `/tdd-green`, and onward. When a judge rejects, the workflow reroutes automatically — no approval prompt — until the gates pass.

The same agents and commands also live in **this** repo's `.claude/`, so you can dogfood the workflow while developing the template itself.

## Updating an existing project

Copier remembers the answers in `.copier-answers.yml` inside the generated project. To pull in template improvements:

```bash
cd my-project
uvx copier update --trust
```

Copier produces a 3-way merge against your local changes. Files listed under `_skip_if_exists` in [`copier.yaml`](copier.yaml) (e.g. `pyproject.toml`, `README.md`, `.gitlab-ci.yml`, the package's `__init__.py`) are **not** overwritten on update — your project's customizations are safe. See [Copier's update docs](https://copier.readthedocs.io/en/stable/updating/) for conflict-resolution details.

## Developing this template

The structure of this repo:

- `copier.yaml` — variables, post-generation tasks, skip-if-exists list.
- `template/` — the template payload. Files with `.jinja` get rendered and the extension stripped (`pyproject.toml.jinja` → `pyproject.toml`). Paths containing `{{ … }}` are also rendered (e.g. `{{ project_slug }}/__init__.py`).
- `tests/test_template.py` — exercises template generation via `copier.run_copy()` and inspects the output.
- `CLAUDE.md` (root) vs. `template/CLAUDE.md` — **separate files**. The root one applies when working on the template; the template one is what gets copied into generated projects.

```bash
# Run the template's own tests
uv run tox                  # All Python versions
uv run pytest               # Faster iteration

# Lint and format
uv run ruff check --fix
uv run ruff format
uv run ty check

# Pre-commit
uv run pre-commit run --all-files
```

Conventions for changes to this repo are in [`CLAUDE.md`](CLAUDE.md) — same agentic workflow applies.

## References

- [Copier documentation](https://copier.readthedocs.io/)
- [UV documentation](https://docs.astral.sh/uv/)
- [Ruff documentation](https://docs.astral.sh/ruff/)
- [ty (type checker)](https://github.com/astral-sh/ty)
- [pytest](https://docs.pytest.org/)
- [tox](https://tox.wiki/)
- [pre-commit](https://pre-commit.com/)
- [Claude Code documentation](https://docs.claude.com/en/docs/claude-code/overview)
- [GitLab CI/CD](https://docs.gitlab.com/ci/)
- [`glab` CLI](https://gitlab.com/gitlab-org/cli)
