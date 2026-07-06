"""Tests for `find_images` and `get_events_for_object`.

Strategy: monkeypatch `generic._dyn_client` + `generic._resource_for_kind`
to feed a controlled list of dicts into the tools, and assert the rendered
output.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from k8s_mcp.tools import discovery, events

# ---------- fake DynamicClient model ---------------------------------------


class _FakeList:
    def __init__(self, items): self.items = items


class _FakeResource:
    def __init__(self, items): self._items = items

    def get(self, **kwargs):
        return _FakeList(self._items)


class _FakeDC:
    pass


def _install_workloads(monkeypatch, items_per_kind: dict[str, list]):
    """items_per_kind: {"Deployment": [...], "StatefulSet": [...], ...}"""
    from k8s_mcp.tools import generic as _generic

    def _resource_for_kind(dc, kind):
        return _FakeResource(items_per_kind.get(kind, []))

    monkeypatch.setattr(_generic, "_dyn_client", lambda: _FakeDC())
    monkeypatch.setattr(_generic, "_resource_for_kind", _resource_for_kind)


def _deploy(ns: str, name: str, image: str, init_image: str | None = None) -> dict:
    containers = [{"name": "app", "image": image}]
    spec: dict = {"containers": containers}
    if init_image:
        spec["initContainers"] = [{"name": "init", "image": init_image}]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"template": {"spec": spec}},
    }


# ---------- find_images ----------------------------------------------------


def test_find_images_substring_match(monkeypatch):
    _install_workloads(monkeypatch, {
        "Deployment": [
            _deploy("app", "web", "nginx:1.21"),
            _deploy("app", "api", "redis:7"),
        ],
    })
    out = discovery.find_images("nginx:1.21")
    assert "web" in out
    assert "nginx:1.21" in out
    # redis workload is NOT a match
    assert "api" not in out
    assert "redis" not in out


def test_find_images_case_insensitive(monkeypatch):
    _install_workloads(monkeypatch, {
        "Deployment": [_deploy("app", "web", "NGINX:1.21")],
    })
    out = discovery.find_images("nginx")
    assert "NGINX:1.21" in out


def test_find_images_matches_init_containers(monkeypatch):
    _install_workloads(monkeypatch, {
        "Deployment": [_deploy("app", "web", "nginx:1.25", init_image="busybox:1.36")],
    })
    out = discovery.find_images("busybox")
    assert "web" in out
    assert "busybox:1.36" in out
    # init marker present
    assert "[init]" in out


def test_find_images_no_match_returns_friendly_message(monkeypatch):
    _install_workloads(monkeypatch, {
        "Deployment": [_deploy("app", "web", "nginx:1.25")],
    })
    out = discovery.find_images("does-not-exist")
    assert "no workloads" in out
    assert "does-not-exist" in out


def test_find_images_empty_substring_raises(monkeypatch):
    with pytest.raises(ValueError, match="image_substring"):
        discovery.find_images("")


def test_find_images_custom_kinds(monkeypatch):
    _install_workloads(monkeypatch, {
        "Deployment": [_deploy("app", "web", "nginx:1.25")],
        "StatefulSet": [],
    })
    out = discovery.find_images("nginx", kinds=["StatefulSet"])
    # Deployment is not in custom kinds → not searched
    assert "web" not in out
    assert "no workloads" in out


# ---------- get_events_for_object ------------------------------------------


def _ev(kind: str, name: str, reason: str, msg: str,
         ts: datetime | None = None):
    """Build a fake Event object with attribute access (events._ts uses
    e.last_timestamp / e.first_timestamp)."""

    class _IO:
        def __init__(self, kind, name):
            self.kind = kind
            self.name = name

    now = (ts or datetime.now(UTC)).isoformat()
    e = type("E", (), {})()
    e.type = "Warning"
    e.reason = reason
    e.message = msg
    e.involved_object = _IO(kind, name)
    e.last_timestamp = now
    e.first_timestamp = now
    e.event_time = None
    e.metadata = type("M", (), {"creation_timestamp": None})()
    e.count = 1
    return e


class _FakeEventResource:
    def __init__(self, items, captured: dict):
        self._items = items
        self.captured = captured

    def get(self, **kwargs):
        self.captured.update(kwargs)
        return _FakeList(self._items)


def test_get_events_for_object_uses_field_selector(monkeypatch):
    captured: dict = {}

    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            captured["ns"] = namespace
            captured["selector"] = field_selector
            return _FakeList([_ev("Pod", "web-1", "BackOff", "restarting")])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.get_events_for_object(kind="Pod", name="web-1",
                                       namespace="app")
    assert "BackOff" in out
    assert "restarting" in out
    assert captured["ns"] == "app"
    assert "involvedObject.kind=Pod" in captured["selector"]
    assert "involvedObject.name=web-1" in captured["selector"]


def test_get_events_for_object_no_events_returns_friendly_message(monkeypatch):
    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            return _FakeList([])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.get_events_for_object(kind="Pod", name="ghost",
                                       namespace="app")
    assert "no events" in out
    assert "Pod/ghost" in out
    assert "app" in out


def test_get_events_for_object_cluster_scoped(monkeypatch):
    """When namespace is None, the tool should call the cluster-wide list."""
    captured: dict = {}

    class _CoreV1:
        def list_event_for_all_namespaces(self, field_selector=None):
            captured["all_ns"] = True
            captured["selector"] = field_selector
            return _FakeList([])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.get_events_for_object(kind="Node", name="node-1",
                                       namespace=None)
    assert captured.get("all_ns") is True
    assert "no events" in out


# =============================================================================
# list_events — multi-namespace support (P5)
# =============================================================================


def _ev(kind, name, reason, msg, *, ts=None):
    """Build a fake k8s Event object with the fields _collect_events reads."""
    from datetime import datetime
    e = type("E", (), {})()
    e.type = "Warning"
    e.reason = reason
    e.involved_object = type("O", (), {"kind": kind, "name": name})()
    e.message = msg
    e.last_timestamp = ts or datetime(2026, 1, 1, tzinfo=UTC)
    e.first_timestamp = None
    e.event_time = None
    e.metadata = type("M", (), {"creation_timestamp": None})()
    e.count = 1
    return e


def test_list_events_namespaces_param_empty_returns_no_events(monkeypatch):
    """An empty namespaces list is a valid query — caller wanted 'no
    namespaces', not 'all namespaces'. Surface "(no events)" cleanly."""
    # If anything tries to hit the apiserver the test should fail because
    # _core_v1 is not monkeypatched; provide one that asserts non-call.
    called = {"n": 0}

    class _CoreV1:
        def list_namespaced_event(self, *a, **kw):
            called["n"] += 1
            return _FakeList([])

        def list_event_for_all_namespaces(self, *a, **kw):
            called["n"] += 1
            return _FakeList([])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.list_events(namespaces=[])
    assert out == "(no events)"
    # Empty list short-circuits before any apiserver call.
    assert called["n"] == 0


