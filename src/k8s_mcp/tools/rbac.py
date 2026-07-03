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

from . import generic

logger = logging.getLogger(__name__)


def create_role(
    name: str,
    namespace: str,
    rules: list[dict[str, Any]],
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
    Returns the apply result.
    """
    if not rules:
        raise ValueError("Provide at least one rule")
    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": name, "namespace": namespace},
        "rules": [_validate_rule(r) for r in rules],
    }
    import yaml
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
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


def create_clusterrole(name: str, rules: list[dict[str, Any]]) -> str:
    """⚠️ WRITE / ⚠️ CLUSTER-SCOPED — ClusterRole bypasses the
    K8S_MCP_NAMESPACE_ALLOWLIST (ClusterRoles have no namespace) and the
    permissions are visible to every namespace.

    For namespace-scoped permissions, use `create_role` instead. Rule
    schema is identical — see `create_role`.

    Args: name + rules (same shape as create_role).
    """
    if not rules:
        raise ValueError("Provide at least one rule")
    manifest = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": name},
        "rules": [_validate_rule(r) for r in rules],
    }
    import yaml
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
    import yaml
    return generic.apply_yaml(yaml.safe_dump(manifest))


# ---------- internals ----------------------------------------------------------


def _validate_rule(rule: dict) -> dict:
    """Validate + normalize a single RBAC rule."""
    verbs = rule.get("verbs")
    if not verbs:
        raise ValueError("Rule missing 'verbs'")
    out = {
        "verbs": list(verbs),
        "apiGroups": list(rule.get("apiGroups", [""])),
        "resources": list(rule.get("resources", [])),
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


def register(mcp) -> None:
    mcp.tool()(create_role)
    mcp.tool()(create_rolebinding)
    mcp.tool()(create_clusterrole)
    mcp.tool()(create_clusterrolebinding)
