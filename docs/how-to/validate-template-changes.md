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

The local `tox` command remains the supported-matrix test runner. In GitLab CI,
the root pipeline lists eight native jobs: `ruff-check`, `ruff-format`,
`pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`, and
`shellcheck`. They extend the one root `.base_uv` definition, whose image is
pinned by immutable digest, and perform no `apt-get`, `uv python install`, or
`uv sync` bootstrap. There is no synthetic aggregate root validation job or
aggregate JUnit gate suite. Inspect an individual root check through its named
GitLab job. The two root pytest jobs retain separate Python 3.11 and 3.14.3
JUnit and coverage reports.

The generated template deliberately keeps its portable `validation (8 gates)`
aggregate job. It performs the dependency bootstrap once, then reports its
aggregate gate suite and separate pytest reports; it does not consume the root
image or require registry credentials. In both configurations, workflow rules
select a merge-request pipeline and suppress a duplicate branch pipeline while
that merge request is open.

If the change adds or modifies a Bash script, validate that script directly:

```bash
bash -n path/to/script.sh
shellcheck path/to/script.sh
```

The ShellCheck gate automatically ignores `.git`, `.cache`,
`.venv`, `.tox`, `.pytest_cache`, and `.ruff_cache`, because they contain
generated or tool-managed files rather than maintained source. Do not silence
findings in tracked scripts; fix the script or make a documented standards
change instead.

For the exact generated-project contract, see the
[template reference](../reference/template-contract.md). For the distinction
between this template and its generated output, see
[the template explanation](../explanation/template-and-generated-project.md).
