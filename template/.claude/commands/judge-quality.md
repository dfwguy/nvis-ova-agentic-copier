---
description: Code reviewer — pure quality assessment, no tooling; classifies findings as Major / Minor / Existing
---

Use the Agent tool with `subagent_type: judge-quality` to do a pure code-quality review of **Python** changes. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration with paths (no diffs needed; the subagent will Read them).
- Mention any architectural intent worth checking against (e.g. "this split was supposed to keep the public API stable").
- The subagent has no Bash — do not ask it to run anything; it returns findings tagged **Major**, **Minor**, or **Existing** plus an `Overall: Approve` or `Overall: Reject` line.

When the subagent returns:
- Read the `Overall:` line first — it decides routing.
- **`Overall: Reject`** (any Major findings): present the Major issues verbatim and route back to `/tdd-green`. Do **not** ask the user — rejection-and-reroute is automatic per `CLAUDE.md`.
- **`Overall: Approve`** with any findings:
  - **Minor** findings → **append each one to `FIXME.md` at the repo root** under a `## YYYY-MM-DD — <current branch>` header (create the file with the standard header from `CLAUDE.md` → "FIXME.md and TODO.md" if it does not exist). Each entry is one line: `- **<file>:<line>** — <verbatim Minor finding>`. After writing, run `git add FIXME.md` so `/tdd-finalizer` includes it in the commit.
  - **Existing** findings → present verbatim in your return note for visibility, but do NOT write them to `FIXME.md` (they are not introduced by this change).
- Once `/tdd-document` and `/judge-cicd` also return approval, route to `/tdd-finalizer` as usual.
- End your response to the orchestrator with a one-line note: `Added <N> entries to FIXME.md` (or `No new FIXME entries` when there were no Minor findings). `/tdd-finalizer` uses this note when surfacing the change to the user after the MR is reported.
