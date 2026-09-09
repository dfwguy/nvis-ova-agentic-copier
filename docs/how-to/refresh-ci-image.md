---
title: Refresh the CI dependency image
type: how-to
summary: Publish and verify the project-only CI dependency image after a reviewed change.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-08
related:
  - path: validate-template-changes.md
    why: Verifies the checks and timing budget protected by the image.
  - path: ../reference/template-contract.md
    why: Defines the boundary between this repository and generated projects.
---

# Refresh the CI dependency image

Use this procedure after changing the CI image definition, `pyproject.toml`,
or `uv.lock`. The image is a project-only CI acceleration: generated Copier
projects remain portable and do not inherit its registry location or
credentials.

## 1. Validate the image merge request

Open a merge request that changes the image prerequisites. Its pipeline builds
the image with rootless BuildKit and validates the result, but does not push to
the registry. Review the build and validation evidence before a human merges
the merge request.

Do not print registry credentials, tokens, or generated authentication files
in job output. The validation path must use GitLab-provided credentials only
where the CI configuration requires them.

## 2. Publish after the human merge

After the merge reaches the default branch, the publication job runs only when
the image definition, `pyproject.toml`, or `uv.lock` changed. It publishes the
project-only image and records its immutable digest as a pipeline artifact.

Retrieve that artifact and confirm that it identifies the image by digest, not
by a mutable tag. Keep the digest with the pipeline evidence; do not copy a
credential or registry-login command into a generated template.

## 3. Verify the consumer change

Use the recorded digest to update the consumer merge request separately. For
the current rollout, that is merge request !88. Its pipeline must retain the
separate test results and complete execution in the project budget: prefer at
most 10 seconds after a runner starts and reject execution over 60 seconds.
Exclude runner queue time when measuring those limits.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
