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
- **Implementer** — starts only after both phase-2 receipts, writes minimal code to pass failing tests, and reconciles the draft against final interfaces
- **Judge** — validates the full cycle: meaningful tests, clean implementation, all tooling passing, conventions followed

## Never

- Write implementation code before a failing test exists
- Skip the Red confirmation step
- Add functionality not covered by a test
- Suppress a failing test — fix the code instead
- Approve (Judge) code that has linter, type-checker, or test failures
