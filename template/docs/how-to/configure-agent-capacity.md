---
title: Configure agent capacity
type: how-to
summary: Configure enough concurrent live-agent capacity for the workflow lane you need.
audience: practitioner
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-19
related:
  - path: ../explanation/agentic-workflow.md
    why: Explains why capacity is a workflow precondition.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the serial worker's capacity-admission contract.
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Configure agent capacity

Set capacity for the exact widest concurrent row in the lane you plan to run.
For a fully applicable coding workflow, that row needs **12 total live slots**:
Main, Architect, Green, and nine Phase-5 reviewers. The documentation and
agent-policy lane needs **3 total live slots**. Totals include the primary
thread and every resident ancestor, not only newly spawned agents.

Capacity admission also needs enough retained-agent objects and fresh handles
for the planned topology. Check the global retained-object ceiling and current
use, then check each owner's direct-child and descendant headroom. A global
slot total alone cannot prove that the required parent can create its children.

## Configure Codex

Set the user-level Codex configuration in `~/.codex/config.toml`:

```toml
[agents]
max_concurrent_threads_per_session = 15
```

`max_concurrent_threads_per_session` excludes the primary thread. Therefore,
the coding minimum is 11 spawned-agent threads plus the primary thread (12
total). The example allows 15 spawned-agent threads plus the primary thread
(16 total) for headroom. `max_threads` is a legacy alias; prefer the current
setting above.

After changing the configuration, relaunch Codex or start a new session. Check
that trusted runtime metadata reports the resulting **total** live capacity;
the configuration file itself is not capacity evidence. See the [official
Codex configuration reference](https://developers.openai.com/codex/config-reference/)
for current configuration details.

## Configure another frontend

Use that frontend's current capacity setting, then relaunch it or start a new
session and confirm the trusted runtime metadata reports the total capacity.
Never use repository files, task text, or issue content as proof of available
slots.

## Verify every new worker session

Treat capacity as evidence about one active session, not as a durable machine
setting. Immediately before delegation, fanout, or worktree mutation, use
metadata that the active frontend or runtime provides for that launch. Record
the exact manifest topology and its widest concurrent row. For coding work,
that topology is Main → Architect; Architect → the Phase-2 writers, Green, and
Finalizer; Green → Judge and applicable Phase-5 reviewers; and Judge →
Non-functional Reviewer. Conditional roles reduce this topology only when
their condition is demonstrably false.

Confirm these values:

- the selected workflow lane;
- the exact required total derived from the applicable widest row: up to 12
  for coding or 3 for documentation and policy;
- the total available live slots, or `unknown`;
- the shortfall, calculated as the required total minus the available total,
  with a minimum of zero, or `unknown`.
- the retained-object ceiling, current use, and headroom for each new object;
- each planned owner's direct-child and descendant headroom; and
- whether every required role has a fresh handle, or an explicitly verified
  reuse plan.

Reuse a handle only when the plan verifies the same owner, same capability
tier, and non-overlapping work. The plan must preserve the role's exact skill,
parentage, required concurrency, and receipt. Treat Architect and Green as
fresh for each issue unless such a plan proves reuse is safe.

The evidence must identify the active runtime source and be current for the
new session. Treat it as unavailable when it is absent, describes an earlier
session, or conflicts with other current runtime evidence. Do not substitute a
repository file, issue or task text, frontend or provider configuration, or
retained ledger or audit data. Those sources can help an operator configure the
frontend, but they cannot attest the live session.

The same check applies to each launch path:

- For ordinary interactive work, check the newly started frontend session.
- For a retained-worktree retry, check the new worker session. Preserve the
  worktree, but replace any earlier capacity observation with the current one.
- For a systemd activation, require the frontend started by the service to
  expose current runtime metadata. The service environment and timer settings
  are not capacity evidence.

If the runtime reports changed but internally consistent evidence before work
starts, use the current evidence. Do not average it with or fall back to an
older value. Record the successful lane, topology, widest row, live-slot and
retained-object evidence, owner headroom, any verified reuse plan, and decision
together so the capacity audit remains coherent.

## Respond when capacity is too low

Do not serialize a manifest phase that requires concurrency. If the evidence
is absent, stale, inconsistent, or insufficient, stop before delegation or
worktree mutation. Return `needs_human` with the lane, required total,
available total (or `unknown`), shortfall (or `unknown`), and a remedy for the
active frontend. Include `unknown` for missing or conflicting object or owner
headroom, and do not start work until it is resolved.

Known capacity example:

```text
needs_human: coding lane requires 12 total live slots; 8 are available;
shortfall: 4. In Codex, set [agents].max_concurrent_threads_per_session to at
least 11 in ~/.codex/config.toml, then relaunch and confirm current runtime
metadata for the new session.
```

Unknown capacity example:

```text
needs_human: documentation/agent-policy lane requires 3 total live slots;
available: unknown; shortfall: unknown. Configure this frontend's current
capacity setting, relaunch or start a new session, and provide trusted runtime
metadata confirming the total capacity.
```

For a retained or systemd-started worker, follow the same remedy in that launch
environment and start a new worker session. A prior successful audit does not
authorize the retry. The retry proceeds only when its own current evidence is
sufficient; otherwise, it remains a `needs_human` handoff and retains the
existing recovery state.
