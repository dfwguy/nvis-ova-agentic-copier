---
description: Filesystem-layout reviewer — validates the Python and Bash structure gate
---

Use the Agent tool with `subagent_type: filesystem-structure-judge` and the
current `group_id` to validate the repository layout. Brief the subagent fully
— it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the changed files and identify Python, Bash, and entry-point changes.
- Require inspection of `.claude/project.md` and the current source/test tree.
- Require an `Overall: Approve` only for standard layouts or a specific,
  accurate documented deviation; otherwise route the rejection to `/tdd-green`.

When the subagent returns, wait for every other final reviewer before routing
to `/tdd-finalizer`.
