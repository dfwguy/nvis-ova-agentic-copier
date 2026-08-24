"""Behavioural red tests for the Phase 2 controlled filesystem handoff."""

import hashlib
import inspect
import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

import pytest

from agent_work import filesystem, models
from agent_work.adapters import AdapterFailure
from agent_work.cli import build_default_file_runner
from agent_work.filesystem import (
    FileIntake,
    FileIntakeFailure,
    FileIntakeReport,
    FilesystemDependencies,
    RepositoryTarget,
)
from agent_work.models import WorkItem
from agent_work.runner import CodexRequest, RunDependencies


class InboxOperations(Protocol):
    """The complete injected filesystem boundary for one inbox attempt."""

    def address(self, path: Path, inbox_root: Path) -> tuple[Path, Path]: ...

    def validate_layout(self, root: Path, item: Path, checkout: Path) -> None: ...

    def read_request(self, item: Path) -> bytes: ...

    def ensure_dedupe(self, root: Path) -> None: ...

    def reserve_deduplication(self, root: Path, digest: str) -> bool: ...

    def claim(self, item: Path, processing: Path) -> Path: ...

    def release_claim(self, claimed: Path) -> None: ...

    def destination(
        self, root: Path, directory: str, digest: str, run_id: str
    ) -> Path: ...


def _request(**overrides: object) -> bytes:
    """Create one valid raw producer request without normalizing its bytes."""
    document = {
        "schema_version": 1,
        "source_id": "nightly",
        "repository": "copier",
        "title": "Bounded file request",
        "body": "Use the shared workflow.",
        "acceptance_criteria": ["Open an MR for human review."],
    } | overrides
    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def test_filesystem_intake_requires_a_public_injected_inbox_operations_boundary() -> (
    None
):
    """Filesystem operations cannot fall back to hidden process-global defaults."""
    protocol_name = "InboxOperations"
    adapter_name = "LocalInboxOperations"

    assert getattr(filesystem, protocol_name) is not None
    assert callable(getattr(filesystem, adapter_name))
    assert (
        inspect.signature(FilesystemDependencies).parameters["inbox"].default
        is inspect.Parameter.empty
    )


