# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Behavior contracts for the read-only NVIDIA license checker."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from xml.etree import ElementTree

import pytest

_COPYRIGHT = (
    "SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. "
    "All rights reserved."
)
_LICENSE_ID = "SPDX-License-Identifier: LicenseRef-NvidiaProprietary"
_NOTICE = (
    "NVIDIA CORPORATION, its affiliates and licensors retain all intellectual\n"
    "property and proprietary rights in and to this material, related\n"
    "documentation and any modifications thereto. Any use, reproduction,\n"
    "disclosure or distribution of this material and related documentation\n"
    "without an express license agreement from NVIDIA CORPORATION or\n"
    "its affiliates is strictly prohibited."
)


def _commented_header(prefix: str) -> str:
    """Build the exact policy header for hash-comment formats."""
    lines = (_COPYRIGHT, _LICENSE_ID, "", *_NOTICE.splitlines())
    return "\n".join(f"{prefix}{line}".rstrip() for line in lines) + "\n"


def _markdown_header() -> str:
    """Build the exact policy header for Markdown formats."""
    lines = (_COPYRIGHT, _LICENSE_ID, "", *_NOTICE.splitlines())
    return "\n".join(f"<!-- {line} -->" for line in lines) + "\n"


def _run_checker(
    project_root: Path, scanned_root: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    """Run the public checker command against an isolated tree."""
    return subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts" / "check_licenses.py"),
            "--root",
            str(scanned_root),
            "--policy",
            str(project_root / ".license-policy.toml"),
            *arguments,
        ],
        capture_output=True,
        check=False,
        text=True,
    )


