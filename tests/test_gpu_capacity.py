"""gpu_capacity_analyze / gpu_idle_resources: K8s reservations × Prometheus windows.

Fakes stand in for the Kubernetes core client (via gpu_capacity's imported
helpers), the Prometheus instant query, and the settings singleton so no
cluster or Prometheus is contacted.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from k8s_mcp.tools import gpu_capacity as cap


def _node(name, allocatable):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels={"nvidia.com/gpu.product": "NVIDIA-L4"}),
        spec=SimpleNamespace(unschedulable=False, taints=[]),
        status=SimpleNamespace(
            capacity={},
            allocatable=allocatable,
            conditions=[SimpleNamespace(type="Ready", status="True")],
        ),
    )


def _pod(name, *, namespace="ml", phase="Running", node="gpu-1", limits=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, labels={}),
        spec=SimpleNamespace(node_name=node,
                             containers=[SimpleNamespace(resources=SimpleNamespace(limits=limits or {}))],
                             init_containers=[]),
        status=SimpleNamespace(phase=phase, conditions=[]),
    )


class _Core:
    def __init__(self, *, nodes=None, pods=None):
        self.nodes = nodes or []
        self.pods = pods or []

    def list_node(self):
        return SimpleNamespace(items=self.nodes)

    def list_pod_for_all_namespaces(self, **kwargs):
        pods = self.pods
        if kwargs.get("field_selector") == "status.phase=Pending":
            pods = [pod for pod in pods if pod.status.phase == "Pending"]
        return SimpleNamespace(items=pods)


def _series(hostname: str, gpu: str, value: str):
    return {"metric": {"Hostname": hostname, "GPU": gpu, "UUID": f"uuid-{gpu}"},
            "value": [1756000000, value]}


class _Query:
    """Returns per-series AVG then MAX; values track the GPU index."""

    def __init__(self, avgs, maxs=None, error=None):
        self.avgs = avgs
        self.maxs = maxs if maxs is not None else avgs
        self.error = error
        self.promqls: list[str] = []

    def __call__(self, promql, base_url=None):
        if self.error:
            raise self.error
        self.promqls.append(promql)
        if promql.startswith("avg_over_time"):
            return self.avgs
        return self.maxs


def _patch(monkeypatch, *, nodes, pods, query):
    core = _Core(nodes=nodes, pods=pods)
    monkeypatch.setattr(cap, "_core_v1", lambda: core)
    monkeypatch.setattr(cap, "_query_instant", query)


def test_capacity_analyze_matches_reservations_to_utilization(monkeypatch):
    nodes = [
        _node("gpu-1", {"nvidia.com/gpu": "4"}),
        _node("gpu-2", {"nvidia.com/gpu": "2"}),
    ]
    pods = [
        _pod("trainer", node="gpu-1", limits={"nvidia.com/gpu": "4"}),   # fully held, idle below
        _pod("infer", node="gpu-2", limits={"nvidia.com/gpu": "1"}),
        _pod("queued", phase="Pending", node=None, limits={"nvidia.com/gpu": "1"}),
    ]
    avgs = [_series("gpu-1", "0", "2"), _series("gpu-1", "1", "4"),          # idle node
            _series("gpu-2", "0", "85")]
    query = _Query(avgs, maxs=[_series("gpu-1", "0", "30"), _series("gpu-1", "1", "35"),
                               _series("gpu-2", "0", "99")])
    _patch(monkeypatch, nodes=nodes, pods=pods, query=query)

    report = cap.gpu_capacity_analyze(duration="2h")

    assert "avg_over_time(DCGM_FI_DEV_GPU_UTIL[2h])" in query.promqls[0]
    assert "gpu-1" in report and "gpu-2" in report
    assert "reserved-but-idle candidate" in report
    assert "1 Pending GPU Pod(s) while 1 GPU unit(s) sit unheld" in report


def test_capacity_analyze_prometheus_unavailable_hint(monkeypatch):
    _patch(monkeypatch, nodes=[_node("gpu-1", {"nvidia.com/gpu": "1"})], pods=[],
           query=_Query(None, error=LookupError("no Prometheus")))
    report = cap.gpu_capacity_analyze()
    assert "Prometheus metric query unavailable" in report


def test_capacity_analyze_reports_unmatched_series(monkeypatch):
    nodes = [_node("gpu-1", {"nvidia.com/gpu": "1"})]
    avgs = [_series("gpu-1", "0", "50"), {"metric": {"instance": "10.0.0.9:9400"}, "value": [1, "7"]}]
    _patch(monkeypatch, nodes=nodes, pods=[], query=_Query(avgs))

    report = cap.gpu_capacity_analyze()

    assert "could not be matched to a Kubernetes Node" in report
    assert "(unmatched)" not in report  # capacity table is per node; unmatched bucket is a finding


def test_idle_resources_flags_idle_and_reserved(monkeypatch):
    nodes = [
        _node("gpu-1", {"nvidia.com/gpu": "2"}),
        _node("gpu-2", {"nvidia.com/gpu": "1"}),
    ]
    pods = [_pod("holder", node="gpu-1", limits={"nvidia.com/gpu": "2"})]
    avgs = [
        _series("gpu-1", "0", "1.5"),   # idle, node holds units -> WARN
        _series("gpu-1", "1", "3"),     # idle
        _series("gpu-2", "0", "92"),    # active
    ]
    _patch(monkeypatch, nodes=nodes, pods=pods, query=_Query(avgs))

    report = cap.gpu_idle_resources(duration="24h", threshold=10)

    assert "avg < 10 over 24h" in report
    assert "VERDICT" in report
    # rows sorted quietest first: the two idle GPUs precede the active one
    assert report.index("idle") < report.index("active")
    assert "Idle rollup" in report
    assert "hold Pod GPU units while" in report


def test_idle_resources_all_active(monkeypatch):
    _patch(monkeypatch, nodes=[_node("gpu-1", {"nvidia.com/gpu": "1"})], pods=[],
           query=_Query([_series("gpu-1", "0", "88")]))

    report = cap.gpu_idle_resources()

    assert "No GPU series averaged below 10" in report


def test_idle_resources_validates_inputs(monkeypatch):
    _patch(monkeypatch, nodes=[], pods=[], query=_Query([]))
    with pytest.raises(ValueError, match="threshold"):
        cap.gpu_idle_resources(threshold=0)
    with pytest.raises(ValueError, match="threshold"):
        cap.gpu_idle_resources(threshold=101)
    with pytest.raises(ValueError, match="duration"):
        cap.gpu_idle_resources(duration="8d")
    with pytest.raises(ValueError, match="metric_name"):
        cap.gpu_idle_resources(metric_name="bad name!")
    with pytest.raises(ValueError, match="limit"):
        cap.gpu_idle_resources(limit=500)


def test_capacity_analyze_validates_inputs(monkeypatch):
    _patch(monkeypatch, nodes=[], pods=[], query=_Query([]))
    with pytest.raises(ValueError, match="idle_threshold"):
        cap.gpu_capacity_analyze(idle_threshold=200)
    with pytest.raises(ValueError, match="duration"):
        cap.gpu_capacity_analyze(duration="0h")
    with pytest.raises(ValueError, match="limit"):
        cap.gpu_capacity_analyze(limit=0)
