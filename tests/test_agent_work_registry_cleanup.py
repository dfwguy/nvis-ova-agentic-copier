# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only container-registry cleanup check."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_work import cli, registry_cleanup


def _project(
    *,
    registry_access: str = "enabled",
    policy: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": 310967,
        "web_url": "https://gitlab.example.test/factory/copier",
        "container_registry_access_level": registry_access,
    }
    if policy is not None:
        payload["container_expiration_policy"] = dict(policy)
    return payload


def _complete_policy(**overrides: object) -> dict[str, object]:
    return {
        "enabled": True,
        "cadence": "7d",
        "keep_n": 20,
        "older_than": "14d",
        "name_regex_delete": r"^[0-9a-f]{40}-[0-9]+$",
        "name_regex": r"^legacy-release-.*$",
        "name_regex_keep": r"^(?:release-.*)$",
    } | overrides


def _run_with(
    payload: Mapping[str, object] | str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[tuple[str, ...]]]:
    commands: list[tuple[str, ...]] = []
    stdout = payload if isinstance(payload, str) else json.dumps(payload)

    def run(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=stderr
        )

    return run, commands


def test_fetch_policy_uses_the_absolute_glab_numeric_project_endpoint_and_timeout() -> (
    None
):
    """The adapter can only read the configured numeric project through glab."""
    commands: list[tuple[str, ...]] = []
    timeouts: list[object] = []

    def run(
        command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        timeouts.append(kwargs.get("timeout_seconds"))
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_project(policy=_complete_policy()))
        )

    observed = registry_cleanup.fetch_registry_cleanup_policy(
        host="gitlab.example.test",
        project_id="310967",
        glab_path=Path("/usr/local/bin/glab"),
        run_command=run,
    )

    assert observed.project_id == 310967
    assert observed.web_url == "https://gitlab.example.test/factory/copier"
    assert commands == [
        (
            "/usr/local/bin/glab",
            "api",
            "--hostname",
            "gitlab.example.test",
            "projects/310967",
        )
    ]
    assert len(timeouts) == 1
    assert isinstance(timeouts[0], int) and 0 < timeouts[0] <= 60


@pytest.mark.parametrize(
    ("configured_id", "payload", "message"),
    (
        ("not-a-number", _project(), "numeric"),
        ("310967", _project() | {"id": 99}, "identity"),
        ("310967", _project() | {"web_url": "https://other.example.test/a/b"}, "host"),
        ("310967", _project() | {"web_url": "not-a-url"}, "web_url"),
    ),
)
def test_fetch_policy_rejects_malformed_or_mismatched_project_identity(
    configured_id: str, payload: Mapping[str, object], message: str
) -> None:
    """Project ID and URL identity are boundary failures, never advisory output."""
    run, _commands = _run_with(payload)

    with pytest.raises(registry_cleanup.RegistryBoundaryError, match=message):
        registry_cleanup.fetch_registry_cleanup_policy(
            host="gitlab.example.test",
            project_id=configured_id,
            glab_path=Path("/usr/local/bin/glab"),
            run_command=run,
        )


@pytest.mark.parametrize(
    "web_url",
    (
        "https://gitlab.example.test?query=value",
        "https://gitlab.example.test/factory/copier#fragment",
        "https://user@gitlab.example.test/factory/copier",
        "https://gitlab.example.test/",
    ),
)
def test_fetch_policy_rejects_unsafe_project_urls(web_url: str) -> None:
    """Settings navigation requires a clean project-specific HTTPS URL."""
    run, _commands = _run_with(_project() | {"web_url": web_url})

    with pytest.raises(registry_cleanup.RegistryBoundaryError, match="host"):
        registry_cleanup.fetch_registry_cleanup_policy(
            host="gitlab.example.test",
            project_id="310967",
            glab_path=Path("/usr/local/bin/glab"),
            run_command=run,
        )


def test_fetch_policy_rejects_malformed_nested_policy_fields() -> None:
    """Typed nested API fields reject malformed boundary data."""
    run, _commands = _run_with(_project(policy={"enabled": "yes"}))

    with pytest.raises(registry_cleanup.RegistryBoundaryError):
        registry_cleanup.fetch_registry_cleanup_policy(
            host="gitlab.example.test",
            project_id="310967",
            glab_path=Path("/usr/local/bin/glab"),
            run_command=run,
        )


