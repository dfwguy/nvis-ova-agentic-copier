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

This is the TDD discipline for this repo's coding workflow. It is
language-agnostic. `AGENTS.md` defines the route. Documentation and agent-policy
work uses separate guidance. Do not load this file just because content is
non-trivial.

## Always

- Write a failing test before implementation. There are no exceptions.
- Confirm the test is Red (failing) before implementation.
- Write the least code that makes the test pass. Write nothing more.
- Confirm Green (all tests passing) before you refactor.
- Refactor only while tests remain Green

## Phases and roles

Use explicit phases. Each phase has a distinct role:

- **Test Writer** — concurrently writes failing tests for the desired behavior.
  Stops when tests are confirmed Red.
- **Diátaxis Writer** — concurrently drafts required `docs/` updates or says
  why none are needed. Exclusively mutates `docs/`.
- **Agent Policy Writer** — when applicable, concurrently edits only approved
  root/template agent-policy paths.
- **Implementer** — starts only after all applicable phase-2 receipts/N/A.
  Writes production, tests, and docs. Reconciles drafts against final
  interfaces. Never edits agent-policy paths.
- **Judge** — validates the full cycle. It checks meaningful tests, clean
  implementation, passing tooling, and conventions.

In Phase 3, Green runs `uv run ruff check --fix`, `uv run ruff format`, and
`uv run ty check`. It then runs focused behavior tests. It stages the exact
candidate and records its tree OID. These fast commands are repair tools.
Phase 4 verifies the candidate. It owns the one expensive tox/full-suite run.
In serial Phase 4, Judge runs the installed hook through Git on that staged
tree. It returns the unchanged tree OID. It returns immutable
supported-version JUnit snapshot paths and SHA-256 digests. It also returns
test counts and its Non-functional Reviewer child receipt. Every coding
candidate runs the full hook. Only then may Green start concurrent Phase-5
read-only reviews. Phase 6 Finalizer verifies that evidence. It makes the
interactive `--no-verify` commit. Phase 7 main publishes. Phase 8 independently
observes the exact-source pipeline.

Policy findings return through Architect to the Agent Policy Writer. Green
consolidates the finding but does not repair policy. Each correction gets a
fresh Judge before dependent review.

## Never

- Write implementation code before a failing test exists.
- Skip the Red confirmation step.
- Add functionality not covered by a test.
- Suppress a failing test. Fix the code instead.
- Approve (Judge) code with linter, type-checker, or test failures.
