"""Workload management: create_deployment, create_statefulset, scale, restart, set_image.

Create functions build YAML manifests and delegate to apply_yaml (so safety
checks apply). Patch functions go straight to the API for precision.

中文说明：
Workload 管理工具覆盖 Deployment / StatefulSet / DaemonSet 的创建、扩缩、
重启、改镜像、改资源等操作。设计上把"创建"和"修改"分两类：

  - 创建类（create_*）：构造 YAML 后走 apply_yaml，read_only 与
    namespace allowlist 检查会自动套上。
  - 修改类（scale / restart / set_image / set_resources）：直接走
    patch API，避免 apply_yaml 的整对象比对，行为更可预测。
"""
from __future__ import annotations

import logging
from datetime import UTC
from typing import Any

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from . import generic

logger = logging.getLogger(__name__)


# ---------- helpers ------------------------------------------------------------


def _ensure_ns(namespace: str) -> str:
    settings = get_settings()
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Write to namespace '{namespace}' is not allowed by K8S_MCP_NAMESPACE_ALLOWLIST"
        )
    return namespace


def _read_only_guard() -> None:
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")


# ---------- Deployment / StatefulSet -------------------------------------------


def create_deployment(
    name: str,
    image: str,
    namespace: str = "default",
    replicas: int = 1,
    container_name: str | None = None,
    ports: list[int] | None = None,
    env: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    resources: dict[str, Any] | None = None,
    image_pull_policy: str | None = None,
) -> str:
    """Create a stateless Deployment — pick THIS when you have a single image
    that doesn't need stable identity or persistent volumes.

    For stateful workloads needing per-replica stable storage / network IDs,
    use `create_statefulset` instead. For raw YAML control (init containers,
    sidecars, multiple containers, custom PodSpec), use `apply_yaml` directly.

    Args:
        name: deployment name.
        image: container image (e.g. "nginx:1.25").
        namespace: target namespace (default "default").
        replicas: desired replica count.
        container_name: name of the container; defaults to the deployment name.
        ports: list of containerPorts to expose.
        env: dict of env vars.
        labels: pod labels (also used as selector).
        resources: e.g. {"requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "256Mi"}}.
        image_pull_policy: "IfNotPresent" / "Always" / "Never".

    Returns the apply result.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    container_name = container_name or name
    labels = labels or {"app": name}

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        _build_container(container_name, image, ports, env, resources, image_pull_policy)
                    ]
                },
            },
        },
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_statefulset(
    name: str,
    image: str,
    service_name: str,
    namespace: str = "default",
    replicas: int = 1,
    container_name: str | None = None,
    ports: list[int] | None = None,
    env: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    volume_mounts: list[dict] | None = None,
    storage_class: str | None = None,
    storage_size: str = "1Gi",
) -> str:
    """Create a StatefulSet with one PersistentVolumeClaim per replica — pick THIS
    when you need stable network IDs (pod-0, pod-1, …) and per-pod persistent
    storage (databases, message brokers, distributed stores).

    For stateless services (web/API/cache), use `create_deployment` instead —
    it's cheaper and faster to roll. `service_name` must already exist as a
    headless Service (clusterIP=None); use `create_service(cluster_ip="None")`
    first. For raw YAML control, use `apply_yaml` directly.

    Args:
        name: statefulset name.
        image: container image.
        service_name: required headless service name (must be created
            beforehand, or use create_service with clusterIP=None).
        namespace: target namespace.
        replicas: desired replica count.
        container_name: defaults to the statefulset name.
        ports: container ports.
        env: env vars.
        labels: pod labels (also used as selector).
        volume_mounts: list of {"name": str, "mountPath": str}.
        storage_class: StorageClass name (optional).
        storage_size: PVC size (default "1Gi").
    """
    _read_only_guard()
    _ensure_ns(namespace)
    container_name = container_name or name
    labels = labels or {"app": name}

    volume_claim_template = {
        "metadata": {"name": f"{name}-data"},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage_size}},
        },
    }
    if storage_class:
        volume_claim_template["spec"]["storageClassName"] = storage_class

    volumes = []
    if volume_mounts:
        volumes = [{"name": vm["name"], "persistentVolumeClaim": {"claimName": vm["name"]}} for vm in volume_mounts]

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "serviceName": service_name,
            "replicas": replicas,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "containers": [
                        _build_container(container_name, image, ports, env, None, None)
                    ],
                    **({"volumes": volumes} if volumes else {}),
                },
            },
            "volumeClaimTemplates": [volume_claim_template],
        },
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


# ---------- patch ops -----------------------------------------------------------


def _apps_v1():
    return client.AppsV1Api(get_api_client())


def scale_workload(kind: str, name: str, namespace: str, replicas: int) -> str:
    """⚠️ WRITE — patches replica count on a Deployment / StatefulSet only
    (not DaemonSet, not Job, not CronJob; HPA-managed workloads will fight back).

    Args:
        kind: "Deployment" or "StatefulSet".
        name, namespace: workload identity.
        replicas: desired replica count.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for scale: {kind}")
    body = {"spec": {"replicas": int(replicas)}}
    api = _apps_v1()
    try:
        if kind_lower == "deployment":
            api.patch_namespaced_deployment_scale(name, namespace, body)
        else:
            api.patch_namespaced_stateful_set_scale(name, namespace, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"{kind} '{namespace}/{name}' not found") from e
        raise
    return f"{kind}/{namespace}/{name} scaled to {replicas}"


