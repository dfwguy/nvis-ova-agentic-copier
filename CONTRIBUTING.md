# Contributing

## Branch protection

`main` is protected — no direct commits. All changes go through a merge request.

## Merge requests

- Open an MR from a feature branch (e.g. `claude/<slug>` for AI-driven changes).
- At least one approval is required, and the MR author and committers cannot self-approve.
- A Maintainer merges.

## AI contributions

Claude Code drives most of the workflow. See [`.claude/git-flow.md`](.claude/git-flow.md) for the branch / commit / MR / return-to-main lifecycle and [`CLAUDE.md`](CLAUDE.md) for the multi-agent TDD process. AI must never run `glab mr approve` — approval is reserved for human reviewers.
