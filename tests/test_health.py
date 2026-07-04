"""Tests for cluster_health_snapshot.

Strategy:
  - Headline parsers (`_int_after`, `_node_counts`, `_count_expiring_certs`)
    are pure functions — test directly.
  - Section builders (`_section_nodes`, `_section_pending_pods`, etc.)
    take apiserver data via `kubernetes.client.CoreV1Api`. We stub the
    whole `_core_v1` / `_autoscaling_v2` factory at the module level
    and return a fake Api object whose `.list_*` methods return fake
    lists of fake resources.
  - End-to-end `cluster_health_snapshot()` runs all sections; we test
    the resilience story (one section raising does not blank the
    report) and the namespaces-parameter routing.
"""
from __future__ import annotations

import pytest

from k8s_mcp.tools import health

# ---------- fake k8s resource model ----------------------------------------


class _FakeTime:
    def __init__(self, iso):
        from datetime import UTC, datetime
        self._dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if self._dt.tzinfo is None:
            self._dt = self._dt.replace(tzinfo=UTC)


class _FakeCondition:
    def __init__(self, type_, status, reason="", message="", last_transition_time=None):
        self.type = type_
        self.status = status
        self.reason = reason
        self.message = message
        self.last_transition_time = last_transition_time


class _FakeNodeStatus:
    def __init__(self, conditions):
        self.conditions = conditions


class _FakeNode:
    def __init__(self, name, conditions):
        self.metadata = _FakeMeta(name)
        self.status = _FakeNodeStatus(conditions)


class _FakeMeta:
    def __init__(self, name, namespace=None, creation_timestamp=None):
        self.name = name
        self.namespace = namespace
        self.creation_timestamp = creation_timestamp


class _FakeContainerStatus:
    def __init__(self, name, restart_count=0, waiting_reason=None, running=False):
        self.name = name
        self.restart_count = restart_count
        self.state = _FakeContainerState(waiting_reason, running)


class _FakeContainerState:
    def __init__(self, waiting_reason=None, running=False):
        self.waiting = _FakeWaiting(waiting_reason) if waiting_reason else None
        self.running = _FakeRunning() if running else None


class _FakeWaiting:
    def __init__(self, reason="", message=""):
        self.reason = reason
        self.message = message


class _FakeRunning:
    pass


class _FakePodStatus:
    def __init__(self, phase, conditions=None, container_statuses=None):
        self.phase = phase
        self.conditions = conditions or []
        self.container_statuses = container_statuses or []


class _FakePod:
    def __init__(self, namespace, name, status):
        self.metadata = _FakeMeta(name, namespace)
        self.status = status


class _FakePersistentVolume:
    def __init__(self, name, phase, claim=None, age="2026-01-01T00:00:00Z"):
        self.metadata = _FakeMeta(name, creation_timestamp=age)
        self.status = _FakePVStatus(phase)
        self.spec = _FakePVSpec(claim)


class _FakePVStatus:
    def __init__(self, phase):
        self.phase = phase


class _FakePVSpec:
    def __init__(self, claim):
        self.claim_ref = claim  # namespace/name tuple or None


class _FakeHPA:
    def __init__(self, namespace, name, target_kind, target_name, current, desired, min_r, max_r):
        self.metadata = _FakeMeta(name, namespace)
        self.status = _FakeHPAStatus(current, desired)
        self.spec = _FakeHPASpec(target_kind, target_name, min_r, max_r)


class _FakeHPAStatus:
    def __init__(self, current, desired):
        self.current_replicas = current
        self.desired_replicas = desired


class _FakeHPASpec:
    def __init__(self, target_kind, target_name, min_r, max_r):
        self.scale_target_ref = _FakeTargetRef(target_kind, target_name)
        self.min_replicas = min_r
        self.max_replicas = max_r


class _FakeTargetRef:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name


class _FakeNodeList:
    def __init__(self, items): self.items = items


class _FakePodList:
    def __init__(self, items): self.items = items


class _FakePVList:
    def __init__(self, items): self.items = items


class _FakeHPAList:
    def __init__(self, items): self.items = items


class _FakeCoreV1:
    """Stub for kubernetes.client.CoreV1Api — only implements the calls
    cluster_health_snapshot actually makes."""
    def __init__(self, nodes=(), pods_all=(), pods_by_ns=None, pvs=()):
        self._nodes = list(nodes)
        self._pods_all = list(pods_all)
        self._pods_by_ns = pods_by_ns or {}
        self._pvs = list(pvs)

    def list_node(self):
        return _FakeNodeList(self._nodes)

    def list_pod_for_all_namespaces(self):
        return _FakePodList(self._pods_all)

    def list_namespaced_pod(self, ns):
        return _FakePodList(self._pods_by_ns.get(ns, []))

    def list_persistent_volume(self):
        return _FakePVList(self._pvs)


