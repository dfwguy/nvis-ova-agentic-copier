---
title: Why the coding standards exist
type: explanation
summary: Understand the rationale for the generated development conventions.
audience: maintainer
tags: [template-architecture]
status: current
last_reviewed: 2026-08-27
related:
  - path: why-diataxis.md
    why: Explains the documentation structure used for these conventions.
---

# Why the coding standards exist

This page explains the intent behind the
Python and Bash conventions generated in `AGENTS.md` and `.agents/lang/`.

The standards are designed to make ordinary changes safe to review, easy to
test, and unsurprising to maintain. They are defaults for a new project, not a
claim that every Python program needs the same architecture.

## Give source code and entry points clear homes

Python projects use the conventional `src/<package>/` layout. Keeping
importable code below `src/` makes a test exercise the installed package rather
than accidentally importing code from the repository root. Tests therefore sit
outside the production package, while `pyproject.toml` remains the single place
for packaging metadata and Python console-script declarations.

Bash has a different packaging model, but benefits from the same separation of
responsibilities. A generated Bash tool has a single public executable in
`bin/`; its implementation lives under `src/` or `lib/`. This gives users one
obvious command to invoke and keeps sourceable helpers from becoming accidental
public interfaces. Development and CI helpers can live elsewhere when their
purpose is clear, but they are not additional user CLIs.

Some projects have genuine compatibility or operational reasons for another
layout. Those decisions are explicit rather than implicit: `.agents/project.md`
must identify the affected language, the normal rule being replaced, the actual
source and entry-point paths, and the reason. The filesystem-structure gate
uses that record to distinguish a deliberate exception from structure that has
simply drifted.

## Make boundaries explicit

The template prefers dependency injection for filesystem access, subprocesses,
HTTP, clocks, environment lookups, and other external effects. Code that
receives an external dependency can be tested with a small fake, rather than
patching internal module symbols. That makes the boundary visible in the API
and reduces tests that pass only because they know implementation details.

Pydantic is reserved for inputs crossing a system boundary, where validation
and useful error messages matter most. Internally, frozen dataclasses provide
simple immutable grouping without bringing runtime validation to every value.
The combination draws a practical line: validate once at the edge, then keep
the core small and predictable.

## Prefer predictable code

Type annotations, public docstrings, narrow exception handling, private-name
prefixes, `pathlib`, and logging all make behavior easier to inspect. Functional
style and immutable values reduce hidden shared state. None of these rules
removes judgment; together they set a readable baseline that a reviewer can
apply consistently.

Ruff is configured broadly so a project starts with many useful checks. Its
exceptions live in configuration, with reasons, rather than accumulating
unexplained inline suppressions. That keeps deviations visible and reviewable.

## Test behavior rather than implementation

The workflow asks for failing tests before implementation so the intended
outcome is explicit before code is optimized around it. Parameterization makes
the set of examples clear, Hypothesis explores unanticipated inputs for pure
functions, and randomized test order exposes state leaks. The coverage floor is
a guardrail: it asks for meaningful exercised behavior without treating a
percentage as proof of correctness.

Specialized tools such as `pytest-subprocess` and `requests-mock` are preferred
at genuine system boundaries. Internal mocking is discouraged because it can
make a test verify call choreography instead of the result a user needs.

## Make mutation intentional

Standalone scripts that can change files, Git state, network services, or other
external systems must default to a dry run and require `--apply` to mutate.
This makes a bare command a preview and gives users a chance to inspect its
effect. Preview and apply should follow the same decision path until the final
side effect, so the safe path is still a meaningful exercise of the command.

## Keep quality checks close to the work

UV provides one consistent way to install and run the toolchain. Pre-commit
catches common problems before a change leaves a workstation, and GitLab CI
repeats the checks independently for every branch. Tox verifies the supported
Python versions rather than letting a change be accidentally validated only on
the contributor's interpreter.

The agent workflow adds independent review and documentation checks around
these tools. It is deliberately process-oriented: the aim is reliable handoff
to a human reviewer, not automation that bypasses review or merge protections.

For the generated layout, commands, and tooling requirements, use the
[template reference](../reference/template-contract.md). For the broader
template design, read
[why the template and generated project are separate](template-and-generated-project.md).
