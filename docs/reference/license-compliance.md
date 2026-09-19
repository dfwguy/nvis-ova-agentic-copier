---
title: License compliance
type: reference
summary: Look up the repository license-header policy, checked scope, and documented exemptions.
audience: maintainer
tags: [compliance, template]
status: current
last_reviewed: 2026-09-17
related:
  - path: ../explanation/agentic-workflow.md
    why: Explains why final validation uses specialized review roles.
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

The repository enforces the canonical NVIDIA proprietary license text for
maintained files. The policy is fail-closed: a file in scope passes only when
its required license information is present and valid, or when it has an
explicit, policy-recognized exemption.

## Enforcement points

License compliance is checked in both local and remote validation:

| Point | Purpose |
| --- | --- |
| Pre-commit | The `license-check` hook rejects a proposed change with missing, malformed, or unrecognized license information before it is committed. |
| GitLab CI | The `license-check` gate independently applies the same policy to the submitted repository state. |
| Final validation | The portable, read-only `$license-checker` is an initial Phase-5 reviewer. It runs concurrently with every other applicable reviewer after the serial Phase-4 Judge against the same stable post-Green diff. Green waits for its receipt before consolidating findings, and `$tdd-finalizer` requires a current approving receipt for the final in-scope diff. |

The checker does not modify files. A violation is a required correction, not a
waivable or not-applicable review result. Maintainers correct the file header or
add an allowed annotation, then rerun the checker if maintained files in its
checked scope changed.

It is a read-only repository development/CI helper, rather than a user-facing
operational application. Its ephemeral diagnostics deliberately preserve the
zero-write contract; this narrow classification does not change global logging
policy.

## Checker interface

Run the checker from the repository root with the policy asset:

```bash
uv run python scripts/check_licenses.py --root . --policy .license-policy.toml
```

| Interface | Meaning |
| --- | --- |
| `scripts/check_licenses.py --root <path> --policy <path>` | Read-only checker command. |
| `.license-policy.toml` | Default policy asset. |
| `.license-annotations.toml` | External annotation asset for files that cannot safely contain a header. |
| `--json` | Emits the versioned machine-readable result described below. |
| `--debug` | Adds traversal and classification detail to diagnostic output. |

`--apply` is intentionally unsupported. The checker reports violations and
never inserts, rewrites, removes, or otherwise creates repository files.

| Exit status | Meaning |
| --- | --- |
| 0 | The checked files comply, or help was requested. |
| 1 | One or more license-policy violations were found. |
| 2 | Command usage, path resolution or traversal, or the supplied policy is invalid. |

When `--json` is used, these statuses still apply. JSON is written only to
standard output and standard error is empty. Ordinary successful runs are
silent; ordinary violations, errors, and `--debug` diagnostics are written to
standard error.

## JSON result

The JSON result is an object with `schema_version` set to `1` and an `outcome`
whose value is one of `compliant`, `violations`, or `error`. For a
`violations` outcome, `violations` is an array of objects, each containing a
`path` and `message`. For an `error` outcome, `error` is present. This lets
automation distinguish compliance findings from operational or input errors
without parsing human-readable output.

The versioned `error` envelope applies even when the checker cannot begin the
normal check. Argument or usage parsing, root or policy path resolution,
configuration loading, and traversal failures all exit with status `2` and
write the same envelope in `--json` mode. Callers can therefore handle every
checker failure through one schema-versioned interface rather than treating
setup failures as a separate human-readable protocol.

## Checked scope

The policy covers the repository root, including maintained `.agents` content,
the Copier template payload (including `template/.agents`), and the rendered
output produced from that payload. A template file and the file it produces
must both satisfy the applicable policy; passing the root repository check
alone does not establish rendered-output compliance.

Traversal is deterministic and top-down. In a Git worktree, the checker asks
Git for maintained paths; outside a Git worktree, it applies `.gitignore`
rules from each traversed directory in order, including nested negations. A
nested rule that re-includes a candidate therefore takes effect before that
candidate can be omitted from checking. Excluded directories are pruned only
when no applicable nested rule can re-include a candidate, and symbolic links
are never followed. The checker records the same sorted set of candidates for
a given repository state, making its violations stable for local runs and CI.

## Header placement

A complete canonical header is valid only at a supported file start:

| File form | Valid header location |
| --- | --- |
| Ordinary header-capable file | Byte offset zero. |
| Script with a shebang | Immediately after its one shebang line. |
| Markdown or MDC file with front matter | Immediately after the closing front-matter delimiter. |

A complete header found later in the file is misplaced and fails validation.
This prevents text copied into a body, example, or trailing block from being
mistaken for file ownership information.

The canonical header is a strictly terminated, contiguous block. Its final
notice line must terminate the header before the file body begins; an
incomplete, extended, or embedded copy of the notice is not a valid header.

### Skill files with YAML front matter

Portable skill files preserve their YAML front matter at the start of the
file. For example, keep the opening delimiter, `name`, `description`, and
`metadata` unchanged, close the front matter, and place the Markdown-form
canonical header immediately after that closing delimiter. Do not move the
notice ahead of front matter, put it inside the YAML mapping, or add a blank
line between the delimiter and the notice.

## Text and configuration handling

The checker decodes checked text and its TOML configuration assets explicitly
as UTF-8. An unreadable or non-UTF-8 checked file is reported as a violation;
an unreadable, missing, malformed, or schema-invalid policy or annotation file
is a configuration error. Configuration errors fail closed with exit status 2:
the checker does not continue with a partial policy or an assumed empty set of
annotations.

## Format exceptions

Some tracked formats cannot safely carry a header without changing their
syntax, semantics, or generated ownership. They remain checked and must use an
explicit annotation rather than an implicit exclusion.

| Format or path kind | Required treatment |
| --- | --- |
| JSON | Record the explicit policy annotation; do not insert a comment into JSON. |
| Generated lockfiles | Record the explicit policy annotation; do not hand-edit generated content to add a header. |
| Symbolic links | Record the explicit policy annotation; do not replace the link with a regular file. |

An annotation applies only to the identified file and format limitation. It is
not a general opt-out for adjacent files or future generated output.

The annotation file is required whenever the policy names it. Its TOML must
parse and satisfy the annotation schema before any annotation is honored; a
missing or invalid file never silently exempts a path.

## Failure meaning

A licensing failure identifies a policy violation, not an advisory style
finding. Resolve it by restoring the canonical NVIDIA proprietary license text
for a header-capable file, or by using the documented explicit annotation for a
format that cannot safely embed a header. Do not weaken the policy, suppress a
failure, or alter a generated artifact merely to make validation pass.

## CI integration

The root pipeline keeps `license-check` as a standalone native job. Generated
projects instead run the checker exactly once from the portable `validation
(9 gates)` aggregate helper and do not define a second standalone license job.
The helper dispatches one `license-check` gate and reports that gate once in
its manifest and JUnit reporter output; it must not duplicate the dispatch or
emit a second reporter result for the same gate.
