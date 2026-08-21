---
description: Test-quality reviewer — pure review of test design against our testing conventions; classifies findings Major / Minor / Existing
---

Use the Agent tool with `subagent_type: judge-test` and the current `group_id` to do a pure test-quality review of the **tests** in this change. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the test files added or changed in this iteration with paths (no diffs needed; the subagent will Read them).
- Point to the production behavior the tests are meant to cover, so the judge can assess whether the tests are meaningful.
- The subagent has no Bash — `tdd-judge` already ran the suite; do not ask it to run anything. It returns findings tagged **Major**, **Minor**, or **Existing** plus an `Overall: Approve` or `Overall: Reject` line.

When the subagent returns:
- Read the `Overall:` line first — it decides routing.
- **`Overall: Reject`** (any Major findings): present the Major issues verbatim and route back to `/tdd-red` automatically — rejection-and-reroute is automatic per `CLAUDE.md`, do **not** ask the user. Exception: if a Major finding is that production code *forces* internal mocking (a missing injection point / DI violation), route that one to `/tdd-green` instead, since the fix is in production code.
- **`Overall: Approve`** with any findings (same mechanism as `/judge-quality` — see `CLAUDE.md` → "FIXME.md and TODO.md"):
  - **Minor** findings → **append each one to `FIXME.md` at the repo root** under a `## YYYY-MM-DD — <current branch>` header (create the file with the standard header if it does not exist). Each entry is one line: `- **<file>:<line>** — <verbatim Minor finding>`. After writing, run `git add FIXME.md` so `/tdd-finalizer` includes it in the commit.
  - **Existing** findings → present verbatim in your return note for visibility, but do NOT write them to `FIXME.md` (they are not introduced by this change).
- Route to `/tdd-finalizer` only once `/judge-quality`, `/judge-cicd`, and `/tdd-document` have **also** returned approval.
- End your response to the orchestrator with a one-line note: `Added <N> entries to FIXME.md` (or `No new FIXME entries` when there were no Minor findings). `/tdd-finalizer` uses this note when surfacing the change to the user after the MR is reported.
