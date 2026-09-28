<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# Consistency Judge evaluation scenarios

These interactive examples start with an approved design and one staged tree. Each receipt
names the base, tree OID, source `path:line`, search scope, and any wrong
`path:line`. These values are examples. They do not set repo rules.

| Changed fact | Evidence to inspect | Expected verdict |
| --- | --- | --- |
| Agent count changes from 11 to 12 | Compare the authoritative admission rule with the topology, capacity guide, tables, and template copies. | Reject when a relevant guide still says 11; report both values and capacity impact. |
| A workflow stage is renamed | Compare the router's exact skill ID and stage name with role links, receipt examples, diagrams, and template copies. | Reject an old name that would route or describe the wrong stage; accept an evidenced historical name in a clearly historical example. |
| A bound changes from 60 to 90 seconds | Compare the project-owned limit or configuration with validation rules, CI, docs, and template copies that use that limit. | Reject a relevant old bound; identify its owner and timing impact. |
| An enum or list gains a member | Compare the owning schema or parser with assertions, fixtures, CLI help, docs, and generated-project contracts. | Reject a missing supported member; accept a narrower list only with an explicit scoped exception. |
| A CLI contract changes | Compare the parser and entry point with `--help`, reference docs, examples, and contract tests. | Reject a documented or tested command that no longer matches the interface. |
| Root policy changes but its template copy does not | Compare the declared root/template pair byte-for-byte and inspect generated-project scope. | Reject drift with both paths and the downstream policy impact. |
| A generated dependency group includes its own project | Compare `template/pyproject.toml.jinja` with rendered `pyproject.toml` and `uv.lock` after trusted tasks. Check `tests/test_template.py` for the self-edge contract. | Reject a self-edge in the rendered lockfile even if the template contains no literal project name. |
| A hook or tox version changes in one place | Compare `pyproject.toml`, `template/pyproject.toml.jinja`, `.pre-commit-config.yaml`, their template counterparts, and `.gitlab-ci.yml` where they affect the same check. Inspect the rendered config when Jinja is involved. | Reject a missing or stale consumer check; accept distinct root and generated values only with their owner, rationale, and scope. |
| A template file is skipped on update | Check `copier.yaml` `_skip_if_exists` and render with the relevant answers and task mode. Compare the affected generated file on fresh copy and on update. | Reject a claim that every existing project receives the changed config when Copier preserves that file. |
| Root and generated package configuration differ | Compare root `pyproject.toml` with `template/pyproject.toml.jinja` and rendered output. The root packages `agent_work`; generated projects package their slug and `agent_work`, with slug coverage. | Accept the scoped difference when the owning project context and config lines explain it; reject an unexplained new mismatch. |
| Root and generated development groups differ | Compare root `pyproject.toml` groups `test`, `tooling`, `typing`, and `dev` with the template's `test` and `dev`. Check the generated project's rendered groups and the project context for ownership. | Accept these scoped groups when the root and generated project owners and purposes explain them; reject a new difference without owner, rationale, and scope. |

If the source `path:line`, relevant search scope, render, exception, or tree
proof is missing, stale, or incomplete, return `Overall: Reject`.
Name the gap. Return `Overall: Approve` only for the tree you checked.
