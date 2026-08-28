"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from k8s_mcp.client import reset_client_cache
from k8s_mcp.config import Settings, reset_settings_cache

# env vars that need a clean slate between tests. Wipe by prefix so newly
# added K8S_MCP_* settings can never leak from the operator's shell into
# a test run (an explicit list inevitably drifts).
_K8S_MCP_ENV_PREFIX = "K8S_MCP_"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe K8S_MCP_* env vars and reset settings cache between tests."""
    for k in [k for k in os.environ if k.startswith(_K8S_MCP_ENV_PREFIX)]:
        monkeypatch.delenv(k, raising=False)
    reset_settings_cache()
    reset_client_cache()

    yield
    reset_settings_cache()
    reset_client_cache()


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
