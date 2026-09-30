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
  and exact Red checkpoint. Interactive Green writes production only and never
  edits tests. Serial Green writes production and tests.
  Shares final interface details with the Diátaxis Writer. Never edits docs,
  README, or agent-policy paths.
- **Judge** — validates the full cycle. It checks meaningful tests, clean
  implementation, passing tooling, and conventions.

Red's evidence file has exact `argv`, `expected_failure`, and normalized changed
`test_paths`. The bounded shell-free `argv` begins with `uv run pytest` and uses
positional test paths or node IDs. Architect checks the receipt, then selects
the workflow mode.

Before a Red checkpoint, the Test Writer runs bounded type checks on changed
Python tests. Run read-only Ruff check and format checks too. Fix errors in
test code before asking the trusted boundary to save Red. A missing production
API may remain when the check lists it as a residual. Stop if a tool fails or
the check is absent. The trusted boundary reruns the checks on the exact test
tree. Interactive Red returns accepted residual details in its output. The
serial checkpoint saves them in its evidence.

In interactive work, Main stages the approved test paths and invokes
`agent-work interactive-commit red --apply`. The trusted boundary creates and
retains each test-only `--no-verify` Red commit under a named local ref. Main
must not push or publish while a failing commit is the branch tip.

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

Interactive Green runs only `uv run ruff check --fix`, `uv run ruff format`,
and `uv run ty check`. It checks readability when needed. It runs no tests,
tox, or pre-commit. Serial Green may run focused tests. It uses its controller.
The workflow owner waits until Green and the Diátaxis Writer stop editing. In interactive work, Main stages
their combined exact candidate and records its tree OID. In serial work,
Green requests staging through the controller-owned boundary while active;
the controller observes and normalizes the exact worktree's changed paths,
stages only allowed changes, and returns its tree OID. No model path list
authorizes serial staging. Repeat the applicable boundary after each correction.

These fast commands are repair tools. In interactive work, Judge owns the sole
normal Green commit and installed hook under its role contract. In
serial work, Judge requests the controller-owned `candidate commit` for
that exact staged tree. It runs the installed hook once and returns the Green
SHA, committed tree, and opaque validation evidence. No separate full
validation hook precedes this commit. Readable JUnit
files yield trusted counts, digests, and complete bounded slow-case lists;
a successful serial Green hook with a missing or `OSError`-unreadable JUnit
file yields `timing_status=unknown` and `timing_unknown`, without times.
A valid readable JUnit file with an overflowing bounded advisory case
summary also yields timing `UNKNOWN`. Malformed, oversized, or invalid
present JUnit data blocks review. A failed hook still fails.
Judge returns its Non-functional Reviewer child receipt. Specialist reviews
start after the normal Green commit, pending-merge validation, or serial Green
commit. Serial specialists review the Green SHA and tree. Test Judge checks
the Red-to-Green Git diff. During
finalization, Finalizer checks that diff, checkpoint, and validation proof.
For normal interactive Green work, it invokes
`agent-work interactive-commit verify` and
remains read-only. Main publishes only the verified Green tip. Pipeline review
independently observes the exact-source pipeline.

Recovery uses the exact retained checkpoint for the mode. It never rebuilds
one from prose. No Red checkpoint ref is pushed. Correctness does not depend on
session summaries or unverified role receipts. Before Green, restart from
current code, retained Red commits/ref, and the exact issue or MR. An
actionable production finding before the first interactive Green commit may
use the same branch only with Architect approval. Mechanically prove `HEAD`
equals authentic Red, retain its ref, base, and branch, and keep accepted tests
frozen. Resolve pending, missing, or lost commit results before editing and
prove no Green commit exists. Repair only behavior justified by Red and the
original scope; do not relax hooks, configuration, security, or identity.
After editors stop, Main restages the complete candidate. Discard prior
candidate proof and approvals. Require a fresh full Judge native hook,
Non-functional Review, and all applicable reviews. Test edits or any existing
Green commit require a fresh branch and TDD cycle. Never append a correction
commit. Serial workers still cannot commit. The
trusted controller creates each serial Green candidate commit before review;
after a correction, it creates a new one and invalidates stale receipts.
Recovery derives the current Red and Green identities from Git and GitLab
evidence, not a session summary. Publication uses the verified Green SHA.

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
candidate. While the merge is pending, Judge invokes
`agent-work validation run --candidate-tree <staged-tree> --apply` for read-only
validation of the exact staged candidate before specialist review. It does not
create a Green commit or consume `MERGE_HEAD`. Judge and Finalizer keep
historical Red-to-source proof apart from fresh three-way merge proof. Main
supplies freshly
fetched exact source and target ref proof before finalization. Finalizer checks
it and local identities. It makes no remote or GitLab calls. Main invokes
`agent-work reconcile-commit commit --apply` for the exact Judge-approved
staged tree from the still-pending merge state. Its installed hook runs during
the commit, which consumes `MERGE_HEAD`.
Captured old source is first. Exact target is second. Finalizer invokes
`agent-work reconcile-commit verify` read-only for the resulting commit and
checks clean state, exact parents, tree, and unchanged Red ref. It separately
compares the commit message with the reviewed message file. Any
source or target ref drift invalidates receipts. Restart exact selection and
preparation. Main
fetches and rechecks both refs just before publishing with exact old-source
`--force-with-lease` and same-merge-request readback. Humans alone approve and
merge. Keep the readback. The factory timer is irrelevant to reconciliation.
Keep each proof. Check each result. Keep the tree. Keep the refs. Check the state.

Documentation and README findings return through Architect to the Diátaxis
Writer. Policy findings return through Architect to the Agent Policy Writer.
In serial work, Green consolidates findings and repairs production and tests. Each
correction gets a fresh Judge before dependent review. In interactive work,
post-Green findings use a fresh branch and TDD cycle.

## Never

- Write implementation code before a failing test exists.
- Skip the Red confirmation step.
- Add functionality not covered by a test.
- Suppress a failing test. Fix the code instead.
- Approve (Judge) code with linter, type-checker, or test failures.
