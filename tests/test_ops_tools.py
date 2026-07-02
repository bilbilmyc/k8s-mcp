"""Tests for node_ops, wait, jsonpath, rollout, metrics safety and behavior."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from k8s_mcp.config import reset_settings_cache
from k8s_mcp.tools import jsonpath as jsonpath_mod
from k8s_mcp.tools import metrics, node_ops, rollout
from k8s_mcp.tools import wait_tool as wait_mod


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


# =============================================================================
# Helpers — fake api objects
# =============================================================================


class _RecordingApi:
    """Stand-in for CoreV1Api/AppsV1Api with method-recording behavior."""

    def __init__(self):
        self.calls = []
        self._raise = None

    def set_not_found(self, on_method):
        self._raise = ("NotFound", on_method)

    def _maybe_raise(self, name):
        if self._raise and self._raise[1] == name:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=404, reason="nope")
        return None


class _FakeCoreV1Api:
    """Implements the CoreV1Api surface used by node_ops/rollout."""

    def __init__(self):
        self.patches = []
        self.reads = []
        self.list_pods = []
        self.deletes = []
        self.evictions = []

    def patch_node(self, name, body, **kwargs):
        self.patches.append((name, body))
        return None

    def read_node(self, name, **kwargs):
        self.reads.append(name)
        return None

    def list_pod_for_all_namespaces(self, **kwargs):
        self.list_pods.append(kwargs)
        return _FakePodList(self.list_pods_items)

    def delete_namespaced_pod(self, name, ns, **kwargs):
        self.deletes.append((name, ns, kwargs.get("body")))
        return None

    def create_namespaced_pod_eviction(self, name, ns, body=None, **kwargs):
        self.evictions.append((name, ns, body))
        return None


class _FakePodList:
    def __init__(self, items):
        self.items = items


def _make_pod(ns, name, owner_kind=None, vols=None):
    p = MagicMock()
    p.metadata.namespace = ns
    p.metadata.name = name
    p.metadata.owner_references = []
    if owner_kind:
        ref = MagicMock()
        ref.kind = owner_kind
        p.metadata.owner_references = [ref]
    if vols:
        p.spec.volumes = vols
    else:
        p.spec.volumes = []
    return p


def _empty_volume():
    v = MagicMock()
    v.empty_dir = {"medium": ""}
    return v


@pytest.fixture
def fake_core_api(monkeypatch):
    """Install a single _FakeCoreV1Api and return it for assertions."""
    api = _FakeCoreV1Api()
    # patch both module-level helpers so CoreV1Api(get_api_client()) calls return our fake
    monkeypatch.setattr(node_ops, "_core_v1", lambda: api)
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda configuration=None: api)
    return api


# =============================================================================
# node_ops: cordon / uncordon / drain
# =============================================================================


def test_cordon_rejects_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        node_ops.cordon_node("n1")


def test_uncordon_rejects_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        node_ops.uncordon_node("n1")


def test_drain_rejects_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        node_ops.drain_node("n1")


def test_cordon_calls_patch_node(fake_core_api):
    fake_core_api.list_pods_items = []
    out = node_ops.cordon_node("n1")
    assert "cordoned" in out
    assert fake_core_api.patches[-1] == ("n1", {"spec": {"unschedulable": True}})


def test_uncordon_calls_patch_node(fake_core_api):
    fake_core_api.list_pods_items = []
    out = node_ops.uncordon_node("n1")
    assert "uncordoned" in out
    assert fake_core_api.patches[-1] == ("n1", {"spec": {"unschedulable": False}})


def test_drain_raises_when_node_not_found(fake_core_api):
    fake_core_api.list_pods_items = []

    def boom(name, **kw):
        from kubernetes.client.rest import ApiException
        raise ApiException(status=404, reason="nope")

    fake_core_api.read_node = boom
    with pytest.raises(LookupError, match="not found"):
        node_ops.drain_node("n1")


def test_drain_cordons_and_skips_daemonset_pods(fake_core_api):
    fake_core_api.list_pods_items = [
        _make_pod("default", "ds-abc", owner_kind="DaemonSet")
    ]
    out = node_ops.drain_node("n1")
    assert "drained" in out
    assert "ds-abc" in out
    assert "Skipped DaemonSet" in out
    assert fake_core_api.evictions == []
    # Cordon patch happened
    assert any(b.get("spec", {}).get("unschedulable") is True for _, b in fake_core_api.patches)


def test_drain_skips_emptydir_pods(fake_core_api):
    fake_core_api.list_pods_items = [_make_pod("default", "ed-1", vols=[_empty_volume()])]
    out = node_ops.drain_node("n1")
    assert "Skipped emptyDir" in out
    assert "delete_emptydir_data=True" in out


def test_drain_force_uses_delete(fake_core_api):
    fake_core_api.list_pods_items = [_make_pod("default", "p1")]
    out = node_ops.drain_node("n1", force=True)
    assert "forced delete" in out
    assert fake_core_api.deletes[0][0] == "p1"
    assert fake_core_api.deletes[0][1] == "default"


# =============================================================================
# wait: wait_resource and JSONPath helper
# =============================================================================


def test_wait_requires_one_of():
    with pytest.raises(ValueError, match="Provide"):
        wait_mod.wait_resource("Pod", "p1", namespace="default")


def test_wait_rejects_both_modes():
    with pytest.raises(ValueError, match="mutually exclusive"):
        wait_mod.wait_resource(
            "Pod", "p1", namespace="default",
            for_condition="Ready", for_jsonpath="status.phase",
        )


def test_wait_jsonpath_requires_value():
    with pytest.raises(ValueError, match="requires jsonpath_value"):
        wait_mod.wait_resource(
            "Pod", "p1", namespace="default", for_jsonpath="status.phase"
        )


def test_jsonpath_dotted():
    obj = {"status": {"replicas": 3, "phase": "Running"}}
    assert wait_mod._jsonpath(obj, "status.phase") == "Running"
    assert wait_mod._jsonpath(obj, "status.replicas") == 3


def test_jsonpath_nested_with_index():
    obj = {"spec": {"containers": [
        {"name": "x", "image": "nginx"},
        {"name": "y", "image": "redis"},
    ]}}
    assert wait_mod._jsonpath(obj, "spec.containers[0].image") == "nginx"
    assert wait_mod._jsonpath(obj, "spec.containers[1].name") == "y"


def test_jsonpath_missing_raises():
    with pytest.raises(LookupError):
        wait_mod._jsonpath({"status": {}}, "status.replicas")


def test_wait_condition_met(monkeypatch):
    obj = {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}

    class FakeItem:
        def to_dict(self):
            return obj

    class FakeResource:
        def get(self, **kwargs):
            return FakeItem()

    class FakeResources:
        def get(self, **kwargs):
            return FakeResource()

    class FakeDyn:
        @property
        def resources(self):
            return FakeResources()

    monkeypatch.setattr(wait_mod, "_dyn", lambda: FakeDyn())
    out = wait_mod.wait_resource(
        "Pod", "p1", namespace="default", for_condition="Ready", timeout_seconds=5
    )
    assert "Ready" in out


def test_wait_condition_timeout(monkeypatch):
    """The resource never satisfies the condition → TimeoutError."""
    obj = {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}

    class FakeItem:
        def to_dict(self):
            return obj

    class FakeResource:
        def get(self, **kwargs):
            return FakeItem()

    class FakeResources:
        def get(self, **kwargs):
            return FakeResource()

    class FakeDyn:
        @property
        def resources(self):
            return FakeResources()

    monkeypatch.setattr(wait_mod, "_dyn", lambda: FakeDyn())

    # Make sleep instantaneous so we don't actually wait
    import time as time_mod
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)

    with pytest.raises(TimeoutError, match="Timeout"):
        wait_mod.wait_resource(
            "Pod", "p1", namespace="default", for_condition="Ready", timeout_seconds=0
        )


# =============================================================================
# jsonpath: get_resource_jsonpath
# =============================================================================


def test_jsonpath_unknown_kind(monkeypatch):
    class _R:
        def get(self, **kwargs):
            raise Exception("no")
    class _Rsc:
        def get(self, **kwargs):
            raise Exception("nope")
    class _D:
        @property
        def resources(self):
            return _Rsc()
    monkeypatch.setattr(jsonpath_mod, "_dyn", lambda: _D())
    with pytest.raises(ValueError, match="Unknown kind"):
        jsonpath_mod.get_resource_jsonpath("NotARealKind", "spec.foo")


def test_jsonpath_single_resource(monkeypatch):
    obj = {"status": {"phase": "Running"}}

    class FakeItem:
        def to_dict(self):
            return obj

    class FakeResource:
        def get(self, **kwargs):
            return FakeItem()

    class FakeResources:
        def get(self, **kwargs):
            return FakeResource()

    class FakeDyn:
        @property
        def resources(self):
            return FakeResources()

    monkeypatch.setattr(jsonpath_mod, "_dyn", lambda: FakeDyn())
    out = jsonpath_mod.get_resource_jsonpath(
        "Pod", "status.phase", name="p1", namespace="default"
    )
    assert out == "Running"


def test_jsonpath_missing_field(monkeypatch):
    class FakeItem:
        def to_dict(self):
            return {"status": {}}

    class FakeResource:
        def get(self, **kwargs):
            return FakeItem()

    class FakeResources:
        def get(self, **kwargs):
            return FakeResource()

    class FakeDyn:
        @property
        def resources(self):
            return FakeResources()

    monkeypatch.setattr(jsonpath_mod, "_dyn", lambda: FakeDyn())
    with pytest.raises(LookupError):
        jsonpath_mod.get_resource_jsonpath(
            "Pod", "status.phase", name="p1", namespace="default"
        )


def test_jsonpath_list_mode(monkeypatch):
    class FakeItem:
        def __init__(self, phase):
            self.phase = phase

        def to_dict(self):
            return {"status": {"phase": self.phase}}

    class FakeList:
        def __init__(self, items):
            self.items = items

    class FakeResource:
        def get(self, **kwargs):
            return FakeList([FakeItem("Running"), FakeItem("Pending")])

    class FakeResources:
        def get(self, **kwargs):
            return FakeResource()

    class FakeDyn:
        @property
        def resources(self):
            return FakeResources()

    monkeypatch.setattr(jsonpath_mod, "_dyn", lambda: FakeDyn())
    out = jsonpath_mod.get_resource_jsonpath("Pod", "status.phase", namespace="default")
    assert out == "Running\nPending"


# =============================================================================
# rollout / metrics safety
# =============================================================================


def test_rollout_undo_rejects_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("K8S_MCP_READ_ONLY", "true")
    reset_settings_cache()
    with pytest.raises(PermissionError, match="read-only"):
        rollout.rollout_undo("Deployment", "d1", namespace="default")


def test_rollout_undo_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="Unsupported kind"):
        rollout.rollout_undo("DaemonSet", "d1", namespace="default")


def test_rollout_status_rejects_unsupported_kind():
    with pytest.raises(ValueError, match="Unsupported kind"):
        rollout.rollout_status("DaemonSet", "d1", namespace="default")


def test_metrics_helpers():
    """The CPU/memory quantity parsers should handle all K8s suffixes."""
    assert metrics._parse_cpu("100m") == 0.1
    assert metrics._parse_cpu("1500u") == 0.0015
    assert metrics._parse_cpu("2") == 2.0

    assert metrics._parse_mem("128Mi") == 128 * 1024 * 1024
    assert metrics._parse_mem("1Gi") == 1024 * 1024 * 1024
    assert metrics._parse_mem("500000") == 500000


def test_metrics_helpers_round_trip_format():
    assert metrics._fmt_cpu(0.05) == "50m"
    assert metrics._fmt_cpu(1.234) == "1.23"
    assert metrics._fmt_mem(123) == "123B"
    assert metrics._fmt_mem(2048) == "2Ki"