class Recorder:
    """Public-boundary fake that makes unsafe effects observable."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.work_items: list[WorkItem] = []
        self.fail_finalization = False
        self.workflow_error: OSError | RuntimeError | None = None
        self.run_count = 0

    def new_run_id(self) -> str:
        """Return a unique deterministic run identity for every bounded attempt."""
        self.run_count += 1
        return f"run-file-{self.run_count}"

    @contextmanager
    def lock(self, _target: RepositoryTarget) -> Iterator[None]:
        self.events.append("lock")
        yield

    def run_normalized(
        self, work_item: WorkItem, _target: RepositoryTarget, _timeout: int
    ) -> dict[str, object]:
        self.events.append("workflow")
        self.work_items.append(work_item)
        if self.workflow_error is not None:
            raise self.workflow_error
        return {
            "schema_version": 1,
            "work_item_id": work_item.work_item_id,
            "run_id": work_item.run_id,
            "mode": "publish",
            "outcome": "awaiting_human",
            "summary": "Published.",
            "branch": work_item.branch,
            "branch_tip": "0123456789abcdef0123456789abcdef01234567",
            "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/1",
            "tests": [],
            "findings": [],
            "next_action": "Human review and merge",
            "error": None,
        }

    def finalize(
        self,
        *,
        destination: Path,
        request: bytes,
        intake_result: Mapping[str, object],
        agent_result: Mapping[str, object],
    ) -> None:
        """Act as the injected atomic bundle boundary using a real tmp layout."""
        self.events.append("finalize")
        if self.fail_finalization:
            raise OSError("disk full")
        staging = destination.with_name(f".{destination.name}.staging")
        staging.mkdir()
        (staging / "request.json").write_bytes(request)
        (staging / "intake-result.json").write_text(json.dumps(intake_result))
        (staging / "agent-result.json").write_text(json.dumps(agent_result))
        os.replace(staging, destination)


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "inbox-root"
    for name in (
        "inbox",
        "processing",
        "awaiting-human",
        "done",
        "failed",
        "quarantine",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "checkout"
    repository.mkdir()
    return root, repository


def _local_inbox(
    *,
    device_id: Callable[[Path], int] = lambda _path: 1,
    read_syscall: Callable[[int, int], bytes] = os.read,
    write_syscall: Callable[[int, bytes], int] = os.write,
    atomic_replace: Callable[[Path, Path], None] | None = None,
    fsync_directory: Callable[[Path], None] | None = None,
) -> InboxOperations:
    """Build the explicit local adapter with all OS seams visible to each test."""
    adapter_name = "LocalInboxOperations"
    adapter = cast(Callable[..., InboxOperations], getattr(filesystem, adapter_name))
    arguments: dict[str, object] = {
        "device_id": device_id,
        "read_syscall": read_syscall,
        "write_syscall": write_syscall,
    }
    if atomic_replace is not None:
        arguments["atomic_replace"] = atomic_replace
    if fsync_directory is not None:
        arguments["fsync_directory"] = fsync_directory
    return adapter(**arguments)


def _dependencies(
    *,
    recorder: Recorder,
    target: RepositoryTarget,
    inbox: InboxOperations,
    finalize_bundle: Callable[..., None] | None = None,
) -> FilesystemDependencies:
    """Construct the runner only with its explicit impure boundaries."""
    factory = cast(Callable[..., FilesystemDependencies], FilesystemDependencies)
    return factory(
        resolve_repository=lambda alias: target if alias == target.alias else None,
        repository_lock=recorder.lock,
        run_normalized=recorder.run_normalized,
        finalize_bundle=recorder.finalize
        if finalize_bundle is None
        else finalize_bundle,
        new_run_id=recorder.new_run_id,
        inbox=inbox,
    )


def _intake(
    recorder: Recorder,
    repository: Path,
    *,
    inbox: InboxOperations,
) -> FileIntake:
    target = RepositoryTarget(
        alias="copier",
        checkout=repository,
        host="gitlab.example.com",
        project_id=310967,
    )
    return FileIntake(
        dependencies=_dependencies(
            recorder=recorder,
            target=target,
            inbox=inbox,
        )
    )


class FakeInbox:
    """Hermetic inbox boundary that has no filesystem implementation behind it."""

    def __init__(self, raw: bytes) -> None:
        self.events: list[str] = []
        self.raw = raw

    def address(self, path: Path, inbox_root: Path) -> tuple[Path, Path]:
        self.events.append("address")
        return inbox_root, path

    def validate_layout(self, root: Path, item: Path, checkout: Path) -> None:
        self.events.append("layout")

    def read_request(self, item: Path) -> bytes:
        self.events.append(f"read:{item}")
        return self.raw

    def ensure_dedupe(self, root: Path) -> None:
        self.events.append("dedupe-setup")

    def reserve_deduplication(self, root: Path, digest: str) -> bool:
        self.events.append("dedupe-reserve")
        return True

    def claim(self, item: Path, processing: Path) -> Path:
        self.events.append(f"claim:{item}->{processing}")
        return processing

    def release_claim(self, claimed: Path) -> None:
        self.events.append("claim-release")

    def destination(self, root: Path, directory: str, digest: str, run_id: str) -> Path:
        self.events.append("destination")
        return root / directory / f"{digest}-{run_id}"


def test_file_intake_uses_only_the_injected_inbox_operations_in_safe_order() -> None:
    """Orchestration has no hidden ``Path``/``os`` behavior outside its boundaries."""
    fake_inbox = FakeInbox(_request())
    inbox: InboxOperations = fake_inbox
    recorder = Recorder()
    target = RepositoryTarget(
        alias="copier",
        checkout=Path("/trusted-checkout"),
        host="gitlab.example.com",
        project_id=310967,
    )
    intake = FileIntake(
        _dependencies(
            recorder=recorder,
            target=target,
            inbox=inbox,
            finalize_bundle=lambda **_kwargs: fake_inbox.events.append("finalize"),
        )
    )

    report = intake.run(
        path=Path("request.json"),
        inbox_root=Path("/controlled-inbox"),
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert report.outcome == "awaiting_human"
    assert fake_inbox.events == [
        "address",
        "layout",
        "layout",
        "claim:request.json->/controlled-inbox/processing/request.json",
        "read:/controlled-inbox/processing/request.json",
        "dedupe-setup",
        "dedupe-reserve",
        "destination",
        "finalize",
        "claim-release",
    ]
    assert recorder.events == ["lock", "workflow"]


def test_preview_reads_only_and_never_claims_locks_deduplicates_or_runs_workflow(
    tmp_path: Path,
) -> None:
    """The default command must be fully read-only even for a valid request."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=False,
        timeout_seconds=900,
    )

    assert report.preview is True
    assert recorder.events == []
    assert item.is_file()
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    } == before


