---
description: Test Writer — writes failing tests and confirms Red before any implementation
---

Use the Agent tool with `subagent_type: tdd-red` and the current `group_id` to write failing tests in a fresh subagent context. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- Paste the architect's design (or a summary of the agreed behavior) — the subagent cannot see it otherwise.
- List the test files to add or extend.
- State the expected failure modes the new tests must cover.
- Note any project conventions (`pytest.mark.parametrize`, dependency injection over mocks, no `# noqa` without explanation).

When the subagent returns, present the failing-test output confirming Red and route to `/tdd-green`.
