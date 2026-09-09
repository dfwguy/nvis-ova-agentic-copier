---
title: Validate template changes
type: how-to
summary: Run the checks required before proposing a template change.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-09
related:
  - path: ../reference/template-contract.md
    why: Defines the template behavior those checks protect.
  - path: refresh-ci-image.md
    why: Publishes the dependency image used to accelerate these checks.
---

# Validate template changes

Use this when you have changed this
Copier template and need to verify the change before opening a merge request.

From the repository root, run the local equivalents of the GitLab validation
checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run tox
uv run yamllint -d '{"extends": "default", "rules": {"line-length": "disable", "document-start": "disable"}}' -f parsable .
uv run kingfisher scan . --git-history none --no-validate --no-update-check --exclude .git --exclude .venv --exclude .tox --exclude .pytest_cache --exclude .ruff_cache
```

Generated projects use one portable `validation (8 gates)` GitLab job. It
creates locked Python 3.11 and 3.14.3 environments once, runs all eight gates,
and publishes a named JUnit gate suite plus separate pytest reports. This keeps
the generated pipeline efficient without requiring a project-private image or
registry credentials. Workflow rules prefer a merge-request pipeline and
suppress the duplicate branch pipeline while that merge request is open.

If the change adds or modifies a Bash script, validate that script directly:

```bash
bash -n path/to/script.sh
shellcheck path/to/script.sh
```

The ShellCheck gate automatically ignores `.git`, `.cache`, `.venv`, `.tox`,
`.pytest_cache`, and `.ruff_cache`, because they contain generated or
tool-managed files rather than maintained source. Do not silence findings in
tracked scripts; fix the script or make a documented standards change instead.

For the exact generated-project contract, see the
[template reference](../reference/template-contract.md). For the distinction
between this template and its generated output, see
[the template explanation](../explanation/template-and-generated-project.md).
