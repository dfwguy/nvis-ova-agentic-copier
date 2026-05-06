---
description: Code reviewer — pure quality assessment, no tooling; Pass/Minor/Reject per section
---

Use the Agent tool with `subagent_type: judge-quality` to do a pure code-quality review. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration with paths (no diffs needed; the subagent will Read them).
- Mention any architectural intent worth checking against (e.g. "this split was supposed to keep the public API stable").
- The subagent has no Bash — do not ask it to run anything; it returns Pass/Minor/Reject per section.

When the subagent returns:
- If approved, route to `/tdd-finalizer` immediately.
- If rejected, present the issues verbatim and ask the user before routing back to `/tdd-green`.