def test_preview_rejects_an_envelope_repository_that_disagrees_with_the_alias(
    tmp_path: Path,
) -> None:
    """Preview exposes task-data alias mismatch without mutating its input."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request(repository="other-repository"))
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=False,
        timeout_seconds=90,
    )

    assert report.preview is True
    assert report.validation_error == "repository does not match selected repository"
    assert recorder.events == []
    assert item.is_file()


@pytest.mark.parametrize(
    "raw", (_request() + b" " * (262_144 - len(_request())), b"\xff{}")
)
def test_exact_raw_file_limit_and_utf8_validation_are_reported_in_preview(
    tmp_path: Path, raw: bytes
) -> None:
    """Exactly 256 KiB is accepted; invalid encoding is exposed without mutation."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(raw)
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=False,
        timeout_seconds=900,
    )

    if raw == _request() + b" " * (262_144 - len(_request())):
        assert report.preview is True
    else:
        assert report.preview is True
        assert report.validation_error is not None
    assert recorder.events == []
    assert item.exists()


def test_applied_oversize_valid_json_is_claimed_then_quarantined_without_dedupe(
    tmp_path: Path,
) -> None:
    """A bounded size error preserves producer evidence without starting a run."""
    root, repository = _layout(tmp_path)
    raw = _request() + b" " * (262_145 - len(_request()))
    item = root / "inbox" / "oversize.json"
    item.write_bytes(raw)
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("oversize.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert report.quarantined is True
    assert report.validation_error == "request exceeds 262144 bytes"
    assert recorder.events == ["lock", "finalize"]
    assert report.destination is not None
    assert (report.destination / "request.json").read_bytes() == raw
    digest = hashlib.sha256(raw).hexdigest()
    assert not (root / ".dedupe" / digest).exists()


def test_apply_reopens_under_repository_lock_and_atomically_claims_before_workflow(
    tmp_path: Path,
) -> None:
    """A valid request becomes visibly owned in processing before any Codex path."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert report.outcome == "awaiting_human"
    assert recorder.events == ["lock", "workflow", "finalize"]
    assert len(recorder.work_items) == 1
    assert recorder.work_items[0].source_type == "filesystem"
    assert not item.exists()
    assert not (root / "processing" / "request.json").exists()
    digest = hashlib.sha256(_request()).hexdigest()
    assert isinstance(report.destination, Path)
    bundle = report.destination
    assert bundle == root / "awaiting-human" / f"{digest}-run-file-1"
    assert (bundle / "request.json").is_file()
    assert (bundle / "intake-result.json").is_file()
    assert (bundle / "agent-result.json").is_file()
    serialized = report.as_dict()
    assert serialized["source"] == "nightly"
    assert serialized["repository"] == "copier"
    assert serialized["sha256"] == digest
    assert serialized["branch"] == f"agent/file-{digest[:16]}"
    assert serialized["outcome"] == "awaiting_human"
    assert serialized["destination"] == str(bundle)
    raw_result = serialized["result"]
    assert isinstance(raw_result, Mapping)
    result = cast(Mapping[str, object], raw_result)
    assert result["merge_request_url"] == (
        "https://gitlab.example.com/group/project/-/merge_requests/1"
    )


def test_apply_duplicate_raw_sha_is_successful_noop_and_never_runs_codex(
    tmp_path: Path,
) -> None:
    """A finalized content hash is a create-only dedupe record, not a second run."""
    root, repository = _layout(tmp_path)
    original = root / "inbox" / "first.json"
    original.write_bytes(_request())
    recorder = Recorder()
    intake = _intake(recorder, repository, inbox=_local_inbox())
    intake.run(
        path=Path("first.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )
    duplicate = root / "inbox" / "duplicate.json"
    duplicate.write_bytes(_request())

    report = intake.run(
        path=Path("duplicate.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert report.duplicate is True
    assert recorder.events.count("workflow") == 1
    digest = hashlib.sha256(_request()).hexdigest()
    assert report.destination == root / "done" / f"{digest}-run-file-2"
    assert not duplicate.exists()
    assert isinstance(report.destination, Path)
    assert (report.destination / "request.json").read_bytes() == _request()
    assert (report.destination / "intake-result.json").is_file()


def test_apply_invalid_payload_quarantines_with_an_intake_result(
    tmp_path: Path,
) -> None:
    """Invalid validly-addressed input is retained for diagnosis without Codex."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "invalid.json"
    item.write_bytes(b"{not json")
    recorder = Recorder()

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("invalid.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert isinstance(report.destination, Path)
    bundle = report.destination
    assert report.quarantined is True
    assert recorder.events == ["lock", "finalize"]
    assert bundle.parent == root / "quarantine"
    assert bundle.name.endswith("-run-file-1")
    assert (bundle / "request.json").read_bytes() == b"{not json"
    assert (
        json.loads((bundle / "intake-result.json").read_text())["outcome"]
        == "quarantined"
    )
    invalid_digest = hashlib.sha256(b"{not json").hexdigest()
    assert not (root / ".dedupe" / invalid_digest).exists()


@pytest.mark.parametrize(
    "workflow_error", (RuntimeError("Codex blocked"), OSError("Codex unavailable"))
)
def test_apply_workflow_failures_finalize_a_failed_bundle_before_reraising(
    tmp_path: Path, workflow_error: OSError | RuntimeError
) -> None:
    """Every operational workflow failure retains durable terminal evidence."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    recorder.workflow_error = workflow_error

    with pytest.raises(type(workflow_error)):
        _intake(recorder, repository, inbox=_local_inbox()).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    bundle = next((root / "failed").iterdir())
    assert bundle.name.endswith("-run-file-1")
    assert (bundle / "request.json").is_file()
    assert (bundle / "intake-result.json").is_file()
    assert (bundle / "agent-result.json").is_file()


def test_finalization_failure_retains_processing_evidence(tmp_path: Path) -> None:
    """A bundle-write failure cannot discard the only claimed request evidence."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    recorder.fail_finalization = True

    with pytest.raises(OSError):
        _intake(recorder, repository, inbox=_local_inbox()).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    assert (root / "processing" / "request.json").is_file()


@pytest.mark.parametrize(
    "unsafe",
    ("root_symlink", "item_symlink", "hardlink", "fifo", "overlap", "done_symlink"),
)
def test_unsafe_layouts_refuse_without_touching_the_addressed_input(
    tmp_path: Path, unsafe: str
) -> None:
    """Path tricks and non-regular files never produce claims or quarantine moves."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    if unsafe == "root_symlink":
        linked = tmp_path / "linked-root"
        root.rename(linked)
        root.symlink_to(linked, target_is_directory=True)
    elif unsafe == "item_symlink":
        target = tmp_path / "elsewhere.json"
        target.write_bytes(_request())
        item.unlink()
        item.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(item, root / "inbox" / "second-name.json")
    elif unsafe == "fifo":
        item.unlink()
        os.mkfifo(item)
    elif unsafe == "overlap":
        (root / "processing").rmdir()
        (root / "processing").symlink_to(root / "inbox", target_is_directory=True)
    else:
        (root / "done").rmdir()
        (root / "done").symlink_to(root / "inbox", target_is_directory=True)
    recorder = Recorder()

    with pytest.raises(RuntimeError):
        _intake(recorder, repository, inbox=_local_inbox()).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    assert recorder.events == []
    if unsafe != "fifo":
        assert item.exists() or item.is_symlink()


def test_repository_overlap_refuses_without_touching_the_inbox_item(
    tmp_path: Path,
) -> None:
    """A checkout cannot be the inbox root or any directory it controls."""
    root, _repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()

    with pytest.raises(RuntimeError):
        _intake(recorder, root, inbox=_local_inbox()).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    assert recorder.events == []
    assert item.is_file()


@pytest.mark.parametrize(
    "target",
    (
        lambda root, repository: RepositoryTarget(
            alias="copier",
            checkout=repository,
            host="https://gitlab.example.com",
            project_id=310967,
        ),
        lambda root, repository: RepositoryTarget(
            alias="copier",
            checkout=root / "inbox" / "..",
            host="gitlab.example.com",
            project_id=310967,
        ),
    ),
)
def test_allowlist_rejects_unsafe_host_and_canonical_repository_overlap(
    tmp_path: Path,
    target: Callable[[Path, Path], RepositoryTarget],
) -> None:
    """Trusted bindings are validated before task data can reach a workflow."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    intake = FileIntake(
        _dependencies(
            recorder=recorder,
            target=target(root, repository),
            inbox=_local_inbox(),
        )
    )

    with pytest.raises(RuntimeError):
        intake.run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=False,
            timeout_seconds=90,
        )

    assert recorder.events == []
    assert item.is_file()


