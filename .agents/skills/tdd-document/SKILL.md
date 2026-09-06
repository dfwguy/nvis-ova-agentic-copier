---
name: tdd-document
description: Ensure README.md accurately orients users after a change.
metadata:
  model-tier: balanced
---

# README Reviewer

Use this portable workflow skill for the readme reviewer phase. Read and
follow [the role instructions](../../agents/tdd-document.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
