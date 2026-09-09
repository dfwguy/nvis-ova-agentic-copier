---
name: judge-cicd
description: Review repository CI configuration alignment and static job shape after implementation.
metadata:
  model-tier: balanced
---

# CI/CD Judge

Use this portable workflow skill for the read-only CI configuration review.
This phase inspects repository configuration only; it does not inspect live
GitLab state. Read and follow [the role instructions](../../agents/judge-cicd.md)
before acting. That file is role-reference material; this directory is the
shared skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
