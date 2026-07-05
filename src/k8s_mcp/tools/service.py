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

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from . import generic


def _read_only_guard(action: str) -> None:
    if get_settings().read_only:
        raise PermissionError(
            f"Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            f"{action} is disabled."
        )


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
    """Create a Service with manual selector/ports/type — pick THIS when you
    know the exact pod selector and want full control over ports/type/clusterIP.

    For the common case "make my Deployment reachable", prefer
    `expose_workload` — it reads the Deployment's selector for you (kubectl
    expose equivalent). For inbound HTTP routing into an existing Service,
    use `create_ingress`. For raw YAML control (multi-port, headless, etc.),
    use `apply_yaml` directly.

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
    _read_only_guard("create_service")
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
    """Create an Ingress that routes external HTTP(S) traffic to existing
    Services — pick THIS for inbound routing (host/path → Service:port).

    Each rule's `service_name` MUST already exist as a Service in the same
    namespace. The Service must be reachable on the listed `service_port`.
    Default `ingress_class_name="nginx"` — for Traefik / HAProxy / other
    controllers, override explicitly. For raw YAML control (custom backends,
    default backends, multiple TLS), use `apply_yaml` directly.

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
    _read_only_guard("create_ingress")
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
    """Auto-build a Service targeting an existing Deployment / StatefulSet /
    DaemonSet — pick THIS when you already have a workload and just want it
    reachable. Reads the workload's `selector.matchLabels` for you.

    Equivalent to `kubectl expose <kind>/<name> --port=...`. Use `create_service`
    instead when you need a custom selector, multi-port Service, headless
    Service, or a Service not backed by an existing workload.
    """
    _read_only_guard("expose_workload")
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


def _core_v1():
    return client.CoreV1Api(get_api_client())


def _networking_v1():
    return client.NetworkingV1Api(get_api_client())


def delete_service(name: str, namespace: str = "default") -> str:
    """⚠️ WRITE — delete a Service (one-step, no two-step HMAC).

    Why one-step: a Service is a traffic-routing rule, not a workload.
    Deleting it stops inbound traffic to the Pods, but the Pods continue
    to run. Re-creatable with `create_service` / `expose_workload` /
    `apply_yaml`.

    .. deprecated::
        Use :func:`delete_resource` with ``kind='Service'`` instead.
        This one-step wrapper will be removed in v0.5.0; the two-step
        preview+confirm flow is the recommended path for all
        destructive ops going forward.

    Args:
        name: Service name.
        namespace: Service namespace (default "default").
    """
    _read_only_guard("delete_service")
    _ensure_ns(namespace)
    try:
        _core_v1().delete_namespaced_service(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Service '{namespace}/{name}' not found") from e
        raise
    return (
        f"⚠️ DEPRECATED: delete_service will be removed in v0.5.0 — "
        f"use delete_resource(kind='Service') for the audited two-step flow.\n"
        f"Service/{namespace}/{name} deleted"
    )


def delete_ingress(name: str, namespace: str = "default") -> str:
    """⚠️ WRITE — delete an Ingress (one-step, no two-step HMAC).

    Why one-step: an Ingress is an HTTP routing rule, not a workload.
    Deleting it stops external HTTP(S) traffic to the Services; the
    Services and Pods keep running. Re-creatable with `create_ingress`
    / `apply_yaml`.

    .. deprecated::
        Use :func:`delete_resource` with ``kind='Ingress'`` instead.
        This one-step wrapper will be removed in v0.5.0; the two-step
        preview+confirm flow is the recommended path for all
        destructive ops going forward.

    Args:
        name: Ingress name.
        namespace: Ingress namespace (default "default").
    """
    _read_only_guard("delete_ingress")
    _ensure_ns(namespace)
    try:
        _networking_v1().delete_namespaced_ingress(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"Ingress '{namespace}/{name}' not found") from e
        raise
    return (
        f"⚠️ DEPRECATED: delete_ingress will be removed in v0.5.0 — "
        f"use delete_resource(kind='Ingress') for the audited two-step flow.\n"
        f"Ingress/{namespace}/{name} deleted"
    )


def register(mcp) -> None:
    mcp.tool()(create_service)
    mcp.tool()(create_ingress)
    mcp.tool()(delete_service)
    mcp.tool()(delete_ingress)
    mcp.tool()(expose_workload)