@pytest.mark.parametrize("policy", (None, {}, {"enabled": False}))
def test_evaluator_returns_successful_advisory_when_policy_is_missing_restricted_or_disabled(
    policy: Mapping[str, object] | None,
) -> None:
    """Unavailable cleanup-policy visibility is actionable but is not an API failure."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(_project(policy=policy))
    )

    assert assessment.healthy is False
    assert assessment.exit_code == 0
    assert assessment.outcome == "advisory"


def test_evaluator_returns_concise_non_warning_success_when_registry_is_disabled() -> (
    None
):
    """Cleanup-policy advice is inapplicable when the registry itself is disabled."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(registry_access="disabled", policy=_complete_policy(enabled=False))
        )
    )

    assert assessment.healthy is False
    assert assessment.exit_code == 0
    assert assessment.outcome == "not_applicable"
    assert "warning" not in assessment.human_message.lower()
    assert "Maintainer/Owner" not in assessment.human_message


def test_evaluator_reports_healthy_for_enabled_complete_policy_that_deletes_transient_tags() -> (
    None
):
    """A complete enabled policy is healthy only when it covers transient validation tags."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(policy=_complete_policy())
        )
    )

    assert assessment.healthy is True
    assert assessment.exit_code == 0
    assert assessment.outcome == "healthy"
    assert "warning" not in assessment.human_message.lower()


def test_evaluator_uses_legacy_delete_regex_when_modern_field_is_omitted() -> None:
    """The still-emitted deprecated field remains compatible during API migration."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(
                policy=_complete_policy(
                    name_regex_delete=None,
                    name_regex=r"^[0-9a-f]{40}-[0-9]+$",
                )
            )
        )
    )

    assert assessment.healthy is True
    assert assessment.effective_delete_regex == r"^[0-9a-f]{40}-[0-9]+$"


def test_evaluator_prefers_modern_delete_regex_over_conflicting_legacy_field() -> None:
    """Mixed-generation field conflict resolves deterministically to the modern field."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(
                policy=_complete_policy(
                    name_regex_delete=r"^[0-9a-f]{40}-[0-9]+$",
                    name_regex=r"^release-.*$",
                )
            )
        )
    )

    assert assessment.healthy is True
    assert assessment.effective_delete_regex == r"^[0-9a-f]{40}-[0-9]+$"


def test_evaluator_accepts_semantically_matching_delete_regex() -> None:
    """Health follows regex semantics rather than one literal pattern."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(policy=_complete_policy(name_regex_delete=r".*"))
        )
    )

    assert assessment.healthy is True


@pytest.mark.parametrize("field", ("name_regex_delete", "name_regex_keep"))
def test_evaluator_treats_invalid_present_regex_as_advisory(field: str) -> None:
    """Malformed present regexes are unsafe rather than omitted configuration."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(policy=_complete_policy(**{field: "["}))
        )
    )

    assert assessment.outcome == "advisory"


def test_evaluator_allows_omitted_keep_regex_when_delete_regex_is_safe() -> None:
    """No keep expression creates no extra tag-name exclusion."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(policy=_complete_policy(name_regex_keep=None))
        )
    )

    assert assessment.healthy is True


@pytest.mark.parametrize(
    "policy",
    (
        _complete_policy(enabled=False),
        _complete_policy(cadence=None),
        _complete_policy(keep_n=None),
        _complete_policy(older_than=None),
        _complete_policy(name_regex_delete=None, name_regex=None),
        _complete_policy(name_regex_delete=r"^release-.*$"),
        _complete_policy(name_regex_keep=r".*"),
    ),
)
def test_evaluator_advises_when_policy_is_incomplete_or_cannot_delete_transient_tags(
    policy: Mapping[str, object],
) -> None:
    """Disabled, incomplete, or over-excluding policies must not look healthy."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(_project(policy=policy))
    )

    assert assessment.healthy is False
    assert assessment.outcome == "advisory"


@given(
    enabled=st.booleans(),
    registry_enabled=st.booleans(),
    delete_regex=st.sampled_from((r"^[0-9a-f]{40}-[0-9]+$", r"^release-.*$", None)),
    keep_regex=st.sampled_from((r"^(?:release-.*)$", r".*", None)),
)
def test_evaluator_never_calls_an_incomplete_or_non_deleting_policy_healthy(
    enabled: bool,
    registry_enabled: bool,
    delete_regex: str | None,
    keep_regex: str | None,
) -> None:
    """Health requires enabled registry and policy plus both safe regex properties."""
    policy = _complete_policy(
        enabled=enabled, name_regex_delete=delete_regex, name_regex_keep=keep_regex
    )
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(
                registry_access="enabled" if registry_enabled else "disabled",
                policy=policy,
            )
        )
    )

    if assessment.healthy:
        assert registry_enabled and enabled
        assert delete_regex is not None
        assert keep_regex != r".*"


def test_advisory_names_every_observed_field_and_the_operator_handoff() -> None:
    """Advice preserves evidence and routes policy changes to a privileged human."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(policy=_complete_policy(enabled=False))
        )
    )

    message = assessment.human_message
    for value in (
        "container_registry_access_level=enabled",
        "enabled=False",
        "cadence=7d",
        "keep_n=20",
        "older_than=14d",
        "name_regex_delete=^[0-9a-f]{40}-[0-9]+$",
        "name_regex=^legacy-release-.*$",
        "name_regex_keep=^(?:release-.*)$",
        "$CI_COMMIT_SHA-$CI_PIPELINE_ID",
        "BuildKit cache",
        "Maintainer/Owner",
        "https://gitlab.example.test/factory/copier/-/settings/packages_and_registries",
        "Settings → Packages and registries → Container registry → Cleanup policies",
    ):
        assert value in message
    assert "\n" in message


