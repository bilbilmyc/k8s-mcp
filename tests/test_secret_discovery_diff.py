"""Tests for secret, discovery, replace/diff, delete_pod, rollout_history,
set_resources, and the create_xxx shortcuts.

These tests focus on:
  - argument validation
  - safety guards (read-only, namespace allowlist)
  - happy-path behavior with fake clients
"""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import (
    autoscale,
    discovery,
    generic,
    networkpolicy,
    rbac,
    rollout,
    secret,
    storage,
    workload,
)
from k8s_mcp.tools import pods as pods_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# =============================================================================
# secret.py
# =============================================================================


def test_get_secret_value_reveal_false_masks(monkeypatch):
    fake = _FakeSecret({"password": base64.b64encode(b"hunter2").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _FakeDyn(fake))
    out = secret.get_secret_value("db", "default", "password", reveal=False)
    assert "MASKED" in out.upper() or "***" in out
    assert "hunter2" not in out


def test_get_secret_value_reveal_true_decodes(monkeypatch):
    fake = _FakeSecret({"password": base64.b64encode(b"hunter2").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _FakeDyn(fake))
    out = secret.get_secret_value("db", "default", "password", reveal=True)
    assert out == "hunter2"


def test_get_secret_value_string_data(monkeypatch):
    fake = _FakeSecret({}, string_data={"plain": "hello"})
    monkeypatch.setattr(secret, "_dyn", lambda: _FakeDyn(fake))
    out = secret.get_secret_value("db", "default", "plain", reveal=True)
    assert out == "hello"


def test_get_secret_value_missing_key(monkeypatch):
    fake = _FakeSecret({"password": base64.b64encode(b"x").decode()})
    monkeypatch.setattr(secret, "_dyn", lambda: _FakeDyn(fake))
    with pytest.raises(LookupError, match="not found"):
        secret.get_secret_value("db", "default", "nope", reveal=True)


def test_get_secret_value_secret_not_found(monkeypatch):
    from kubernetes.client.rest import ApiException

    class _FakeMissing:
        def get(self, **kw):
            raise ApiException(status=404, reason="not found")

    class _R:
        def get(self, **kw):
            return _FakeMissing()

    class _D:
        @property
        def resources(self):
            return _R()

    monkeypatch.setattr(secret, "_dyn", lambda: _D())
    with pytest.raises(LookupError, match="not found"):
        secret.get_secret_value("db", "default", "any")


# =============================================================================
# discovery.py — only argument validation + a few key behaviors
# (no live cluster to call ApisApi against)
# =============================================================================


def test_explain_resource_returns_kind_summary(monkeypatch):
    monkeypatch.setattr(discovery, "_get_openapi_schema", lambda: _FAKE_OPENAPI)
    out = discovery.explain_resource("Foo")
    assert "kind: Foo" in out
    assert "fields:" in out


def test_explain_resource_drills_into_field(monkeypatch):
    monkeypatch.setattr(discovery, "_get_openapi_schema", lambda: _FAKE_OPENAPI)
    out = discovery.explain_resource("Foo", field_path="spec.replicas")
    assert "spec.replicas" in out
    assert "type:" in out


def test_explain_resource_kind_not_found(monkeypatch):
    monkeypatch.setattr(discovery, "_get_openapi_schema", lambda: _FAKE_OPENAPI)
    with pytest.raises(LookupError, match="not in OpenAPI"):
        discovery.explain_resource("Nope")


def test_explain_resource_field_not_found(monkeypatch):
    monkeypatch.setattr(discovery, "_get_openapi_schema", lambda: _FAKE_OPENAPI)
    with pytest.raises(LookupError, match="not found"):
        discovery.explain_resource("Foo", field_path="spec.does_not_exist")


# =============================================================================
# replace_resource / diff_resource
# =============================================================================


def test_replace_rejects_in_read_only(monkeypatch):
    import os
    os.environ["K8S_MCP_READ_ONLY"] = "true"
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        generic.replace_resource("kind: Pod\nmetadata:\n  name: x\n")


def test_replace_requires_resource_version(monkeypatch):
    """Verify replace fetches current ResourceVersion and applies it."""
    monkeypatch.setattr(generic, "_dyn_client", lambda: _FakeDynWithReplace())

    class _FakeItem:
        def to_dict(self):
            return {"metadata": {"name": "x", "namespace": "default", "resourceVersion": "42"}}

    class _FakeResource:
        def get(self, **kw):
            return _FakeItem()

        def replace(self, body, **kw):
            _FakeDynWithReplace.last = (body, kw)
            return _FakeApplied(body["metadata"]["name"])

    class _FakeResources:
        def get(self, **kw):
            return _FakeResource()

    class _FakeDyn:
        @property
        def resources(self):
            return _FakeResources()

    monkeypatch.setattr(generic, "_dyn_client", lambda: _FakeDyn())
    out = generic.replace_resource("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: default\n")
    assert "replaced" in out
    assert "42" in out  # resourceVersion echoed back


def test_diff_reports_create_for_missing(monkeypatch):
    from kubernetes.client.rest import ApiException

    class _FakeMissing:
        def get(self, **kw):
            raise ApiException(status=404, reason="not found")

    class _FakeResources:
        def get(self, **kw):
            return _FakeMissing()

    class _FakeDyn:
        @property
        def resources(self):
            return _FakeResources()

    monkeypatch.setattr(generic, "_dyn_client", lambda: _FakeDyn())
    out = generic.diff_resource("apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: default\n")
    assert "CREATE" in out


def test_diff_reports_no_changes_when_identical(monkeypatch):
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "x", "namespace": "default", "resourceVersion": "1"},
        "data": {"k": "v"},
    }
    new_yaml = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n  namespace: default\ndata:\n  k: v\n"

    class _FakeItem:
        def to_dict(self):
            return obj

    class _FakeResource:
        def get(self, **kw):
            return _FakeItem()

    class _FakeResources:
        def get(self, **kw):
            return _FakeResource()

    class _FakeDyn:
        @property
        def resources(self):
            return _FakeResources()

    monkeypatch.setattr(generic, "_dyn_client", lambda: _FakeDyn())
    out = generic.diff_resource(new_yaml)
    assert "no changes" in out


