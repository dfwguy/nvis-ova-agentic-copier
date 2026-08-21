---
description: Documentation writer — creates or updates Diátaxis documentation in docs/ alongside tdd-red
---

Use the Agent tool with `subagent_type: diataxis-writer` and the current
`group_id` to write `docs/` documentation in a fresh subagent context. Launch
it in parallel with `/tdd-red` after `/architect`; the subagent has no memory
of this conversation.

Task: $ARGUMENTS

Required briefing:
- Paste the architect's design or agreed behavior.
- List changed or planned user and maintainer surfaces.
- Identify existing `docs/` pages that may own the topic.
- Require a single Diátaxis category for every new page: Tutorial, How-to
  guide, Reference, or Explanation.
- State that `README.md`, code, tests, and agent definitions are out of scope.

When the subagent returns, present the changed documentation files and their
categories (or its reason that no `docs/` update is needed). After
`/tdd-green`, launch `/diataxis-judge` in parallel with `/tdd-judge`.
