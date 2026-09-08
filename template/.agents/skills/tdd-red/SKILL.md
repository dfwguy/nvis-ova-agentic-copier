---
name: tdd-red
description: Write failing behavior tests before implementation and stop at Red.
metadata:
  model-tier: balanced
---

# Test Writer

Use this portable workflow skill for the test writer phase. Read and
follow [the role instructions](../../agents/tdd-red.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

This continuity-sensitive phase stays with the main agent. Use the current enabled main-session model;
do not switch models mid-session or delegate Red.
The `balanced` metadata is the recommendation when a frontend is selecting the
main model at session start, not an instruction to replace a running model.
