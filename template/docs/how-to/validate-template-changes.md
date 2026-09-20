---
title: How to validate template changes
type: how-to
summary: Run the checks required before proposing a template change.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-20
related:
  - path: ../reference/template-contract.md
    why: Defines the template behavior those checks protect.
  - path: refresh-ci-image.md
    why: Explains the commit-specific image used for these checks.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the merge-request publication command.
---
<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->

# How to validate template changes

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
the root pipeline lists nine native jobs: `ruff-check`, `ruff-format`,
`pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`,
`license-check`, and `shellcheck`. They extend the one root `.base_uv`
definition, whose image is the commit-specific candidate
`$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID`. All nine jobs wait for the
BuildKit image job before starting and perform no `apt-get`, `uv python
install`, or `uv sync` bootstrap. They retain `uv run --no-sync`. The BuildKit
job uses a registry-backed cache and retains build metadata and the image digest
as pipeline evidence. There is no synthetic aggregate root validation job or
aggregate JUnit gate suite. Inspect an individual root check through its named
GitLab job. Each root pytest job runs four deterministic concurrent shards: one
complete `tests/test_agent_work_serial_runtime.py` shard, one complete
`tests/test_template.py` shard, and two exhaustive, non-overlapping partitions
of the remaining suite. The remaining-suite shards use
`tests.helpers.serial_runtime_shard` with indices 0 and 1 of 2 and ignore both
dedicated files. The job waits for every shard PID and publishes a JUnit report
for each shard. After all shards finish, the job
combines their coverage data, measures only the maintained `src/agent_work`
source tree, enforces the existing 85% floor, and publishes one combined
Cobertura report for its Python version. A failure in any shard, or a failure while
combining, checking, or reporting coverage, fails the named pytest job. When
investigating a failed job, inspect every shard JUnit and, when generation
succeeded, its combined coverage report; otherwise use the job log. The
generated Copier payload is not part of that root coverage
measurement.

Before validation starts, each configuration's shared `.base_uv` setup trusts
only its current `$CI_PROJECT_DIR` checkout with Git. This setting exists only
in the ephemeral validation container; it does not trust wildcard paths or
change host Git configuration.

The generated template deliberately keeps its portable `validation (9 gates)`
aggregate job. It does not consume the root image or require registry
credentials. Instead, before its `uv run --no-sync` checks, it unconditionally
runs `uv sync --locked --all-groups` against the checkout's `pyproject.toml`
and `uv.lock` for both Python environments. That makes its dependency content
current without a private registry image. It then reports its aggregate gate
suite and separate pytest reports. In both configurations, workflow rules
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

## Publish the verified branch for review

After the checks pass, push the feature branch. Then preview the merge-request
description before changing GitLab:

```bash
uv run agent-work merge-request publish \
  --source-branch "$(git branch --show-current)" \
  --target-branch main \
  --title "Add concise merge-request publication" \
  --problem "Reviewers need a consistent summary of the change." \
  --outcome "The branch has a structured merge-request description." \
  --validation "uv run tox" \
  --validation "uv run ruff check ." \
  --issue-iid 139
```

Review the preview. Run the same command with `--apply` to create a merge
request or update the one open merge request for that source branch. The
command does not push, approve, or merge. It rejects more than one open merge
request for the source branch. With `--issue-iid`, the preview ends with
`Closes #139`. After create or update, the command reads the merge request
back and requires GitLab to project exactly that configured-project issue as
closing. A missing or conflicting projection fails closed. Omit `--issue-iid`
for an issue-less merge request.

Add `--impact`, `--human-action`, or `--tracking` only when that information
helps the reviewer. `--tracking` may accompany `--issue-iid`, but it must not
contain a closing directive. Do not put a closing directive in any structured
text; use `--issue-iid` for the one closing issue.

See the [merge-request publication reference](../reference/serial-gitlab-agent-loop.md#merge-request-publish)
for every option and its verification contract.
