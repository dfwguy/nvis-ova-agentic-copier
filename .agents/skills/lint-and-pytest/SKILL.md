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

Use this skill to check Python changes. It can also find and fix failures in
the standard Python checks. It does not replace design, review, or publication
in the repository workflow.

Run the checks that fit the request:

```bash
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
```

Run `uv run tox` when the request needs the full supported-version matrix or
the workflow needs final validation.

When asked to fix a failure, fix one root cause at a time. Rerun that check
before you continue. For validation only, do not use automatic fixes. Do not
change files. Report failures.
