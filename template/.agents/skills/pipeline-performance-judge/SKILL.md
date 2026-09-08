---
name: pipeline-performance-judge
description: Analyze the current merge request pipeline's health and timing after publication.
metadata:
  model-tier: balanced
---

# Pipeline Performance Judge

Use this portable workflow skill for one read-only snapshot within bounded,
post-publication reconciliation of a merge request's GitLab pipeline. The main
agent owns the wait deadline, progress updates, and re-invocation; this judge
owns pipeline evidence and diagnosis. Read and follow [the role
instructions](../../agents/pipeline-performance-judge.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the
shared skill entry point for Codex, Pi, and Claude Code.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
