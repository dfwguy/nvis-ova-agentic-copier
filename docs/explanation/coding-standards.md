---
title: Why the coding standards exist
type: explanation
summary: Learn why projects use these coding standards.
audience: maintainer
tags: [template-architecture]
status: current
last_reviewed: 2026-08-27
related:
  - path: why-diataxis.md
    why: Explains the documentation structure used for these conventions.
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

# Why the coding standards exist

These standards make code easier to read and tests easier to write. They give
source code and commands clear homes. They mark external effects. They keep
checks near each change. The rules are defaults, not ceremony. A project can
record an exception and its reason. Reviewers can then check that choice. This
supports safe routine work. It also helps maintainers see each change and lets
users trust the tool.

This page explains the intent behind the Python and Bash conventions generated
in `AGENTS.md` and `.agents/lang/`.

The standards make routine changes safe to review and easy to test. They are
defaults for new projects. They do not claim every Python program needs the
same architecture.

## Give source code and entry points clear homes

Python projects use the standard `src/<package>/` layout. Code below `src/`
lets tests use the installed package. They cannot import code by mistake from
the repository root. Tests sit outside the production package.
`pyproject.toml` holds packaging data and Python console-script declarations.

Bash uses a different package model. It still benefits from clear roles. A
generated Bash tool has one public executable in `bin/`. Its code lives under
`src/` or `lib/`. Users get one clear command. Sourceable helpers do not become
public interfaces. Development and CI helpers can live elsewhere when their
role is clear. They are not extra user CLIs.

Some projects need another layout for compatibility or operations. Record that
choice in `.agents/project.md`. Name the language, the rule it replaces, the
real source and entry-point paths, and the reason. The filesystem-structure
gate can then tell a planned exception from drift.

## Make boundaries explicit

The template prefers dependency injection for filesystem access, subprocesses,
HTTP, clocks, environment lookups, and other external effects. Code can accept
an external dependency and use a small fake in tests. This shows the API
boundary. It also reduces tests that depend on internal details.

Pydantic is reserved for inputs crossing a system boundary, where validation
and useful error messages matter most. Internally, frozen dataclasses provide
simple immutable grouping without bringing runtime validation to every value.
The combination draws a practical line: validate once at the edge, then keep
the core small and predictable.

## Prefer predictable code

Type annotations, public docstrings, narrow exception handling, private-name
prefixes, `pathlib`, and logging make behavior easier to inspect. Functional
style and immutable values reduce hidden shared state. These rules keep room
for judgment. Together, they give reviewers a readable baseline.

Ruff starts projects with many useful checks. Its exceptions and reasons live
in configuration. They do not build up as unexplained inline suppressions. This
keeps deviations visible and easy to review.

## Test behavior rather than implementation

The workflow asks for failing tests before implementation. The intended result
is clear before code grows around it. Parameterization lists examples.
Hypothesis explores new inputs for pure functions. Random test order exposes
state leaks. The coverage floor asks for meaningful exercise. A percentage is
not proof of correctness.

Use tools such as `pytest-subprocess` and `requests-mock` at real system
boundaries. Avoid internal mocking. It can make a test check call order instead
of the result a user needs.

## Make mutation intentional

Scripts that can change files, Git state, network services, or other external
systems start as a dry run. They require `--apply` to make changes. A bare
command is a preview, so users can inspect its effect. Preview and apply follow
the same choices until the last side effect. The safe path still tests the
command well.

## Keep quality checks close to the work

UV gives one way to install and run the toolchain. Pre-commit catches common
problems before a change leaves a workstation. GitLab CI repeats the checks for
merge-request events and the default branch. Tox checks supported Python
versions. It avoids testing a change only on the contributor's interpreter.

The agent workflow adds review and documentation checks around these tools. Its
goal is a reliable handoff to a human reviewer. It does not bypass review or
merge safeguards.

For generated layout, commands, and tool needs, use the
[template reference](../reference/template-contract.md). For the wider design,
read [why the template and generated project are separate](template-and-generated-project.md).
