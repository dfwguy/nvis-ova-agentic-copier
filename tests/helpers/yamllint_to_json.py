#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.

"""Convert yamllint output to GitLab code quality JSON format.

This module provides functionality to parse yamllint parsable output and convert it
to GitLab's code quality report format for integration with CI/CD pipelines.
"""

import json
import sys


def parse_yamllint_output(lines: list[str]) -> list[dict]:
    """Convert yamllint parsable output to GitLab code quality format."""
    issues = []

    for line in lines:
        if not line.strip():
            continue

        # yamllint parsable format: file:line:col:level:message
        try:
            file_path, line_num, col_num, level, *message_parts = line.strip().split(
                ":",
            )
            message = ":".join(message_parts)

            issue = {
                "description": message,
                "fingerprint": f"{file_path}:{line_num}:{col_num}:{message}",
                "severity": "major" if level == "error" else "minor",
                "location": {
                    "path": file_path,
                    "lines": {"begin": int(line_num), "end": int(line_num)},
                },
            }
            issues.append(issue)
        except ValueError:
            print(f"Warning: Could not parse line: {line}", file=sys.stderr)
            continue

    return issues


def main() -> None:
    """Read yamllint output from stdin and convert to GitLab code quality JSON format.

    Reads yamllint parsable output from standard input, parses it, and outputs
    the converted GitLab code quality format as JSON to standard output.
    """
    lines = sys.stdin.readlines()
    issues = parse_yamllint_output(lines)
    print(json.dumps(issues, indent=2))


if __name__ == "__main__":
    main()
