---
name: tdd-finalizer
description: Run the final language-aware validation gates and report any required reroute.
metadata:
  model-tier: fast
---

# Finalizer

Use this portable workflow skill for the finalizer phase. Read and
follow [the role instructions](../../agents/tdd-finalizer.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `fast` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

