---
description: Final gate — runs applicable Python/Bash checks, commits locally; orchestrator then publishes per git-flow.md
---

Use the Agent tool with `subagent_type: tdd-finalizer` and the current `group_id` to run the applicable Python/Bash checks, non-tox pre-commit, and the Python tox matrix only when Python changed, then create the commit. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- The intended commit message subject + body (the subagent will create the commit).
- Confirmation that prior gates (`tdd-judge`, `diataxis-judge`, `judge-quality`, `judge-test`, `judge-cicd`, `cli-syntax-judge`, `filesystem-structure-judge`) approved and `tdd-document` finished.
- The `Added <N> entries to FIXME.md` line from `/judge-quality` **and** `/judge-test` (or `No new FIXME entries`) so the subagent knows whether `FIXME.md` is part of this commit.
- Whether the change touched any user-facing surface (CLI flags, scripts, slash commands, setup steps, env vars). The subagent will verify `README.md` reflects it and reject back to `tdd-document` if not.
- Do NOT pass `--no-verify` or `--no-gpg-sign`. Reject if a hook fails for a real (non-auto-fix) reason.
- Identify whether changed files are Python, Bash, or mixed. Require Python
  changes to run `uv run ruff check`, `uv run ruff format --check`, and
  `uv run tox run-parallel`; require Bash changes to run `bash -n` and
  `shellcheck` on changed shell files. Run non-tox pre-commit after the
  applicable lint gates. Tox is the single Python test execution; do not run
  direct pytest. Stop and return on the first blocking failure; do not rerun a
  failing hook or stage/commit after any failed validation.

The subagent must also confirm that `FIXME.md` is staged whenever `/judge-quality` or `/judge-test` reported new entries, so the findings land in the same commit as the change that produced them. If `FIXME.md` was modified but not staged, the subagent stages it before committing.

When the subagent returns:
- If rejected for missing README updates, route back to `/tdd-document`.
- If rejected for any other reason, route back to `/tdd-green`.
- If approved, present the final commit hash and `git status`, then **follow `.claude/git-flow.md` Step 3 (publish: push + MR) and Step 4 (stay on the branch until the MR merges and tests are green)**. Do not duplicate the recipe here — git-flow.md is the source of truth. Do **not** return to `main` immediately after the MR is opened.

After the MR is reported:
- If `/judge-quality` or `/judge-test` added entries to `FIXME.md` this iteration, summarize them (or point the user at the new `FIXME.md` section) and ask whether any should be elevated to `TODO.md` as scheduled follow-up work. Do not move entries without explicit user direction.
- If `/judge-quality` or `/judge-test` also surfaced **Existing** findings (not persisted to `FIXME.md`), present those verbatim and ask the user how to handle each (ignore, follow-up, or open a separate task).
