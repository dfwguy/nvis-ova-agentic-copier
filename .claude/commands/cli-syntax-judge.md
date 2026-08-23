---
description: CLI design reviewer — validates changed command-line interfaces against CLIG and required safety contracts
---

Use the Agent tool with `subagent_type: cli-syntax-judge` and the current
`group_id` to review the CLI impact of this iteration. Brief the subagent fully
— it has no memory of this conversation.

Task: $ARGUMENTS

Required briefing:
- List all changed files that define, invoke, test, or document a CLI.
- State whether this iteration adds or changes an executable, entry point,
  subcommand, flag, output, exit code, or mutating action.
- Require the subagent to evaluate the change against CLIG and the required
  `--json`, `--help`, `--apply`, dry-run, non-interactive, exit-code, and
  language-tooling contracts.
- The subagent has no Bash — do not ask it to run commands or tooling.

When the subagent returns:
- `Overall: Approve` means this gate is complete, including when no CLI surface
  changed.
- `Overall: Reject` means route automatically to `/tdd-green`; present the
  blocking findings verbatim and do not ask the user.
- Route to `/tdd-finalizer` only once `/judge-quality`, `/judge-test`,
  `/tdd-document`, and `/judge-cicd` have also returned approval.