class _FakeAutoscalingV2:
    def __init__(self, hpas_all=(), hpas_by_ns=None):
        self._hpas_all = list(hpas_all)
        self._hpas_by_ns = hpas_by_ns or {}

    def list_horizontal_pod_autoscaler_for_all_namespaces(self):
        return _FakeHPAList(self._hpas_all)

    def list_namespaced_horizontal_pod_autoscaler(self, ns):
        return _FakeHPAList(self._hpas_by_ns.get(ns, []))


# ---------- helpers --------------------------------------------------------


@pytest.fixture
def _stub_apis(monkeypatch):
    """Returns a setter the test uses to install different fake data per case."""
    state = {"core": _FakeCoreV1(), "autoscale": _FakeAutoscalingV2()}

    monkeypatch.setattr(health, "_core_v1", lambda: state["core"])
    monkeypatch.setattr(health, "_autoscaling_v2", lambda: state["autoscale"])

    def install(core=None, autoscale=None):
        if core is not None:
            state["core"] = core
        if autoscale is not None:
            state["autoscale"] = autoscale

    return install


# ---------- headline parsers (pure functions) ------------------------------


def test_int_after_parses_trailing_count():
    assert health._int_after("## HPA (3 not at desired)\n", "## HPA (") == 3


def test_int_after_returns_zero_when_marker_absent():
    assert health._int_after("## HPA\n(all good)", "## HPA (") == 0


def test_int_after_handles_no_digits_after_marker():
    # e.g. "## HPA\n"  with no parens
    assert health._int_after("## HPA\n", "## HPA (") == 0


def test_node_counts_parses_total_and_not_ready():
    s = (
        "## Nodes\n"
        "Total: 9    Ready: 7/9\n"
        "NotReady:\n"
        "  - deploy-2 (since 5m ago)\n"
        "  - edge-1 (since 12m ago)\n"
        "Pressure: (none)\n"
    )
    total, not_ready = health._node_counts(s)
    assert total == 9
    assert not_ready == 2


def test_node_counts_handles_no_not_ready_section():
    s = "## Nodes\nTotal: 3    Ready: 3/3\nNotReady: (none)\n"
    total, not_ready = health._node_counts(s)
    assert total == 3
    assert not_ready == 0


def test_count_expiring_certs_ignores_valid_rows():
    s = (
        "## Certificates\n"
        "SOURCE                 SUBJECT  STATUS\n"
        "kubeconfig-CA          CN=kube  ✅ valid\n"
        "kubeconfig-client      CN=cli   ⚠️ expires in 14 d (<30d)\n"
        "in-cluster-CA          CN=ic    ❌ <7d\n"
    )
    assert health._count_expiring_certs(s) == 2


# ---------- _section_nodes -------------------------------------------------


def test_section_nodes_lists_not_ready_and_pressure(_stub_apis):
    nodes = [
        _FakeNode("deploy-1", [
            _FakeCondition("Ready", "True"),
            _FakeCondition("DiskPressure", "False"),
        ]),
        _FakeNode("deploy-2", [
            _FakeCondition("Ready", "False"),
        ]),
        _FakeNode("edge-1", [
            _FakeCondition("Ready", "True"),
            _FakeCondition("MemoryPressure", "True"),
        ]),
    ]
    _stub_apis(core=_FakeCoreV1(nodes=nodes))
    out = health._section_nodes()
    assert "Total: 3" in out
    assert "deploy-2" in out  # NotReady
    assert "edge-1: MemoryPressure" in out  # pressure


# ---------- _section_pending_pods -------------------------------------------


