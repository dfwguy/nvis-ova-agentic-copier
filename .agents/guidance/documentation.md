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

Use this discipline only when content alone changes under `AGENTS.md`.
Reclassify mixed, executable, or uncertain work to the coding workflow. Do
this before editing.

- Treat agent instructions as policy. They are operational, not incidental
  metadata.
- Write human-facing prose in plain language. Keep it clear. Keep it plain. Lead with the problem, impact,
  intended outcome or requested action, and validation. State each fact once.
  Keep receipts, structured result envelopes, and correction ledgers in the
  durable audit. Do not put ordinary prose or transcripts in code formatting.
  This includes inline and fenced code. Use code formatting only for commands
  and identifiers. Do not force a fixed heading template on every document.
- Check claims against source files. Do not lock prose to phrases.
- Preserve declared root and `template/` mirrors.
- Smoke-render Copier output when rendered Markdown changes. Do this when
  policy changes too.
- Validate structure, navigation, links, and front matter. Validate policy
  consistency too.
- Do not create behavior tests just to justify prose changes.
