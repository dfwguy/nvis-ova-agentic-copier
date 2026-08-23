---
description: Baseline an existing repository against its checked-in standards and remediate it through the normal quality workflow
---

Use the Agent tool with `subagent_type: remediate` and the current `group_id`.
Brief the subagent fully — it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:

- Treat “Make this repo meet our standards” as a request to meet the
  repository's checked-in standards unless the user supplies another written
  standard.
- Identify the tracked languages and load every applicable convention before
  running checks.
- Require a read-only baseline with the command output/evidence, a Diátaxis
  assessment, and a README-map assessment.
- Require independently reviewable remediation slices with Red tests,
  documentation updates, and validation gates. Do not bulk-fix the repository
  in a single unreviewable edit.

When the subagent returns, present the baseline and begin the listed slices in
order. Each slice uses the normal `/architect` (when non-trivial), `/tdd-red`,
`/diataxis-writer`, `/tdd-green`, judge, finalizer, and publication flow. A
failed baseline is the work queue, not a reason to weaken or skip a standard.
Stop only for a genuine destructive action, ambiguous external standard, or
missing authority; otherwise continue until the final verification matrix
passes and report the evidence.
