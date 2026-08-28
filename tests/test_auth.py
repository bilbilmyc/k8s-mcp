"""Tests for auth mode selection (A / B / C)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from kubernetes import client

from k8s_mcp.auth import (
    AuthError,
    _load_kube_config,
    _load_token_config,
    inspect_auth,
    is_in_cluster,
    load_configuration,
)
from k8s_mcp.config import Settings

# ---- mode A: token config -----------------------------------------------------


def test_token_config_basic():
    s = Settings(api_server="https://api.example.com:6443", api_token="tok-abc")
    cfg = _load_token_config(s)
    assert cfg.host == "https://api.example.com:6443"
    assert cfg.api_key == {"authorization": "tok-abc"}
    assert cfg.api_key_prefix == {"authorization": "Bearer"}


def test_token_config_trailing_slash_stripped():
    s = Settings(api_server="https://api.example.com:6443/", api_token="tok")
    assert _load_token_config(s).host == "https://api.example.com:6443"


def test_token_config_insecure():
    s = Settings(
        api_server="https://x", api_token="t", api_insecure=True
    )
    assert _load_token_config(s).verify_ssl is False


def test_token_config_ca_cert(tmp_path: Path):
    ca = tmp_path / "ca.crt"
    ca.write_text("-----BEGIN CERTIFICATE-----\nxxx\n-----END CERTIFICATE-----\n")
    s = Settings(
        api_server="https://x", api_token="t", api_ca_cert=str(ca)
    )
    cfg = _load_token_config(s)
    assert cfg.ssl_ca_cert == str(ca)
    assert cfg.verify_ssl is True


def test_token_config_missing_ca_cert(tmp_path: Path):
    s = Settings(
        api_server="https://x",
        api_token="t",
        api_ca_cert=str(tmp_path / "missing.crt"),
    )
    with pytest.raises(AuthError, match="CA cert not found"):
        _load_token_config(s)


# ---- is_in_cluster detection -------------------------------------------------


def test_is_in_cluster_false_when_no_token_file():
    with patch("k8s_mcp.auth.os.path.exists", return_value=False):
        assert is_in_cluster() is False


def test_is_in_cluster_true_when_token_file_exists():
    with patch("k8s_mcp.auth.os.path.exists", return_value=True):
        assert is_in_cluster() is True


# ---- load_configuration priority ----------------------------------------------


def test_mode_a_wins_over_kubeconfig(tmp_kubeconfig: Path):
    """Explicit mode A env vars take precedence over kubeconfig."""
    s = Settings(
        api_server="https://api.example.com:6443",
        api_token="tok",
        kubeconfig=str(tmp_kubeconfig),
    )
    cfg = load_configuration(s)
    assert cfg.host == "https://api.example.com:6443"
    assert cfg.api_key == {"authorization": "tok"}


def test_mode_b_explicit_kubeconfig(tmp_kubeconfig: Path):
    s = Settings(kubeconfig=str(tmp_kubeconfig), kube_context="test")
    cfg = load_configuration(s)
    assert cfg.host == "https://test.example.com:6443"
    # Kubernetes 29 uses `authorization`; newer clients use `BearerToken`.
    token_value = cfg.api_key.get("BearerToken") or cfg.api_key.get("authorization", "")
    assert "fake-token" in token_value


def test_mode_c_when_in_cluster_no_kubeconfig():
    s = Settings()  # no api_server, no kubeconfig
    with patch("k8s_mcp.auth.is_in_cluster", return_value=True), \
         patch("k8s_mcp.auth._load_incluster") as mock_inc:
        mock_inc.return_value = client.Configuration(host="https://kubernetes.default.svc")
        cfg = load_configuration(s)
        assert cfg.host == "https://kubernetes.default.svc"
        mock_inc.assert_called_once()


def test_no_auth_mode_raises(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    # Pretend no default kubeconfig
    monkeypatch.setattr(
        "k8s_mcp.auth.Path.home", lambda: tmp_path
    )
    s = Settings()
    with patch("k8s_mcp.auth.is_in_cluster", return_value=False):
        with pytest.raises(AuthError, match="No auth mode available"):
            load_configuration(s)


def test_mode_b_default_kubeconfig_when_env_set(tmp_path: Path, monkeypatch):
    kc = tmp_path / "kubeconfig"
    kc.write_text(
        "apiVersion: v1\n"
        "kind: Config\n"
        "current-context: c\n"
        "clusters:\n"
        "- name: c\n"
        "  cluster:\n"
        "    server: https://default\n"
        "contexts:\n"
        "- name: c\n"
        "  context:\n"
        "    cluster: c\n"
        "    user: c\n"
        "users:\n"
        "- name: c\n"
        "  user:\n"
        "    token: t\n"
    )
    monkeypatch.setenv("KUBECONFIG", str(kc))
    s = Settings()
    cfg = load_configuration(s)
    assert cfg.host == "https://default"


def test_kubeconfig_env_preserves_multiple_paths(tmp_path: Path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_text("apiVersion: v1\nkind: Config\n")
    second.write_text("apiVersion: v1\nkind: Config\n")
    combined = f"{first}{os.pathsep}{second}"
    monkeypatch.setenv("KUBECONFIG", combined)

    with patch("k8s_mcp.auth.load_kube_config") as load:
        _load_kube_config(Settings())

    assert load.call_args.kwargs["config_file"] == combined


def test_inspect_auth_reports_incomplete_token_pair_and_fallback(tmp_path: Path):
    settings = Settings(api_server="https://api.example")
    with patch("k8s_mcp.auth.is_in_cluster", return_value=False), \
         patch("k8s_mcp.auth.Path.home", return_value=tmp_path):
        mode, source, warnings = inspect_auth(settings)
    assert (mode, source) == ("unavailable", "none")
    assert any("K8S_MCP_API_TOKEN" in warning for warning in warnings)


def test_inspect_auth_reports_incluster_before_standard_kubeconfig(monkeypatch):
    monkeypatch.setenv("KUBECONFIG", "C:/does-not-need-to-exist")
    with patch("k8s_mcp.auth.is_in_cluster", return_value=True):
        mode, source, warnings = inspect_auth(Settings())
    assert (mode, source) == ("in_cluster", "service_account")
    assert warnings == []


def test_mode_a_requires_both_api_server_and_token():
    s = Settings(api_server="https://x")  # no token
    with patch("k8s_mcp.auth.is_in_cluster", return_value=False), \
         patch("k8s_mcp.auth.Path.home") as mock_home:
        mock_home.return_value = Path("/nonexistent")
        with pytest.raises(AuthError, match="No auth mode available"):
            load_configuration(s)


def test_load_incluster_propagates_failure(monkeypatch):
    from kubernetes.config import ConfigException

    monkeypatch.setattr(
        "k8s_mcp.auth.load_incluster_config",
        lambda: (_ for _ in ()).throw(ConfigException("boom")),
    )
    s = Settings()
    with patch("k8s_mcp.auth.is_in_cluster", return_value=True):
        with pytest.raises(AuthError, match="Failed to load in-cluster"):
            load_configuration(s)
