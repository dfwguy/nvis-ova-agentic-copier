---
name: tdd-green
description: Implement the minimum production change needed to make approved Red tests pass.
metadata:
  model-tier: balanced
---

# Implementer

Use this portable workflow skill for the implementer phase. Read and
follow [the role instructions](../../agents/tdd-green.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

The live Architect delegates this phase after Red and waits while Green owns
all subsequent worktree mutations and review feedback. Request the `balanced`
capability tier defined in `AGENTS.md`.
