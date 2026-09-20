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

This file is the single source of truth for Python conventions in this repo. Agents
working on Python code include it automatically.

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

Ruff uses `select = ["ALL"]`. Exceptions are in `pyproject.toml`. Do not use
inline `# noqa` without a compelling reason. Put it in the `pyproject.toml`
ignore list with a comment explaining why.

## Python Conventions

### Filesystem structure

- Keep importable production code in `src/<package>/`; do not add a top-level importable package beside `pyproject.toml`.
- Keep tests in `tests/`, outside the production package.
- Keep packaging metadata in `pyproject.toml`.
- Declare Python CLI entry points in `[project.scripts]`. They invoke the installed package.
- Nonstandard layout requires `.agents/project.md` to state language, replaced
  rule, actual paths, and rationale.

### General rules

- Use **Pathlib** over `os` for all path operations.
- Use **dependency inversion**. See [Dependency Inversion](#dependency-inversion).
- Pass external calls as parameters. Do not directly import and call them.
- Parse any structured config files, CLI args, HTTP payloads, and file-format I/O into Pydantic
  models. That validates system-boundary data once at the edge.
- Use `@dataclass(frozen=True)` for internal grouped data. It is test-cheap,
  immutable by default, and has no runtime validation overhead.
- Do not use Pydantic for internal types just because it is available.
- Type all functions and methods. Add docstrings to all public functions, classes, and modules.
- Use `raise`, not `sys.exit(1)`, for errors.
- Catch the narrowest type. Never bare `except Exception` without re-raising.
- Prefix non-public functions with `_`.
- Use `logger`, not `print`. `print` is allowed only in `if __name__ == "__main__"`.
- Use `uv run`, never `python` directly.

## Dependency Inversion

Use dependency inversion throughout. Pass subprocess, filesystem, HTTP, time,
and environment lookups as parameters.

- Functions take dependencies as `Callable`s, small protocols, or dataclasses of callables.
- Wire real implementations once at `main()` or a top-level orchestrator.
- Tests pass fakes or stubs. Do not patch modules.

If a test uses `unittest.mock.patch` to replace our symbol, apply DI first. It
is a refactor, not a test workaround. Patch only true system boundaries. Use
`pytest-subprocess` for subprocess, `requests-mock` for HTTP, and stdlib
`unittest.mock` for dependencies that cannot be injected.

The Architect names every injection point. The implementer wires it. Judges
reject hardcoded external calls and internal module patching in tests.

## Functional Programming

- Prefer pure functions: same inputs → same outputs, with no side effects.
- Use small, composable functions with one responsibility each. Chain them.
- Prefer tuples for unchanged collections.
- Prefer list, dict, and set comprehensions for transforms.
- Do not use global mutable state.

## Testing

- Test method docstrings are not required. Names document tests.
- Use `pytest.mark.parametrize` instead of duplicate data-driven tests.
- Test behavior, not implementation. Mock only system boundaries.
- Use `pytest-subprocess` for subprocess, `unittest.mock` (stdlib) for other
  external calls, and `requests-mock` for HTTP.
- Use property-based `hypothesis` tests for pure functions. Declare invariants.
- Use hypothesis with `parametrize`: edges are handpicked; unknowns are found.
- `pytest-randomly` is enabled. Tests must be order-independent. Fix shared
  state; never pin an order.
- Tox enforces `--cov-fail-under=85`. Raise it as code matures. Never lower it
  to pass CI.

For examples of subprocess, HTTP, imports, filesystem, logging, and conftest
fixtures, see [`python-testing.md`](python-testing.md). Read it on demand for
non-trivial tests. It is not auto-loaded.

## Script Conventions

- Include a shebang. Define `main()` and separate `parse_arguments()`.
- Add `--debug` / `-d` for `logger.debug` output.
- Operational logging stays local. A user-facing CLI or service must use a
  `logging.FileHandler` or rotating file handler at an explicit application-owned local path.
- `--debug` / `-d` raises local-handler verbosity. Ordinary runs retain local
  lifecycle and failure events.
- Do not add `logging.handlers.SysLogHandler`, another remote syslog handler,
  or a network log handler. Test ordinary and `--debug` verbosity.
- A mutating script or CLI is dry-run by default. It needs `--apply` / `-a` to
  write files, call a network/external service, change git state, or delete.
- Without `--apply`, it makes no changes. Log intended actions with `logger.info`
  and a prefix such as `[dry-run]`, then exit successfully.
- Mutate only with `--apply`. This is non-negotiable. A bare run is harmless.
- Read-only scripts need no flag. Use `logger.info`, not `print`, after setup.

Preview and apply share a code path. Branch only at the injected side-effecting
boundary. Pass `--apply` as a parameter; do not check it ad hoc.

### Verifying mutating commands against the real environment

Tests inject fakes at all system boundaries. See
[Dependency Inversion](#dependency-inversion). They never reach a real API,
network, or filesystem outside the sandbox. They are fast and hermetic. Green
tests do not prove auth, connectivity, argument wiring, or the read side.

Before and after changing a mutating script or CLI, run its actual command
outside the sandbox without `--apply`. The first run confirms the baseline. The
second confirms the real-world path. It is harmless: it reaches the
side-effecting boundary, logs intended work, and changes nothing.

This sanctioned run is the exception to `AGENTS.md` sandbox-first. Tests cannot
cover this path. Assume local authentication, remotes, and dependencies exist,
unless `.agents/project.md` says local running is not set up. In that case, skip
the real-environment run rather than guessing.
