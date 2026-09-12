---
title: Configure agent capacity
type: how-to
summary: Configure enough concurrent live-agent capacity for the workflow lane you need.
audience: practitioner
tags: [agent-workflow]
status: current
last_reviewed: 2026-09-12
related:
  - path: ../explanation/agentic-workflow.md
    why: Explains why capacity is a workflow precondition.
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

Set capacity for the widest concurrent phase of the lane you plan to run. The
coding lane needs **13 total live slots**; the documentation and agent-policy
lane needs **3 total live slots**. Totals include the primary thread and every
resident ancestor, not just newly spawned agents.

## Configure Codex

Set the user-level Codex configuration in `~/.codex/config.toml`:

```toml
[agents]
max_concurrent_threads_per_session = 15
```

`max_concurrent_threads_per_session` excludes the primary thread. Therefore,
the coding minimum is 12 spawned-agent threads plus the primary thread (13
total). Set 15 spawned-agent threads plus the primary thread (16 total) for
headroom. `max_threads` is a legacy alias; prefer the current setting above.

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

## Respond when capacity is too low

Do not serialize a manifest phase that requires concurrency. Stop before
delegation and return `needs_human` with the lane, required total, available
total (or `unknown`), shortfall (or `unknown`), and a remedy for the active
frontend.

Known capacity example:

```text
needs_human: coding lane requires 13 total live slots; 8 are available;
shortfall: 5. In Codex, set [agents].max_concurrent_threads_per_session to at
least 12 in ~/.codex/config.toml, then relaunch and confirm runtime metadata.
```

Unknown capacity example:

```text
needs_human: documentation/agent-policy lane requires 3 total live slots;
available: unknown; shortfall: unknown. Configure this frontend's current
capacity setting, relaunch or start a new session, and provide trusted runtime
metadata confirming the total capacity.
```
