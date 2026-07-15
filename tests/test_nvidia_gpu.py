from __future__ import annotations

from types import SimpleNamespace

import pytest
from kubernetes.client.exceptions import ApiException

from k8s_mcp.tools import nvidia_gpu as gpu


def _container(limits: dict[str, str] | None = None):
    return SimpleNamespace(resources=SimpleNamespace(limits=limits or {}))


def _pod(
    name: str,
    *,
    namespace: str = "ml",
    phase: str = "Running",
    node: str | None = "gpu-1",
    limits: dict[str, str] | None = None,
    conditions: list[object] | None = None,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace, labels={"app": "trainer"}),
        spec=SimpleNamespace(node_name=node, containers=[_container(limits)], init_containers=[]),
        status=SimpleNamespace(phase=phase, conditions=conditions or []),
    )


def _node(
    name: str,
    *,
    capacity: dict[str, str] | None = None,
    allocatable: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    ready: str = "True",
    unschedulable: bool = False,
):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels or {}),
        spec=SimpleNamespace(unschedulable=unschedulable, taints=[]),
        status=SimpleNamespace(
            capacity=capacity or {},
            allocatable=allocatable or {},
            conditions=[SimpleNamespace(type="Ready", status=ready)],
        ),
    )


class _Core:
    def __init__(self, *, nodes=None, pods=None, namespaced_pods=None, read_node=None, read_pod=None, operator_pods=None):
        self.nodes = nodes or []
        self.pods = pods or []
        self.namespaced_pods = namespaced_pods if namespaced_pods is not None else self.pods
        self.read_node_result = read_node
        self.read_pod_result = read_pod
        self.operator_pods = operator_pods
        self.all_pod_calls: list[dict] = []

    def list_node(self):
        return SimpleNamespace(items=self.nodes)

    def list_pod_for_all_namespaces(self, **kwargs):
        self.all_pod_calls.append(kwargs)
        pods = self.pods
        if kwargs.get("field_selector") == "status.phase=Pending":
            pods = [pod for pod in pods if pod.status.phase == "Pending"]
        if kwargs.get("field_selector", "").startswith("spec.nodeName="):
            target = kwargs["field_selector"].split("=", 1)[1]
            pods = [pod for pod in pods if pod.spec.node_name == target]
        return SimpleNamespace(items=pods)

    def list_namespaced_pod(self, namespace, **kwargs):
        if self.operator_pods is not None and namespace == "gpu-operator":
            return SimpleNamespace(items=self.operator_pods)
        pods = [pod for pod in self.namespaced_pods if pod.metadata.namespace == namespace]
        if kwargs.get("field_selector") == "status.phase=Pending":
            pods = [pod for pod in pods if pod.status.phase == "Pending"]
        return SimpleNamespace(items=pods)

    def read_node(self, name):
        if self.read_node_result is None:
            raise ApiException(status=404, reason="Not Found")
        return self.read_node_result

    def read_namespaced_pod(self, name, namespace):
        if self.read_pod_result is None:
            raise ApiException(status=404, reason="Not Found")
        return self.read_pod_result


class _CustomObjects:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"items": []}
        self.error = error

    def list_cluster_custom_object(self, **kwargs):
        if self.error:
            raise self.error
        return self.result


def test_pod_gpu_limits_uses_app_sum_and_init_max():
    pod = SimpleNamespace(
        spec=SimpleNamespace(
            containers=[_container({"nvidia.com/gpu": "1"}), _container({"nvidia.com/gpu": "2"})],
            init_containers=[_container({"nvidia.com/gpu": "4"}), _container({"nvidia.com/mig-1g.10gb": "1"})],
        )
    )

    assert gpu._pod_gpu_limits(pod) == {"nvidia.com/gpu": "4", "nvidia.com/mig-1g.10gb": "1"}


def test_gpu_cluster_overview_discovers_mig_resources_and_demand(monkeypatch):
    nodes = [
        _node(
            "gpu-1",
            capacity={"nvidia.com/gpu": "4", "nvidia.com/mig-1g.10gb": "7"},
            allocatable={"nvidia.com/gpu": "4", "nvidia.com/mig-1g.10gb": "7"},
            labels={"nvidia.com/gpu.product": "NVIDIA-A100"},
        ),
        _node("cpu-1"),
    ]
    pods = [_pod("running", limits={"nvidia.com/gpu": "2"}), _pod("done", phase="Succeeded", limits={"nvidia.com/gpu": "1"})]
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(nodes=nodes, pods=pods))
    monkeypatch.setattr(gpu, "_custom_objects", lambda: _CustomObjects({"items": [{"metadata": {"name": "cluster-policy"}, "status": {"state": "ready"}}]}))

    report = gpu.gpu_cluster_overview()

    assert "GPU nodes discovered: 1 / 2" in report
    assert "nvidia.com/mig-1g.10gb=7" in report
    assert "Requested GPU limits: nvidia.com/gpu=2" in report
    assert "ClusterPolicy: cluster-policy=ready" in report


