"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest

from k8s_mcp.config import Settings, reset_settings_cache

# env vars that need a clean slate between tests
_K8S_MCP_ENV_PREFIX = "K8S_MCP_"
_K8S_MCP_ENV_KEYS = [
    "K8S_MCP_LOG_LEVEL",
    "K8S_MCP_DEFAULT_TAIL_LINES",
    "K8S_MCP_API_SERVER",
    "K8S_MCP_API_TOKEN",
    "K8S_MCP_API_CA_CERT",
    "K8S_MCP_API_INSECURE",
    "K8S_MCP_KUBECONFIG",
    "K8S_MCP_KUBE_CONTEXT",
    "K8S_MCP_READ_ONLY",
    "K8S_MCP_NAMESPACE_ALLOWLIST",
    "K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST",
    "K8S_MCP_METRICS_SERVER_MANIFEST_URL",
    "K8S_MCP_MAX_CONCURRENT_TOOLS",
    "K8S_MCP_NOTIFIER_ALLOW_PRIVATE_HOSTS",
    "K8S_MCP_NOTIFIER_URL_ALLOW_HTTP",
    "K8S_MCP_NOTIFIER_URL_ALLOWLIST",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe K8S_MCP_* env vars and reset settings cache between tests."""
    for k in _K8S_MCP_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    reset_settings_cache()

    yield
    reset_settings_cache()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def tmp_kubeconfig(tmp_path: Path) -> Path:
    """Write a minimal kubeconfig file and return its path."""
    kc = tmp_path / "kubeconfig"
    kc.write_text(
        """apiVersion: v1
kind: Config
current-context: test
clusters:
- name: test
  cluster:
    server: https://test.example.com:6443
contexts:
- name: test
  context:
    cluster: test
    user: test
users:
- name: test
  user:
    token: fake-token
"""
    )
    return kc
