"""Verify the deprecated `delete_*` wrappers emit a visible marker.

Phase B1: delete_pod / delete_service / delete_ingress /
delete_configmap / delete_pvc are deprecated as of v0.4.0 and will be
removed in v0.5.0. The migration target is `delete_resource(kind=...)`
for the audited two-step flow.

Each wrapper prepends `⚠️ DEPRECATED: <tool> will be removed in v0.5.0
— use delete_resource(kind='<Kind>') for the audited two-step flow.` to
its return string so the agent sees the warning every time the tool is
called. This file pins that marker down so a refactor can't silently
drop the deprecation.
"""
from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import configmap, service, storage
from k8s_mcp.tools import pods as pods_mod


@pytest.fixture(autouse=True)
def _reset_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


def _ok_fake_api(_attr_name):
    class FakeApi:
        def __getattr__(self, name):
            if name == _attr_name:
                return lambda *a, **kw: None
            raise AttributeError(name)
    return FakeApi()


def _stub_settings(monkeypatch, *, ns_allowlist=None):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    if ns_allowlist is None:
        monkeypatch.delenv("K8S_MCP_NAMESPACE_ALLOWLIST", raising=False)
    else:
        monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", ns_allowlist)
    reset_settings_cache()


def test_delete_pod_marks_deprecation(monkeypatch):
    _stub_settings(monkeypatch)
    monkeypatch.setattr(pods_mod, "_core_v1", lambda: _ok_fake_api("delete_namespaced_pod"))
    out = pods_mod.delete_pod("p1", "default")
    assert "DEPRECATED" in out
    assert "delete_resource(kind='Pod')" in out
    assert "v0.5.0" in out
    assert "Pod/default/p1 deleted" in out  # original success message kept


def test_delete_service_marks_deprecation(monkeypatch):
    _stub_settings(monkeypatch)
    monkeypatch.setattr(service, "_core_v1", lambda: _ok_fake_api("delete_namespaced_service"))
    out = service.delete_service("web", "default")
    assert "DEPRECATED" in out
    assert "delete_resource(kind='Service')" in out
    assert "v0.5.0" in out
    assert "Service/default/web deleted" in out


def test_delete_ingress_marks_deprecation(monkeypatch):
    _stub_settings(monkeypatch)
    monkeypatch.setattr(service, "_networking_v1",
                        lambda: _ok_fake_api("delete_namespaced_ingress"))
    out = service.delete_ingress("main", "default")
    assert "DEPRECATED" in out
    assert "delete_resource(kind='Ingress')" in out
    assert "v0.5.0" in out
    assert "Ingress/default/main deleted" in out


def test_delete_configmap_marks_deprecation(monkeypatch):
    _stub_settings(monkeypatch)
    monkeypatch.setattr(configmap, "_core_v1",
                        lambda: _ok_fake_api("delete_namespaced_config_map"))
    out = configmap.delete_configmap("env", "default")
    assert "DEPRECATED" in out
    assert "delete_resource(kind='ConfigMap')" in out
    assert "v0.5.0" in out
    assert "ConfigMap/default/env deleted" in out


def test_delete_pvc_marks_deprecation(monkeypatch):
    _stub_settings(monkeypatch)
    monkeypatch.setattr(storage, "_core_v1",
                        lambda: _ok_fake_api("delete_namespaced_persistent_volume_claim"))
    out = storage.delete_pvc("data", "app")
    assert "DEPRECATED" in out
    assert "delete_resource(kind='PersistentVolumeClaim')" in out
    assert "v0.5.0" in out
    assert "PVC/app/data deleted" in out


def test_deprecated_delete_still_rejects_read_only(monkeypatch):
    """Even deprecated, the read_only / ns allowlist guards must still
    run first — the deprecation marker shouldn't leak past security
    checks."""
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        storage.delete_pvc("x", "allowed")


def test_api_exception_on_404_still_raises_lookup_error(monkeypatch):
    """Refactor safety: 404 → LookupError still happens before the
    deprecation marker would be appended. Confirms the guard order
    wasn't broken by the deprecation preamble."""
    _stub_settings(monkeypatch)

    class Boom:
        def delete_namespaced_pod(self, *a, **kw):
            raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(pods_mod, "_core_v1", lambda: Boom())
    with pytest.raises(LookupError, match="not found"):
        pods_mod.delete_pod("ghost", "default")


# Sanity: the import path used by the wrappers uses _core_v1() — confirm
# that hasn't drifted across the four affected modules. Keeps the
# deprecation patch honest if someone later swaps the underlying
# client construction.
def test_deprecated_wrappers_still_use_core_v1_factory(monkeypatch):
    calls: list[str] = []

    def _core_v1_factory_called():
        calls.append("core_v1")
        return _ok_fake_api("delete_namespaced_persistent_volume_claim")

    monkeypatch.setattr(storage, "_core_v1", _core_v1_factory_called)
    _stub_settings(monkeypatch)
    storage.delete_pvc("x", "default")
    assert calls == ["core_v1"]
