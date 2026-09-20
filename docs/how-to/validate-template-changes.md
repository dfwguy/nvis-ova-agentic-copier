---
title: How to validate template changes
type: how-to
summary: Check a change.
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

Use this guide after changing the Copier template. Complete these checks before
you open a merge request.

From the repository root, run the local equivalents of the GitLab validation
checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run --locked --all-groups python -m agent_work.readability --root . --scope all
uv run tox
uv run yamllint -d '{"extends": "default", "rules": {"line-length": "disable", "document-start": "disable"}}' -f parsable .
uv run kingfisher scan . --git-history none --no-validate --no-update-check --exclude .git --exclude .venv --exclude .tox --exclude .pytest_cache --exclude .ruff_cache
```

The local `tox` command remains the supported-matrix test runner. In GitLab CI,
the root pipeline has ten native validation jobs: `ruff-check`, `ruff-format`,
`pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`,
`license-check`, `readability-check`, and `shellcheck`. They extend the root
`.base_uv` definition, whose image is the commit-specific candidate
`$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID`. The separate
`ci-image:candidate` BuildKit image-build job produces it, and all ten
validation jobs wait for that job. They do not bootstrap with `apt-get`,
`uv python install`, or `uv sync`. All except `readability-check` use
`uv run --no-sync`; `readability-check` uses `uv run --locked --all-groups`.
The BuildKit job uses a registry-backed cache. It keeps build metadata and the
image digest as pipeline evidence. The root has no aggregate validation job or
aggregate JUnit suite. Inspect each check in its named GitLab job.

Each root pytest job runs four fixed concurrent shards. One runs all of
`tests/test_agent_work_serial_runtime.py`. One runs all of
`tests/test_template.py`. The other two split the remaining suite without
overlap. They use `tests.helpers.serial_runtime_shard`, with indices 0 and 1
of 2, and ignore both dedicated files. The job waits for every shard PID and
publishes one JUnit report per shard. It then combines coverage, measures only
the maintained `src/agent_work` tree, enforces the existing 85% floor, and
publishes one Cobertura report per Python version. Any shard, coverage combine,
coverage check, or coverage report failure fails its named pytest job. For a
failed job, inspect every shard JUnit report. Inspect its combined coverage
report when it exists; otherwise, use the job log. Root coverage excludes the
generated Copier payload.

Before validation, each shared `.base_uv` setup trusts only its current
`$CI_PROJECT_DIR` checkout with Git. This applies only in the temporary
validation container. It trusts no wildcard path and changes no host Git
configuration.

The generated template keeps its portable `validation (10 gates)` aggregate
job. It uses no root image or registry credentials. Before its
`uv run --no-sync` checks, it always runs `uv sync --locked --all-groups` with
the checkout's `pyproject.toml` and `uv.lock` for both Python environments.
This keeps dependencies current without a private registry image. It reports
its aggregate gate suite and separate pytest reports. In both configurations,
workflow rules select a merge-request pipeline and suppress a duplicate branch
pipeline while that merge request is open.

If the change adds or modifies a Bash script, validate that script directly:

```bash
bash -n path/to/script.sh
shellcheck path/to/script.sh
```

The ShellCheck gate ignores `.git`, `.cache`, `.venv`, `.tox`,
`.pytest_cache`, and `.ruff_cache`. Those paths hold generated or tool-managed
files, not maintained source. Do not silence findings in tracked scripts. Fix
the script or document a standards change.

For the exact generated-project contract and the distinction between the
template and its generated output, see the [template reference](../reference/template-contract.md#rendering-rules).

## Publish the verified branch for review.

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
`Closes #139`. After create or update, it reads the merge request back. GitLab
must project exactly that configured-project issue as closing. A missing or
conflicting projection fails closed. Omit `--issue-iid` for an issue-less merge
request.

Add `--impact`, `--human-action`, or `--tracking` only when it helps the
reviewer. `--tracking` may accompany `--issue-iid`, but cannot contain a
closing directive. Do not put a closing directive in structured text. Use
`--issue-iid` for the one closing issue.

See the [merge-request publication reference](../reference/serial-gitlab-agent-loop.md#merge-request-publish)
for every option and its verification contract.