def restart_workload(kind: str, name: str, namespace: str) -> str:
    """⚠️ WRITE — triggers a rolling restart of every Pod in a Deployment /
    StatefulSet (Deployment / StatefulSet only; DaemonSet, Job, CronJob
    are not supported).

    Implemented by patching the `kubectl.kubernetes.io/restartedAt`
    annotation on the pod template — kubectl-equivalent.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for restart: {kind}")
    from datetime import datetime
    now = datetime.now(UTC).isoformat()
    body = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "kubectl.kubernetes.io/restartedAt": now,
                    }
                }
            }
        }
    }
    api = _apps_v1()
    try:
        if kind_lower == "deployment":
            api.patch_namespaced_deployment(name, namespace, body)
        else:
            api.patch_namespaced_stateful_set(name, namespace, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"{kind} '{namespace}/{name}' not found") from e
        raise
    return f"{kind}/{namespace}/{name} restart triggered"


def set_image(kind: str, name: str, namespace: str, container: str, image: str) -> str:
    """⚠️ WRITE — triggers a rolling update by changing one container's image on
    a Deployment / StatefulSet (Deployment / StatefulSet only; DaemonSet, Job,
    CronJob are not supported).

    Uses a JSON strategic merge patch under the hood via the kubernetes client.
    `container` must match an existing container name in the PodSpec (case-
    sensitive); for multi-container workloads call once per container.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for set_image: {kind}")
    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "image": image}
                    ]
                }
            }
        }
    }
    api = _apps_v1()
    try:
        if kind_lower == "deployment":
            api.patch_namespaced_deployment(name, namespace, body)
        else:
            api.patch_namespaced_stateful_set(name, namespace, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"{kind} '{namespace}/{name}' not found") from e
        raise
    return f"{kind}/{namespace}/{name} container '{container}' -> {image}"


def set_resources(
    kind: str,
    name: str,
    namespace: str,
    container: str,
    requests: dict[str, str] | None = None,
    limits: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE — silently overwrites the `resources:` block of one container in
    a Deployment / StatefulSet (Deployment / StatefulSet only; DaemonSet, Job,
    CronJob are not supported). Triggers a rolling update.

    Args:
        kind: "Deployment" or "StatefulSet".
        name, namespace, container: workload + container identity.
        requests: e.g. {"cpu": "100m", "memory": "128Mi"}; any subset of
            CPU/memory. Omit keys you don't want to change.
        limits: same shape as requests.

    Pass an empty value for a key (e.g. `requests={"cpu": ""}`) to REMOVE
    that quota. Equivalent to `kubectl set resources`.
    """
    _read_only_guard()
    _ensure_ns(namespace)
    if not requests and not limits:
        raise ValueError("Provide at least one of requests=... or limits=...")
    kind_lower = kind.lower()
    if kind_lower not in ("deployment", "statefulset"):
        raise ValueError(f"Unsupported kind for set_resources: {kind}")

    resources: dict = {}
    if requests is not None:
        resources["requests"] = {k: str(v) for k, v in requests.items() if v != ""}
    if limits is not None:
        resources["limits"] = {k: str(v) for k, v in limits.items() if v != ""}

    body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {"name": container, "resources": resources}
                    ]
                }
            }
        }
    }
    api = _apps_v1()
    try:
        if kind_lower == "deployment":
            api.patch_namespaced_deployment(name, namespace, body)
        else:
            api.patch_namespaced_stateful_set(name, namespace, body)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"{kind} '{namespace}/{name}' not found") from e
        raise
    return f"{kind}/{namespace}/{name} container '{container}' resources updated"


# ---------- private -------------------------------------------------------------


def _build_container(name, image, ports, env, resources, image_pull_policy):
    container = {"name": name, "image": image}
    if ports:
        container["ports"] = [{"containerPort": p} for p in ports]
    if env:
        container["env"] = [{"name": k, "value": v} for k, v in env.items()]
    if resources:
        container["resources"] = resources
    if image_pull_policy:
        container["imagePullPolicy"] = image_pull_policy
    return container


def register(mcp) -> None:
    mcp.tool()(create_deployment)
    mcp.tool()(create_statefulset)
    mcp.tool()(scale_workload)
    mcp.tool()(restart_workload)
    mcp.tool()(set_image)
    mcp.tool()(set_resources)