def test_allowlist_rejects_unsafe_alias_before_reading_task_data(
    tmp_path: Path,
) -> None:
    """Alias validation belongs to the trusted resolver, not the producer envelope."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    unsafe_alias = "copier/../other"
    intake = FileIntake(
        _dependencies(
            recorder=recorder,
            target=RepositoryTarget(
                alias=unsafe_alias,
                checkout=repository,
                host="gitlab.example.com",
                project_id=310967,
            ),
            inbox=_local_inbox(),
        )
    )

    with pytest.raises(RuntimeError):
        intake.run(
            path=Path("request.json"),
            inbox_root=root,
            repository=unsafe_alias,
            mode="publish",
            apply=False,
            timeout_seconds=90,
        )

    assert recorder.events == []
    assert item.is_file()


@pytest.mark.parametrize(
    "alias", ("", "copier/other", "copier other", "copier:other", "é", "x" * 129)
)
def test_one_models_alias_validator_bounds_envelopes_targets_and_allowlists(
    tmp_path: Path, alias: str
) -> None:
    """Every alias boundary shares one public portable-name validation contract."""
    alias_validator = "validate_alias"
    validator = cast(Callable[[str], str], getattr(models, alias_validator))
    with pytest.raises(ValueError):
        validator(alias)
    with pytest.raises(ValueError):
        models.FilesystemEnvelope.model_validate(
            json.loads(_request(source_id=alias, repository=alias))
        )

    root, repository = _layout(tmp_path)
    recorder = Recorder()
    fake_inbox = FakeInbox(_request())
    intake = FileIntake(
        _dependencies(
            recorder=recorder,
            target=RepositoryTarget(
                alias=alias,
                checkout=repository,
                host="gitlab.example.com",
                project_id=310967,
            ),
            inbox=fake_inbox,
        )
    )
    with pytest.raises(FileIntakeFailure):
        intake.run(
            path=Path("request.json"),
            inbox_root=root,
            repository=alias,
            mode="publish",
            apply=False,
            timeout_seconds=90,
        )
    assert fake_inbox.events == ["address"]

    allowlist = tmp_path / "repositories.json"
    allowlist.write_text(
        json.dumps(
            {
                alias: {
                    "checkout": str(repository),
                    "host": "gitlab.example.com",
                    "project_id": 310967,
                }
            }
        )
    )
    with pytest.raises(AdapterFailure):
        build_default_file_runner(
            environment={"AGENT_WORK_REPOSITORIES_FILE": str(allowlist)}
        )


@pytest.mark.parametrize(
    "kind",
    (
        "symlink_file",
        "unsafe_alias",
        "unsafe_host",
        "extra_field",
        "dotdot_checkout",
        "symlink_checkout",
    ),
)
def test_repository_allowlist_rejects_unsafe_files_aliases_hosts_and_paths(
    tmp_path: Path, kind: str
) -> None:
    """The external allowlist is trusted config only after strict path validation."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    alias = "copier"
    entry: dict[str, object] = {
        "checkout": str(checkout),
        "host": "gitlab.example.com",
        "project_id": 310967,
    }
    if kind == "unsafe_alias":
        alias = "copier/other"
    elif kind == "unsafe_host":
        entry["host"] = "https://gitlab.example.com"
    elif kind == "extra_field":
        entry["untrusted"] = True
    elif kind == "dotdot_checkout":
        entry["checkout"] = str(checkout / "..")
    elif kind == "symlink_checkout":
        checkout_link = tmp_path / "checkout-link"
        checkout_link.symlink_to(checkout, target_is_directory=True)
        entry["checkout"] = str(checkout_link)
    config = tmp_path / "repositories.json"
    config.write_text(json.dumps({alias: entry}))
    if kind == "symlink_file":
        target = config.with_name("target.json")
        config.rename(target)
        config.symlink_to(target)

    with pytest.raises(AdapterFailure):
        build_default_file_runner(
            environment={"AGENT_WORK_REPOSITORIES_FILE": str(config)}
        )


