---
description: Run all tests and lint, then fix failures systematically
---

# Run All Tests and Fix Failures

## Steps

1. **Run test suite**:
   ```
   uv run tox
   ```
   Capture output and identify failures across all Python versions.

2. **Analyze failures** — categorize by type: flaky, broken, new. Prioritize by impact and check if related to recent changes.

3. **Fix issues systematically** — one at a time, re-running tests after each fix.

4. **Run lint, format, and type checking**:
   ```
   uv run ruff check -f
   uv run ruff format
   uv run ty check
   ```
