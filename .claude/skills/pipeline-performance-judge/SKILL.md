---
name: pipeline-performance-judge
description: Analyze the current merge request pipeline's health and timing after publication.
metadata:
  model-tier: balanced
---

# Pipeline Performance Judge

Use this portable workflow skill only for bounded, post-publication analysis of
a merge request's current GitLab pipeline. Read and follow [the role
instructions](../../agents/pipeline-performance-judge.md) before acting. That
file is a compatibility reference for Claude Code; this directory is the
shared skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
