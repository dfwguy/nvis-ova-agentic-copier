---
description: Review shared timed test results for applicable non-functional requirements before validation
---

Use the Agent tool with `subagent_type: nonfunctional-review` and the current
`group_id` after `/tdd-green` produces `test-results.xml`. Brief the subagent
fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- State the non-functional requirements and any agreed performance budgets or
  baselines. Do not ask the reviewer to infer a threshold.
- List the changed tests and the Architect's relevant design decisions.
- Confirm `/tdd-green` completed successfully and wrote the shared
  `test-results.xml` JUnit artifact.

When the subagent returns:
- **`Overall: Reject`** — route test gaps to `/tdd-red` and implementation or
  performance-budget failures to `/tdd-green` automatically.
- **`Overall: Approve`** — retain its timing report in the work-item handoff,
  then start `/tdd-judge` and `/diataxis-judge` in parallel.
