# Contributing

## Branch protection

`main` is protected — no direct commits. All changes go through a merge request.

## Merge requests

- Open an MR from a feature branch (e.g. `claude/<slug>` for AI-driven changes).
- At least one approval is required, and the MR author and committers cannot self-approve.
- A Maintainer merges.

## AI contributions

The shared agent workflow is defined by [`AGENTS.md`](AGENTS.md) and
the [`git-flow` skill](.agents/skills/git-flow/SKILL.md). AI must never run `glab mr
approve` — approval is reserved for human reviewers.
