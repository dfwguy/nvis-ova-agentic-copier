---
title: How to refresh the CI validation images
type: how-to
summary: Build and check three root CI images and clean up old tags.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-24
related:
  - path: validate-template-changes.md
    why: Verifies the checks served by the images.
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

# How to refresh the CI validation images

Use this guide after changing `Dockerfile`, `pyproject.toml`, or `uv.lock`.
These files build the three root validation images. Keep the lock current.
Commit it with any locked dependency change. The pipeline makes new images
before root checks begin.

## 1. Open the change merge request.

Open a merge request. Each merge-request and default-branch pipeline builds
three rootless BuildKit candidates from the current `Dockerfile`,
`pyproject.toml`, and `uv.lock`. It does this before root validation.

| Target | Locked dependency scope | Candidate tag | Jobs |
| --- | --- | --- | --- |
| `tooling` | Only the `tooling` group | `$CI_REGISTRY_IMAGE/validation-tooling:$CI_COMMIT_SHA-$CI_PIPELINE_ID` | `ruff-check`, `ruff-format`, `yamllint`, `kingfisher`, `readability-check`, and `shellcheck` |
| `pytest-py311` | Project defaults, `test` group, and `typing` group on Python 3.11 | `$CI_REGISTRY_IMAGE/validation-pytest-py311:$CI_COMMIT_SHA-$CI_PIPELINE_ID` | The 17 `pytest-py311` shards, `coverage-py311`, `type-check`, and `license-check` |
| `pytest-py314` | Project default dependencies and the `test` group on Python 3.14.3 | `$CI_REGISTRY_IMAGE/validation-pytest-py314:$CI_COMMIT_SHA-$CI_PIPELINE_ID` | The six `pytest-py314` shards and `coverage-py314` |

The `tooling` group has Kingfisher, Ruff, textstat, and yamllint. The `typing`
group has ty and textstat. Ty needs textstat to check the readability code.
Local development includes test, tooling,
and typing groups, plus pre-commit and tox. The tooling image also has pinned
Git, jq, and ShellCheck.
Both pytest images have pinned Git and jq because the tests exercise
repository and JSON behavior.
They do not include ShellCheck, Kingfisher, Ruff, or yamllint. Each job
waits only for its lane's image. Each image has the locked packages its lane
needs. Jobs must not run `apt-get`, `uv python install`, `uv sync`, or an
install fallback. Validation jobs use `uv run --no-sync`.
`readability-check` also uses the prepared tooling image, with `src` on
`PYTHONPATH` so it can import `agent_work` without installing dependencies at
runtime. `type-check` and `license-check` use the Python 3.11 image to resolve
test and project imports.
Test and coverage jobs select the environment already prepared in their
Python-specific image.

The tooling image build runs the locked Kingfisher version as its non-root
user. It compiles Kingfisher's unchanged built-in Vectorscan rules once. It
keeps those rules in a stable cache for the root `kingfisher` job.
The cache avoids compiling unchanged rules during validation. This optimization
changes image preparation only. Do not change the runtime Kingfisher command,
rules, exclusions, or failure behavior to use the cache.

Each target has its own mutable BuildKit cache:
`validation-tooling-cache`, `validation-pytest-py311-cache`, and
`validation-pytest-py314-cache`. The split limits each image to its lane.
GitLab can build the images at the same time. This aims to reduce the longest
job's work, but it does not guarantee a build time. Job duration is diagnostic
evidence, not a publication gate. Do not print credentials, tokens, or auth
files in logs.

## 2. Review the image evidence.

Review all three image-build jobs: `ci-image:tooling`,
`ci-image:pytest-py311`, and `ci-image:pytest-py314`. Each keeps its BuildKit
metadata and digest environment file as artifacts:

| Target | Build metadata | Digest environment file | Digest variable |
| --- | --- | --- | --- |
| `tooling` | `build-metadata-tooling.json` | `ci-image-tooling.env` | `CI_TOOLING_IMAGE` |
| `pytest-py311` | `build-metadata-pytest-py311.json` | `ci-image-pytest-py311.env` | `CI_PY311_IMAGE` |
| `pytest-py314` | `build-metadata-pytest-py314.json` | `ci-image-pytest-py314.env` | `CI_PY314_IMAGE` |

Check that every job pushed its commit tag and recorded an immutable digest.
Each digest must match `sha256:<64 hexadecimal characters>`. Keep all six
artifact files as pipeline evidence. Do not put login commands or credentials
in the generated template.

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
`validation-tooling`, `validation-pytest-py311`, and
`validation-pytest-py314` candidates, whose tag suffix is
`$CI_COMMIT_SHA-$CI_PIPELINE_ID`.
Only disabled registry access is a concise not-applicable success. Private
registry access is active and still needs cleanup assessment. When
the registry is enabled, no policy, a disabled or incomplete policy, or regexes
unlikely to cover those tags is advisory: setup continues and exits 0. An API,
permission, malformed response, wrong-project, or wrong-host result is a
separate failure and exits 1. Give that failure to a Maintainer or Owner with
access to the settings page.

Use bounded retention for transient validation tags, but do not choose a
cadence, count, age, or regex value from this check. Keep the mutable
target-specific BuildKit caches usable; they are separate from commit-specific
validation candidates.

## 4. Verify the root validation jobs.

Inspect the eight named root validation jobs: `ruff-check`, `ruff-format`,
`yamllint`, `kingfisher`, `type-check`, `license-check`, `readability-check`,
and `shellcheck`, plus all 17 Python 3.11 shards, all six Python 3.14 shards,
and both aggregation jobs. Confirm that each job uses the target shown in the
mapping table. None may run `apt-get`, `uv python install`, `uv sync`, or an
install fallback.

Inspect those jobs directly. The root has no aggregate validation job or
aggregate JUnit gate suite. The 17 Python 3.11 shards and six Python 3.14
shards each use validated `CI_NODE_INDEX` and `CI_NODE_TOTAL` values to make
one deterministic, exhaustive full-suite partition per version. Invalid
metadata must fail before pytest. Each shard publishes a unique JUnit report
and raw coverage file. Each aggregation job waits for its matching shards.
`coverage-py311` combines exactly 17 files; `coverage-py314` combines exactly
six. Both jobs measure only maintained `src/agent_work` code, enforce the 85%
floor, and publish Cobertura. A shard, coverage combine, coverage floor,
report, or aggregation failure fails the pipeline.

For the exact MR source SHA, verify at least one current required job and
success from every current required job. Report skipped, canceled, and manual jobs separately when
checking a result. Job duration and wait time can explain delays; they do
not decide the handoff.

If an image needs a dependency, update the inputs. The next pipeline makes new
commit candidates. Do not add bootstrap work to root jobs. Do not open a
post-merge pin-update merge request.

Generated Copier projects need no registry. Their portable validation job
always runs `uv sync --locked --all-groups` on its own `pyproject.toml` and
`uv.lock` in both Python environments. It does this before `uv run --no-sync`
checks. This keeps dependencies current without the root images or their
Kingfisher cache.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
