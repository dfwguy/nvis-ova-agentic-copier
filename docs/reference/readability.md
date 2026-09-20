---
title: Readability checking
type: reference
summary: Look up the Markdown readability checker, its scopes, and its results.
audience: maintainer
tags: [compliance, writing]
status: current
last_reviewed: 2026-09-19
related:
  - path: license-compliance.md
    why: Describes another read-only repository checker.
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

# Readability checking

The read-only checker limits each selected writing file to a Flesch-Kincaid
grade of 8 or less. It uses `textstat` to score the prose from the whole file.

## Command

Run the checker from the repository root:

```bash
uv run python -m agent_work.readability --root . --scope files --paths docs/guide.md
uv run python -m agent_work.readability --root . --scope changed --base-ref origin/main
uv run python -m agent_work.readability --root . --scope all
```

| Option | Meaning |
| --- | --- |
| `--root <path>` | Sets the root used to find and read files. |
| `--scope files` | Checks paths given with `--paths`. |
| `--scope changed` | Checks files changed from `--base-ref`. |
| `--scope all` | Checks every eligible writing file under the root. |
| `--paths <path> ...` | Supplies paths for `files` scope. |
| `--base-ref <ref>` | Supplies the Git base ref for `changed` scope. |
| `--max-grade <number>` | Sets the highest allowed grade; the default is 8. |
| `--json` | Writes the result as JSON. |
| `--debug` | Adds diagnostic detail. |

The checker has no `--apply` option. It never edits writing files.

## Scope and score

Eligible writing files end in `.md` or `.md.jinja`. The checker scores the
prose taken from each selected file as one whole input. A file passes when its
grade is at or below the limit. A file with no prose also passes.

Markdown extraction keeps human prose. It removes front matter, HTML and
license comments, fenced and indented code, tables, inline code, URLs, link
destinations, and markup before scoring.

The checker now supports all three scopes. The initial gates do not require
every old writing file to pass. A later migration will revise that writing and
then use `all` scope for both gates.

## Results

| Exit status | Meaning |
| --- | --- |
| 0 | All selected files pass, or help was requested. |
| 1 | One or more files exceed the allowed grade. |
| 2 | Command use or an operational check failed. |

Use `--json` when another tool needs the result. Use `--debug` when you need
more detail about selection or extraction.

## Enforcement

Pre-commit checks staged, changed writing files. GitLab CI checks writing files
changed from the GitLab merge-request diff base. These gates check changed
writing only until the planned repository-wide migration is complete.

## Related pages

- Read the [license compliance reference](license-compliance.md) for another
  read-only repository check.
