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
- **`Overall: Approve`**: continue. If the report contains any **Minor** or **Existing** findings, **track them** — keep the verbatim list in your working memory (or a TaskCreate entry titled "deferred quality findings") so they can be surfaced after `/tdd-finalizer` completes. The main agent owns this list; do not lose it across the parallel fan-out.
- Once `/tdd-document` and `/judge-cicd` also return approval, route to `/tdd-finalizer` as usual. **After** the finalizer commits, present the tracked Minor/Existing findings to the user and ask how to proceed (defer, fix in a follow-up, ignore). This is the only judge-quality moment that asks the user.
