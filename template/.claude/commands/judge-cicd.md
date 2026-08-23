---
description: CI/CD reviewer — verifies .gitlab-ci.yml is in sync with the change and checks pipeline health via glab
---

Use the Agent tool with `subagent_type: judge-cicd` and the current `group_id` to review the CI/CD impact of this iteration. Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List the files changed in this iteration with paths (the subagent will Read them).
- Note whether the change touched `/template/` content (which has its own `.gitlab-ci.yml`) versus repo-level files.
- Mention any new tooling, linters, file types, or scripts that might need a matching CI job.
- Provide `GITLAB_HOST` and `GITLAB_PROJECT_ID`; the subagent uses the
  project-ID `glab api` workaround for pipeline health. Do not also run `glab`
  here.
- Require it to complete the local CI checks first and return immediately on a blocking local or pipeline-health failure.

When the subagent returns:
- If approved, route to `/tdd-finalizer` immediately (after `/judge-quality`, `/judge-test`, `/tdd-document`, and `/cli-syntax-judge` have also returned).
- If rejected for a config gap or remote failure caused by this iteration, route back to `/tdd-green`.
- If rejected for an unrelated remote failure, surface the finding to the user and do not auto-route.
