---
description: Design phase — produces a module/function breakdown before any code is written
---

Use the Agent tool with `subagent_type: architect` to design the following work in a fresh subagent context. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- Restate the task and any relevant constraints from this conversation (target files, public API to preserve, behavior to keep working).
- Mention which tests must continue to pass (run `uv run pytest -q --collect-only` if you need a current list).
- Point to any prior design notes (e.g. REFACTOR_PLAN.md) the subagent should read.

When the architect returns, present the design verbatim and route to `/tdd-red` — no approval step needed unless the design surfaces a genuine open question for the user.

**If the design splits the work into phases, identifies follow-ups, or carves out scope for "later"**, pause before routing and ask the user whether the deferred phases / follow-ups should be recorded in `TODO.md` (see `CLAUDE.md` → "FIXME.md and TODO.md"). Capture only what the user confirms; do not append speculatively. After recording (or skipping), continue to `/tdd-red` with the current-phase scope.
