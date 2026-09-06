---
name: judge-cicd
description: Review GitLab CI alignment and pipeline health after implementation.
metadata:
  model-tier: balanced
---

# CI/CD Judge

Use this portable workflow skill for the ci/cd judge phase. Read and
follow [the role instructions](../../agents/judge-cicd.md) before acting. That
file is role-reference material; this directory is the shared
skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