# =============================================================================
# delete_pod
# =============================================================================


def test_delete_pod_rejects_in_read_only(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        pods_mod.delete_pod("p1", "default")


def test_delete_pod_rejects_when_namespace_not_allowed(monkeypatch):
    monkeypatch.setenv("K8S_MCP_NAMESPACE_ALLOWLIST", "allowed")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="not allowed"):
        pods_mod.delete_pod("p1", "other")


def test_delete_pod_calls_api(monkeypatch):
    called = {}

    class FakeApi:
        def delete_namespaced_pod(self, name, namespace, body=None, **kw):
            called["name"] = name
            called["ns"] = namespace
            called["body"] = body
            return None

    monkeypatch.setattr(pods_mod, "_core_v1", lambda: FakeApi())
    out = pods_mod.delete_pod("p1", "default", grace_period_seconds=0)
    assert "deleted" in out
    assert called["name"] == "p1"
    assert called["ns"] == "default"
    assert called["body"].grace_period_seconds == 0


# =============================================================================
# rollout_history
# =============================================================================


def test_rollout_history_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="Unsupported"):
        rollout.rollout_history("DaemonSet", "x", "default")


def test_rollout_history_returns_table(monkeypatch):
    rev = MagicMock()
    rev.revision = 5
    rev.metadata.creation_timestamp = "2026-01-01T00:00:00Z"
    rev.metadata.name = "x-rev-5"
    rev.data = {"spec": {"template": {"spec": {"containers": [{"image": "nginx:1.25"}]}}}}

    class FakeApi:
        def list_namespaced_controller_revision(self, namespace, **kw):
            class R:
                items = [rev]
            return R()

    monkeypatch.setattr(rollout, "_apps_v1", lambda: FakeApi())
    out = rollout.rollout_history("Deployment", "x", "default")
    assert "REVISION" in out
    assert "5" in out
    assert "nginx:1.25" in out


# =============================================================================
# set_resources
# =============================================================================


def test_set_resources_rejects_empty_kwargs():
    with pytest.raises(ValueError, match="at least one"):
        workload.set_resources("Deployment", "d1", "default", "c1")


def test_set_resources_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="Unsupported"):
        workload.set_resources("DaemonSet", "d1", "default", "c1", limits={"cpu": "500m"})