def test_default_file_runner_uses_the_same_injected_run_store_lock_as_run_issue(
    tmp_path: Path,
) -> None:
    """File intake must not create a parallel checkout-local lock namespace."""
    root, checkout = _layout(tmp_path)
    (root / "inbox" / "request.json").write_bytes(_request())
    allowlist = tmp_path / "repositories.json"
    allowlist.write_text(
        json.dumps(
            {
                "copier": {
                    "checkout": str(checkout),
                    "host": "gitlab.example.com",
                    "project_id": 310967,
                }
            }
        )
    )
    lock_events: list[str] = []
    factory_targets: list[RepositoryTarget] = []
    active_work_item: list[WorkItem] = []

    @contextmanager
    def shared_lock(work_item_id: str) -> Iterator[None]:
        lock_events.append(f"lock:{work_item_id}")
        yield
        lock_events.append("unlock")

    def preflight(_config: object, work_item: WorkItem) -> None:
        lock_events.append("preflight")
        active_work_item.append(work_item)

    def invoke_codex(_request: CodexRequest) -> object:
        lock_events.append("codex")
        item = active_work_item[-1]
        return {
            "work_item_id": item.work_item_id,
            "run_id": item.run_id,
            "mode": "publish",
            "outcome": "awaiting_human",
            "branch": item.branch,
            "branch_tip": "0123456789abcdef0123456789abcdef01234567",
            "merge_request_url": "https://gitlab.example.com/group/project/-/merge_requests/1",
        }

    def dependencies_factory(target: RepositoryTarget) -> RunDependencies:
        factory_targets.append(target)
        return RunDependencies(
            load_config=lambda: models.GitLabConfig(
                host=target.host, project_id=target.project_id
            ),
            fetch_issue=lambda _config, _iid: models.GitLabIssue(iid=1, title="unused"),
            repository_lock=shared_lock,
            preflight_repository=preflight,
            create_worktree=lambda _item: tmp_path / "worktree",
            invoke_codex=invoke_codex,
            verify_publication=lambda _result, _worktree: lock_events.append(
                "verify-publication"
            ),
            append_journal=lambda _transition: None,
            project_issue=lambda _result: None,
            new_run_id=lambda: "unused",
            load_result_schema=lambda: {"type": "object"},
            claim_issue=lambda _item: None,
            write_canonical_result=lambda _run_id, _document: lock_events.append(
                "write-canonical-result"
            ),
            write_recovery_record=lambda _run_id, _document: None,
            schema_path=lambda: tmp_path / "result.schema.json",
            now=lambda: "2026-08-24T00:00:00+00:00",
            result_digest=lambda _document: "digest",
            read_worktree_head=lambda _worktree: (
                "0123456789abcdef0123456789abcdef01234567"
            ),
        )

    factory = cast(Callable[..., Callable[..., object]], build_default_file_runner)
    runner = factory(
        environment={"AGENT_WORK_REPOSITORIES_FILE": str(allowlist)},
        run_dependencies_factory=dependencies_factory,
    )
    report = cast(
        FileIntakeReport,
        runner(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        ),
    )

    assert factory_targets == [
        RepositoryTarget(
            alias="copier",
            checkout=checkout,
            host="gitlab.example.com",
            project_id=310967,
        )
    ]
    assert lock_events == [
        f"lock:filesystem:{hashlib.sha256(_request()).hexdigest()}",
        "preflight",
        "codex",
        "verify-publication",
        "write-canonical-result",
        "unlock",
    ]
    assert not (checkout / ".git" / "agent-work-file.lock").exists()
    assert report.outcome == "awaiting_human"


