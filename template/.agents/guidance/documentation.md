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

Use this discipline only when the whole change is content-only under
`AGENTS.md`. Reclassify mixed, executable, or uncertain work to the coding
workflow before editing.

- Treat agent instructions as operational policy, not incidental metadata.
- Write human-facing prose in plain language: lead with the problem, impact,
  intended outcome or requested action, and validation; state each fact once.
  Keep receipts, structured result envelopes, and correction ledgers in the
  durable audit. Do not wrap ordinary prose or transcripts in inline or fenced
  code formatting; use code formatting only for commands and identifiers. Do
  not force a fixed heading template onto every document.
- Check claims against their source files instead of locking prose to phrases.
- Preserve declared root and `template/` mirrors.
- Smoke-render Copier output when rendered Markdown or policy changes.
- Validate structure, navigation, links, front matter, and policy consistency.
- Do not create behavior tests merely to justify prose changes.
