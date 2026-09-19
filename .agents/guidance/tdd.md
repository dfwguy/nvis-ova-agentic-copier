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

Language-agnostic TDD discipline for this repo's coding workflow. `AGENTS.md`
defines the route; documentation and agent-policy work uses its separate
guidance and must not load this file merely because the content is non-trivial.

## Always

- Write a failing test before any implementation — no exceptions
- Confirm the test is Red (failing) before switching to implementation
- Write the minimum code to make the test pass — nothing more
- Confirm Green (all tests passing) before refactoring
- Refactor only while tests remain Green

## Phases and roles

Work in explicit phases with distinct roles:

- **Test Writer** — concurrently writes failing tests that define the desired behavior; stops when tests are confirmed Red
- **Diátaxis Writer** — concurrently drafts required `docs/` updates or records why none are needed; exclusively mutates `docs/`
- **Agent Policy Writer** — when applicable, concurrently edits only approved root/template agent-policy paths
- **Implementer** — starts only after all applicable phase-2 receipts/N/A, writes production, tests, and docs, and reconciles drafts against final interfaces; it never edits agent-policy paths
- **Judge** — validates the full cycle: meaningful tests, clean implementation, all tooling passing, conventions followed

In Phase 3, Green runs `uv run ruff check --fix`, `uv run ruff format`, and
`uv run ty check`, then focused behavior tests, stages the exact candidate, and
records its tree OID. These fast commands are repair tools; Phase 4 verifies the
candidate and owns the one expensive tox/full-suite run. In serial Phase 4,
Judge runs the installed hook through Git against that staged tree and returns
the unchanged tree OID plus immutable supported-version JUnit snapshot paths,
SHA-256 digests, test counts, and its Non-functional Reviewer child receipt.
Every coding candidate runs the full hook. Only then may Green start the
concurrent Phase-5 read-only reviews. Phase 6
Finalizer verifies that evidence and makes the interactive `--no-verify` commit;
Phase 7 main publishes, and Phase 8 independently observes the exact-source
pipeline.

Policy findings return through Architect to the Agent Policy Writer. Green
consolidates the finding but does not repair policy; every correction gets a
fresh Judge before dependent review.

## Never

- Write implementation code before a failing test exists
- Skip the Red confirmation step
- Add functionality not covered by a test
- Suppress a failing test — fix the code instead
- Approve (Judge) code that has linter, type-checker, or test failures
