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
from kubernetes import client, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError

from ..client import get_api_client
from ..formatters import short_table
from . import generic

logger = logging.getLogger(__name__)


def _dyn_client() -> dynamic.DynamicClient:
    return dynamic.DynamicClient(get_api_client())


def _rbac_resource(dc: dynamic.DynamicClient, kind: str):
    """Resolve a RBAC kind to its DynamicClient resource handle."""
    api_version = "rbac.authorization.k8s.io/v1"
    return dc.resources.get(api_version=api_version, kind=kind)


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


# ---------- analyze_rbac ----------------------------------------------------


def _subject_matches(subject: dict, needle: str) -> bool:
    """Match a binding subject against a user-provided needle.

    Match on `name` (exact) OR on the fully-qualified form
    `kind:name` (e.g. `system:serviceaccount:default:app`). We don't
    try to match `kind:namespace:name` — that's rare and adds
    confusion for the common case.
    """
    needle = (needle or "").strip()
    if not needle:
        return False
    if subject.get("name") == needle:
        return True
    # `User:alice` / `Group:system:masters` / `ServiceAccount:default:app`
    fq = f"{subject.get('kind', '')}:{subject.get('name', '')}"
    if fq == needle:
        return True
    return False


def _rule_matches(
    rule: dict,
    verb: str | None,
    resource: str | None,
    api_group: str | None,
) -> bool:
    """Check if a rule grants the requested (verb, resource, apiGroup).

    Wildcards: if the rule uses `*` for any axis it matches anything on
    that axis. If the caller doesn't specify an axis we don't filter on
    it (a `verb=delete` query should match rules that grant delete on
    pods AND rules that grant delete on deployments — the agent cares
    about the verb, not the resource, when asking "who can delete?").
    """
    verbs = rule.get("verbs") or []
    resources = rule.get("resources") or []
    api_groups = rule.get("apiGroups") or [""]  # core group = ""

    if verb and not (verb in verbs or "*" in verbs):
        return False
    if resource and not (resource in resources or "*" in resources):
        return False
    if api_group is not None:
        # apiGroup="" means core; a request for "" matches only rules
        # that explicitly include "" (or "*").
        if (
            api_group not in api_groups
            and "*" not in api_groups
        ):
            return False
    return True


def analyze_rbac(
    subject: str | None = None,
    verb: str | None = None,
    resource: str | None = None,
    api_group: str | None = None,
    namespace: str | None = None,
) -> str:
    """🔍 RBAC analyzer — multi-mode read-only RBAC inspector.

    Modes (set at least one parameter to get a focused result;
    set none for a cluster-wide summary):

    - **`subject`** (e.g. `"alice"`, `"system:serviceaccount:default:app"`,
      `"Group:system:masters"`) → list every rule granted to this
      subject via any RoleBinding / ClusterRoleBinding in scope.
    - **`verb + resource [+ api_group]`** (e.g. `verb="delete"`,
      `resource="pods"`) → list every subject that can perform this
      action, and the bindings / roles through which the access flows.
    - **`namespace`** only → list all Roles / RoleBindings in that
      namespace (cluster-scope rules are NOT listed — pass `subject=`
      or `verb+resource` to see those).
    - nothing set → cluster-wide summary of all Roles / ClusterRoles /
      RoleBindings / ClusterRoleBindings with a count + a flag for any
      wildcard-bearing rule (cluster-admin risk surface).

    Args:
        subject: user / SA / group name, or `Kind:name` form.
        verb: action verb (`get` / `list` / `create` / `update` /
            `delete` / `*`).
        resource: resource name (`pods` / `deployments` / `*`).
        api_group: API group. Use `""` for the core group. `None` means
            "don't filter on api_group" (matches rules in any group).
        namespace: scope to a single namespace; `None` = cluster-wide.

    Returns a multi-section report. Empty mode returns a summary.
    Always read-only — no mutations.
    """
    dc = _dyn_client()
    try:
        role_res = _rbac_resource(dc, "Role")
        binding_res = _rbac_resource(dc, "RoleBinding")
        cluster_role_res = _rbac_resource(dc, "ClusterRole")
        cluster_binding_res = _rbac_resource(dc, "ClusterRoleBinding")
    except ResourceNotFoundError as e:
        raise RuntimeError(
            "rbac.authorization.k8s.io/v1 not available — is the RBAC "
            "API enabled on this cluster?"
        ) from e

    # Determine scope and load the relevant objects.
    cluster_only = namespace is None
    if cluster_only:
        roles = list(cluster_role_res.get().items)
        bindings = list(cluster_binding_res.get().items)
        if namespace is None:
            # Also include namespaced roles / bindings, but flag them.
            roles.extend(list(role_res.get().items))
            bindings.extend(list(binding_res.get().items))
    else:
        roles = list(role_res.get(namespace=namespace).items)
        bindings = list(binding_res.get(namespace=namespace).items)

    # Mode 1: subject → forward lookup
    if subject:
        return _render_subject_report(subject, roles, bindings, namespace)

    # Mode 2: verb + resource → reverse lookup
    if verb or resource:
        return _render_rule_report(
            verb, resource, api_group, roles, bindings, namespace,
        )

    # Mode 3: namespace only → list RBAC in that ns
    if namespace is not None:
        return _render_namespace_report(namespace, roles, bindings)

    # Mode 4: cluster-wide summary
    return _render_summary_report(roles, bindings)


