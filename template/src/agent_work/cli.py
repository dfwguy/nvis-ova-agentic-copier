"""Command-line entry point for one serial GitLab work-item pass."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from .adapters import AdapterError, LockBusy, build_default_dependencies
from .models import RunReport
from .serial import RunNextDependencies, RunNextError, run_next


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the deliberately small public command surface."""
    parser = argparse.ArgumentParser(prog="agent-work")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser(
        "run-next", help="preview or process one GitLab issue"
    )
    command.add_argument(
        "--apply", action="store_true", help="claim and process the selected issue"
    )
    command.add_argument(
        "--json", action="store_true", help="emit one safe JSON report"
    )
    command.add_argument(
        "--timeout-seconds",
        type=_positive_integer,
        default=1800,
        metavar="SECONDS",
        help="bound the single Codex invocation (default: 1800)",
    )
    return parser


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _render(report: RunReport, *, as_json: bool) -> str:
    payload = {
        "branch": report.branch,
        "issue_iid": report.issue_iid,
        "merge_request_url": report.merge_request_url,
        "outcome": report.outcome,
    }
    if as_json:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    details: list[str] = [report.outcome]
    if report.issue_iid is not None:
        details.append(f"issue=#{report.issue_iid}")
    if report.branch is not None:
        details.append(f"branch={report.branch}")
    if report.merge_request_url is not None:
        details.append(f"merge_request={report.merge_request_url}")
    return " ".join(details)


def main(
    arguments: Sequence[str] | None = None,
    *,
    dependencies_factory: Callable[
        [], RunNextDependencies
    ] = build_default_dependencies,
    run: Callable[..., RunReport] = run_next,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Run one preview or applied pass and return its documented exit code."""
    parsed = build_parser().parse_args(arguments)
    write_stderr("Checking for next work item...")
    try:
        report = run(
            dependencies_factory(),
            apply=parsed.apply,
            timeout_seconds=parsed.timeout_seconds,
            report_progress=write_stderr,
        )
    except LockBusy as error:
        write_stderr(str(error))
        return 75
    except (AdapterError, RunNextError, ValueError) as error:
        write_stderr(str(error))
        return 1
    if report.failure_reason is not None:
        write_stderr(f"Failure: {report.failure_reason}")
    if report.agent_diagnostic is not None:
        write_stderr(f"Codex diagnostic (redacted):\n{report.agent_diagnostic}")
    write_stdout(_render(report, as_json=parsed.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
