"""ConfigMap read and update (full replace).

中文说明：
`get_configmap` 返回 ConfigMap 内容（按 key 列出）；`update_configmap`
默认走整体替换（保留 ResourceVersion）；`merge=True` 时仅覆盖传入的
key，未提及的 key 保持原值。Secret 没有类似工具——改 Secret 必须显式
用 `apply_yaml`，避免误操作。
"""
from __future__ import annotations

from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import to_yaml


def _core_v1():
    return client.CoreV1Api(get_api_client())


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


def get_configmap(name: str, namespace: str = "default") -> str:
    """Read a ConfigMap and return it as YAML."""
    try:
        cm = _core_v1().read_namespaced_config_map(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"ConfigMap '{namespace}/{name}' not found") from e
        raise
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": cm.metadata.name,
            "namespace": cm.metadata.namespace,
            "labels": cm.metadata.labels,
            "annotations": cm.metadata.annotations,
        },
        "data": cm.data or {},
        "binaryData": cm.binary_data or {},
    }
    return to_yaml(obj)


def update_configmap(
    name: str,
    namespace: str,
    data: dict[str, str],
    merge: bool = False,
) -> str:
    """⚠️ WRITE — replace (or merge) a ConfigMap's `data` field.

    ⚠️ When `merge=False` (default), the entire `data` field is REPLACED with
    `data` — keys not present in `data` are WIPED. Pass `merge=True` to
    overwrite only the supplied keys and keep the rest.

    Args:
        name, namespace: ConfigMap identity.
        data: new key/value mapping.
        merge: if False (default), the entire data field is replaced with `data`
            (existing keys not in `data` are removed). If True, new keys are
            merged over the existing data and missing keys are preserved.
    """
    _read_only_guard("update_configmap")
    _ensure_ns(namespace)

    api = _core_v1()
    try:
        existing = api.read_namespaced_config_map(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"ConfigMap '{namespace}/{name}' not found") from e
        raise

    final_data = dict(data) if merge else dict(data)
    if merge:
        final_data = {**(existing.data or {}), **data}

    body = {"data": final_data}
    api.patch_namespaced_config_map(name, namespace, body)
    return f"ConfigMap/{namespace}/{name} updated ({len(final_data)} keys)"


def delete_configmap(name: str, namespace: str = "default") -> str:
    """⚠️ WRITE — delete a ConfigMap (one-step, no two-step HMAC).

    Why one-step: ConfigMaps are loose-coupled config data; deleting one
    will cause Pods that mount it to fail to start, but the failure mode
    is visible (CrashLoopBackOff / CreateContainerConfigError) and the
    CM is re-creatable with `apply_yaml` or `create_pvc`-style helpers.

    For higher-risk delete (Secret, anything that triggers a cascade),
    use the generic two-step `delete_resource` instead.

    .. deprecated::
        Use :func:`delete_resource` with ``kind='ConfigMap'`` instead.
        This one-step wrapper will be removed in v0.5.0; the two-step
        preview+confirm flow is the recommended path for all
        destructive ops going forward.

    Args:
        name: ConfigMap name.
        namespace: ConfigMap namespace (default "default").
    """
    _read_only_guard("delete_configmap")
    _ensure_ns(namespace)
    try:
        _core_v1().delete_namespaced_config_map(name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise LookupError(f"ConfigMap '{namespace}/{name}' not found") from e
        raise
    return (
        f"⚠️ DEPRECATED: delete_configmap will be removed in v0.5.0 — "
        f"use delete_resource(kind='ConfigMap') for the audited two-step flow.\n"
        f"ConfigMap/{namespace}/{name} deleted"
    )


def register(mcp) -> None:
    mcp.tool()(get_configmap)
    mcp.tool()(update_configmap)
    mcp.tool()(delete_configmap)
