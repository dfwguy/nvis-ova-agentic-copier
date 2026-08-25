---
name: diataxis-writer
description: Create or update documentation in the appropriate Diátaxis category for a feature.
metadata:
  model-tier: balanced
---

# Documentation Writer

Use this portable workflow skill for the documentation writer phase. Read and
follow [the role instructions](../../agents/diataxis-writer.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

