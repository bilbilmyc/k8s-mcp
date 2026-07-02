"""Delete with two-step confirmation (preview → confirm).

NEVER delete without first showing the user what will be deleted and getting
their explicit approval. The tool enforces this with a signed short-lived
confirmation_token.

中文说明：
删除是 k8s-mcp 中最危险的工具，所以强制走"预览 → 二次确认"两步流程：

  1. 第一步：`confirm=False`，工具返回当前对象的 YAML（Secret 会脱敏）
     + 一个 HMAC 签名的 token（默认 5 分钟过期）。
  2. 第二步：Agent 把预览展示给用户，得到明确同意后用 `confirm=True`
     + 同一个 token 再调一次，token 里的 kind/name/namespace/grace_period
     必须与本次请求完全一致才执行真正的删除。

这一步无法跳过，也无法批量复用 token。任何 token 不匹配、过期、伪造、
read_only、或 namespace 不在 allowlist 内的情况都会被拒。
"""
from __future__ import annotations

import logging
from typing import Any

from kubernetes import dynamic
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from ..config import Settings, get_settings
from ..formatters import mask_secret_data, to_yaml
from ..safety import (
    TokenError,
    assert_payload_matches,
    issue_token,
    make_delete_payload,
    verify_token,
)
from .generic import _api_version_for, _dyn_client, _fetch

logger = logging.getLogger(__name__)


def delete_resource(
    kind: str,
    name: str,
    namespace: str | None = None,
    confirm: bool = False,
    confirmation_token: str | None = None,
    grace_period_seconds: int = 30,
) -> dict[str, Any]:
    """Delete a Kubernetes resource. Two-step (preview → confirm) flow.

    DANGEROUS: this is IRREVERSIBLE. Always confirm with the user before
    calling with confirm=True.

    Workflow:
      1. Call with confirm=False (default). The tool returns:
         - preview_yaml: the YAML of the resource as it currently exists
         - confirmation_token: an HMAC-signed, short-lived (5 min) token
         - expires_in_seconds: how long the token is valid
         - instruction: a reminder for the agent
      2. Show the preview_yaml to the user and ask them to confirm.
      3. After the user explicitly approves, re-call with confirm=True AND the
         same confirmation_token. The token's payload must match the current
         call (same kind/name/namespace/grace_period_seconds).

    Safety:
      - Refused when settings.read_only is True.
      - Refused when settings.namespace_allowlist is set and the target
        namespace is not allowed.
      - Refused if confirm=True but no confirmation_token provided.
      - Refused if confirmation_token is expired, forged, or doesn't match
        the current request's parameters.
    """
    settings = get_settings()
    if settings.read_only:
        raise PermissionError(
            "Server is in read-only mode (K8S_MCP_READ_ONLY=true). delete is disabled."
        )
    if not settings.ns_allowed(namespace):
        raise PermissionError(
            f"Delete in namespace '{namespace}' is not allowed by "
            "K8S_MCP_NAMESPACE_ALLOWLIST"
        )

    if not confirm:
        return _preview(kind, name, namespace, grace_period_seconds, settings)

    if not confirmation_token:
        raise TokenError(
            "confirm=True requires the confirmation_token returned by the "
            "preview step. Call with confirm=False first."
        )
    try:
        payload = verify_token(confirmation_token, settings.delete_token_secret)
    except TokenError:
        raise  # propagate to caller

    assert_payload_matches(
        payload,
        kind=kind,
        name=name,
        namespace=namespace,
        grace_period_seconds=grace_period_seconds,
    )

    return _execute(kind, name, namespace, grace_period_seconds, settings)


# ---------- private ------------------------------------------------------------


def _preview(kind: str, name: str, namespace: str | None,
              grace_period_seconds: int, settings: Settings) -> dict[str, Any]:
    """Return a preview dict containing the current YAML and a confirmation token."""
    obj = _fetch(kind, name, namespace)
    if kind.lower() == "secret":
        obj = mask_secret_data(obj)
    preview_yaml = to_yaml(obj)

    payload = make_delete_payload(kind, name, namespace, grace_period_seconds)
    token = issue_token(
        payload,
        secret=settings.delete_token_secret,
        ttl_seconds=settings.delete_token_ttl_seconds,
    )
    return {
        "preview_yaml": preview_yaml,
        "confirmation_token": token,
        "expires_in_seconds": int(settings.delete_token_ttl_seconds),
        "instruction": (
            "Show the preview_yaml to the user. ONLY after they explicitly "
            "approve, re-call delete_resource with confirm=True and the "
            "confirmation_token above."
        ),
    }


def _execute(kind: str, name: str, namespace: str | None,
              grace_period_seconds: int, settings: Settings) -> dict[str, Any]:
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
        raise LookupError(f"{kind} '{name}' already gone{suffix(namespace)}") from e
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
    return f" from namespace '{namespace}'" if namespace else ""


def register(mcp) -> None:
    mcp.tool()(delete_resource)
