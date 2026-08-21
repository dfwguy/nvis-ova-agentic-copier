---
description: Keep README.md accurate for people landing on the project
---

Use the Agent tool with `subagent_type: tdd-document` and the current `group_id` to keep the landing `README.md` accurate. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration.
- Mention any user-facing surface that changed (CLI flags, scripts, slash commands, setup steps, env vars). If you're unsure whether a change is user-facing, say so — the subagent will decide.
- The subagent edits `README.md` only — the Diátaxis Writer owns `docs/`.

When the subagent returns, present whether `README.md` was updated (or why it was already accurate). Wait for `/judge-quality` and `/judge-cicd` to also return before routing to `/tdd-finalizer`.
