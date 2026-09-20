<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Bash Conventions

This file is the single source of truth for Bash conventions in this repo. Load it
when a change adds or modifies a Bash script (`*.sh`) or a file whose shebang
invokes Bash.

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

- Treat `*.sh` files as Bash. Also treat files whose first line is
  `#!/usr/bin/env bash` or `#!/bin/bash` as Bash.
- ShellCheck also supports POSIX `sh`. Set the shell dialect from the shebang
  when linting that file. Do not assume Bash syntax for `sh`.
- In a mixed-language change, run each applicable language gate. Do not run
  Python-only tools just because the repository has Python.

## Script Conventions

### Filesystem structure

- Put user-facing Bash CLI code behind exactly one user-facing entry point in
  `bin/<tool>`. Do not add additional user-executable wrappers.
- Keep sourceable implementation and library code in a separate `src/` or
  `lib/` directory. Keep tests outside production code.
- Clearly labeled development and CI helpers are not user-facing CLIs. Do not
  put them in `bin/`.
- A nonstandard layout is allowed only when `.agents/project.md` records the
  language, replaced rule, actual paths, and rationale.

- Start Bash scripts with a Bash shebang and `set -Eeuo pipefail`. A documented
  compatibility need may prevent this.
- Quote parameter expansions and command substitutions. Prefer arrays for
  command arguments. Do not build executable commands in strings or use `eval`.
- Use `[[ ... ]]`, `printf`, and lowercase local variables. Declare function
  locals with `local`.
- Validate required inputs and send errors to stderr. Return meaningful non-zero
  exit codes on failure.
- Keep side effects at the boundary. Where practical, functions receive values
  or command paths as arguments so their behavior is testable.
- Mutating scripts are safe by default. A bare invocation is a dry run. State
  changes require `--apply` / `-a`. Read-only scripts do not need it.
- Add `--help` for user-facing scripts. Document arguments, output, and exit
  behavior.

## Testing

- Exercise success and failure paths. Use a shell test framework already in the
  project. Otherwise, use an isolated temporary directory and explicit
  assertions in a portable test script.
- Never make tests depend on the caller's working tree, environment, network,
  wall clock, or command output. The dependency must be injected or stubbed.
- Run `bash -n` and ShellCheck before tests. Tests complement static linting.
  They do not replace it.
