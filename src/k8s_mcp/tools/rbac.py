"""RBAC shortcuts: Role / RoleBinding / ClusterRole / ClusterRoleBinding.

These build minimal YAML and delegate to apply_yaml so safety checks apply.
For complex rule sets, prefer apply_yaml with a hand-written manifest.

中文说明：
- `create_role` / `create_clusterrole`：必须传至少一条 rule。
- `create_rolebinding` / `create_clusterrolebinding`：role_kind 必须是
  `Role` 或 `ClusterRole`（不能是 ServiceAccount —— 那是 subject）。
- subjects 通常是 `{"kind": "ServiceAccount", "name": "..."}`，可多个。

复杂权限策略建议直接用 `apply_yaml` 传完整 manifest，工具函数只覆盖
最常见的快捷路径。
"""
from __future__ import annotations

import logging
from typing import Any

import yaml
from kubernetes import client
from kubernetes.client.rest import ApiException

from ..client import get_api_client
from . import generic

logger = logging.getLogger(__name__)


def create_role(
    name: str,
    namespace: str,
    rules: list[dict[str, Any]],
    allow_wildcard: bool = False,
) -> str:
    """Create a namespaced Role.

    Args:
        name: Role name.
        namespace: target namespace.
        rules: list of policy rules, each:
            {
              "apiGroups": [""],            # core group is ""
              "resources": ["pods"],
              "verbs": ["get", "list", "watch"],
              "resourceNames": ["..."]      # optional
            }
        allow_wildcard: must be True to allow any rule whose `verbs`,
            `resources`, and `apiGroups` are all `["*"]` (cluster-admin
            equivalent). Single-axis wildcards (e.g. `verbs=["*"]` on a
            specific resource) are still allowed without this flag — they
            are a normal K8s pattern for controller ServiceAccounts.
    Returns the apply result.
    """
    if not rules:
        raise ValueError("Provide at least one rule")
    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": namespace},
        "rules": [_validate_rule(r, allow_wildcard=allow_wildcard) for r in rules],
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_rolebinding(
    name: str,
    namespace: str,
    role_kind: str,
    role_name: str,
    subjects: list[dict[str, str]],
) -> str:
    """Create a namespaced RoleBinding.

    Args:
        name: binding name.
        namespace: target namespace.
        role_kind: "Role" or "ClusterRole".
        role_name: referenced Role/ClusterRole name.
        subjects: list of {kind, name, namespace?, apiGroup?}:
            kind: "ServiceAccount" / "User" / "Group"
            name: subject name
            namespace: required when kind=ServiceAccount
            apiGroup: defaults inferred from kind
    """
    if role_kind not in ("Role", "ClusterRole"):
        raise ValueError("role_kind must be Role or ClusterRole")
    if not subjects:
        raise ValueError("Provide at least one subject")

    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": name, "namespace": namespace},
        "subjects": [_normalize_subject(s) for s in subjects],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": role_kind,
            "name": role_name,
        },
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_clusterrole(
    name: str,
    rules: list[dict[str, Any]],
    allow_wildcard: bool = False,
) -> str:
    """⚠️ WRITE / ⚠️ CLUSTER-SCOPED — ClusterRole bypasses the
    K8S_MCP_NAMESPACE_ALLOWLIST (ClusterRoles have no namespace) and the
    permissions are visible to every namespace.

    For namespace-scoped permissions, use `create_role` instead. Rule
    schema is identical — see `create_role`.

    Args: name + rules (same shape as create_role).
    `allow_wildcard` gates the cluster-admin triple (`verbs=*` ∧
    `resources=*` ∧ `apiGroups=*`); without it, _validate_rule refuses.
    """
    if not rules:
        raise ValueError("Provide at least one rule")
    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": name},
        "rules": [_validate_rule(r, allow_wildcard=allow_wildcard) for r in rules],
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_clusterrolebinding(
    name: str,
    role_name: str,
    subjects: list[dict[str, str]],
) -> str:
    """⚠️ WRITE / ⚠️ CLUSTER-SCOPED — grants the bound subjects cluster-wide
    access; bypasses K8S_MCP_NAMESPACE_ALLOWLIST and is visible everywhere.

    For namespace-scoped bindings, use `create_rolebinding` instead.
    `role_name` MUST reference a ClusterRole (not a namespaced Role).
    """
    if not subjects:
        raise ValueError("Provide at least one subject")
    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": name},
        "subjects": [_normalize_subject(s) for s in subjects],
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": role_name,
        },
    }
    return generic.apply_yaml(yaml.safe_dump(manifest))


# ---------- internals ----------------------------------------------------------