def test_create_only_dedupe_reservation_blocks_a_competing_same_content_run(
    tmp_path: Path,
) -> None:
    """A reservation failure is a duplicate no-op, not a glob-race second publish."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    digest = hashlib.sha256(_request()).hexdigest()
    reservations = root / ".dedupe"
    reservations.mkdir()
    (reservations / digest).write_text(f"{digest}\n")

    report = _intake(recorder, repository, inbox=_local_inbox()).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=True,
        timeout_seconds=90,
    )

    assert report.duplicate is True
    assert recorder.events == ["lock", "finalize"]
    assert recorder.work_items == []


def test_adapter_reads_at_most_maximum_request_bytes_plus_one(tmp_path: Path) -> None:
    """The file boundary receives a limit, preventing `read_bytes()` on huge input."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()
    limits: list[int] = []

    def read_request(descriptor: int, limit: int) -> bytes:
        limits.append(limit)
        return os.read(descriptor, limit)

    target = RepositoryTarget(
        alias="copier",
        checkout=repository,
        host="gitlab.example.com",
        project_id=310967,
    )
    dependencies = _dependencies(
        recorder=recorder,
        target=target,
        inbox=_local_inbox(read_syscall=read_request),
    )

    FileIntake(dependencies).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=False,
        timeout_seconds=90,
    )

    assert limits == [262_145]


