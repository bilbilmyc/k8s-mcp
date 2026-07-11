"""ConfigMap read and update (full replace).

中文说明：
`get_configmap` 返回 ConfigMap 内容（按 key 列出）；`update_configmap`
默认走整体替换（保留 ResourceVersion）；`merge=True` 时仅覆盖传入的
key，未提及的 key 保持原值。Secret 没有类似工具——改 Secret 必须显式
用 `apply_yaml`，避免误操作。
"""
from __future__ import annotations

import logging

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from ..config import get_settings
from ..formatters import to_yaml
from . import generic

logger = logging.getLogger(__name__)


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


def create_configmap(
    name: str,
    namespace: str,
    data: dict[str, str] | None = None,
    yaml_content: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """⚠️ WRITE — create a ConfigMap — pick THIS for the common case
    "give me a ConfigMap with these key/value pairs" without having to
    hand-write the YAML manifest.

    Two input modes:

      1. **`data=`** — flat dict of string key/value pairs (most common).
         Stored as `ConfigMap.data` (UTF-8 strings only). For binary content
         (cert / key files), use the raw YAML mode below and put the bytes
         under `binaryData`.
      2. **`yaml_content=`** — a complete multi-document YAML manifest.
         Useful when the source already lives in a `*.yaml` file the agent
         read, or when `binaryData` / `immutable` / complex annotations are
         needed. Forwarded to `apply_yaml` so existing safety nets apply.

    Args:
        name: ConfigMap name.
        namespace: target namespace.
        data: optional `data: dict[str, str]` of string key/value pairs.
        yaml_content: optional raw YAML (single or multi-doc); if set,
            `data` / `labels` are ignored.
        labels: optional labels applied to the ConfigMap.

    Returns the apply result (kind/name: action).

    Raises:
        ValueError: neither data nor yaml_content is set, both are set, or
            `name` / `labels` are invalid.
        PermissionError: read-only mode or namespace allowlist denies write.
    """
    _read_only_guard("create_configmap")
    _ensure_ns(namespace)

    if (data is None) == (yaml_content is None):
        raise ValueError(
            "Provide exactly one of `data` (dict) or `yaml_content` (raw YAML)"
        )

    if yaml_content is not None:
        return generic.apply_yaml(yaml_content)

    if not isinstance(data, dict) or not data:
        raise ValueError("`data` must be a non-empty dict[str, str]")

    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels

    manifest = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": md,
        "data": {k: str(v) for k, v in data.items()},
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def register(mcp) -> None:
    mcp.tool()(get_configmap)
    mcp.tool()(update_configmap)
    mcp.tool()(create_configmap)
