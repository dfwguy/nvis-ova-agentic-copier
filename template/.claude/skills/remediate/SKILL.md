---
name: remediate
description: Assess an existing repository against its configured standards and sequence repairs.
metadata:
  model-tier: most-capable
---

# Remediator

Use this portable workflow skill for the remediator phase. Read and
follow [the role instructions](../../agents/remediate.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `most-capable` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

