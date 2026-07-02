"""Service and Ingress creation, plus expose_workload.

中文说明：
提供三个常用入口：

  - `create_service`：手动指定 selector / ports / type
  - `create_ingress`：基于已有的 Service 建 Ingress，支持 hosts / tls
  - `expose_workload`：给定 Deployment / StatefulSet，自动生成 ClusterIP
    Service（kubectl expose 的等价物）

所有创建类工具都委托给 generic.apply_yaml，自动套上 read-only 和
namespace allowlist 检查。
"""
from __future__ import annotations

from typing import Any

from kubernetes import client

from ..client import get_api_client
from ..config import get_settings
from . import generic


def _read_only_guard() -> None:
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")


def _ensure_ns(namespace: str) -> None:
    if not get_settings().ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )


def create_service(
    name: str,
    namespace: str,
    selector: dict[str, str],
    ports: list[dict[str, Any]],
    service_type: str = "ClusterIP",
    cluster_ip: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Create a Service.

    Args:
        name: service name.
        namespace: target namespace.
        selector: pod label selector, e.g. {"app": "nginx"}.
        ports: list of {"port": int, "targetPort": int|str, "protocol"?: str,
            "name"?: str}. At minimum "port" and "targetPort" required.
        service_type: ClusterIP (default), NodePort, LoadBalancer.
        cluster_ip: explicit cluster IP; for headless set to "None".
        labels: optional labels for the Service.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    if service_type not in ("ClusterIP", "NodePort", "LoadBalancer"):
        raise ValueError(f"Unsupported service_type: {service_type}")

    spec: dict[str, Any] = {
        "selector": selector,
        "ports": ports,
        "type": service_type,
    }
    if cluster_ip is not None:
        spec["clusterIP"] = cluster_ip

    manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, **({"labels": labels} if labels else {})},
        "spec": spec,
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_ingress(
    name: str,
    namespace: str,
    rules: list[dict[str, Any]],
    tls: list[dict[str, Any]] | None = None,
    ingress_class_name: str = "nginx",
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Create an Ingress.

    Args:
        name: ingress name.
        namespace: target namespace.
        rules: list of {"host": str, "path": str, "pathType": str,
            "service_name": str, "service_port": int|str}.
        tls: list of {"hosts": [str], "secretName": str}.
        ingress_class_name: defaults to "nginx".
        annotations: e.g. {"nginx.ingress.kubernetes.io/rewrite-target": "/"}.
        labels: optional labels.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    ingress_rules = []
    for r in rules:
        ingress_rules.append({
            "host": r.get("host"),
            "http": {
                "paths": [{
                    "path": r.get("path", "/"),
                    "pathType": r.get("pathType", "Prefix"),
                    "backend": {
                        "service": {
                            "name": r["service_name"],
                            "port": _service_port(r["service_port"]),
                        }
                    },
                }]
            },
        })

    md: dict[str, Any] = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels
    if annotations:
        md["annotations"] = annotations

    spec: dict[str, Any] = {
        "ingressClassName": ingress_class_name,
        "rules": ingress_rules,
    }
    if tls:
        spec["tls"] = tls

    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": md,
        "spec": spec,
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def expose_workload(
    workload_kind: str,
    workload_name: str,
    namespace: str,
    port: int,
    service_name: str | None = None,
    target_port: int | None = None,
    service_type: str = "ClusterIP",
) -> str:
    """Create a Service that targets an existing workload.

    Reads the workload's selector labels and creates a matching Service.
    Equivalent to `kubectl expose`.
    """
    _read_only_guard()
    _ensure_ns(namespace)

    workload_kind_lower = workload_kind.lower()
    api_client = get_api_client()
    apps_v1 = client.AppsV1Api(api_client)

    if workload_kind_lower == "deployment":
        w = apps_v1.read_namespaced_deployment(workload_name, namespace)
        selector = w.spec.selector.match_labels or {}
    elif workload_kind_lower == "statefulset":
        w = apps_v1.read_namespaced_stateful_set(workload_name, namespace)
        selector = w.spec.selector.match_labels or {}
    elif workload_kind_lower == "daemonset":
        w = apps_v1.read_namespaced_daemon_set(workload_name, namespace)
        selector = w.spec.selector.match_labels or {}
    else:
        raise ValueError(f"Unsupported workload kind: {workload_kind}")

    if not selector:
        raise ValueError(f"{workload_kind}/{workload_name} has no selector.matchLabels")

    svc_name = service_name or workload_name
    target = target_port or port
    ports = [{"port": port, "targetPort": target, "protocol": "TCP", "name": f"http-{port}"}]
    return create_service(
        name=svc_name,
        namespace=namespace,
        selector=selector,
        ports=ports,
        service_type=service_type,
        labels={"app.kubernetes.io/managed-by": "k8s-mcp"},
    )


def _service_port(p: Any) -> dict[str, Any]:
    if isinstance(p, int):
        return {"number": p}
    return {"name": str(p)}


def register(mcp) -> None:
    mcp.tool()(create_service)
    mcp.tool()(create_ingress)
    mcp.tool()(expose_workload)
