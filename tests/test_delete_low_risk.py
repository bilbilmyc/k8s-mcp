"""Tests for the one-step low-risk delete tools.

Strategy: monkeypatch the relevant CoreV1Api / NetworkingV1Api method
to a recording fake. Each tool has the same shape:
  1. read_only check (rejection with PermissionError)
  2. namespace allowlist check
  3. 404 -> LookupError
  4. success path
"""
from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import configmap, service, storage


@pytest.fixture(autouse=True)
def _clear():
    reset_settings_cache()
    yield
    reset_settings_cache()


# ---------- delete_pvc ------------------------------------------------------


def test_delete_pvc_calls_api(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    called: list[tuple[str, str]] = []

    class _FakeApi:
        def delete_namespaced_persistent_volume_claim(self, name, namespace):
            called.append((namespace, name))
    monkeypatch.setattr(storage, "_core_v1", lambda: _FakeApi())

    out = storage.delete_pvc(name="data", namespace="app")
    assert "PVC/app/data deleted" in out
    assert called == [("app", "data")]


def test_delete_pvc_rejects_in_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        storage.delete_pvc(name="x", namespace="app")


def test_delete_pvc_rejects_outside_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        storage.delete_pvc(name="x", namespace="other")


def test_delete_pvc_404_raises_lookup_error(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    class _FakeApi:
        def delete_namespaced_persistent_volume_claim(self, name, namespace):
            raise ApiException(status=404, reason="Not Found")
    monkeypatch.setattr(storage, "_core_v1", lambda: _FakeApi())

    with pytest.raises(LookupError, match="not found"):
        storage.delete_pvc(name="ghost", namespace="app")


# ---------- delete_configmap ------------------------------------------------


def test_delete_configmap_calls_api(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    called: list[tuple[str, str]] = []

    class _FakeApi:
        def delete_namespaced_config_map(self, name, namespace):
            called.append((namespace, name))
    monkeypatch.setattr(configmap, "_core_v1", lambda: _FakeApi())

    out = configmap.delete_configmap(name="env", namespace="app")
    assert "ConfigMap/app/env deleted" in out
    assert called == [("app", "env")]


def test_delete_configmap_rejects_in_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        configmap.delete_configmap(name="x")


def test_delete_configmap_404_raises_lookup_error(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    class _FakeApi:
        def delete_namespaced_config_map(self, name, namespace):
            raise ApiException(status=404, reason="Not Found")
    monkeypatch.setattr(configmap, "_core_v1", lambda: _FakeApi())

    with pytest.raises(LookupError, match="not found"):
        configmap.delete_configmap(name="ghost", namespace="app")


# ---------- delete_service --------------------------------------------------


def test_delete_service_calls_api(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    called: list[tuple[str, str]] = []

    class _FakeApi:
        def delete_namespaced_service(self, name, namespace):
            called.append((namespace, name))
    monkeypatch.setattr(service, "_core_v1", lambda: _FakeApi())

    out = service.delete_service(name="web", namespace="app")
    assert "Service/app/web deleted" in out
    assert called == [("app", "web")]


def test_delete_service_rejects_in_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        service.delete_service(name="x")


def test_delete_service_404_raises_lookup_error(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    class _FakeApi:
        def delete_namespaced_service(self, name, namespace):
            raise ApiException(status=404, reason="Not Found")
    monkeypatch.setattr(service, "_core_v1", lambda: _FakeApi())

    with pytest.raises(LookupError, match="not found"):
        service.delete_service(name="ghost", namespace="app")


# ---------- delete_ingress --------------------------------------------------


def test_delete_ingress_calls_api(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    called: list[tuple[str, str]] = []

    class _FakeApi:
        def delete_namespaced_ingress(self, name, namespace):
            called.append((namespace, name))
    monkeypatch.setattr(service, "_networking_v1", lambda: _FakeApi())

    out = service.delete_ingress(name="main", namespace="app")
    assert "Ingress/app/main deleted" in out
    assert called == [("app", "main")]


def test_delete_ingress_rejects_in_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        service.delete_ingress(name="x")


def test_delete_ingress_404_raises_lookup_error(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()

    class _FakeApi:
        def delete_namespaced_ingress(self, name, namespace):
            raise ApiException(status=404, reason="Not Found")
    monkeypatch.setattr(service, "_networking_v1", lambda: _FakeApi())

    with pytest.raises(LookupError, match="not found"):
        service.delete_ingress(name="ghost", namespace="app")
