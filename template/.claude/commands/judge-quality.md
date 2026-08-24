---
description: Code reviewer — pure quality assessment, no tooling; classifies findings as Major / Minor / Existing
---

Use the Agent tool with `subagent_type: judge-quality` and the current `group_id` to do a pure code-quality review. Identify whether changed files are Python, Bash, or mixed and require the corresponding language checklist. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration with paths (no diffs needed; the subagent will Read them).
- Mention any architectural intent worth checking against (e.g. "this split was supposed to keep the public API stable").
- The subagent has no Bash — do not ask it to run anything; it returns findings tagged **Major**, **Minor**, or **Existing** plus an `Overall: Approve` or `Overall: Reject` line.

When the subagent returns:
- Read the `Overall:` line first — it decides routing.
- **`Overall: Reject`** (any Major findings): present the Major issues verbatim and route back to `/tdd-green`. Do **not** ask the user — rejection-and-reroute is automatic per `CLAUDE.md`.
- **`Overall: Approve`** with any findings:
  - **Minor** findings → the main agent files each one as a GitLab issue using the Bug template, normally with severity `S4`, and applies `type::quality` when that label exists. Link the relevant file and line plus the branch, MR, or review context. Use the numeric project ID required by `AGENTS.md`.
  - **Existing** findings → present verbatim in your return note for visibility, but do not file them automatically because this change did not introduce them.
  - **Issue creation unavailable** → preserve the Minor finding in the return note as unfiled and state the precise authorization, authentication, or API blocker. Never claim it was persisted and do not create a Markdown backlog file as a fallback.
- Once `/judge-test`, `/tdd-document`, `/judge-cicd`, and `/cli-syntax-judge` also return approval, route to `/tdd-finalizer` as usual.
- End your response to the orchestrator with a one-line note: `Created <N> GitLab issues` (or `No new issues` when no issue was created). Include the created issue URLs and any unfiled findings immediately above that line. `/tdd-finalizer` uses this handoff when assembling the MR and final report.
