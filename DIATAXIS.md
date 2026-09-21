<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Documentation Structure Rules

These rules govern Markdown in `docs/`. They are required.
Use them before general writing habits. This file is the source.
Writers and judges must read it before working in `docs/`.

## Plain language

Load `$simplifying-writing` before prose work in `docs/`. Write for the reader.
Use direct and short words. Define needed terms. Use active voice where natural.
Keep sentences and paragraphs short. Remove filler. Keep facts, notation,
needs, limits, warnings, commands, names, and links.

## Canonical layout

```text
docs/
  index.md
  tutorials/
    index.md
    <slug>.md
  how-to/
    index.md
    <slug>.md
  reference/
    index.md
    <slug>.md
  explanation/
    index.md
    <slug>.md
```

The four types are top-level navigation. Do not add a fifth docs directory.
Group by product, team, or version within a type. Each page has one type.
The only root file is `docs/index.md`. Add a subdirectory only after a type has
more than 15 pages.

## Required front matter

Every non-index page must begin with `title`, `type`, `summary`, `audience`,
`tags`, `status`, `last_reviewed`, and `related`. `related` is a non-empty list
of reciprocal objects. Each has a `path` and a short `why`. Type is `tutorial`,
`how-to`, `reference`, or `explanation`. Audience is `new-user`,
`practitioner`, `integrator`, or `maintainer`. Status is `draft`, `current`,
or `deprecated`. A summary is one sentence of under 25 words. Landing pages
use it as written. Landing pages use only `title` and `type: index`. Rewrite a
missing or generic summary before other documentation work.

## Classification and placement

Every page lives in exactly one type directory. Classify in this order; the
first match wins. A fixed learning lesson with a guaranteed outcome is a
tutorial. A real-world task with variation is a how-to. APIs, CLIs,
configuration, and schemas are reference. Rationale, trade-offs, and background
are explanation. Split a page that answers both how and why. Add reciprocal
`related` links. When unsure, use draft how-to status. Note this in the merge
request. Tutorials use
“Getting started” or “Your first”. How-to guides use “How to” or “Configure”.
Reference titles are nouns. Explanations use “Understanding”, “Why”, or
“architecture”.

For a new page, classify it. Make a kebab-case slug without type words. Add
front matter and reciprocal links. Regenerate the landing pages.

## Landing pages

Each `<type>/index.md` has two to four editable framing sentences above this
generated block:

```markdown
<!-- framing: hand-written, 2-4 sentences, EDITABLE -->

<!-- BEGIN GENERATED -->
### Tag name
- [Page title](page.md) — Summary from front matter.
<!-- END GENERATED -->
```

Do not hand-edit below the marker. Group entries by their most common tag.
Title-case the tag. Use `### Other` for ungrouped entries. Keep deprecated pages
off the main list. Put them in collapsed `<details>` at the end.
`docs/index.md` lists only four sections, each with framing and a count.
Regenerate after an add, removal, rename, retype, or summary change.

## Link and orphan audit

Run this audit for every `docs/` change. Flag or fix each result as shown.

| Check | Condition | Action |
| --- | --- | --- |
| Orphan | No inbound link from a doc or landing page | Flag |
| Landing-page gap | Page is not on its type landing page | Regenerate |
| Broken link | A relative target or anchor does not resolve | Flag |
| Non-reciprocal `related` | A relation lacks a reciprocal entry | Fix |
| Isolated how-to | No related reference or explanation link | Flag |
| Reference bloat | Reference has procedures or rationale | Flag: extract |
| Tutorial branching | Has “if you want to”, “optionally”, or “depending on” | Flag |
| Stale | Reviewed over 365 days ago, or subject code changed | Flag |

Do not silently fix flags. Regenerate a missing landing entry. Add a missing
reciprocal link and reason.

## Merge and prune proposals

Propose merges. Never do them automatically. Propose them for same-type pages
with 60% heading or command overlap, near-duplicate summaries, non-reference
pages under 150 words, or variant-only procedures. Propose pruning a deprecated
page reviewed over 180 days ago. Also propose it for an unlinked page whose
feature no longer exists. Use this exact report form:

```text
MERGE: how-to/rotate-keys.md + how-to/rotate-keys-staging.md
  Reason: identical procedure, differs only by environment.
  Proposal: keep rotate-keys.md, add an environment variant table, delete the other, add redirect.
```

## Documentation change report

For every docs change, use exactly these headings: **Classified** (a
`file | type | reason` table), **Written/edited**, **Regenerated**, **Flags**
(a `check | file | detail` table), and **Proposals**. Write `none` below an
empty heading. Do not repeat content across types. Link instead. Structure
follows the reader's mode, not the organisation chart or codebase. These rules
control structure and placement. House style guides prose and tone.
