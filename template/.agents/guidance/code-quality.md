<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Production Code Quality

This file owns shared production code rules. It covers design, build, and
review. Language-specific rules stay in the matching `.agents/lang/` guide.

## Integration and Public Surface

- Reuse package logic, constants, helpers, data shapes, and known patterns
  when they fit. Do not reinvent them without a stated reason.
- When two places do the same work, identify both and the shared home.
- Match nearby patterns for logs, errors, dependency injection, and names.
- Keep module bounds and public exports clear. Do not leak private symbols. Do
  not add unused exports.
- Group related work in modules. Keep their dependencies few.
- Give each shared business rule one owner in code.

## Functional Design

- Prefer pure functions with clear inputs and outputs. Keep side effects at
  boundaries.
- Give each function one clear purpose. Keep functions small and composable.
- Prefer immutable data. Do not use mutable global state.

## Dependencies and Side Effects

- Point dependencies toward core rules. Do not create cycles.
- Keep core decisions apart from external work. Use thin adapters.
  They map data at the edge and call core logic. They do not own business rules.
- Apply dependency inversion when the language supports it. Inject external
  calls at a boundary. This includes subprocess, filesystem, HTTP, time, and
  environment calls. Do not hide them in business logic.
- Make each external call testable. Wire real dependencies only at an entry
  point or top-level orchestrator.
- Do not make user-facing production behavior rely on undocumented ambient
  state. Ambient state is working-directory, environment, or network state
  inherited without an explicit choice from the process or runtime.

## Efficiency and Maintenance

- Avoid repeated work, such as parsing in a loop or fetching per item. Use
  data structures that fit. Avoid needless copies.
- Do not leave dead code, commented-out blocks, unused imports, or duplicate
  logic.
- Name who owns errors and recovery at each boundary. Make success, refusal,
  and recovery easy to test. Preserve data and safeguards.
- Before adding permanent compatibility for recovery, establish that the
  triggering state can recur after merge. Name the owner, behavior tests, and
  ongoing contract for that state. A retained one-time snapshot alone does
  not justify a permanent path. Fix a reproducible bug when its behavior can
  recur, even if a single snapshot revealed it.

## Clarity

- Use clear names and readable structure.
- Avoid unclear one-letter names, except in small, usual scopes.
- Make production code easy to follow. Do not rely on hidden state or
  hard-to-see control flow.