def _validate_rule(rule: dict, *, allow_wildcard: bool = False) -> dict:
    """Validate + normalize a single RBAC rule.

    Refuses the cluster-admin triple (`verbs=["*"]` ∧ `resources=["*"]` ∧
    `apiGroups=["*"]`) unless `allow_wildcard=True` is set by the caller.
    Single-axis wildcards (e.g. `verbs=["*"]` on specific resources) are
    still allowed — they are a normal K8s pattern for controller SAs.
    """
    verbs = rule.get("verbs")
    if not verbs:
        raise ValueError("Rule missing 'verbs'")
    api_groups = list(rule.get("apiGroups", [""]))
    resources = list(rule.get("resources", []))

    if (
        not allow_wildcard
        and verbs == ["*"]
        and resources == ["*"]
        and api_groups == ["*"]
    ):
        raise PermissionError(
            "Refusing cluster-admin wildcard triple (verbs=['*'] ∧ "
            "resources=['*'] ∧ apiGroups=['*']). This grants cluster-admin "
            "on every resource and is one prompt-injection away from full "
            "cluster takeover. Pass allow_wildcard=True to opt in "
            "explicitly (or use apply_yaml with a hand-written manifest "
            "for fine-grained rules)."
        )

    out = {
        "verbs": list(verbs),
        "apiGroups": api_groups,
        "resources": resources,
    }
    if "resourceNames" in rule:
        out["resourceNames"] = list(rule["resourceNames"])
    return out


def _normalize_subject(s: dict) -> dict:
    """Normalize a subject dict; defaults apiGroup based on kind."""
    kind = s.get("kind")
    name = s.get("name")
    if not kind or not name:
        raise ValueError("Subject needs 'kind' and 'name'")
    api_group = s.get("apiGroup")
    if api_group is None:
        api_group = "" if kind == "ServiceAccount" else "rbac.authorization.k8s.io"
    out: dict = {"kind": kind, "name": name, "apiGroup": api_group}
    if kind == "ServiceAccount" and s.get("namespace"):
        out["namespace"] = s["namespace"]
    return out


def whoami(namespace: str = "default") -> str:
    """👤 WHOAMI — show the identity the MCP server is running as and the
    effective permissions in the given namespace.

    Use this when a write/read tool returns `Forbidden` and you want to
    know why — call `whoami` first to see which user/SA you are, which
    groups you're in, and what resources/verbs you can touch in the
    target namespace. Saves a round of "let me try the same call with
    different role bindings".

    Args:
        namespace: namespace to check effective permissions in. Default
            "default" — that's where the SelfSubjectRulesReview API
            scopes its results. Cluster-scoped permissions (e.g.
            ClusterRoles bound via ClusterRoleBinding) are NOT listed
            here; check those with `get_role_bindings` / `list_resources
            (kind="ClusterRoleBinding", ...)`.

    Returns a multi-line report with:
        ## Identity
          user/SA, UID, groups
        ## Effective permissions in namespace '<ns>'
          table of api_group / resources / verbs
    """
    api_client = get_api_client()
    authn_api = client.AuthenticationV1Api(api_client)

    try:
        review = authn_api.create_self_subject_review(body={})
    except ApiException as e:
        return f"❌ whoami: identity check failed: {e.status} {e.reason}"

    username = (getattr(review.status, "username", None) or "(anonymous)")
    uid = getattr(review.status, "uid", None) or ""
    groups = list(getattr(review.status, "groups", None) or [])

    lines = ["## Identity"]
    lines.append(f"User/SA:    {username}")
    if uid:
        lines.append(f"UID:        {uid}")
    if groups:
        lines.append(f"Groups:     {', '.join(groups)}")

    # Effective permissions in the requested namespace
    try:
        from kubernetes.client.models import (
            V1SelfSubjectRulesReview,
            V1SelfSubjectRulesReviewSpec,
        )
        authz_api = client.AuthorizationV1Api(api_client)
        spec = V1SelfSubjectRulesReviewSpec(namespace=namespace)
        body = V1SelfSubjectRulesReview(spec=spec)
        result = authz_api.create_self_subject_rules_review(body)
        rules = list(getattr(result.status, "resource_rules", None) or [])
        non_resource = list(getattr(result.status, "non_resource_rules", None) or [])

        lines.append("")
        lines.append(f"## Effective permissions in namespace '{namespace}'")
        if not rules and not non_resource:
            lines.append("(none — this identity has no namespace-scoped rules here)")
        if rules:
            for r in rules:
                api_groups = ", ".join(r.api_groups or []) or "core"
                resources = ", ".join(r.resources or [])
                verbs = ", ".join(r.verbs or [])
                lines.append(f"  - {api_groups}/{resources}: {verbs}")
        if non_resource:
            lines.append("  Non-resource URLs:")
            for url in non_resource:
                lines.append(f"    - {url}")
    except ApiException as e:
        lines.append("")
        lines.append(f"## Effective permissions in namespace '{namespace}'")
        lines.append(f"(SelfSubjectRulesReview failed: {e.status} {e.reason})")
    except Exception as e:  # noqa: BLE001
        # Older apiservers may not support the API; surface that clearly.
        lines.append("")
        lines.append(f"## Effective permissions in namespace '{namespace}'")
        lines.append(f"(SelfSubjectRulesReview unsupported: {type(e).__name__}: {e})")

    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(create_role)
    mcp.tool()(create_rolebinding)
    mcp.tool()(create_clusterrole)
    mcp.tool()(create_clusterrolebinding)
    mcp.tool()(whoami)
