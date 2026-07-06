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
    "K8S_MCP_DELETE_TOKEN_SECRET",
    "K8S_MCP_PROMETHEUS_NAMESPACE_ALLOWLIST",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe K8S_MCP_* env vars and reset settings cache between tests.

    Default-inject a real-looking HMAC secret so the `enforce_write_safety`
    guard (refuses the source-tree literal 'change-me') does not fire
    spuriously on tests that exercise delete/bulk flows without explicitly
    setting K8S_MCP_DELETE_TOKEN_SECRET. Also mock the caller-identity
    helper so destructive-op tokens get a stable, predictable identity
    bound to them.
    """
    for k in _K8S_MCP_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("K8S_MCP_DELETE_TOKEN_SECRET", "test-secret-not-change-me")
    reset_settings_cache()

    # Stable caller identity for tests so token-issue + token-verify
    # stay in the same identity without standing up an apiserver.
    from k8s_mcp.client import reset_caller_identity_cache
    reset_caller_identity_cache()
    monkeypatch.setattr(
        "k8s_mcp.client.get_caller_identity",
        lambda: {"username": "test-user", "uid": "test-uid", "groups": ["system:mcp"]},
    )
    # The tools import the symbol directly — patch on the calling side too.
    # Iterate every tools module rather than naming them explicitly so a
    # new tool that imports get_caller_identity picks up the same mock.
    import importlib
    import pkgutil

    import k8s_mcp.tools as _tools_pkg

    for mod_info in pkgutil.iter_modules(_tools_pkg.__path__):
        mod_name = f"k8s_mcp.tools.{mod_info.name}"
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "get_caller_identity"):
            monkeypatch.setattr(
                mod,
                "get_caller_identity",
                lambda: {"username": "test-user", "uid": "test-uid", "groups": ["system:mcp"]},
            )

    yield
    reset_settings_cache()
    reset_caller_identity_cache()


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
