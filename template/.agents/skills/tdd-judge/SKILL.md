---
name: tdd-judge
description: Run applicable lint and non-test validation gates without editing files.
metadata:
  model-tier: balanced
---

# Implementation Judge

Use this portable workflow skill for the implementation judge phase. Read and
follow [the role instructions](../../agents/tdd-judge.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
