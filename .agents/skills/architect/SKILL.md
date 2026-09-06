---
name: architect
description: Design non-trivial changes before tests or implementation; use at the start of feature work or refactors.
metadata:
  model-tier: most-capable
---

# Architect

Use this portable workflow skill for the architect phase. Read and
follow [the role instructions](../../agents/architect.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `most-capable` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