def _role_rules(role_obj: dict) -> list[dict]:
    return role_obj.get("rules") or []


def _binding_role_ref(binding_obj: dict) -> tuple[str, str]:
    """Return (kind, name) of the role referenced by a binding."""
    role_ref = binding_obj.get("roleRef") or {}
    return role_ref.get("kind", "?"), role_ref.get("name", "?")


def _find_role(
    role_kind: str, role_name: str, roles: list[dict],
) -> dict | None:
    for r in roles:
        if r.get("metadata", {}).get("name") == role_name and (
            (role_kind == "ClusterRole" and r.get("kind") == "ClusterRole")
            or (role_kind == "Role" and r.get("kind") == "Role")
        ):
            return r
    return None


def _render_subject_report(
    subject: str, roles: list[dict], bindings: list[dict],
    namespace: str | None,
) -> str:
    lines: list[str] = [f"## RBAC analysis: subject = '{subject}'"]

    matched_bindings: list[dict] = []
    for b in bindings:
        for s in (b.get("subjects") or []):
            if _subject_matches(s, subject):
                matched_bindings.append(b)
                break

    if not matched_bindings:
        lines.append(
            "(no RoleBinding / ClusterRoleBinding references this subject)"
        )
        return "\n".join(lines)

    scope_label = namespace or "cluster-wide"
    lines.append(f"Scope: {scope_label}")
    lines.append(f"Matched {len(matched_bindings)} binding(s):\n")

    for b in matched_bindings:
        bmeta = b.get("metadata") or {}
        bns = bmeta.get("namespace", "")
        bname = bmeta.get("name", "?")
        kind = b.get("kind", "?")
        role_kind, role_name = _binding_role_ref(b)
        lines.append(
            f"### {kind}/{bns}/{bname} → {role_kind}/{role_name}"
        )
        subjects_block = b.get("subjects") or []
        for s in subjects_block:
            marker = " ←" if _subject_matches(s, subject) else ""
            lines.append(
                f"  subject: {s.get('kind', '?')}:{s.get('name', '?')}"
                f"{(' ns=' + s['namespace']) if s.get('namespace') else ''}{marker}"
            )
        # Expand the referenced role's rules.
        role = _find_role(role_kind, role_name, roles)
        if role is None:
            lines.append(
                f"  (⚠️ referenced {role_kind}/{role_name} not found in scope)"
            )
            continue
        rules = _role_rules(role)
        if not rules:
            lines.append("  (no rules in role)")
            continue
        rule_rows = []
        for r in rules:
            for v in r.get("verbs") or []:
                for res in r.get("resources") or []:
                    for ag in r.get("apiGroups") or [""]:
                        rule_rows.append({
                            "VERB": v,
                            "RESOURCE": res,
                            "APIGROUP": ag or "(core)",
                        })
        lines.append(short_table(
            rule_rows,
            ["VERB", "RESOURCE", "APIGROUP"],
        ))
        # Wildcard risk flag — surfaces cluster-admin / overly broad rules.
        wildcards = [
            r for r in rules
            if "*" in (r.get("verbs") or [])
            or "*" in (r.get("resources") or [])
            or "*" in (r.get("apiGroups") or [])
        ]
        if wildcards:
            lines.append(
                f"  ⚠️ {len(wildcards)} rule(s) in this role use `*` wildcards "
                f"— review for cluster-admin risk"
            )

    return "\n".join(lines)


