---
description: Validator — runs language-appropriate lint and non-test pre-commit checks; rejects and routes back
---

Use the Agent tool with `subagent_type: tdd-judge` and the current `group_id` to validate the implementation. Brief the subagent fully — it has no memory of this conversation. Pass that same `group_id` to every fan-out Agent call.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration (use `git status` / `git diff --stat` to capture).
- Identify whether changed files are Python, Bash, or mixed. Require Python
  changes to run `uv run ruff check` then `uv run ruff format --check`; require
  Bash changes to run `bash -n` then `shellcheck` on changed shell files.
- Require non-tox pre-commit checks after the applicable language gates,
  returning immediately on the first blocking failure. Tox is the sole Python
  test execution in `/tdd-finalizer`.

When the subagent returns:
- If it approves, wait for `/diataxis-judge`, which runs in parallel. Once both approve, fan out to `/judge-quality`, `/judge-test`, `/tdd-document`, `/judge-cicd`, and `/cli-syntax-judge` in parallel (single message, multiple Agent calls). Route to `/tdd-finalizer` once all five have returned approval.
- If it rejects, present the first blocking failure verbatim and route automatically to `/tdd-green` (production-code fix) or `/tdd-red` (test fix).
