---
description: Validator — runs pytest, ruff, ty, pre-commit (sandbox-safe); rejects and routes back
---

Use the Agent tool with `subagent_type: tdd-judge` to validate the implementation. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration (use `git status` / `git diff --stat` to capture).
- Mention the agreed test-pass / lint-clean / type-clean expectation.

When the subagent returns:
- If it approves, route to `/judge-quality`.
- If it rejects, present the failures verbatim and ask the user whether to send back to `/tdd-green` (production-code fix) or `/tdd-red` (test fix).