def test_adapter_failure_is_distinct_and_does_not_echo_credentials() -> None:
    """A failed API invocation is a hard failure without secret-bearing diagnostics."""
    run, _commands = _run_with(
        _project(), returncode=1, stderr="token=super-secret authentication failed"
    )

    with pytest.raises(registry_cleanup.RegistryBoundaryError) as error:
        registry_cleanup.fetch_registry_cleanup_policy(
            host="gitlab.example.test",
            project_id="310967",
            glab_path=Path("/usr/local/bin/glab"),
            run_command=run,
        )

    assert "super-secret" not in str(error.value)


def test_cli_default_composition_emits_human_and_json_advisory() -> None:
    """The read-only CLI keeps `--apply` harmless while reporting advisories as success."""
    run, _commands = _run_with(_project(policy=None))
    human_stdout: list[str] = []
    json_stdout: list[str] = []
    stderr: list[str] = []
    environment = {
        "GITLAB_HOST": "gitlab.example.test",
        "GITLAB_PROJECT_ID": "310967",
        "AGENT_WORK_GLAB": "/usr/local/bin/glab",
    }

    human_status = cli.main(
        ["repository", "cleanup-policy", "--apply"],
        environment=environment,
        run_command=run,
        find_executable=lambda _name: None,
        write_stdout=human_stdout.append,
        write_stderr=stderr.append,
    )
    json_status = cli.main(
        ["repository", "cleanup-policy", "--json"],
        environment=environment,
        run_command=run,
        find_executable=lambda _name: None,
        write_stdout=json_stdout.append,
        write_stderr=stderr.append,
    )

    assert human_status == json_status == 0
    assert stderr == []
    assert "Maintainer/Owner" in human_stdout[0]
    assert json.loads(json_stdout[0])["outcome"] == "advisory"
    payload = json.loads(json_stdout[0])
    assert payload["applied"] is False and payload["read_only"] is True
    assert payload["settings_url"].endswith("/-/settings/packages_and_registries")
    assert isinstance(payload["observed"], dict)


def test_cli_help_and_invalid_arguments_have_documented_exit_codes() -> None:
    """Help succeeds while argparse failures keep their normal exit status."""
    with pytest.raises(SystemExit) as help_exit:
        cli.main(["repository", "cleanup-policy", "--help"])
    with pytest.raises(SystemExit) as usage_exit:
        cli.main(["repository", "cleanup-policy", "--unexpected"])

    assert help_exit.value.code == 0
    assert usage_exit.value.code == 2


def test_private_registry_with_a_healthy_policy_is_healthy() -> None:
    """Private access is active and receives the same cleanup assessment."""
    assessment = registry_cleanup.evaluate_cleanup_policy(
        registry_cleanup.RegistryCleanupPolicy.from_project(
            _project(registry_access="private", policy=_complete_policy())
        )
    )
    assert assessment.healthy is True


def test_agent_work_cli_forwards_cleanup_command_to_injected_check_and_renders_result() -> (
    None
):
    """The installed command surface retains the registry check's read-only semantics."""
    calls: list[bool] = []
    stdout: list[str] = []
    stderr: list[str] = []

    def registry_cleanup_action(*, apply: bool) -> registry_cleanup.CleanupAssessment:
        calls.append(apply)
        return registry_cleanup.CleanupAssessment(
            healthy=False,
            outcome="advisory",
            exit_code=0,
            human_message="Maintainer/Owner review required.",
            json_payload={"outcome": "advisory", "healthy": False},
        )

    status = cli.main(
        ["repository", "cleanup-policy", "--json", "--apply"],
        registry_cleanup_action=registry_cleanup_action,
        write_stdout=stdout.append,
        write_stderr=stderr.append,
    )

    assert status == 0
    assert calls == [True]
    assert stderr == []
    assert json.loads(stdout[0]) == {"outcome": "advisory", "healthy": False}


def test_root_and_template_registry_cleanup_modules_remain_byte_identical(
    project_root: Path,
) -> None:
    """The generated agent receives the same read-only cleanup behavior."""
    assert (project_root / "src/agent_work/registry_cleanup.py").read_bytes() == (
        project_root / "template/src/agent_work/registry_cleanup.py"
    ).read_bytes()
