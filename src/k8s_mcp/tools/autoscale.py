"""Auto-scaling and disruption budgets: create_hpa, create_pdb.

中文说明：
- `create_hpa`：HPA 仅支持 Deployment / StatefulSet / ReplicaSet 之一，
  CPU 与 memory 指标至少要传一个。
- `create_pdb`：min_available 与 max_unavailable 必须二选一（PDB 语义上
  互斥），用 `=` 而非 `>=` 约束副本下限，避免自愿驱逐时被卡住。
"""
from __future__ import annotations

import logging
from typing import Any

import yaml

from . import generic

logger = logging.getLogger(__name__)


def create_hpa(
    name: str,
    target_kind: str,
    target_name: str,
    namespace: str,
    min_replicas: int,
    max_replicas: int,
    cpu_utilization: int | None = None,
    memory_average_value: str | None = None,
) -> str:
    """Create a HorizontalPodAutoscaler targeting a workload.

    Args:
        name: HPA name.
        target_kind: "Deployment" or "StatefulSet" (DaemonSet not scalable).
        target_name: name of the workload.
        namespace: namespace for both HPA and target.
        min_replicas / max_replicas: scaling range.
        cpu_utilization: target average CPU utilization percent (e.g. 70).
        memory_average_value: target average memory value (e.g. "500Mi").

    Provide at least one of cpu_utilization or memory_average_value.

    Returns the apply result.
    """
    if target_kind.lower() not in ("deployment", "statefulset"):
        raise ValueError("HPA only supports Deployment / StatefulSet")
    if cpu_utilization is None and memory_average_value is None:
        raise ValueError("Provide at least one of cpu_utilization or memory_average_value")

    metrics: list[dict[str, Any]] = []
    if cpu_utilization is not None:
        metrics.append({
            "type": "Resource",
            "resource": {
                "name": "cpu",
                "target": {"type": "Utilization", "averageUtilization": int(cpu_utilization)},
            },
        })
    if memory_average_value is not None:
        metrics.append({
            "type": "Resource",
            "resource": {
                "name": "memory",
                "target": {"type": "AverageValue", "averageValue": memory_average_value},
            },
        })

    api_version = "apps/v1"
    manifest = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": api_version,
                "kind": target_kind,
                "name": target_name,
            },
            "minReplicas": int(min_replicas),
            "maxReplicas": int(max_replicas),
            "metrics": metrics,
        },
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_pdb(
    name: str,
    target_kind: str,
    target_name: str,
    namespace: str,
    min_available: str | int | None = None,
    max_unavailable: str | int | None = None,
) -> str:
    """Create a PodDisruptionBudget for a workload.

    Args:
        name: PDB name.
        target_kind: "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet",
            or "ReplicationController".
        target_name: workload name (selector matches pods with this label).
        namespace: namespace.
        min_available: e.g. 1, 2, or "50%" (string for percentage).
        max_unavailable: same shape.

    Provide exactly one of min_available / max_unavailable. The other is
    derived (e.g. min=2 is equivalent to max=replicas-2).
    """
    allowed = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController")
    if target_kind not in allowed:
        raise ValueError(f"PDB target_kind must be one of {allowed}")
    if (min_available is None) == (max_unavailable is None):
        raise ValueError("Provide exactly one of min_available or max_unavailable")

    pdb_spec: dict = {
        "selector": {"matchLabels": _discovered_selector_label(target_kind, target_name)},
    }
    if min_available is not None:
        pdb_spec["minAvailable"] = _minmax_value(min_available)
    else:
        pdb_spec["maxUnavailable"] = _minmax_value(max_unavailable)

    manifest = {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": namespace},
        "spec": pdb_spec,
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def _discovered_selector_label(target_kind: str, target_name: str) -> dict[str, str]:
    """The PDB selector needs to match pods, not the workload directly.

    We use the conventional `app.kubernetes.io/name=<workload>` label here.
    If your pods are labeled differently, supply a custom selector via
    apply_yaml instead.
    """
    return {"app.kubernetes.io/name": target_name}


def _minmax_value(v: str | int) -> int | str:
    if isinstance(v, int):
        return v
    return v  # strings like "50%" pass through


def register(mcp) -> None:
    mcp.tool()(create_hpa)
    mcp.tool()(create_pdb)
