"""Tests for Settings."""
from __future__ import annotations

from k8s_mcp.config import Settings


def test_defaults():
    s = Settings()
    assert s.log_level == "INFO"
    assert s.default_tail_lines == 100
    assert s.read_only is False
    assert s.namespace_allowlist is None
    assert s.api_server is None
    assert s.api_token is None


def test_env_prefix(monkeypatch):
    monkeypatch.setenv("K8S_MCP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    monkeypatch.setenv("K8S_MCP_DEFAULT_TAIL_LINES", "500")
    s = Settings()
    assert s.log_level == "DEBUG"
    assert s.read_only is True
    assert s.default_tail_lines == 500


def test_ns_allowed_when_no_allowlist():
    s = Settings()
    assert s.ns_allowed("default") is True
    assert s.ns_allowed(None) is True


def test_ns_allowed_with_allowlist():
    s = Settings(namespace_allowlist=["default", "app"])
    assert s.ns_allowed("default") is True
    assert s.ns_allowed("kube-system") is False


def test_ns_allowed_blocks_cluster_scoped_when_allowlist_set():
    s = Settings(namespace_allowlist=["default"])
    assert s.ns_allowed(None) is False


def test_ns_allowed_blocks_writes_in_read_only():
    s = Settings(read_only=True)
    assert s.ns_allowed("default") is False
    assert s.ns_allowed("anything") is False
