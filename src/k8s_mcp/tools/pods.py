"""Pod listing + delete (escape hatch).

中文说明：
- `list_pods`：支持 namespace / label_selector / field_selector 三类筛选，
  `include_all=True` 跨所有 namespace。
- `delete_pod`：单 Pod 删除的低风险逃生通道——故意绕过 delete_resource 的
  二次确认机制（删除一个 Pod 通常只是触发重启，不是真删数据），适合 Agent
  "重启 pod" 这一类常规排障动作。
"""
from __future__ import annotations

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import format_age, short_table


def _core_v1():
    return client.CoreV1Api(get_api_client())


def list_pods(
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    include_all: bool = False,
) -> str:
    """List Pods with Pod-specific columns (PHASE / RESTARTS / NODE). For a
    generic cross-kind list, prefer `list_resources(kind="Pod", ...)` — that
    one works on any kind (including CRDs); use THIS tool only when you need
    Pod-specific columns or the `include_all` Succeeded/Failed filter.
    Equivalent to `kubectl get pods`.

    Note: prefer reusing the most recent result for the same query rather
    than re-calling if the underlying state is unlikely to have changed. New
    calls remain valid when verifying a mutation's effect.

    Args:
        namespace: namespace to list; None = all namespaces.
        label_selector: e.g. "app=nginx".
        field_selector: e.g. "status.phase=Running" or "spec.nodeName=node-1".
        include_all: by default completed/evicted pods are hidden. Set True to
            include them.

    Returns a NAME / NAMESPACE / PHASE / RESTARTS / AGE / NODE table.
    """
    api = _core_v1()
    if namespace:
        ret = api.list_namespaced_pod(
            namespace,
            label_selector=label_selector,
            field_selector=field_selector,
        )
    else:
        ret = api.list_pod_for_all_namespaces(
            label_selector=label_selector,
            field_selector=field_selector,
        )

    rows = []
    for pod in ret.items:
        phase = (pod.status.phase or "")
        if not include_all and phase in ("Succeeded", "Failed", "Evicted"):
            continue
        restarts = sum(cs.restart_count for cs in (pod.status.container_statuses or []))
        rows.append({
            "NAME": pod.metadata.name,
            "NAMESPACE": pod.metadata.namespace,
            "PHASE": phase,
            "RESTARTS": str(restarts),
            "AGE": format_age(pod.metadata.creation_timestamp),
            "NODE": pod.spec.node_name or "",
        })

    return short_table(rows, ["NAME", "NAMESPACE", "PHASE", "RESTARTS", "AGE", "NODE"])


def delete_pod(name: str, namespace: str, grace_period_seconds: int = 30) -> str:
    """Delete a single Pod (immediate reschedule).

    This bypasses the two-step delete confirmation in `delete_resource`
    because deleting a Pod is a low-risk recovery / restart primitive —
    the controller (Deployment, StatefulSet, Job, …) will recreate it.

    .. deprecated::
        Use :func:`delete_resource` with ``kind='Pod'`` instead. This
        one-step wrapper will be removed in v0.5.0; the two-step
        preview+confirm flow is the recommended path for all
        destructive ops going forward. Keep using ``delete_pod`` for
        now if you specifically need the one-step behavior.

    Args:
        name: pod name.
        namespace: pod namespace.
        grace_period_seconds: how long to wait before force-killing
            containers (default 30; set to 0 for immediate kill).
    """
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")
    if not get_settings().ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )
    body = client.V1DeleteOptions()
    if grace_period_seconds is not None:
        body.grace_period_seconds = grace_period_seconds
    try:
        _core_v1().delete_namespaced_pod(name, namespace, body=body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Pod '{namespace}/{name}' not found") from e
        raise
    return (
        f"⚠️ DEPRECATED: delete_pod will be removed in v0.5.0 — "
        f"use delete_resource(kind='Pod') for the audited two-step flow.\n"
        f"Pod/{namespace}/{name} deleted (grace={grace_period_seconds}s); "
        f"controller will recreate"
    )


def register(mcp) -> None:
    mcp.tool()(list_pods)
    mcp.tool()(delete_pod)
