---
title: Refresh the CI dependency image
type: how-to
summary: Publish, pin, and verify the project-only CI image that root validation consumes.
audience: maintainer
tags: [template-maintenance]
status: current
last_reviewed: 2026-09-09
related:
  - path: validate-template-changes.md
    why: Verifies the checks and timing budget protected by the image.
  - path: ../reference/template-contract.md
    why: Defines the boundary between this repository and generated projects.
---

# Refresh the CI dependency image

Use this procedure after changing the CI image definition, `pyproject.toml`,
or `uv.lock`. The image is a project-only root-repository dependency: root
validation consumes its already-published contents by immutable digest and
does no `apt` or UV bootstrap. Generated Copier projects keep their own
portable CI bootstrap and do not inherit this registry location, image pin, or
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

Retrieve that artifact and confirm that it records `CI_IMAGE` in the form
`<registry>/<project>/validation@sha256:<64 hexadecimal characters>`, not a
mutable tag. Keep that exact digest with the pipeline evidence; do not copy a
credential or registry-login command into a generated template.

## 3. Verify the consumer change

Use the recorded digest to update the single image pin in the root `.base_uv`
definition in a separate merge request. Set it to the complete `CI_IMAGE`
value, including `@sha256:...`; never substitute a floating tag. The eight
native jobs that extend `.base_uv`—`ruff-check`, `ruff-format`,
`pytest-py311`, `pytest-py314`, `yamllint`, `kingfisher`, `type-check`, and
`shellcheck`—then use the image's prepared interpreter, system tools, and
locked project dependencies. They must not run `apt-get`, `uv python install`,
`uv sync`, or another dependency-installation fallback.

Run the consumer pipeline and inspect the eight named root jobs directly; do
not look for a root synthetic aggregate validation job or JUnit gate suite. The
two pytest jobs retain separate JUnit and coverage reports. Apply the project
budget to the overall queue-excluded pipeline duration: prefer at most 10
seconds and reject execution over 60 seconds. If the image needs another
dependency, refresh and publish the image first, then repeat this pin-update
procedure; do not restore bootstrap work to root validation.

For the local equivalents of the CI checks, see [Validate template
changes](validate-template-changes.md). For the generated-project ownership
boundary, see the [template contract](../reference/template-contract.md).