def test_durable_bundle_finalizer_fsyncs_files_and_both_directories_around_replace(
    tmp_path: Path,
) -> None:
    """A crash cannot acknowledge a bundle whose contents or rename are not durable."""
    events: list[tuple[str, Path]] = []
    destination = tmp_path / "awaiting-human" / "digest-run"
    destination.parent.mkdir()
    finalizer_name = "durable_finalize_bundle"
    finalize = cast(Callable[..., None], getattr(filesystem, finalizer_name))

    finalize(
        destination=destination,
        request=_request(),
        intake_result={"outcome": "awaiting_human"},
        agent_result={"outcome": "awaiting_human"},
        fsync_path=lambda path: events.append(("fsync", path)),
        atomic_replace=lambda source, target: (
            events.append(("replace", target)),
            os.replace(source, target),
        )[1],
    )

    staging = destination.with_name(f".{destination.name}.staging")
    assert ("fsync", staging / "request.json") in events
    assert ("fsync", staging / "intake-result.json") in events
    assert ("fsync", staging / "agent-result.json") in events
    assert ("fsync", staging) in events
    assert ("replace", destination) in events
    assert events[-1] == ("fsync", destination.parent)


def test_cross_device_terminal_directory_refuses_before_claim_or_quarantine(
    tmp_path: Path,
) -> None:
    """A cross-device move must never degrade silently into copy-and-delete."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    recorder = Recorder()

    with pytest.raises(RuntimeError):
        _intake(
            recorder,
            repository,
            inbox=_local_inbox(
                device_id=lambda path: 2 if path.name == "awaiting-human" else 1
            ),
        ).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    assert recorder.events == []
    assert item.is_file()


def test_claim_collision_rolls_back_its_unfinalized_dedupe_reservation(
    tmp_path: Path,
) -> None:
    """An unclaimed request cannot reserve content forever after a local collision."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    (root / "processing" / "request.json").write_bytes(b"another claimed request")
    recorder = Recorder()
    digest = hashlib.sha256(_request()).hexdigest()

    with pytest.raises(RuntimeError):
        _intake(recorder, repository, inbox=_local_inbox()).run(
            path=Path("request.json"),
            inbox_root=root,
            repository="copier",
            mode="publish",
            apply=True,
            timeout_seconds=90,
        )

    assert not (root / ".dedupe" / digest).exists()
    assert item.is_file()
    assert recorder.events == ["lock"]


