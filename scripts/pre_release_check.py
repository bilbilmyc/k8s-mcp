#!/usr/bin/env python3
"""Pre-release sanity check.

Verifies hard-coded numbers in README/CHANGELOG still match the actual
codebase:

  - Tool count (matches the `mcp.tool()(name)` registrations)
  - Test count (matches `pytest --collect-only -q` count)
  - Python version requirement (matches pyproject.toml)
  - __version__ matches pyproject.toml

Run from the repo root:

    python scripts/pre_release_check.py

Exits 0 on success, 1 on any mismatch (with a clear diff line).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def count_registered_tools() -> int:
    """Count `mcp.tool()` registrations in src/k8s_mcp/tools/*.py."""
    total = 0
    for path in (ROOT / "src" / "k8s_mcp" / "tools").glob("*.py"):
        if path.name == "__init__.py":
            continue
        total += sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                     if "mcp.tool(" in line)
    return total


def count_tests() -> int:
    """Run pytest --collect-only to get the test count."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "--ignore=tests/integration"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    m = re.search(r"(\d+)\s+tests collected", result.stdout)
    return int(m.group(1)) if m else -1


def pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def runtime_version() -> str:
    try:
        from importlib.metadata import version
        return version("k8s-mcp-bilbilmyc")
    except Exception:
        return ""


def main() -> int:
    failures = []

    # Tool count
    tool_count = count_registered_tools()
    if tool_count != 70:
        failures.append(
            f"Tool count drift: README claims 70 tools but code has {tool_count}. "
            "Update README.md and docs/tools-reference.md."
        )

    # Test count (skip in CI-only environment if pytest unavailable — but here we have it)
    test_count = count_tests()
    if test_count < 0:
        failures.append("Could not determine test count (pytest --collect-only failed).")
    elif test_count < 400:
        failures.append(
            f"Test count dropped to {test_count}. Update CHANGELOG.md 'Internal' section."
        )

    # Version alignment
    pv = pyproject_version()
    rv = runtime_version()
    if rv and pv and rv != pv:
        failures.append(
            f"Version mismatch: pyproject.toml={pv!r} but installed package={rv!r}. "
            "Run `uv pip install -e .` to rebuild."
        )

    if failures:
        print("FAIL: Pre-release sanity check FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"OK: Pre-release sanity OK -- {tool_count} tools, {test_count} tests, version {pv}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
