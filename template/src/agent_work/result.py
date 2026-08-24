"""Canonical result-envelope validation."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Self

from jsonschema import Draft202012Validator, FormatChecker


class ResultValidationError(ValueError):
    """Raised when an agent result is not a canonical Phase 1 result."""


class _FrozenList(tuple[Any, ...]):
    """Immutable sequence retaining normal JSON-list equality."""

    def __eq__(self, other: object) -> bool:
        """Compare logically with JSON arrays as well as tuples."""
        return (
            tuple(self) == tuple(other) if isinstance(other, (list, tuple)) else False
        )


class FrozenDocument(dict[str, Any]):
    """A statically mutable dictionary that rejects all runtime mutation."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        """Populate the document before public mutation guards take effect."""
        dict.__init__(self, values)

    def __setitem__(self, key: str, value: Any) -> None:
        """Reject public item assignment after validation."""
        raise TypeError("validated result documents are immutable")

    def __delitem__(self, key: str) -> None:
        """Reject public item deletion after validation."""
        raise TypeError("validated result documents are immutable")

    def clear(self) -> None:
        """Reject public clearing after validation."""
        raise TypeError("validated result documents are immutable")

    def pop(self, key: str, default: Any = None) -> Any:
        """Reject public removal after validation."""
        raise TypeError("validated result documents are immutable")

    def popitem(self) -> tuple[str, Any]:
        """Reject public removal after validation."""
        raise TypeError("validated result documents are immutable")

    def setdefault(self, key: str, default: Any = None) -> Any:
        """Reject public updates after validation."""
        raise TypeError("validated result documents are immutable")

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Reject public updates after validation."""
        raise TypeError("validated result documents are immutable")

    def __ior__(self, other: object) -> Self:
        """Reject in-place dictionary union after validation."""
        raise TypeError("validated result documents are immutable")


@dataclass(frozen=True)
class AgentResult:
    """Validated result data retained by the runner."""

    outcome: Literal["completed", "awaiting_human", "retry", "dead_letter"]
    branch: str | None
    branch_tip: str | None
    merge_request_url: str | None
    document: dict[str, Any]


def _freeze(value: Any) -> Any:
    """Recursively freeze a validated boundary document."""
    if isinstance(value, Mapping):
        return FrozenDocument({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    return value


def validate_agent_result(
    document: Mapping[str, Any],
    *,
    work_item_id: str,
    run_id: str,
    mode: str,
    schema: Mapping[str, Any],
) -> AgentResult:
    """Validate a result against its injected schema and claimed identity."""
    errors = Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
        document
    )
    first_error = next(errors, None)
    if first_error is not None:
        raise ResultValidationError(first_error.message)
    if document.get("work_item_id") != work_item_id or document.get("run_id") != run_id:
        raise ResultValidationError("result does not belong to this work item and run")
    if document.get("mode") != mode:
        raise ResultValidationError("result mode does not match the requested mode")
    return AgentResult(
        outcome=document["outcome"],
        branch=document.get("branch"),
        branch_tip=document.get("branch_tip"),
        merge_request_url=document.get("merge_request_url"),
        document=_freeze(document),
    )
