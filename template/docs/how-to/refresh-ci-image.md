---
title: How to refresh the CI validation image
type: how-to
summary: Build and verify the commit-specific CI image used by root validation.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-19
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

Use this procedure after changing `Dockerfile`, `pyproject.toml`, or `uv.lock`.
They are the validation-image build inputs. The pipeline builds and pushes a
candidate for the current commit before root validation starts. Locked
dependency changes must update and commit `uv.lock`.

## 1. Open the change merge request

Open a merge request for the change. Every merge-request and default-branch
pipeline builds the root validation image with rootless BuildKit from the
current `Dockerfile`, `pyproject.toml`, and `uv.lock`. It pushes the candidate
as `$CI_REGISTRY_IMAGE/validation:$CI_COMMIT_SHA-$CI_PIPELINE_ID`.

The nine native root validation jobs wait for that candidate and run in it.
They retain `uv run --no-sync`; the image supplies the interpreter, system
tools, and locked dependencies, so the jobs must not install dependencies at
runtime. This removes the window where validation could run against stale
dependency content from a fixed digest.

The BuildKit job uses a registry-backed cache to stay within the per-job
budget: prefer 60 seconds and reject a duration over 120 seconds. Do not count
queue time toward that limit. Do not print registry credentials, tokens, or
generated authentication files in job output.

## 2. Review the image evidence

Review the image-build job in the same pipeline. It retains BuildKit metadata
and the produced image digest as artifacts. Confirm that the job pushed the
commit-specific tag and that the digest has the form
`sha256:<64 hexadecimal characters>`.

Keep the metadata and digest with the pipeline evidence. They record the image
build result. Do not copy registry-login commands or
credentials into a generated template.

## 3. Verify the root validation jobs

Inspect the nine named root jobs: `ruff-check`, `ruff-format`, `pytest-py311`,
`pytest-py314`, `yamllint`, `kingfisher`, `type-check`, `license-check`, and
`shellcheck`. They must wait for the candidate image and use its prepared
environment. They must not run `apt-get`, `uv python install`, `uv sync`, or a
dependency-installation fallback.

Run the consumer pipeline and inspect the nine named root jobs directly; do
not look for a root synthetic aggregate validation job or JUnit gate suite. Each
pytest job runs four concurrent shards: one complete serial-runtime test shard,
one complete template-test shard, and two exhaustive, non-overlapping remaining-suite
partitions. The remaining-suite shards use `tests.helpers.serial_runtime_shard`
with indices 0 and 1 of 2 and ignore both dedicated test files. The job waits
for every shard PID before coverage
aggregation. Inspect every shard JUnit report and the one combined
per-version Cobertura report when it was generated; otherwise inspect the job
log. The combined report measures only maintained `src/agent_work` source. A
failure in any shard or in coverage aggregation, floor enforcement, or
report generation fails that named job. Apply the
project budget to the longest executed terminal job with a numeric duration:
prefer at most 60 seconds and reject execution over 120 seconds. Report skipped,
canceled, or manual jobs, the overall pipeline span, and queue time separately;
runner scheduling does not count against the execution budget. If the image
needs another dependency, update its build inputs and let the next pipeline
build a new commit-specific candidate; do not restore bootstrap work to root
validation. There is no separate post-merge pin-update merge request.

Generated Copier projects remain registry-independent. Their portable
validation job unconditionally runs `uv sync --locked --all-groups` against
its own checkout's `pyproject.toml` and `uv.lock` for both Python environments
before it runs `uv run --no-sync` checks. That bootstrap gives generated
projects the same no-stale-dependency guarantee without using the root image.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
