---
title: How to refresh the CI dependency image
type: how-to
summary: Publish, pin, and verify the project-only CI image that root validation consumes.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-17
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

# How to refresh the CI dependency image

Use this procedure after changing `Dockerfile`, `pyproject.toml`, or `uv.lock`.
They are all image-build inputs. The image is a project-only root-repository
dependency: root validation consumes its already-published contents by
immutable digest and does no `apt` or UV bootstrap. Locked dependency changes
must update and commit `uv.lock`. Generated Copier projects keep their own
portable CI bootstrap and do not inherit this registry location, image pin, or
credentials.

## 1. Validate the image merge request

Open a merge request that changes the image prerequisites. Its pipeline builds
the image with rootless BuildKit and validates the result, but does not push to
the registry. Review the build and validation evidence before a human merges
the merge request.

The root validation jobs use the currently pinned published image. Their empty
`needs` declaration starts them immediately in parallel with `ci-image:validate`
rather than waiting for that image-validation stage. This keeps the image build
off the root validation critical path while retaining its evidence in the same
pipeline.

Do not print registry credentials, tokens, or generated authentication files
in job output. The validation path must use GitLab-provided credentials only
where the CI configuration requires them.

## 2. Publish after the human merge

After the merge reaches the default branch, the publication job runs only when
`Dockerfile`, `pyproject.toml`, or `uv.lock` changed. It publishes the
project-only image and records its immutable digest as a pipeline artifact.

Retrieve that artifact and confirm that it records `CI_IMAGE` in the form
`<registry>/<project>/validation@sha256:<64 hexadecimal characters>`, not a
mutable tag. Keep that exact digest with the pipeline evidence; do not copy a
credential or registry-login command into a generated template.

## 3. Verify the consumer change

Use the recorded digest to update the single image pin in the root `.base_uv`
definition in a separate merge request. Set it to the complete `CI_IMAGE`
value, including `@sha256:...`; never substitute a floating tag. The nine
native jobs that extend `.base_uv`—`ruff-check`, `ruff-format`,
`pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`,
`license-check`, and `shellcheck`—then use the image's prepared interpreter, system tools, and
locked project dependencies. They must not run `apt-get`, `uv python install`,
`uv sync`, or another dependency-installation fallback.

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
needs another dependency, refresh and publish the image first, then repeat this
pin-update procedure; do not restore bootstrap work to root validation.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
