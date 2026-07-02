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


def _read_only_guard() -> None:
    if get_settings().read_only:
        raise PermissionError("Server is in read-only mode.")


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
    """Replace (or merge) a ConfigMap's `data` field.

    Args:
        name, namespace: ConfigMap identity.
        data: new key/value mapping.
        merge: if False (default), the entire data field is replaced with `data`
            (existing keys not in `data` are removed). If True, new keys are
            merged over the existing data and missing keys are preserved.
    """
    _read_only_guard()
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


def register(mcp) -> None:
    mcp.tool()(get_configmap)
    mcp.tool()(update_configmap)
