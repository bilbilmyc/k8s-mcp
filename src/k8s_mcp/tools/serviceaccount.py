"""ServiceAccount creation shortcut.

中文说明：
`create_serviceaccount(name, namespace, image_pull_secrets=[]?)`：
- `image_pull_secrets` 是可选 list，引用已有的 Secret 名（用于私有镜像仓库）。
- 创建后需要 RoleBinding / ClusterRoleBinding 才能让 SA 有权限操作资源。
"""
from __future__ import annotations

import logging

import yaml

from . import generic

logger = logging.getLogger(__name__)


def create_serviceaccount(
    name: str,
    namespace: str,
    image_pull_secrets: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Create a ServiceAccount.

    Pass `image_pull_secrets=[...]` when Pods in this namespace need to
    pull from a private registry. To inspect the current caller's
    identity (NOT this), use `whoami(namespace=...)`; to inspect
    permissions granted to an existing SA, use `analyze_rbac(subject=...)`.

    Args:
        name: ServiceAccount name.
        namespace: target namespace.
        image_pull_secrets: list of Secret names in the same namespace
            used to pull images from private registries.
        labels: optional labels applied to the SA.
    """
    md: dict = {"name": name, "namespace": namespace}
    if labels:
        md["labels"] = labels
    sa: dict = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": md}
    if image_pull_secrets:
        sa["imagePullSecrets"] = [{"name": n} for n in image_pull_secrets]
    return generic.apply_yaml(yaml.safe_dump(sa))


def register(mcp) -> None:
    mcp.tool()(create_serviceaccount)
