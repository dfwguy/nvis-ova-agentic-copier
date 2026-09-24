---
title: Configure agent capacity
type: how-to
summary: Set live-agent capacity for a workflow lane.
audience: practitioner
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-24
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

Each lane has one fixed global limit. Code work needs **12 total live slots**.
Docs and agent-policy work needs **5 total live slots**. Do not make another
limit from the route or current use. Do not use retained agents, fresh handles,
or parent and child space to make one.

## Configure Codex

Set the user-level Codex configuration in `~/.codex/config.toml`:

```toml
[agents]
max_concurrent_threads_per_session = 15
```

`max_concurrent_threads_per_session` does not count the primary thread. Code
work thus needs 11 spawned threads plus the primary thread. That makes 12. The
example gives 15 spawned threads plus the primary thread. That makes 16 and
leaves room to spare. `max_threads` is an old alias. Use the setting above.

After the change, relaunch Codex or start a new session. Use a trusted current
capacity report when the active runtime gives one. The config file is not
capacity proof. See the [official
Codex configuration reference](https://developers.openai.com/codex/config-reference/)
for current configuration details.

## Configure another frontend

Use that frontend's current capacity setting. Then relaunch it or start a new
session. Use a trusted current report when the frontend or runtime gives one.
Repo files, task text, and issue text cannot prove that slots are free.

## Verify interactive Architect admission

This rule applies to an Architect started directly in an interactive session.
It also applies after a serial wrapper passes preflight and delegates Architect.
Capacity belongs to one launch. It is not a saved machine setting. Choose the
lane before you delegate, fan out, or change the worktree. Use a trusted current
global report if the active frontend or runtime gives one.

- Admit coding when a current report shows at least 12 total live slots.
- Admit docs and agent-policy work when it shows at least 5.
- Stop when a current report shows fewer slots than the fixed requirement.
- Continue when global capacity is not reported. Missing facts are not a
  shortage.

At this boundary, do not ask for or work out another capacity value. Route
shape, topology, retained-agent counts, current use, fresh handles, and owner
space do not control admission. Do not use them as diagnostics for a second
gate.

Use a newer trusted report in place of an older one. Repo files, issue or task
text, settings, service setup, and saved records cannot prove live capacity.

## Check serial wrapper admission

The serial wrapper uses the same fixed global requirement when it delegates
Architect. This applies to terminal, normal serial, retained-worktree retry,
and systemd runs. Use a current trusted global report when the active frontend
or runtime provides one. Continue when it does not provide a report.

Do not request or infer topology, retained-object, or owner-headroom counts for
the wrapper. A current report below the selected lane's fixed requirement
returns `needs_human` before delegation or a worktree change. A prior report
does not authorize a new run.

## Respond when capacity is too low

Stop before delegation or a worktree change when a trusted current report shows
too few slots. Name the lane, fixed limit, reported total, and shortfall. Give
the operator a fix. Do not run concurrent work in series. Also stop when you
cannot safely resolve a real delegation or tool refusal.

Known capacity example:

```text
needs_human: coding lane requires 12 total live slots; 8 are available;
shortfall: 4. In Codex, set [agents].max_concurrent_threads_per_session to at
least 11 in ~/.codex/config.toml, then relaunch and confirm current runtime
metadata for the new session.
```

Use the same fixed global rule for every Architect launch. Missing global
capacity evidence is not a `needs_human` handoff.
