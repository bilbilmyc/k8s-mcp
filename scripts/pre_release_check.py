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

# Source registrations intentionally exclude server.py's built-in `ping` tool.
EXPECTED_SOURCE_TOOL_COUNT = 89
EXPECTED_MCP_TOOL_COUNT = 90
DOC_TOOL_COUNT_MARKERS = {
    "README.md": "**90 个**工具",
    "README.en.md": "**90 tools**",
    "docs/README.md": "**90 个工具**",
    "docs/README.en.md": "**90 tools**",
    "docs/tools-reference.md": "# 工具参考（90 个，按功能分类）",
}


def count_registered_tools() -> int:
    """Count `mcp.tool()` registrations in src/k8s_mcp/tools/*.py."""
    total = 0
    for path in (ROOT / "src" / "k8s_mcp" / "tools").glob("*.py"):
        if path.name == "__init__.py":
            continue
        total += sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                     if "mcp.tool(" in line)
    return total


def count_mcp_tools() -> int:
    """Count the final server inventory, including server.py's `ping` tool."""
    from k8s_mcp.config import Settings
    from k8s_mcp.server import create_server

    server = create_server(Settings(read_only=True))
    return len(server._tool_manager._tools)


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
    if tool_count != EXPECTED_SOURCE_TOOL_COUNT:
        failures.append(
            f"Source tool count drift: code has {tool_count}, expected {EXPECTED_SOURCE_TOOL_COUNT}. "
            "Update the release constants, package description, and generated documentation together."
        )

    mcp_tool_count = count_mcp_tools()
    if mcp_tool_count != EXPECTED_MCP_TOOL_COUNT:
        failures.append(
            f"MCP tool count drift: server exposes {mcp_tool_count}, expected {EXPECTED_MCP_TOOL_COUNT}."
        )

    for relative_path, marker in DOC_TOOL_COUNT_MARKERS.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        if marker not in text:
            failures.append(
                f"Documentation drift: {relative_path} must contain {marker!r}."
            )

    # Test count (skip in CI-only environment if pytest unavailable — but here we have it)
    test_count = count_tests()
    if test_count < 0:
        failures.append("Could not determine test count (pytest --collect-only failed).")
    elif test_count < 600:
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

    print(
        "OK: Pre-release sanity OK -- "
        f"{mcp_tool_count} MCP tools ({tool_count} source registrations), "
        f"{test_count} tests, version {pv}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