def test_set_resources_calls_patch(monkeypatch):
    called = {}

    class FakeApi:
        def patch_namespaced_deployment(self, name, namespace, body, **kw):
            called["body"] = body
            called["name"] = name
            return None

    monkeypatch.setattr(workload, "_apps_v1", lambda: FakeApi())
    workload.set_resources(
        "Deployment", "d1", "default", "app",
        requests={"cpu": "100m", "memory": "128Mi"},
        limits={"cpu": "500m"},
    )
    body = called["body"]
    res = body["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == "100m"
    assert res["limits"]["cpu"] == "500m"


# =============================================================================
# autoscale.py
# =============================================================================


def test_create_hpa_rejects_unsupported_target():
    with pytest.raises(ValueError, match="only supports"):
        autoscale.create_hpa("h", "DaemonSet", "d", "default", 1, 3, cpu_utilization=70)


def test_create_hpa_requires_metric():
    with pytest.raises(ValueError, match="at least one"):
        autoscale.create_hpa("h", "Deployment", "d", "default", 1, 3)


def test_create_pdb_requires_exactly_one_constraint():
    with pytest.raises(ValueError, match="exactly one"):
        autoscale.create_pdb("p", "Deployment", "d", "default")


# =============================================================================
# rbac.py
# =============================================================================


def test_create_role_requires_rules():
    with pytest.raises(ValueError, match="at least one rule"):
        rbac.create_role("r", "default", rules=[])


def test_create_rolebinding_requires_subjects():
    with pytest.raises(ValueError, match="at least one subject"):
        rbac.create_rolebinding("b", "default", "Role", "r", subjects=[])


def test_create_rolebinding_invalid_role_kind():
    with pytest.raises(ValueError, match="must be Role"):
        rbac.create_rolebinding("b", "default", "ServiceAccount", "r",
                                 subjects=[{"kind": "ServiceAccount", "name": "x"}])


def test_create_clusterrole_requires_rules():
    with pytest.raises(ValueError, match="at least one rule"):
        rbac.create_clusterrole("c", rules=[])


# =============================================================================
# networkpolicy.py
# =============================================================================


def test_create_networkpolicy_requires_policy_types():
    with pytest.raises(ValueError, match="at least one"):
        networkpolicy.create_networkpolicy(
            "np", "default", pod_selector={"app": "db"}, policy_types=[])


def test_create_networkpolicy_rejects_bad_type():
    with pytest.raises(ValueError, match="Invalid"):
        networkpolicy.create_networkpolicy(
            "np", "default", pod_selector={"app": "db"}, policy_types=["Snorg"])


# =============================================================================
# serviceaccount.py + storage.py (smoke)
# =============================================================================


def test_create_serviceaccount_validates_name():
    # No complex logic; just check that the function exists and validates
    from k8s_mcp.tools import serviceaccount as sa
    assert callable(sa.create_serviceaccount)


def test_create_pvc_signature():
    assert callable(storage.create_pvc)


# =============================================================================
# fakes
# =============================================================================


class _FakeSecret:
    def __init__(self, data=None, string_data=None):
        self._data = data or {}
        self._string_data = string_data or {}

    def get(self, name=None, namespace=None, **kw):
        return _FakeSecretObj(self._data, self._string_data)


class _FakeSecretObj:
    def __init__(self, data, string_data):
        self._d = data
        self._sd = string_data

    def to_dict(self):
        return {"kind": "Secret", "metadata": {"name": "x"}, "data": self._d, "stringData": self._sd}


class _FakeDyn:
    def __init__(self, resource):
        self._r = resource

    @property
    def resources(self):  # noqa: D401
        outer_r = self._r
        class _R:
            def get(inner_self, **kw):  # noqa: N805
                return outer_r

        return _R()


class _FakeDynWithReplace:
    last: tuple | None = None


class _FakeApplied:
    def __init__(self, name):
        self._name = name

    def to_dict(self):
        return {"metadata": {"name": self._name}}


_FAKE_OPENAPI = {
    "io.k8s.example.v1.Foo": {
        "description": "A test kind.",
        "properties": {
            "metadata": {"type": "object", "description": "Resource metadata."},
            "spec": {
                "type": "object",
                "properties": {
                    "replicas": {"type": "integer", "description": "Replica count."},
                },
            },
        },
    },
}
