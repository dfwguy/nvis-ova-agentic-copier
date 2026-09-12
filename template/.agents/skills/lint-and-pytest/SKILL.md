---
name: lint-and-pytest
description: Run the repository's Python lint, format, type, and test checks, and repair failures when the user requests fixes.
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

# Lint and Pytest

Use this skill to validate a Python change or diagnose and repair failures in
the standard Python quality gates. It is not a replacement for the repository
workflow's design, review, or publication requirements.

Run the checks appropriate to the request:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

Run `uv run tox` when the request needs the full supported-version matrix or
the repository workflow requires final validation.

When asked to fix failures, address one root cause at a time and rerun the
affected check before continuing. Do not apply automatic fixes or change files
for a validation-only request; report the failures instead.
