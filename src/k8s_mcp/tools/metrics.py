"""Resource usage metrics (kubectl top equivalent).

中文说明：
- `top_pods` / `top_nodes`：要求集群装了 metrics-server，否则 K8s API
  会返回 503；错误会原样回传，便于 Agent 排查。
- `sort_by=memory|cpu`：排序字段；CPU 单位是核（如 250m），内存是字节。
"""
from __future__ import annotations

import logging

from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..formatters import short_table

logger = logging.getLogger(__name__)


def _custom_objects_api():
    from kubernetes import client
    return client.CustomObjectsApi(get_api_client())


def top_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    sort_by: str = "memory",
) -> str:
    """Show current CPU and memory usage for Pods (kubectl top pods).

    Args:
        namespace: namespace to query; None = all namespaces.
        label_selector: e.g. "app=nginx".
        sort_by: "cpu" or "memory" (default "memory", top consumers first).

    Returns a NAME / CPU(cores) / MEMORY(bytes) table.

    Requires metrics-server to be installed in the cluster; otherwise returns
    an explanatory error.
    """
    api = _custom_objects_api()
    kwargs = {}
    if label_selector:
        kwargs["label_selector"] = label_selector

    try:
        if namespace:
            items = api.list_namespaced_custom_object(
                "metrics.k8s.io", "v1beta1", namespace, "pods", **kwargs
            )["items"]
        else:
            items = api.list_cluster_custom_object(
                "metrics.k8s.io", "v1beta1", "pods", **kwargs
            )["items"]
    except ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                "metrics-server is not installed in the cluster. "
                "kubectl top requires metrics-server. Install it with: "
                "kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml"
            ) from e
        raise

    rows = []
    for it in items:
        containers = it.get("containers", [])
        cpu = sum(_parse_cpu(c["usage"]["cpu"]) for c in containers)
        mem = sum(_parse_mem(c["usage"]["memory"]) for c in containers)
        rows.append({
            "NAME": it["metadata"]["name"],
            "NAMESPACE": it["metadata"].get("namespace", ""),
            "CPU": _fmt_cpu(cpu),
            "MEMORY": _fmt_mem(mem),
        })

    if not rows:
        return "(no metrics — no pods matched or metrics-server is silent)"
    rows.sort(key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory"))
    return short_table(rows, ["NAME", "NAMESPACE", "CPU", "MEMORY"])


def top_nodes(sort_by: str = "memory") -> str:
    """Show current CPU and memory usage for Nodes (kubectl top nodes).

    Args:
        sort_by: "cpu" or "memory" (default "memory").

    Returns a NAME / CPU(cores) / MEMORY(bytes) table. Requires metrics-server.
    """
    api = _custom_objects_api()
    try:
        items = api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", "nodes")["items"]
    except ApiException as e:
        if e.status == 404:
            raise RuntimeError("metrics-server is not installed in the cluster.") from e
        raise

    rows = []
    for it in items:
        u = it["usage"]
        rows.append({
            "NAME": it["metadata"]["name"],
            "CPU": _fmt_cpu(_parse_cpu(u["cpu"])),
            "MEMORY": _fmt_mem(_parse_mem(u["memory"])),
        })

    rows.sort(key=lambda r: _parse_mem(r["MEMORY"]), reverse=(sort_by == "memory"))
    return short_table(rows, ["NAME", "CPU", "MEMORY"])


def _parse_cpu(v: str) -> float:
    """Convert Kubernetes CPU quantity to cores (float)."""
    if v.endswith("n"):
        return float(v[:-1]) / 1_000_000_000
    if v.endswith("u"):
        return float(v[:-1]) / 1_000_000
    if v.endswith("m"):
        return float(v[:-1]) / 1_000
    return float(v)


def _parse_mem(v: str) -> int:
    """Convert Kubernetes memory quantity to bytes (int)."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
              "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}
    if v.endswith(("Ei", "Pi", "Ti", "Gi", "Mi", "Ki")):
        num, unit = v[:-2], v[-2:]
        return int(float(num) * units[unit])
    if v.endswith(("E", "P", "T", "G", "M", "K")):
        num, unit = v[:-1], v[-1:]
        return int(float(num) * units[unit])
    return int(float(v))


def _fmt_cpu(cores: float) -> str:
    if cores < 0.001:
        return "0"
    if cores < 1:
        return f"{int(cores * 1000)}m"
    return f"{cores:.2f}"


def _fmt_mem(bytes_: int) -> str:
    if bytes_ < 1024:
        return f"{bytes_}B"
    if bytes_ < 1024**2:
        return f"{bytes_ // 1024}Ki"
    if bytes_ < 1024**3:
        return f"{bytes_ // 1024**2}Mi"
    return f"{bytes_ / 1024**3:.1f}Gi"


def register(mcp) -> None:
    mcp.tool()(top_pods)
    mcp.tool()(top_nodes)
