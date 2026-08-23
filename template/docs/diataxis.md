# Diátaxis documentation process

Diátaxis is a documentation framework that treats four reader needs as distinct:

- **Tutorials** teach by leading a learner through a successful, repeatable lesson.
- **How-to guides** help a reader complete a specific real-world task.
- **Reference** documents facts completely and accurately for lookup.
- **Explanation** builds understanding by describing concepts, context, and trade-offs.

Do not blend these needs into a single catch-all page. Choose the reader's need
first, then write the page in its appropriate quadrant. A how-to may link to a
reference page, for example, but it should not become one.

## Workflow

For non-trivial changes, documentation is built and checked as a first-class
part of the TDD workflow:

1. After `/architect`, run `/tdd-red` and `/diataxis-writer` in parallel. The
   Test Writer creates and confirms failing behavior tests; the Diátaxis Writer
   creates or updates the affected `docs/` page in the right quadrant.
2. `/tdd-green` implements only enough code to satisfy the Red tests.
3. Run `/tdd-judge` and `/diataxis-judge` in parallel. The first validates the
   implementation; the second verifies the documentation's Diátaxis category,
   completeness, technical accuracy, links, and consistency with the change.
4. Continue to the quality, test, CI/CD, and landing-README gates only after
   both judges approve. `/tdd-document` owns `README.md` only: it makes sure a
   new visitor can understand and start using the project.

The Diátaxis Writer edits `docs/` only. The Diátaxis Judge is read-only and
routes documentation defects back to the writer. Neither changes code, tests,
or `README.md`.

## README map

`README.md` is the repository's landing page, not a Diátaxis quadrant. It
must contain a **Repository map** that lists every tracked top-level file and
directory, each with a brief explanation of its purpose. Exclude `.git/` and
other untracked/generated working files; list tracked dotfiles too. A directory
may link readers to its own index rather than recursively duplicating every
file beneath it.

`/tdd-document` owns this map. Whenever a change adds, removes, renames, or
materially repurposes a tracked top-level entry, it updates the map in the same
change. `/diataxis-judge` verifies that the Diátaxis documents are accurate;
the README-map check belongs to `/tdd-document` and the finalizer.
