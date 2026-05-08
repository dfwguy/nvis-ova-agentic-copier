---
description: CI/CD reviewer — verifies .gitlab-ci.yml is in sync with the change and checks pipeline health via glab
---

Use the Agent tool with `subagent_type: judge-cicd` to review the CI/CD impact of this iteration. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration with paths (the subagent will Read them).
- Note whether the change touched `/template/` content (which has its own `.gitlab-ci.yml`) versus repo-level files.
- Mention any new tooling, linters, file types, or scripts that might need a matching CI job.
- The subagent runs `glab` for pipeline health; do not also run `glab` here.

When the subagent returns:
- If approved, route to `/tdd-finalizer` immediately (after `/judge-quality` and `/tdd-document` have also returned).
- If rejected for a config gap or remote failure caused by this iteration, route back to `/tdd-green`.
- If rejected for an unrelated remote failure, surface the finding to the user and do not auto-route.
