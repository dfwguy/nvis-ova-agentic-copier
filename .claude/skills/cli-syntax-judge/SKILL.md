---
name: cli-syntax-judge
description: Validate CLI changes against the repository's command-line contract after implementation.
metadata:
  model-tier: fast
---

# CLI Judge

Use this portable workflow skill for the cli judge phase. Read and
follow [the role instructions](../../agents/cli-syntax-judge.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `fast` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

