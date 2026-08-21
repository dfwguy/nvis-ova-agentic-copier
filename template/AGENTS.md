# AGENTS.md

This repository uses Claude Code as its primary AI coding assistant. If you are another AI coding agent (Codex, Cursor, etc.), this file is your entry point.

@CLAUDE.md

@.claude/lang/python.md

## Mandatory publication handoff

For every user-requested repository change, do not stop at local edits, validation, or a local commit. The main agent must complete `.claude/git-flow.md` Step 3: publish the feature branch and open or update its merge request. The final response must include the direct MR URL and branch tip, unless the user explicitly requested local-only work or publishing is genuinely blocked. In a blocked case, state the exact blocker and the next action; never imply that uncommitted or unpublished work is complete.
