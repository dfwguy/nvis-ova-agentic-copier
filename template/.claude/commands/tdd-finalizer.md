---
description: Final gate — runs applicable Python/Bash checks, commits locally; orchestrator then publishes per git-flow.md
---

Use the Agent tool with `subagent_type: tdd-finalizer` and the current `group_id` to run the applicable Python/Bash checks, non-tox pre-commit, and the Python tox matrix only when Python changed, then create the commit. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- The intended commit message subject + body (the subagent will create the commit).
- Confirmation that prior gates (`nonfunctional-review`, `tdd-judge`, `diataxis-judge`, `judge-quality`, `judge-test`, `judge-cicd`, `cli-syntax-judge`, `filesystem-structure-judge`) approved and `tdd-document` finished.
- The `Created <N> GitLab issues` line from `/judge-quality` **and** `/judge-test` (or `No new issues`), plus created issue URLs and any unfiled findings, so the final report and MR can link them accurately.
- Whether the change touched any user-facing surface (CLI flags, scripts, slash commands, setup steps, env vars). The subagent will verify `README.md` reflects it and reject back to `tdd-document` if not.
- Do NOT pass `--no-verify` or `--no-gpg-sign`. Reject if a hook fails for a real (non-auto-fix) reason.
- Identify whether changed files are Python, Bash, or mixed. Require Python
  changes to run `uv run ruff check`, `uv run ruff format --check`, and
  `uv run tox run-parallel`; require Bash changes to run `bash -n` and
  `shellcheck` on changed shell files. Run non-tox pre-commit after the
  applicable lint gates. Tox is the single Python test execution; do not run
  direct pytest. Stop and return on the first blocking failure; do not rerun a
  failing hook or stage/commit after any failed validation.

When the subagent returns:
- If rejected for missing README updates, route back to `/tdd-document`.
- If rejected for any other reason, route back to `/tdd-green`.
- If approved, present the final commit hash and `git status`, then follow `.claude/git-flow.md` Step 3 (publish: push + MR) and Step 4 (return an `awaiting_human` handoff without waiting for merge). Do not duplicate the recipe here — `git-flow.md` is the source of truth.

After the MR is reported:
- Link every GitLab issue created by `/judge-quality` or `/judge-test` from the MR description or final report.
- Surface **Existing** and unfiled findings verbatim with any precise issue-creation blocker. They remain report-only unless the user directs follow-up; do not create a Markdown backlog file.
