"""ServiceAccount creation shortcut."""
from __future__ import annotations

import logging

from . import generic

logger = logging.getLogger(__name__)


def create_serviceaccount(
    name: str,
    namespace: str,
    image_pull_secrets: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Create a ServiceAccount.

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
    import yaml
    return generic.apply_yaml(yaml.safe_dump(sa))


def register(mcp) -> None:
    mcp.tool()(create_serviceaccount)
