# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@.claude/project.md

@.claude/git-flow.md

## Commands

```bash
# Run tests
uv run tox                  # All tests across Python versions
uv run pytest               # Run pytest directly (faster iteration)

# Lint and format
uv run ruff check --fix     # Lint and auto-fix
uv run ruff format          # Format code
uv run ty check             # Type checking

# Pre-commit
uv run pre-commit run --all-files
```


## Sandbox

Run commands in the sandbox first — never preemptively disable it. On a sandbox failure, try to fix the command (or sandbox config) before falling back to disabling the sandbox.

## Linting

Ruff is configured with `select = ["ALL"]`. Exceptions are documented in `pyproject.toml`. Do not suppress rules inline (`# noqa`) without a compelling reason — add to the `pyproject.toml` ignore list with a comment explaining why.

## Python Conventions

- **Pathlib** over `os` for all path operations
- **Dependency inversion** (see [Dependency Inversion](#dependency-inversion) below) — external calls are passed in as parameters, not imported and called directly
- **Pydantic at boundaries, dataclasses internally** — any structured data crossing a system boundary (config files, CLI args, HTTP payloads, file-format I/O) is parsed into a Pydantic model so validation happens once at the edge. Internal grouped data uses `@dataclass(frozen=True)` — cheap to construct in tests, immutable by default, no runtime validation overhead. Do not reach for Pydantic on internal types just because it's available.
- **Type annotations** on all functions and methods
- **Docstrings** on all public functions, classes, and modules
- **`raise`** instead of `sys.exit(1)` for error conditions
- **Specific exceptions** — catch the narrowest type possible; never bare `except Exception` without re-raising
- **Private functions** prefixed with `_` when not part of the public interface
- **No `print`** — use `logger` everywhere; `print` is only acceptable inside `if __name__ == "__main__"` guards
- Use `uv run` — never invoke `python` directly

## Dependency Inversion

We use dependency inversion throughout. Every external call — subprocess, filesystem, HTTP, time, environment lookups, anything that crosses a system boundary — is **passed in as a parameter** rather than imported and invoked directly.

The pattern:

- Functions accept their external dependencies as arguments (typically `Callable`s, small protocols, or dataclasses of callables).
- Real implementations are wired in once, at the entry point (`main()` or a top-level orchestrator).
- Tests pass fakes/stubs through the same parameters — no module-level patching.

If a test reaches for `unittest.mock.patch` to swap a symbol inside our own code, the production code needs DI applied first — that's a refactor, not a test workaround. Patching is reserved for true system boundaries handled by purpose-built tools: `pytest-subprocess` for subprocess, `requests-mock` for HTTP, stdlib `unittest.mock` for things we genuinely cannot inject.

The architect names every injection point in the design; the implementer wires them through; the judges reject changes that hardcode external calls or that patch internal modules in tests.

## Functional Programming

Prefer functional style throughout:

- **Pure functions**: same inputs → same outputs, no side effects
- **Small, composable functions**: one responsibility each; chain them
- **Immutable data**: prefer tuples over lists when the collection won't change
- **Comprehensions over loops**: list/dict/set comprehensions are preferred for transforms
- No global mutable state

## Testing

- Test method docstrings are not required — the method name is sufficient documentation
- Use `pytest.mark.parametrize` over duplicated test functions for data-driven cases
- Test behavior, not implementation — avoid mocking internal code; mock at system boundaries only
- Use `pytest-subprocess` for subprocess isolation; `unittest.mock` (stdlib) for other external calls; `requests-mock` for HTTP calls
- **Property-based tests with `hypothesis`** for pure functions — declare the invariant, let hypothesis find the counterexamples. Use it alongside `parametrize` (handpicked cases for known edges, hypothesis for the unknown ones), not as a replacement.
- **`pytest-randomly` is enabled** — tests must be order-independent. A test that only passes in a given order is broken; fix the shared state rather than pinning order.
- **Coverage gate**: tox enforces `--cov-fail-under=85` as the floor. Raise it as the codebase matures; never lower it to make a CI run pass.

## Multi-Agent Workflow

All feature work routes through explicit agents invoked as slash commands (e.g. `/architect`). Definitions are in `.claude/commands/`.

**Scope: this workflow is for Python projects.** The agents assume `uv`, `ruff`, `ty`, `pytest`, and `tox` are available, and the conventions enforced by `/judge-quality` (pathlib, dependency inversion, type annotations, docstrings, `pytest.mark.parametrize`, etc.) are Python-specific. Do not invoke this flow against non-Python code.

### Routing

The branch / commit / push / MR / return-to-main lifecycle is described once in `.claude/git-flow.md` (loaded above) and applies to **every** change — TDD or trivial. The diagram below covers the in-between "do the work" phase for non-trivial changes; the surrounding lifecycle steps come from `git-flow.md`.

```
New feature or non-trivial change
        │
        ▼
   (Step 1 of git-flow.md: ensure feature branch)
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
   /tdd-judge          ← stages files, runs pytest + pre-commit (inc. tox)
        │
        ├──────────────┬─────────────────┬────────────────────┐
        ▼              ▼                 ▼                    ▼
  /judge-quality   /tdd-document    /judge-cicd       (run in parallel after tdd-judge)
   pure review     syncs README to  checks .gitlab-ci.yml
                   user-facing      sync + pipeline
                   changes          health via glab
        │              │                 │
        └──────┬───────┴─────────────────┘
               ▼
        /tdd-finalizer  ← always runs; verifies README, runs pre-commit (inc. tox),
                          and commits locally. Rejects to /tdd-document if README is
                          stale.
        │
        ▼
   (Steps 3–4 of git-flow.md: push + open/update MR, then return to main)
```

### Agent Summary

| Command | Role | Does not |
|---------|------|----------|
| `/architect` | Designs modules, data structures, dependency boundaries | Write code or tests |
| `/tdd-red` | Writes failing tests; stops at Red | Touch implementation |
| `/tdd-green` | Minimal implementation to reach Green | Refactor or add untested features |
| `/tdd-judge` | Stages files, runs pytest + pre-commit (inc. tox) | Fix issues — rejects and routes back |
| `/judge-quality` | Pure code review: design, clarity, conventions; tags findings **Major** / **Minor** / **Existing** | Run any tooling |
| `/tdd-document` | Updates README.md and other user-facing docs to reflect command/CLI changes | Touch code, tests, or agent definitions |
| `/judge-cicd` | Verifies `.gitlab-ci.yml` is in sync with the change and checks pipeline health via `glab` | Edit CI config or retry pipelines |
| `/tdd-finalizer` | Verifies README sync, runs tox across all Python versions, commits locally; the orchestrator then follows `git-flow.md` Steps 3–4 (push + MR + return to main) | Fix issues — rejects and routes back |

Never write implementation before a failing test exists. Never merge without all validators approving.

### Routing back on rejection

When a judge (`/tdd-judge`, `/judge-quality`, `/judge-cicd`, `/tdd-finalizer`) rejects and routes back to an earlier agent, proceed automatically — **do not pause to ask the user for approval**. Rejection-and-reroute is a normal part of the workflow, not a decision point. Only stop for the user when there is a genuine decision to make (ambiguous design, conflicting requirements, destructive action). A judge's structured reroute is not such a decision.

### `/judge-quality` severity model

`/judge-quality` classifies every finding as one of:

- **Major** — defect in this change. Triggers `Overall: Reject`; the main agent reroutes to `/tdd-green` automatically.
- **Minor** — non-blocking style/clarity nit caused or kept by this change. Does not reroute.
- **Existing** — pre-existing issue in surrounding code, not introduced by this change. Never blocks.

The **main agent owns the deferred-findings list**: when `/judge-quality` returns `Overall: Approve` with Minor or Existing findings, the main agent records them verbatim and surfaces them to the user **after** `/tdd-finalizer` commits — at that point the user decides whether to defer, fix in a follow-up, or ignore each item. Do not silently drop these findings between the parallel fan-out and the finalizer.

## Script Conventions

When writing standalone scripts:
- Include a shebang line
- Define a `main()` function as the entry point
- Define `parse_arguments()` separately from `main()`
- Add a `--debug` / `-d` flag that routes extra output to `logger.debug`
- Use `logger.info` (not `print`) once the logger is initialized
