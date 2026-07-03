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
