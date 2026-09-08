---
title: Why this repository uses Diátaxis
type: explanation
summary: Understand why the documentation separates reader needs.
audience: maintainer
tags: [template-architecture]
status: current
last_reviewed: 2026-09-06
related:
  - path: coding-standards.md
    why: Explains the conventions that apply this documentation structure.
---

# Why this repository uses Diátaxis

This page explains why the repository
organizes its documentation around reader needs instead of collecting every
topic in a single guide.

[Diátaxis](https://diataxis.fr/) is a documentation framework that separates
four distinct reader needs.

People reach documentation with different intentions. A new user wants a
guided success before they need every option. Someone already working with a
project wants the shortest reliable path to a task. A maintainer needs exact
facts to look up. A contributor deciding whether a convention makes sense needs
context and trade-offs. Those are different questions, and one page rarely
answers all of them well.

Diátaxis gives those questions separate homes:

- **Tutorials** help a learner gain confidence through a complete lesson.
- **How-to guides** help a reader accomplish a particular task.
- **Reference** provides precise facts without a narrative detour.
- **Explanation** supplies the context and rationale behind a design.

The separation is useful for a Copier template because its audience changes
with the task. A team creating a project needs a tutorial or a how-to. A team
updating one needs the exact Copier contract. A maintainer evaluating the
template or its standards needs an explanation. Classifying a page before
writing keeps each path short and lets the pages link to one another instead of
duplicating content.

This is not a rule against cross-links or a demand for artificial isolation. A
how-to should link to the reference it relies on, and an explanation can point
to the tutorial that makes the concept concrete. The boundary is about the
page's primary job: a task guide should not turn into an API catalog, and a
reference should not hide its facts inside a long lesson.

The repository makes this operational rather than aspirational. Its workflow
assigns documentation writing and a read-only documentation review alongside
the normal development checks. The writer chooses the affected quadrant and
the judge verifies category fit, technical accuracy, completeness, and links.
That makes documentation a reviewed part of a change rather than a final,
undifferentiated add-on.

For the process used by contributors, see the
[Diátaxis documentation process](../../DIATAXIS.md). To browse the current
quadrants, start at the [documentation index](../index.md). For another
example of an Explanation page, see
[why the coding standards exist](coding-standards.md).