def _render_rule_report(
    verb: str | None, resource: str | None, api_group: str | None,
    roles: list[dict], bindings: list[dict], namespace: str | None,
) -> str:
    """Reverse lookup: which subjects can perform (verb, resource, apiGroup)?"""
    query_parts = []
    if verb:
        query_parts.append(f"verb={verb}")
    if resource:
        query_parts.append(f"resource={resource}")
    if api_group is not None:
        query_parts.append(f"apiGroup={api_group or '(core)'}")
    if namespace:
        query_parts.append(f"namespace={namespace}")
    header = " / ".join(query_parts) if query_parts else "(all rules)"
    lines: list[str] = [f"## RBAC analysis: who can {header}?"]

    matched_roles: list[tuple[dict, list[dict]]] = []
    for r in roles:
        matched_rules = [
            rule for rule in _role_rules(r)
            if _rule_matches(rule, verb, resource, api_group)
        ]
        if matched_rules:
            matched_roles.append((r, matched_rules))

    if not matched_roles:
        lines.append("(no role grants this action in scope)")
        return "\n".join(lines)

    # For each matched role, find bindings that reference it.
    rows: list[dict] = []
    for role, rules in matched_roles:
        rname = role.get("metadata", {}).get("name", "?")
        rkind = role.get("kind", "?")
        rns = role.get("metadata", {}).get("namespace", "")
        for b in bindings:
            role_kind, role_name = _binding_role_ref(b)
            if role_kind != rkind or role_name != rname:
                continue
            # If we're scoping to a namespace, the binding must be in it.
            if namespace is not None:
                bns = (b.get("metadata") or {}).get("namespace")
                if bns != namespace:
                    continue
            bmeta = b.get("metadata") or {}
            for s in (b.get("subjects") or []):
                rows.append({
                    "SUBJECT": f"{s.get('kind', '?')}:{s.get('name', '?')}",
                    "VIA_BINDING": f"{b.get('kind', '?')}/"
                                   f"{bmeta.get('namespace', '')}/"
                                   f"{bmeta.get('name', '?')}",
                    "ROLE": f"{rkind}/{rns}/{rname}",
                    "MATCHED_RULES": str(len(rules)),
                })

    if not rows:
        lines.append(
            f"(no binding references the {len(matched_roles)} matching role(s) "
            f"in scope — the rule exists but is unused)"
        )
        # Still list the matching roles so the agent can investigate.
        lines.append("\nMatching roles (no binding → unreachable):")
        for role, rules in matched_roles:
            rmeta = role.get("metadata") or {}
            lines.append(
                f"  - {role.get('kind', '?')}/"
                f"{rmeta.get('namespace', '')}/{rmeta.get('name', '?')} "
                f"({len(rules)} matching rule(s))"
            )
        return "\n".join(lines)

    lines.append(
        f"Found {len(matched_roles)} role(s) granting this action, "
        f"reached via {len(rows)} binding→subject edge(s):\n"
    )
    lines.append(short_table(rows, ["SUBJECT", "VIA_BINDING", "ROLE", "MATCHED_RULES"]))

    # Wildcard risk flag.
    total_wild = sum(
        1 for _, rules in matched_roles
        for r in rules
        if "*" in (r.get("verbs") or [])
        or "*" in (r.get("resources") or [])
        or "*" in (r.get("apiGroups") or [])
    )
    if total_wild:
        lines.append(
            f"\n⚠️ {total_wild} matching rule(s) use `*` wildcards"
        )
    return "\n".join(lines)