def _load_checker(project_root: Path) -> ModuleType:
    """Load the standalone checker to exercise its injected boundaries."""
    script = project_root / "scripts" / "check_licenses.py"
    spec = importlib.util.spec_from_file_location("license_checker_under_test", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Decode a successful JSON protocol response."""
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    return payload


def _run_fallback_checker(
    project_root: Path,
    scanned_root: Path,
    *,
    git_error: str = "fatal: not a git repository",
) -> tuple[int, dict[str, object]]:
    """Run the checker with an injected non-Git discovery boundary."""
    module = _load_checker(project_root)
    stdout, stderr = io.StringIO(), io.StringIO()

    def unavailable_repository(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 128, stdout="", stderr=git_error)

    status = module.main(
        [
            "--root",
            str(scanned_root),
            "--policy",
            str(project_root / ".license-policy.toml"),
            "--json",
        ],
        filesystem=module.LocalFilesystem(),
        output=module.Output(stdout, stderr),
        logger_factory=lambda _root: logging.getLogger("license-check.fallback"),
        run_command=unavailable_repository,
    )

    assert stderr.getvalue() == ""
    return status, json.loads(stdout.getvalue())


def test_checker_help_documents_the_read_only_interface(
    project_root: Path, tmp_path: Path
) -> None:
    """The public help names every supported read-only command boundary."""
    result = _run_checker(project_root, tmp_path, "--help")

    assert result.returncode == 0
    assert result.stderr == ""
    assert "canonical NVIDIA license header" in result.stdout
    for option in ("--root", "--policy", "--json", "--debug"):
        assert option in result.stdout
    assert "--apply" not in result.stdout


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("module.py", _commented_header("# ") + "VALUE = 1\n"),
        ("settings.toml", _commented_header("# ") + "enabled = true\n"),
        ("pipeline.yaml", _commented_header("# ") + "job: test\n"),
        ("guide.md", _markdown_header() + "# Guide\n"),
        ("run.sh", "#!/usr/bin/env bash\n" + _commented_header("# ") + "true\n"),
        ("unit.service", _commented_header("# ") + "[Service]\n"),
    ],
)
def test_checker_accepts_exact_headers_only_at_documented_leading_positions(
    project_root: Path, tmp_path: Path, path: str, content: str
) -> None:
    """Supported files accept an offset-zero header or a single shebang."""
    (tmp_path / path).write_text(content)
    (tmp_path / "frontmatter.md").write_text(
        "---\ntitle: Guide\n---\n" + _markdown_header() + "# Guide\n"
    )

    result = _run_checker(project_root, tmp_path, "--json")

    assert result.returncode == 0
    assert _payload(result) == {
        "schema_version": 1,
        "outcome": "compliant",
        "violations": [],
    }


@pytest.mark.parametrize(
    ("path", "content", "message"),
    [
        (
            "late.py",
            "VALUE = 1\n" + _commented_header("# "),
            "misplaced NVIDIA license header",
        ),
        (
            "blank-after-shebang.py",
            "#!/usr/bin/env python3\n\n" + _commented_header("# "),
            "misplaced NVIDIA license header",
        ),
        ("missing.py", "VALUE = 1\n", "missing NVIDIA license header"),
        ("opaque.unknown", "opaque\n", "unsupported file type"),
        ("data.json", "{}\n", "external license annotation"),
    ],
)
def test_checker_fails_closed_with_actionable_diagnostics(
    project_root: Path, tmp_path: Path, path: str, content: str, message: str
) -> None:
    """Misplaced, missing, and unclassifiable maintained files are violations."""
    (tmp_path / path).write_text(content)

    result = _run_checker(project_root, tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"{path}: {message}" in result.stderr


def test_checker_requires_annotation_for_symlinks(
    project_root: Path, tmp_path: Path
) -> None:
    """Traversal never follows a maintained symlink implicitly."""
    target = tmp_path / "target.py"
    target.write_text(_commented_header("# ") + "VALUE = 1\n")
    (tmp_path / "linked.py").symlink_to(target.name)

    result = _run_checker(project_root, tmp_path)

    assert result.returncode == 1
    assert "linked.py" in result.stderr
    assert "external license annotation" in result.stderr


def test_json_envelopes_and_apply_rejection_are_versioned(
    project_root: Path, tmp_path: Path
) -> None:
    """The CLI has compliant, violation, and usage-error JSON boundaries."""
    (tmp_path / "missing.py").write_text("VALUE = 1\n")
    violation = _run_checker(project_root, tmp_path, "--json")
    assert violation.returncode == 1
    assert _payload(violation) == {
        "schema_version": 1,
        "outcome": "violations",
        "violations": [
            {"path": "missing.py", "message": "missing NVIDIA license header"}
        ],
    }

    usage = _run_checker(project_root, tmp_path, "--json", "--apply")
    assert usage.returncode == 2
    assert _payload(usage)["outcome"] == "error"
    assert "read-only" in str(_payload(usage)["error"])


def test_main_keeps_parse_and_resolution_errors_inside_json_boundary(
    project_root: Path, tmp_path: Path
) -> None:
    """Injected argument and path failures become an exit-two JSON error."""
    module = _load_checker(project_root)
    stdout, stderr = io.StringIO(), io.StringIO()
    policy = tmp_path / "policy.toml"
    policy.write_text("")

    class ResolvingFilesystem(module.LocalFilesystem):
        def resolve(self, path: Path) -> Path:
            """Inject a policy-resolution failure at the filesystem boundary."""
            if path == policy:
                raise RuntimeError("injected policy resolution failure")
            return super().resolve(path)

    status = module.main(
        ["--root", str(tmp_path), "--policy", str(policy), "--json"],
        filesystem=ResolvingFilesystem(),
        output=module.Output(stdout, stderr),
    )

    assert status == 2
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == {
        "schema_version": 1,
        "outcome": "error",
        "error": "injected policy resolution failure",
    }


def test_main_uses_injected_git_discovery_and_reports_failure_as_json(
    project_root: Path, tmp_path: Path
) -> None:
    """Git discovery is injected, deterministic, and fail-closed."""
    module = _load_checker(project_root)
    (tmp_path / "tracked.py").write_text("VALUE = 1\n")
    stdout, stderr = io.StringIO(), io.StringIO()
    commands: list[tuple[str, ...]] = []

    def run_command(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="tracked.py\0", stderr="")

    status = module.main(
        [
            "--root",
            str(tmp_path),
            "--policy",
            str(project_root / ".license-policy.toml"),
            "--json",
        ],
        filesystem=module.LocalFilesystem(),
        output=module.Output(stdout, stderr),
        logger_factory=lambda _root: logging.getLogger("license-check.git"),
        run_command=run_command,
    )

    assert status == 1
    assert json.loads(stdout.getvalue())["violations"] == [
        {"path": "tracked.py", "message": "missing NVIDIA license header"}
    ]
    assert commands == [
        (
            "git",
            "-C",
            str(tmp_path.resolve()),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            ".",
        )
    ]


@pytest.mark.parametrize(
    "git_error",
    [
        "fatal: not a git repository",
        "fatal: detected dubious ownership in repository at '/builds/project'",
    ],
)
def test_repository_unavailable_git_errors_fall_back_and_honor_nested_gitignore_negations(
    project_root: Path, tmp_path: Path, git_error: str
) -> None:
    """Repository-unavailable discovery errors use the fallback traverser."""
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + "nested/*.py\n")
    (nested / ".gitignore").write_text(_commented_header("# ") + "!visible.py\n")
    (nested / "visible.py").write_text("VALUE = 1\n")

    status, payload = _run_fallback_checker(
        project_root,
        tmp_path,
        git_error=git_error,
    )

    assert status == 1
    assert payload == {
        "schema_version": 1,
        "outcome": "violations",
        "violations": [
            {"path": "nested/visible.py", "message": "missing NVIDIA license header"}
        ],
    }


def test_git_discovery_errors_remain_structured_json_errors(
    project_root: Path, tmp_path: Path
) -> None:
    """Unexpected tracked-file enumeration failures remain exit-two errors."""
    module = _load_checker(project_root)
    stdout, stderr = io.StringIO(), io.StringIO()

    def failed_enumeration(
        command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 128, stdout="", stderr="fatal: git ls-files failed"
        )

    status = module.main(
        [
            "--root",
            str(tmp_path),
            "--policy",
            str(project_root / ".license-policy.toml"),
            "--json",
        ],
        filesystem=module.LocalFilesystem(),
        output=module.Output(stdout, stderr),
        logger_factory=lambda _root: logging.getLogger("license-check.git-error"),
        run_command=failed_enumeration,
    )

    assert status == 2
    assert stderr.getvalue() == ""
    assert json.loads(stdout.getvalue()) == {
        "schema_version": 1,
        "outcome": "error",
        "error": "fatal: git ls-files failed",
    }


@pytest.mark.parametrize(
    ("ignore_rule", "path"),
    [
        ("generated/**/artifact.py", "generated/one/two/artifact.py"),
        (r"\#artifact.py", "#artifact.py"),
        (r"\!artifact.py", "!artifact.py"),
    ],
)
def test_non_git_fallback_uses_git_wildmatch_rules(
    project_root: Path, tmp_path: Path, ignore_rule: str, path: str
) -> None:
    """Fallback ignore matching preserves Git escapes and recursive globs."""
    ignored = tmp_path / path
    ignored.parent.mkdir(parents=True, exist_ok=True)
    ignored.write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + f"{ignore_rule}\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 0
    assert payload == {
        "schema_version": 1,
        "outcome": "compliant",
        "violations": [],
    }


def test_non_git_fallback_preserves_significant_leading_whitespace(
    project_root: Path, tmp_path: Path
) -> None:
    """A leading space in an ignore pattern cannot hide another maintained file."""
    (tmp_path / "visible.py").write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + " visible.py\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {"path": "visible.py", "message": "missing NVIDIA license header"}
    ]


def test_non_git_fallback_treats_a_lone_directory_separator_as_a_no_op(
    project_root: Path, tmp_path: Path
) -> None:
    """A slash-only ignore rule cannot hide a maintained file."""
    (tmp_path / "visible.py").write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + "/\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {"path": "visible.py", "message": "missing NVIDIA license header"}
    ]


def test_non_git_fallback_honors_caret_negated_character_classes(
    project_root: Path, tmp_path: Path
) -> None:
    """A leading unescaped caret negates a Git bracket character class."""
    (tmp_path / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "b.py").write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + "[^a].py\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {"path": "a.py", "message": "missing NVIDIA license header"}
    ]


def test_non_git_fallback_never_reads_a_symlinked_gitignore(
    project_root: Path, tmp_path: Path
) -> None:
    """Fallback traversal treats a symlinked ignore file as a checked link."""
    outside = tmp_path.parent / "outside-ignore"
    outside.write_text("visible.py\n")
    (tmp_path / ".gitignore").symlink_to(outside)
    (tmp_path / "visible.py").write_text("VALUE = 1\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {
            "path": ".gitignore",
            "message": "external license annotation required for symlink",
        },
        {"path": "visible.py", "message": "missing NVIDIA license header"},
    ]


def test_non_git_fallback_keeps_escaped_glob_metacharacters_literal(
    project_root: Path, tmp_path: Path
) -> None:
    """An escaped wildcard ignores its literal name without hiding a glob match."""
    (tmp_path / ".gitignore").write_text(
        _commented_header("# ") + r"literal\*.py" + "\n"
    )
    (tmp_path / "literal*.py").write_text("VALUE = 1\n")
    (tmp_path / "literalX.py").write_text("VALUE = 1\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {"path": "literalX.py", "message": "missing NVIDIA license header"}
    ]


def test_non_git_fallback_never_hides_a_directory_symlink(
    project_root: Path, tmp_path: Path
) -> None:
    """A directory-only ignore pattern cannot hide or traverse a symlink."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside-directory"
    outside.mkdir()
    (outside / "unlicensed.py").write_text("VALUE = 1\n")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    (tmp_path / ".gitignore").write_text(_commented_header("# ") + "linked/\n")

    status, payload = _run_fallback_checker(project_root, tmp_path)

    assert status == 1
    assert payload["violations"] == [
        {
            "path": "linked",
            "message": "external license annotation required for symlink",
        }
    ]


def test_check_root_prunes_exclusions_and_uses_injected_logger(
    project_root: Path,
) -> None:
    """Excluded directories are not traversed and diagnostics use caller logger."""
    module = _load_checker(project_root)
    root = Path("/virtual-project")
    policy = module.LicensePolicy.model_validate(
        {
            "copyright": _COPYRIGHT,
            "identifier": _LICENSE_ID,
            "notice": _NOTICE.splitlines(),
            "hash_suffixes": [".py"],
            "markdown_suffixes": [".md", ".mdc"],
            "hash_names": [],
            "annotation_suffixes": [],
            "annotations_file": ".license-annotations.toml",
            "exclude_paths": [".cache"],
            "exclude_globs": [],
        }
    )

    class Filesystem:
        def list_directory(self, path: Path) -> tuple[Path, ...]:
            if path == root:
                return (root / ".cache", root / "visible.py")
            raise AssertionError("excluded directory was traversed")

        def is_file(self, path: Path) -> bool:
            return path == root / "visible.py"

        def is_directory(self, path: Path) -> bool:
            return path == root / ".cache"

        def is_symlink(self, _path: Path) -> bool:
            return False

        def read_text(self, _path: Path) -> str:
            return _commented_header("# ") + "VALUE = 1\n"

    output = io.StringIO()
    logger = logging.getLoggerClass()("license-check.injected", level=logging.DEBUG)
    logger.addHandler(logging.StreamHandler(output))
    assert (
        module.check_root(
            root,
            policy,
            module.LicenseAnnotations(paths=()),
            filesystem=Filesystem(),
            logger=logger,
        )
        == ()
    )
    assert "classifying path=visible.py" in output.getvalue()


def test_checker_debug_and_ordinary_processes_never_write(
    project_root: Path, tmp_path: Path
) -> None:
    """Both modes preserve existing content and metadata and debug is useful."""
    (tmp_path / "visible.py").write_text(_commented_header("# ") + "VALUE = 1\n")
    before = tuple(
        (path.name, path.stat().st_mtime_ns, path.read_bytes())
        for path in tmp_path.iterdir()
    )
    ordinary = _run_checker(project_root, tmp_path)
    debug = _run_checker(project_root, tmp_path, "--debug")
    after = tuple(
        (path.name, path.stat().st_mtime_ns, path.read_bytes())
        for path in tmp_path.iterdir()
    )

    assert ordinary.returncode == debug.returncode == 0
    assert ordinary.stderr == ""
    assert "classifying path=visible.py" in debug.stderr
    assert after == before


def test_repository_assets_are_portable_and_checker_mirrors_are_identical(
    project_root: Path,
) -> None:
    """Both Copier source and rendered payload retain the exact checker assets."""
    template = project_root / "template"
    assert (project_root / "scripts" / "check_licenses.py").read_bytes() == (
        template / "scripts" / "check_licenses.py"
    ).read_bytes()
    for path in (".license-policy.toml", ".license-annotations.toml"):
        assert (project_root / path).read_bytes() == (template / path).read_bytes()


def test_license_enforcement_is_wired_into_precommit_and_ci(project_root: Path) -> None:
    """Root enforcement has one native CI job and a pre-commit entry point."""
    precommit = (project_root / ".pre-commit-config.yaml").read_text()
    ci = (project_root / ".gitlab-ci.yml").read_text()
    assert "scripts/check_licenses.py" in precommit
    assert "license-check:" in ci
    assert ci.count("license-check:") == 1


def test_repository_template_and_rendered_project_satisfy_license_policy(
    generated_project: Path, project_root: Path, template_dir: Path
) -> None:
    """Every maintained file in root, template, and Copier output is compliant."""
    for root in (project_root, template_dir, generated_project):
        result = _run_checker(project_root, root)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("license_status", (0, 7))
def test_generated_aggregate_dispatches_records_and_reports_license_once(
    template_dir: Path, tmp_path: Path, license_status: int
) -> None:
    """The generated aggregate executes the license gate exactly once."""
    project = tmp_path / "generated-project"
    helpers = project / "tests" / "helpers"
    helpers.mkdir(parents=True)
    for relative_path in (
        Path("tests/helpers/run-ci-checks.sh"),
        Path("tests/helpers/ci_gate_report.py"),
        Path("tests/helpers/yamllint_to_json.py"),
    ):
        shutil.copy2(template_dir / relative_path, project / relative_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invocations = tmp_path / "uv-invocations.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$UV_INVOCATIONS"\n'
        'case "$*" in\n'
        '  *ci_gate_report.py*) exec "$REAL_PYTHON" tests/helpers/ci_gate_report.py ;;\n'
        "  *yamllint_to_json.py*) printf '[]\\n' ;;\n"
        '  *check_licenses.py*) exit "$LICENSE_STATUS" ;;\n'
        "  *agent_work.readability*) exit 0 ;;\n"
        "esac\n"
    )
    fake_uv.chmod(0o755)
    for command, body in (("jq", "printf '[]\\n'\n"), ("shellcheck", "exit 0\n")):
        executable = fake_bin / command
        executable.write_text(f"#!/usr/bin/env bash\n{body}")
        executable.chmod(0o755)

    result = subprocess.run(
        ["bash", "tests/helpers/run-ci-checks.sh"],
        capture_output=True,
        check=False,
        cwd=project,
        env=os.environ
        | {
            "CI_PROJECT_DIR": str(project),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REAL_PYTHON": sys.executable,
            "UV_INVOCATIONS": str(invocations),
            "LICENSE_STATUS": str(license_status),
            "CI_PIPELINE_SOURCE": "merge_request_event",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "test-base",
        },
        text=True,
    )

    assert result.returncode == (0 if license_status == 0 else 1), result.stderr
    license_invocation = "run --no-sync python scripts/check_licenses.py --root ."
    assert invocations.read_text().splitlines().count(license_invocation) == 1
    manifest = (project / "ci-gates.tsv").read_text().splitlines()
    license_results = [line for line in manifest if line.startswith("license-check\t")]
    assert len(license_results) == 1
    assert license_results[0].split("\t")[1] == str(license_status)
    report = ElementTree.parse(project / "ci-gates.xml")
    license_cases = report.findall(".//testcase[@name='license-check']")
    assert len(license_cases) == 1
    assert (license_cases[0].find("failure") is not None) is (license_status != 0)
