---
description: Implementer — writes minimal code to pass failing tests; no extras
---

Use the Agent tool with `subagent_type: tdd-green` and the current `group_id` to implement the minimum code needed to pass the failing tests. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- Name the failing tests (file paths and test ids) the subagent must turn green.
- Paste the architect's design or summarize the agreed module layout.
- List any production files that may be edited and any that must stay untouched.
- Reiterate project conventions (functional style, dependency injection, no `print`, ruff ALL).

When the subagent returns, confirm all previously failing tests now pass and no existing tests regressed, then route to `/tdd-judge`.
