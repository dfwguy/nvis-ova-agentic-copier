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
- **Architect** — accepts Red's exact evidence. It returns interactive work to
  Main for a local Red commit, or invokes the trusted serial
  `agent-work red-checkpoint create` boundary. It otherwise stays read-only.
- **Implementer** — starts only after all applicable preparation receipts/N/A
  and the exact mode-specific Red checkpoint. Writes production, tests, and
  docs. Reconciles drafts against final interfaces. Never edits agent-policy
  paths.
- **Judge** — validates the full cycle. It checks meaningful tests, clean
  implementation, passing tooling, and conventions.

Red's evidence file has exact `argv`, `expected_failure`, and normalized changed
`test_paths`. The bounded shell-free `argv` begins with `uv run pytest` and uses
positional test paths or node IDs. Architect checks the receipt, then selects
the workflow mode.

In interactive work, Main alone stages the approved test paths. It creates a
local `--no-verify` Red commit. Main keeps its SHA and paths under a named local
ref and returns them to Architect. It must not push or publish while that
failing commit is the branch tip.

In serial work, commits are prohibited. Architect runs:

```bash
uv run agent-work red-checkpoint create \
  --issue-iid IID --evidence-file PATH --apply
```

The executable reruns the evidence and owns the serial local Git ref and state change.
Its report binds project, issue, branch, base, tree, ref, test paths, command,
nonzero exit, failure marker, and output digest. It cannot commit, push, or
publish. Green requires this checkpoint and inspects the ordinary Git diff from
it before implementation and review.

During implementation, Green runs `uv run ruff check --fix`, `uv run ruff format`, and
`uv run ty check`. It then runs focused behavior tests. It stages the exact
candidate and records its tree OID. These fast commands are repair tools.
Validation verifies the candidate. It owns the one expensive tox/full-suite
run. In serial validation, Judge invokes exactly one mutation-gated durable validation
session for that staged tree. The session runs the installed hook and persists
evidence. Judge checks and returns the unchanged tree OID plus immutable
supported-version JUnit snapshot paths and SHA-256 digests. It also returns
test counts and its Non-functional Reviewer child receipt. Every coding
candidate runs the full hook. Only then may Green start concurrent specialist
reviews. Test Judge verifies the Red-to-Green Git diff. During finalization,
Finalizer checks that diff, checkpoint, and validation proof. In interactive
work, it creates one final passing commit over the original base. It keeps the
Red ref. Main publishes only that tip. Its history has one final commit over
the base before push. Pipeline review
independently observes the exact-source pipeline.

Finalizer uses one bounded local compaction. Before it acts, it proves the
passing head, base, Red commit and ref, Judge tree, clean state, ancestry, and
no remote action. It creates the Judge tree commit with the base as sole parent.
It atomically compare-and-swaps the branch from the passing head. It then proves
the new head, parent, tree, unchanged Red ref, and clean state. Any failure stops
publication.

Recovery reuses the exact retained mode-specific checkpoint; it never rebuilds
one from prose. No Red checkpoint ref is pushed. Correctness does not depend on
server-side squash. Serial workers still cannot commit. The trusted controller
retains its single validated final commit path.

Policy findings return through Architect to the Agent Policy Writer. Green
consolidates the finding but does not repair policy. Each correction gets a
fresh Judge before dependent review.

## Never

- Write implementation code before a failing test exists.
- Skip the Red confirmation step.
- Add functionality not covered by a test.
- Suppress a failing test. Fix the code instead.
- Approve (Judge) code with linter, type-checker, or test failures.
