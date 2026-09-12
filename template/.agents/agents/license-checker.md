---
name: license-checker
description: Read-only license-policy reviewer returning a receipt to Green.
tools: Read, Grep, Glob, Bash
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

Run `uv run python scripts/check_licenses.py --root .` after Phase 4 reviews.
Do not edit files. Return an approval receipt to Green when it succeeds;
otherwise return every diagnostic to the phase that introduced the violation.
