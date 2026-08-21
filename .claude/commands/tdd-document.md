---
description: Update README.md (and other user-facing docs) to reflect command/CLI changes
---

Use the Agent tool with `subagent_type: tdd-document` and the current `group_id` to sync user-facing documentation. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration.
- Mention any user-facing surface that changed (CLI flags, scripts, slash commands, setup steps, env vars). If you're unsure whether a change is user-facing, say so — the subagent will decide.
- The subagent edits docs only — it will not touch code, tests, or agent definitions.

When the subagent returns, present the list of updated doc files (or "no documentation changes needed"). Wait for `/judge-quality` and `/judge-cicd` to also return before routing to `/tdd-finalizer`.
