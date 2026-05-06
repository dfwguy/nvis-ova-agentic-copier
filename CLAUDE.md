# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@.claude/project.md

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

Always run commands inside the sandbox first. Only retry with the sandbox disabled after seeing an actual sandbox-related failure (e.g. "Operation not permitted", network host denied) — never preemptively. Pausing to ask before trying the sandbox is the wrong default; trying and failing is cheap.

## Linting

Ruff is configured with `select = ["ALL"]`. Exceptions are documented in `pyproject.toml`. Do not suppress rules inline (`# noqa`) without a compelling reason — add to the `pyproject.toml` ignore list with a comment explaining why.

## Python Conventions

- **Pathlib** over `os` for all path operations
- **Dependency inversion**: pass functions as arguments rather than calling external dependencies directly — keeps code testable without patching
- **Dataclasses** for structured configuration or grouped parameters
- **Type annotations** on all functions and methods
- **Docstrings** on all public functions, classes, and modules
- **`raise`** instead of `sys.exit(1)` for error conditions
- **Specific exceptions** — catch the narrowest type possible; never bare `except Exception` without re-raising
- **Private functions** prefixed with `_` when not part of the public interface
- **No `print`** — use `logger` everywhere; `print` is only acceptable inside `if __name__ == "__main__"` guards
- Use `uv run` — never invoke `python` directly

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

## Multi-Agent Workflow

All feature work routes through explicit agents invoked as slash commands (e.g. `/architect`). Definitions are in `.claude/commands/`.

### Routing

```
New feature or non-trivial change
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
        ▼
  /judge-quality       ← pure code review, no tooling; Pass / Minor / Reject per section
        │
        ▼
  /tdd-finalizer       ← always runs; full pre-commit (inc. tox), run until clean
```

### Agent Summary

| Command | Role | Does not |
|---------|------|----------|
| `/architect` | Designs modules, data structures, dependency boundaries | Write code or tests |
| `/tdd-red` | Writes failing tests; stops at Red | Touch implementation |
| `/tdd-green` | Minimal implementation to reach Green | Refactor or add untested features |
| `/tdd-judge` | Stages files, runs pytest + pre-commit (inc. tox) | Fix issues — rejects and routes back |
| `/judge-quality` | Pure code review: design, clarity, conventions | Run any tooling |
| `/tdd-finalizer` | Stages files, runs tox across all Python versions, commits; mandatory final gate | Fix issues — rejects and routes back |

Never write implementation before a failing test exists. Never merge without all three validators approving.

## Script Conventions

When writing standalone scripts:
- Include a shebang line
- Define a `main()` function as the entry point
- Define `parse_arguments()` separately from `main()`
- Add a `--debug` / `-d` flag that routes extra output to `logger.debug`
- Use `logger.info` (not `print`) once the logger is initialized
