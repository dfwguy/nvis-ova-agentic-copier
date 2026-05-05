#!/usr/bin/env python3

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
            message = ":".join(
                message_parts,
            )  # Rejoin message parts in case message contains colons

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
            print(f"Warning: Could not parse line: {line}", file=sys.stderr)  # noqa: T201
            continue

    return issues


def main() -> None:
    """Read yamllint output from stdin and convert to GitLab code quality JSON format.

    Reads yamllint parsable output from standard input, parses it, and outputs
    the converted GitLab code quality format as JSON to standard output.
    """
    # Read from stdin
    lines = sys.stdin.readlines()

    # Parse and convert to GitLab code quality format
    issues = parse_yamllint_output(lines)

    # Output as JSON
    print(json.dumps(issues, indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
