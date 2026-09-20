<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Security Policy: NVIS-OVA Agentic Copier

NVIDIA protects its software, services, and source repositories.

Do not report security flaws in public GitLab issues or merge requests.

## Report a flaw

- Web (preferred): [NVIDIA Vulnerability Disclosure Program](https://www.nvidia.com/en-us/security/)
- Email: [psirt@nvidia.com](mailto:psirt@nvidia.com)
- PGP key: use the [NVIDIA public PGP Key](https://www.nvidia.com/en-us/security/pgp-key) for secure email.
- GitLab: use the project Security tab and select Report a vulnerability.

Include the project and version or branch. State the flaw type. Give steps to
reproduce it. Add a proof of concept when one exists. State the likely impact.

NVIDIA PSIRT will confirm receipt, validate severity, develop a fix, and
publish a security bulletin as appropriate.

## Context

NVIS-OVA Agentic Copier is a Copier template. It makes Python projects with
UV, Ruff, ty, pytest, tox, pre-commit, GitLab CI, and portable agent files.
Those files include `AGENTS.md`, `.agents/agents/`, and `.agents/skills/`.

It is a CLI developer tool. It must not add flaws to generated project layout,
agent files, or CI. It runs on a developer machine. It uses the `glab` CLI for
GitLab work.

## Boundaries

- Template tasks: `copier copy --trust` runs `_tasks`. They check needs, set up
  a repository, run guarded Claude compatibility migration, diagnose, sync
  deps, and add hooks.
  `--trust` is clear user consent. Copier will not run tasks without it.
- Local hooks: pre-commit runs tools through `uv` from `.pre-commit-config.yaml`.
  Dependency data and the lockfile select their versions.
- Agent control: `AGENTS.md` and shared skills forbid agents from approving or
  merging merge requests. Claude settings block known commands. GitLab rules
  keep human review in place across frontends.
- GitLab API: repository setup uses `glab api` to set branch protection and
  approval rules on `gitlab-master.nvidia.com`.

## Threat model

1. An injected or compromised agent could call an approval API. GitLab rules
   and human review are the enforcement point. Open Argus finding `0a1f365f`.
   Confidence: 0.84.
2. A bad package or lockfile change could run code on a developer machine during
   pre-commit. Both `.pre-commit-config.yaml` files run `uv` tools.
3. A changed template could run shell commands after rendering through
   `copier.yaml` tasks. Someone who changes `copier.yaml` or `template/`, such
   as through an approval-bypass MR, could affect every `copier copy --trust` run.
4. A hostile `project_slug` could inject TOML into `pyproject.toml.jinja`.
   The variable has no `validator` on lines 2 and 27. Quotes or newlines could
   change the generated `pyproject.toml`. This risk is low for self-owned work.
   It rises when CI passes untrusted `--data project_slug` values. Accepted risk:
   Argus finding `796e7078`; normal use has no cross-trust boundary.
5. Claude blocks known approval and merge commands. Codex and Pi use shared
   rules. Other clients or commands may act differently. GitLab protection and
   approval rules are the final enforcement boundary.

## Assumptions

- GitLab protects `main`. It blocks direct pushes and needs MR approval. This
  is not assured until repository setup runs and `CONTRIBUTING.md` exists.
- The developer machine is trusted. `copier copy --trust` and hooks run code
  there. No sandbox or verification layer protects that machine.
- Local tools are trusted. Dependency metadata and lockfiles are not tampered
  with.
- The `glab` OAuth token is valid and uncompromised. A stolen token grants the
  user's GitLab access.
- Agent prompts and child inputs are not adversarial. Claude denies patterns.
  It is not a complete sandbox. Other frontends may allow other commands.
- Generated-project owners run Copier for their own projects. The slug risk
  assumes this. Automated use with untrusted `--data` is outside the threat
  model.
