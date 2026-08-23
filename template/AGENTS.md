# AGENTS.md

This repository uses Claude Code as its primary AI coding assistant. If you are another AI coding agent (Codex, Cursor, etc.), this file is your entry point.

@CLAUDE.md

@.claude/lang/python.md

@.claude/lang/bash.md

## Codex workflow

The `.claude/` agents and slash commands are Claude Code configuration. Codex
does not execute their front matter; it follows the shared workflow and
conventions they describe, then uses native Codex subagents when delegation is
useful.

Use this model routing for Codex subagents. These are capability/cost tiers,
not parameter-count equivalences:

| Work | Model | Reasoning effort |
|---|---|---|
| Architecture and code-quality review | `gpt-5.6-sol` | `high` |
| Implementation, tests, documentation, CI review, and test review | `gpt-5.6-terra` | `medium` |
| Focused validation and documentation checks | `gpt-5.6-luna` | `low` |

For a non-trivial change, preserve the same sequence described in `CLAUDE.md`:
design; tests and documentation in parallel; minimum implementation;
validation; independent review; final validation and commit. Keep the
main-agent model for tasks where continuity matters, such as the red/green
handoff. Do not create a second `.claude/` workflow with Codex model names.

## GitLab project IDs

All `glab` API operations must address the numeric project ID; never infer a
repository path from `origin`. Require `GITLAB_HOST` and the numeric
`GITLAB_PROJECT_ID` to be supplied by the environment, and validate the pair
before remote work:

```bash
glab api --hostname "$GITLAB_HOST" "projects/$GITLAB_PROJECT_ID"
```

Use endpoints under `projects/$GITLAB_PROJECT_ID` for merge requests,
pipelines, branch protection, and approval rules. For MR creation, push with
Git first, then call `POST projects/$GITLAB_PROJECT_ID/merge_requests`.

## Mandatory publication handoff

For every user-requested repository change, do not stop at local edits, validation, or a local commit. The main agent must complete `.claude/git-flow.md` Step 3: publish the feature branch and open or update its merge request. The final response must include the direct MR URL and branch tip, unless the user explicitly requested local-only work or publishing is genuinely blocked. In a blocked case, state the exact blocker and the next action; never imply that uncommitted or unpublished work is complete.
