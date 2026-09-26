---
title: How to validate template changes
type: how-to
summary: Run local tests and CI checks for a template change.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-26
related:
  - path: ../reference/template-contract.md
    why: Defines the template behavior those checks protect.
  - path: refresh-ci-image.md
    why: Explains the commit-specific images used for these checks.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the merge-request publication command.
  - path: ../reference/testing-strategy.md
    why: Defines per-test timing targets and records long-running cases.
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
the root pipeline has eight single validation jobs: `ruff-check`,
`ruff-format`, `yamllint`, `kingfisher`, `type-check`, `license-check`,
`readability-check`, and `shellcheck`. Seventeen native pytest shards run on
Python 3.11, and six run on Python 3.14. Three BuildKit jobs produce
commit-specific candidates:

| Build job and target | Dependency scope | Validation work |
| --- | --- | --- |
| `ci-image:tooling` / `tooling` | Only the locked `tooling` group, plus pinned system tools | Six single validation jobs |
| `ci-image:pytest-py311` / `pytest-py311` | Pinned Git and jq, plus locked project defaults, `test` group, and `typing` group on Python 3.11 | `pytest-py311` shards, `coverage-py311`, `type-check`, and `license-check` |
| `ci-image:pytest-py314` / `pytest-py314` | Pinned Git and jq, plus locked project defaults and `test` group on Python 3.14.3 | `pytest-py314` shards and `coverage-py314` |

Each validation job waits for and runs in its lane's image. The tags are
`validation-tooling`, `validation-pytest-py311`, and
`validation-pytest-py314`, each followed by
`:$CI_COMMIT_SHA-$CI_PIPELINE_ID`. They do not bootstrap with `apt-get`,
`uv python install`, or `uv sync`. Tooling jobs use `uv run --no-sync`.
`readability-check` sets `PYTHONPATH` to the checkout's `src` directory so
the prepared image can import `agent_work` without installing dependencies at
runtime. `type-check` and `license-check` run in the Python 3.11 image so they
can resolve test and project imports. Test and coverage jobs use the prepared environment in their
Python-specific image.

Each target has a separate registry cache. Its name has the target name and a
`-cache` suffix. Each build keeps its own metadata and digest environment file
as evidence. Inspect all three build jobs and every named validation job. The
root has no aggregate validation job or aggregate JUnit suite.

For each supported Python, its native pytest shards form one deterministic,
exhaustive full-suite partition. Each validates `CI_NODE_INDEX` and
`CI_NODE_TOTAL` before pytest; invalid metadata fails before tests begin. Each
shard publishes a unique JUnit report and raw coverage file. One aggregation
job waits for its matching shards. `coverage-py311` combines exactly 17 raw
coverage files; `coverage-py314` combines exactly six. Both jobs measure only
maintained `src/agent_work` code, enforce the existing 85% floor, and publish
Cobertura. Any shard, combine, coverage-floor, report, or aggregation failure
fails the pipeline. For a failed job, inspect the shard
JUnit reports and the matching Cobertura report when it exists; otherwise, use
the job log. Root coverage excludes the generated Copier payload.

The [testing strategy](../reference/testing-strategy.md) records per-case
timing targets and the long-running test register. Use the JUnit case times to
review changed tests; job duration measures a different limit.

## Investigate local test timing

1. Find the exact baseline and candidate Git trees. Prepare separate
   checkouts with the same Python version and packages. Record each tree
   hash, UTC date, interpreter, host load, and command. A branch name or test
   total does not identify a run.
2. Run `uv run tox -e py311` and `uv run tox -e py314` on the candidate.
   Preserve their complete `test-results-py311.xml` and
   `test-results-py314.xml` files. Record each file's SHA-256 digest, case
   count, failure count, error count, and pytest seed. The fixed tox commands
   do not pass a requested seed through `PYTEST_ADDOPTS` or extra arguments
   after `tox --`. Use the full JUnit files to list **every** candidate case
   above 100 ms and at or above 1 second, by full case ID and environment.
3. Find new and changed tests, including each parameter. Include cases that
   use changed shared fixtures or helpers. If you cannot tell which cases the
   change touches, find out before approval. Select those cases and relevant
   slow cases for focused diagnosis. Use the same node IDs on both trees
   wherever the case exists at baseline.
