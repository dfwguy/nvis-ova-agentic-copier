<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Test-Driven Development

This is the TDD discipline for coding work in this repo. It applies to all
languages. `AGENTS.md` defines the route. Docs and agent-policy work use other
guidance. Do not load this file for content-only work.

## Always

- Write a failing test before implementation. There are no exceptions.
- Confirm the test is Red (failing) before implementation.
- Write the least code that makes the test pass. Write nothing more.
- Confirm Green (all tests passing) before you refactor.
- Refactor only while tests remain Green

## Stages and roles

Use explicit stages. Each stage has a distinct role:

- **Test Writer** — concurrently writes failing tests for the desired behavior.
  Stops when tests are confirmed Red.
- **Diátaxis Writer** — concurrently drafts required `docs/` updates or says
  why none are needed. Exclusively mutates `docs/`.
- **Agent Policy Writer** — when applicable, concurrently edits only approved
  root/template agent-policy paths.
- **Implementer** — starts only after all preparation receipts or N/A records.
  Writes production, tests, and docs. Reconciles drafts against final
  interfaces. Never edits agent-policy paths.
- **Judge** — validates the full cycle. It checks meaningful tests, clean
  implementation, passing tooling, and conventions.

During implementation, Green runs `uv run ruff check --fix`, `uv run ruff
format`, and `uv run ty check`. It then runs focused behavior tests. It stages
the exact candidate and records its tree OID. These fast commands repair the
candidate.

Validation owns the costly tox or full-suite run. For serial work, Judge starts
one mutation-gated durable session for the staged tree. The session runs the
installed hook and saves its evidence. Judge returns the unchanged tree OID.
It also returns fixed-version JUnit snapshot paths, SHA-256 digests, test
counts, and its Non-functional Reviewer receipt. Every coding candidate runs
the full hook. Only then may Green start specialist reviews.

Finalization checks that evidence and makes the interactive `--no-verify`
commit. Publication pushes the work. Pipeline review checks the exact source
pipeline on its own.

Policy findings return through Architect to the Agent Policy Writer. Green
consolidates the finding but does not repair policy. Each correction gets a
fresh Judge before dependent review.

## Never

- Write implementation code before a failing test exists.
- Skip the Red confirmation step.
- Add functionality not covered by a test.
- Suppress a failing test. Fix the code instead.
- Approve (Judge) code with linter, type-checker, or test failures.
