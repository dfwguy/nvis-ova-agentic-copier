---
description: Test-quality reviewer — pure review of test design against our testing conventions; classifies findings Major / Minor / Existing
---

Use the Agent tool with `subagent_type: judge-test` and the current `group_id` to do a pure test-quality review. Identify whether the change exercises Python, Bash, or both and require language-appropriate test practices. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the test files added or changed in this iteration with paths (no diffs needed; the subagent will Read them).
- Point to the production behavior the tests are meant to cover, so the judge can assess whether the tests are meaningful.
- The subagent has no Bash — `tdd-judge` already ran the suite; do not ask it to run anything. It returns findings tagged **Major**, **Minor**, or **Existing** plus an `Overall: Approve` or `Overall: Reject` line.

When the subagent returns:
- Read the `Overall:` line first — it decides routing.
- **`Overall: Reject`** (any Major findings): present the Major issues verbatim and route back to `/tdd-red` automatically — rejection-and-reroute is automatic per `CLAUDE.md`, do **not** ask the user. Exception: if a Major finding is that production code *forces* internal mocking (a missing injection point / DI violation), route that one to `/tdd-green` instead, since the fix is in production code.
- **`Overall: Approve`** with any findings (same GitLab issue mechanism as `/judge-quality`):
  - **Minor** findings → the main agent files each one as a GitLab issue using the Bug template, normally with severity `S4`, and applies `type::quality` when that label exists. Link the relevant file and line plus the branch, MR, or review context. Use the numeric project ID required by `AGENTS.md`.
  - **Existing** findings → present verbatim in your return note for visibility, but do not file them automatically because this change did not introduce them.
  - **Issue creation unavailable** → preserve the Minor finding in the return note as unfiled and state the precise authorization, authentication, or API blocker. Never claim it was persisted and do not create a Markdown backlog file as a fallback.
- Route to `/tdd-finalizer` only once `/judge-quality`, `/judge-cicd`, `/tdd-document`, and `/cli-syntax-judge` have **also** returned approval.
- End your response to the orchestrator with a one-line note: `Created <N> GitLab issues` (or `No new issues` when no issue was created). Include the created issue URLs and any unfiled findings immediately above that line. `/tdd-finalizer` uses this handoff when assembling the MR and final report.
