---
description: Documentation validator — verifies docs/ follows Diátaxis and matches the change
---

Use the Agent tool with `subagent_type: diataxis-judge` and the current
`group_id` to validate `docs/` in a fresh subagent context. Launch it in
parallel with `/tdd-judge` after `/tdd-green`; the subagent has no memory of
this conversation.

Task: $ARGUMENTS

Required briefing:
- List files changed in this iteration, including `docs/` files.
- Paste the architect's design and the Diátaxis Writer's report.
- Require a read-only Diátaxis review: category fit, technical accuracy,
  coverage, and link validity.

When the subagent returns:
- If it rejects, present the first blocking issue verbatim and route
  automatically to `/diataxis-writer`.
- If it approves, wait for `/tdd-judge` to approve. Only then fan out to
  `/judge-quality`, `/judge-test`, `/tdd-document`, and `/judge-cicd`.