def test_local_inbox_claim_fsyncs_source_and_destination_directories_after_replace(
    tmp_path: Path,
) -> None:
    """An atomic claim is durable only once both directory entries are flushed."""
    root, _repository = _layout(tmp_path)
    source = root / "inbox" / "request.json"
    destination = root / "processing" / "request.json"
    source.write_bytes(_request())
    events: list[tuple[str, Path]] = []

    def replace(item: Path, processing: Path) -> None:
        events.append(("replace", processing))
        os.replace(item, processing)

    inbox = _local_inbox(
        atomic_replace=replace,
        fsync_directory=lambda directory: events.append(("fsync", directory)),
    )

    claimed = inbox.claim(source, destination)

    assert claimed == destination
    assert destination.read_bytes() == _request()
    assert events == [
        ("replace", destination),
        ("fsync", source.parent),
        ("fsync", destination.parent),
    ]


def test_default_reader_reassembles_real_short_os_read_chunks_up_to_the_limit(
    tmp_path: Path,
) -> None:
    """POSIX permits short reads, so an exact-size cap must be reached by looping."""
    root, repository = _layout(tmp_path)
    item = root / "inbox" / "request.json"
    item.write_bytes(_request())
    real_read = os.read
    requested_sizes: list[int] = []

    def short_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, min(size, 7))

    report = _intake(
        Recorder(), repository, inbox=_local_inbox(read_syscall=short_read)
    ).run(
        path=Path("request.json"),
        inbox_root=root,
        repository="copier",
        mode="publish",
        apply=False,
        timeout_seconds=90,
    )

    assert report.validation_error is None
    assert requested_sizes[0] == 262_145
    assert len(requested_sizes) > 1
    assert all(size <= 262_145 for size in requested_sizes)


def test_durable_finalizer_retries_real_short_os_writes_without_truncating_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX permits short writes, so every request and result byte must be retried."""
    destination = tmp_path / "awaiting-human" / "digest-run"
    destination.parent.mkdir()
    request = _request(body="x" * 64)
    intake_result = {"outcome": "awaiting_human", "sha256": "a" * 64}
    agent_result = {"outcome": "awaiting_human", "summary": "published"}
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(descriptor: int, data: bytes) -> int:
        write_sizes.append(len(data))
        return real_write(descriptor, data[:5])

    monkeypatch.setattr(os, "write", short_write)

    filesystem.durable_finalize_bundle(
        destination=destination,
        request=request,
        intake_result=intake_result,
        agent_result=agent_result,
        fsync_path=lambda _path: None,
    )

    assert (destination / "request.json").read_bytes() == request
    assert json.loads((destination / "intake-result.json").read_text()) == intake_result
    assert json.loads((destination / "agent-result.json").read_text()) == agent_result
    assert len(write_sizes) > 3


def test_dedupe_reservation_retries_real_short_os_writes_without_truncating_digest(
    tmp_path: Path,
) -> None:
    """A true reservation must durably contain the complete content identity."""
    root = tmp_path / "inbox-root"
    (root / ".dedupe").mkdir(parents=True)
    digest = hashlib.sha256(b"exact producer bytes").hexdigest()
    real_write = os.write

    def short_write(descriptor: int, data: bytes) -> int:
        return real_write(descriptor, data[:3])

    inbox = _local_inbox(write_syscall=short_write)

    assert inbox.reserve_deduplication(root, digest) is True
    assert (root / ".dedupe" / digest).read_text() == f"{digest}\n"


def test_dedupe_reservation_rejects_zero_progress_and_removes_partial_marker(
    tmp_path: Path,
) -> None:
    """A zero-byte write cannot claim a digest or poison later deliveries."""
    root = tmp_path / "inbox-root"
    (root / ".dedupe").mkdir(parents=True)
    digest = hashlib.sha256(b"exact producer bytes").hexdigest()
    inbox = _local_inbox(write_syscall=lambda _descriptor, _data: 0)

    with pytest.raises(FileIntakeFailure):
        inbox.reserve_deduplication(root, digest)

    assert not (root / ".dedupe" / digest).exists()
