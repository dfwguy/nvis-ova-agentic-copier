# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the nonpersistent GitLab Git credential helper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(params=(Path("."), Path("template")))
def credential_helper(request: pytest.FixtureRequest) -> Path:
    """Return each maintained copy of the shared credential helper."""
    return Path(request.param) / "src" / "agent-work-git-credential.sh"


def _run_helper(
    credential_helper: Path,
    *arguments: str,
    token: str | None,
    credential_request: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"GITLAB_HOST": "gitlab.example.test"}
    if token is not None:
        environment["GITLAB_TOKEN"] = token
    else:
        environment.pop("GITLAB_TOKEN", None)
    return subprocess.run(
        [str(credential_helper), *arguments],
        input=credential_request,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_credential_helper_emits_gitlab_basic_credentials_only_for_matching_https_get(
    credential_helper: Path,
) -> None:
    token = "test-only-gitlab-token"

    completed = _run_helper(
        credential_helper,
        "get",
        token=token,
        credential_request="protocol=https\nhost=gitlab.example.test\npath=group/project.git\n\n",
    )

    assert completed.returncode == 0
    assert completed.stdout == f"username=oauth2\npassword={token}\n\n"
    assert completed.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "credential_request", "token"),
    (
        (
            ("erase",),
            "protocol=https\nhost=gitlab.example.test\n\n",
            "test-only-gitlab-token",
        ),
        ((), "protocol=https\nhost=gitlab.example.test\n\n", "test-only-gitlab-token"),
        (
            ("get", "extra"),
            "protocol=https\nhost=gitlab.example.test\n\n",
            "test-only-gitlab-token",
        ),
        (
            ("get",),
            "protocol=http\nhost=gitlab.example.test\n\n",
            "test-only-gitlab-token",
        ),
        (
            ("get",),
            "protocol=https\nhost=untrusted.example.test\n\n",
            "test-only-gitlab-token",
        ),
        (
            ("get",),
            "protocol=https\nhost=gitlab.example.test\n\n",
            None,
        ),
        (
            ("get",),
            "protocol=https\nhost=gitlab.example.test\n\n",
            "",
        ),
    ),
)
def test_credential_helper_fails_closed_without_echoing_credentials(
    credential_helper: Path,
    arguments: tuple[str, ...],
    credential_request: str,
    token: str | None,
) -> None:
    completed = _run_helper(
        credential_helper,
        *arguments,
        token=token,
        credential_request=credential_request,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "test-only-gitlab-token" not in completed.stderr
    assert completed.stderr


@pytest.mark.parametrize(
    "host", ("https://gitlab.example.test", "gitlab.example.test/path")
)
def test_credential_helper_rejects_a_malformed_configured_host(
    credential_helper: Path, host: str
) -> None:
    environment = os.environ | {
        "GITLAB_HOST": host,
        "GITLAB_TOKEN": "test-only-gitlab-token",
    }
    completed = subprocess.run(
        [str(credential_helper), "get"],
        input="protocol=https\nhost=gitlab.example.test\n\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "test-only-gitlab-token" not in completed.stderr
