<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: LicenseRef-NvidiaProprietary -->
<!--  -->
<!-- NVIDIA CORPORATION, its affiliates and licensors retain all intellectual -->
<!-- property and proprietary rights in and to this material, related -->
<!-- documentation and any modifications thereto. Any use, reproduction, -->
<!-- disclosure or distribution of this material and related documentation -->
<!-- without an express license agreement from NVIDIA CORPORATION or -->
<!-- its affiliates is strictly prohibited. -->
# Python Testing Patterns

This is an on-demand guide to Python tests in this repo. The short rules are in
[`python.md`](python.md) → Testing. This file has worked examples. Read it for
non-trivial tests: subprocess, HTTP, imports, fixtures, or logging. It is
**not** auto-loaded. Read it when writing or reviewing non-trivial tests.

## Core Principles

- Run tests with `uv run pytest`. Never invoke `python`/`pytest` directly.
- Mock only when required. Mock true system boundaries: subprocess, HTTP,
  filesystem, and time. Never mock internal code.
- Prefer dependency injection. Pass a fake through a parameter. Do not patch a
  symbol in our own modules.
- Use one logical assertion per test. Related asserts may stay together.
- Use `pytest.raises` for exceptions, even in parameterized cases.
- Avoid unneeded assertion messages.
- Test behavior, not implementation.
- `pytest-randomly` is enabled. Tests must be order-independent. Fix shared
  state rather than pinning order.

## Fixtures and conftest.py

Define these once per project in `conftest.py` when they apply.

### Clean directory per test

```python
import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def cleandir() -> Generator[None, None, None]:
    """Run each test in a fresh temp cwd so file writes are isolated."""
    with tempfile.TemporaryDirectory() as tmpdir:
        old_cwd = Path.cwd()
        os.chdir(Path(tmpdir))
        yield
        os.chdir(old_cwd)
```

### Session-scoped shared directory

```python
@pytest.fixture(scope="session")
def shared_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped temp dir for read-only shared data."""
    return tmp_path_factory.mktemp("shared_data")
```

### Mock sleep

```python
@pytest.fixture(autouse=True)
def mock_sleep(mocker: pytest.MonkeyPatch) -> None:
    """Never actually sleep in tests."""
    mocker.patch("time.sleep")
```

### Command-line arguments

```python
import sys
from collections.abc import Callable

import pytest


@pytest.fixture
def set_argv(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], None]:
    """Set sys.argv for a test invoking a CLI entry point."""

    def _set_argv(args: list[str]) -> None:
        monkeypatch.setattr(sys, "argv", ["pytest", *args])

    return _set_argv
```

### Logging capture

```python
import logging

import pytest


@pytest.fixture
def caplog_fixture(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """caplog pre-configured to INFO for the module under test."""
    caplog.set_level(logging.INFO, logger="my_logger")
    return caplog
```

## Parameterization

Prefer `@pytest.mark.parametrize` to duplicate test functions.

```python
import pytest


@pytest.mark.parametrize(
    ("input_value", "expected"),
    [("hello", "HELLO"), ("world", "WORLD"), ("", "")],
)
def test_uppercase(input_value: str, expected: str) -> None:
    assert input_value.upper() == expected
```

Use this form for errors in a parameterized test:

```python
@pytest.mark.parametrize(
    ("divisor", "expect_error"),
    [(10, False), (0, True)],
)
def test_division(divisor: int, *, expect_error: bool) -> None:
    if expect_error:
        with pytest.raises(ZeroDivisionError):
            _ = 1 / divisor
    else:
        assert 1 / divisor == pytest.approx(0.1)
```

Use the `set_argv` fixture for CLI argument variations:

```python
@pytest.mark.parametrize(
    ("args", "expected"),
    [(["--verbose"], "verbose"), (["--output", "out.log"], "out.log")],
)
def test_multiple_args(
    set_argv: Callable[[list[str]], None], args: list[str], expected: str
) -> None:
    set_argv(args)
    assert parse_args() == expected
```

## Subprocess — `pytest-subprocess`

Use the `fp` fixture from `pytest-subprocess` to mock external commands. Never
run a real subprocess or shell out in a test.

```python
import subprocess


def get_git_branch() -> str:
    """Return the current git branch."""
    return subprocess.check_output(
        ["git", "branch", "--show-current"], text=True
    ).strip()


def test_get_git_branch(fp) -> None:
    fp.register(["git", "branch", "--show-current"], stdout="main\n", returncode=0)
    assert get_git_branch() == "main"
    assert fp.call_count() == 1
```

Test failure paths and exact arguments too:

```python
@pytest.mark.parametrize(
    ("returncode", "should_raise"),
    [(0, False), (1, True), (127, True)],
)
def test_return_code_handling(fp, returncode: int, *, should_raise: bool) -> None:
    fp.register(["some", "command"], returncode=returncode)
    if should_raise:
        with pytest.raises(subprocess.CalledProcessError):
            subprocess.check_call(["some", "command"])
    else:
        subprocess.check_call(["some", "command"])
```

Use `fp.calls[i].command` to assert exact command arguments.

## HTTP / API — `requests-mock`

```python
import requests
import requests_mock


def test_api_call() -> None:
    with requests_mock.Mocker() as m:
        m.get("http://test.com", text="resp")
        assert requests.get("http://test.com", timeout=5).text == "resp"
```

Prefer injecting the HTTP call as a dependency when production code allows it.
Otherwise, use `requests-mock` at the true boundary.

## Filesystem access

Inject the file-opening function. This keeps success and failure paths pure and
parameterizable. Do not patch.

```python
import pytest


def fake_open_success(_filename: str, _mode: str):
    class DummyFile:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> str:
            return "hello world"

    return DummyFile()


def fake_open_permission_error(_filename: str, _mode: str):
    raise PermissionError("Permission denied")


@pytest.mark.parametrize(
    ("open_func", "expected"),
    [
        (fake_open_success, "hello world"),
        (fake_open_permission_error, "Permission denied"),
    ],
)
def test_read_file_contents(open_func, expected: str) -> None:
    assert read_file_contents("dummy.txt", open_func=open_func) == expected
```

## Logging

Use `caplog`, or `caplog_fixture` above, to assert log output.

```python
import pytest


@pytest.mark.usefixtures("caplog_fixture")
def test_logging_with_caplog(caplog_fixture: pytest.LogCaptureFixture) -> None:
    my_function_that_logs()
    assert "Expected error message" in caplog_fixture.text
```

Two common pitfalls follow:

- **Empty `caplog.text`** — the logger name must match exactly. Configure
  `caplog.set_level(logging.INFO, logger="my_package.my_module")`, not the
  top-level package name.
- **A module's own `setup_logger` interfering** — inject or mock it to a no-op
  so caplog owns configuration. Do not reconfigure logging in the mock.

## Imports

For conditional/optional imports or retry-on-import logic, use `monkeypatch` to
drive failures. Clear the module cache. Test success and the missing-dependency
path:

```python
import builtins
import sys

import pytest


def test_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def failing_import(name: str, *args, **kwargs):
        if name == "some_module":
            raise ImportError("Module not available")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    monkeypatch.delitem(sys.modules, "my_module", raising=False)

    with pytest.raises(ImportError, match="Module not available"):
        import my_module  # noqa: F401
```

## Test organization

Use one test file per module/feature. Group related tests in subdirectories
that mirror the source tree:

```text
my_project/
├── my_project/
│   ├── module_a.py
│   └── module_b.py
└── tests/
    ├── test_module_a.py
    └── test_module_b.py
```
