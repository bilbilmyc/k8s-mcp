"""k8s-mcp: a Kubernetes MCP server for LLM agents.

The package version is derived from pyproject.toml at build time. We keep
it as a module attribute so `create_server` and `notify` (which surfaces
the version to operators) can log it without re-parsing pyproject.
"""

from __future__ import annotations

try:
    # Python 3.8+ stdlib; the metadata for the installed package reflects
    # whatever wheel/sdist we're running from.
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("k8s-mcp-bilbilmyc")
    except PackageNotFoundError:
        # Source checkout without `uv pip install -e .` — fall back to
        # pyproject.toml so dev runs still report something sensible.
        __version__ = "0.0.0+local"
except Exception:  # noqa: BLE001 — never let version lookup crash startup
    __version__ = "0.0.0+unknown"