4. For each relevant Python environment, run the selected cases at least
   twice on the baseline tree and twice on the candidate tree. Use the same
   prepared interpreter, pytest command, arguments, and explicit
   `--randomly-seed` for all four runs. Use `-vv --durations=0` to capture
   setup, call, and teardown, including phases below 5 ms that pytest may
   otherwise omit. Give every run a distinct JUnit path and phase-log path.
   Save each file unchanged with its SHA-256 digest and JUnit case count.
   For Python 3.11, the focused command shape is
   `uv run --no-project --python .tox/py311/bin/python python -m pytest -vv --no-cov --durations=0 --randomly-seed=262 --junitxml=<unique-path>`
   followed by each selected full pytest node ID as a separate quoted
   argument. Use `py314` in the interpreter path for Python 3.14. Record the
   exact command and seed for every run.
5. Supply the files and their tree, environment, interpreter, command, seed,
   digest, and count records to the read-only
   `agent_work.local_test_timing.compare_local_test_timings` callable. It
   accepts an evidence manifest, the expected baseline and candidate trees,
   and a file-reading callback. It gives signed candidate-minus-baseline time
   changes, phase costs, case groups, and full candidate lists above 100 ms
   and at or above 1 second. It does not run tests or provide a command-line
   interface. Check its report against the diff before assigning a slowdown
   to changed code.

A `review` result needs closer review; it does not by itself prove a
regression. Missing, mismatched, or partial proof blocks approval until the
evidence owner repairs it. If sound paired proof shows mixed case directions,
record the doubt and queue case-specific diagnosis. That doubt alone need not
block an unrelated change when its impact map is clear and no other timing
rule blocks it. Do not call the mixed case a timing pass or a regression.
Check host load and fixture setup before assigning cause. Do not infer cause
from one run. An inherited case that stays above 100 ms but is unchanged or
faster remains in the report without automatic rejection. Give each
confirmed cost an owner and a
reduction or integration-exception plan in the
[long-running register](../reference/testing-strategy.md#long-running-local-test-register).
The serial worker still uses trusted candidate-only timing proof; issue #270
tracks paired baseline evidence there.

This is a simple CI DAG, not a state machine. The separate images aim to reduce
unrelated work. They do not guarantee a run time. Measure the longest terminal
job only. Prefer 60 seconds or less and reject more than 120 seconds. Queue
time is not part of either limit.

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
description before changing GitLab. Git-flow resolves the active worktree root
and calls its publisher wrapper. This uses the checkout's `agent-work`, rather
than selecting an ambient command:

The checkout wrapper supplies `UV_CACHE_DIR` when the caller has not set it. It
uses `$XDG_CACHE_HOME/uv` when `XDG_CACHE_HOME` is set, otherwise
`$HOME/.cache/uv`. Set `UV_CACHE_DIR` explicitly to a writable, private cache
when running in a workspace-write sandbox with a read-only home directory.
Serial services and interactive callers use the same wrapper and therefore the
same contract; the cache path is the only additional writable scratch path
needed by `uv`.

```bash
active_worktree="$(git rev-parse --show-toplevel)"
"$active_worktree/bin/agent-work-from-checkout" merge-request publish \
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
`Closes #139`. After create or update and the exact authoritative merge-request
read-back, GitLab must project exactly that configured-project issue as closing.
Only an empty or missing projection means GitLab is still catching up. The
publisher reads it at most four times: once, then up to three retries. Each
retry waits exactly one second, for at most three seconds of intentional wait.
It never repeats create or update. An exact
configured-project issue succeeds. A conflicting or malformed projection fails
closed at once. The same is true for identity drift, a wrong project or IID,
or extra or multiple issues. An empty or missing projection after the fourth
read also fails closed. Omit `--issue-iid` for an issue-less merge request.

Add `--impact`, `--human-action`, or `--tracking` only when it helps the
reviewer. `--tracking` may accompany `--issue-iid`, but cannot contain a
closing directive. Do not put a closing directive in structured text. Use
`--issue-iid` for the one closing issue.

See the [merge-request publication reference](../reference/serial-gitlab-agent-loop.md#merge-request-publish)
for every option and its verification contract.