def test_list_events_namespaces_single_collapses_to_namespace(monkeypatch):
    """A single-element namespaces list takes the same code path as the
    legacy `namespace=` arg — no per-namespace fan-out overhead."""
    captured: dict = {}

    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            captured["ns"] = namespace
            return _FakeList([_ev("Pod", "web", "BackOff", "boom")])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.list_events(namespaces=["app"], warning_only=True)
    assert captured["ns"] == "app"
    assert "BackOff" in out


def test_list_events_namespaces_multi_fans_out_per_namespace(monkeypatch):
    """2+ namespaces → call list_namespaced_event once per namespace,
    never the cluster-wide list. Before P5 this would silently broaden
    to list_event_for_all_namespaces and pollute the snapshot with
    unrelated namespaces' noise."""
    captured: dict = {"nss": []}

    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            captured["nss"].append(namespace)
            if namespace == "app":
                return _FakeList([_ev("Pod", "web", "BackOff", "restarting")])
            return _FakeList([_ev("Pod", "db", "OOMKilled", "killed")])

        def list_event_for_all_namespaces(self, *a, **kw):
            captured["all_ns_called"] = True
            return _FakeList([])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.list_events(namespaces=["app", "data"], warning_only=True)
    assert sorted(captured["nss"]) == ["app", "data"]
    assert not captured.get("all_ns_called")
    # Both rows present in output
    assert "BackOff" in out
    assert "OOMKilled" in out


def test_list_events_namespaces_multi_sorts_by_recency(monkeypatch):
    """When fanned-out results merge, the newest events surface first —
    not the order of namespace iteration. Verified by stamping distinct
    lastTimestamp values and checking the rendered row order."""
    from datetime import datetime, timedelta

    newer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    older = newer - timedelta(hours=2)

    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            # "app" returns the older event; "data" returns the newer.
            if namespace == "app":
                return _FakeList([_ev("Pod", "web", "BackOff", "old", ts=older)])
            return _FakeList([_ev("Pod", "db", "OOMKilled", "new", ts=newer)])

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.list_events(namespaces=["app", "data"], warning_only=True)
    # Newer event renders before the older one.
    assert out.index("OOMKilled") < out.index("BackOff")


def test_list_events_namespaces_multi_truncates_to_limit(monkeypatch):
    """Merged fan-out truncates to `limit`, not 2×limit. We push 4
    events (2 per namespace) and ask for 3 → exactly 3 rendered."""
    from datetime import datetime, timedelta
    base = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    def _events_for(ns):
        # 2 events per namespace, with descending timestamps
        return _FakeList([
            _ev("Pod", f"{ns}-1", f"R-{ns}-1", f"m-{ns}-1",
                ts=base - timedelta(minutes=0)),
            _ev("Pod", f"{ns}-2", f"R-{ns}-2", f"m-{ns}-2",
                ts=base - timedelta(minutes=10)),
        ])

    class _CoreV1:
        def list_namespaced_event(self, namespace, field_selector=None):
            return _events_for(namespace)

    monkeypatch.setattr(events, "_core_v1", lambda: _CoreV1())
    out = events.list_events(
        namespaces=["app", "data"], warning_only=True, limit=3,
    )
    # Each rendered row corresponds to a REASON. Count REASON tokens.
    reasons = [r for r in ["R-app-1", "R-app-2", "R-data-1", "R-data-2"]
               if r in out]
    assert len(reasons) == 3, f"expected 3 reasons, got {reasons}"
