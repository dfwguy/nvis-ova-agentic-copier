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

These rules govern every Markdown document under `docs/`. They are normative:
prefer them over general documentation instincts. This file is the single
source of truth for the Diátaxis process; documentation writers and judges
must read it before editing or reviewing `docs/`.

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

The four types are the top-level navigation. Do not add a fifth top-level docs
directory; group by product, team, or version only within a type directory.
Every documentation page lives in exactly one type directory. The only root
documentation file is `docs/index.md`. Add subdirectories within a type only
after that type exceeds 15 documents.

## Required front matter

Every non-index documentation page begins with `title`, `type`, `summary`,
`audience`, `tags`, `status`, `last_reviewed`, and at least one reciprocal
`related` entry. Use one of these values: `tutorial`, `how-to`, `reference`, or
`explanation`; `new-user`, `practitioner`, `integrator`, or `maintainer`; and
`draft`, `current`, or `deprecated`. Summaries are one sentence under 25 words
and are used verbatim in landing pages. Landing pages use `title` and
`type: index` only.

## Classification and placement

Classify in this order: a fixed learning lesson is a tutorial; a practical goal
with real-world variation is a how-to; machinery consulted for facts is
reference; and rationale, background, or trade-offs are explanation. Split a
page that answers both how and why, then add reciprocal `related` links. If
genuinely ambiguous, use draft how-to status and note it in the merge request.
Titles must signal their type: “Getting started” or “Your first” for tutorials,
“How to” or “Configure” for how-to guides, noun phrases for reference, and
“Understanding”, “Why”, or “architecture” for explanations.

For a new page, classify it, derive a kebab-case slug without type words, add
front matter, make related links reciprocal, and regenerate the landing pages.

## Landing pages

Each type index has two to four editable framing sentences above
`<!-- BEGIN GENERATED -->`. Do not hand-edit its generated block. Group entries
by their most common tag, title-casing that tag; list ungrouped entries under
`### Other`. Each entry is `[title](relative-path.md) — summary`. Omit
deprecated pages from the main listing and include them in a collapsed details
block. `docs/index.md` lists only the four sections, each with framing and a
count. Regenerate indexes after a page is added, removed, renamed, retyped, or
its summary changes.

## Link and orphan audit

Run the audit for every documentation change. Flag orphan pages, broken
relative links, isolated how-to guides, procedural or rationale-heavy
reference pages, branching tutorials, and stale review dates. Regenerate any
landing-page gap and add missing reciprocal `related` links. Do not silently
fix flags. A current page is stale after 365 days or when its subject code
changes.

## Merge and prune proposals

Propose, but never automatically perform, consolidation for same-type pages
with at least 60% heading or command overlap, near-duplicate summaries,
non-reference pages under 150 words, or procedures differing only by variant.
Propose pruning deprecated pages reviewed over 180 days ago, or unlinked pages
for removed features. Use `MERGE: <paths>`, followed by `Reason:` and
`Proposal:` lines.

## Documentation change report

For every documentation change, produce these exact headings: **Classified**
(a `file | type | reason` table), **Written/edited**, **Regenerated**, **Flags**
(a `check | file | detail` table), and **Proposals**. Write `none` below any
empty heading. Never duplicate content across types; link instead. These rules
control structure and placement while house style controls prose and tone.
