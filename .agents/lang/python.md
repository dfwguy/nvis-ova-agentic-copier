<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Python Conventions

This file is the single source of truth for Python-specific conventions in this repo. It is included automatically by agents that work on Python code.

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

## Python Versions

- Minimum: **3.11**
- Tested: **3.11** and **3.14** (via tox)
- Avoid `3.12+` stdlib additions

## Linting

Ruff is configured with `select = ["ALL"]`. Exceptions are documented in `pyproject.toml`. Do not suppress rules inline (`# noqa`) without a compelling reason — add to the `pyproject.toml` ignore list with a comment explaining why.

## Python Conventions

### Filesystem structure

- Keep importable production code in `src/<package>/`; do not add a top-level
  importable package beside `pyproject.toml`.
- Keep tests in `tests/`, outside the production package.
- Keep packaging metadata in `pyproject.toml`; declare Python CLI entry points
  through `[project.scripts]` so they invoke the installed package.
- A nonstandard layout is allowed only when `.agents/project.md` records the
  language, replaced rule, actual paths, and rationale.

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

For worked examples of the harder cases — subprocess (`pytest-subprocess`), HTTP (`requests-mock`), imports, filesystem, logging, and conftest fixtures — see [`python-testing.md`](python-testing.md). Read it on demand when writing or reviewing non-trivial tests; it is not auto-loaded.

## Script Conventions

When writing standalone scripts:
- Include a shebang line
- Define a `main()` function as the entry point
- Define `parse_arguments()` separately from `main()`
- Add a `--debug` / `-d` flag that routes extra output to `logger.debug`
- **Operational logging stays local.** A user-facing CLI or service with
  operational logging must configure a `logging.FileHandler` (or rotating file
  handler) at an explicit application-owned local path. `--debug` / `-d` must
  raise the local handler's verbosity to include diagnostic events; ordinary
  runs retain lifecycle and failure events locally. Do not add
  `logging.handlers.SysLogHandler`, another remote syslog handler, or a network
  log handler. Tests must cover both ordinary and `--debug` verbosity.
- **Make no changes by default; require an `--apply` / `-a` flag to mutate state** for any script or CLI that writes files, calls a network/external service, changes git state, or deletes anything. Without `--apply`, the script makes **no** changes — it logs each action it *would* take via `logger.info` (prefixed e.g. `[dry-run]`) and exits success. The mutation only happens when the user passes `--apply`. This safe-by-default stance is non-negotiable for mutating tools: a bare invocation must always be a harmless preview. Read-only scripts do not need the flag.
- Use `logger.info` (not `print`) once the logger is initialized

The preview and apply paths should share the same code path, branching only at the side-effecting boundary (the injected dependency) — so a preview run exercises the same logic and the `--apply` value flows in as a parameter rather than being checked ad hoc throughout.

### Verifying mutating commands against the real environment

Our tests inject fakes at every system boundary (see [Dependency Inversion](#dependency-inversion)) and never reach a real API, network, or filesystem outside the sandbox — fast and hermetic, but it means **green tests alone don't prove the command works against the real thing**: auth, connectivity, argument wiring, and the read side all go unexercised. So before and after changing a mutating script or CLI, run the **actual command outside the sandbox, without `--apply`**, to smoke-test it against the real environment. Run it *before* the change to confirm the baseline works, and *after* to confirm the change didn't break the real-world path. Omitting `--apply` keeps the run a harmless preview — it drives the real code path up to the side-effecting boundary and logs what it *would* do, mutating nothing — which is exactly what makes it safe to run for real.

This is the deliberate exception to the "sandbox first" rule in `AGENTS.md`: the run is sanctioned outside the sandbox precisely because the tests cannot cover this path. Assume the working directory is already provisioned for these local runs (authenticated tooling, configured remotes, installed dependencies) **unless the project's `.agents/project.md` notes otherwise** — if it says the environment isn't set up for local running, skip the real-environment run rather than guessing.
