---
name: tdd-green
description: Implement the minimum production change needed to make approved Red tests pass.
metadata:
  model-tier: balanced
---

# Implementer

Use this portable workflow skill for the implementer phase. Read and
follow [the role instructions](../../agents/tdd-green.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

