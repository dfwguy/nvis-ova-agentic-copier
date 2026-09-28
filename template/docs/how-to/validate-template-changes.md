---
title: How to validate template changes
type: how-to
summary: Run local tests and CI checks for a template change.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-27
related:
  - path: ../reference/template-contract.md
    why: Defines the template behavior those checks protect.
  - path: refresh-ci-image.md
    why: Explains the commit-specific images used for these checks.
  - path: ../reference/serial-gitlab-agent-loop.md
    why: Defines the merge-request publication command.
  - path: ../reference/testing-strategy.md
    why: Defines local per-test timing targets and slow-case review rules.
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
`readability-check`, and `shellcheck`. One full pytest job runs on Python 3.11,
and one runs on Python 3.14. Three BuildKit jobs produce
commit-specific candidates:

| Build job and target | Dependency scope | Validation work |
| --- | --- | --- |
| `ci-image:tooling` / `tooling` | Only the locked `tooling` group, plus pinned system tools | Six single validation jobs |
| `ci-image:pytest-py311` / `pytest-py311` | Pinned Git and jq, plus locked project defaults, `test` group, and `typing` group on Python 3.11 | `pytest-py311`, `type-check`, and `license-check` |
| `ci-image:pytest-py314` / `pytest-py314` | Pinned Git and jq, plus locked project defaults and `test` group on Python 3.14.3 | `pytest-py314` |

Each validation job waits for and runs in its lane's image. The tags are
`validation-tooling`, `validation-pytest-py311`, and
`validation-pytest-py314`, each followed by
`:$CI_COMMIT_SHA-$CI_PIPELINE_ID`. They do not bootstrap with `apt-get`,
`uv python install`, or `uv sync`. Tooling jobs use `uv run --no-sync`.
`readability-check` sets `PYTHONPATH` to the checkout's `src` directory so
the prepared image can import `agent_work` without installing dependencies at
runtime. `type-check` and `license-check` run in the Python 3.11 image so they
can resolve test and project imports. The pytest jobs use the prepared
environment in their Python-specific image.

Each target has a separate registry cache. Its name has the target name and a
`-cache` suffix. Each build keeps its own metadata and digest environment file
as evidence. Inspect all three build jobs and every named validation job. The
root has no aggregate validation job or aggregate JUnit suite.

For each supported Python, the matching pytest job runs the complete suite
once. Each job publishes its own JUnit report and Cobertura coverage, measures
only maintained `src/agent_work` code, and enforces the existing 85% floor.
Any test, coverage-floor, or report failure fails the pipeline. For a failed
job, inspect its JUnit and Cobertura reports when they exist; otherwise, use
the job log. Root coverage excludes the generated Copier payload.

The [testing strategy](../reference/testing-strategy.md) defines per-case
timing targets and slow-case review rules. Use the JUnit case times to review
changed tests.

## Investigate local test timing

Enter one project-scoped validation window before the Judge hook and keep it
through the focused repeats and comparison. `GITLAB_HOST` must be a bare host
name, and `GITLAB_PROJECT_ID` must be a positive numeric project ID. Prepare
a separate baseline worktree at the exact comparison commit. Replace
`BASELINE_COMMIT_SHA` with its full commit ID. Prepare both baseline tox
interpreters without running tests. Keep the staged candidate in its own
worktree.

```bash
candidate_dir="$(pwd -P)"
baseline_commit="BASELINE_COMMIT_SHA"
baseline_parent="$(mktemp -d)"
baseline_dir="$baseline_parent/baseline"
git worktree add --detach "$baseline_dir" "$baseline_commit"
(cd "$baseline_dir" && uv run --locked tox run -e py311,py314 --notest)
export candidate_dir baseline_dir
```

From the candidate repository root, enter the window:

```bash
agent-work validation window --apply -- bash
```

At the child shell prompt, run:

```bash
candidate_tree="$(git write-tree)"
export PYTEST_ADDOPTS='--randomly-seed=262'
agent-work validation run --candidate-tree "$candidate_tree" --apply
"$baseline_dir/.tox/py311/bin/python" --version
"$candidate_dir/.tox/py311/bin/python" --version
"$baseline_dir/.tox/py314/bin/python" --version
"$candidate_dir/.tox/py314/bin/python" --version
```

Keep the child shell open through all repeats and comparison. Leave it with
`exit` after comparison; admission ends when the child exits, including after
a crash. It admits one local timing measurement per project at a time. Other
projects and CI continue independently. A busy admission waits up to 30
seconds, then exits with status `1`, prints `project timing admission busy`,
and saves `timing-contention.json` in the project's XDG state. Defer the
proof and retry after the active window ends. Treat that record and any
overlapping host work as contention evidence. Ad hoc tox timing outside the
window does not count as admitted clean proof.

Inside the admitted child shell:

1. Record the exact baseline and staged candidate Git trees, UTC date, host
   load, commands, and prepared interpreters. Confirm that the baseline and
   candidate interpreters use the same Python versions and packages. A branch
   name or test total does not identify a run.
2. The Judge hook above runs the full supported matrix for the staged tree.
   Preserve its complete `test-results-py311.xml` and
   `test-results-py314.xml` JUnit snapshots. Record each file's SHA-256
   digest, case count, failure count, error count, and pytest seed. Both tox
   environments pass `PYTEST_ADDOPTS` to pytest, so the exported seed reaches
   the full runs. Check the reported seed rather than assuming it did. Use
   the full JUnit files to list **every** candidate case
   above 100 ms and at or above 1 second, by full case ID and environment.
