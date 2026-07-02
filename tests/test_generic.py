"""Tests for generic tools' safety logic (read_only, namespace allowlist).

The DynamicClient calls themselves require a live cluster; here we exercise
the guards that run before any API call.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from k8s_mcp.config import Settings, reset_settings_cache
from k8s_mcp.tools import generic


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_apply_yaml_rejects_in_read_only_mode():
    Settings(_env_file=None, read_only=True)  # noqa - just force a settings re-read
    # override the get_settings() cache via monkeypatched env
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        generic.apply_yaml("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n")


def test_apply_yaml_rejects_when_namespace_not_in_allowlist():
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "false"
    os.environ["K8S_MCP_NAMESPACE_ALLOWLIST"] = "allowed"
    reset_settings_cache()
    yaml = (
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: x\n"
        "  namespace: other\n"
    )
    with pytest.raises(PermissionError, match="not allowed"):
        generic.apply_yaml(yaml)


def test_apply_yaml_accepts_when_namespace_in_allowlist(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()

    fake_resource = _FakeResource()
    fake_dyn = _FakeDynClient(resources={"ConfigMap": fake_resource})

    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        out = generic.apply_yaml(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: allowed\n"
        )
    # FakeResource.get returns success → apply path is "configured (patched)"
    assert "ConfigMap/x" in out
    assert ("created" in out) or ("configured" in out)


def test_apply_yaml_handles_multi_doc(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    cm = _FakeResource()
    dep = _FakeResource()
    fake_dyn = _FakeDynClient(resources={"ConfigMap": cm, "Deployment": dep})

    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        out = generic.apply_yaml(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: c\n"
            "---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: d\n"
        )
    assert "ConfigMap/c" in out
    assert "Deployment/d" in out


def test_apply_yaml_raises_for_unknown_kind(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "false")
    reset_settings_cache()
    fake_dyn = _FakeDynClient(resources={})
    with patch.object(generic, "_dyn_client", return_value=fake_dyn):
        with pytest.raises(ValueError, match="Unknown kind"):
            generic.apply_yaml("apiVersion: v1\nkind: WeirdKind\nmetadata:\n  name: x\n")


# ---- fakes --------------------------------------------------------------------


class _FakeResource:
    def apply(self, body, namespace=None):
        return _FakeApplied(body["metadata"]["name"])

    def get(self, name, namespace=None, **kwargs):
        from kubernetes.dynamic.exceptions import NotFoundError
        if name == "__missing__":
            raise NotFoundError("not found")
        return _FakeApplied(name)

    def create(self, body, namespace=None, **kwargs):
        return _FakeApplied(body["metadata"]["name"])

    def patch(self, body, namespace=None, **kwargs):
        return _FakeApplied(body["metadata"]["name"])

    def delete(self, name, namespace=None, **kwargs):
        return None


class _FakeApplied:
    def __init__(self, name):
        self._name = name

    def to_dict(self):
        return {"metadata": {"name": self._name}}


class _FakeDynClient:
    def __init__(self, resources):
        self._resources = resources

    @property
    def resources(self):
        return _FakeResources(self._resources)


class _FakeResources:
    def __init__(self, resources):
        self._resources = resources

    def get(self, api_version=None, kind=None):
        if kind not in self._resources:
            from kubernetes.dynamic.exceptions import ResourceNotFoundError
            raise ResourceNotFoundError(f"nope {kind}")
        return self._resources[kind]
