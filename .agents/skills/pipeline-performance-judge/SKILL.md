---
name: pipeline-performance-judge
description: Own post-publication GitLab pipeline reconciliation, timing diagnosis, and loop-ready feedback.
metadata:
  model-tier: balanced
---

# Pipeline Performance Judge

Use this portable workflow skill for one complete bounded reconciliation invocation
after a merge request is published. This one agent owns live GitLab
pipeline observation, internal waiting, timing and failure diagnosis, and any
deduplicated feedback issue that should enter the serial work loop. No other
workflow role polls or interprets the live pipeline.

Read and follow [the role instructions](../../agents/pipeline-performance-judge.md)
before acting. That file is a compatibility reference for Claude Code; this
directory is the shared skill entry point for Codex, Pi, and Claude Code.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
