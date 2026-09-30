# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""CLI transport for identity-bound committed candidate reads."""

from __future__ import annotations

import json

import pytest

from agent_work.cli import main


def test_committed_inventory_cli_preview_and_applied_restart() -> None:
    sha = "c" * 40
    tree = "d" * 40
    paths = [f"docs/file-{number:02}.md" for number in range(36)]
    calls: list[tuple[int, int, str, str, str, dict[str, object]]] = []

    def committed_candidate_read(
        issue_iid: int,
        mr_iid: int,
        candidate_sha: str,
        candidate_tree: str,
        role: str,
        selector: dict[str, object],
    ) -> dict[str, object]:
        calls.append((issue_iid, mr_iid, candidate_sha, candidate_tree, role, selector))
        return {"candidate_tree": tree, "changed_paths": paths, "files": []}

    args = [
        "candidate",
        "committed-changed-paths",
        "--issue-iid",
        "12",
        "--mr-iid",
        "88",
        "--candidate-sha",
        sha,
        "--candidate-tree",
        tree,
        "--role",
        "judge",
        "--json",
    ]
    output: list[str] = []
    assert (
        main(
            args,
            committed_candidate_read=committed_candidate_read,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop())["applied"] is False
    assert calls == []

    # A new CLI invocation has no live socket or nonce from the earlier process.
    assert (
        main(
            [*args, "--apply"],
            committed_candidate_read=committed_candidate_read,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop())["changed_paths"] == paths
    assert calls == [(12, 88, sha, tree, "judge", {"mode": "changed_paths"})]


def test_committed_range_cli_preview_then_apply_routes_exact_identity_and_selector() -> (
    None
):
    sha, tree = "c" * 40, "d" * 40
    selector = {"path": "docs/a.md", "side": "new", "offset": 2, "length": 3}
    calls: list[tuple[int, int, str, str, str, dict[str, object]]] = []

    def committed_candidate_read(
        issue_iid: int,
        mr_iid: int,
        candidate_sha: str,
        candidate_tree: str,
        role: str,
        requested_selector: dict[str, object],
    ) -> dict[str, object]:
        calls.append(
            (issue_iid, mr_iid, candidate_sha, candidate_tree, role, requested_selector)
        )
        return {
            "candidate_sha": sha,
            "candidate_tree": tree,
            "mr_iid": mr_iid,
            "role": role,
            **selector,
            "data_base64": "dGVz",
        }

    args = [
        "candidate",
        "committed-read",
        "--issue-iid",
        "12",
        "--mr-iid",
        "88",
        "--candidate-sha",
        sha,
        "--candidate-tree",
        tree,
        "--role",
        "judge",
        "--path",
        "docs/a.md",
        "--side",
        "new",
        "--offset",
        "2",
        "--length",
        "3",
        "--json",
    ]
    output: list[str] = []
    assert (
        main(
            args,
            committed_candidate_read=committed_candidate_read,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop()) == {
        "action": "committed-read",
        "applied": False,
        "candidate_sha": sha,
        "candidate_tree": tree,
        "issue_iid": 12,
        "mr_iid": 88,
        "role": "judge",
        "selector": selector,
    }
    assert calls == []
    assert (
        main(
            [*args, "--apply"],
            committed_candidate_read=committed_candidate_read,
            write_stdout=output.append,
        )
        == 0
    )
    assert json.loads(output.pop())["data_base64"] == "dGVz"
    assert calls == [(12, 88, sha, tree, "judge", selector)]


@pytest.mark.parametrize(("option", "value"), [("--offset", "-1"), ("--length", "0")])
def test_committed_range_cli_rejects_invalid_range_without_external_read(
    option: str,
    value: str,
) -> None:
    calls: list[object] = []
    args = [
        "candidate",
        "committed-read",
        "--issue-iid",
        "12",
        "--mr-iid",
        "88",
        "--candidate-sha",
        "c" * 40,
        "--candidate-tree",
        "d" * 40,
        "--role",
        "judge",
        "--path",
        "docs/a.md",
        "--side",
        "new",
        "--offset",
        "2",
        "--length",
        "3",
        "--apply",
    ]
    args[args.index(option) + 1] = value

    def unexpected_read(*call: object) -> dict[str, object]:
        calls.append(call)
        return {}

    assert main(args, committed_candidate_read=unexpected_read) == 2
    assert calls == []


def test_committed_range_cli_rejects_unknown_role_without_external_read() -> None:
    calls: list[object] = []
    args = [
        "candidate",
        "committed-read",
        "--issue-iid",
        "12",
        "--mr-iid",
        "88",
        "--candidate-sha",
        "c" * 40,
        "--candidate-tree",
        "d" * 40,
        "--role",
        "bogus",
        "--path",
        "docs/a.md",
        "--side",
        "new",
        "--offset",
        "0",
        "--length",
        "1",
    ]

    def unexpected_read(*call: object) -> dict[str, object]:
        calls.append(call)
        return {}

    try:
        status = main(args, committed_candidate_read=unexpected_read)
    except SystemExit as error:
        status = error.code
    assert status == 2
    assert calls == []


def test_committed_range_human_output_labels_and_escapes_identity() -> None:
    sha, tree = "c" * 40, "d" * 40
    output: list[str] = []
    receipt: dict[str, object] = {
        "candidate_sha": sha,
        "candidate_tree": tree,
        "mr_iid": 88,
        "role": "judge\x1b[31m",
        "path": "docs/a.md",
        "side": "new",
        "offset": 0,
        "length": 1,
        "data_base64": "YQ==",
    }
    args = [
        "candidate",
        "committed-read",
        "--issue-iid",
        "12",
        "--mr-iid",
        "88",
        "--candidate-sha",
        sha,
        "--candidate-tree",
        tree,
        "--role",
        "judge",
        "--path",
        "docs/a.md",
        "--side",
        "new",
        "--offset",
        "0",
        "--length",
        "1",
        "--apply",
    ]
    assert (
        main(
            args,
            committed_candidate_read=lambda *_: receipt,
            write_stdout=output.append,
        )
        == 0
    )
    rendered = output.pop()
    assert f"Commit: {sha}" in rendered
    assert "Merge request: 88" in rendered
    assert "Role: judge" in rendered
    assert "\x1b" not in rendered
