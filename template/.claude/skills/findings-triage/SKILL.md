---
name: findings-triage
description: File deduplicated GitLab Bug issues for actionable Minor judge findings.
metadata:
  model-tier: balanced
---

# Findings Triage

Use this portable workflow skill after judges report their findings. Read and
follow [the role instructions](../../agents/findings-triage.md) before acting.
That file is a compatibility reference for Claude Code; this directory is the
shared skill entry point for Claude Code, Codex, and Pi.

## Model selection

Request the `balanced` capability tier defined in `AGENTS.md`. Each frontend
resolves that tier through its configured model mapping; do not hard-code a
provider-specific model name in this skill.
