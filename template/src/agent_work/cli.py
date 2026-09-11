"""Command-line interface for recoverable agent-work operations."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .domain import CommandRequest
from .ledger import LockBusy
from .models import CommandReport
from .runtime import dispatch as default_dispatch


def _positive_integer(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _common(
    parser: argparse.ArgumentParser,
    *,
    apply: bool = True,
    apply_help: str = "apply the operation",
) -> None:
    if apply:
        parser.add_argument("--apply", action="store_true", help=apply_help)
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    parser.add_argument(
        "--debug", action="store_true", help="show a traceback on failure"
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the nested resource/action command surface."""
    parser = argparse.ArgumentParser(prog="agent-work")
    resources = parser.add_subparsers(dest="resource", required=True)
    loop = resources.add_parser("loop", help="inspect or advance the serial loop")
    loop_actions = loop.add_subparsers(dest="action", required=True)
    run = loop_actions.add_parser(
        "run",
        help="preview or run one bounded recoverable activation",
        description=(
            "Preview the next safe action without mutation, or use --apply to "
            "advance one bounded recoverable activation."
        ),
    )
    _common(
        run,
        apply_help=(
            "advance one bounded recoverable activation; omit for a read-only preview"
        ),
    )
    run.add_argument(
        "--timeout-seconds",
        type=_positive_integer,
        default=1800,
        help="maximum seconds for each workflow session (default: 1800)",
    )
    run.add_argument(
        "--max-segments",
        type=_positive_integer,
        default=16,
        help="maximum persisted workflow attempts in one applied activation (default: 16)",
    )
    run.add_argument(
        "--max-activation-seconds",
        type=_positive_integer,
        default=3600,
        help="maximum seconds for one applied activation (default: 3600)",
    )
    status = loop_actions.add_parser("status", help="show durable loop state")
    _common(status, apply=False)
    status.add_argument("--issue-iid", type=_positive_integer)
    retry = loop_actions.add_parser("retry", help="preview or retry one blocked item")
    _common(retry)
    retry.add_argument("--issue-iid", type=_positive_integer, required=True)

    migration = resources.add_parser("migration", help="inventory legacy evidence")
    migration_actions = migration.add_subparsers(dest="action", required=True)
    preview = migration_actions.add_parser(
        "preview", help="build a read-only migration plan"
    )
    _common(preview, apply=False)
    apply_parser = migration_actions.add_parser(
        "apply", help="apply an exact reviewed plan"
    )
    _common(apply_parser)
    apply_parser.add_argument("--plan-file", type=Path, required=True)
    return parser


def _stdout(message: str) -> None:
    sys.stdout.write(f"{message}\n")


def _stderr(message: str) -> None:
    sys.stderr.write(f"{message}\n")


def _render(report: CommandReport, *, as_json: bool) -> str:
    if as_json:
        return report.model_dump_json(exclude_none=True)
    details = [report.outcome]
    if report.stop_reason:
        details.append(f"stop_reason={report.stop_reason}")
    if report.items:
        item = report.items[-1]
        details.extend(
            (f"issue=#{item.issue_iid}", f"state={item.state}", f"phase={item.phase}")
        )
        if item.next_action:
            details.append(f"next_action={item.next_action}")
    return " ".join(details)


def main(
    arguments: Sequence[str] | None = None,
    *,
    dispatch: Callable[..., CommandReport] = default_dispatch,
    write_stdout: Callable[[str], None] = _stdout,
    write_stderr: Callable[[str], None] = _stderr,
) -> int:
    """Dispatch one command and return the documented exit status."""
    parsed = build_parser().parse_args(arguments)
    command = f"{parsed.resource}.{parsed.action}"
    request = CommandRequest(
        command=command,
        issue_iid=getattr(parsed, "issue_iid", None),
        plan_file=getattr(parsed, "plan_file", None),
        timeout_seconds=getattr(parsed, "timeout_seconds", 1800),
        max_segments=getattr(parsed, "max_segments", 16),
        max_activation_seconds=getattr(parsed, "max_activation_seconds", 3600),
        debug=parsed.debug,
    )
    try:
        report = dispatch(request, apply=getattr(parsed, "apply", False))
    except LockBusy as error:
        logging.getLogger("agent-work").error("failure=%s", error)
        write_stderr(str(error))
        return 75
    except (RuntimeError, ValueError, OSError) as error:
        logging.getLogger("agent-work").error("failure=%s", error)
        if parsed.debug:
            raise
        write_stderr(str(error))
        return 1
    logging.getLogger("agent-work").info(
        "outcome=%s command=%s", report.outcome, report.command
    )
    write_stdout(_render(report, as_json=parsed.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
