---
title: How to refresh the CI validation image
type: how-to
summary: Build the root CI image.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-21
related:
  - path: validate-template-changes.md
    why: Verifies the checks and timing budget protected by the image.
  - path: ../reference/template-contract.md
    why: Defines the boundary between this repository and generated projects.
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

# How to refresh the CI validation image

Use this guide after changing `Dockerfile`, `pyproject.toml`, or `uv.lock`.
These files build the validation image. Keep the lock current and commit it
with any locked dependency change. The pipeline makes a new image before root
checks begin.

## 1. Open the change merge request.

Open a merge request. Each merge-request and default-branch pipeline builds a
rootless BuildKit candidate from the current `Dockerfile`, `pyproject.toml`,
and `uv.lock`. It does this before root validation. Its tag is
`$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID`.

Eight single root jobs—`ruff-check`, `ruff-format`, `yamllint`, `kingfisher`,
`type-check`, `license-check`, `readability-check`, and `shellcheck`—and six
parallel pytest shards for each Python version wait for that candidate and run
in it. The image has the Python tools and locked dependencies. Most use
`uv run --no-sync`. `readability-check` uses `uv run --locked --all-groups`.
Do not install tools or dependencies in these jobs. This avoids a stale digest
or stale dependency set.

BuildKit uses a registry cache. Prefer a job run of 60 seconds or less. Reject
one over 120 seconds. Do not count queue time. Do not print credentials,
tokens, or auth files in logs.

## 2. Review the image evidence.

Review the image-build job in that pipeline. It keeps BuildKit metadata and its
image digest as artifacts. Check that it pushed the commit tag. The digest must
match `sha256:<64 hexadecimal characters>`.

Keep the metadata and digest with the pipeline evidence. Do not put login
commands or credentials in the template.

## 3. Verify the root validation jobs.

Inspect the eight single root jobs and the six `pytest-py311` and six
`pytest-py314` GitLab-native parallel jobs. Each waits for the candidate and
uses its prepared environment. None may run `apt-get`, `uv python install`,
`uv sync`, or an install fallback.

Each pytest shard uses `CI_NODE_INDEX` and `CI_NODE_TOTAL` for one deterministic,
exhaustive suite partition. It uploads one unique JUnit report and raw coverage
file. The small per-version aggregation job uses `needs` to wait for all six
shards, combines exactly their six raw coverage files, enforces the 85% floor, and publishes one
Cobertura report. Check the failed shard's JUnit report or the aggregation job's
Cobertura report and log. Any shard or aggregation failure fails the pipeline.

Use the longest terminal job with a numeric execution time for the budget.
Prefer 60 seconds or less. Reject more than 120 seconds. Report skipped,
canceled, and manual jobs separately. Also report pipeline span and queue time.
Queue time is excluded from this budget.

If the image needs a dependency, update the inputs. The next pipeline makes a
new commit candidate. Do not add bootstrap work to root jobs. Do not open a
post-merge pin-update merge request.

Generated Copier projects need no registry. Their portable validation job
always runs `uv sync --locked --all-groups` on its own `pyproject.toml` and
`uv.lock` in both Python environments. It does this before `uv run --no-sync`
checks. This keeps dependencies current without the root image.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
