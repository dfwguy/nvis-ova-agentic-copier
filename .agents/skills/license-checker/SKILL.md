---
name: license-checker
description: Verify maintained files comply with the repository license policy.
metadata:
  model-tier: fast
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

# License Checker

Read and follow [the role instructions](../../agents/license-checker.md) before
running the repository checker after all required reviews and before finalization:

```bash
uv run python scripts/check_licenses.py --root .
```

This phase is read-only. Do not add headers, annotations, exclusions, or policy
changes. Return an approval receipt when the command exits successfully. On
failure, report each diagnostic and reject to the phase that introduced the
noncompliant file.

## Model selection

Request the `fast` capability tier defined in `AGENTS.md`.