def _render_namespace_report(
    namespace: str, roles: list[dict], bindings: list[dict],
) -> str:
    lines = [f"## RBAC in namespace '{namespace}'"]
    lines.append(f"Roles: {len(roles)}, RoleBindings: {len(bindings)}")

    if not roles and not bindings:
        lines.append("(no RBAC objects in this namespace)")
        return "\n".join(lines)

    if roles:
        lines.append("\n### Roles")
        rows = []
        for r in roles:
            rmeta = r.get("metadata") or {}
            wild = any(
                "*" in (rule.get("verbs") or [])
                or "*" in (rule.get("resources") or [])
                for rule in _role_rules(r)
            )
            rows.append({
                "NAME": rmeta.get("name", "?"),
                "RULES": str(len(_role_rules(r))),
                "WILDCARD": "⚠️" if wild else "",
            })
        lines.append(short_table(rows, ["NAME", "RULES", "WILDCARD"]))

    if bindings:
        lines.append("\n### RoleBindings")
        rows = []
        for b in bindings:
            bmeta = b.get("metadata") or {}
            subjects_str = ", ".join(
                f"{s.get('kind', '?')}:{s.get('name', '?')}"
                for s in (b.get("subjects") or [])
            )
            role_kind, role_name = _binding_role_ref(b)
            rows.append({
                "NAME": bmeta.get("name", "?"),
                "ROLE": f"{role_kind}/{role_name}",
                "SUBJECTS": subjects_str or "(none)",
            })
        lines.append(short_table(rows, ["NAME", "ROLE", "SUBJECTS"]))

    return "\n".join(lines)


def _render_summary_report(
    roles: list[dict], bindings: list[dict],
) -> str:
    n_cluster_roles = sum(1 for r in roles if r.get("kind") == "ClusterRole")
    n_namespaced_roles = sum(1 for r in roles if r.get("kind") == "Role")
    n_cluster_bindings = sum(1 for b in bindings if b.get("kind") == "ClusterRoleBinding")
    n_namespaced_bindings = sum(1 for b in bindings if b.get("kind") == "RoleBinding")

    lines = [
        "## RBAC summary (cluster-wide)",
        f"ClusterRoles: {n_cluster_roles}",
        f"ClusterRoleBindings: {n_cluster_bindings}",
        f"Roles (across all namespaces): {n_namespaced_roles}",
        f"RoleBindings (across all namespaces): {n_namespaced_bindings}",
    ]

    # Surface wildcard-bearing rules — that's the cluster-admin risk surface.
    wild_rules: list[tuple[str, dict]] = []
    for r in roles:
        rmeta = r.get("metadata") or {}
        rname = f"{r.get('kind', '?')}/{rmeta.get('namespace', '')}/{rmeta.get('name', '?')}"
        for rule in _role_rules(r):
            if (
                "*" in (rule.get("verbs") or [])
                or "*" in (rule.get("resources") or [])
                or "*" in (rule.get("apiGroups") or [])
            ):
                wild_rules.append((rname, rule))

    if wild_rules:
        lines.append(
            f"\n⚠️ {len(wild_rules)} rule(s) use `*` wildcards "
            f"(cluster-admin risk surface):"
        )
        for rname, rule in wild_rules[:20]:
            verbs = ", ".join(rule.get("verbs") or [])
            resources = ", ".join(rule.get("resources") or [])
            api_groups = ", ".join(rule.get("apiGroups") or []) or "(core)"
            lines.append(
                f"  - {rname}: verbs=[{verbs}] resources=[{resources}] "
                f"apiGroups=[{api_groups}]"
            )
        if len(wild_rules) > 20:
            lines.append(f"  ... and {len(wild_rules) - 20} more")
    else:
        lines.append("\n(no wildcard-bearing rules — clean RBAC surface)")

    return "\n".join(lines)


def register(mcp) -> None:
    mcp.tool()(create_role)
    mcp.tool()(create_rolebinding)
    mcp.tool()(create_clusterrole)
    mcp.tool()(create_clusterrolebinding)
    mcp.tool()(whoami)
    mcp.tool()(analyze_rbac)
