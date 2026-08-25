---
name: judge-test
description: Perform a read-only review of test quality, coverage, and test practices.
metadata:
  model-tier: balanced
---

# Test Judge

Use this portable workflow skill for the test judge phase. Read and
follow [the role instructions](../../agents/judge-test.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.