3. Find new and changed tests, including each parameter. Include cases that
   use changed shared fixtures or helpers. If you cannot tell which cases the
   change touches, find out before approval. Select those cases and relevant
   slow cases for focused diagnosis. Use the same node IDs on both trees
   wherever the case exists at baseline.
4. In each Python environment, run selected cases at least twice on each
   tree. Use the same number of baseline and candidate runs. Pass only node IDs
   present in the baseline to its runs; record added tests as candidate-only
   evidence. Use the corresponding prepared tox interpreter in each worktree,
   matching Python versions, pytest arguments, and an explicit
   `--randomly-seed` for every paired run. Use `-vv --durations=0` to capture
   setup, call, and teardown, including phases below 5 ms that pytest may
   otherwise omit. Give every run a distinct JUnit path and phase-log path.
   Save each file unchanged with its SHA-256 digest and JUnit case count.
   Record other host work that may have competed for CPU, disk, or memory.
   For Python 3.11, the focused command shape is
   `uv run --no-project --python .tox/py311/bin/python python -m pytest -vv --no-cov --durations=0 --randomly-seed=262 --junitxml=<unique-path>`
   followed by each selected full pytest node ID as a separate quoted
   argument. Use `py314` in the interpreter path for Python 3.14. Record the
   exact command and seed for every run.
5. Within the same window, supply the files and their tree, environment,
   interpreter, command, seed, digest, and count records to the read-only
   `agent_work.local_test_timing.compare_local_test_timings` callable. It
   accepts an evidence manifest, the expected baseline and candidate trees,
   and a file-reading callback. It gives signed candidate-minus-baseline time
   changes, phase costs, case groups, and full candidate lists above 100 ms
   and at or above 1 second. It does not run tests or provide a command-line
   interface. Check its report against the diff before assigning a slowdown
   to changed code. Check session fixtures before attributing the first
   consuming case's setup cost to its test body.

A `review` result needs closer review; it does not by itself prove a
regression. Missing, mismatched, or partial proof blocks approval until the
evidence owner repairs it. If sound paired proof shows mixed case directions,
record the doubt and queue case-specific diagnosis. That doubt alone need not
block an unrelated change when its impact map is clear and no other timing
rule blocks it. Do not call the mixed case a timing pass or a regression.
Check host load and fixture setup before assigning cause. Do not infer cause
from one run. An inherited case that stays above 100 ms but is unchanged or
faster remains in the report without automatic rejection. Give each confirmed
cost an owner and a reduction or integration-exception plan in the durable
audit or linked issue record. Keep the full per-run inventory and proof there.
The serial controller collects full-suite baseline and candidate proof by
default for both supported Python versions. It checks the same seed and
prepared environments, then runs focused comparisons where needed. Use its
source-bound timing findings and complete candidate inventory for review.
Missing or mismatched paired proof stops serial validation.

### Resume after an interrupted successful hook

If the window ends after a successful Judge hook but before timing is done,
close it. Read the saved status for the same staged tree. Require
`terminal-success`, hook exit code `0`, and both saved full-suite JUnit files.
Keep each absolute path, SHA-256 digest, and test count. Do not run the hook,
validation run, or retry again for that tree.

Check process and supervisor evidence first. It must show that the old factory
worker and all its child processes have ended and are quiet. A free project
lock alone does not prove this. Discard all timing runs from the old window,
even a partial pair. Open a new admitted window for timing only. Inside it,
recheck the saved status and JUnit files. Check the exact staged tree and the
unchanged candidate worktree. Stop if an identity or file has changed. The
saved hook result cannot validate a changed candidate.

In the new window, run complete equal baseline and candidate repeats for both
`py311` and `py314`. Run each tree at least twice. Record the baseline commit
and tree, candidate tree, and selected node IDs. Record each command, Python
interpreter and package version, pytest seed, JUnit and phase-log path, SHA-256
digest, and case count. Bind the read-only comparator to those exact trees.
Check for competing host work during and after the runs. Any overlap voids
that window's timing proof. Gather fresh complete pairs once it ends. Log the
interruption, old-worker check, new window, identities, saved full-suite files,
focused files, and contention checks in the durable audit. Non-functional
Review and Finalizer wait for sound proof.

This is a simple CI DAG, not a state machine. Check that the exact-source
pipeline has at least one current required job and that all current required
jobs pass.

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
publisher reads it at most eight times: once, then up to seven retries. Each
retry waits exactly one second, for at most seven seconds of intentional wait.
It revalidates the same merge request identity and source commit on each retry
and never repeats create or update. An exact configured-project issue succeeds.
A conflicting or malformed projection fails closed at once. The same is true
for identity drift, a wrong project or IID, or extra or multiple issues. An
empty or missing projection after the eighth read also fails closed and names
the existing merge request IID and source commit. Omit `--issue-iid` for an
issue-less merge request.

Add `--impact`, `--human-action`, or `--tracking` only when it helps the
reviewer. `--tracking` may accompany `--issue-iid`, but cannot contain a
closing directive. Do not put a closing directive in structured text. Use
`--issue-iid` for the one closing issue.

See the [merge-request publication reference](../reference/serial-gitlab-agent-loop.md#merge-request-publish)
for every option and its verification contract.