def test_gpu_node_inspect_shows_labels_taints_and_gpu_pods(monkeypatch):
    node = _node(
        "gpu-1",
        capacity={"nvidia.com/gpu": "1"},
        allocatable={"nvidia.com/gpu": "1"},
        labels={"nvidia.com/gpu.product": "NVIDIA-L4", "topology.kubernetes.io/zone": "a"},
    )
    node.spec.taints = [SimpleNamespace(key="nvidia.com/gpu", value="true", effect="NoSchedule")]
    pod = _pod("inference", limits={"nvidia.com/gpu": "1"})
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(read_node=node, pods=[pod]))

    report = gpu.gpu_node_inspect("gpu-1")

    assert "nvidia.com/gpu.product" in report
    assert "nvidia.com/gpu=true:NoSchedule" in report
    assert "inference" in report
    assert "nvidia.com/gpu=1" in report


def test_gpu_workload_inspect_pod_includes_scheduler_verdict(monkeypatch):
    condition = SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="0/1 nodes available: insufficient nvidia.com/gpu")
    pod = _pod("trainer-0", phase="Pending", node=None, limits={"nvidia.com/gpu": "1"}, conditions=[condition])
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(read_pod=pod))

    report = gpu.gpu_workload_inspect("trainer-0", "ml", "Pod")

    assert "Scheduler verdict: Unschedulable" in report
    assert "insufficient nvidia.com/gpu" in report


def test_gpu_workload_inspect_deployment_lists_matching_gpu_pods(monkeypatch):
    deployment = SimpleNamespace(
        spec=SimpleNamespace(
            selector=SimpleNamespace(match_labels={"app": "trainer"}),
            template=SimpleNamespace(spec=SimpleNamespace(containers=[_container({"nvidia.com/gpu": "2"})], init_containers=[])),
        )
    )
    pod = _pod("trainer-abc", limits={"nvidia.com/gpu": "2"})
    apps = SimpleNamespace(read_namespaced_deployment=lambda name, namespace: deployment)
    monkeypatch.setattr(gpu, "_apps_v1", lambda: apps)
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(namespaced_pods=[pod]))

    report = gpu.gpu_workload_inspect("trainer", "ml", "Deployment")

    assert "Template GPU limits: nvidia.com/gpu=2" in report
    assert "trainer-abc" in report


def test_gpu_pending_workloads_filters_to_gpu_pods_and_validates_limit(monkeypatch):
    condition = SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="Insufficient nvidia.com/mig-1g.10gb")
    gpu_pending = _pod("gpu-waiting", phase="Pending", node=None, limits={"nvidia.com/mig-1g.10gb": "1"}, conditions=[condition])
    cpu_pending = _pod("cpu-waiting", phase="Pending", node=None, limits={"cpu": "1"})
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(pods=[gpu_pending, cpu_pending]))

    report = gpu.gpu_pending_workloads()

    assert "Found: 1; shown: 1" in report
    assert "gpu-waiting" in report
    assert "cpu-waiting" not in report
    with pytest.raises(ValueError, match="limit must be"):
        gpu.gpu_pending_workloads(limit=0)


def test_gpu_diagnose_reports_operator_and_pending_gpu_workloads(monkeypatch):
    gpu_node = _node("gpu-1", capacity={"nvidia.com/gpu": "1"}, allocatable={"nvidia.com/gpu": "1"})
    pending = _pod(
        "waiting",
        phase="Pending",
        node=None,
        limits={"nvidia.com/gpu": "1"},
        conditions=[SimpleNamespace(type="PodScheduled", status="False", reason="Unschedulable", message="Insufficient nvidia.com/gpu")],
    )
    operator = _pod("nvidia-device-plugin-daemonset-abc", namespace="gpu-operator", limits={})
    monkeypatch.setattr(gpu, "_core_v1", lambda: _Core(nodes=[gpu_node], pods=[pending], operator_pods=[operator]))
    monkeypatch.setattr(gpu, "_custom_objects", lambda: _CustomObjects(error=ApiException(status=404, reason="Not Found")))

    report = gpu.gpu_diagnose()

    assert "ClusterPolicy: not available" in report
    assert "1 Pending GPU Pod(s)" in report
    assert "device-plugin" in report


def test_gpu_diagnose_handles_missing_operator_namespace(monkeypatch):
    class MissingOperatorCore(_Core):
        def list_namespaced_pod(self, namespace, **kwargs):
            raise ApiException(status=404, reason="Not Found")

    monkeypatch.setattr(gpu, "_core_v1", lambda: MissingOperatorCore(nodes=[]))
    monkeypatch.setattr(gpu, "_custom_objects", lambda: _CustomObjects())

    report = gpu.gpu_diagnose()

    assert "No NVIDIA GPU Nodes were discovered" in report
    assert "namespace 'gpu-operator' not found" in report
