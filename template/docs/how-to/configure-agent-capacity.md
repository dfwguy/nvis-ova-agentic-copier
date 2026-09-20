---
title: Configure agent capacity
type: how-to
summary: Set live-agent capacity for a workflow lane.
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

Check capacity now.

Set capacity for the widest row that runs at once in your lane. A full coding
workflow needs **12 total live slots**. Main uses one slot. Architect uses one.
Green uses one. Nine Phase-5 reviewers use the rest. The documentation and
agent-policy lane needs **3 total live slots**. Count the primary thread. Count
every resident ancestor. Do not count only newly spawned agents.

Admission also needs enough retained-agent objects and fresh handles. Check the
global retained-object ceiling. Check its current use. Then check each owner's
direct-child headroom. Check its descendant headroom too. A global slot total
cannot show that a required parent can create its children.

## Configure Codex

Set the user-level Codex configuration in `~/.codex/config.toml`:

```toml
[agents]
max_concurrent_threads_per_session = 15
```

`max_concurrent_threads_per_session` excludes the primary thread. Therefore,
the coding minimum is 11 spawned-agent threads plus the primary thread (12
total). The example allows 15 spawned-agent threads plus the primary thread
(16 total) for headroom. `max_threads` is a legacy alias. Prefer the current
setting above.

After changing the configuration, relaunch Codex or start a new session. Check
trusted runtime metadata. It must report the resulting **total** live capacity.
The configuration file is not capacity evidence. See the [official
Codex configuration reference](https://developers.openai.com/codex/config-reference/)
for current configuration details.

## Configure another frontend

Use that frontend's current capacity setting. Then relaunch it. Or start a new
session. Confirm trusted runtime metadata. It must report the total capacity.
Never use repository files as proof of available slots. Do not use task text.
Do not use issue content.

## Verify every new worker session

Treat capacity as evidence for one active session. It is not a durable machine
setting. Immediately before delegation, fanout, or worktree mutation, check
metadata from the active frontend or runtime. Use metadata for that launch. Record the exact
manifest topology. Record its widest row. For coding work, Main starts
Architect. Architect starts the Phase-2 writers. It starts Green and Finalizer.
Green starts Judge. Green starts the applicable Phase-5 reviewers. Judge starts
the Non-functional Reviewer. Omit a conditional role only when its condition is
demonstrably false.

Confirm these values:

- the selected workflow lane.
- the exact required total derived from the applicable widest row: up to 12
  for coding or 3 for documentation and policy.
- the total available live slots, or `unknown`.
- the shortfall: required total minus available total, with a minimum of zero,
  or `unknown`.
- the retained-object ceiling, current use, and headroom for each new object.
- each planned owner's direct-child and descendant headroom.
- whether every required role has a fresh handle or an explicitly verified
  reuse plan.

Reuse a handle only when the plan verifies the same owner. Verify the same
capability tier. Verify non-overlapping work. Preserve the role's exact skill.
Preserve its parentage, required concurrency, and receipt. Treat Architect and
Green as fresh for each issue. Reuse them only when the plan proves it is safe.

Evidence must name the active runtime source. It must be current for the new
session. Treat it as unavailable when it is absent. Treat it as unavailable
when it describes an earlier session. Do the same when it conflicts with other
current runtime evidence. Do not substitute a repository file. Do not use issue
or task text. Do not use frontend or provider configuration. Do not use retained
ledger or audit data. These sources can help configure the frontend. They cannot
attest the live session.

The same check applies to each launch path:

- For ordinary interactive work, check the newly started frontend session.
- For a retained-worktree retry, check the new worker session. Preserve the
  worktree, but replace every earlier capacity observation with the current one.
- For a systemd activation, require the frontend started by the service to
  expose current runtime metadata. The service environment and timer settings
  are not capacity evidence.

If the runtime reports changed but consistent evidence before work starts, use
the current evidence. Do not average it. Do not fall back to an older value.
Record the successful lane. Record the topology and widest row. Record live-slot
and retained-object evidence. Record owner headroom and any verified reuse plan.
Record the decision with these facts. This keeps the capacity audit coherent.

## Respond when capacity is too low

Stop before you delegate. Stop before you change a worktree. Name the lane.
Name the required total, available total, and shortfall. Give the operator a
remedy. Wait for current evidence. Then start a new session.

Do not serialize a manifest phase that requires concurrency. If evidence is
absent, stop before delegation or worktree mutation. Do the same when evidence
is stale, inconsistent, or insufficient. Return `needs_human` with the lane.
Include the required total and available total (or `unknown`). Include the
shortfall (or `unknown`). Give a remedy for the active frontend. Use `unknown`
for missing or conflicting object or owner headroom. Do not start work until it
is resolved.

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

For a retained worker, follow the same remedy in that launch environment. Do
the same for a systemd-started worker. Start a new worker session. A prior
successful audit does not authorize the retry. The retry proceeds only when
its own current evidence is sufficient. Otherwise, it remains a `needs_human`
handoff. It retains the existing recovery state.
