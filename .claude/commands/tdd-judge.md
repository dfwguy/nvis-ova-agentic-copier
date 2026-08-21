---
description: Validator — runs pytest, ruff, ty, pre-commit (sandbox-safe); rejects and routes back
---

Use the Agent tool with `subagent_type: tdd-judge` and the current `group_id` to validate the implementation. Brief the subagent fully — it has no memory of this conversation. Pass that same `group_id` to every fan-out Agent call.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration (use `git status` / `git diff --stat` to capture).
- Mention the agreed test-pass / lint-clean / type-clean expectation.
- Require the subagent to run `uv run ruff check`, `uv run ruff format --check`, and direct pytest in that order before pre-commit, returning immediately on the first blocking failure.

When the subagent returns:
- If it approves, fan out to `/judge-quality`, `/judge-test`, `/tdd-document`, and `/judge-cicd` in parallel (single message, multiple Agent calls). Route to `/tdd-finalizer` once all four have returned approval.
- If it rejects, present the first blocking failure verbatim and route automatically to `/tdd-green` (production-code fix) or `/tdd-red` (test fix).
