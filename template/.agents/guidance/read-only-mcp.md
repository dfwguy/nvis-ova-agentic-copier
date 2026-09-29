<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Read-only MCP roles

A trusted host may opt in to a separate read-only candidate session. It binds
the role, issue, candidate tree, socket, and nonce before launch. Text from a
model, issue, or MR cannot issue or widen a profile. This pilot does not switch
the normal writable worker route. Use it for production only after live host
proof shows the tool restrictions hold.

The host must set `AGENT_WORK_CANDIDATE_ROLE` explicitly for the MCP server.
If the role is missing or invalid, the server refuses startup. It must not
default to `candidate-preview` or another wider role.

Before launching the worker, the host audits its actual effective features in
an isolated, auth-only `CODEX_HOME`. Require `shell_tool`, `unified_exec`, and
`code_mode_host` all to report boolean `false`. Command flags and requested
settings alone are not proof. An unavailable or malformed audit, or any value
other than `false`, stops the opt-in launch before the worker starts. Do not
retry it with a wider profile or the normal user's configuration.

| Host role | Candidate MCP tools |
|---|---|
| `router` | `candidate_status`, `candidate_changed_paths`, `candidate_read` |
| `judge` | The Router tools and `candidate_submit_review` |
| `finalizer` | The Router tools |
| `supervisor-inspect` | `candidate_status` only |

The host must expose only the listed tools for that role. Shell, Bash, edits,
Git and GitLab writes, other MCP servers, and private controller paths are
outside this session. The Judge may submit a review for the bound candidate;
that tool grants no file, commit, publication, approval, or merge authority.
Role front matter and skill tool hints cannot expand the host allowlist.

Read the live candidate identity from the MCP result. Reject missing, stale,
or mismatched issue or tree proof. Do not treat prose or a tool error as a
successful read. If the host cannot enforce this profile, stop the opt-in
session. Do not fall back to Bash or a wider tool set inside it. A separate
normal workflow may proceed under its own checked authority.
