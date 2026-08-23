---
description: Validator — runs ruff and non-tox pre-commit checks (sandbox-safe); rejects and routes back
---

Use the Agent tool with `subagent_type: tdd-judge` and the current `group_id` to validate the implementation. Brief the subagent fully — it has no memory of this conversation. Pass that same `group_id` to every fan-out Agent call.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration (use `git status` / `git diff --stat` to capture).
- Mention the agreed test-pass / lint-clean / type-clean expectation.
- Require the subagent to run `uv run ruff check`, `uv run ruff format --check`, and then non-tox pre-commit checks in that order, returning immediately on the first blocking failure. Tox is the sole test execution in `/tdd-finalizer`.

When the subagent returns:
- If it approves, wait for `/diataxis-judge`, which runs in parallel. Once both approve, fan out to `/judge-quality`, `/judge-test`, `/tdd-document`, `/judge-cicd`, and `/cli-syntax-judge` in parallel (single message, multiple Agent calls). Route to `/tdd-finalizer` once all five have returned approval.
- If it rejects, present the first blocking failure verbatim and route automatically to `/tdd-green` (production-code fix) or `/tdd-red` (test fix).
