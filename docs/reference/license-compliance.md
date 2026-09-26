---
title: License compliance
type: reference
summary: Look up license rules, checked files, and allowed exemptions.
audience: maintainer
tags: [compliance, template]
status: current
last_reviewed: 2026-09-26
related:
  - path: ../explanation/agentic-workflow.md
    why: Explains why final validation uses specialized review roles.
  - path: readability.md
    why: Describes the writing readability check.
  - path: serial-gitlab-agent-loop.md
    why: Defines the Judge validation session that runs the license preflight.
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

# License compliance

This policy checks file headers. It fails on a bad header and does not edit
files.

## Checks

| Point | Rule |
| --- | --- |
| Pre-commit | `license-check` stops a commit with a bad header. |
| GitLab CI | `license-check` checks the submitted tree. |
| Judge preflight | The read-only checker scans the exact staged candidate tree before a durable full-hook session starts. A failed check stops validation before JUnit cleanup or the hook. |
| Final review | `$license-checker` reviews the stable Green diff. Green waits for its result, and `$tdd-finalizer` requires a current passing rerun. |

Fix every failed file and rerun the check after a checked file changes. A fail
is not a waiver.

## Command

Run this from the repository root.

```bash
uv run python scripts/check_licenses.py --root . --policy .license-policy.toml
```

For a staged candidate, pass its exact Git tree ID:

```bash
uv run python scripts/check_licenses.py --root . --policy .license-policy.toml --candidate-tree TREE --json
```

| Item | Use |
| --- | --- |
| `scripts/check_licenses.py --root <path> --policy <path>` | Read-only command. |
| `.license-policy.toml` | Main policy file. |
| `.license-annotations.toml` | File for listed exceptions. |
| `--candidate-tree TREE` | Scan the exact Git tree, including its policy and annotations, instead of checkout content. |
| `--json` | Writes a versioned result. |
| `--debug` | Adds scan detail. |
| `--apply` | Not supported. |

| Exit | Meaning |
| --- | --- |
| 0 | Files pass, or help was asked for. |
| 1 | One or more files fail. With `--candidate-tree`, runtime Git, policy, annotation, or scan errors also exit 1; `--json` reports `outcome: error`. |
| 2 | Invalid command syntax or arguments. In ordinary checkout mode, input, path, scan, or policy errors also exit 2. |

JSON goes to standard output with an empty error stream. Normal passes are
silent. Normal errors and debug notes go to standard error.

## JSON result

The JSON result has `schema_version` 1. Its `outcome` is `compliant`,
`violations`, or `error`. A violation has `path` and `message`; an error has
`error`. This format also covers startup errors.

Judge accepts only a valid `compliant` JSON result with a successful exit
status. A violation, checker error, or malformed result fails the preflight.
After a correction, stage a new tree and validate that tree.

## Scope

The policy checks the root, `.agents`, the Copier payload, and rendered output.
A template file and its output must both pass. Git gives the scan its tracked
paths. Outside Git, the scan uses `.gitignore` rules. It does not follow links
and sorts paths.

With `--candidate-tree`, the checker reads tracked files from that tree. It
does not use unstaged checkout changes or change the tree.

The scan walks from the top down and applies each nested ignore rule in order.
A nested rule can add a path back. The scan prunes a skipped directory only
when no nested rule can add a path back.

## Text and config

The scan reads text and TOML as UTF-8. A bad or unreadable checked file fails.
A bad, missing, or unreadable policy or annotation file is a setup error. It
exits 2 in ordinary checkout mode. With `--candidate-tree`, policy, annotation,
or scan execution errors exit 1; `--json` emits `outcome: error`. The scan does
not use part of a policy or assume an empty annotation list.

## Header rules

| File | Header start |
| --- | --- |
| Normal file | Byte zero. |
| Script with a shebang | After the shebang. |
| Markdown or MDC with front matter | After front matter. |

The full header must be one block at that start. A later, partial, or changed
copy fails. Skill files keep YAML front matter first. Put their Markdown header
right after it with no blank line.

The last notice line ends the header before file text starts. A longer or
embedded copy is invalid.

## Exceptions

| Kind | Required action |
| --- | --- |
| JSON | Add its named annotation. |
| Lock file | Add its named annotation. Do not hand-edit it. |
| Symbolic link | Add its named annotation. Do not replace it. |

An annotation applies to one file. Its TOML must parse and meet its schema. A
missing or bad annotation does not excuse a file. It is not a broad opt-out for
nearby files or later output.

## Failures

A license fail is a policy fail, not style advice. Restore the full NVIDIA
header when a file can hold it. Use an allowed annotation when it cannot. Do
not weaken the policy, hide a fail, or change generated output just to pass.

## CI

The root pipeline has one native `license-check` job. A generated project runs
one `license-check` gate in `validation (10 gates)`. That helper reports the
gate once.
