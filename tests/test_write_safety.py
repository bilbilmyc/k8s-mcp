"""Tests for the write-safety guard that refuses the literal 'change-me'
HMAC default when writes are enabled.

The guard is enforced both at server startup AND at every write tool's
entry point — see config.enforce_write_safety and its callers in
delete_tool / bulk / storage.
"""
from __future__ import annotations

import pytest

from k8s_mcp.config import (
    Settings,
    enforce_write_safety,
    reset_settings_cache,
)


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_enforce_passes_in_read_only_mode_with_default_secret():
    """read_only=True bypasses the check — the secret isn't load-bearing."""
    s = Settings(read_only=True)  # delete_token_secret stays "change-me"
    enforce_write_safety(s)  # must not raise


def test_enforce_passes_with_real_secret():
    s = Settings(delete_token_secret="a-real-random-hex-string")
    enforce_write_safety(s)


def test_enforce_raises_with_literal_default_and_writes_enabled(monkeypatch):
    """Pin the guard's behavior against the source-tree default.

    The conftest autouse fixture injects a real-looking secret into the
    env to keep unrelated tests green — strip it for this one so we can
    observe the literal-default code path."""
    monkeypatch.delenv("K8S_MCP_DELETE_TOKEN_SECRET", raising=False)
    s = Settings()  # reads field default → 'change-me'
    assert s.delete_token_secret == "change-me"
    assert s.read_only is False
    with pytest.raises(RuntimeError, match="literal source-tree default 'change-me'"):
        enforce_write_safety(s)


def test_enforce_raises_with_empty_secret_and_writes_enabled():
    s = Settings(delete_token_secret="")
    with pytest.raises(RuntimeError, match="is empty while writes are ENABLED"):
        enforce_write_safety(s)


def test_enforce_raises_via_live_settings(monkeypatch):
    """Function reads get_settings() when called with no arg — verify
    the live env-var-driven path."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "change-me")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="literal source-tree default"):
        enforce_write_safety()


def test_enforce_raises_at_server_startup(monkeypatch):
    """create_server must propagate the RuntimeError rather than swallow it.
    A misconfigured deployment must NOT be able to come up."""
    from k8s_mcp.server import create_server

    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "change-me")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="literal source-tree default"):
        create_server()


def test_enforce_passes_at_server_startup_with_real_secret(monkeypatch):
    from k8s_mcp.server import create_server

    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "a-real-secret-32bytes")
    reset_settings_cache()
    # Should not raise; we don't actually run the server, just confirm
    # construction succeeds.
    mcp = create_server()
    assert mcp is not None
    reset_settings_cache()


def test_delete_resource_refuses_default_secret(monkeypatch):
    """Per-tool belt-and-suspenders: even if startup was bypassed (e.g.
    a deploy started in read_only and flipped to writes), the first
    delete call refuses with a clear error."""
    from k8s_mcp.tools import delete_tool

    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "change-me")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="literal source-tree default"):
        delete_tool.delete_resource("Pod", "x", namespace="default", confirm=False)
    reset_settings_cache()


def test_bulk_refuses_default_secret_at_issue(monkeypatch):
    """bulk's _issue_bulk_token is the per-call entrypoint for token
    issuance; it must refuse the default before issuing a forge-able
    token."""
    from k8s_mcp.tools import bulk

    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "change-me")
    reset_settings_cache()
    with pytest.raises(RuntimeError, match="literal source-tree default"):
        bulk._issue_bulk_token({"op": "bulk_restart"})
    reset_settings_cache()