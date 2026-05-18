---
description: Final gate — runs full tox across all Python versions, commits locally; orchestrator then publishes per git-flow.md
---

Use the Agent tool with `subagent_type: tdd-finalizer` to run pre-commit (full tox) and create the commit. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- The intended commit message subject + body (the subagent will create the commit).
- Confirmation that prior gates (`tdd-judge`, `judge-quality`, `judge-cicd`) approved and `tdd-document` finished.
- Whether the change touched any user-facing surface (CLI flags, scripts, slash commands, setup steps, env vars). The subagent will verify `README.md` reflects it and reject back to `tdd-document` if not.
- Do NOT pass `--no-verify` or `--no-gpg-sign`. Reject if a hook fails for a real (non-auto-fix) reason.

When the subagent returns:
- If rejected for missing README updates, route back to `/tdd-document`.
- If rejected for any other reason, route back to `/tdd-green`.
- If approved, present the final commit hash and `git status`, then **follow `.claude/git-flow.md` Step 3 (publish: push + MR) and Step 4 (return to main)**. Do not duplicate the recipe here — git-flow.md is the source of truth.

After the MR is reported, surface any deferred `/judge-quality` Minor/Existing findings verbatim and ask the user how to handle each (defer, follow-up, ignore). Do not act on them without explicit user direction.
