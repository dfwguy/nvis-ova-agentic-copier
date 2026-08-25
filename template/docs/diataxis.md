# Diátaxis documentation process

Diátaxis is a documentation framework that treats four reader needs as distinct:

- **Tutorials** teach by leading a learner through a successful, repeatable lesson.
- **How-to guides** help a reader complete a specific real-world task.
- **Reference** documents facts completely and accurately for lookup.
- **Explanation** builds understanding by describing concepts, context, and trade-offs.

Do not blend these needs into a single catch-all page. Choose the reader's need
first, then write the page in its appropriate quadrant. A how-to may link to a
reference page, for example, but it should not become one.

## Diagrams in explanations

When an Explanation describes a lifecycle, relationship, or decision path that
is difficult to scan in prose, include a Mermaid flowchart. Use a fenced
`mermaid` block and keep the surrounding prose: the diagram should clarify the
explanation, not replace its rationale. Do not add diagrams merely as
decoration; tutorials, how-to guides, and reference pages should use one only
when it directly helps their reader task.

## Workflow

For non-trivial changes, documentation is built and checked as a first-class
part of the TDD workflow:

1. After the Architect, run the Test Writer and the Documentation Writer in parallel. The
   Test Writer creates and confirms failing behavior tests; the Documentation Writer
   creates or updates the affected `docs/` page in the right quadrant.
2. The Implementer implements only enough code to satisfy the Red tests.
3. Run the Implementation Judge and the Documentation Judge in parallel. The first validates the
   implementation; the second verifies the documentation's Diátaxis category,
   completeness, technical accuracy, links, and consistency with the change.
4. Continue to the quality, test, CI/CD, and landing-README gates only after
   both judges approve. The README Reviewer owns `README.md` only: it makes sure a
   new visitor can understand and start using the project.

The Diátaxis Writer edits `docs/` only. The Diátaxis Judge is read-only and
routes documentation defects back to the writer. Neither changes code, tests,
or `README.md`.

## Documentation index

`docs/README.md` is the documentation landing page. It must link every
Diátaxis page in `docs/`—Tutorials, How-to guides, Reference, and
Explanation—with a brief description that helps readers choose the right page.
It also links the Diátaxis process itself. The index need not duplicate page
contents, but directory links alone are insufficient when they hide individual
pages.

The Diátaxis Writer owns this index. Whenever a change adds, removes, renames,
or materially repurposes a Diátaxis page, it updates `docs/README.md` in the
same change. The Diátaxis Judge and finalizer verify the index is complete.
