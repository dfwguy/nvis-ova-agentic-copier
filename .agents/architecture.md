<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Optional architecture for a new Web UI application

Use this guide only for the first design of a new app that gathers outside
data and shows it in a Web UI or CLI. First confirm that it fits the project.
The project owner and `.agents/project.md` choose the app's design and tools.

## Design the seams

- **Collector:** Give each outside source a small interface. Name its access
  rules, limits, errors, and run results. Keep its parsing out of the UI.
- **Storage:** Name the saved records and their IDs. Decide how repeat runs
  change them. Hide storage details from the collector and API.
- **API:** Name the actions and data shapes used by the UI or CLI. Check all
  input here. Hide jobs and storage details from callers.
- **UI and CLI:** Call the API or another app boundary. Keep display and
  command code out of the collector and storage.

Trace one record from source to screen or command output. State who owns each
retry, error, and access check. Name the seams and data before choosing tools.

## Confirm technology choices

If the app needs Python HTTP routes, propose FastAPI with Pydantic for data at
the edge. If it needs a separate Web server or proxy, propose Caddy. These are
ideas that need human and project approval. They are not defaults. Record any
approved choice and its reason in `.agents/project.md` before coding. Follow
the project's Python, security, and deploy rules.
