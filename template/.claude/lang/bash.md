# Bash Conventions

This file is the single source of truth for Bash-specific conventions in this
repo. Load it whenever the change adds or modifies a Bash script (`*.sh`) or a
file whose shebang invokes Bash.

## Commands

```bash
# Lint changed Bash files
shellcheck --shell=bash --severity=warning path/to/script.sh

# Check Bash syntax without executing the script
bash -n path/to/script.sh

# Run the hooks that apply to changed files
pre-commit run --files path/to/script.sh
```

## Detection

- Treat `*.sh` files and files whose first line is `#!/usr/bin/env bash` or
  `#!/bin/bash` as Bash.
- ShellCheck supports POSIX `sh` too, but set the shell dialect from the
  shebang when linting such a file; do not assume Bash syntax for `sh`.
- In a mixed-language change, run every applicable language gate. Do not run
  Python-only tools merely because the repository contains Python.

## Script Conventions

- Start Bash scripts with a Bash shebang and `set -Eeuo pipefail` unless a
  documented compatibility need prevents it.
- Quote parameter expansions and command substitutions. Prefer arrays for
  command arguments; do not construct executable commands in strings or use
  `eval`.
- Use `[[ ... ]]`, `printf`, and lowercase local variables. Declare function
  locals with `local`.
- Validate required inputs and emit errors to stderr. Return meaningful,
  non-zero exit codes on failures.
- Keep side effects at the boundary. Functions should receive values or
  command paths as arguments where practical so their behavior is testable.
- Mutating scripts are safe by default: a bare invocation is a dry run and
  state changes require `--apply` / `-a`. Read-only scripts do not need it.
- Add `--help` for user-facing scripts and document arguments, output, and
  exit behavior.

## Testing

- Exercise both success and failure paths. Use a shell test framework already
  present in the project; otherwise use an isolated temporary directory and
  explicit assertions in a portable test script.
- Never make tests depend on the caller's working tree, environment, network,
  wall clock, or command output unless those dependencies are explicitly
  injected or stubbed.
- Run `bash -n` and ShellCheck before test execution. Tests complement, but do
  not replace, static linting.
