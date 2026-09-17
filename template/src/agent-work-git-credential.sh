#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
set -Eeuo pipefail

fail() {
  printf 'agent-work Git credential request rejected\n' >&2
  exit 1
}

[[ "${1:-}" == get && $# == 1 ]] || fail
host="${GITLAB_HOST:-}"
[[ "$host" != "" && "$host" != *://* && "$host" != */* ]] || fail
token="${GITLAB_TOKEN:-}"
[[ -n "$token" ]] || fail

protocol=""
request_host=""
while IFS= read -r line; do
  [[ -n "$line" ]] || break
  case "$line" in
    protocol=*) protocol="${line#protocol=}" ;;
    host=*) request_host="${line#host=}" ;;
  esac
done
[[ "$protocol" == https && "$request_host" == "$host" ]] || fail

printf 'username=oauth2\npassword=%s\n\n' "$token"
