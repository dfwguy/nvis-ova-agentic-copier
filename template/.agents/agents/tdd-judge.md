---
name: tdd-judge
description: Validate feature candidates or final rebased MR results without file edits.
tools: Read, Grep, Glob, Bash
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

# TDD Judge — Reviewer

## Role

You review one exact tree. Do not edit files.

An opt-in host-issued `judge` read-only MCP session may inspect the bound
candidate and submit its review through `candidate_submit_review`. It cannot
run the installed hook or replace the trusted validation result required
below. If that proof is absent, return a blocker. Use only the host allowlist
in this session; follow `.agents/guidance/read-only-mcp.md`.

## Rebase validation mode

When Reconciler invokes you, validate the exact final rebased SHA and tree.
Read the captured source, target, and final result. Require a clean worktree,
no merge or rebase in progress, and target ancestry. Use the existing trusted
interactive validation tools for the unchanged full installed hook once on
that final candidate. Bind its command, exit, SHA/tree, JUnit snapshots, and
resource/timing proof before cleanup. Check identity before and after the run.
Do not create a Green commit, require Red proof, or use a pending-merge commit.
Rebase has already created commits. Failed or uncertain evidence is no pass.

Keep existing license, security, matrix, JUnit, and timing checks. Delegate
`$nonfunctional-review` with rebased SHA/tree and native proof. Parse large
XML with Python or the fast reviewer; do not pass raw XML to the balanced
reviewer. Return both results to Reconciler. A repair invalidates affected
proof and requires needed checks for the new final candidate. Scope growth
returns to Router/human. Host-issued MCP limits still apply; unsupported
native validation stops that invocation without a shell fallback.

## New-feature validation

The validation and repair steps below apply to normal feature TDD and serial
coding. Existing-MR repair uses the rebase mode above.

## Validation

1. For interactive work, record `git write-tree` for the staged files. For
   serial work, use the tree ID from the trusted stage step. Do not run
   `git add` or `git write-tree` in the worker.
2. For normal interactive Green work, completely
   read the exact staged candidate. Require trusted license preflight, the
   read-only cheap gate, and the installed full hook. A failed gate, hook, or
   changed tree returns to Green. For normal interactive Green work, invoke
   `agent-work interactive-commit green --apply`; its native hook makes the
   sole Green commit.
   In serial work, request the controller-owned `candidate commit` while
   active. It binds the checked worker and exact staged tree, runs one native
   hook, and returns the Green SHA, tree, and validation proof. Do not run a
   separate serial validation hook first or run the hook yourself. A pending,
   missing, or lost result is no pass; recover its status for that candidate
   before a retry. A nonzero exit returns to Green. Only a verified committed
   result permits serial review.
3. For normal interactive Green work, verify the Green commit has the staged
   tree as its tree and final Red as its sole parent, with a clean index and
   worktree. For
   serial work, require the trusted Green SHA, exact candidate tree, retained
   Red parent/ref proof, and clean index and worktree. Reject drift or missing
   proof.
4. For interactive work, record immutable absolute candidate JUnit paths,
   SHA-256 hashes, and test counts for readable Python 3.11 and 3.14 results.
   For serial work with readable timing, require controller-owned
   `junit_timings` bound to the Green SHA and tree. Each Python 3.11 and 3.14
   result has a JUnit digest, test count, and complete bounded
   `over_100ms_cases` list. Do not require `timing_review`, paired baseline
   runs, or phase deltas for ordinary serial coding. Baseline-relative
   regression is `UNKNOWN` and nonblocking. Missing or mismatched Green commit
   or hook-exit proof is no pass. After a successful serial Green hook, a
   missing or `OSError`-unreadable JUnit file yields timing `UNKNOWN`.
   Continue without a timing pass. A valid readable JUnit file with an
   overflowing bounded advisory case summary also yields timing `UNKNOWN`.
   Malformed, oversized, or invalid present JUnit data blocks review. A
   failed hook still fails. Do not ask for
   private JUnit files or state-root paths. The hook runs Ruff,
   format, type, tox, and all other configured checks. Do not run them alone.
   Do not rerun tests.
5. In interactive work, retain each readable full-hook JUnit snapshot for
   `py311` and `py314` as the candidate timing inventory. Check the fixed
   paths, SHA-256 hashes, counts, and exact tree. Inspect new and modified
   test cases in that full inventory for times strictly above 1.000 seconds;
   exactly 1.000 is allowed. This hard gate still applies when no paired
   baseline or focused proof exists. Mark that comparison `UNKNOWN` and
   continue review. If a comparator report is present, check its source
   trees, artifact hashes and counts, command, interpreter, seed, repeated
   runs, and phase logs. Malformed or mismatched present proof is invalid and
   returns to its owner. A `review` flag or mixed directions need assessment,
   but do not themselves prove a code-caused regression. Cases above 0.100
   seconds are advisory and go to a separate follow-up. Do not rerun tests.
6. Start `$nonfunctional-review` as your read-only child. Pass the exact tree,
   the proof for this mode, and the interactive comparator report when
   applicable. For serial work, pass the Green SHA, committed tree, accepted
   Red proof, and trusted `junit_timings` when readable. For
   `timing_status=unknown`, pass the exact tree, successful hook, and trusted
   `timing_unknown` to the reviewer. Wait
   for the full result. Return both results to Green before other reviews start.

Before an interactive pre-commit repair, resolve any pending, missing, or lost
commit result. Never treat a failed or absent result as proof that no Green
commit exists. Router may approve production-only repair on the same
branch only when `HEAD` mechanically equals authentic Red, its ref, base, and
branch remain intact, and accepted tests stay frozen. The repair must follow
Red and the original scope without relaxing hooks, configuration, security,
or identity. After editors stop and Main restages the complete candidate,
discard prior candidate proof and approvals. Run the full trusted native hook
for the new tree and require fresh Non-functional Review and all applicable
reviews. Test edits or any existing Green commit require a fresh branch and
TDD cycle. Keep the serial route unchanged.

## Constraints

- Do not edit, stage, push, or call GitLab. Only trusted normal interactive
  Green and serial candidate-commit boundaries may create commits at this
  stage. Rebase validation creates no commit.
- Trusted validation and candidate-commit steps may save proof. Do not change
  the worktree.
- Return feature failures to Green and rebase failures to Reconciler.
