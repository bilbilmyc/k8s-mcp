"""Delete a Kubernetes resource.

v0.5.2: single-step delete. The previous two-step preview → confirm flow
required an HMAC-signed `confirmation_token` round-trip; it was removed
because the threat model of an LLM agent driving this MCP is that the
agent both issues and confirms the token in the same call, so the
two-step pattern provides no defense the agent can't bypass in one
shot. Single-step deletes are now guarded by:

  - `K8S_MCP_READ_ONLY=true` (global kill switch — every write tool
    raises PermissionError when this is on)
  - `K8S_MCP_NAMESPACE_ALLOWLIST` (per-namespace write scoping; cluster-
    scoped writes are rejected when this is set)

If you need an additional safety net in a sensitive environment, gate
the MCP server itself with an external RBAC layer (e.g. only run as a
read-mostly SA) rather than relying on the agent to honor a confirmation
handshake.

中文说明：
v0.5.2 起改成一步删除。之前的两步预览（confirm=False 拿 preview_yaml +
HMAC token → confirm=True 真删）在 LLM 驱动的场景下不构成实际防护——
agent 自己既是 token 签发者也是 token 提交者，单次调用就能完成两步。
改成单步后唯一的安全开关是 `K8S_MCP_READ_ONLY`（一刀切）和
`K8S_MCP_NAMESPACE_ALLOWLIST`（ns 维度）。高敏环境请在 MCP server 外部
加 RBAC（例如 SA 用只读角色），不要依赖 agent 自觉。
"""
from __future__ import annotations

import logging
from typing import Any

from kubernetes import dynamic
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from ..config import get_settings
from .generic import _api_version_for, _dyn_client

logger = logging.getLogger(__name__)


def delete_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    grace_period_seconds: int = 30,
) -> dict[str, Any]:
    """Delete a Kubernetes resource.

    DANGEROUS: this is IRREVERSIBLE. The MCP server itself does NOT prompt
    the operator — the LLM agent is responsible for surfacing the target
    (kind / name / namespace / grace_period_seconds) to the human before
    calling. If your deployment needs an additional check, set
    `K8S_MCP_READ_ONLY=true` to disable every write tool.

    Args:
        kind: the Kubernetes kind (Pod, Deployment, ConfigMap, ...).
        name: the resource name.
        namespace: namespace for namespaced resources. Omit for
            cluster-scoped resources.
        grace_period_seconds: how long the apiserver waits for graceful
            termination before force-killing. Default 30s matches the
            Kubernetes default.

    Returns:
        `{"deleted": True, "kind": ..., "name": ..., "namespace": ...,
          "grace_period_seconds": ...}`.

    Raises:
        PermissionError — server is in read-only mode OR namespace is
            outside the allowlist.
        LookupError — resource already gone.
        ValueError — unknown kind.
        RuntimeError — apiserver conflict.
        ApiException — other apiserver failures (RBAC, quota, ...).
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). "
            "delete is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Delete in namespace '{namespace}' is not allowed by "
            "K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    return _execute(kind, name, namespace, grace_period_seconds)


# ---------- private ------------------------------------------------------------


def _execute(
    kind: str,
    name: str,
    namespace: str | None,
    grace_period_seconds: int,
) -> dict[str, Any]:
    dc = _dyn_client()
    api_version = _api_version_for(kind)
    try:
        resource = dc.resources.get(api_version=api_version, kind=kind)
    except ResourceNotFoundError as e:
        raise ValueError(f"Unknown kind: {kind}") from e

    delete_kwargs: dict[str, Any] = {"grace_period_seconds": int(grace_period_seconds)}
    try:
        if namespace:
            resource.delete(name=name, namespace=namespace, **delete_kwargs)
        else:
            resource.delete(name=name, **delete_kwargs)
    except dynamic.exceptions.NotFoundError as e:
        suffix = f" from namespace '{namespace}'" if namespace else ""
        raise LookupError(f"{kind} '{name}' already gone{suffix}") from e
    except dynamic.exceptions.ConflictError as e:
        raise RuntimeError(f"{kind} delete conflict: {e}") from e

    return {
        "deleted": True,
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "grace_period_seconds": int(grace_period_seconds),
    }


def suffix(namespace: str | None) -> str:
    """Backwards-compat helper kept for any callers (was used by the old
    two-step error path). Returns `" from namespace 'X'"` or empty string.
    """
    return f" from namespace '{namespace}'" if namespace else ""


def register(mcp) -> None:
    mcp.tool()(delete_resource)
