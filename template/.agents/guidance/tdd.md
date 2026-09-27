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

- **Test Writer** — writes failing tests for the desired behavior. It runs
  first and stops when tests are confirmed Red. In the first serial segment,
  only this role may change files.
- **Diátaxis Writer** — owns `docs/**`, `template/docs/**`, `README.md`, and
  prose in `template/README.md.jinja`, or says why no update is needed. It does
  not change executable Jinja logic. It runs beside Green after the accepted Red checkpoint.
  Serial work starts it only after the exact retained Red checkpoint passes
  controller checks.
- **Agent Policy Writer** — when needed, edits only approved root/template
  agent-policy paths after the accepted Red checkpoint and before Green and
  the Diátaxis Writer start.
- **Architect** — accepts and routes Red's direct evidence. It returns
  interactive work to Main for a local Red commit. In serial work, the same
  live workflow owner that assembles the exact boundary input invokes the
  trusted result boundary through its identity-bound broker while active.
  Architect stays read-only. Each fresh Architect session gets three
  independent read-only plans at once before its first child can edit: one
  exact `$architect-proposal-balanced` and two exact `$architect-proposal-fast`
  children. It checks all three against repo facts and writes the only formal
  receipt. The first and resumed serial segments each repeat this gate with
  fresh children. A failed child gets at most one fresh replacement with the
  same skill and tier. If that fails, stop before edits. Plans cannot enter
  trusted boundary input.
- **Implementer** — starts after the applicable policy preparation receipt/N/A
  and the exact mode-specific Red checkpoint. Writes production and tests only.
  Shares final interface details with the Diátaxis Writer. Never edits docs,
  README, or agent-policy paths.
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

Serial workers do not commit. The first workflow segment does Red only. Its
live owner assembles the `red_checkpoint_ready` intent. It invokes
`agent-work worker-result build ... --apply` through the controller-owned,
identity-bound broker before exit. The accepted boundary sets the coding lane.
It checks preconditions and changed paths. It reruns Red. It persists and
ref-binds the exact checkpoint. Then it reloads and verifies it. The accepted
artifact is program-owned evidence. It binds the project, issue, branch, base,
tree, ref, test paths, command, nonzero exit, failure marker, and output digest.
It cannot commit, push, or publish. Before capture, only approved test paths may
change. Production, documentation, or policy changes reject capture. They remain
preserved. A canonical rejection goes to the live producer for one corrected
request. Only accepted retained evidence may start the resumed segment,
policy preparation, then concurrent Green and documentation writing. Green inspects the ordinary
Git diff from that checkpoint before implementation and review.

During implementation, Green runs `uv run ruff check --fix`, `uv run ruff format`,
and `uv run ty check`. It then runs focused behavior tests. The workflow owner
waits until Green and the Diátaxis Writer stop editing. In interactive work, Main stages
their combined exact candidate and records its tree OID. In serial work,
Green requests staging through the controller-owned boundary while active;
the controller observes and normalizes the exact worktree's changed paths,
stages only allowed changes, and returns its tree OID. No model path list
authorizes serial staging. Repeat the applicable boundary after each correction.

These fast commands are repair tools. Validation verifies the candidate. It
owns the one expensive tox/full-suite run. In serial work, Judge invokes the
controller-owned durable validation and status boundary for that exact tree.
The session runs the installed hook and persists evidence. The controller
returns an unchanged-tree proof, opaque evidence identity, and trusted
program-derived JUnit counts, digests, three slowest cases, complete
`over_100ms_cases` lists, and `timing_complete` flags for each supported local
Python environment without revealing controller-private snapshot paths.
Interactive work keeps immutable JUnit
snapshot paths, SHA-256 digests, and counts. Judge returns its Non-functional
Reviewer child receipt. Every coding candidate runs the full hook. Only then
may Green start concurrent specialist reviews. Test Judge verifies the
Red-to-Green Git diff. During finalization,
Finalizer checks that diff, checkpoint, and validation proof. In interactive
unpublished work, it creates one final passing commit over the original base.
For an open, published MR correction, it uses `agent-work published-commit append`.
It keeps the Red ref. Main pushes and checks the same MR. Pipeline review
independently observes the exact-source pipeline.

For unpublished work, Finalizer uses one bounded local compaction. Before it
acts, it proves the passing head, base, Red commit and ref, Judge tree, clean
state, ancestry, and no remote action. It creates the Judge tree commit with
the base as sole parent.
It atomically compare-and-swaps the branch from the passing head. It then proves
the new head, parent, tree, unchanged Red ref, and clean state. Any failure stops
publication.

Recovery reuses the exact retained mode-specific checkpoint; it never rebuilds
one from prose. No Red checkpoint ref is pushed. Correctness does not depend on
server-side squash. Serial workers still cannot commit. The trusted controller
retains its single validated final commit path.

## Reconciliation of completed coding work

Reconciliation recovers completed post-policy source work. It is not a new TDD
cycle. Architect mechanically re-proves the exact retained authentic Red
checkpoint against the unchanged captured source. Never create Red evidence
retrospectively. A pre-checkpoint merge request routes to issue #158. A missing
or mismatched post-policy checkpoint stops safely for recovery. The re-proved
retained receipt is the Red preparation receipt. Architect does not delegate a
fresh Test Writer. TDD Red does not manufacture evidence.

Architect prepares the pending target merge and conflict manifest. It freshly
routes the joined diff. Green resolves coding conflicts by preserving approved
source and target intent. Fresh validation and reviews cover the combined
candidate. Judge and Finalizer keep historical Red-to-source proof apart from
fresh three-way source-plus-target-to-candidate proof. Main supplies freshly
fetched exact source and target ref proof before finalization. Finalizer checks
it and local identities. It makes no remote or GitLab calls. It directly commits
the exact Judge-approved staged tree from the still-pending merge state. This
consumes `MERGE_HEAD`. Captured old source is first. Exact target/`MERGE_HEAD`
is second. Do not create an intermediate passing commit or a sole-parent
compaction. Postchecks require cleared merge state, a clean index and worktree,
exact parents, tree, message, and unchanged Red ref. Any source or target ref
drift invalidates receipts. Restart exact selection and preparation. Main
fetches and rechecks both refs just before publishing with exact old-source
`--force-with-lease` and same-merge-request readback. Humans alone approve and
merge. Keep the readback. The factory timer is irrelevant to reconciliation.
Keep each proof. Check each result. Keep the tree. Keep the refs. Check the state.

Documentation and README findings return through Architect to the Diátaxis
Writer. Policy findings return through Architect to the Agent Policy Writer.
Green consolidates findings but repairs only production and tests. Each
correction gets a fresh Judge before dependent review.

## Never

- Write implementation code before a failing test exists.
- Skip the Red confirmation step.
- Add functionality not covered by a test.
- Suppress a failing test. Fix the code instead.
- Approve (Judge) code with linter, type-checker, or test failures.