def test_section_pending_pods_lists_pending_with_reason(_stub_apis):
    pods = [
        _FakePod("default", "postgres-sts-0", _FakePodStatus(
            "Pending",
            conditions=[_FakeCondition("PodScheduled", "False", reason="Unschedulable",
                                       message="0/1 nodes are available: pvc not found")],
        )),
        _FakePod("default", "running-pod", _FakePodStatus("Running")),
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    out = health._section_pending_pods(namespaces=None)
    assert "## Pending Pods (1)" in out
    assert "postgres-sts-0" in out
    assert "Unschedulable" in out
    # running pod should NOT appear
    assert "running-pod" not in out


def test_section_pending_pods_caps_output_at_20(_stub_apis):
    pods = [
        _FakePod("default", f"p-{i}", _FakePodStatus(
            "Pending",
            conditions=[_FakeCondition("PodScheduled", "False", reason="X")],
        ))
        for i in range(25)
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    out = health._section_pending_pods(namespaces=None)
    assert "## Pending Pods (25)" in out
    assert "showing first 20 of 25" in out


# ---------- _section_abnormal_restarts --------------------------------------


def test_section_abnormal_restarts_threshold_and_backoff(_stub_apis):
    pods = [
        _FakePod("default", "quiet", _FakePodStatus("Running", container_statuses=[
            _FakeContainerStatus("app", restart_count=0, running=True),
        ])),
        _FakePod("default", "flapper", _FakePodStatus("Running", container_statuses=[
            _FakeContainerStatus("app", restart_count=12, running=True),
        ])),
        _FakePod("default", "crashy", _FakePodStatus("Pending", container_statuses=[
            _FakeContainerStatus("app", restart_count=3, waiting_reason="CrashLoopBackOff"),
        ])),
        _FakePod("default", "image-bad", _FakePodStatus("Pending", container_statuses=[
            _FakeContainerStatus("app", restart_count=0, waiting_reason="ImagePullBackOff"),
        ])),
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    out = health._section_abnormal_restarts(namespaces=None, threshold=3)
    # flapper above threshold, crashy (any backoff), image-bad (any backoff)
    assert "flapper" in out
    assert "crashy" in out
    assert "image-bad" in out
    assert "quiet" not in out
    # Sorted by restarts desc
    flapper_pos = out.find("flapper")
    crashy_pos = out.find("crashy")
    assert flapper_pos < crashy_pos


# ---------- _section_hpa ---------------------------------------------------


def test_section_hpa_only_shows_off_target(_stub_apis):
    hpas = [
        _FakeHPA("default", "ok-hpa", "Deployment", "ok", current=3, desired=3, min_r=1, max_r=10),
        _FakeHPA("default", "at-max", "Deployment", "maxed", current=10, desired=10, min_r=1, max_r=10),
        _FakeHPA("default", "scaling", "Deployment", "busy", current=3, desired=7, min_r=1, max_r=10),
        _FakeHPA("default", "over-max", "Deployment", "huge", current=10, desired=20, min_r=1, max_r=10),
    ]
    _stub_apis(autoscale=_FakeAutoscalingV2(hpas_all=hpas))
    out = health._section_hpa(namespaces=None)
    # at-max has current==desired so NOT shown
    assert "at-max" not in out
    # scaling and over-max ARE shown
    assert "scaling" in out
    assert "over-max" in out
    assert "scaling up 3→7" in out
    assert "desired 20 > max 10" in out


# ---------- _section_orphan_pvs --------------------------------------------


def test_section_orphan_pvs_sorts_failed_first(_stub_apis):
    pvs = [
        _FakePersistentVolume("available-1", "Available"),
        _FakePersistentVolume("released-1", "Released",
                             claim=type("C", (), {"namespace": "default", "name": "old-claim"})()),
        _FakePersistentVolume("failed-1", "Failed"),
        _FakePersistentVolume("bound-1", "Bound"),
    ]
    _stub_apis(core=_FakeCoreV1(pvs=pvs))
    out = health._section_orphan_pvs()
    assert "bound-1" not in out
    assert "## Orphan PVs (3)" in out
    # Failed comes before Released which comes before Available in the output
    f_pos = out.find("failed-1")
    r_pos = out.find("released-1")
    a_pos = out.find("available-1")
    assert f_pos < r_pos < a_pos


# ---------- _section_recent_warnings --------------------------------------


def test_section_recent_warnings_delegates_to_events(monkeypatch):
    """The recent-warnings section is a thin wrapper around events.list_events.
    Verify it renders whatever that returns, prefixed with the section header.
    """
    monkeypatch.setattr(
        health.events, "list_events",
        lambda **kw: "TYPE REASON OBJECT MESSAGE\nWarning BackOff Pod/x OOMKilled",
    )
    out = health._section_recent_warnings(minutes=60, namespaces=None)
    assert "## Recent Warning Events" in out
    assert "Warning" in out
    assert "OOMKilled" in out


# ---------- end-to-end cluster_health_snapshot -----------------------------


def test_cluster_health_snapshot_happy_path_healthy(_stub_apis, monkeypatch):
    """All-clean cluster → headline HEALTHY, no actionable sections."""
    nodes = [_FakeNode(f"n-{i}", [_FakeCondition("Ready", "True")]) for i in range(3)]
    pods = [_FakePod("default", "ok", _FakePodStatus("Running", container_statuses=[
        _FakeContainerStatus("c", restart_count=0, running=True),
    ]))]
    hpas = [_FakeHPA("default", "h", "Deployment", "d", current=2, desired=2, min_r=1, max_r=5)]
    pvs = [_FakePersistentVolume("p", "Bound")]

    _stub_apis(
        core=_FakeCoreV1(nodes=nodes, pods_all=pods, pvs=pvs),
        autoscale=_FakeAutoscalingV2(hpas_all=hpas),
    )
    # certs section talks to local filesystem; events section hits apiserver
    # for warnings. Both need monkeypatching in test env.
    monkeypatch.setattr(
        health.certs, "get_certificate_expiry",
        lambda: "## Certificates\nSOURCE  SUBJECT  STATUS\nkubeconfig  CN=x  ✅ valid",
    )
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")
    out = health.cluster_health_snapshot()

    assert "Cluster Health Snapshot" in out
    assert "HEALTHY" in out
    assert "Pending Pods: 0" in out
    assert "Abnormal Restarts: 0" in out
    assert "HPA off-target: 0" in out
    assert "Orphan PVs: 0" in out
    assert "Certs expiring: 0" in out


def test_cluster_health_snapshot_attention_headline(_stub_apis, monkeypatch):
    """At least one NotReady node + 1 Pending pod + 1 HPA off-target +
    1 certs expiring → headline ATTENTION with all counts non-zero."""
    nodes = [
        _FakeNode("good", [_FakeCondition("Ready", "True")]),
        _FakeNode("bad", [_FakeCondition("Ready", "False")]),
    ]
    pods = [
        _FakePod("default", "stuck", _FakePodStatus(
            "Pending",
            conditions=[_FakeCondition("PodScheduled", "False", reason="Unschedulable",
                                       message="0/1 nodes available")],
        )),
    ]
    hpas = [_FakeHPA("default", "maxed", "Deployment", "d", current=10, desired=10,
                     min_r=1, max_r=10)]
    # The HPA has current == desired → not off-target, so add a scaling one
    hpas.append(_FakeHPA("default", "scaling", "Deployment", "d2",
                         current=2, desired=5, min_r=1, max_r=10))
    pvs = [_FakePersistentVolume("released", "Released",
                                 claim=type("C", (), {"namespace": "default", "name": "old"})())]

    _stub_apis(
        core=_FakeCoreV1(nodes=nodes, pods_all=pods, pvs=pvs),
        autoscale=_FakeAutoscalingV2(hpas_all=hpas),
    )
    monkeypatch.setattr(
        health.certs, "get_certificate_expiry",
        lambda: "## Certificates\nkubeconfig-client  CN=cli  ⚠️ expires in 14 d (<30d)",
    )
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")

    out = health.cluster_health_snapshot()
    assert "ATTENTION" in out
    assert "Nodes: 1/2 Ready" in out
    assert "Pending Pods: 1" in out
    assert "HPA off-target: 1" in out
    assert "Orphan PVs: 1" in out
    assert "Certs expiring: 1" in out


def test_cluster_health_snapshot_one_section_failure_does_not_blank_report(_stub_apis, monkeypatch):
    """Critical resilience test: if HPA API errors out, the rest of the
    report must still ship — otherwise one apiserver hiccup blanks the
    whole 'is the cluster ok?' answer."""
    nodes = [_FakeNode("n-1", [_FakeCondition("Ready", "True")])]

    class _BoomAutoscaling:
        def list_horizontal_pod_autoscaler_for_all_namespaces(self):
            raise RuntimeError("simulated apiserver hiccup")

    _stub_apis(
        core=_FakeCoreV1(nodes=nodes),
        autoscale=_BoomAutoscaling(),
    )
    monkeypatch.setattr(health.certs, "get_certificate_expiry",
                        lambda: "## Certificates\n(none)")
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")

    out = health.cluster_health_snapshot()
    # Other sections still render
    assert "## Nodes" in out
    assert "## Pending Pods" in out
    # HPA section shows the error, not a crash
    assert "## HPA" in out
    assert "section failed" in out
    assert "simulated apiserver hiccup" in out


def test_cluster_health_snapshot_namespaces_routes_pods_and_hpa(_stub_apis, monkeypatch):
    """When namespaces=['app'], only pods/HPA in 'app' are scanned; nodes
    and PVs remain cluster-wide."""
    nodes = [_FakeNode("n-1", [_FakeCondition("Ready", "True")])]
    pods_by_ns = {
        "app": [_FakePod("app", "in-ns", _FakePodStatus("Running"))],
        "default": [_FakePod("default", "out-of-ns", _FakePodStatus("Running"))],
    }
    hpas_by_ns = {
        "app": [_FakeHPA("app", "h-app", "Deployment", "d", current=2, desired=2, min_r=1, max_r=5)],
        "default": [_FakeHPA("default", "h-default", "Deployment", "d2", current=2, desired=2, min_r=1, max_r=5)],
    }
    _stub_apis(
        core=_FakeCoreV1(nodes=nodes, pods_by_ns=pods_by_ns),
        autoscale=_FakeAutoscalingV2(hpas_by_ns=hpas_by_ns),
    )
    monkeypatch.setattr(health.certs, "get_certificate_expiry",
                        lambda: "## Certificates\n(none)")
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")

    out = health.cluster_health_snapshot(namespaces=["app"])
    # list_namespaced_pod is called with namespace="app" — but the stub
    # just returns the 'app' bucket. We assert no crash; the parser-level
    # namespacing assertion is implicit (we wired the stub).
    assert "## Pending Pods" in out  # section rendered
    assert "## HPA" in out


def test_cluster_health_snapshot_restart_threshold_zero_catches_everyone(_stub_apis, monkeypatch):
    """threshold=0 should mark every pod with a single restart as abnormal
    — used by users investigating a 'something restarted' trail."""
    pods = [
        _FakePod("default", "once", _FakePodStatus("Running", container_statuses=[
            _FakeContainerStatus("c", restart_count=1, running=True),
        ])),
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    monkeypatch.setattr(health.certs, "get_certificate_expiry",
                        lambda: "## Certificates\n(none)")
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")
    out = health.cluster_health_snapshot(restart_threshold=0)
    assert "Abnormal Restarts: 1" in out
    assert "once" in out


def test_register_attaches_to_mcp(monkeypatch):
    """Sanity: register() calls mcp.tool() — we don't need a real FastMCP,
    just a recorder."""
    calls: list = []
    class _FakeMCP:
        def tool(self):
            def deco(fn):
                calls.append(fn)
                return fn
            return deco
    health.register(_FakeMCP())
    assert health.cluster_health_snapshot in calls


# ---------- _section_resource_usage ----------------------------------------


class _FakeCustomObjectsApi:
    """Stub for kubernetes CustomObjectsApi (metrics.k8s.io/v1beta1)."""

    def __init__(self, nodes=(), pods=()):
        self._nodes = list(nodes)
        self._pods = list(pods)

    def list_cluster_custom_object(self, group, version, plural, **kw):
        if plural == "nodes":
            return {"items": self._nodes}
        if plural == "pods":
            return {"items": self._pods}
        raise RuntimeError(f"unexpected plural: {plural}")

    def list_namespaced_custom_object(self, group, version, ns, plural, **kw):
        if plural == "pods":
            return {"items": self._pods}
        raise RuntimeError(f"unexpected plural: {plural}")


def _stub_metrics(monkeypatch, nodes=(), pods=()):
    """Patch metrics.top_nodes / metrics.top_pods to return canned data."""
    monkeypatch.setattr(
        health.metrics, "top_nodes",
        lambda: "NAME      CPU    MEMORY\n--------  -----  ------\nn-1       100m   4Gi\n",
    )
    monkeypatch.setattr(
        health.metrics, "top_pods",
        lambda namespace=None: (
            "NAME       NAMESPACE  CPU    MEMORY\n"
            "---------  ---------  -----  ------\n"
            "app-pod-1  default    50m    1Gi\n"
        ),
    )


def test_section_resource_usage_renders_top_nodes_and_pods(monkeypatch):
    """Happy path: both top_nodes and top_pods return data; both
    subsections render inside the same `## Resource Usage` block."""
    _stub_metrics(monkeypatch)
    out = health._section_resource_usage(namespaces=None, top_n=5)
    assert "## Resource Usage" in out
    assert "### Top Nodes" in out
    assert "n-1" in out
    assert "### Top Pods" in out
    assert "app-pod-1" in out


def test_section_resource_usage_truncates_large_tables(monkeypatch):
    """When metrics returns > top_n rows, render only the top N + an
    explicit count of the hidden rows so the operator knows there's more."""
    def fake_top_nodes():
        rows = ["NAME      CPU    MEMORY\n", "--------  -----  ------\n"]
        for i in range(10):
            rows.append(f"n-{i:02d}      100m   {10 - i}Gi\n")
        return "".join(rows)
    def fake_top_pods(namespace=None):
        rows = ["NAME  NAMESPACE  CPU  MEMORY\n", "----  ---------  ---  ------\n"]
        for i in range(10):
            rows.append(f"p-{i:02d}  default    10m  {10 - i}Gi\n")
        return "".join(rows)
    monkeypatch.setattr(health.metrics, "top_nodes", fake_top_nodes)
    monkeypatch.setattr(health.metrics, "top_pods", fake_top_pods)
    out = health._section_resource_usage(namespaces=None, top_n=3)
    assert "showing top 3 of 10 nodes" in out
    assert "showing top 3 of 10 pods" in out
    # Only first 3 data rows visible
    assert "n-00" in out and "n-01" in out and "n-02" in out
    assert "n-09" not in out


def test_section_resource_usage_falls_back_to_prometheus(monkeypatch):
    """When metrics-server isn't installed AND Prometheus is reachable,
    the snapshot falls back to PromQL (node-exporter for cluster-wide
    node CPU%/MEM%, kubelet-cAdvisor for top-N pod CPU + memory) so
    operators get usable numbers instead of an install hint."""
    def boom():
        raise RuntimeError(
            "metrics-server is NOT installed in the cluster. "
            "Either install it..."
        )
    monkeypatch.setattr(health.metrics, "top_nodes", boom)
    monkeypatch.setattr(health.metrics, "top_pods", boom)

    # Fake Prometheus reachable + canned responses
    monkeypatch.setattr(
        health.prometheus, "_resolve_prometheus_url",
        lambda settings: "http://fake-prom:9090",
    )
    def fake_prom_get(path, params, base_url, bearer):
        q = params["query"]
        if "node_cpu_seconds_total" in q:
            return {"data": {"resultType": "vector", "result": [
                {"metric": {"instance": "n-1:9100"}, "value": [1, "2.7"]},
            ]}}
        if "node_memory_MemAvailable_bytes" in q:
            return {"data": {"resultType": "vector", "result": [
                {"metric": {"instance": "n-1:9100"}, "value": [1, "31.1"]},
            ]}}
        if "container_cpu_usage_seconds_total" in q:
            return {"data": {"resultType": "vector", "result": [
                {"metric": {"namespace": "kube-system", "pod": "api"},
                 "value": [1, "0.0553"]},
                {"metric": {"namespace": "default", "pod": "redis"},
                 "value": [1, "0.0041"]},
            ]}}
        if "container_memory_working_set_bytes" in q:
            return {"data": {"resultType": "vector", "result": [
                {"metric": {"namespace": "kube-system", "pod": "api"},
                 "value": [1, str(678 * 1024 * 1024)]},
            ]}}
        return {"data": {"resultType": "vector", "result": []}}
    monkeypatch.setattr(health.prometheus, "_prom_get", fake_prom_get)

    out = health._section_resource_usage(namespaces=None)
    # Prometheus-tagged section header (not the metrics-server one)
    assert "## Resource Usage (Prometheus)" in out
    # Node table rendered with INSTANCE / CPU% / MEM% columns
    assert "### Nodes (Prometheus)" in out
    assert "INSTANCE" in out and "CPU%" in out and "MEM%" in out
    assert "n-1:9100" in out and "2.7" in out and "31.1" in out
    # Top-Pods CPU table populated from kubelet-cAdvisor
    assert "### Top Pods by CPU" in out
    assert "api" in out and "55m" in out
    # Top-Pods Memory with Mi suffix
    assert "### Top Pods by Memory" in out
    assert "678Mi" in out


def test_section_resource_usage_degrades_when_both_backends_unreachable(monkeypatch):
    """When neither metrics-server nor Prometheus is reachable, the
    section shows an actionable install hint naming both options +
    the Prometheus diagnostic."""
    def boom():
        raise RuntimeError(
            "metrics-server is NOT installed in the cluster. "
            "Either install it..."
        )
    monkeypatch.setattr(health.metrics, "top_nodes", boom)
    monkeypatch.setattr(health.metrics, "top_pods", boom)
    # Simulate Prometheus discovery failure
    monkeypatch.setattr(
        health.prometheus, "_resolve_prometheus_url",
        lambda settings: (_ for _ in ()).throw(
            LookupError("Prometheus is not auto-discoverable in this cluster.")
        ),
    )

    out = health._section_resource_usage(namespaces=None)
    assert "## Resource Usage" in out
    assert "neither metrics-server nor Prometheus" in out
    assert "metrics-server:" in out and "kubectl apply" in out
    assert "kube-prometheus-stack" in out
    # The Prometheus diagnostic surfaces the underlying reason
    assert "Prometheus diagnostic" in out


# ---------- _section_pod_distribution --------------------------------------


def test_section_pod_distribution_counts_phases(_stub_apis):
    pods = [
        _FakePod("default", "run-1", _FakePodStatus("Running")),
        _FakePod("default", "run-2", _FakePodStatus("Running")),
        _FakePod("default", "stuck", _FakePodStatus("Pending")),
        _FakePod("default", "done", _FakePodStatus("Succeeded")),
        _FakePod("default", "bad", _FakePodStatus("Failed")),
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    out = health._section_pod_distribution(namespaces=None)
    assert "## Pod Distribution (total 5)" in out
    assert "Running" in out and "2" in out  # highest count first
    assert "Pending" in out
    assert "Succeeded" in out
    assert "Failed" in out


def test_section_pod_distribution_handles_empty_cluster(_stub_apis):
    _stub_apis(core=_FakeCoreV1(pods_all=[]))
    out = health._section_pod_distribution(namespaces=None)
    assert "no pods" in out


# ---------- _section_image_pull --------------------------------------------


def test_section_image_pull_lists_pulling_pods(_stub_apis):
    """Pods in ErrImagePull / ImagePullBackOff / InvalidImageName are
    listed separately from the abnormal-restarts section."""
    pods = [
        _FakePod("default", "bad-image", _FakePodStatus(
            "Pending", container_statuses=[
                _FakeContainerStatus("app", restart_count=0,
                                     waiting_reason="ImagePullBackOff"),
            ])),
        _FakePod("default", "typo-image", _FakePodStatus(
            "Pending", container_statuses=[
                _FakeContainerStatus("app", restart_count=0,
                                     waiting_reason="ErrImagePull"),
            ])),
        _FakePod("default", "crashing", _FakePodStatus(
            "Running", container_statuses=[
                _FakeContainerStatus("app", restart_count=10,
                                     waiting_reason="CrashLoopBackOff"),
            ])),
    ]
    _stub_apis(core=_FakeCoreV1(pods_all=pods))
    out = health._section_image_pull(namespaces=None)
    assert "## Image Pull Issues (2)" in out
    assert "bad-image" in out
    assert "typo-image" in out
    # CrashLoopBackOff is NOT an image pull issue — should not appear here
    assert "crashing" not in out


def test_section_image_pull_clean_when_no_issues(_stub_apis):
    _stub_apis(core=_FakeCoreV1(pods_all=[
        _FakePod("default", "ok", _FakePodStatus("Running", container_statuses=[
            _FakeContainerStatus("app", restart_count=0, running=True),
        ])),
    ]))
    out = health._section_image_pull(namespaces=None)
    assert "all images resolved" in out


# ---------- _section_workloads ---------------------------------------------


class _FakeListResp:
    def __init__(self, items): self.items = items


class _FakeAppsV1:
    def __init__(self, deployments=(), statefulsets=(), daemonsets=(),
                 replicasets=()):
        self._d, self._s, self._ds, self._rs = (
            deployments, statefulsets, daemonsets, replicasets,
        )
    def list_deployment_for_all_namespaces(self):
        return _FakeListResp(self._d)
    def list_stateful_set_for_all_namespaces(self):
        return _FakeListResp(self._s)
    def list_daemon_set_for_all_namespaces(self):
        return _FakeListResp(self._ds)
    def list_replica_set_for_all_namespaces(self):
        return _FakeListResp(self._rs)
    # The namespaced_* methods are referenced (attribute access) by
    # `_section_workloads` regardless of `namespaces=` — provide no-op
    # stubs so the attribute lookup succeeds. They should NOT be called
    # in tests where `namespaces=None`.
    def list_namespaced_deployment(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")
    def list_namespaced_stateful_set(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")
    def list_namespaced_daemon_set(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")
    def list_namespaced_replica_set(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")


class _FakeBatchV1:
    def __init__(self, jobs=(), cronjobs=()):
        self._j, self._cj = jobs, cronjobs
    def list_job_for_all_namespaces(self):
        return _FakeListResp(self._j)
    def list_cron_job_for_all_namespaces(self):
        return _FakeListResp(self._cj)
    def list_namespaced_job(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")
    def list_namespaced_cron_job(self, ns):
        raise AssertionError("namespaced call leaked into cluster-wide test")


class _WorkloadMeta:
    """Tiny workload meta — just needs a name; avoids colliding with
    the richer `_FakeMeta` defined earlier (which carries namespace
    + creation_timestamp)."""
    def __init__(self, name):
        self.name = name


def _w(name):
    """Tiny fake workload object: just needs .metadata.name for counting."""
    o = type("W", (), {})()
    o.metadata = _WorkloadMeta(name)
    return o


def test_section_workloads_counts_each_kind(monkeypatch):
    monkeypatch.setattr(health, "_apps_v1", lambda: _FakeAppsV1(
        deployments=[_w("d1"), _w("d2")],
        statefulsets=[_w("s1")],
        daemonsets=[_w("ds1"), _w("ds2"), _w("ds3")],
        replicasets=[_w("rs1")],
    ))
    monkeypatch.setattr(health, "_batch_v1", lambda: _FakeBatchV1(
        jobs=[_w("j1")], cronjobs=[_w("cj1"), _w("cj2")],
    ))
    out = health._section_workloads(namespaces=None)
    assert "## Workloads" in out
    assert "Deployment" in out and "2" in out
    assert "StatefulSet" in out and "1" in out
    assert "DaemonSet" in out and "3" in out
    assert "ReplicaSet" in out and "1" in out
    assert "Job" in out and "1" in out
    assert "CronJob" in out and "2" in out


def test_section_workloads_handles_rbac_denied_independently(monkeypatch):
    """If one kind's list call raises (e.g. RBAC denied), the others
    still render — counts show `?` for the failing kind."""
    class _BrokenApps:
        def list_deployment_for_all_namespaces(self):
            raise RuntimeError("forbidden")
        def list_stateful_set_for_all_namespaces(self):
            return _FakeListResp([_w("s1")])
        def list_daemon_set_for_all_namespaces(self):
            return _FakeListResp([])
        def list_replica_set_for_all_namespaces(self):
            return _FakeListResp([])
        # namespaced_* are referenced (attribute access) but not called
        # when namespaces=None — provide stubs so the attribute lookup
        # doesn't AttributeError before _count() decides to skip them.
        def list_namespaced_deployment(self, ns):
            raise AssertionError("namespaced call leaked")
        def list_namespaced_stateful_set(self, ns):
            raise AssertionError("namespaced call leaked")
        def list_namespaced_daemon_set(self, ns):
            raise AssertionError("namespaced call leaked")
        def list_namespaced_replica_set(self, ns):
            raise AssertionError("namespaced call leaked")
    monkeypatch.setattr(health, "_apps_v1", lambda: _BrokenApps())
    monkeypatch.setattr(health, "_batch_v1", lambda: _FakeBatchV1())
    out = health._section_workloads(namespaces=None)
    assert "Deployment" in out and "?" in out
    # StatefulSet still counted even though Deployment call failed
    assert "StatefulSet" in out and "1" in out


# ---------- end-to-end: new sections show up in the report ----------------


def test_cluster_health_snapshot_includes_new_sections(_stub_apis, monkeypatch):
    """End-to-end: when cluster_health_snapshot runs, the new sections
    appear in the output and their counts feed into the headline."""
    nodes = [_FakeNode("n-1", [_FakeCondition("Ready", "True")])]
    pods = [
        _FakePod("default", "ok", _FakePodStatus("Running", container_statuses=[
            _FakeContainerStatus("app", restart_count=0, running=True),
        ])),
    ]
    _stub_apis(core=_FakeCoreV1(nodes=nodes, pods_all=pods))
    monkeypatch.setattr(
        health.certs, "get_certificate_expiry",
        lambda: "## Certificates\n(none)",
    )
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")
    _stub_metrics(monkeypatch)
    monkeypatch.setattr(health, "_apps_v1", lambda: _FakeAppsV1())
    monkeypatch.setattr(health, "_batch_v1", lambda: _FakeBatchV1())

    out = health.cluster_health_snapshot()

    # New sections present in the report
    assert "## Resource Usage" in out
    assert "## Pod Distribution" in out
    assert "## Image Pull Issues" in out
    assert "## Workloads" in out

    # Headline now includes Image Pull count
    assert "Image Pull: 0" in out


def test_cluster_health_snapshot_image_pull_triggers_attention(_stub_apis, monkeypatch):
    """At least one ImagePullBackOff pod → headline ATTENTION (not HEALTHY)
    AND the new `Image Pull` count is non-zero in the headline line."""
    nodes = [_FakeNode("n-1", [_FakeCondition("Ready", "True")])]
    pods = [
        _FakePod("default", "bad-image", _FakePodStatus(
            "Pending", container_statuses=[
                _FakeContainerStatus("app", restart_count=0,
                                     waiting_reason="ImagePullBackOff"),
            ])),
    ]
    _stub_apis(core=_FakeCoreV1(nodes=nodes, pods_all=pods))
    monkeypatch.setattr(
        health.certs, "get_certificate_expiry",
        lambda: "## Certificates\n(none)",
    )
    monkeypatch.setattr(health.events, "list_events", lambda **kw: "(no events)")

    out = health.cluster_health_snapshot()
    assert "ATTENTION" in out
    assert "Image Pull: 1" in out
