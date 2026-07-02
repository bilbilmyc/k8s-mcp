"""Storage shortcut: create_pvc.

中文说明：
`create_pvc(name, namespace, size, access_modes?, storage_class?, labels?)`：
- `size` 用 K8s 资源量字符串，如 `"10Gi"`、`"500Mi"`。
- `access_modes` 默认 `["ReadWriteOnce"]`；多读多写场景传
  `["ReadWriteMany"]` 等。
- `storage_class` 留空走集群默认；集群无默认 SC 时会 Pending。
"""
from __future__ import annotations

import logging

from . import generic

logger = logging.getLogger(__name__)


def create_pvc(
    name: str,
    namespace: str,
    size: str,
    access_modes: list[str] | None = None,
    storage_class: str | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Create a PersistentVolumeClaim.

    Args:
        name: PVC name.
        namespace: target namespace.
        size: requested size, e.g. "1Gi", "10Gi".
        access_modes: defaults to ["ReadWriteOnce"]. Pass a list like
            ["ReadOnlyMany", "ReadWriteMany"] for ROX/RWX filesystems.
        storage_class: optional StorageClass name.
        labels: optional labels.
    """
    if access_modes is None:
        access_modes = ["ReadWriteOnce"]
    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels
    spec = {
        "accessModes": access_modes,
        "resources": {"requests": {"storage": size}},
    }
    if storage_class:
        spec["storageClassName"] = storage_class
    manifest = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": md,
        "spec": spec,
    }
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def register(mcp) -> None:
    mcp.tool()(create_pvc)
