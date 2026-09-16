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

## Plain language

Before writing or reviewing `docs/` prose, load `$simplifying-writing`. Write
for the reader with direct, specific, concise language. Prefer common, concrete
words; define unavoidable technical terms; and use active, direct voice where
natural. Keep sentences and paragraphs short and focused. Remove filler and
repetition, but do not omit facts, technical notation, prerequisites,
constraints, warnings, commands, names, or links merely to make text shorter.

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

Every non-index documentation page begins with:

```yaml
---
title: Human-readable title
type: tutorial | how-to | reference | explanation
summary: One sentence, under 25 words, describing what the reader gets.
audience: new-user | practitioner | integrator | maintainer
tags: [tag-a, tag-b]
status: draft | current | deprecated
last_reviewed: YYYY-MM-DD
related:
  - path: ../reference/some-doc.md
    why: Short reason for the link.
---
```

Landing pages use only their documented `title` and `type: index` front
matter. Their summaries come from the pages they list. Rewrite missing or
generic summaries before doing other documentation work.

## Classification and placement

Classify a page in this order; the first match wins:

| Reader need | Type |
| --- | --- |
| Learning by doing through a fixed, guaranteed outcome | `tutorial` |
| Accomplishing a real-world goal with variation | `how-to` |
| Looking up APIs, CLIs, configuration, or schemas | `reference` |
| Understanding rationale, trade-offs, or background | `explanation` |

Split pages that answer both “how do I?” and “why?” into a how-to and an
explanation, then cross-link them with reciprocal `related` entries. Titles
must match their type: tutorials use “Getting started” or “Your first”; how-to
pages use “How to” or “Configure”; reference pages use noun phrases; and
explanations use “Understanding”, “Why”, or “architecture”. If still ambiguous,
default to `how-to`, set `status: draft`, and note the ambiguity in the merge
request rather than silently guessing.

For a new page, classify it, derive a kebab-case slug without type words, add
the front matter and at least one `related` entry, add reciprocal entries to
its related pages, and regenerate the landing pages.

## Landing pages

Each `<type>/index.md` has editable framing followed by a generated block:

```markdown
<!-- framing: hand-written, 2-4 sentences, EDITABLE -->

<!-- BEGIN GENERATED -->
### Tag name
- [Page title](page.md) — Summary from front matter.
<!-- END GENERATED -->
```

Group pages by their most common tag, title-casing the tag for the heading; put
ungrouped pages under `### Other`. Omit deprecated pages from the main list and
place them in a collapsed `<details>` block at the end. Do not hand-edit below
the marker. `docs/index.md` lists only the four sections, each with framing and
a count. Regenerate landing pages after any add, removal, rename, retype, or
summary change.

## Link and orphan audit

Run this audit for every `docs/` change. Flag or fix the following as stated:

| Check | Condition | Action |
| --- | --- | --- |
| Orphan | No inbound link from a documentation page or landing page | Flag |
| Landing-page gap | Page is not on its type landing page | Fix by regenerating |
| Broken link | A relative target does not resolve | Flag |
| Non-reciprocal `related` | A declared relation lacks a reciprocal entry | Fix |
| Isolated how-to | No related link to reference or explanation | Flag |
| Reference bloat | Reference contains procedures or rationale | Flag: extract |
| Tutorial branching | Contains “if you want to”, “optionally”, or “depending on” | Flag |
| Stale | Reviewed over 365 days ago, or current subject code changed | Flag |

## Merge and prune proposals

Propose consolidation for same-type pages with at least 60% heading or command
overlap, near-duplicate summaries, a non-reference page under 150 words, or
procedures differing only by version, environment, or product variant. Propose
pruning a deprecated page reviewed more than 180 days ago, or an unlinked page
whose feature no longer exists. Never merge or delete automatically; emit:

```text
MERGE: how-to/rotate-keys.md + how-to/rotate-keys-staging.md
  Reason: identical procedure, differs only by environment.
  Proposal: keep rotate-keys.md, add an environment variant table, delete the other, add redirect.
```

## Documentation change report

For every documentation change, produce exactly these headings: **Classified**
(a `file | type | reason` table), **Written/edited**, **Regenerated**, **Flags**
(a `check | file | detail` table), and **Proposals**. Write `none` below any
empty heading.

Never repeat content across types: link instead. Structure follows the
reader's mode, not the organisation chart or codebase. When these rules
conflict with a house style guide, these rules control structure and placement;
the house guide controls prose and tone.
