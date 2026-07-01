# Test-Driven Development

Language-agnostic TDD discipline for this repo. The full multi-agent routing (which agent runs when) lives in `CLAUDE.md` → "Multi-Agent Workflow"; this file is the underlying principle every contributor and tool follows.

## Always

- Write a failing test before any implementation — no exceptions
- Confirm the test is Red (failing) before switching to implementation
- Write the minimum code to make the test pass — nothing more
- Confirm Green (all tests passing) before refactoring
- Refactor only while tests remain Green

## Phases and roles

Work in explicit phases with distinct roles:

- **Test Writer** — writes failing tests that define the desired behavior; stops when tests are confirmed Red
- **Implementer** — writes minimal code to pass the failing tests; stops when tests are confirmed Green
- **Judge** — validates the full cycle: meaningful tests, clean implementation, all tooling passing, conventions followed

## Never

- Write implementation code before a failing test exists
- Skip the Red confirmation step
- Add functionality not covered by a test
- Suppress a failing test — fix the code instead
- Approve (Judge) code that has linter, type-checker, or test failures
