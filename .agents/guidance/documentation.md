<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Documentation and Agent Policy

Use this guide only for content-only work under `AGENTS.md`. Route mixed,
executable, or unclear work to coding before you edit.

- Treat agent instructions as policy, not metadata.
- Write human-facing prose in plain language. Lead with the problem and impact.
  Then state the outcome or needed action and validation. State each fact once.
  Keep receipts, result envelopes, and repair ledgers in the durable audit.
  Do not format normal prose or transcripts as code. Use code format only for
  commands and IDs. Do not force fixed headings on every file.
- In human-facing status and merge-request prose, use descriptive workflow
  stage names. Do not use ordinal workflow labels or abbreviated correction
  labels. Say issue number or merge request number instead of unexplained IID.
  If the GitLab term is necessary, define internal ID (IID) once.
- Check each claim against its source. Do not lock prose to a phrase.
- Preserve declared root and `template/` mirrors.
- Smoke-render Copier output when rendered Markdown or policy changes.
- Check structure, navigation, links, front matter, and policy consistency.
- Do not add behavior tests just to support prose changes.
