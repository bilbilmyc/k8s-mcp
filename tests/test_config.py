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


def test_prometheus_namespace_allowlist_default_none():
    """Default = None = scan every namespace. Setting it to a list caps the
    wide-scan surface in `find_prometheus_service` /
    `_resolve_prometheus_url`."""
    s = Settings()
    assert s.prometheus_namespace_allowlist is None


def test_prometheus_namespace_allowlist_from_comma_string(monkeypatch):
    """Comma-separated env var parses the same way as namespace_allowlist."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "default,monitoring,prod")
    s = Settings()
    assert s.prometheus_namespace_allowlist == ["default", "monitoring", "prod"]


def test_prometheus_namespace_allowlist_empty_string_means_none(monkeypatch):
    """Empty string = 'unset', not 'allowlist of nothing'. The latter
    would mean scan no namespaces and break everything."""
    monkeypatch.setenv("K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST", "")
    s = Settings()
    assert s.prometheus_namespace_allowlist is None


def test_prometheus_namespace_allowlist_direct_list():
    s = Settings(prometheus_namespace_allowlist=["default"])
    assert s.prometheus_namespace_allowlist == ["default"]


def test_prometheus_namespace_allowlist_independent_of_namespace_allowlist():
    """The two allowlists are independent fields. Setting
    K8S_MCP_NAMESPACE_ALLOWLIST does NOT change prometheus discovery
    scope, and vice versa. (Critical to keep tests from accidentally
    coupling them.)"""
    s = Settings(
        namespace_allowlist=["app"],
        prometheus_namespace_allowlist=["monitoring"],
    )
    assert s.namespace_allowlist == ["app"]
    assert s.prometheus_namespace_allowlist == ["monitoring"]
    # ns_allowed still gated by namespace_allowlist, not prom one
    assert s.ns_allowed("monitoring") is False
    assert s.ns_allowed("app") is True
