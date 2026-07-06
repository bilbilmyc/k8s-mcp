"""Tests for `analyze_resource_usage` — static requests/limits auditor.

We patch `resource_usage._dyn_client` and `_resource_for_kind` to
return dict-based pods / deployments / statefulsets / daemonsets. The
analyzer only touches `.spec.containers[*].resources.{requests,limits}`
via plain dict.get, so dict fakes are sufficient.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import NotFoundError

from k8s_mcp.tools import resource_usage as ru

# ---------- fakes -----------------------------------------------------------


class _FakeReturn:
    def __init__(self, items):
        self.items = items


class _FakeResource:
    def __init__(self, items):
        self._items = items

    def get(self, namespace=None, label_selector=None, name=None):
        if name is not None:
            for it in self._items:
                if (it.get("metadata") or {}).get("name") == name:
                    return it
            raise NotFoundError(ApiException(status=404, reason="not found"))
        return _FakeReturn(self._items)


def _patch(pods=(), deploys=(), statefulsets=(), daemonsets=()):
    registry = {
        ("v1", "Pod"): _FakeResource(pods),
        ("apps/v1", "Deployment"): _FakeResource(deploys),
        ("apps/v1", "StatefulSet"): _FakeResource(statefulsets),
        ("apps/v1", "DaemonSet"): _FakeResource(daemonsets),
    }

    def resource_for_kind(dc, kind, api_version=None):
        return registry[(api_version, kind)]

    return patch.multiple(
        ru,
        _dyn_client=lambda: object(),
        _generic=MagicMock(_resource_for_kind=resource_for_kind),
    )


# ---------- builders --------------------------------------------------------


def _cont(name, *, image="nginx:1.25", requests=None, limits=None):
    return {
        "name": name, "image": image,
        "resources": {
            "requests": requests or {},
            "limits": limits or {},
        },
    }


def _pod(name, *, containers=None, labels=None, owner_kind="Deployment"):
    meta: dict = {"name": name, "namespace": "default",
                  "labels": labels or {"app": name}}
    if owner_kind:
        meta["ownerReferences"] = [{"kind": owner_kind, "name": name}]
    return {
        "metadata": meta,
        "spec": {
            "containers": containers or [],
        },
    }


def _workload(kind, name, *, containers=None, replicas=1):
    spec = {"replicas": replicas, "template": {
        "metadata": {"labels": {"app": name}},
        "spec": {"containers": containers or []},
    }}
    return {
        "apiVersion": "apps/v1", "kind": kind,
        "metadata": {"name": name, "namespace": "default"},
        "spec": spec,
    }


# ---------- building blocks -------------------------------------------------


def test_classify_containers_marks_missing_requests():
    healthy = _cont("a", requests={"cpu": "100m"})
    unbound = _cont("b", requests={})
    bare = _cont("c")
    rows = list(ru._per_container_rows([healthy, unbound, bare]))
    by = {r["NAME"]: r for r in rows}
    assert by["a"]["REQUESTS"] == "✓"
    assert by["a"]["LIMITS"] == "❌"
    assert by["b"]["REQUESTS"] == "❌"
    assert by["c"]["REQUESTS"] == "❌"
    assert by["c"]["LIMITS"] == "❌"


def test_extract_containers_from_pod_spec():
    p = _pod("p", owner_kind="", containers=[_cont("x", requests={"cpu": "1"})])
    conts = list(ru._extract_pod_containers(p))
    assert len(conts) == 1
    assert conts[0]["name"] == "x"


def test_extract_containers_from_workload_spec():
    d = _workload("Deployment", "web", containers=[
        _cont("app"), _cont("side"),
    ])
    conts = list(ru._extract_workload_containers(d))
    names = [c["name"] for c in conts]
    assert names == ["app", "side"]


def test_workload_missing_requests_uses_template_containers():
    d = _workload("Deployment", "web", containers=[_cont("app"), _cont("side")])
    with _patch(deploys=[d]):
        out = ru.analyze_resource_usage("default", kind="Deployment",
                                        mode="missing_requests")
    assert "app" in out
    assert "side" in out
    assert "❌" in out


# ---------- modes -----------------------------------------------------------


def test_mode_missing_requests_lists_only_containers_without_requests():
    """Containers with empty requests bucket should be flagged; others ignored."""
    p = _pod("p", owner_kind="", containers=[
        _cont("good", requests={"cpu": "100m"}),
        _cont("bad", requests={}),
    ])
    with _patch(pods=[p]):
        out = ru.analyze_resource_usage("default", mode="missing_requests")
    assert "bad" in out
    assert "good" not in out


def test_mode_missing_limits_only_shows_unbounded():
    p = _pod("p", owner_kind="", containers=[
        _cont("with-lim", requests={"cpu": "100m"},
              limits={"cpu": "200m"}),
        _cont("no-lim", requests={"cpu": "100m"}),
    ])
    with _patch(pods=[p]):
        out = ru.analyze_resource_usage("default", mode="missing_limits")
    assert "no-lim" in out
    assert "with-lim" not in out


def test_mode_inconsistent_flags_limits_below_requests():
    """CPU limits < requests is allowed by scheduler (it bumps to requests)
    but it is almost always a misconfiguration."""
    p = _pod("p", owner_kind="", containers=[
        _cont("overspec", requests={"cpu": "500m"}, limits={"cpu": "100m"}),
        _cont("balanced", requests={"cpu": "100m"}, limits={"cpu": "200m"}),
    ])
    with _patch(pods=[p]):
        out = ru.analyze_resource_usage("default", mode="inconsistent")
    assert "overspec" in out
    assert "balanced" not in out


def test_kind_default_is_pod():
    p = _pod("p", owner_kind="", containers=[_cont("app")])
    with _patch(pods=[p]):
        out = ru.analyze_resource_usage("default")
    assert "Pod" in out


def test_kind_deployment_switches_to_workload_view():
    d = _workload("Deployment", "web", containers=[_cont("app")])
    with _patch(deploys=[d]):
        out = ru.analyze_resource_usage("default", kind="Deployment")
    assert "Deployment" in out
    assert "web" in out


def test_pod_view_skips_owned_pods_to_avoid_duplicates():
    """Workload-owned pods are already covered by their Deployment; flag the
    orphan pods separately so the report doesn't drown in duplicates."""
    owned = _pod("p-owned", containers=[_cont("a")],
                 owner_kind="Deployment")
    orphan = _pod("p-orphan", containers=[_cont("b")], owner_kind="")
    with _patch(pods=[owned, orphan]):
        out = ru.analyze_resource_usage("default", kind="Pod")
    assert "p-orphan" in out
    assert "p-owned" not in out
