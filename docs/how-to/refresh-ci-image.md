---
title: How to refresh the CI validation image
type: how-to
summary: Rebuild and check the root CI image and registry cleanup.
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

The ten native root jobs wait for that candidate and run in it:
`ruff-check`, `ruff-format`, `pytest-py311`, `pytest-py314`, `yamllint`,
`kingfisher`, `type-check`, `license-check`, `readability-check`, and
`shellcheck`. The image has the Python tools and locked dependencies. Most use
`uv run --no-sync`. `readability-check` uses `uv run --locked --all-groups`.
Do not install tools or dependencies in these jobs. This avoids a stale digest
or stale dependency set.

The image build runs the locked Kingfisher version as its non-root user. It
precompiles Kingfisher's unchanged built-in Vectorscan rules into a stable
cache path and keeps that cache in the image for the root `kingfisher` job.
The cache avoids compiling unchanged rules during validation. This optimization
changes image preparation only. Do not change the runtime Kingfisher command,
rules, exclusions, or failure behavior to use the cache.

BuildKit uses a registry cache. Prefer a job run of 60 seconds or less. Reject
one over 120 seconds. These are measured pipeline thresholds, not a guaranteed
build time. Do not count queue time. Do not print credentials, tokens, or auth
files in logs.

## 2. Review the image evidence.

Review the image-build job in that pipeline. It keeps BuildKit metadata and its
image digest as artifacts. Check that it pushed the commit tag. The digest must
match `sha256:<64 hexadecimal characters>`.

Keep the metadata and digest with the pipeline evidence. Do not put login
commands or credentials in the template.

## 3. Check the container registry cleanup policy during repository setup.

Repository setup runs `uv run agent-work repository cleanup-policy` as a
read-only check. It does not change GitLab settings, including when the
consistency interface accepts `--apply`. Use `--json` when a structured status,
observations, advisory, and settings URL are needed. The normal output uses
real line breaks.

Open the validated project URL followed by
`/-/settings/packages_and_registries`, then follow **Settings → Packages and
registries → Container registry → Cleanup policies**. The check observes
`container_registry_access_level`, whether a policy is visible and present,
and its `enabled`, `cadence`, `keep_n`, and `older_than` values. The current
deletion-pattern field is `name_regex_delete`; older responses can use the
deprecated `name_regex`. The check reports the field that GitLab actually
returns. Its `name_regex_keep` applies to tag names within each repository.

A concise healthy result means the visible policy can cover the transient
`validation` candidates, whose tag suffix is `$CI_COMMIT_SHA-$CI_PIPELINE_ID`.
Only disabled registry access is a concise not-applicable success. Private
registry access is active and still needs cleanup assessment. When
the registry is enabled, no policy, a disabled or incomplete policy, or regexes
unlikely to cover those tags is advisory: setup continues and exits 0. An API,
permission, malformed response, wrong-project, or wrong-host result is a
separate failure and exits 1. Give that failure to a Maintainer or Owner with
access to the settings page.

Use bounded retention for transient validation tags, but do not choose a
cadence, count, age, or regex value from this check. Keep the mutable
`validation-cache` BuildKit cache usable; it is separate from commit-specific
validation candidates.

## 4. Verify the root validation jobs.

Inspect the ten named root jobs: `ruff-check`, `ruff-format`, `pytest-py311`,
`pytest-py314`, `yamllint`, `kingfisher`, `type-check`, `license-check`,
`readability-check`, and `shellcheck`. Each waits for the candidate and uses
its prepared environment. None may run `apt-get`, `uv python install`, `uv
sync`, or an install fallback.

Inspect those jobs directly. There is no root aggregate job or aggregate JUnit
gate suite. Each pytest job starts four shards in parallel. One runs the full
serial-runtime file. One runs the full template file. The other two split all
remaining tests without overlap. They use `tests.helpers.serial_runtime_shard`
with indices 0 and 1 of 2. They ignore both dedicated files. The job waits for
every shard PID before it combines coverage.

Check every shard JUnit report. Check the one per-version Cobertura report when
it exists. Otherwise, check the job log. The combined report covers only
maintained `src/agent_work` code. A shard, coverage combine, coverage floor,
or report failure fails its named job.

Use the longest terminal job with a numeric run time for the budget. Prefer 60
seconds or less. Reject more than 120 seconds. Report skipped, canceled, and
manual jobs separately. Also report pipeline span and queue time. Runner wait
time is not part of this budget.

If the image needs a dependency, update the inputs. The next pipeline makes a
new commit candidate. Do not add bootstrap work to root jobs. Do not open a
post-merge pin-update merge request.

Generated Copier projects need no registry. Their portable validation job
always runs `uv sync --locked --all-groups` on its own `pyproject.toml` and
`uv.lock` in both Python environments. It does this before `uv run --no-sync`
checks. This keeps dependencies current without the root image or its
Kingfisher cache.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
